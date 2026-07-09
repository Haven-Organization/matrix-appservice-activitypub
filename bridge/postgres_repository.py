"""PostgreSQL-backed ``ActorRepository`` -- alternative durable storage to
``bridge.sqlite_repository.SqliteActorRepository``, selected via
``storage.backend: postgresql`` in ``config.yaml`` (see
``bridge.config.PostgresSection``). Implements the exact same protocol and
schema shape (same tables/columns, same upsert semantics) as the sqlite
version, just against a real server instead of a local file -- for a
deployment that already runs Postgres for other services (e.g. Synapse
itself) and would rather not also manage a second, separate sqlite file, or
that needs the bridge's own bookkeeping reachable from more than one host.

Per the project's data-sovereignty constraint this stores only bridge
bookkeeping -- no post content or media, which live exclusively in Matrix --
identical in scope to the sqlite version.

Unlike sqlite (a single-writer local file needing its own thread-offload/lock
wrapper -- see that module's docstring), ``asyncpg`` is natively async and
pools its own connections, so every method here just borrows a pooled
connection directly with no extra serialization needed.
"""

from __future__ import annotations

import logging
import time

import asyncpg

from bridge.repository import (
    ActorRecord,
    FederatedEvent,
    GhostProfile,
    ReactionRecord,
    RemoteActorRoom,
    ThirdPartyAllowRecord,
)

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS local_actors (
        username TEXT PRIMARY KEY,
        matrix_user_id TEXT NOT NULL UNIQUE,
        room_id TEXT NOT NULL,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        hide_followers BOOLEAN NOT NULL DEFAULT FALSE,
        hide_following BOOLEAN NOT NULL DEFAULT FALSE,
        is_third_party BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # Admin-granted third-party access (see ActorRecord.is_third_party and
    # the ";allow"/";disallow"/";allowed" commands). rule_type is 'mxid',
    # 'room', or 'homeserver'; value is the corresponding exact MXID/room
    # ID/domain.
    """
    CREATE TABLE IF NOT EXISTS third_party_allowlist (
        rule_type TEXT NOT NULL,
        value TEXT NOT NULL,
        granted_by TEXT NOT NULL,
        granted_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (rule_type, value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_room_history (
        room_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS followers (
        username TEXT NOT NULL,
        remote_actor_id TEXT NOT NULL,
        PRIMARY KEY (username, remote_actor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS following (
        username TEXT NOT NULL,
        remote_actor_id TEXT NOT NULL,
        PRIMARY KEY (username, remote_actor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blocked_actors (
        username TEXT NOT NULL,
        remote_actor_id TEXT NOT NULL,
        PRIMARY KEY (username, remote_actor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS muted_actors (
        username TEXT NOT NULL,
        remote_actor_id TEXT NOT NULL,
        PRIMARY KEY (username, remote_actor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS remote_actor_rooms (
        actor_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL UNIQUE,
        ghost_user_id TEXT NOT NULL,
        inbox_url TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        pending_backfill BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # ap_object_id is deliberately NOT globally UNIQUE here -- see the
    # partial unique index created below instead, and
    # record_federated_event's docstring (bridge.repository) for why: a
    # multi-attachment post's 2nd+ attachment gets its own FederatedEvent
    # row sharing the SAME ap_object_id as the post's primary row (so
    # reacting to it still resolves correctly), which a table-wide UNIQUE
    # would reject.
    """
    CREATE TABLE IF NOT EXISTS federated_events (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        ap_object_id TEXT NOT NULL,
        author_actor_id TEXT NOT NULL,
        thread_root_event_id TEXT,
        boosted_object_id TEXT,
        boosted_author_actor_id TEXT,
        is_primary_event BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_transactions (
        txn_id TEXT PRIMARY KEY,
        processed_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_media (
        mxc_uri TEXT PRIMARY KEY,
        published_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reactions (
        activity_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        target_ap_object_id TEXT NOT NULL,
        reactor_ghost_mxid TEXT,
        reactor_matrix_user_id TEXT,
        secondary_event_id TEXT UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ghost_profiles (
        actor_id TEXT PRIMARY KEY,
        display_name TEXT,
        icon_url TEXT,
        mxid TEXT,
        handle TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ghost_profiles_mxid ON ghost_profiles (mxid)",
    """
    CREATE TABLE IF NOT EXISTS user_spaces (
        matrix_user_id TEXT PRIMARY KEY,
        space_room_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_dm_rooms (
        matrix_user_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ghost_dm_rooms (
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY (actor_id, matrix_user_id)
    )
    """,
    # Deliberately separate from ghost_dm_rooms -- see
    # ActorRepository.get_ghost_chat_room's docstring for why a Note-based
    # DM and an ActivityPub ChatMessage are different wire formats a room
    # commits to for its whole lifetime, so the same (ghost, local user)
    # pair can have up to one room of EACH kind, never one room serving both.
    """
    CREATE TABLE IF NOT EXISTS ghost_chat_rooms (
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY (actor_id, matrix_user_id)
    )
    """,
    # Dedup cache for custom-emoji reaction images (see
    # ActorRepository.get_custom_emoji_mxc) -- source_url is the remote
    # emoji image's own URL (unique per emoji per source instance), mxc_url
    # is where it was uploaded to Synapse's media repo. Checked before ever
    # fetching one, so the same emoji is only ever downloaded/uploaded once
    # no matter how many reactions use it.
    """
    CREATE TABLE IF NOT EXISTS custom_emoji (
        source_url TEXT PRIMARY KEY,
        mxc_url TEXT NOT NULL
    )
    """,
    # Per-subject resolved custom emoji (see ActorRepository.record_resolved_emoji)
    # -- subject_id is either a post's ap_object_id (content emoji) or a
    # remote actor's own actor_id (display-name emoji). Lets a later
    # re-render show the right image without the original AP tag data
    # still being around.
    """
    CREATE TABLE IF NOT EXISTS resolved_emoji (
        subject_id TEXT NOT NULL,
        shortcode TEXT NOT NULL,
        mxc_url TEXT NOT NULL,
        PRIMARY KEY (subject_id, shortcode)
    )
    """,
    # Permanent, append-only history for Remote User Rooms / ghost DM rooms
    # / ghost Chat rooms -- the ``profile_room_history`` treatment, extended
    # to the two other room kinds whose own primary tables (above) only
    # ever reflect the CURRENT room per actor (their upsert overwrites
    # ``room_id`` in place on replace, no history kept there). Only one row
    # per key may have ``is_current`` set -- enforced by the partial unique
    # indexes below, not just application logic -- so "what's the current
    # room" is always unambiguous even if a bug ever left more than one row
    # per key. See ``bridge.note_mirroring.resolve_old_remote_actor_room``/
    # ``resolve_old_ghost_room_owner``, which check these tables first and
    # only fall back to reading a room's own state back out of Synapse for
    # a room old enough to predate this table's introduction.
    """
    CREATE TABLE IF NOT EXISTS remote_actor_room_history (
        room_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ghost_dm_room_history (
        room_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ghost_chat_room_history (
        room_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # Additive migrations for columns introduced after a table's initial
    # CREATE TABLE IF NOT EXISTS -- unlike sqlite (no native "ADD COLUMN IF
    # NOT EXISTS" until fairly recent versions, hence the manual
    # PRAGMA table_info check in SqliteActorRepository._migrate),
    # Postgres has supported this directly since 9.6, so no separate
    # migration step/tracking is needed here at all.
    "ALTER TABLE local_actors ADD COLUMN IF NOT EXISTS banner_url TEXT",
    "ALTER TABLE local_actors ADD COLUMN IF NOT EXISTS hide_followers BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE local_actors ADD COLUMN IF NOT EXISTS hide_following BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE local_actors ADD COLUMN IF NOT EXISTS is_third_party BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS icon_url TEXT",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS banner_url TEXT",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS pending_backfill BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS thread_root_event_id TEXT",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS boosted_object_id TEXT",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS boosted_author_actor_id TEXT",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS is_primary_event BOOLEAN NOT NULL DEFAULT TRUE",
    # Drops the table-wide UNIQUE a table created before is_primary_event
    # existed still has -- Postgres's default name for a single-column
    # inline UNIQUE is "{table}_{column}_key"; IF EXISTS makes this a no-op
    # against a table that never had it (i.e. one created fresh from the
    # CREATE TABLE above, which never declares it in the first place).
    "ALTER TABLE federated_events DROP CONSTRAINT IF EXISTS federated_events_ap_object_id_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_federated_events_primary_ap_object "
    "ON federated_events (ap_object_id) WHERE is_primary_event = TRUE",
    "ALTER TABLE ghost_profiles ADD COLUMN IF NOT EXISTS mxid TEXT",
    "ALTER TABLE ghost_profiles ADD COLUMN IF NOT EXISTS handle TEXT",
    "ALTER TABLE reactions ADD COLUMN IF NOT EXISTS secondary_event_id TEXT UNIQUE",
    "ALTER TABLE reactions ADD COLUMN IF NOT EXISTS custom_emoji_mxc TEXT",
    # is_current gives profile_room_history the same DB-enforced "only one
    # current room per identity" guarantee as the three newer history
    # tables below -- previously that guarantee only came from
    # local_actors.room_id being a single column, correct by construction
    # but not something profile_room_history itself could answer without
    # joining back to local_actors.
    "ALTER TABLE profile_room_history ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_room_history_current "
    "ON profile_room_history (username) WHERE is_current",
    # Backfill: mark whichever row matches each username's CURRENT room
    # (per local_actors) as current -- cheap and idempotent, runs
    # unconditionally on every startup like the other backfills here.
    """
    UPDATE profile_room_history h SET is_current = TRUE
    FROM local_actors a
    WHERE h.room_id = a.room_id AND a.room_id <> '' AND NOT h.is_current
    """,
    # Same reasoning as SqliteActorRepository._migrate's identical index:
    # mark_transaction_processed's opportunistic cleanup DELETEs by
    # processed_at on every single write, against what's in practice the
    # largest table here by a wide margin.
    "CREATE INDEX IF NOT EXISTS idx_processed_transactions_processed_at "
    "ON processed_transactions (processed_at)",
    # Same backfill as SqliteActorRepository._migrate -- cheap and
    # idempotent, so it just runs unconditionally on every startup.
    """
    INSERT INTO profile_room_history (room_id, username, matrix_user_id)
    SELECT room_id, username, matrix_user_id FROM local_actors WHERE room_id <> ''
    ON CONFLICT (room_id) DO NOTHING
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_actor_room_history_current "
    "ON remote_actor_room_history (actor_id) WHERE is_current",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ghost_dm_room_history_current "
    "ON ghost_dm_room_history (actor_id, matrix_user_id) WHERE is_current",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ghost_chat_room_history_current "
    "ON ghost_chat_room_history (actor_id, matrix_user_id) WHERE is_current",
    # Backfill each history table (added 2026-07) from its primary table's
    # CURRENT rows -- same reasoning/limitation as the profile_room_history
    # backfill above: cheap, idempotent, recovers only what's live right
    # now, not anything already replaced away before this table existed
    # (that gap is what resolve_old_remote_actor_room/
    # resolve_old_ghost_room_owner's state-reading fallback is for).
    """
    INSERT INTO remote_actor_room_history (room_id, actor_id, is_current)
    SELECT room_id, actor_id, TRUE FROM remote_actor_rooms
    ON CONFLICT (room_id) DO NOTHING
    """,
    """
    INSERT INTO ghost_dm_room_history (room_id, actor_id, matrix_user_id, is_current)
    SELECT room_id, actor_id, matrix_user_id, TRUE FROM ghost_dm_rooms
    ON CONFLICT (room_id) DO NOTHING
    """,
    """
    INSERT INTO ghost_chat_room_history (room_id, actor_id, matrix_user_id, is_current)
    SELECT room_id, actor_id, matrix_user_id, TRUE FROM ghost_chat_rooms
    ON CONFLICT (room_id) DO NOTHING
    """,
]

_TRANSACTION_RETENTION_SECONDS = 7 * 24 * 3600


class PostgresActorRepository:
    """Durable ``ActorRepository`` backed by a Postgres connection pool.

    Construct via ``await PostgresActorRepository.create(dsn)`` rather than
    the constructor directly -- connecting and provisioning the pool is
    inherently async (``asyncpg.create_pool``), which a plain ``__init__``
    can't do. Safe to share one Postgres database between multiple
    concurrently-running bridge processes (unlike the single-file sqlite
    backend), since Postgres itself serializes/isolates concurrent access.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(
        cls, dsn: str, *, min_size: int = 1, max_size: int = 10
    ) -> "PostgresActorRepository":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        repository = cls(pool)
        await repository._migrate()
        return repository

    async def _migrate(self) -> None:
        async with self._pool.acquire() as conn:
            for statement in _SCHEMA_STATEMENTS:
                await conn.execute(statement)

    async def close(self) -> None:
        await self._pool.close()

    # -- local actors ------------------------------------------------------

    async def get_local_actor(self, username: str) -> ActorRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM local_actors WHERE username = $1", username)
        return self._row_to_actor(row) if row else None

    async def get_local_actor_by_matrix_id(self, matrix_user_id: str) -> ActorRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM local_actors WHERE matrix_user_id = $1", matrix_user_id
        )
        return self._row_to_actor(row) if row else None

    async def get_local_actor_by_room_id(self, room_id: str) -> ActorRecord | None:
        if not room_id:
            return None
        row = await self._pool.fetchrow(
            "SELECT * FROM local_actors WHERE room_id = $1 AND room_id <> ''", room_id
        )
        return self._row_to_actor(row) if row else None

    async def get_profile_room_owner(self, room_id: str) -> str | None:
        if not room_id:
            return None
        row = await self._pool.fetchrow(
            "SELECT matrix_user_id FROM profile_room_history WHERE room_id = $1", room_id
        )
        return row["matrix_user_id"] if row else None

    async def get_profile_room_history(self, username: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT room_id FROM profile_room_history WHERE username = $1", username
        )
        return [row["room_id"] for row in rows]

    async def register_local_actor(self, record: ActorRecord) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO local_actors
                        (username, matrix_user_id, room_id, public_key_pem, private_key_pem,
                         display_name, summary, icon_url, banner_url, hide_followers, hide_following,
                         is_third_party)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (username) DO UPDATE SET
                        matrix_user_id = EXCLUDED.matrix_user_id,
                        room_id = EXCLUDED.room_id,
                        public_key_pem = EXCLUDED.public_key_pem,
                        private_key_pem = EXCLUDED.private_key_pem,
                        display_name = EXCLUDED.display_name,
                        summary = EXCLUDED.summary,
                        icon_url = EXCLUDED.icon_url,
                        banner_url = EXCLUDED.banner_url,
                        hide_followers = EXCLUDED.hide_followers,
                        hide_following = EXCLUDED.hide_following,
                        is_third_party = EXCLUDED.is_third_party
                    """,
                    record.username, record.matrix_user_id, record.room_id,
                    record.public_key_pem, record.private_key_pem,
                    record.display_name, record.summary, record.icon_url, record.banner_url,
                    record.hide_followers, record.hide_following, record.is_third_party,
                )
                if record.room_id:
                    # Permanent, unlike local_actors.room_id itself -- see
                    # SqliteActorRepository._register_local_actor's
                    # identical reasoning. is_current mirrors the same
                    # guarantee remote_actor_room_history/ghost_dm_room_history/
                    # ghost_chat_room_history enforce -- clear the old flag
                    # first so the partial unique index never sees two
                    # current rows for the same username at once.
                    await conn.execute(
                        "UPDATE profile_room_history SET is_current = FALSE WHERE username = $1 AND is_current",
                        record.username,
                    )
                    await conn.execute(
                        """
                        INSERT INTO profile_room_history (room_id, username, matrix_user_id, is_current)
                        VALUES ($1, $2, $3, TRUE)
                        ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                        """,
                        record.room_id, record.username, record.matrix_user_id,
                    )

    async def unregister_local_actor(self, username: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM local_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM followers WHERE username = $1", username)
                await conn.execute("DELETE FROM following WHERE username = $1", username)
                await conn.execute("DELETE FROM blocked_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM muted_actors WHERE username = $1", username)

    @staticmethod
    def _row_to_actor(row: asyncpg.Record) -> ActorRecord:
        return ActorRecord(
            username=row["username"],
            matrix_user_id=row["matrix_user_id"],
            room_id=row["room_id"],
            public_key_pem=row["public_key_pem"],
            private_key_pem=row["private_key_pem"],
            display_name=row["display_name"],
            summary=row["summary"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
            hide_followers=row["hide_followers"],
            hide_following=row["hide_following"],
            is_third_party=row["is_third_party"],
        )

    # -- followers / following ----------------------------------------------

    async def list_followers(self, username: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT remote_actor_id FROM followers WHERE username = $1 ORDER BY remote_actor_id", username
        )
        return [row["remote_actor_id"] for row in rows]

    async def list_following(self, username: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT remote_actor_id FROM following WHERE username = $1 ORDER BY remote_actor_id", username
        )
        return [row["remote_actor_id"] for row in rows]

    async def is_following(self, username: str, remote_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM following WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )
        return row is not None

    async def is_anyone_following(self, remote_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM following WHERE remote_actor_id = $1 LIMIT 1", remote_actor_id
        )
        return row is not None

    async def add_follower(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO followers (username, remote_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            username, remote_actor_id,
        )

    async def remove_follower(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM followers WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )

    async def add_following(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO following (username, remote_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            username, remote_actor_id,
        )

    async def remove_following(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM following WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )

    async def set_followers_hidden(self, username: str, hidden: bool) -> None:
        await self._pool.execute(
            "UPDATE local_actors SET hide_followers = $1 WHERE username = $2", hidden, username
        )

    async def set_following_hidden(self, username: str, hidden: bool) -> None:
        await self._pool.execute(
            "UPDATE local_actors SET hide_following = $1 WHERE username = $2", hidden, username
        )

    async def list_third_party_records(self) -> list[ActorRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM local_actors WHERE is_third_party AND room_id = ''"
        )
        return [self._row_to_actor(row) for row in rows]

    async def is_blocked(self, username: str, remote_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM blocked_actors WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )
        return row is not None

    async def add_blocked(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO blocked_actors (username, remote_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            username, remote_actor_id,
        )

    async def remove_blocked(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM blocked_actors WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )

    async def is_muted(self, username: str, remote_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM muted_actors WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )
        return row is not None

    async def add_muted(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO muted_actors (username, remote_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            username, remote_actor_id,
        )

    async def remove_muted(self, username: str, remote_actor_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM muted_actors WHERE username = $1 AND remote_actor_id = $2", username, remote_actor_id
        )

    # -- third-party allowlist -----------------------------------------------

    async def add_third_party_allow(self, rule_type: str, value: str, *, granted_by: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO third_party_allowlist (rule_type, value, granted_by, granted_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (rule_type, value) DO UPDATE SET
                granted_by = EXCLUDED.granted_by,
                granted_at = EXCLUDED.granted_at
            """,
            rule_type, value, granted_by,
        )

    async def remove_third_party_allow(self, rule_type: str, value: str) -> None:
        await self._pool.execute(
            "DELETE FROM third_party_allowlist WHERE rule_type = $1 AND value = $2", rule_type, value
        )

    async def list_third_party_allows(self, rule_type: str | None = None) -> list[ThirdPartyAllowRecord]:
        if rule_type is not None:
            rows = await self._pool.fetch(
                "SELECT * FROM third_party_allowlist WHERE rule_type = $1 ORDER BY rule_type, value", rule_type
            )
        else:
            rows = await self._pool.fetch("SELECT * FROM third_party_allowlist ORDER BY rule_type, value")
        return [
            ThirdPartyAllowRecord(
                rule_type=row["rule_type"],
                value=row["value"],
                granted_by=row["granted_by"],
                granted_at=row["granted_at"].isoformat(),
            )
            for row in rows
        ]

    async def is_third_party_allowed(self, *, mxid: str, homeserver: str, room_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'mxid' AND value = $1", mxid
        )
        if row is not None:
            return True
        if room_id:
            row = await self._pool.fetchrow(
                "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'room' AND value = $1", room_id
            )
            if row is not None:
                return True
        if homeserver:
            row = await self._pool.fetchrow(
                "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'homeserver' AND value = $1", homeserver
            )
            if row is not None:
                return True
        return False

    # -- remote actor rooms --------------------------------------------------

    async def get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM remote_actor_rooms WHERE actor_id = $1", actor_id)
        return self._row_to_remote_room(row) if row else None

    async def get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM remote_actor_rooms WHERE room_id = $1", room_id)
        return self._row_to_remote_room(row) if row else None

    async def register_remote_actor_room(self, record: RemoteActorRoom) -> None:
        # pending_backfill is deliberately NOT in the DO UPDATE SET below --
        # only the initial INSERT sets it (from the caller's RemoteActorRoom,
        # True only when bridge.commands._establish_remote_follow is creating
        # a brand-new room). An ordinary re-registration (a profile Update
        # sync, a room replace) must never clobber an still-unconsumed
        # pending flag back to False, nor resurrect an already-consumed one
        # back to True -- see mark_backfill_pending_done, the only thing
        # that's ever allowed to change it after row creation.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO remote_actor_rooms
                        (actor_id, room_id, ghost_user_id, inbox_url, display_name, icon_url, banner_url, pending_backfill)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (actor_id) DO UPDATE SET
                        room_id = EXCLUDED.room_id,
                        ghost_user_id = EXCLUDED.ghost_user_id,
                        inbox_url = EXCLUDED.inbox_url,
                        display_name = EXCLUDED.display_name,
                        icon_url = EXCLUDED.icon_url,
                        banner_url = EXCLUDED.banner_url
                    """,
                    record.actor_id, record.room_id, record.ghost_user_id,
                    record.inbox_url, record.display_name, record.icon_url, record.banner_url, record.pending_backfill,
                )
                # Permanent history row -- see
                # get_remote_actor_room_history_actor_id's docstring. Clear
                # the old "current" flag first so the partial unique index
                # never sees two current rows for the same actor at once.
                await conn.execute(
                    "UPDATE remote_actor_room_history SET is_current = FALSE WHERE actor_id = $1 AND is_current",
                    record.actor_id,
                )
                await conn.execute(
                    """
                    INSERT INTO remote_actor_room_history (room_id, actor_id, is_current)
                    VALUES ($1, $2, TRUE)
                    ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                    """,
                    record.room_id, record.actor_id,
                )

    async def get_remote_actor_room_history_actor_id(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT actor_id FROM remote_actor_room_history WHERE room_id = $1", room_id
        )
        return row["actor_id"] if row else None

    async def mark_backfill_pending_done(self, room_id: str) -> None:
        await self._pool.execute(
            "UPDATE remote_actor_rooms SET pending_backfill = FALSE WHERE room_id = $1", room_id
        )

    @staticmethod
    def _row_to_remote_room(row: asyncpg.Record) -> RemoteActorRoom:
        return RemoteActorRoom(
            actor_id=row["actor_id"],
            room_id=row["room_id"],
            ghost_user_id=row["ghost_user_id"],
            inbox_url=row["inbox_url"],
            display_name=row["display_name"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
            pending_backfill=row["pending_backfill"],
        )

    # -- federated event map --------------------------------------------------

    async def record_federated_event(self, record: FederatedEvent, *, is_primary: bool = True) -> None:
        await self._pool.execute(
            """
            INSERT INTO federated_events
                (event_id, room_id, ap_object_id, author_actor_id, thread_root_event_id,
                 boosted_object_id, boosted_author_actor_id, is_primary_event)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (event_id) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                ap_object_id = EXCLUDED.ap_object_id,
                author_actor_id = EXCLUDED.author_actor_id,
                thread_root_event_id = EXCLUDED.thread_root_event_id,
                boosted_object_id = EXCLUDED.boosted_object_id,
                boosted_author_actor_id = EXCLUDED.boosted_author_actor_id,
                is_primary_event = EXCLUDED.is_primary_event
            """,
            record.event_id, record.room_id, record.ap_object_id,
            record.author_actor_id, record.thread_root_event_id,
            record.boosted_object_id, record.boosted_author_actor_id,
            is_primary,
        )

    async def get_federated_event_by_matrix_event(self, event_id: str) -> FederatedEvent | None:
        row = await self._pool.fetchrow("SELECT * FROM federated_events WHERE event_id = $1", event_id)
        return self._row_to_federated_event(row) if row else None

    async def get_federated_event_by_ap_object(self, ap_object_id: str) -> FederatedEvent | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM federated_events WHERE ap_object_id = $1 AND is_primary_event = TRUE", ap_object_id
        )
        return self._row_to_federated_event(row) if row else None

    @staticmethod
    def _row_to_federated_event(row: asyncpg.Record) -> FederatedEvent:
        return FederatedEvent(
            event_id=row["event_id"],
            room_id=row["room_id"],
            ap_object_id=row["ap_object_id"],
            author_actor_id=row["author_actor_id"],
            thread_root_event_id=row["thread_root_event_id"],
            boosted_object_id=row["boosted_object_id"],
            boosted_author_actor_id=row["boosted_author_actor_id"],
        )

    # -- AppService transaction idempotency --------------------------------

    async def has_processed_transaction(self, txn_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_transactions WHERE txn_id = $1", txn_id
        )
        return row is not None

    async def mark_transaction_processed(self, txn_id: str) -> None:
        now = time.time()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO processed_transactions (txn_id, processed_at) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    txn_id, now,
                )
                # Opportunistic cleanup -- bounds table growth without a
                # separate sweep task, same as the sqlite version.
                await conn.execute(
                    "DELETE FROM processed_transactions WHERE processed_at < $1",
                    now - _TRANSACTION_RETENTION_SECONDS,
                )

    # -- published-media allowlist ------------------------------------------

    async def is_media_published(self, mxc_uri: str) -> bool:
        row = await self._pool.fetchrow("SELECT 1 FROM published_media WHERE mxc_uri = $1", mxc_uri)
        return row is not None

    async def mark_media_published(self, mxc_uri: str) -> None:
        await self._pool.execute(
            "INSERT INTO published_media (mxc_uri, published_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            mxc_uri, time.time(),
        )

    # -- reactions -----------------------------------------------------------

    async def record_reaction(self, record: ReactionRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO reactions
                (activity_id, room_id, event_id, target_ap_object_id,
                 reactor_ghost_mxid, reactor_matrix_user_id, secondary_event_id, custom_emoji_mxc)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (activity_id) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                event_id = EXCLUDED.event_id,
                target_ap_object_id = EXCLUDED.target_ap_object_id,
                reactor_ghost_mxid = EXCLUDED.reactor_ghost_mxid,
                reactor_matrix_user_id = EXCLUDED.reactor_matrix_user_id,
                secondary_event_id = EXCLUDED.secondary_event_id,
                custom_emoji_mxc = EXCLUDED.custom_emoji_mxc
            """,
            record.activity_id, record.room_id, record.event_id, record.target_ap_object_id,
            record.reactor_ghost_mxid, record.reactor_matrix_user_id, record.secondary_event_id,
            record.custom_emoji_mxc,
        )

    async def get_reaction_by_activity_id(self, activity_id: str) -> ReactionRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM reactions WHERE activity_id = $1", activity_id)
        return self._row_to_reaction(row) if row else None

    async def get_reaction_by_matrix_event(self, event_id: str) -> ReactionRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM reactions WHERE event_id = $1 OR secondary_event_id = $1", event_id
        )
        return self._row_to_reaction(row) if row else None

    async def remove_reaction(self, activity_id: str) -> None:
        await self._pool.execute("DELETE FROM reactions WHERE activity_id = $1", activity_id)

    async def get_custom_emoji_mxc(self, source_url: str) -> str | None:
        row = await self._pool.fetchrow("SELECT mxc_url FROM custom_emoji WHERE source_url = $1", source_url)
        return row["mxc_url"] if row else None

    async def record_custom_emoji_mxc(self, source_url: str, mxc_url: str) -> None:
        await self._pool.execute(
            "INSERT INTO custom_emoji (source_url, mxc_url) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            source_url, mxc_url,
        )

    async def get_custom_emoji_by_reaction_event_ids(self, event_ids: list[str]) -> dict[str, str]:
        if not event_ids:
            return {}
        rows = await self._pool.fetch(
            "SELECT event_id, custom_emoji_mxc FROM reactions "
            "WHERE event_id = ANY($1::text[]) AND custom_emoji_mxc IS NOT NULL",
            event_ids,
        )
        return {row["event_id"]: row["custom_emoji_mxc"] for row in rows}

    async def record_resolved_emoji(self, subject_id: str, shortcode: str, mxc_url: str) -> None:
        await self._pool.execute(
            "INSERT INTO resolved_emoji (subject_id, shortcode, mxc_url) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            subject_id, shortcode, mxc_url,
        )

    async def get_resolved_emoji(self, subject_id: str) -> dict[str, str]:
        rows = await self._pool.fetch(
            "SELECT shortcode, mxc_url FROM resolved_emoji WHERE subject_id = $1", subject_id
        )
        return {row["shortcode"]: row["mxc_url"] for row in rows}

    @staticmethod
    def _row_to_reaction(row: asyncpg.Record) -> ReactionRecord:
        return ReactionRecord(
            activity_id=row["activity_id"],
            room_id=row["room_id"],
            event_id=row["event_id"],
            target_ap_object_id=row["target_ap_object_id"],
            reactor_ghost_mxid=row["reactor_ghost_mxid"],
            reactor_matrix_user_id=row["reactor_matrix_user_id"],
            secondary_event_id=row["secondary_event_id"],
            custom_emoji_mxc=row["custom_emoji_mxc"],
        )

    # -- ghost profile sync cache -------------------------------------------

    @staticmethod
    def _row_to_ghost_profile(row: asyncpg.Record) -> GhostProfile:
        return GhostProfile(
            actor_id=row["actor_id"],
            display_name=row["display_name"],
            icon_url=row["icon_url"],
            mxid=row["mxid"],
            handle=row["handle"],
        )

    async def get_ghost_profile(self, actor_id: str) -> GhostProfile | None:
        row = await self._pool.fetchrow("SELECT * FROM ghost_profiles WHERE actor_id = $1", actor_id)
        return self._row_to_ghost_profile(row) if row else None

    async def get_ghost_profile_by_mxid(self, mxid: str) -> GhostProfile | None:
        row = await self._pool.fetchrow("SELECT * FROM ghost_profiles WHERE mxid = $1", mxid)
        return self._row_to_ghost_profile(row) if row else None

    async def record_ghost_profile(self, profile: GhostProfile) -> None:
        await self._pool.execute(
            """
            INSERT INTO ghost_profiles (actor_id, display_name, icon_url, mxid, handle)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (actor_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                icon_url = EXCLUDED.icon_url,
                mxid = EXCLUDED.mxid,
                handle = EXCLUDED.handle
            """,
            profile.actor_id, profile.display_name, profile.icon_url, profile.mxid, profile.handle,
        )

    # -- user spaces -------------------------------------------------------

    async def get_user_space(self, matrix_user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT space_room_id FROM user_spaces WHERE matrix_user_id = $1", matrix_user_id
        )
        return row["space_room_id"] if row else None

    async def register_user_space(self, matrix_user_id: str, space_room_id: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO user_spaces (matrix_user_id, space_room_id) VALUES ($1, $2)
            ON CONFLICT (matrix_user_id) DO UPDATE SET space_room_id = EXCLUDED.space_room_id
            """,
            matrix_user_id, space_room_id,
        )

    # -- bot DM rooms --------------------------------------------------------

    async def get_bot_dm_room(self, matrix_user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT room_id FROM bot_dm_rooms WHERE matrix_user_id = $1", matrix_user_id
        )
        return row["room_id"] if row else None

    async def register_bot_dm_room(self, matrix_user_id: str, room_id: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO bot_dm_rooms (matrix_user_id, room_id) VALUES ($1, $2)
            ON CONFLICT (matrix_user_id) DO UPDATE SET room_id = EXCLUDED.room_id
            """,
            matrix_user_id, room_id,
        )

    async def get_bot_dm_room_owner(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT matrix_user_id FROM bot_dm_rooms WHERE room_id = $1", room_id)
        return row["matrix_user_id"] if row else None

    # -- ghost DM rooms -------------------------------------------------------

    async def get_ghost_dm_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT room_id FROM ghost_dm_rooms WHERE actor_id = $1 AND matrix_user_id = $2",
            actor_id, matrix_user_id,
        )
        return row["room_id"] if row else None

    async def register_ghost_dm_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO ghost_dm_rooms (actor_id, matrix_user_id, room_id) VALUES ($1, $2, $3)
                    ON CONFLICT (actor_id, matrix_user_id) DO UPDATE SET room_id = EXCLUDED.room_id
                    """,
                    actor_id, matrix_user_id, room_id,
                )
                # Permanent history row -- see get_ghost_dm_room_history's
                # docstring. Clear the old "current" flag first so the
                # partial unique index never sees two current rows at once.
                await conn.execute(
                    """
                    UPDATE ghost_dm_room_history SET is_current = FALSE
                    WHERE actor_id = $1 AND matrix_user_id = $2 AND is_current
                    """,
                    actor_id, matrix_user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO ghost_dm_room_history (room_id, actor_id, matrix_user_id, is_current)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                    """,
                    room_id, actor_id, matrix_user_id,
                )

    async def get_ghost_dm_room_history(self, room_id: str) -> tuple[str, str] | None:
        row = await self._pool.fetchrow(
            "SELECT actor_id, matrix_user_id FROM ghost_dm_room_history WHERE room_id = $1", room_id
        )
        return (row["actor_id"], row["matrix_user_id"]) if row else None

    async def is_ghost_dm_room(self, room_id: str) -> bool:
        row = await self._pool.fetchrow("SELECT 1 FROM ghost_dm_rooms WHERE room_id = $1", room_id)
        return row is not None

    async def get_ghost_dm_room_actor_id(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT actor_id FROM ghost_dm_rooms WHERE room_id = $1", room_id)
        return row["actor_id"] if row else None

    async def get_ghost_dm_room_matrix_user_id(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT matrix_user_id FROM ghost_dm_rooms WHERE room_id = $1", room_id)
        return row["matrix_user_id"] if row else None

    async def get_ghost_dm_room_ids_for_actor(self, actor_id: str) -> list[str]:
        rows = await self._pool.fetch("SELECT room_id FROM ghost_dm_rooms WHERE actor_id = $1", actor_id)
        return [row["room_id"] for row in rows]

    # -- ghost chat rooms (ActivityPub ChatMessage, distinct from DM) -------

    async def get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT room_id FROM ghost_chat_rooms WHERE actor_id = $1 AND matrix_user_id = $2",
            actor_id, matrix_user_id,
        )
        return row["room_id"] if row else None

    async def register_ghost_chat_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO ghost_chat_rooms (actor_id, matrix_user_id, room_id) VALUES ($1, $2, $3)
                    ON CONFLICT (actor_id, matrix_user_id) DO UPDATE SET room_id = EXCLUDED.room_id
                    """,
                    actor_id, matrix_user_id, room_id,
                )
                # Permanent history row -- see get_ghost_chat_room_history's
                # docstring. Clear the old "current" flag first so the
                # partial unique index never sees two current rows at once.
                await conn.execute(
                    """
                    UPDATE ghost_chat_room_history SET is_current = FALSE
                    WHERE actor_id = $1 AND matrix_user_id = $2 AND is_current
                    """,
                    actor_id, matrix_user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO ghost_chat_room_history (room_id, actor_id, matrix_user_id, is_current)
                    VALUES ($1, $2, $3, TRUE)
                    ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                    """,
                    room_id, actor_id, matrix_user_id,
                )

    async def get_ghost_chat_room_history(self, room_id: str) -> tuple[str, str] | None:
        row = await self._pool.fetchrow(
            "SELECT actor_id, matrix_user_id FROM ghost_chat_room_history WHERE room_id = $1", room_id
        )
        return (row["actor_id"], row["matrix_user_id"]) if row else None

    async def is_ghost_chat_room(self, room_id: str) -> bool:
        row = await self._pool.fetchrow("SELECT 1 FROM ghost_chat_rooms WHERE room_id = $1", room_id)
        return row is not None

    async def get_ghost_chat_room_actor_id(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT actor_id FROM ghost_chat_rooms WHERE room_id = $1", room_id)
        return row["actor_id"] if row else None

    async def get_ghost_chat_room_matrix_user_id(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT matrix_user_id FROM ghost_chat_rooms WHERE room_id = $1", room_id)
        return row["matrix_user_id"] if row else None

    async def get_ghost_chat_room_ids_for_actor(self, actor_id: str) -> list[str]:
        rows = await self._pool.fetch("SELECT room_id FROM ghost_chat_rooms WHERE actor_id = $1", actor_id)
        return [row["room_id"] for row in rows]
