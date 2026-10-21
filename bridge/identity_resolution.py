"""Central "who is acting" resolver for multi-identity support.

Before multi-identity support, every command assumed a Matrix user owned
exactly one ActivityPub identity, resolved by a single
``repository.get_local_actor_by_matrix_id(sender)`` call. That assumption no
longer holds once ``bridge.custom_identities_enabled`` lets a user own
several. ``resolve_acting_identity`` is the one place that answers "which of
this sender's identities is acting right now," replacing every one of
those call sites with a single, consistent rule:

1. If ``room_id`` is already bound to one of the sender's own identities --
   their Profile Room, a PeerTube channel they own, or a hosted Lemmy
   community they own -- that identity acts, full stop. This is the
   "implicit identity" case from the feature's own design: posting in a
   specific Profile Room or publishing to a channel/community already tied
   to one identity at creation was never meant to consult Active at all,
   multi-identity or not (``bridge.profile_posts`` already resolved Profile
   Room posting this way before this module existed).
2. Else, if ``room_id`` is a Ghost DM or Ghost Chat room already bound (at
   creation) to one of the sender's identities, that identity acts -- a
   conversation with a remote contact stays "the same person" for its whole
   lifetime even if the sender later switches their Active identity
   elsewhere (see ``register_ghost_dm_room``/``register_ghost_chat_room``'s
   own docstrings for why that binding is permanent).
3. Otherwise -- a Remote User Room, a brand new DM/Chat, ``;follow`` run
   from anywhere, etc. -- the sender's Active identity acts
   (``ActorRepository.get_active_local_actor``).

Returns ``None`` only if the sender has no linked identity at all; callers
keep their own existing "run ``;create profile`` first" notice for that
case, unchanged.

``_resolve_owned_identity`` in ``bridge.commands`` (backing ``;set``) is a
related but distinct resolver: it additionally treats the sender's own
Fediverse Notifications DM room as a profile-management shortcut, which is
specific to that one command and deliberately NOT folded in here -- every
other caller should fall through to Active instead of that shortcut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

    from bridge.repository import ActorRecord


async def resolve_acting_identity(
    request: "Request", *, sender: str, room_id: str
) -> "ActorRecord | None":
    repository = request.app.state.repository
    identities = await repository.list_local_actors_by_matrix_id(sender)
    if not identities:
        return None

    for identity in identities:
        if identity.room_id and identity.room_id == room_id:
            return identity

    channel = await repository.get_peertube_channel_by_room_id(room_id)
    if channel is not None:
        for identity in identities:
            if identity.username == channel.owner_username:
                return identity

    community = await repository.get_lemmy_community_by_room_id(room_id)
    if community is not None:
        for identity in identities:
            if identity.username == community.owner_username:
                return identity

    local_username = await repository.get_ghost_dm_room_local_username(room_id)
    if local_username is None:
        local_username = await repository.get_ghost_chat_room_local_username(room_id)
    if local_username is not None:
        for identity in identities:
            if identity.username == local_username:
                return identity

    return await repository.get_active_local_actor(sender)
