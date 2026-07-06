"""Converts messages posted in a linked Profile Room into ActivityPub
``Create`` activities and distributes them to the local actor's followers.

Triggered for every ``m.room.message`` AppService transaction event that
wasn't a bot command (see ``bridge.commands``) and wasn't already handled as
a reply to a previously-federated post (see ``bridge.reply_bridge``) -- i.e.
fresh, top-level posts made by a Matrix user in their own linked Profile Room.

Media (images/video/audio/files) is attached to the Note as a link through
the bridge's own ``/media/{server}/{id}`` proxy (see
``bridge.activitypub.routes.get_media``) rather than Synapse's media API
directly -- Matrix media downloads require an access token (MSC3916) that
remote fediverse servers don't have. The bridge never stores media itself,
per the project's data-sovereignty constraint; it only proxies. Profile
Rooms must be unencrypted for this to work -- an encrypted room sends
``m.room.encrypted`` events the ghosted bridge users can't decrypt, which
this module (matching on ``m.room.message``) naturally never sees.

``maybe_handle_topic_change``/``maybe_handle_room_name_change``/
``maybe_handle_room_avatar_change`` are the other things this module watches
for in a Profile Room: its ``m.room.topic``/``m.room.name``/``m.room.avatar``
double as the local actor's ActivityPub bio/display name/avatar -- room
creation no longer seeds a placeholder topic (see ``bridge.commands``'s
``create profile``/``replace room``), so these are genuinely theirs to set,
and whatever they're set to here is kept in sync one-way (Matrix -> AP)
automatically: both in ``ActorRepository`` (so a fresh fetch of the actor
document already reflects it) and pushed out as a signed ``Update`` to every
follower (so an already-cached copy on their server actually refreshes too,
rather than silently going stale until whenever that server next decides to
re-fetch it on its own).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import Request

from bridge.activitypub.models import AS_PUBLIC, Activity, Note
from bridge.activitypub.sanitize import plain_text_to_note_html
from bridge.activitypub.urls import actor_url, followers_url, main_key_id, media_url
from bridge.commands import message_addresses_bot
from bridge.media import build_ap_attachment, media_caption
from bridge.mentions import resolve_pill_mentions, resolve_plaintext_mentions
from bridge.note_mirroring import deliver_to_actor_or_followers, push_profile_update
from bridge.reaction_bridge import send_boost
from bridge.repository import FederatedEvent

logger = logging.getLogger(__name__)


async def _maybe_boost_forwarded_post(
    request: Request, *, actor_record, room_id: str, matrix_event_id: str, external_url: str
) -> bool:
    """Turn a mirrored fediverse post FORWARDED into the owner's Profile Room
    (Element's "forward message", which copies the source event's content
    verbatim into the target room) into a boost of the original post.

    Detection is ``external_url``: the bridge stamps it on every mirrored
    fediverse post (see ``bridge.note_mirroring.source_post_url``), no
    Matrix client puts one on a hand-typed message, and a forward copies it
    along with everything else -- so a Profile Room message carrying one is
    the owner putting someone else's mirrored post on their own timeline.
    Fediverse-wise that act is an ``Announce`` (boost) of the original --
    NOT authorship of a byte-identical new post, which is what falling
    through to the ordinary fresh-post path would publish in their name.

    Returns True if this was handled as a boost -- ``send_boost`` does the
    full job (delivers the Announce, posts the standard "\U0001F501 X
    boosted Y's post" card, records the ``ReactionRecord``), with THIS
    forwarded event recorded as the boost's trigger, so redacting the
    forward un-boosts exactly like redacting a boost reaction/command
    would. Returns False to let the caller fall through to the ordinary
    fresh-post path: the URL doesn't resolve to any post we track, or the
    tracked post lives in a private DM/Chat room -- a boost would publicly
    ``Announce`` an object our own serving route (correctly) refuses to
    serve (see ``bridge.activitypub.routes.get_note``'s privacy check), so
    the forward stays what it always was, content the owner chose to
    repost as their own.
    """
    repository = request.app.state.repository

    if await repository.get_reaction_by_matrix_event(matrix_event_id) is not None:
        return True  # redelivered transaction -- this exact forward already boosted

    parent = await repository.get_federated_event_by_ap_object(external_url)
    if parent is None:
        # external_url is the post's human permalink (source_post_url
        # prefers the Note's `url` over its bare `id`), but tracking is
        # keyed by AP object id -- when they differ, content-negotiating
        # the permalink (Pleroma/Mastodon/Misskey all serve or redirect it
        # as activity+json) recovers the id to look up by.
        try:
            response = await request.app.state.http_client.get(
                external_url, headers={"Accept": "application/activity+json"}
            )
            obj = response.json() if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            obj = None
        obj_id = obj.get("id") if isinstance(obj, dict) else None
        if isinstance(obj_id, str) and obj_id and obj_id != external_url:
            parent = await repository.get_federated_event_by_ap_object(obj_id)
    if parent is None:
        return False

    if await repository.is_ghost_dm_room(parent.room_id) or await repository.is_ghost_chat_room(parent.room_id):
        return False

    await send_boost(
        request,
        actor_record=actor_record,
        parent=parent,
        matrix_event_id=matrix_event_id,
        room_id=room_id,
        reactor_matrix_user_id=actor_record.matrix_user_id,
    )
    return True


async def maybe_distribute_profile_post(request: Request, event: dict) -> bool:
    """Returns True if this event belonged to a linked Profile Room (handled,
    successfully distributed or not) -- callers shouldn't process it further."""
    if event.get("type") != "m.room.message":
        return False

    content = event.get("content") or {}
    if message_addresses_bot(content, request.app.state.config):
        # bridge.commands.maybe_handle_command runs earlier in the dispatch
        # chain and normally intercepts anything tagging the bot before we're
        # ever even reached -- this is a second, independent guarantee of
        # our own: whatever happens upstream, a message addressed to the bot
        # must never be mistaken for ordinary post content and federated out
        # to followers. Bot bookkeeping stays in Matrix, full stop.
        return True

    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        # bridge.edit_bridge runs earlier in the dispatch chain and
        # consumes every edit -- this is a second, independent guarantee
        # of our own (same pattern as the bot-address check above): an
        # edit event's "* ..." fallback body must never federate as a
        # fresh post, which is exactly what happened before edit handling
        # existed (2026-07-04).
        return True

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    config = request.app.state.config
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room
    room_owner = actor_record
    if actor_record.matrix_user_id != sender:
        bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
        if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
            return False  # the bot's / a ghost's own messages are never guest posts
        guest_record = await repository.get_local_actor_by_matrix_id(sender)
        if guest_record is None:
            return False  # a Matrix user with no linked fediverse profile -- stays Matrix-only, as ever
        # A DIFFERENT local user with a linked profile posting in someone
        # else's Profile Room (user-requested, 2026-07-04): federate it as
        # the GUEST's own post -- minted under their actor, signed by them,
        # on their outbox -- with the room owner's mention tacked on the
        # front (below), the nearest fediverse equivalent of walking into
        # someone's room to talk to them. Without this, the message stayed
        # Matrix-only, so the owner's later on-Matrix replies to it
        # federated as fresh context-free posts instead of replies.
        actor_record = guest_record

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed (e.g. a redelivered transaction) -- nothing to do

    # A forwarded mirrored post becomes a boost of the original, not a new
    # post published in the owner's name -- see _maybe_boost_forwarded_post.
    external_url = content.get("external_url")
    if isinstance(external_url, str) and external_url:
        if await _maybe_boost_forwarded_post(
            request,
            actor_record=actor_record,
            room_id=room_id,
            matrix_event_id=matrix_event_id,
            external_url=external_url,
        ):
            return True

    raw_body = (content.get("body") or "").strip()
    base = config.bridge.public_base_url
    attachment = build_ap_attachment(base, content)
    # A media message's `body` is its real caption only under the caption
    # convention (separate differing `filename` -- how Element X posts an
    # image with text); otherwise it's just the filename, not post text.
    body = media_caption(content) if attachment is not None else raw_body
    if not body and attachment is None:
        return False

    if attachment is not None:
        await repository.mark_media_published(content["url"])

    # A Matrix mention of a ghost user (a fediverse account mirrored here)
    # should read as a real fediverse mention on the other end, not the
    # ghost's Matrix display name or raw MXID -- see bridge.mentions. A
    # remote handle typed out by hand (never touching m.mentions at all)
    # needs its own, separate resolution pass.
    mention_tags: list[dict] = []
    mention_cc: list[str] = []
    if body:
        body, mention_tags, mention_cc = await resolve_pill_mentions(request, body, content)
        already_tagged = {tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")}
        plaintext_tags, plaintext_cc = await resolve_plaintext_mentions(request, body, already_tagged=already_tagged)
        mention_tags += plaintext_tags
        mention_cc += plaintext_cc

    if actor_record is not room_owner:
        # Guest post: tack the room owner's mention onto the front (unless
        # the guest already mentioned them themselves -- no double-tagging,
        # same rule as reply participant carryover), so the fediverse side
        # reads it as directed at the profile's owner and notifies them.
        owner_actor_id = actor_url(base, room_owner.username)
        owner_handle = f"@{room_owner.username}@{config.bridge.domain}"
        already_mentioned = owner_actor_id in {tag.get("href") for tag in mention_tags} or owner_handle in body
        if not already_mentioned:
            body = f"{owner_handle} {body}".strip()
            mention_tags.insert(0, {"type": "Mention", "href": owner_actor_id, "name": owner_handle})
            mention_cc.insert(0, owner_actor_id)

    # Real fediverse clients write a mention's <a> into their post's HTML
    # themselves; ours builds content from a plain Matrix body instead, so
    # it has to add that anchor itself -- see plain_text_to_note_html.
    mention_links = {tag["name"]: tag["href"] for tag in mention_tags if tag.get("name") and tag.get("href")}

    note_id = f"{actor_url(base, actor_record.username)}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    note = Note(
        id=note_id,
        attributed_to=actor_url(base, actor_record.username),
        content=plain_text_to_note_html(body, mention_links) if body else "",
        published=published,
        to=[AS_PUBLIC],
        cc=[followers_url(base, actor_record.username), *mention_cc],
        attachment=[attachment] if attachment else [],
        tag=mention_tags,
    )
    create_activity = Activity(
        id=f"{note_id}/activity",
        type="Create",
        actor=actor_url(base, actor_record.username),
        object=note,
        published=published,
        to=note.to,
        cc=note.cc,
    )

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id,
            room_id=room_id,
            ap_object_id=note_id,
            author_actor_id=actor_url(base, actor_record.username),
        )
    )

    followers = await repository.list_followers(actor_record.username)
    # A mentioned account isn't necessarily a follower -- without delivering
    # to it directly too, it would never actually receive (and so never be
    # notified of) a post that only reaches people already following this
    # actor. dict.fromkeys de-dupes while preserving order, for anyone who's
    # both. deliver_to_actor_or_followers handles a recipient being ANOTHER
    # LOCAL bridge user (a guest post's mention of the room owner, most
    # notably): their real followers get the delivery instead of a useless
    # "no inbox" warning -- the same convention bridge.reply_bridge already
    # uses for exactly this situation. Best-effort throughout, never raises.
    recipients = list(dict.fromkeys([*followers, *mention_cc]))
    for recipient_actor_id in recipients:
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=recipient_actor_id,
            activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )

    return True


async def maybe_handle_topic_change(request: Request, event: dict) -> bool:
    """Returns True if this event was an ``m.room.topic`` change in a linked
    Profile Room (handled -- keeps the local actor's AP ``summary`` in sync
    with it, and pushes the change out to followers -- see
    ``push_profile_update``). Matrix's own power levels already gated who
    could set the topic in the first place, so this doesn't separately
    check who sent it -- same trust level as the room's name/avatar, which
    anyone sufficiently-powered in the room can already set."""
    if event.get("type") != "m.room.topic":
        return False
    content = event.get("content") or {}
    topic = content.get("topic")
    if not isinstance(topic, str):
        return False

    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room

    if actor_record.summary != topic:
        actor_record = dataclasses.replace(actor_record, summary=topic)
        await repository.register_local_actor(actor_record)
        await push_profile_update(request, actor_record)
    return True


async def maybe_handle_room_name_change(request: Request, event: dict) -> bool:
    """Returns True if this event was an ``m.room.name`` change in a linked
    Profile Room (handled -- keeps the local actor's AP display name in
    sync with it, and pushes the change out to followers). Same trust
    model as ``maybe_handle_topic_change``."""
    if event.get("type") != "m.room.name":
        return False
    content = event.get("content") or {}
    name = content.get("name")
    if not isinstance(name, str) or not name:
        return False

    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room

    if actor_record.display_name != name:
        actor_record = dataclasses.replace(actor_record, display_name=name)
        await repository.register_local_actor(actor_record)
        await push_profile_update(request, actor_record)
    return True


async def maybe_handle_room_avatar_change(request: Request, event: dict) -> bool:
    """Returns True if this event was an ``m.room.avatar`` change in a
    linked Profile Room (handled -- keeps the local actor's AP avatar in
    sync with it, and pushes the change out to followers). Same trust
    model as ``maybe_handle_topic_change``.

    The new avatar is published through the bridge's own media proxy (see
    ``bridge.activitypub.routes.get_media``), same as an avatar set at
    ``create profile``/``link profile`` time -- a remote fediverse server
    has no access token to fetch Matrix media directly, and the mxc URI
    has to be added to the publish allowlist before the proxy will ever
    serve it (an unpublished mxc:// isn't otherwise exposed)."""
    if event.get("type") != "m.room.avatar":
        return False
    content = event.get("content") or {}
    avatar_mxc = content.get("url")
    if not isinstance(avatar_mxc, str) or not avatar_mxc:
        return False

    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room

    config = request.app.state.config
    try:
        icon_url = media_url(config.bridge.public_base_url, avatar_mxc)
    except ValueError:
        logger.info("Room avatar %r for %s is not an mxc:// URI; ignoring", avatar_mxc, room_id)
        return True

    if actor_record.icon_url != icon_url:
        await repository.mark_media_published(avatar_mxc)
        actor_record = dataclasses.replace(actor_record, icon_url=icon_url)
        await repository.register_local_actor(actor_record)
        await push_profile_update(request, actor_record)
    return True
