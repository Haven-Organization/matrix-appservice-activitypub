"""The bridge's own ActivityPub service actor.

Inbound following (the bot's ``follow`` command) is done on behalf of a single,
persistent actor representing the bridge as a whole -- not per-Matrix-user --
since a Remote User Room is shared by every local user who follows that
fediverse account. It's stored in ``ActorRepository`` exactly like a linked
Profile Room actor (with an empty ``room_id``, since it isn't tied to one),
so it persists across restarts wherever the repository itself persists
(``SqliteActorRepository`` by default -- see ``bridge.server.create_app``).
"""

from __future__ import annotations

from bridge.crypto import generate_keypair
from bridge.repository import ActorRecord, ActorRepository


async def load_or_create_service_actor(
    repository: ActorRepository,
    *,
    localpart: str,
    matrix_user_id: str,
    display_name: str,
) -> ActorRecord:
    """Fetch the bridge's service actor from ``repository``, creating it on first run."""
    existing = await repository.get_local_actor(localpart)
    if existing is not None:
        return existing

    private_key_pem, public_key_pem = generate_keypair()
    record = ActorRecord(
        username=localpart,
        matrix_user_id=matrix_user_id,
        room_id="",
        public_key_pem=public_key_pem,
        private_key_pem=private_key_pem,
        display_name=display_name,
        summary="Bridge service actor used for following fediverse accounts on behalf of this homeserver.",
    )
    await repository.register_local_actor(record)
    return record
