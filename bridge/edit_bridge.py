"""Federates Matrix message edits (``m.replace`` relations) of
already-distributed posts/replies out as ActivityPub ``Update`` activities.

Runs ahead of ``bridge.reply_bridge``/``bridge.profile_posts`` in the
dispatch chain and CONSUMES every ``m.replace`` event, whether or not it
federates anything: an edit event's own top-level ``body`` is the ``"* ..."``
fallback rendering, and before this module existed one falling through to
the fresh-post path went out as a brand-new, malformed post -- confirmed
live (2026-07-04) with an edit of a reply that was already part of an AP
thread.

Edits federate as ``Update`` -- the SAME object id with replaced content and
an ``updated`` timestamp -- rather than a ``Delete`` + new ``Create``. This
is deliberate, and not just because it's what every mainstream
implementation (Mastodon, Pleroma, Misskey) does for its own edits: the
object id is what every remote reply's ``inReplyTo``, every Like, and every
Announce out there points at. Delete+recreate would orphan all of those and
knock the post out of its thread, the exact opposite of what an edit should
do. The updated Note also re-carries its ``inReplyTo`` and mention/
participant tags (rebuilt the same way the original delivery built them), so
implementations that replace the object wholesale don't lose threading or
the "reply to @a @b" participant line.

A ghost DM/Chat room's messages are deliberately NOT covered yet: their
original Notes went out privately addressed, and a correct private Update
needs that same addressing rather than this module's public one -- until
that's built, an edit of a DM simply stays Matrix-only (the old behavior).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Request

from bridge.activitypub.models import AS_PUBLIC, Activity, Note
from bridge.activitypub.sanitize import plain_text_to_note_html, strip_reply_fallback
from bridge.activitypub.urls import actor_url, followers_url, main_key_id
from bridge.media import build_ap_attachment, media_caption
from bridge.mentions import collect_reply_participants, resolve_pill_mentions, resolve_plaintext_mentions
from bridge.note_mirroring import deliver_to_actor_or_followers
from bridge.reply_bridge import derive_in_reply_to
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


def _ts_to_iso(origin_server_ts: int | None) -> str:
    return datetime.fromtimestamp((origin_server_ts or 0) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def maybe_federate_edit(request: Request, event: dict) -> bool:
    """Returns True if this event was an ``m.replace`` edit (ALWAYS handled,
    even when nothing federates -- see module docstring for why an edit
    event must never fall through to the fresh-post/reply paths)."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") != "m.replace":
        return False

    # Everything from here on returns True: this IS an edit event, and the
    # only question is whether there's a federated Update to send for it.
    target_event_id = relates_to.get("event_id")
    new_content = content.get("m.new_content")
    if not target_event_id or not isinstance(new_content, dict):
        return True

    config = request.app.state.config
    sender = event.get("sender", "")
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return True  # the bot's/a ghost's own edits (e.g. card fixups) never federate

    repository = request.app.state.repository
    federated = await repository.get_federated_event_by_matrix_event(target_event_id)
    if federated is None:
        return True  # editing a message that was never federated -- stays Matrix-only

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return True  # no linked profile -- nothing to sign an Update with
    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    if federated.author_actor_id != own_actor_id:
        return True  # only the post's own author can edit it (Matrix PLs permitting others is irrelevant AP-side)
    if federated.reposted_object_id:
        return True  # a ;repost echo's AP content isn't derived from its Matrix body -- not editable this way
    if await repository.is_ghost_dm_room(federated.room_id) or await repository.is_ghost_chat_room(federated.room_id):
        return True  # see module docstring -- private edits not federated yet

    attachment = build_ap_attachment(base, new_content)
    raw_body = strip_reply_fallback(new_content.get("body") or "")
    # Same convention as the original send paths: a media message's body
    # is a real caption only when a separate differing `filename` exists --
    # see bridge.media.media_caption.
    body = strip_reply_fallback(media_caption(new_content)) if attachment is not None else raw_body
    if not body and attachment is None:
        return True
    if attachment is not None:
        await repository.mark_media_published(new_content["url"])

    mention_tags: list[dict] = []
    mention_cc: list[str] = []
    if body:
        body, mention_tags, mention_cc = await resolve_pill_mentions(request, body, new_content)
        already_tagged = {tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")}
        plaintext_tags, plaintext_cc = await resolve_plaintext_mentions(request, body, already_tagged=already_tagged)
        mention_tags += plaintext_tags
        mention_cc += plaintext_cc

    # A guest post's owner-mention tack (see bridge.profile_posts) is part
    # of the delivered content, so the edited content owes it too -- same
    # rule as the serving routes' _guest_post_owner_mention.
    room_owner = await repository.get_local_actor_by_room_id(federated.room_id)
    if room_owner is not None and actor_url(base, room_owner.username) != own_actor_id:
        owner_handle = f"@{room_owner.username}@{config.bridge.domain}"
        owner_actor_id = actor_url(base, room_owner.username)
        if owner_actor_id not in {tag.get("href") for tag in mention_tags} and owner_handle not in body:
            body = f"{owner_handle} {body}".strip()
            mention_tags.insert(0, {"type": "Mention", "href": owner_actor_id, "name": owner_handle})
            mention_cc.insert(0, owner_actor_id)

    # The edited object must keep its place in its thread: inReplyTo comes
    # from the ORIGINAL event's own relations (m.new_content never carries
    # them), resolved exactly like the original delivery resolved it.
    published = None
    in_reply_to = None
    try:
        original_event = await request.app.state.synapse.get_event(
            federated.room_id, federated.event_id, as_user_id=bot_mxid
        )
    except SynapseError:
        original_event = None
    if original_event is not None:
        published = _ts_to_iso(original_event.get("origin_server_ts"))
        in_reply_to = await derive_in_reply_to(repository, original_event.get("content") or {})
    if in_reply_to:
        participant_tags, participant_cc = await collect_reply_participants(
            request,
            in_reply_to,
            exclude_actor_ids={own_actor_id, *(tag["href"] for tag in mention_tags if tag.get("href"))},
            already_tagged={tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")},
        )
        mention_tags += participant_tags
        mention_cc += participant_cc

    mention_links = {tag["name"]: tag["href"] for tag in mention_tags if tag.get("name") and tag.get("href")}
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    note = Note(
        id=federated.ap_object_id,  # SAME id -- this is an edit, not a new post
        attributed_to=own_actor_id,
        content=plain_text_to_note_html(body, mention_links) if body else "",
        published=published or updated,
        updated=updated,
        to=[AS_PUBLIC],
        cc=list(dict.fromkeys([followers_url(base, actor_record.username), *mention_cc])),
        in_reply_to=in_reply_to,
        attachment=[attachment] if attachment else [],
        tag=mention_tags,
    )
    update_activity = Activity(
        id=f"{federated.ap_object_id}#updates/{uuid.uuid4().hex}",
        type="Update",
        actor=own_actor_id,
        object=note,
        published=updated,
        to=note.to,
        cc=note.cc,
    )

    # Same audience the original went to: own followers (via own actor id --
    # deliver_to_actor_or_followers resolves a local actor to exactly that)
    # plus everyone mentioned/carried on the thread. Best-effort throughout.
    recipients = list(dict.fromkeys([own_actor_id, *mention_cc]))
    for recipient_actor_id in recipients:
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=recipient_actor_id,
            activity=update_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    return True
