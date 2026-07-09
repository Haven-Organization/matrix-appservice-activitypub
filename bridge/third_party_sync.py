"""Periodic profile-mirroring sync for Follow-Only third-party identities.

A Follow-Only third-party actor (see ``bridge.repository.ActorRecord.is_third_party``
and ``bridge.commands._effective_third_party_mode``) is never self-controlled --
its served AP profile always live-mirrors its current Matrix display name/
avatar. ``bridge.activitypub.routes._build_actor`` already does this live, on
every single fetch of the actor document itself, but a remote server that
already has a CACHED copy of that document (the common case -- most
implementations don't re-fetch an already-known actor on every interaction)
only refreshes it on receiving a signed ``Update(Person)``. This sweep is
what generates that: periodically comparing each such identity's stored
``display_name``/``icon_url`` against a fresh ``synapse.get_profile()`` call,
and pushing an ``Update`` (via ``bridge.note_mirroring.push_profile_update``)
only when something actually changed.

Deliberately no per-record cooldown/timestamp -- the sweep interval itself is
the rate limit for everyone uniformly, so there's nothing to keep in sync
when the underlying identity's mode changes (see ``ActorRecord.is_third_party``'s
own docstring for why this feature avoids per-identity cached state in
general). No config knob either -- simple enough to hardcode.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from types import SimpleNamespace

from fastapi import FastAPI

from bridge.activitypub.urls import media_url
from bridge.note_mirroring import push_profile_update
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 20 * 60


async def sync_all_third_party_profiles(app: FastAPI) -> None:
    """One sweep over every Follow-Only third-party identity (``is_third_party=True``,
    ``room_id`` empty -- see ``ActorRepository.list_third_party_records``, which
    already excludes anyone graduated to a real Profile Room)."""
    repository = app.state.repository
    synapse = app.state.synapse
    base = app.state.config.bridge.public_base_url
    # push_profile_update/_effective_third_party_mode only ever touch
    # request.app.state -- there's no real inbound HTTP request behind a
    # background sweep, so this stands in for one.
    request = SimpleNamespace(app=app)

    for record in await repository.list_third_party_records():
        try:
            profile = await synapse.get_profile(record.matrix_user_id)
        except SynapseError:
            logger.info("Could not fetch profile for %s during third-party sync", record.matrix_user_id)
            continue

        new_display_name = profile.get("displayname") or record.matrix_user_id
        matrix_avatar_mxc = profile.get("avatar_url")
        new_icon_url = None
        if matrix_avatar_mxc:
            try:
                new_icon_url = media_url(base, matrix_avatar_mxc)
            except ValueError:
                new_icon_url = None

        if new_display_name == record.display_name and new_icon_url == record.icon_url:
            continue

        updated = dataclasses.replace(record, display_name=new_display_name, icon_url=new_icon_url)
        await repository.register_local_actor(updated)
        if matrix_avatar_mxc and new_icon_url:
            await repository.mark_media_published(matrix_avatar_mxc)
        try:
            await push_profile_update(request, updated)
        except Exception:
            logger.warning("Failed to push profile update for %s", record.matrix_user_id, exc_info=True)


async def third_party_profile_sync_loop(app: FastAPI) -> None:
    """Runs until cancelled at shutdown -- started as a background task from
    ``bridge.server.create_app``'s lifespan."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await sync_all_third_party_profiles(app)
        except Exception:
            logger.warning("Third-party profile sync sweep failed", exc_info=True)
