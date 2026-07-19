"""Tiny shared helper with no dependencies of its own, so both
``bridge.commands`` and ``bridge.inbox_dispatch`` (which already import
*from* ``commands``, so this can't just live there without an import cycle)
can format matrix.to links back to a specific event.
"""

from __future__ import annotations

import html as _html


def matrix_to_link(room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{room_id}/{event_id}"


def matrix_to_room_link(room_id: str) -> str:
    """A matrix.to permalink to a room itself (no specific event) -- e.g.
    for pointing someone at their own Fediverse space."""
    return f"https://matrix.to/#/{room_id}"


def room_pill_html(room_id: str, text: str | None = None) -> str:
    """A clickable Matrix room pill -- ``<a href="https://matrix.to/#/{room_id}">
    ...</a>`` -- the room counterpart of a user mention pill (see
    ``bridge.notifications.notification_actor_html``). ``text`` defaults to
    the bare room id when the caller has nothing more human-readable to show
    (no room name looked up) -- the anchor is never left empty, same
    reasoning as the user-pill helper's: an empty anchor's own text is what
    Element Web's desktop notifications are built from, so it'd otherwise
    silently vanish there too."""
    return f'<a href="{matrix_to_room_link(room_id)}">{_html.escape(text or room_id)}</a>'
