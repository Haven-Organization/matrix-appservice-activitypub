"""Federates Matrix replies/thread-replies (in Remote User Rooms or Profile
Rooms) out to the fediverse post they're responding to.

Triggered for every ``m.room.message`` event the AppService receives: if the
event is a reply -- a rich reply or a thread reply, Matrix's two relation
shapes -- to a Matrix event that mirrors, or is itself chained from, a
fediverse post (tracked via ``ActorRepository.record_federated_event``),
this builds a ``Create{Note}`` activity addressed to that post's author and
delivers it to their inbox, signed with the replying Matrix user's own
linked Profile Room actor. Media attachments are handled exactly like
``bridge.profile_posts`` (via the same ``build_ap_attachment``), so a reply
that's a photo/video/audio/file -- with or without accompanying text --
attaches it rather than silently dropping it.

The reply's *immediate* Matrix parent might be one of our OWN local actors'
earlier messages -- most commonly, continuing your own thread by replying
to your own prior reply, but also just as validly a DIFFERENT local bridge
user's post (e.g. replying inside their Profile Room, which you can be a
member of without being its owner). Only the "continuing your own thread"
case has no fediverse party to deliver to via that immediate parent:
self-delivering would just loop the activity back into our own inbound
handler, which refuses to do anything with it (see
``bridge.note_mirroring.resolve_and_invite_ghost``'s docstring). So the
actual delivery target is resolved separately -- walking up to the thread
root's author only when the immediate parent's author IS the sender
themselves -- while ``inReplyTo`` still correctly references the immediate
parent either way. A DIFFERENT local user's post is treated exactly like a
remote one: the reply is addressed to them and delivered to THEIR real
followers (see ``deliver_to_actor_or_followers``), not self-delivered,
since they already see it live in Matrix and have no inbox of their own
worth POSTing to.

A message sent in a ghost DM room (``bridge.note_mirroring.mirror_direct_message``)
that ISN'T a reply at all still goes out -- as a fresh, unthreaded message to
that same conversation partner (see ``_send_outbound_dm``) rather than being
dropped or rejected, since the room only ever has one fediverse party to
address it to regardless of whether a specific message was picked to reply
to. A notice tells the sender it went out that way (not as a reply), so they
know to reply directly to a specific message next time if they want it
threaded there instead.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request

from bridge.activitypub.models import AS_PUBLIC, Activity, Note
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.sanitize import plain_text_to_note_html, strip_reply_fallback
from bridge.activitypub.urls import actor_url, followers_url, main_key_id, username_from_actor_url
from bridge.commands import is_third_party_still_allowed, message_addresses_bot
from bridge.media import build_ap_attachment, media_caption
from bridge.mentions import collect_reply_participants, resolve_pill_mentions, resolve_plaintext_mentions
from bridge.note_mirroring import deliver_to_actor_or_followers
from bridge.repository import FederatedEvent

logger = logging.getLogger(__name__)


def _effective_ap_object_id(fe: FederatedEvent) -> str:
    """The AP object id a reply's ``inReplyTo`` should actually name.

    For an ordinary mirrored post/reply this is just its own
    ``ap_object_id`` -- but for a mirrored repost (an inbound ``Announce``),
    ``ap_object_id`` deliberately names the Announce *activity itself* (see
    ``FederatedEvent``'s docstring), which isn't a real Note/Object at all;
    naming it in ``inReplyTo`` produces a reply most fediverse software
    can't properly thread (or even accept) since it doesn't resolve to an
    actual post. ``reposted_object_id`` is the real reposted Note's own id.
    """
    return fe.reposted_object_id or fe.ap_object_id


def _effective_author_actor_id(fe: FederatedEvent) -> str:
    """The actor a reply should actually be addressed/delivered to.

    Same reasoning as ``_effective_ap_object_id``: a mirrored repost's own
    ``author_actor_id`` is the reposter, who merely reshared the post --
    replying should reach the post's actual original author instead.
    """
    return fe.reposted_author_actor_id or fe.author_actor_id


async def _resolve_remote_recipient(
    request: Request, parent: FederatedEvent, *, sender_username: str
) -> FederatedEvent | None:
    """Find who a reply should actually be addressed/delivered to.

    Returns ``parent`` itself unless its (effective -- see
    ``_effective_author_actor_id``) author is ``sender_username``
    THEMSELVES -- i.e. they're continuing their own earlier thread. That's
    the only case with no fediverse party to reach via this specific
    parent: a genuinely remote author is obviously a real recipient, but so
    is a DIFFERENT local bridge user -- they have real fediverse followers
    of their own who need this reply delivered to them (see
    ``bridge.note_mirroring.deliver_to_actor_or_followers``), exactly as if
    they were a remote account, even though the person themselves already
    sees it live in Matrix and needs no delivery for their own sake.

    When it IS the sender's own earlier post, walks up to the thread
    root's author instead -- almost always the actual other party the
    thread is with -- and returns None only if that's ALSO the sender
    themselves (or untracked), meaning this is an entirely local
    self-thread with no other party to reach at all.
    """
    base = request.app.state.config.bridge.public_base_url
    parent_author_username = username_from_actor_url(base, _effective_author_actor_id(parent))
    if parent_author_username != sender_username:
        return parent  # remote, or a DIFFERENT local bridge user -- both are real recipients

    if not parent.thread_root_event_id or parent.thread_root_event_id == parent.event_id:
        return None  # no further ancestor -- this is a fully local self-thread

    repository = request.app.state.repository
    root = await repository.get_federated_event_by_matrix_event(parent.thread_root_event_id)
    if root is None:
        return None
    root_author_username = username_from_actor_url(base, _effective_author_actor_id(root))
    if root_author_username == sender_username:
        return None  # the root is also the sender's own -- nothing else to reach

    return root


async def derive_in_reply_to(repository, content: dict) -> str | None:
    """Re-derive the ``inReplyTo`` the original delivery sent for an
    already-mirrored reply, from its Matrix event ``content`` alone.

    Exists for the AP serving routes (``bridge.activitypub.routes``'
    ``get_note``/outbox): they reconstruct a Note fresh from its Matrix
    event, and a reconstruction without ``inReplyTo`` reads as a standalone
    post to any instance that learns of the post by FETCHING it rather
    than receiving the delivered ``Create`` -- confirmed live (2026-07-03)
    as a reply threading correctly on an instance that got the delivery
    but rendering standalone on one that dereferenced it. Mirrors exactly
    how ``maybe_federate_reply`` resolved the parent at send time
    (including the thread-root fallback for an untracked immediate
    parent), so the served copy can't disagree with what was delivered.
    """
    target_event_id = _extract_reply_target_event_id(content)
    if not target_event_id:
        return None
    parent = await repository.get_federated_event_by_matrix_event(target_event_id)
    if parent is None:
        relates_to = content.get("m.relates_to") or {}
        if relates_to.get("rel_type") == "m.thread":
            root_event_id = relates_to.get("event_id")
            if root_event_id and root_event_id != target_event_id:
                parent = await repository.get_federated_event_by_matrix_event(root_event_id)
    if parent is None:
        return None
    return _effective_ap_object_id(parent)


def _extract_reply_target_event_id(content: dict) -> str | None:
    """Find the event being replied to, covering both Matrix relation shapes.

    Rich replies put it directly at ``m.relates_to.m.in_reply_to.event_id``.
    Thread replies use ``rel_type: m.thread`` with their own nested
    ``m.in_reply_to`` pointing at the specific event being replied to within
    the thread (falling back to the thread root for clients that omit it).
    """
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") == "m.thread":
        in_reply_to = relates_to.get("m.in_reply_to") or {}
        return in_reply_to.get("event_id") or relates_to.get("event_id")
    in_reply_to = relates_to.get("m.in_reply_to") or {}
    return in_reply_to.get("event_id")


async def _send_outbound_dm(
    request: Request,
    *,
    event: dict,
    content: dict,
    room_id: str,
    actor_record,
    recipient_actor_id: str,
    in_reply_to: str | None,
    thread_root_event_id: str | None,
) -> None:
    """Build, deliver, and record a ``Create{Note}`` addressed privately to
    ``recipient_actor_id`` ONLY (never ``AS_PUBLIC``) -- shared by both a
    genuine reply within a ghost DM room and a fresh, unthreaded message in
    one (see ``maybe_federate_reply``), which differ only in whether the
    resulting Note has an ``inReplyTo`` and what ``thread_root_event_id`` the
    recorded ``FederatedEvent`` inherits. Mirrors the equivalent portion of
    ``maybe_federate_reply``'s own public-reply handling -- kept separate
    (rather than folded into one generic function covering both) since the
    public case's ``to``/``cc`` addressing and recipient resolution are
    different enough that sharing would need more branching than it'd save.

    ``content``'s HTML wrapping (``plain_text_to_note_html`` -- every other
    outbound Note, DM or public, gets this) assumes a Mastodon-family
    recipient that renders ``content`` as sanitized HTML. Confirmed live
    2026-07-14: Shoot (github.com/MaddyUnderStars/shoot) stores AND
    displays a message's ``content`` completely raw, no HTML parsing at
    all (its own transformer just does ``content: note.content``/
    ``content: message.content`` in both directions, verbatim) -- so the
    ``<p>...</p>`` tags show up as literal, visible text in its UI instead
    of ever being rendered as paragraphs. Detected via NodeInfo (see
    ``bridge.activitypub.nodeinfo.remote_software_name`` -- nothing in a
    Shoot actor DOCUMENT itself is distinguishable from an ordinary
    Mastodon ``Person``), and only applied here (DM/Chat delivery to a
    single known fediverse party) -- ordinary public replies keep the HTML
    convention unconditionally, since Shoot's own channel/guild model
    doesn't interact with that path at all yet (see GitHub issue #3).
    """
    repository = request.app.state.repository
    config = request.app.state.config
    base = config.bridge.public_base_url

    attachment = build_ap_attachment(base, content)
    # A media message's `body` is its real caption only under the caption
    # convention (separate differing `filename`); otherwise it's just the
    # filename, not message text -- see bridge.media.media_caption.
    raw_body = strip_reply_fallback(content.get("body") or "")
    body = strip_reply_fallback(media_caption(content)) if attachment is not None else raw_body
    if not body and attachment is None:
        return

    if attachment is not None:
        await repository.mark_media_published(content["url"])

    # A Matrix mention of a ghost user should read as a real fediverse
    # mention on the other end -- see bridge.mentions. A remote handle typed
    # out by hand (never touching m.mentions at all) needs its own,
    # separate resolution pass.
    mention_tags: list[dict] = []
    mention_cc: list[str] = []
    if body:
        body, mention_tags, mention_cc = await resolve_pill_mentions(request, body, content)
        already_tagged = {tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")}
        plaintext_tags, plaintext_cc = await resolve_plaintext_mentions(request, body, already_tagged=already_tagged)
        mention_tags += plaintext_tags
        mention_cc += plaintext_cc
    mention_links = {tag["name"]: tag["href"] for tag in mention_tags if tag.get("name") and tag.get("href")}

    note_id = f"{actor_url(base, actor_record.username)}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    recipient_domain = urlsplit(recipient_actor_id).hostname or ""
    software = await remote_software_name(request, recipient_domain) if recipient_domain else None
    # See this function's own docstring -- Shoot renders `content` completely
    # raw, so the usual HTML wrapping shows up as literal tag text there.
    content_html = body if software == "shoot" else plain_text_to_note_html(body, mention_links)

    note = Note(
        id=note_id,
        attributed_to=actor_url(base, actor_record.username),
        content=content_html if body else "",
        published=published,
        to=[recipient_actor_id],
        cc=mention_cc,
        in_reply_to=in_reply_to,
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

    await deliver_to_actor_or_followers(
        request,
        target_actor_id=recipient_actor_id,
        activity=create_activity.to_dict(),
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )
    # Someone else mentioned in the text isn't necessarily the DM partner,
    # and has no other route to receive this activity -- deliver to them
    # directly too, or the mention would never actually reach them.
    for mention_actor_id in dict.fromkeys(mention_cc):
        if mention_actor_id == recipient_actor_id:
            continue
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=mention_actor_id,
            activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )

    matrix_event_id = event.get("event_id")
    if matrix_event_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=matrix_event_id,
                room_id=room_id,
                ap_object_id=note_id,
                author_actor_id=actor_url(base, actor_record.username),
                thread_root_event_id=thread_root_event_id,
            )
        )


async def maybe_federate_reply(request: Request, event: dict) -> bool:
    """Returns True if this event was a reply to a federated post (handled,
    successfully or not) -- callers should not also treat it as a fresh post."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}

    if message_addresses_bot(content, request.app.state.config):
        # bridge.commands.maybe_handle_command runs earlier in the dispatch
        # chain and normally intercepts anything tagging the bot before
        # we're ever even reached -- this is a second, independent
        # guarantee of our own: a reply that's actually addressed to the
        # bot (e.g. tagging it while replying in a thread) must never be
        # mistaken for a real reply and federated out. Bot bookkeeping
        # stays in Matrix, full stop.
        return True

    config = request.app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return False  # never re-federate our own ghosts'/bot's own messages

    repository = request.app.state.repository
    target_event_id = _extract_reply_target_event_id(content)
    if not target_event_id:
        # A ghost DM room (bridge.note_mirroring.mirror_direct_message) has
        # no reply target to derive `inReplyTo` from -- but unlike a Profile
        # Room it also isn't its own local identity with a public outbox to
        # post a fresh Create from either. Every message in it is inherently
        # part of one specific fediverse DM conversation, so this still
        # sends it -- just as a fresh, unthreaded message to that same
        # conversation partner rather than dropping it -- and tells the
        # sender it went out that way instead of as a reply, so they know to
        # reply directly to a specific message next time if they want it
        # threaded there instead.
        if not await repository.is_ghost_dm_room(room_id):
            return False

        recipient_actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
        if recipient_actor_id is None:
            return False  # shouldn't happen, but nothing to address this to

        actor_record = await repository.get_local_actor_by_matrix_id(sender)
        if actor_record is None:
            try:
                await request.app.state.synapse.send_message_event(
                    room_id,
                    {
                        "msgtype": "m.notice",
                        "body": f"This message wasn't sent to the fediverse: link a profile first by "
                        f'tagging me with "{bot_mxid} link profile".',
                    },
                    as_user_id=bot_mxid,
                )
            except Exception:
                logger.warning("Failed to send link-profile notice to %s", room_id, exc_info=True)
            return True

        if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
            return True

        await _send_outbound_dm(
            request,
            event=event,
            content=content,
            room_id=room_id,
            actor_record=actor_record,
            recipient_actor_id=recipient_actor_id,
            in_reply_to=None,
            thread_root_event_id=None,
        )

        # This notice exists to steer a Mastodon/Pleroma-style DM partner
        # toward proper inReplyTo threading, which THEIR own UI benefits
        # from -- but Shoot's chat UI has no equivalent concept at all
        # (every message is just an ordinary sequential chat message, never
        # "should have been a reply"), so for a Shoot recipient specifically
        # this fired on every single message with nothing to actually warn
        # about (confirmed live 2026-07-14 -- a normal back-and-forth chat
        # produced this notice constantly). Same NodeInfo detection as
        # _send_outbound_dm's own content-format check.
        recipient_domain = urlsplit(recipient_actor_id).hostname or ""
        software = await remote_software_name(request, recipient_domain) if recipient_domain else None
        if software != "shoot":
            try:
                offending_event_id = event.get("event_id")
                notice_content: dict = {
                    "msgtype": "m.notice",
                    "body": "Your message was sent to the fediverse as a new direct message, not a "
                    "reply -- reply directly to a specific message next time to keep it in that thread.",
                }
                # Same reasoning as the link-profile notice above: a real reply
                # to the message it's talking about, so it's obvious which one.
                if offending_event_id:
                    notice_content["m.relates_to"] = {"m.in_reply_to": {"event_id": offending_event_id}}
                await request.app.state.synapse.send_message_event(room_id, notice_content, as_user_id=bot_mxid)
            except Exception:
                logger.warning("Failed to send new-DM notice to %s", room_id, exc_info=True)
        return True

    parent = await repository.get_federated_event_by_matrix_event(target_event_id)
    if parent is None:
        # The immediate parent (e.g. an earlier reply in the same thread)
        # might itself be untracked -- it may predate some change to what we
        # track, or federating it may have failed or been refused for its
        # own reasons. A Matrix thread reply also carries the thread's ROOT
        # event id (`m.relates_to.event_id`, distinct from the specific
        # ancestor in `m.in_reply_to`) -- falling back to it, if IT is
        # tracked, means a thread doesn't permanently stop federating just
        # because one link in the middle has no record. It's also the more
        # correct target regardless: the untracked immediate parent was
        # never published as an AP object of its own, so there'd be nothing
        # valid to thread `inReplyTo` against even if we could find it.
        relates_to = content.get("m.relates_to") or {}
        if relates_to.get("rel_type") == "m.thread":
            root_event_id = relates_to.get("event_id")
            if root_event_id and root_event_id != target_event_id:
                parent = await repository.get_federated_event_by_matrix_event(root_event_id)
        if parent is None:
            return False  # replying to a purely-local Matrix message; nothing to federate

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        try:
            await request.app.state.synapse.send_message_event(
                room_id,
                {
                    "msgtype": "m.notice",
                    "body": f"This reply wasn't sent to the fediverse: link a profile first by "
                    f'tagging me with "{bot_mxid} link profile".',
                },
                as_user_id=bot_mxid,
            )
        except Exception:
            logger.warning("Failed to send link-profile notice to %s", room_id, exc_info=True)
        return True

    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return True

    recipient = await _resolve_remote_recipient(request, parent, sender_username=actor_record.username)
    is_dm_room = await repository.is_ghost_dm_room(room_id)
    if recipient is None and is_dm_room:
        # Unlike a public thread, a DM room has exactly one fixed fediverse
        # party (see bridge.repository's ghost_dm_rooms), known regardless
        # of who authored the ancestor chain -- there's no "self-thread,
        # nothing to reach" case here the way there is publicly.
        # _resolve_remote_recipient returning None just means every tracked
        # ancestor up to the root happened to be the sender's OWN earlier
        # messages, which is the completely ordinary case of continuing
        # your own side of an active DM (e.g. a follow-up thought before
        # the other party has replied within this specific thread yet) --
        # confirmed live (2026-07-03) that this was silently dropping such
        # replies instead of delivering them to the room's own known
        # partner. Falls through to the same `_send_outbound_dm` call the
        # "not a reply at all" DM case already uses, just with this reply's
        # actual `in_reply_to`/`thread_root_event_id` instead of None/None.
        recipient_actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
        if recipient_actor_id is None:
            return True  # shouldn't happen, but nothing to address this to
        reply_cc_target = recipient_actor_id
    elif recipient is None:
        # A public self-thread: no remote or different-local-user party to
        # reach via the parent or its thread root, but our own followers --
        # already watching this thread publicly -- should still see it
        # continue rather than the thread just silently stopping for them.
        # Addressing `deliver_to_actor_or_followers` at our OWN actor id
        # resolves to exactly our own real followers (see its docstring),
        # the same as an ordinary top-level post, rather than self-POSTing
        # to our own inbox -- so, like a reply to a genuinely different
        # local user, this can never loop back into our own inbound handler
        # the way the local-actor self-follow redaction storm did.
        recipient_actor_id = actor_url(config.bridge.public_base_url, actor_record.username)
        reply_cc_target = followers_url(config.bridge.public_base_url, actor_record.username)
    else:
        recipient_actor_id = _effective_author_actor_id(recipient)
        reply_cc_target = recipient_actor_id

    # A reply sent in a ghost DM room (see bridge.note_mirroring's
    # mirror_direct_message) is itself a continuation of that same private
    # conversation -- it must go out addressed only to the specific
    # recipient, never to AS_PUBLIC, or the reply (and by extension the
    # DM's existence) would be visible to anyone able to see the sender's
    # public posts, defeating the whole point of it being a direct message.
    if is_dm_room:
        await _send_outbound_dm(
            request,
            event=event,
            content=content,
            room_id=room_id,
            actor_record=actor_record,
            recipient_actor_id=recipient_actor_id,
            in_reply_to=_effective_ap_object_id(parent),
            thread_root_event_id=parent.thread_root_event_id or parent.event_id,
        )
        return True

    base = config.bridge.public_base_url
    attachment = build_ap_attachment(base, content)
    # A media message's `body` is its real caption only under the caption
    # convention (separate differing `filename`); otherwise it's just the
    # filename, not reply text -- see bridge.media.media_caption.
    raw_body = strip_reply_fallback(content.get("body") or "")
    body = strip_reply_fallback(media_caption(content)) if attachment is not None else raw_body
    if not body and attachment is None:
        return True

    if attachment is not None:
        await repository.mark_media_published(content["url"])

    # A Matrix mention of a ghost user should read as a real fediverse
    # mention on the other end -- see bridge.mentions. A remote handle typed
    # out by hand (never touching m.mentions at all) needs its own,
    # separate resolution pass.
    mention_tags: list[dict] = []
    mention_cc: list[str] = []
    if body:
        body, mention_tags, mention_cc = await resolve_pill_mentions(request, body, content)
        already_tagged = {tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")}
        plaintext_tags, plaintext_cc = await resolve_plaintext_mentions(request, body, already_tagged=already_tagged)
        mention_tags += plaintext_tags
        mention_cc += plaintext_cc

    # Replying to a post keeps the whole conversation on the thread (see
    # collect_reply_participants) -- a Mention for the parent's author,
    # then the parent's other participants, minus the sender themselves
    # and anyone their own text already tags. Runs regardless of `body`:
    # a media-only reply is just as much part of the conversation.
    participant_tags, participant_cc = await collect_reply_participants(
        request,
        _effective_ap_object_id(parent),
        exclude_actor_ids={
            actor_url(base, actor_record.username),
            *(tag["href"] for tag in mention_tags if tag.get("href")),
        },
        already_tagged={tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")},
    )
    mention_tags += participant_tags
    mention_cc += participant_cc

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
        # Deduped: someone can arrive here twice over -- e.g. as the reply
        # target AND a typed mention, or via the parent's participant list.
        cc=list(dict.fromkeys([reply_cc_target, *mention_cc])),
        in_reply_to=_effective_ap_object_id(parent),
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

    # deliver_to_actor_or_followers handles the recipient turning out to be
    # ANOTHER LOCAL bridge user itself (see its own docstring): delivers to
    # their real followers instead of a self-HTTP round trip to their own
    # inbox, which would just loop back into our own inbound handler and be
    # dropped there. Best-effort throughout -- never raises.
    await deliver_to_actor_or_followers(
        request,
        target_actor_id=recipient_actor_id,
        activity=create_activity.to_dict(),
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )

    # Someone else mentioned in the reply's text isn't necessarily the
    # person being replied to, and has no other route to receive this
    # activity (no follow relationship to fall back on) -- deliver to them
    # directly too, or the mention would never actually reach them.
    for mention_actor_id in dict.fromkeys(mention_cc):
        if mention_actor_id == recipient_actor_id:
            continue
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=mention_actor_id,
            activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )

    matrix_event_id = event.get("event_id")
    if matrix_event_id:
        # Same reasoning as bridge.inbox_dispatch's own reply mirroring:
        # inherit the parent's thread root if it has one (it's itself a
        # reply), otherwise the parent -- which we're a direct reply to --
        # *is* the root, so later replies to us resolve to the same thread.
        await repository.record_federated_event(
            FederatedEvent(
                event_id=matrix_event_id,
                room_id=room_id,
                ap_object_id=note_id,
                author_actor_id=actor_url(base, actor_record.username),
                thread_root_event_id=parent.thread_root_event_id or parent.event_id,
            )
        )
    return True
