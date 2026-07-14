"""FastAPI application factory.

Wires together config, the durable ``ActorRepository`` (sqlite-backed by
default), the shared ``httpx.AsyncClient`` used for outbound federation
requests, the remote-actor public-key cache, the Synapse client, and the
bridge's persistent service actor (used for all inbound following) -- then
mounts the ActivityPub router (``bridge.activitypub.routes``) and the
AppService transaction receiver (``bridge.appservice_routes``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI

from bridge.activitypub.routes import router as activitypub_router
from bridge.activitypub.signatures import ActorKeyCache
from bridge.activitypub.urls import main_key_id
from bridge.appservice_routes import router as appservice_router
from bridge.config import BridgeConfig, ConfigError
from bridge.ghosts import ensure_ghost_user
from bridge.repository import ActorRepository
from bridge.service_actor import load_or_create_service_actor
from bridge.sqlite_repository import SqliteActorRepository
from bridge.synapse_client import SynapseClient, SynapseError
from bridge.third_party_sync import third_party_profile_sync_loop
from bridge.widget import router as widget_router

logger = logging.getLogger(__name__)

# On a full system reboot, systemd's unit ordering (After=matrix-synapse.service)
# only guarantees Synapse's own process has *started* -- not that it's
# finished its own (often slow, especially cold-cache) startup and is
# actually accepting connections yet. Hitting it immediately at that point
# raises a raw, unhandled httpx.ConnectError that crashes the whole app with
# a multi-frame stack trace, when the correct behavior is just to wait a
# few seconds like any other client would. In every observed case Synapse
# was ready within seconds, so this budget is deliberately generous, not
# tuned to the common case.
_SYNAPSE_READY_TIMEOUT = 60.0
_SYNAPSE_READY_POLL_INTERVAL = 2.0


async def _wait_for_synapse_ready(synapse: SynapseClient, base_url: str) -> None:
    deadline = time.monotonic() + _SYNAPSE_READY_TIMEOUT
    last_exc: Exception | None = None
    logged_wait = False
    while time.monotonic() < deadline:
        try:
            await synapse.get_versions()
            if logged_wait:
                logger.info("Synapse is now reachable at %s", base_url)
            return
        # httpx.HTTPError covers connection-level failures (refused,
        # timed out, ...) -- the observed real-world case, Synapse's
        # listener not up yet at all. SynapseError covers it responding
        # but with a non-2xx status, which is equally plausible while it's
        # still finishing its own startup (DB migrations, etc.) even after
        # its listener is already accepting connections.
        except (httpx.HTTPError, SynapseError) as exc:
            last_exc = exc
            if not logged_wait:
                logger.info("Waiting for Synapse to become reachable at %s...", base_url)
                logged_wait = True
            await asyncio.sleep(_SYNAPSE_READY_POLL_INTERVAL)
    raise RuntimeError(
        f"Synapse never became reachable at {base_url} after {_SYNAPSE_READY_TIMEOUT:.0f}s"
    ) from last_exc


async def _create_repository(config: BridgeConfig) -> ActorRepository:
    """Instantiate whichever durable ``ActorRepository`` ``storage.backend``
    selects. Split out of ``create_app.lifespan`` because construction is
    async for one backend (``PostgresActorRepository.create``) but not the
    other (``SqliteActorRepository``'s constructor connects synchronously)
    -- callers only ever want one or the other, never both at once, so this
    picks and awaits the right one rather than making ``lifespan`` do it
    inline.

    ``bridge.postgres_repository`` (and its ``asyncpg`` dependency) is
    imported here, not at module level -- a deployment that stays on the
    sqlite default should never need ``asyncpg`` installed at all just to
    start the bridge."""
    if config.storage.backend == "postgresql":
        from bridge.postgres_repository import PostgresActorRepository

        pg = config.storage.postgres
        return await PostgresActorRepository.create(
            pg.resolved_dsn(), min_size=pg.min_pool_size, max_size=pg.max_pool_size
        )
    if config.storage.backend == "sqlite":
        return SqliteActorRepository(Path(config.storage.data_dir) / "bridge.db")
    raise ConfigError(f"Unknown storage.backend {config.storage.backend!r}")


def create_app(
    config: BridgeConfig, *, repository: ActorRepository | None = None
) -> FastAPI:
    """Build the FastAPI app.

    ``repository`` defaults to whichever durable backend ``storage.backend``
    selects (see ``bridge.config.StorageSection``): ``SqliteActorRepository``
    persisted at ``{storage.data_dir}/bridge.db`` (the default), or
    ``PostgresActorRepository`` connected per ``storage.postgres``. Either
    way, linked profiles, follows, ghost/room mappings, and the post/event
    map all survive a restart. Pass an alternative implementation (e.g.
    ``InMemoryActorRepository``) for tests, bypassing this selection
    entirely.
    """
    owns_repository = repository is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config
        app.state.repository = repository or await _create_repository(config)

        # Moved ahead of http_client/key_cache below (only needs the
        # repository) so its key material is available to sign THEIR own
        # setup -- see ActorKeyCache's signing_key_id/signing_private_key_pem
        # below.
        app.state.service_actor = await load_or_create_service_actor(
            app.state.repository,
            localpart=config.appservice.bot_localpart,
            matrix_user_id=f"@{config.appservice.bot_localpart}:{config.synapse.server_name}",
            display_name="Fediverse Bridge",
        )

        # Several real-world implementations (Pleroma/Akkoma notably) 302 a
        # post's human-facing permalink (e.g. /notice/<id>) to its actual AP
        # object URL (e.g. /objects/<uuid>) rather than content-negotiating
        # in place the way Mastodon does -- without follow_redirects, that
        # response body is a tiny HTML redirect stub, not the post, which
        # every JSON-parsing caller (fetch_actor and everything built on it:
        # actor/Note fetches, webfinger, media, delivery) would otherwise
        # choke on.
        http_client = httpx.AsyncClient(timeout=config.federation.request_timeout, follow_redirects=True)
        app.state.http_client = http_client
        app.state.key_cache = ActorKeyCache(
            http_client,
            ttl_seconds=config.federation.actor_key_cache_ttl,
            # Some servers require HTTP Signatures on actor-document GETs
            # too, not just inbox POSTs (confirmed live 2026-07-14 against
            # Shoot's own chat.understars.dev -- see
            # bridge.activitypub.remote_actor.fetch_actor's own docstring
            # for the fuller story) -- signed as the bridge's own service
            # actor, the same identity used for every other fetch_actor call.
            signing_key_id=main_key_id(config.bridge.public_base_url, app.state.service_actor.username),
            signing_private_key_pem=app.state.service_actor.private_key_pem,
        )
        app.state.synapse = SynapseClient(
            config.synapse.base_url,
            as_token=config.appservice.as_token,
            admin_token=config.synapse.admin_token,
            timeout=config.federation.request_timeout,
        )
        await _wait_for_synapse_ready(app.state.synapse, config.synapse.base_url)

        if config.appservice.bot_display_name or config.appservice.bot_avatar_mxc:
            # Compare-first: only push the bot's profile when it actually
            # differs from config. ensure_ghost_user PUTs unconditionally,
            # and each PUT makes Synapse schedule an update_join_states
            # task rewriting the bot's member event in EVERY room it's in
            # (hundreds here) -- the avatar PUT then cancels the still-
            # running displayname task, which trips Synapse's double-pop
            # bug and logs a "KeyError: '<task id>'" traceback on every
            # single bridge restart (diagnosed live, 2026-07-04). A normal
            # restart changes nothing, so it should touch nothing.
            bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
            try:
                bot_profile = await app.state.synapse.get_profile(bot_mxid)
            except SynapseError:
                bot_profile = {}
            desired_name = config.appservice.bot_display_name
            desired_avatar = config.appservice.bot_avatar_mxc
            if (desired_name and bot_profile.get("displayname") != desired_name) or (
                desired_avatar and bot_profile.get("avatar_url") != desired_avatar
            ):
                await ensure_ghost_user(
                    app.state.synapse,
                    server_name=config.synapse.server_name,
                    localpart=config.appservice.bot_localpart,
                    display_name=desired_name,
                    avatar_mxc=desired_avatar,
                )

        sync_task = asyncio.create_task(third_party_profile_sync_loop(app))
        try:
            yield
        finally:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
            await http_client.aclose()
            await app.state.synapse.aclose()
            if owns_repository:
                await app.state.repository.close()

    app = FastAPI(
        title="matrix-appservice-activitypub",
        description="Native Matrix <-> ActivityPub federation bridge",
        lifespan=lifespan,
    )
    app.include_router(activitypub_router)
    app.include_router(appservice_router)
    app.include_router(widget_router)
    return app
