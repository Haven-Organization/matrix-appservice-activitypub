"""Per-Matrix-user "Fediverse" space (``m.space``) that organizes every room
this bridge manages for them: their own linked Profile Room (current AND any
past one -- see ``bridge.repository.ActorRepository.get_profile_room_owner``)
plus every Remote User Room they're a member of.

One space per Matrix user, created lazily the first time they end up in any
bridge-managed room (their own Profile Room, or a Remote User Room they
join/follow into). The bridge bot is always co-admin of it (alongside the
user) so it can re-invite them if they ever get shut out of it the same way
a room can lock someone out (see ``bridge.commands``'s ``rejoin``).

Room membership within the space is kept in sync reactively, from
``bridge.membership``: joining a bridge-managed room adds it as a space
child; leaving a Remote User Room (voluntarily, or via the ``unfollow``
command's kick, which is the same event) removes it again. A Profile Room
-- current or past -- is never removed, even if its owner leaves it; see
``remove_room_from_space``.
"""

from __future__ import annotations

import logging

from fastapi import Request

from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

SPACE_NAME = "Fediverse"
_SPACE_ROOM_TYPE = "m.space"


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def ensure_user_space(request: Request, *, matrix_user_id: str) -> str | None:
    """Get-or-create ``matrix_user_id``'s personal Fediverse space, with the
    bot as full admin and the user one level below (99, not 100 -- see
    ``_handle_create_profile``'s identical reasoning) so the bot can help
    re-invite later, and still kick them back out if it ever needs to.
    Returns the space's room ID, or None if creation failed -- best-effort,
    same as the rest of the bridge's room bookkeeping."""
    repository = request.app.state.repository
    existing = await repository.get_user_space(matrix_user_id)
    if existing is not None:
        return existing

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    try:
        space_room_id = await request.app.state.synapse.create_room(
            as_user_id=bot_mxid,
            name=SPACE_NAME,
            invite=[matrix_user_id],
            avatar_mxc=config.appservice.bot_avatar_mxc,
            room_type=_SPACE_ROOM_TYPE,
            # bot_mxid (the creator, as_user_id above) deliberately omitted
            # -- room v12 gives creators implicit, immutable "infinite"
            # power level and REJECTS m.room.power_levels outright if the
            # creator appears in its own `users` (see
            # SynapseClient.create_room's docstring). Every earlier room
            # version already defaults a room's creator to 100 on its own.
            power_level_content_override={"users": {matrix_user_id: 99}},
        )
    except SynapseError:
        logger.warning("Could not create Fediverse space for %s", matrix_user_id, exc_info=True)
        return None

    await repository.register_user_space(matrix_user_id, space_room_id)
    await add_bridge_widget(request, room_id=space_room_id)
    return space_room_id


async def add_room_to_space(request: Request, *, matrix_user_id: str, child_room_id: str) -> None:
    """Add ``child_room_id`` as a child of ``matrix_user_id``'s Fediverse
    space, creating the space first if this is their first bridge-managed
    room. Idempotent -- safe to call again for a room already listed.
    Best-effort throughout."""
    space_room_id = await ensure_user_space(request, matrix_user_id=matrix_user_id)
    if space_room_id is None:
        return

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    synapse = request.app.state.synapse
    via = [config.synapse.server_name]
    try:
        await synapse.send_state_event(
            space_room_id, "m.space.child", child_room_id, {"via": via}, as_user_id=bot_mxid
        )
    except SynapseError:
        logger.info("Could not add %s to %s's space", child_room_id, matrix_user_id, exc_info=True)
        return

    # Not required for the space to show its children (that's the
    # m.space.child above) -- this is the child room's own "which space is
    # this canonically part of" pointer, which some clients use for
    # breadcrumbs/context. Best-effort and independent of the child event
    # above succeeding or not: worth attempting even if, say, the bot
    # somehow lacks power in the child room but not the space.
    try:
        await synapse.send_state_event(
            child_room_id, "m.space.parent", space_room_id, {"via": via, "canonical": True}, as_user_id=bot_mxid
        )
    except SynapseError:
        logger.info("Could not set m.space.parent on %s", child_room_id, exc_info=True)


async def remove_room_from_space(request: Request, *, matrix_user_id: str, child_room_id: str) -> None:
    """Remove ``child_room_id`` from ``matrix_user_id``'s Fediverse space --
    UNLESS it's ``matrix_user_id``'s own linked Profile Room, current or
    past, which always stays (per ``get_profile_room_owner``, so this holds
    even for an old room they've since moved on from via `replace room`).
    A no-op if they have no space at all yet. Best-effort.

    Per the spec, an ``m.space.child`` event with no ``via`` key means "not
    actually a child" -- so removal is just overwriting it with empty
    content, not a special "delete" operation.
    """
    repository = request.app.state.repository
    if await repository.get_profile_room_owner(child_room_id) == matrix_user_id:
        return

    space_room_id = await repository.get_user_space(matrix_user_id)
    if space_room_id is None:
        return

    config = request.app.state.config
    try:
        await request.app.state.synapse.send_state_event(
            space_room_id, "m.space.child", child_room_id, {}, as_user_id=_bot_mxid(config)
        )
    except SynapseError:
        logger.info("Could not remove %s from %s's space", child_room_id, matrix_user_id, exc_info=True)
