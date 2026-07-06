"""Tiny shared helper with no dependencies of its own, so both
``bridge.commands`` and ``bridge.inbox_dispatch`` (which already import
*from* ``commands``, so this can't just live there without an import cycle)
can format matrix.to links back to a specific event.
"""

from __future__ import annotations


def matrix_to_link(room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{room_id}/{event_id}"


def matrix_to_room_link(room_id: str) -> str:
    """A matrix.to permalink to a room itself (no specific event) -- e.g.
    for pointing someone at their own Fediverse space."""
    return f"https://matrix.to/#/{room_id}"
