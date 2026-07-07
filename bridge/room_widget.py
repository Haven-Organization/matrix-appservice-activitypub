"""Adds the bridge's own room widget (the web UI served by ``bridge.widget``)
to a room as a state event -- split out from
``bridge.commands._handle_widget`` so it can also run automatically
whenever the bridge creates a room, not just on explicit ``;widget``
request. Deliberately its own module rather than living in
``bridge.note_mirroring`` alongside sibling room-shape constants like
``SOCIAL_PROFILE_ROOM_TYPE``: ``bridge.notifications`` needs to call this
too, and ``bridge.note_mirroring`` already imports from
``bridge.notifications``, so putting it there would cycle.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# Sent under both the current ``m.widget`` type (MSC1236/2764) and the
# legacy ``im.vector.modular.widgets`` type it superseded, same reasoning as
# ``send_bridge_info``'s dual ``m.bridge``/``uk.half-shot.bridge`` write --
# clients vary in which one they actually look for.
WIDGET_STATE_TYPES = ("m.widget", "im.vector.modular.widgets")


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def add_bridge_widget(request: Request, *, room_id: str) -> bool:
    """Add this bridge's room widget to ``room_id``, always as the bridge
    bot -- never a ghost, even for a room a ghost created (every Remote
    User Room, ghost DM room, ghost Chat room), so the widget consistently
    shows as coming from the bridge itself rather than whichever random
    ghost happened to create that particular room. Best-effort, same as
    the rest of the bridge's room bookkeeping -- returns whether it
    succeeded, but never raises, so a widget failure never blocks whatever
    larger operation (room creation, ``;widget``) triggered it.

    The bot is only ever INVITED into a ghost-created room, not its
    creator, and that invite's acceptance is normally asynchronous (it has
    to wait for Synapse to round-trip the invite event back through the
    AppService transaction pipeline) -- calling this as the bot right after
    creating such a room used to be a race that reliably lost under any
    real concurrency, which was previously worked around by using the
    room's own creator (a ghost) instead. Fixed properly here instead: an
    explicit, synchronous ``join_room`` as the bot right before sending the
    widget state, so there's no window where the bot's invite is still
    pending. Idempotent if the bot's already a member (an ordinary
    already-joined room, or the ``;widget`` command re-running against one
    it's long since joined) -- Synapse's own ``/join`` no-ops rather than
    erroring in that case.

    A fresh widget id is minted every call (rather than reusing a fixed
    one), so calling this again against a room that already has one adds a
    second instance instead of silently no-op'ing against the existing one."""
    config = request.app.state.config
    as_user_id = _bot_mxid(config)
    synapse = request.app.state.synapse
    try:
        await synapse.join_room(room_id, as_user_id=as_user_id)
    except SynapseError:
        logger.info("Bot could not join %s before adding widget", room_id, exc_info=True)
    widget_id = f"fediverse-bridge-{uuid.uuid4().hex[:12]}"
    widget_url = (
        f"{config.bridge.public_base_url}/widget"
        "?matrix_user_id=$matrix_user_id&matrix_room_id=$matrix_room_id&theme=$theme"
        f"&widgetId={widget_id}"
    )
    content = {
        "type": "m.custom",
        "url": widget_url,
        "name": "Fediverse Bridge",
        "data": {"title": "Fediverse Bridge"},
    }
    if config.appservice.bot_avatar_mxc:
        # Element's WidgetAvatar component (matrix-react-sdk) reads this
        # exact top-level field -- an mxc:// URI, not a URL -- to show a
        # widget's own icon in its widgets/apps tile list, falling back to
        # a generic icon otherwise; there's no other documented per-widget
        # icon mechanism for a custom (non-built-in-type) widget like ours.
        content["avatar_url"] = config.appservice.bot_avatar_mxc
    ok = True
    for state_type in WIDGET_STATE_TYPES:
        try:
            await synapse.send_state_event(room_id, state_type, widget_id, content, as_user_id=as_user_id)
        except SynapseError as exc:
            ok = False
            logger.warning("Could not add widget (%s) to %s: %s", state_type, room_id, exc)
    return ok
