"""Federates Matrix messages sent in a ghost chat room out as ActivityPub
``ChatMessage``s (Pleroma/Akkoma's "Chats" -- see
``bridge.activitypub.models.ChatMessage`` and ``Actor.accepts_chat_messages``),
a distinct instant-messaging concept from a Note-based direct message.

Triggered for every ``m.room.message`` event the AppService receives: if the
room is tracked as a ghost chat room (``ActorRepository.is_ghost_chat_room``
-- created either by ``bridge.commands``' ``chat`` command, an inbound
``ChatMessage`` actually arriving (see ``bridge.note_mirroring.mirror_chat_message``),
or a Matrix-native DM invite sent directly to a ghost, auto-accepted by
``bridge.membership.maybe_accept_invite``), this builds a
``Create{ChatMessage}`` addressed to the room's one other party and delivers
it to their inbox, signed with the sender's own linked Profile Room actor.

Deliberately simpler than ``bridge.reply_bridge``: a chat room only ever has
one other party for its whole lifetime, and Pleroma's own Chats don't thread
or reply the way a Note does -- so unlike a DM room, there's no "reply
target" concept here at all; EVERY message sent in a chat room goes out this
way, as its own fresh ``ChatMessage``, regardless of any Matrix reply
relation it might carry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Request

from bridge.activitypub.models import Activity, ChatMessage
from bridge.activitypub.sanitize import strip_reply_fallback
from bridge.activitypub.urls import actor_url, main_key_id
from bridge.commands import message_addresses_bot
from bridge.media import build_ap_attachment
from bridge.note_mirroring import deliver_to_actor_or_followers
from bridge.repository import FederatedEvent

logger = logging.getLogger(__name__)


async def maybe_federate_chat_message(request: Request, event: dict) -> bool:
    """Returns True if this event was sent in a ghost chat room (handled,
    successfully or not) -- callers should not also treat it as a reply or
    a fresh post."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    config = request.app.state.config

    if message_addresses_bot(content, config):
        # Same reasoning as bridge.reply_bridge's identical guard: a
        # message actually addressed to the bot (e.g. tagging it inside a
        # chat room, unusual but not impossible) must never be mistaken
        # for real chat content and federated out.
        return True

    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return False  # never re-federate our own ghosts'/bot's own messages

    repository = request.app.state.repository
    if not await repository.is_ghost_chat_room(room_id):
        return False

    recipient_actor_id = await repository.get_ghost_chat_room_actor_id(room_id)
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

    base = config.bridge.public_base_url
    attachment = build_ap_attachment(base, content)
    # A media message's `body` is just its filename (Matrix has no separate
    # caption field) -- only treat it as the message's text for plain ones,
    # same reasoning as bridge.reply_bridge/bridge.profile_posts.
    raw_body = strip_reply_fallback(content.get("body") or "")
    body = "" if attachment is not None else raw_body
    if not body and attachment is None:
        return True

    if attachment is not None:
        await repository.mark_media_published(content["url"])

    chat_message_id = f"{actor_url(base, actor_record.username)}/chat-messages/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    chat_message = ChatMessage(
        id=chat_message_id,
        attributed_to=actor_url(base, actor_record.username),
        to=recipient_actor_id,
        content=body,
        published=published,
        attachment=[attachment] if attachment else [],
    )
    create_activity = Activity(
        id=f"{chat_message_id}/activity",
        type="Create",
        actor=actor_url(base, actor_record.username),
        object=chat_message,
        published=published,
        to=[recipient_actor_id],
    )

    # deliver_to_actor_or_followers handles the recipient turning out to be
    # ANOTHER LOCAL bridge user itself (see its own docstring) -- shouldn't
    # actually be reachable for a chat room specifically (a ghost is never
    # provisioned for one of our own local actors -- see
    # bridge.note_mirroring._provision_ghost), but reusing it costs nothing
    # and keeps this consistent with every other outbound delivery path.
    await deliver_to_actor_or_followers(
        request,
        target_actor_id=recipient_actor_id,
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
                ap_object_id=chat_message_id,
                author_actor_id=actor_url(base, actor_record.username),
            )
        )
    return True
