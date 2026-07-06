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


async def add_bridge_widget(request: Request, *, room_id: str, as_user_id: str | None = None) -> bool:
    """Add this bridge's room widget to ``room_id``. Best-effort, same as
    the rest of the bridge's room bookkeeping -- returns whether it
    succeeded, but never raises, so a widget failure never blocks whatever
    larger operation (room creation, ``;widget``) triggered it.

    ``as_user_id`` defaults to the bot, correct for the ``;widget`` command
    (bot is necessarily already in the room, or it wouldn't have seen the
    command) and for any room the bot itself created. For a room a GHOST
    created instead (every Remote User Room, ghost DM room, ghost Chat
    room -- see e.g. ``_replace_remote_actor_room``), callers MUST pass
    that ghost's mxid explicitly: the bot is only ever invited into those,
    not the creator, and its invite-acceptance is asynchronous (it has to
    wait for Synapse to round-trip the invite event back through the
    AppService transaction pipeline) -- calling this as the bot right after
    creating such a room is a race that reliably loses under any real
    concurrency (see the room-creation call sites for confirmation this
    was actually hit, not just theoretical).

    A fresh widget id is minted every call (rather than reusing a fixed
    one), so calling this again against a room that already has one adds a
    second instance instead of silently no-op'ing against the existing one."""
    config = request.app.state.config
    as_user_id = as_user_id or _bot_mxid(config)
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
    synapse = request.app.state.synapse
    ok = True
    for state_type in WIDGET_STATE_TYPES:
        try:
            await synapse.send_state_event(room_id, state_type, widget_id, content, as_user_id=as_user_id)
        except SynapseError as exc:
            ok = False
            logger.warning("Could not add widget (%s) to %s: %s", state_type, room_id, exc)
    return ok
