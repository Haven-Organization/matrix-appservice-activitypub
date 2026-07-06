"""Sends the bridge bot's own notices (new follower, mention, boost, like,
...) to a Matrix user via a private 1:1 room with the bot, rather than into
their linked Profile Room.

A Profile Room is knock-joinable and, per its own purpose (it's the room a
local user actually posts their fediverse timeline from), can have other
Matrix users invited into it by its owner. Dropping a "so-and-so mentioned
you" or "X liked your post" notice there means every co-occupant sees a feed
of who's interacting with the owner -- a privacy leak with no fediverse-side
equivalent, since those interactions are genuinely between the owner and the
remote account, not something addressed to the room's whole membership. A DM
with the bot has no such audience.

One DM room per Matrix user, named "Fediverse Notifications" and created
lazily the first time they'd otherwise receive a notification (or, via
``welcome_new_user``, right when they first link/create a profile, so it's
there waiting rather than appearing as a surprise invite later), and reused
after (looked up by ``matrix_user_id`` via ``ActorRepository.get_bot_dm_room``)
rather than a fresh room every time.

Every notification sent into this room -- new follower, mention, boost,
like/reaction -- is an ordinary ``m.text`` message, never ``m.notice``
(which most clients render more quietly, and which Matrix's default push
rules suppress from notifying at all before any other rule gets a say) and
never tags the recipient with an intentional ``m.mentions`` or their own
mxid spelled out in the body. Both would force a notification regardless
of how the recipient has configured this specific room (Element's "All
messages" / "Mentions & Keywords" / "Off", for instance) -- overriding a
choice that setting exists specifically to let them make. An ordinary,
untagged message leaves that decision entirely to the room's own setting,
which is the whole reason this DM is a room of its own rather than mixed
into a Profile Room they might have others in.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import Request

from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

NOTIFICATIONS_ROOM_NAME = "Fediverse Notifications"


def notification_actor_html(*, mxid: str, handle: str, display_name: str | None = None) -> str:
    """Builds the user-pill HTML fragment at the start of every
    notification that names who/what triggered it (new follower, boost,
    reaction, ...) -- shared so all of them look the same.

    A real Matrix user pill (an ``<a href="https://matrix.to/#/{mxid}">``,
    same convention ``bridge.activitypub.sanitize`` already uses for a
    mention inside post content), carrying ``display_name`` (falling back
    to ``handle``) as its inner text -- the project-wide convention for
    every user pill this bridge emits, set by the user (2026-07-03): an
    empty anchor contributes nothing to anything extracting the message's
    TEXT content, which is how Element Web builds desktop-notification
    text, so names silently vanished from notifications (reported live
    via dunst; same incident as ``bridge.note_mirroring.
    actor_html_with_avatar``'s identical change). Confirmed rendering
    correctly on both Element Web and current Element X -- an older
    Element X build's pill-plus-inner-text double-render no longer
    reproduces. Never emit an empty pill anchor. ``handle`` stays as the
    ``title`` attribute (shown on hover) either way.
    """
    pill_href = html.escape(f"https://matrix.to/#/{mxid}", quote=True)
    pill_title = html.escape(handle, quote=True)
    pill_text = html.escape(display_name or handle)
    return f'<a href="{pill_href}" title="{pill_title}">{pill_text}</a>'

_WELCOME_BODY = (
    "Fediverse Notifications\n"
    "I'll DM you here whenever something happens on the fediverse that involves you -- new "
    "followers, mentions, likes, and boosts. Stay in this room to keep receiving them."
)
_WELCOME_FORMATTED_BODY = (
    "<p><strong>Fediverse Notifications</strong></p>"
    "<p>I'll DM you here whenever something happens on the fediverse that involves you -- new "
    "followers, mentions, likes, and boosts. Stay in this room to keep receiving them.</p>"
)


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def ensure_bot_dm_room(request: Request, *, matrix_user_id: str) -> str | None:
    """Get-or-create the bot's 1:1 DM room with ``matrix_user_id``, inviting
    them into it if this is the first time the bot has ever notified them.
    Returns the room ID, or None if creation failed outright -- best-effort,
    same as the rest of the bridge's room bookkeeping."""
    repository = request.app.state.repository
    existing = await repository.get_bot_dm_room(matrix_user_id)
    if existing is not None:
        return existing

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    try:
        room_id = await request.app.state.synapse.create_room(
            as_user_id=bot_mxid,
            name=NOTIFICATIONS_ROOM_NAME,
            invite=[matrix_user_id],
            is_direct=True,
            preset="trusted_private_chat",
            avatar_mxc=config.appservice.bot_avatar_mxc,
        )
    except SynapseError:
        logger.warning("Could not create DM room with %s", matrix_user_id, exc_info=True)
        return None

    await repository.register_bot_dm_room(matrix_user_id, room_id)
    await add_bridge_widget(request, room_id=room_id)
    return room_id


async def ensure_bot_dm_invite(request: Request, *, matrix_user_id: str) -> str | None:
    """Get-or-create the bot's DM room with ``matrix_user_id`` (see
    ``ensure_bot_dm_room``) AND make sure they're actually (re-)invited
    into it -- unlike ``ensure_bot_dm_room`` alone, which only ever invites
    the very first time the room is created and is a silent no-op on every
    later call even if the user has since left (or was never actually
    joined at all). Needed for ``create profile``/``link profile``
    reattaching a previously ``unlink profile``d identity: that path skips
    ``welcome_new_user`` (which already handles the brand-new-identity
    case) since it'd otherwise repeat its whole intro text, but its own
    Profile Room welcome message unconditionally claims "I've also invited
    you to a Fediverse Notifications DM" regardless -- this is what makes
    that actually true even then. Swallows an "already in the room" error,
    same as every other best-effort re-invite in this bridge."""
    room_id = await ensure_bot_dm_room(request, matrix_user_id=matrix_user_id)
    if room_id is None:
        return None
    config = request.app.state.config
    try:
        await request.app.state.synapse.invite_user(room_id, matrix_user_id, as_user_id=_bot_mxid(config))
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            logger.warning(
                "Could not invite %s to their notifications room %s", matrix_user_id, room_id, exc_info=True
            )
    return room_id


async def notify_user(request: Request, *, matrix_user_id: str, content: dict[str, Any]) -> str | None:
    """Send ``content`` (a full Matrix message-event content dict -- the
    caller builds body/formatted_body/m.mentions/etc. exactly as it would
    for any other ``m.room.message``) to ``matrix_user_id`` via the bot's DM
    room with them, creating/inviting into that room first if needed.
    Returns the sent event's ID, or None if the room couldn't be resolved or
    sending failed -- best-effort, never meant to block whatever fediverse
    activity triggered the notification."""
    room_id = await ensure_bot_dm_room(request, matrix_user_id=matrix_user_id)
    if room_id is None:
        return None

    config = request.app.state.config
    try:
        return await request.app.state.synapse.send_message_event(room_id, content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Could not notify %s in DM room %s", matrix_user_id, room_id, exc_info=True)
        return None


async def welcome_new_user(request: Request, *, matrix_user_id: str) -> None:
    """Invite ``matrix_user_id`` into their Fediverse Notifications DM room
    right away (rather than waiting for the first thing that would actually
    notify them) and explain what it's for -- called once, when they first
    link/create a fediverse profile. Sent as an ordinary ``m.text`` message
    with a bold header, not ``m.notice`` -- this is the first thing they see
    in a brand new room and should stand out, not get muted into whatever
    quieter styling (or no notification at all) a client gives notices.
    Best-effort, same as the rest of the bridge's room bookkeeping."""
    await notify_user(
        request,
        matrix_user_id=matrix_user_id,
        content={
            "msgtype": "m.text",
            "body": _WELCOME_BODY,
            "format": "org.matrix.custom.html",
            "formatted_body": _WELCOME_FORMATTED_BODY,
        },
    )
