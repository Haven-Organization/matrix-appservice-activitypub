"""Detects a real network-level outage on OUR side (not remote servers
individually being flaky) and automatically backfills every locally-followed
account's recent posts once reconnected, in case anything was missed while
unreachable.

Detection: many DIFFERENT remote homeservers independently starting to fail
delivery to us within the same tight time window is a strong signal the
problem was ours, not theirs -- no coincidence of individually flaky remote
servers explains dozens of them all failing at the same moment (confirmed
live 2026-08-13: 66+ distinct destinations all showed Synapse's own
``failure_ts`` within the same ~15-minute window during a real multi-hour
outage of glowers.club's own network reachability). Read straight from
Synapse's own ``/_synapse/admin/v1/federation/destinations`` (not a
bridge-side heartbeat) specifically because a heartbeat can't tell "the
process was down" apart from "the process stayed up but the network path
was cut" -- this incident was the latter, and the bridge's own uptime showed
nothing unusual at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from fastapi import FastAPI

from bridge.commands import _BackfillSourceError, _mirror_backfilled_notes, _notice, _resolve_backfill_source
from bridge.notifications import notify_user
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 15 * 60
# Only failures within this long of "now" count toward a cluster -- keeps a
# months-old batch of unrelated flaky-server failures from ever contributing.
LOOKBACK_SECONDS = 3 * 3600
# How tight a window counts as "simultaneous" for clustering purposes.
CLUSTER_WINDOW_SECONDS = 15 * 60
# Well above ordinary background churn (a random 15-minute window rarely
# has more than a handful of unrelated destinations newly failing) and well
# below the 66+ seen in the real incident this was built from.
CLUSTER_MIN_DESTINATIONS = 15
# Don't re-trigger a sweep more than once in this long, even if a fresh
# check still sees a qualifying cluster -- avoids repeatedly re-backfilling
# the same accounts every CHECK_INTERVAL_SECONDS for one ongoing incident.
COOLDOWN_SECONDS = 3 * 3600


def _find_outage_cluster(failure_timestamps_ms: list[int]) -> tuple[int, int] | None:
    """Densest ``CLUSTER_WINDOW_SECONDS``-wide window among
    ``failure_timestamps_ms`` -- returns ``(window_start_ms, count)``, or
    None if no window has at least ``CLUSTER_MIN_DESTINATIONS`` entries.
    O(n^2) but n is at most a few hundred, so this is instant in practice."""
    window_ms = CLUSTER_WINDOW_SECONDS * 1000
    ts = sorted(failure_timestamps_ms)
    best_start: int | None = None
    best_count = 0
    for i, start in enumerate(ts):
        count = 1
        for other in ts[i + 1 :]:
            if other - start > window_ms:
                break
            count += 1
        if count > best_count:
            best_count = count
            best_start = start
    if best_start is not None and best_count >= CLUSTER_MIN_DESTINATIONS:
        return best_start, best_count
    return None


async def check_for_outage_and_recover(app: FastAPI) -> None:
    """One check: look for a recent mass-failure cluster in Synapse's own
    federation bookkeeping, and if one hasn't already been handled recently,
    sweep every followed account's outbox for anything missed.

    Entirely a no-op when ``bridge.use_synapse_admin_api`` is off -- unlike
    the OTHER admin-API-gated features (see that setting's own docstring,
    ``bridge.commands._list_bridge_managed_rooms``), there's no slower
    bridge-side fallback available here: federation delivery bookkeeping
    (who's failing to receive from us, and since when) is inherently
    Synapse-internal state with no Client-Server API equivalent at all, not
    something this bridge could reconstruct another way. A homeserver that
    doesn't grant the Admin API (or isn't Synapse at all) just never gets
    automatic outage detection -- the one-time manual sweep this was built
    from (``;backfill`` per followed account) still works regardless."""
    config = app.state.config
    if not config.bridge.use_synapse_admin_api:
        return
    repository = app.state.repository
    synapse = app.state.synapse

    last_recovery_at = await repository.get_last_outage_recovery_at()
    if last_recovery_at is not None and time.time() - last_recovery_at < COOLDOWN_SECONDS:
        return  # already handled a sweep recently -- see COOLDOWN_SECONDS

    try:
        destinations = await synapse.admin_list_federation_destinations()
    except SynapseError:
        logger.info("Outage-recovery check couldn't reach the federation destinations admin API", exc_info=True)
        return

    lookback_cutoff_ms = (time.time() - LOOKBACK_SECONDS) * 1000
    recent_failures = [
        d["failure_ts"] for d in destinations
        if d.get("failure_ts") and d["failure_ts"] > lookback_cutoff_ms
    ]
    cluster = _find_outage_cluster(recent_failures)
    if cluster is None:
        return
    outage_started_at, destinations_affected = cluster

    logger.warning(
        "Detected a likely network outage starting ~%s (%d destinations failed within %d minutes) -- "
        "sweeping followed accounts for anything missed",
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(outage_started_at / 1000)),
        destinations_affected, CLUSTER_WINDOW_SECONDS // 60,
    )
    await repository.record_outage_recovery(
        detected_at=time.time(), outage_started_at=outage_started_at, destinations_affected=destinations_affected,
    )
    await _sweep_followed_accounts(app)


async def _sweep_followed_accounts(app: FastAPI) -> None:
    """Backfills every followed account's outbox ONCE each (shared across
    however many local users follow it -- unlike iterating each local
    user's own ``list_following``, which would redundantly re-backfill an
    account followed by more than one local user), notices a room only when
    something was actually found (silence for the common case of an account
    with nothing new -- a room-wide "0 posts mirrored" notice for every one
    of dozens of followed accounts would be pure noise), then DMs each
    local user a single aggregate summary covering everyone THEY follow."""
    repository = app.state.repository
    config = app.state.config
    request = SimpleNamespace(app=app)
    count = config.bridge.backfill_default_count

    imported_by_room: dict[str, int] = {}
    for room_id in await repository.list_all_remote_actor_room_ids():
        room = await repository.get_remote_actor_room_by_room_id(room_id)
        if room is None:
            continue
        try:
            raw_items, fallback_author = await _resolve_backfill_source(
                request, remote_room=room, count=count, thread_root_event_id=None,
            )
            imported, _already, _failed = await _mirror_backfilled_notes(
                request, raw_items=raw_items, fallback_author=fallback_author,
            )
        except _BackfillSourceError:
            continue
        except Exception:
            logger.warning("Outage-recovery backfill failed for %s", room.actor_id, exc_info=True)
            continue
        imported_by_room[room_id] = imported
        if imported:
            try:
                await _notice(
                    request, room_id,
                    f"Reconnected after a network outage -- backfilled {imported} post(s) that may have been "
                    "missed while unreachable.",
                )
            except Exception:
                logger.warning("Failed to post outage-recovery notice in %s", room_id, exc_info=True)

    for username in await repository.list_local_usernames():
        actor_record = await repository.get_local_actor(username)
        if actor_record is None:
            continue
        following_room_ids = []
        for actor_id in await repository.list_following(username):
            followed_room = await repository.get_remote_actor_room(actor_id)
            if followed_room is not None:
                following_room_ids.append(followed_room.room_id)
        total_imported = sum(imported_by_room.get(rid, 0) for rid in following_room_ids)
        if total_imported == 0:
            continue
        accounts_with_new_content = sum(1 for rid in following_room_ids if imported_by_room.get(rid, 0) > 0)
        await notify_user(
            request, matrix_user_id=actor_record.matrix_user_id,
            content={
                "msgtype": "m.text",
                "body": f"\U0001F50C Reconnected after a network outage -- backfilled {total_imported} post(s) "
                f"across {accounts_with_new_content} followed account(s) that may have been missed while "
                "unreachable.",
            },
        )


async def outage_recovery_loop(app: FastAPI) -> None:
    """Runs until cancelled at shutdown -- started as a background task from
    ``bridge.server.create_app``'s lifespan, same pattern as
    ``bridge.third_party_sync.third_party_profile_sync_loop``."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            await check_for_outage_and_recover(app)
        except Exception:
            logger.warning("Outage-recovery check failed", exc_info=True)
