"""One-off backfill: upgrade existing rooms + ghosts to the backstage space.

``bridge.backstage_space_enabled`` only changes behavior for a NEW invite,
a room created, or a ghost provisioned, before the flag was flipped on keeps
working exactly as it always did until something gives it a reason to
change (see ``bridge.backstage_space.ensure_ghost_access``'s own lazy,
on-demand upgrade). That self-converts gradually on its own, but this script
does the same work up front in one pass, for every Remote User Room, local
Profile Room, hosted PeerTube Channel room, and hosted+mirrored Lemmy
Community/Group room that already exists: joins every ghost currently in
each one to the backstage space, then upgrades the room's own
``m.room.join_rules`` to reference it.

DM, Chat, and Notification rooms are never touched, out of scope for this
feature entirely, same as everywhere else in ``bridge.backstage_space``.

Run natively, no containerization:

    python adhoc_backfill_backstage_space.py [--public-join] [path/to/config.yaml]

Requires ``bridge.backstage_space_enabled: true`` already set (in config.yaml
or via a database setting override), refuses to run otherwise, since
without it ``maybe_upgrade_join_rule`` would silently no-op on every room.

``--public-join`` trades a brief window of exposure for half the member
events: instead of joining every ghost to the backstage space one at a time
(an invite+join pair, or a single Admin API force-join when
``bridge.use_synapse_admin_api`` is on), it flips the space's own
``join_rule`` to ``public`` just long enough for every ghost to self-join
with one plain ``join`` event each, then locks the space back down to
whatever it was before. During that window, anyone on this (or a
federating) homeserver who learns the space's room ID could join it too,
the same consequence the admin widget's "Get invited" button warns about,
since space membership grants direct join access to every room that trusts
it. The window only spans the ghost-joining phase, not the whole room
sweep, and is always closed again even if the run is interrupted partway
through.
"""

from __future__ import annotations

import asyncio
import sys

from bridge.backstage_space import (
    close_backstage_space,
    ensure_backstage_space,
    join_ghost_to_backstage_space,
    maybe_upgrade_join_rule,
    open_backstage_space_publicly,
)
from bridge.bridge_settings import effective_config
from bridge.config import ConfigError, load_config
from bridge.server import _create_repository
from bridge.synapse_client import SynapseClient, SynapseError


async def _in_scope_room_ids(repository) -> dict[str, str]:
    """Every room this feature applies to, as ``{room_id: label}`` for
    reporting, Remote User Room, local Profile Room, hosted PeerTube
    Channel, hosted+mirrored Lemmy Community, hosted+mirrored Group. Never
    DM/Chat/Notification/Shoot Channel/Guild Space/Personal Space, those
    aren't in ``bridge.backstage_space``'s scope at all."""
    rooms: dict[str, str] = {}
    for a in await repository.list_all_local_actors():
        if a.room_id:
            rooms[a.room_id] = f"Profile ({a.username})"
    for c in await repository.list_all_peertube_channels():
        if c.room_id:
            rooms[c.room_id] = f"Channel ({c.username})"
    for lc in await repository.list_all_lemmy_communities():
        if lc.room_id:
            rooms[lc.room_id] = f"Community, hosted ({lc.username})"
    for g in await repository.list_all_groups():
        if g.room_id:
            rooms[g.room_id] = f"Group, hosted ({g.username})"
    for m in await repository.list_all_mapped_rooms():
        if m.kind in ("Remote User Room", "Lemmy Community (mirrored)", "Group (mirrored)"):
            rooms[m.room_id] = f"{m.kind} ({m.maps_to})"
    return rooms


async def _ghosts_by_room(synapse: SynapseClient, config, bot_mxid: str, rooms: dict[str, str]) -> tuple[dict[str, list[str]], set[str]]:
    """``{room_id: [ghost_mxid, ...]}`` for every room whose members are
    readable, plus the flat set of every distinct ghost seen, gathered
    once up front so ``--public-join``'s exposure window can be scoped to
    exactly the ghost-joining phase, not the slower per-room upgrade sweep
    that follows it."""
    ghosts_by_room: dict[str, list[str]] = {}
    all_ghosts: set[str] = set()
    for room_id in rooms:
        try:
            members = await synapse.get_joined_members(room_id, as_user_id=bot_mxid)
        except Exception as exc:  # noqa: BLE001, best-effort sweep, one bad room must not abort the rest
            print(f"  SKIP  {room_id} ({rooms[room_id]}): could not list members: {exc}")
            continue
        ghosts = [m for m in members if m.startswith(f"@{config.appservice.user_prefix}")]
        ghosts_by_room[room_id] = ghosts
        all_ghosts.update(ghosts)
    return ghosts_by_room, all_ghosts


async def run(config_path: str | None, *, public_join: bool = False) -> int:
    try:
        base_config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    repository = await _create_repository(base_config)
    config = await effective_config(base_config, repository)
    if not config.bridge.backstage_space_enabled:
        print(
            "bridge.backstage_space_enabled is off, enable it first (config.yaml or the admin widget's "
            "Settings tab), then re-run this script.",
            file=sys.stderr,
        )
        return 1

    synapse = SynapseClient(
        config.synapse.base_url,
        as_token=config.appservice.as_token,
        admin_token=config.synapse.admin_token,
        timeout=config.federation.request_timeout,
    )
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    space_room_id = await ensure_backstage_space(synapse, repository, config)
    if space_room_id is None:
        print("Could not create/find the backstage space, aborting.", file=sys.stderr)
        return 1
    print(f"Backstage space: {space_room_id}")

    rooms = await _in_scope_room_ids(repository)
    print(f"Found {len(rooms)} in-scope room(s).")

    ghosts_by_room, all_ghosts = await _ghosts_by_room(synapse, config, bot_mxid, rooms)
    readable_rooms = set(ghosts_by_room)
    print(f"Found {len(all_ghosts)} distinct ghost(s) across {len(readable_rooms)} readable room(s).")

    joined_ghosts: set[str] = set()
    if public_join:
        previous_join_rule = await open_backstage_space_publicly(synapse, config, space_room_id)
        print(
            f"Backstage space temporarily set to public (was {previous_join_rule!r}), "
            f"self-joining {len(all_ghosts)} ghost(s) with one event each..."
        )
        try:
            for ghost_mxid in all_ghosts:
                try:
                    await synapse.join_room(space_room_id, as_user_id=ghost_mxid)
                    joined_ghosts.add(ghost_mxid)
                except SynapseError as exc:
                    print(f"    WARN: {ghost_mxid} could not self-join the backstage space: {exc}")
        finally:
            await close_backstage_space(synapse, config, space_room_id, previous_join_rule)
            print(f"Backstage space locked back down to {previous_join_rule!r}.")
    else:
        for ghost_mxid in all_ghosts:
            await join_ghost_to_backstage_space(synapse, repository, config, ghost_mxid)
            joined_ghosts.add(ghost_mxid)

    upgraded, skipped = 0, len(rooms) - len(readable_rooms)
    for room_id in readable_rooms:
        label = rooms[room_id]
        if await maybe_upgrade_join_rule(synapse, repository, config, room_id):
            print(f"  OK    {room_id} ({label}): restricted to the backstage space.")
            upgraded += 1
        else:
            print(f"  SKIP  {room_id} ({label}): no permission, already public, or unrecognized join_rules.")
            skipped += 1

    print(
        f"\nDone. {upgraded} room(s) upgraded, {skipped} skipped, "
        f"{len(joined_ghosts)} distinct ghost(s) joined to the backstage space."
    )
    return 0


def main(argv: list[str]) -> int:
    public_join = "--public-join" in argv
    positional = [a for a in argv if not a.startswith("--")]
    config_path = positional[0] if positional else None
    return asyncio.run(run(config_path, public_join=public_join))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
