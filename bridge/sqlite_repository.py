"""SQLite-backed ``ActorRepository`` -- durable storage for bridge bookkeeping.

Implements the same protocol as ``InMemoryActorRepository`` but persists
everything (linked profiles and their keys, the bridge's service actor,
follower/following sets, remote-actor-room mappings, the bidirectional
Matrix-event/AP-object map, AppService transaction idempotency, and the
public-media allowlist) to a single sqlite file under ``storage.data_dir``,
so none of it is lost across restarts. This is the default repository used
by ``bridge.server.create_app``.

Per the project's data-sovereignty constraint this stores only bridge
bookkeeping -- no post content or media, which live exclusively in Matrix.

``sqlite3`` connections aren't safe to use concurrently from multiple
threads without care; this wraps every call in ``asyncio.to_thread`` behind
a single ``asyncio.Lock`` so the (fast, local, low-volume) database access
never blocks the event loop while still serializing access deterministically.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from bridge.repository import (
    ActorRecord,
    ChannelRoom,
    FederatedEvent,
    GhostProfile,
    GuildChannel,
    GuildMembership,
    PendingGuildFollow,
    PollVoteRecord,
    ReactionRecord,
    RemoteActorRoom,
    ThirdPartyAllowRecord,
)

_SCHEMA = """
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
    hide_followers INTEGER NOT NULL DEFAULT 0,
    hide_following INTEGER NOT NULL DEFAULT 0,
    is_third_party INTEGER NOT NULL DEFAULT 0
);

-- Admin-granted third-party access (see ActorRecord.is_third_party and the
-- ";allow"/";disallow"/";allowed" commands). rule_type is 'mxid', 'room', or
-- 'homeserver'; value is the corresponding exact MXID/room ID/domain.
CREATE TABLE IF NOT EXISTS third_party_allowlist (
    rule_type TEXT NOT NULL,
    value TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (rule_type, value)
);

CREATE TABLE IF NOT EXISTS profile_room_history (
    room_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followers (
    username TEXT NOT NULL,
    remote_actor_id TEXT NOT NULL,
    PRIMARY KEY (username, remote_actor_id)
);

CREATE TABLE IF NOT EXISTS following (
    username TEXT NOT NULL,
    remote_actor_id TEXT NOT NULL,
    PRIMARY KEY (username, remote_actor_id)
);

CREATE TABLE IF NOT EXISTS blocked_actors (
    username TEXT NOT NULL,
    remote_actor_id TEXT NOT NULL,
    PRIMARY KEY (username, remote_actor_id)
);

CREATE TABLE IF NOT EXISTS muted_actors (
    username TEXT NOT NULL,
    remote_actor_id TEXT NOT NULL,
    PRIMARY KEY (username, remote_actor_id)
);

CREATE TABLE IF NOT EXISTS remote_actor_rooms (
    actor_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL UNIQUE,
    ghost_user_id TEXT NOT NULL,
    inbox_url TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    icon_url TEXT,
    banner_url TEXT,
    pending_backfill INTEGER NOT NULL DEFAULT 0
);

-- ap_object_id is deliberately NOT globally UNIQUE here (a plain column
-- constraint can't be scoped) -- see the partial unique index _migrate()
-- creates instead (NOT here -- an existing pre-is_primary_event database
-- doesn't have that column yet at the point this script runs, so a
-- WHERE clause referencing it would fail against one), and
-- record_federated_event's docstring for why: a multi-attachment post's
-- 2nd+ attachment gets its own FederatedEvent row sharing the SAME
-- ap_object_id as the post's primary row (so reacting to it still
-- resolves correctly), which a table-wide UNIQUE would reject.
CREATE TABLE IF NOT EXISTS federated_events (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    ap_object_id TEXT NOT NULL,
    author_actor_id TEXT NOT NULL,
    thread_root_event_id TEXT,
    reposted_object_id TEXT,
    reposted_author_actor_id TEXT,
    is_primary_event INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS processed_transactions (
    txn_id TEXT PRIMARY KEY,
    processed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS published_media (
    mxc_uri TEXT PRIMARY KEY,
    published_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reactions (
    activity_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    target_ap_object_id TEXT NOT NULL,
    reactor_ghost_mxid TEXT,
    reactor_matrix_user_id TEXT,
    secondary_event_id TEXT UNIQUE
);

-- Idempotency bookkeeping for poll votes, both directions -- see
-- PollVoteRecord's own docstring for why this table exists and what it
-- deliberately does NOT store (which option was chosen).
CREATE TABLE IF NOT EXISTS poll_votes (
    vote_activity_id TEXT PRIMARY KEY,
    question_ap_object_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    voter_actor_id TEXT,
    matrix_user_id TEXT,
    matrix_event_id TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_poll_votes_question_user
    ON poll_votes (question_ap_object_id, matrix_user_id);

CREATE TABLE IF NOT EXISTS ghost_profiles (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT,
    icon_url TEXT,
    mxid TEXT,
    handle TEXT
);

CREATE TABLE IF NOT EXISTS user_spaces (
    matrix_user_id TEXT PRIMARY KEY,
    space_room_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_dm_rooms (
    matrix_user_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ghost_dm_rooms (
    actor_id TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL,
    room_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (actor_id, matrix_user_id)
);

-- Deliberately separate from ghost_dm_rooms -- see
-- ActorRepository.get_ghost_chat_room's docstring for why a Note-based DM
-- and an ActivityPub ChatMessage are different wire formats a room commits
-- to for its whole lifetime, so the same (ghost, local user) pair can have
-- up to one room of EACH kind, never one room serving both.
CREATE TABLE IF NOT EXISTS ghost_chat_rooms (
    actor_id TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL,
    room_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (actor_id, matrix_user_id)
);

-- Dedup cache for custom-emoji reaction images (see
-- ActorRepository.get_custom_emoji_mxc) -- source_url is the remote emoji
-- image's own URL (unique per emoji per source instance), mxc_url is where
-- it was uploaded to Synapse's media repo. Checked before ever fetching one,
-- so the same emoji is only ever downloaded/uploaded once no matter how many
-- reactions use it.
CREATE TABLE IF NOT EXISTS custom_emoji (
    source_url TEXT PRIMARY KEY,
    mxc_url TEXT NOT NULL
);

-- Per-subject resolved custom emoji (see ActorRepository.record_resolved_emoji)
-- -- subject_id is either a post's ap_object_id (content emoji) or a remote
-- actor's own actor_id (display-name emoji). Lets a later re-render show
-- the right image without the original AP tag data still being around.
CREATE TABLE IF NOT EXISTS resolved_emoji (
    subject_id TEXT NOT NULL,
    shortcode TEXT NOT NULL,
    mxc_url TEXT NOT NULL,
    PRIMARY KEY (subject_id, shortcode)
);

-- Permanent, append-only history for Remote User Rooms / ghost DM rooms /
-- ghost Chat rooms -- the profile_room_history treatment, extended to the
-- two other room kinds whose own primary tables above only ever reflect
-- the CURRENT room per actor (their upsert overwrites room_id in place on
-- replace, no history kept there). Only one row per key may have
-- is_current set -- enforced by the partial unique indexes in _migrate(),
-- not just application logic. See
-- bridge.note_mirroring.resolve_old_remote_actor_room/
-- resolve_old_ghost_room_owner, which check these tables first and only
-- fall back to reading a room's own state back out of Synapse for a room
-- old enough to predate this table's introduction.
CREATE TABLE IF NOT EXISTS remote_actor_room_history (
    room_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ghost_dm_room_history (
    room_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ghost_chat_room_history (
    room_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0
);

-- Shoot Guild/Channel bridging (see bridge.channel_bridge) -- an outstanding
-- FEP-bebd guild-join Follow, between sending it and the guild's
-- Accept/Reject arriving back over the inbox. See
-- ActorRepository.record_pending_guild_follow's docstring.
CREATE TABLE IF NOT EXISTS pending_guild_follows (
    follow_id TEXT PRIMARY KEY,
    guild_actor_id TEXT NOT NULL,
    username TEXT NOT NULL,
    matrix_user_id TEXT NOT NULL,
    invite_code TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_memberships (
    username TEXT NOT NULL,
    guild_actor_id TEXT NOT NULL,
    PRIMARY KEY (username, guild_actor_id)
);

-- Separate from guild_memberships -- the Space is one-per-guild, shared by
-- every local member, not one-per-membership-row. Same shape as
-- user_spaces, just keyed by guild instead of by Matrix user.
CREATE TABLE IF NOT EXISTS guild_spaces (
    guild_actor_id TEXT PRIMARY KEY,
    space_room_id TEXT NOT NULL
);

-- A joined guild's own flat `channels` collection, cached once right after
-- the join Accept -- see ActorRepository.get_guild_channel's docstring for
-- why this makes inbound Announce disambiguation a table lookup instead of
-- a live fetch per message.
CREATE TABLE IF NOT EXISTS guild_channels (
    channel_actor_id TEXT PRIMARY KEY,
    guild_actor_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_guild_channels_guild ON guild_channels (guild_actor_id);

-- One Matrix room per Shoot Channel -- deliberately no single ghost_user_id
-- column the way remote_actor_rooms has one, since a channel's messages
-- come from many different guild-member ghosts, resolved per-message.
CREATE TABLE IF NOT EXISTS channel_rooms (
    channel_actor_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL UNIQUE,
    guild_actor_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT ''
);

-- Which member ghosts have already been seen/invited in a channel room --
-- pure optimization (resolve_and_invite_ghost is already idempotent on its
-- own), not a correctness dependency.
CREATE TABLE IF NOT EXISTS channel_room_members (
    room_id TEXT NOT NULL,
    member_actor_id TEXT NOT NULL,
    PRIMARY KEY (room_id, member_actor_id)
);
"""

_TRANSACTION_RETENTION_SECONDS = 7 * 24 * 3600


class SqliteActorRepository:
    """Durable ``ActorRepository``. One instance per process -- not safe to
    share a single sqlite file between multiple concurrently-running bridge
    processes."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()
        self._migrate()
        self._lock = asyncio.Lock()

    def _migrate(self) -> None:
        """Add columns introduced after a table's initial ``CREATE TABLE IF NOT EXISTS``
        ran against an existing database file."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(local_actors)")}
        if "banner_url" not in columns:
            self._conn.execute("ALTER TABLE local_actors ADD COLUMN banner_url TEXT")
            self._conn.commit()
        if "hide_followers" not in columns:
            self._conn.execute("ALTER TABLE local_actors ADD COLUMN hide_followers INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE local_actors ADD COLUMN hide_following INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        if "is_third_party" not in columns:
            self._conn.execute("ALTER TABLE local_actors ADD COLUMN is_third_party INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

        # is_current gives profile_room_history the same DB-enforced "only
        # one current room per identity" guarantee as the three newer
        # *_room_history tables -- previously that guarantee only came from
        # local_actors.room_id being a single column, correct by
        # construction but not something profile_room_history itself could
        # answer without joining back to local_actors.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(profile_room_history)")}
        if "is_current" not in columns:
            self._conn.execute("ALTER TABLE profile_room_history ADD COLUMN is_current INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_room_history_current "
            "ON profile_room_history (username) WHERE is_current = 1"
        )
        self._conn.commit()
        # Backfill: mark whichever row matches each username's CURRENT room
        # (per local_actors) as current -- cheap and idempotent, runs
        # unconditionally on every startup like the other backfills here.
        self._conn.execute(
            """
            UPDATE profile_room_history SET is_current = 1
            WHERE is_current = 0 AND room_id IN (
                SELECT room_id FROM local_actors WHERE room_id <> ''
            )
            """
        )
        self._conn.commit()

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(remote_actor_rooms)")}
        if "icon_url" not in columns:
            self._conn.execute("ALTER TABLE remote_actor_rooms ADD COLUMN icon_url TEXT")
            self._conn.commit()
        if "banner_url" not in columns:
            self._conn.execute("ALTER TABLE remote_actor_rooms ADD COLUMN banner_url TEXT")
            self._conn.commit()
        if "pending_backfill" not in columns:
            self._conn.execute(
                "ALTER TABLE remote_actor_rooms ADD COLUMN pending_backfill INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.commit()

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(federated_events)")}
        if "thread_root_event_id" not in columns:
            self._conn.execute("ALTER TABLE federated_events ADD COLUMN thread_root_event_id TEXT")
            self._conn.commit()
        if "boosted_object_id" in columns and "reposted_object_id" not in columns:
            # Renamed 2026-07-11 (boost -> repost terminology, project-wide)
            # -- RENAME COLUMN preserves the existing data, unlike an
            # ADD-COLUMN-then-drop, which is why this isn't just another
            # ADD-COLUMN-IF-NOT-EXISTS guard like the others here.
            self._conn.execute("ALTER TABLE federated_events RENAME COLUMN boosted_object_id TO reposted_object_id")
            self._conn.execute(
                "ALTER TABLE federated_events RENAME COLUMN boosted_author_actor_id TO reposted_author_actor_id"
            )
            self._conn.commit()
        elif "reposted_object_id" not in columns:
            self._conn.execute("ALTER TABLE federated_events ADD COLUMN reposted_object_id TEXT")
            self._conn.execute("ALTER TABLE federated_events ADD COLUMN reposted_author_actor_id TEXT")
            self._conn.commit()
        if "is_primary_event" not in columns:
            # A database from before is_primary_event existed still has the
            # OLD table-wide "ap_object_id ... UNIQUE" constraint (a plain
            # column constraint, not droppable via ALTER TABLE) -- adding
            # the column alone wouldn't be enough, since record_federated_event
            # relies on that UNIQUE being scoped to is_primary_event=1 rows
            # (see its docstring): a multi-attachment post's 2nd+ attachment
            # sharing the primary event's own ap_object_id would still hit
            # the old, unscoped constraint and fail exactly like the bug
            # this migration exists to fix. Full rebuild is sqlite's normal
            # way to change a table's constraints. Every existing row here
            # predates is_primary_event and is unconditionally the
            # (only, since the old UNIQUE already guaranteed that) primary
            # row for its ap_object_id.
            self._conn.execute("ALTER TABLE federated_events RENAME TO federated_events_old")
            self._conn.execute(
                """
                CREATE TABLE federated_events (
                    event_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    ap_object_id TEXT NOT NULL,
                    author_actor_id TEXT NOT NULL,
                    thread_root_event_id TEXT,
                    reposted_object_id TEXT,
                    reposted_author_actor_id TEXT,
                    is_primary_event INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO federated_events
                    (event_id, room_id, ap_object_id, author_actor_id,
                     thread_root_event_id, reposted_object_id, reposted_author_actor_id, is_primary_event)
                SELECT event_id, room_id, ap_object_id, author_actor_id,
                       thread_root_event_id, reposted_object_id, reposted_author_actor_id, 1
                FROM federated_events_old
                """
            )
            self._conn.execute("DROP TABLE federated_events_old")
            self._conn.commit()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_federated_events_primary_ap_object "
            "ON federated_events (ap_object_id) WHERE is_primary_event = 1"
        )
        self._conn.commit()

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(reactions)")}
        if "secondary_event_id" not in columns:
            # No inline UNIQUE here -- SQLite's ALTER TABLE ADD COLUMN doesn't
            # support column constraints, only a separate CREATE UNIQUE INDEX
            # (below, unconditional so a brand new database -- whose
            # CREATE TABLE already declares this column UNIQUE inline --
            # still gets an index, just a redundant one to the same effect).
            self._conn.execute("ALTER TABLE reactions ADD COLUMN secondary_event_id TEXT")
            self._conn.commit()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reactions_secondary_event_id "
            "ON reactions (secondary_event_id)"
        )
        self._conn.commit()

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(reactions)")}
        if "custom_emoji_mxc" not in columns:
            self._conn.execute("ALTER TABLE reactions ADD COLUMN custom_emoji_mxc TEXT")
            self._conn.commit()

        # mark_transaction_processed's own opportunistic cleanup DELETEs by
        # processed_at on every single write (once per AppService
        # transaction) -- without an index, that's a full table scan against
        # a table that, in practice, dwarfs every other table here (a row
        # per Matrix event batch this homeserver ever sends the bridge, not
        # just fediverse-relevant ones).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_transactions_processed_at "
            "ON processed_transactions (processed_at)"
        )
        self._conn.commit()

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(ghost_profiles)")}
        if "mxid" not in columns:
            self._conn.execute("ALTER TABLE ghost_profiles ADD COLUMN mxid TEXT")
            self._conn.execute("ALTER TABLE ghost_profiles ADD COLUMN handle TEXT")
            self._conn.commit()
        # Not nested in the migration branch above -- a brand new database
        # already has the mxid column from _SCHEMA's CREATE TABLE and would
        # otherwise never get this index at all.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ghost_profiles_mxid ON ghost_profiles (mxid)")
        self._conn.commit()

        # Backfill profile_room_history (added 2026-07) from each local
        # actor's CURRENT room -- cheap and idempotent (INSERT OR IGNORE), so
        # just runs unconditionally on every startup rather than needing its
        # own "have we done this before" tracking. This only recovers each
        # actor's room as of right now; a room that was already replaced
        # away from before this existed isn't recoverable from here (nothing
        # in local_actors remembers it), only from here forward.
        self._conn.execute(
            """
            INSERT OR IGNORE INTO profile_room_history (room_id, username, matrix_user_id)
            SELECT room_id, username, matrix_user_id FROM local_actors WHERE room_id <> ''
            """
        )
        self._conn.commit()

        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_actor_room_history_current "
            "ON remote_actor_room_history (actor_id) WHERE is_current = 1"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ghost_dm_room_history_current "
            "ON ghost_dm_room_history (actor_id, matrix_user_id) WHERE is_current = 1"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ghost_chat_room_history_current "
            "ON ghost_chat_room_history (actor_id, matrix_user_id) WHERE is_current = 1"
        )
        self._conn.commit()

        # Backfill each history table (added 2026-07) from its primary
        # table's CURRENT rows -- same reasoning/limitation as the
        # profile_room_history backfill above.
        self._conn.execute(
            """
            INSERT OR IGNORE INTO remote_actor_room_history (room_id, actor_id, is_current)
            SELECT room_id, actor_id, 1 FROM remote_actor_rooms
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ghost_dm_room_history (room_id, actor_id, matrix_user_id, is_current)
            SELECT room_id, actor_id, matrix_user_id, 1 FROM ghost_dm_rooms
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ghost_chat_room_history (room_id, actor_id, matrix_user_id, is_current)
            SELECT room_id, actor_id, matrix_user_id, 1 FROM ghost_chat_rooms
            """
        )
        self._conn.commit()

    async def close(self) -> None:
        # sqlite3's close() is fast/local -- no need for the asyncio.to_thread
        # offload the rest of this class uses for actual queries. async only
        # so callers (bridge.server) can treat every ActorRepository
        # implementation's close() uniformly, since
        # PostgresActorRepository's (an actual network round-trip) has to be.
        self._conn.close()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # -- local actors ------------------------------------------------------

    def _get_local_actor(self, username: str) -> ActorRecord | None:
        row = self._conn.execute(
            "SELECT * FROM local_actors WHERE username = ?", (username,)
        ).fetchone()
        return self._row_to_actor(row) if row else None

    async def get_local_actor(self, username: str) -> ActorRecord | None:
        return await self._run(self._get_local_actor, username)

    def _get_local_actor_by_matrix_id(self, matrix_user_id: str) -> ActorRecord | None:
        row = self._conn.execute(
            "SELECT * FROM local_actors WHERE matrix_user_id = ?", (matrix_user_id,)
        ).fetchone()
        return self._row_to_actor(row) if row else None

    async def get_local_actor_by_matrix_id(self, matrix_user_id: str) -> ActorRecord | None:
        return await self._run(self._get_local_actor_by_matrix_id, matrix_user_id)

    def _get_local_actor_by_room_id(self, room_id: str) -> ActorRecord | None:
        if not room_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM local_actors WHERE room_id = ? AND room_id <> ''", (room_id,)
        ).fetchone()
        return self._row_to_actor(row) if row else None

    async def get_local_actor_by_room_id(self, room_id: str) -> ActorRecord | None:
        return await self._run(self._get_local_actor_by_room_id, room_id)

    def _get_profile_room_owner(self, room_id: str) -> str | None:
        if not room_id:
            return None
        row = self._conn.execute(
            "SELECT matrix_user_id FROM profile_room_history WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["matrix_user_id"] if row else None

    async def get_profile_room_owner(self, room_id: str) -> str | None:
        return await self._run(self._get_profile_room_owner, room_id)

    def _get_profile_room_history(self, username: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT room_id FROM profile_room_history WHERE username = ?", (username,)
        ).fetchall()
        return [row["room_id"] for row in rows]

    async def get_profile_room_history(self, username: str) -> list[str]:
        return await self._run(self._get_profile_room_history, username)

    def _register_local_actor(self, record: ActorRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO local_actors
                (username, matrix_user_id, room_id, public_key_pem, private_key_pem,
                 display_name, summary, icon_url, banner_url, hide_followers, hide_following,
                 is_third_party)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                matrix_user_id=excluded.matrix_user_id,
                room_id=excluded.room_id,
                public_key_pem=excluded.public_key_pem,
                private_key_pem=excluded.private_key_pem,
                display_name=excluded.display_name,
                summary=excluded.summary,
                icon_url=excluded.icon_url,
                banner_url=excluded.banner_url,
                hide_followers=excluded.hide_followers,
                hide_following=excluded.hide_following,
                is_third_party=excluded.is_third_party
            """,
            (
                record.username, record.matrix_user_id, record.room_id,
                record.public_key_pem, record.private_key_pem,
                record.display_name, record.summary, record.icon_url, record.banner_url,
                int(record.hide_followers), int(record.hide_following), int(record.is_third_party),
            ),
        )
        if record.room_id:
            # Permanent, unlike local_actors.room_id itself -- a `replace
            # room` overwrites that column with the new room, but this room
            # genuinely was (part of) this actor's history and should stay
            # provably theirs for ownership checks (e.g. the `rejoin`
            # command) even after they've moved on from it. is_current
            # mirrors the same guarantee the three newer *_room_history
            # tables enforce -- clear the old flag first so the partial
            # unique index never sees two current rows for the same
            # username at once.
            self._conn.execute(
                "UPDATE profile_room_history SET is_current = 0 WHERE username = ? AND is_current = 1",
                (record.username,),
            )
            self._conn.execute(
                """
                INSERT INTO profile_room_history (room_id, username, matrix_user_id, is_current)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(room_id) DO UPDATE SET is_current = 1
                """,
                (record.room_id, record.username, record.matrix_user_id),
            )
        self._conn.commit()

    async def register_local_actor(self, record: ActorRecord) -> None:
        await self._run(self._register_local_actor, record)

    def _unregister_local_actor(self, username: str) -> None:
        self._conn.execute("DELETE FROM local_actors WHERE username = ?", (username,))
        self._conn.execute("DELETE FROM followers WHERE username = ?", (username,))
        self._conn.execute("DELETE FROM following WHERE username = ?", (username,))
        self._conn.execute("DELETE FROM blocked_actors WHERE username = ?", (username,))
        self._conn.execute("DELETE FROM muted_actors WHERE username = ?", (username,))
        self._conn.commit()

    async def unregister_local_actor(self, username: str) -> None:
        await self._run(self._unregister_local_actor, username)

    @staticmethod
    def _row_to_actor(row: sqlite3.Row) -> ActorRecord:
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
            hide_followers=bool(row["hide_followers"]),
            hide_following=bool(row["hide_following"]),
            is_third_party=bool(row["is_third_party"]),
        )

    # -- followers / following ----------------------------------------------

    def _list_followers(self, username: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT remote_actor_id FROM followers WHERE username = ? ORDER BY remote_actor_id",
            (username,),
        ).fetchall()
        return [row["remote_actor_id"] for row in rows]

    async def list_followers(self, username: str) -> list[str]:
        return await self._run(self._list_followers, username)

    def _list_following(self, username: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT remote_actor_id FROM following WHERE username = ? ORDER BY remote_actor_id",
            (username,),
        ).fetchall()
        return [row["remote_actor_id"] for row in rows]

    async def list_following(self, username: str) -> list[str]:
        return await self._run(self._list_following, username)

    def _is_following(self, username: str, remote_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM following WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        ).fetchone()
        return row is not None

    async def is_following(self, username: str, remote_actor_id: str) -> bool:
        return await self._run(self._is_following, username, remote_actor_id)

    def _is_anyone_following(self, remote_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM following WHERE remote_actor_id = ? LIMIT 1", (remote_actor_id,)
        ).fetchone()
        return row is not None

    async def is_anyone_following(self, remote_actor_id: str) -> bool:
        return await self._run(self._is_anyone_following, remote_actor_id)

    def _add_follower(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO followers (username, remote_actor_id) VALUES (?, ?)",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def add_follower(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._add_follower, username, remote_actor_id)

    def _remove_follower(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "DELETE FROM followers WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def remove_follower(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._remove_follower, username, remote_actor_id)

    def _add_following(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO following (username, remote_actor_id) VALUES (?, ?)",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def add_following(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._add_following, username, remote_actor_id)

    def _remove_following(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "DELETE FROM following WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def remove_following(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._remove_following, username, remote_actor_id)

    def _set_followers_hidden(self, username: str, hidden: bool) -> None:
        self._conn.execute(
            "UPDATE local_actors SET hide_followers = ? WHERE username = ?", (int(hidden), username)
        )
        self._conn.commit()

    async def set_followers_hidden(self, username: str, hidden: bool) -> None:
        await self._run(self._set_followers_hidden, username, hidden)

    def _set_following_hidden(self, username: str, hidden: bool) -> None:
        self._conn.execute(
            "UPDATE local_actors SET hide_following = ? WHERE username = ?", (int(hidden), username)
        )
        self._conn.commit()

    async def set_following_hidden(self, username: str, hidden: bool) -> None:
        await self._run(self._set_following_hidden, username, hidden)

    def _list_third_party_records(self) -> list[ActorRecord]:
        rows = self._conn.execute(
            "SELECT * FROM local_actors WHERE is_third_party = 1 AND room_id = ''"
        ).fetchall()
        return [self._row_to_actor(row) for row in rows]

    async def list_third_party_records(self) -> list[ActorRecord]:
        return await self._run(self._list_third_party_records)

    def _is_blocked(self, username: str, remote_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocked_actors WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        ).fetchone()
        return row is not None

    async def is_blocked(self, username: str, remote_actor_id: str) -> bool:
        return await self._run(self._is_blocked, username, remote_actor_id)

    def _add_blocked(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO blocked_actors (username, remote_actor_id) VALUES (?, ?)",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def add_blocked(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._add_blocked, username, remote_actor_id)

    def _remove_blocked(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "DELETE FROM blocked_actors WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def remove_blocked(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._remove_blocked, username, remote_actor_id)

    def _is_muted(self, username: str, remote_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM muted_actors WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        ).fetchone()
        return row is not None

    async def is_muted(self, username: str, remote_actor_id: str) -> bool:
        return await self._run(self._is_muted, username, remote_actor_id)

    def _add_muted(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO muted_actors (username, remote_actor_id) VALUES (?, ?)",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def add_muted(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._add_muted, username, remote_actor_id)

    def _remove_muted(self, username: str, remote_actor_id: str) -> None:
        self._conn.execute(
            "DELETE FROM muted_actors WHERE username = ? AND remote_actor_id = ?",
            (username, remote_actor_id),
        )
        self._conn.commit()

    async def remove_muted(self, username: str, remote_actor_id: str) -> None:
        await self._run(self._remove_muted, username, remote_actor_id)

    # -- third-party allowlist -----------------------------------------------

    def _add_third_party_allow(self, rule_type: str, value: str, granted_by: str) -> None:
        self._conn.execute(
            """
            INSERT INTO third_party_allowlist (rule_type, value, granted_by, granted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_type, value) DO UPDATE SET
                granted_by=excluded.granted_by,
                granted_at=excluded.granted_at
            """,
            (rule_type, value, granted_by, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    async def add_third_party_allow(self, rule_type: str, value: str, *, granted_by: str) -> None:
        await self._run(self._add_third_party_allow, rule_type, value, granted_by)

    def _remove_third_party_allow(self, rule_type: str, value: str) -> None:
        self._conn.execute(
            "DELETE FROM third_party_allowlist WHERE rule_type = ? AND value = ?", (rule_type, value)
        )
        self._conn.commit()

    async def remove_third_party_allow(self, rule_type: str, value: str) -> None:
        await self._run(self._remove_third_party_allow, rule_type, value)

    def _list_third_party_allows(self, rule_type: str | None) -> list[ThirdPartyAllowRecord]:
        if rule_type is not None:
            rows = self._conn.execute(
                "SELECT * FROM third_party_allowlist WHERE rule_type = ? ORDER BY rule_type, value",
                (rule_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM third_party_allowlist ORDER BY rule_type, value"
            ).fetchall()
        return [
            ThirdPartyAllowRecord(
                rule_type=row["rule_type"],
                value=row["value"],
                granted_by=row["granted_by"],
                granted_at=row["granted_at"],
            )
            for row in rows
        ]

    async def list_third_party_allows(self, rule_type: str | None = None) -> list[ThirdPartyAllowRecord]:
        return await self._run(self._list_third_party_allows, rule_type)

    def _is_third_party_allowed(self, mxid: str, homeserver: str, room_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'mxid' AND value = ?", (mxid,)
        ).fetchone()
        if row is not None:
            return True
        if room_id:
            row = self._conn.execute(
                "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'room' AND value = ?", (room_id,)
            ).fetchone()
            if row is not None:
                return True
        if homeserver:
            row = self._conn.execute(
                "SELECT 1 FROM third_party_allowlist WHERE rule_type = 'homeserver' AND value = ?",
                (homeserver,),
            ).fetchone()
            if row is not None:
                return True
        return False

    async def is_third_party_allowed(self, *, mxid: str, homeserver: str, room_id: str) -> bool:
        return await self._run(self._is_third_party_allowed, mxid, homeserver, room_id)

    # -- remote actor rooms --------------------------------------------------

    def _get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None:
        row = self._conn.execute(
            "SELECT * FROM remote_actor_rooms WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        return self._row_to_remote_room(row) if row else None

    async def get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None:
        return await self._run(self._get_remote_actor_room, actor_id)

    def _get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None:
        row = self._conn.execute(
            "SELECT * FROM remote_actor_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return self._row_to_remote_room(row) if row else None

    async def get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None:
        return await self._run(self._get_remote_actor_room_by_room_id, room_id)

    def _list_all_remote_actor_room_ids(self) -> list[str]:
        rows = self._conn.execute("SELECT room_id FROM remote_actor_rooms").fetchall()
        return [row["room_id"] for row in rows]

    async def list_all_remote_actor_room_ids(self) -> list[str]:
        return await self._run(self._list_all_remote_actor_room_ids)

    def _register_remote_actor_room(self, record: RemoteActorRoom) -> None:
        # pending_backfill is deliberately NOT in the DO UPDATE SET below --
        # see the identical reasoning in PostgresActorRepository's version
        # of this method.
        self._conn.execute(
            """
            INSERT INTO remote_actor_rooms
                (actor_id, room_id, ghost_user_id, inbox_url, display_name, icon_url, banner_url, pending_backfill)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET
                room_id=excluded.room_id,
                ghost_user_id=excluded.ghost_user_id,
                inbox_url=excluded.inbox_url,
                display_name=excluded.display_name,
                icon_url=excluded.icon_url,
                banner_url=excluded.banner_url
            """,
            (
                record.actor_id, record.room_id, record.ghost_user_id,
                record.inbox_url, record.display_name, record.icon_url, record.banner_url, record.pending_backfill,
            ),
        )
        # Permanent history row -- see get_remote_actor_room_history_actor_id's
        # docstring. Clear the old "current" flag first so the partial
        # unique index never sees two current rows for the same actor.
        self._conn.execute(
            "UPDATE remote_actor_room_history SET is_current = 0 WHERE actor_id = ? AND is_current = 1",
            (record.actor_id,),
        )
        self._conn.execute(
            """
            INSERT INTO remote_actor_room_history (room_id, actor_id, is_current) VALUES (?, ?, 1)
            ON CONFLICT(room_id) DO UPDATE SET is_current = 1
            """,
            (record.room_id, record.actor_id),
        )
        self._conn.commit()

    async def register_remote_actor_room(self, record: RemoteActorRoom) -> None:
        await self._run(self._register_remote_actor_room, record)

    def _get_remote_actor_room_history_actor_id(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT actor_id FROM remote_actor_room_history WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["actor_id"] if row else None

    async def get_remote_actor_room_history_actor_id(self, room_id: str) -> str | None:
        return await self._run(self._get_remote_actor_room_history_actor_id, room_id)

    def _mark_backfill_pending_done(self, room_id: str) -> None:
        self._conn.execute("UPDATE remote_actor_rooms SET pending_backfill = 0 WHERE room_id = ?", (room_id,))
        self._conn.commit()

    async def mark_backfill_pending_done(self, room_id: str) -> None:
        await self._run(self._mark_backfill_pending_done, room_id)

    @staticmethod
    def _row_to_remote_room(row: sqlite3.Row) -> RemoteActorRoom:
        return RemoteActorRoom(
            actor_id=row["actor_id"],
            room_id=row["room_id"],
            ghost_user_id=row["ghost_user_id"],
            inbox_url=row["inbox_url"],
            display_name=row["display_name"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
            pending_backfill=bool(row["pending_backfill"]),
        )

    # -- federated event map --------------------------------------------------

    def _record_federated_event(self, record: FederatedEvent, is_primary: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO federated_events
                (event_id, room_id, ap_object_id, author_actor_id, thread_root_event_id,
                 reposted_object_id, reposted_author_actor_id, is_primary_event)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                room_id=excluded.room_id,
                ap_object_id=excluded.ap_object_id,
                author_actor_id=excluded.author_actor_id,
                thread_root_event_id=excluded.thread_root_event_id,
                reposted_object_id=excluded.reposted_object_id,
                reposted_author_actor_id=excluded.reposted_author_actor_id,
                is_primary_event=excluded.is_primary_event
            """,
            (
                record.event_id, record.room_id, record.ap_object_id,
                record.author_actor_id, record.thread_root_event_id,
                record.reposted_object_id, record.reposted_author_actor_id,
                1 if is_primary else 0,
            ),
        )
        self._conn.commit()

    async def record_federated_event(self, record: FederatedEvent, *, is_primary: bool = True) -> None:
        await self._run(self._record_federated_event, record, is_primary)

    def _get_federated_event_by_matrix_event(self, event_id: str) -> FederatedEvent | None:
        row = self._conn.execute(
            "SELECT * FROM federated_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_to_federated_event(row) if row else None

    async def get_federated_event_by_matrix_event(self, event_id: str) -> FederatedEvent | None:
        return await self._run(self._get_federated_event_by_matrix_event, event_id)

    def _get_federated_event_by_ap_object(self, ap_object_id: str) -> FederatedEvent | None:
        row = self._conn.execute(
            "SELECT * FROM federated_events WHERE ap_object_id = ? AND is_primary_event = 1", (ap_object_id,)
        ).fetchone()
        return self._row_to_federated_event(row) if row else None

    async def get_federated_event_by_ap_object(self, ap_object_id: str) -> FederatedEvent | None:
        return await self._run(self._get_federated_event_by_ap_object, ap_object_id)

    @staticmethod
    def _row_to_federated_event(row: sqlite3.Row) -> FederatedEvent:
        return FederatedEvent(
            event_id=row["event_id"],
            room_id=row["room_id"],
            ap_object_id=row["ap_object_id"],
            author_actor_id=row["author_actor_id"],
            thread_root_event_id=row["thread_root_event_id"],
            reposted_object_id=row["reposted_object_id"],
            reposted_author_actor_id=row["reposted_author_actor_id"],
        )

    # -- AppService transaction idempotency --------------------------------

    def _has_processed_transaction(self, txn_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return row is not None

    async def has_processed_transaction(self, txn_id: str) -> bool:
        return await self._run(self._has_processed_transaction, txn_id)

    def _mark_transaction_processed(self, txn_id: str) -> None:
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_transactions (txn_id, processed_at) VALUES (?, ?)",
            (txn_id, now),
        )
        # Opportunistic cleanup -- bounds table growth without a separate sweep task.
        self._conn.execute(
            "DELETE FROM processed_transactions WHERE processed_at < ?",
            (now - _TRANSACTION_RETENTION_SECONDS,),
        )
        self._conn.commit()

    async def mark_transaction_processed(self, txn_id: str) -> None:
        await self._run(self._mark_transaction_processed, txn_id)

    # -- published-media allowlist ------------------------------------------

    def _is_media_published(self, mxc_uri: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM published_media WHERE mxc_uri = ?", (mxc_uri,)
        ).fetchone()
        return row is not None

    async def is_media_published(self, mxc_uri: str) -> bool:
        return await self._run(self._is_media_published, mxc_uri)

    def _mark_media_published(self, mxc_uri: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO published_media (mxc_uri, published_at) VALUES (?, ?)",
            (mxc_uri, time.time()),
        )
        self._conn.commit()

    async def mark_media_published(self, mxc_uri: str) -> None:
        await self._run(self._mark_media_published, mxc_uri)

    # -- reactions -----------------------------------------------------------

    def _record_reaction(self, record: ReactionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO reactions
                (activity_id, room_id, event_id, target_ap_object_id,
                 reactor_ghost_mxid, reactor_matrix_user_id, secondary_event_id, custom_emoji_mxc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                room_id=excluded.room_id,
                event_id=excluded.event_id,
                target_ap_object_id=excluded.target_ap_object_id,
                reactor_ghost_mxid=excluded.reactor_ghost_mxid,
                reactor_matrix_user_id=excluded.reactor_matrix_user_id,
                secondary_event_id=excluded.secondary_event_id,
                custom_emoji_mxc=excluded.custom_emoji_mxc
            """,
            (
                record.activity_id, record.room_id, record.event_id, record.target_ap_object_id,
                record.reactor_ghost_mxid, record.reactor_matrix_user_id, record.secondary_event_id,
                record.custom_emoji_mxc,
            ),
        )
        self._conn.commit()

    async def record_reaction(self, record: ReactionRecord) -> None:
        await self._run(self._record_reaction, record)

    def _get_reaction_by_activity_id(self, activity_id: str) -> ReactionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM reactions WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        return self._row_to_reaction(row) if row else None

    async def get_reaction_by_activity_id(self, activity_id: str) -> ReactionRecord | None:
        return await self._run(self._get_reaction_by_activity_id, activity_id)

    def _get_reaction_by_matrix_event(self, event_id: str) -> ReactionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM reactions WHERE event_id = ? OR secondary_event_id = ?", (event_id, event_id)
        ).fetchone()
        return self._row_to_reaction(row) if row else None

    async def get_reaction_by_matrix_event(self, event_id: str) -> ReactionRecord | None:
        return await self._run(self._get_reaction_by_matrix_event, event_id)

    def _remove_reaction(self, activity_id: str) -> None:
        self._conn.execute("DELETE FROM reactions WHERE activity_id = ?", (activity_id,))
        self._conn.commit()

    async def remove_reaction(self, activity_id: str) -> None:
        await self._run(self._remove_reaction, activity_id)

    # -- poll votes ------------------------------------------------------

    def _record_poll_vote(self, record: PollVoteRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO poll_votes
                (vote_activity_id, question_ap_object_id, room_id,
                 voter_actor_id, matrix_user_id, matrix_event_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(vote_activity_id) DO UPDATE SET
                question_ap_object_id=excluded.question_ap_object_id,
                room_id=excluded.room_id,
                voter_actor_id=excluded.voter_actor_id,
                matrix_user_id=excluded.matrix_user_id,
                matrix_event_id=excluded.matrix_event_id
            """,
            (
                record.vote_activity_id, record.question_ap_object_id, record.room_id,
                record.voter_actor_id, record.matrix_user_id, record.matrix_event_id,
            ),
        )
        self._conn.commit()

    async def record_poll_vote(self, record: PollVoteRecord) -> None:
        await self._run(self._record_poll_vote, record)

    def _get_poll_vote_by_activity_id(self, vote_activity_id: str) -> PollVoteRecord | None:
        row = self._conn.execute(
            "SELECT * FROM poll_votes WHERE vote_activity_id = ?", (vote_activity_id,)
        ).fetchone()
        return self._row_to_poll_vote(row) if row else None

    async def get_poll_vote_by_activity_id(self, vote_activity_id: str) -> PollVoteRecord | None:
        return await self._run(self._get_poll_vote_by_activity_id, vote_activity_id)

    def _get_poll_vote_by_matrix_user(
        self, question_ap_object_id: str, matrix_user_id: str
    ) -> PollVoteRecord | None:
        row = self._conn.execute(
            "SELECT * FROM poll_votes WHERE question_ap_object_id = ? AND matrix_user_id = ?",
            (question_ap_object_id, matrix_user_id),
        ).fetchone()
        return self._row_to_poll_vote(row) if row else None

    async def get_poll_vote_by_matrix_user(
        self, question_ap_object_id: str, matrix_user_id: str
    ) -> PollVoteRecord | None:
        return await self._run(self._get_poll_vote_by_matrix_user, question_ap_object_id, matrix_user_id)

    def _get_custom_emoji_mxc(self, source_url: str) -> str | None:
        row = self._conn.execute(
            "SELECT mxc_url FROM custom_emoji WHERE source_url = ?", (source_url,)
        ).fetchone()
        return row["mxc_url"] if row else None

    async def get_custom_emoji_mxc(self, source_url: str) -> str | None:
        return await self._run(self._get_custom_emoji_mxc, source_url)

    def _record_custom_emoji_mxc(self, source_url: str, mxc_url: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO custom_emoji (source_url, mxc_url) VALUES (?, ?)", (source_url, mxc_url)
        )
        self._conn.commit()

    async def record_custom_emoji_mxc(self, source_url: str, mxc_url: str) -> None:
        await self._run(self._record_custom_emoji_mxc, source_url, mxc_url)

    def _get_custom_emoji_by_reaction_event_ids(self, event_ids: list[str]) -> dict[str, str]:
        if not event_ids:
            return {}
        placeholders = ",".join("?" * len(event_ids))
        rows = self._conn.execute(
            f"SELECT event_id, custom_emoji_mxc FROM reactions "
            f"WHERE event_id IN ({placeholders}) AND custom_emoji_mxc IS NOT NULL",
            event_ids,
        ).fetchall()
        return {row["event_id"]: row["custom_emoji_mxc"] for row in rows}

    async def get_custom_emoji_by_reaction_event_ids(self, event_ids: list[str]) -> dict[str, str]:
        return await self._run(self._get_custom_emoji_by_reaction_event_ids, event_ids)

    def _record_resolved_emoji(self, subject_id: str, shortcode: str, mxc_url: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO resolved_emoji (subject_id, shortcode, mxc_url) VALUES (?, ?, ?)",
            (subject_id, shortcode, mxc_url),
        )
        self._conn.commit()

    async def record_resolved_emoji(self, subject_id: str, shortcode: str, mxc_url: str) -> None:
        await self._run(self._record_resolved_emoji, subject_id, shortcode, mxc_url)

    def _get_resolved_emoji(self, subject_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT shortcode, mxc_url FROM resolved_emoji WHERE subject_id = ?", (subject_id,)
        ).fetchall()
        return {row["shortcode"]: row["mxc_url"] for row in rows}

    async def get_resolved_emoji(self, subject_id: str) -> dict[str, str]:
        return await self._run(self._get_resolved_emoji, subject_id)

    @staticmethod
    def _row_to_reaction(row: sqlite3.Row) -> ReactionRecord:
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

    @staticmethod
    def _row_to_poll_vote(row: sqlite3.Row) -> PollVoteRecord:
        return PollVoteRecord(
            vote_activity_id=row["vote_activity_id"],
            question_ap_object_id=row["question_ap_object_id"],
            room_id=row["room_id"],
            voter_actor_id=row["voter_actor_id"],
            matrix_user_id=row["matrix_user_id"],
            matrix_event_id=row["matrix_event_id"],
        )

    # -- ghost profile sync cache -------------------------------------------

    @staticmethod
    def _row_to_ghost_profile(row: sqlite3.Row) -> GhostProfile:
        return GhostProfile(
            actor_id=row["actor_id"],
            display_name=row["display_name"],
            icon_url=row["icon_url"],
            mxid=row["mxid"],
            handle=row["handle"],
        )

    def _get_ghost_profile(self, actor_id: str) -> GhostProfile | None:
        row = self._conn.execute(
            "SELECT * FROM ghost_profiles WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        return self._row_to_ghost_profile(row) if row else None

    async def get_ghost_profile(self, actor_id: str) -> GhostProfile | None:
        return await self._run(self._get_ghost_profile, actor_id)

    def _get_ghost_profile_by_mxid(self, mxid: str) -> GhostProfile | None:
        row = self._conn.execute(
            "SELECT * FROM ghost_profiles WHERE mxid = ?", (mxid,)
        ).fetchone()
        return self._row_to_ghost_profile(row) if row else None

    async def get_ghost_profile_by_mxid(self, mxid: str) -> GhostProfile | None:
        return await self._run(self._get_ghost_profile_by_mxid, mxid)

    def _record_ghost_profile(self, profile: GhostProfile) -> None:
        self._conn.execute(
            """
            INSERT INTO ghost_profiles (actor_id, display_name, icon_url, mxid, handle)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(actor_id) DO UPDATE SET
                display_name = excluded.display_name,
                icon_url = excluded.icon_url,
                mxid = excluded.mxid,
                handle = excluded.handle
            """,
            (profile.actor_id, profile.display_name, profile.icon_url, profile.mxid, profile.handle),
        )
        self._conn.commit()

    async def record_ghost_profile(self, profile: GhostProfile) -> None:
        await self._run(self._record_ghost_profile, profile)

    # -- user spaces -------------------------------------------------------

    def _get_user_space(self, matrix_user_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT space_room_id FROM user_spaces WHERE matrix_user_id = ?", (matrix_user_id,)
        ).fetchone()
        return row["space_room_id"] if row else None

    async def get_user_space(self, matrix_user_id: str) -> str | None:
        return await self._run(self._get_user_space, matrix_user_id)

    def _register_user_space(self, matrix_user_id: str, space_room_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO user_spaces (matrix_user_id, space_room_id) VALUES (?, ?)
            ON CONFLICT(matrix_user_id) DO UPDATE SET space_room_id = excluded.space_room_id
            """,
            (matrix_user_id, space_room_id),
        )
        self._conn.commit()

    async def register_user_space(self, matrix_user_id: str, space_room_id: str) -> None:
        await self._run(self._register_user_space, matrix_user_id, space_room_id)

    # -- Shoot guild/channel bridging ----------------------------------------

    def _record_pending_guild_follow(self, record: PendingGuildFollow) -> None:
        self._conn.execute(
            """
            INSERT INTO pending_guild_follows
                (follow_id, guild_actor_id, username, matrix_user_id, invite_code, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(follow_id) DO UPDATE SET
                guild_actor_id=excluded.guild_actor_id,
                username=excluded.username,
                matrix_user_id=excluded.matrix_user_id,
                invite_code=excluded.invite_code,
                created_at=excluded.created_at
            """,
            (
                record.follow_id, record.guild_actor_id, record.username,
                record.matrix_user_id, record.invite_code, record.created_at,
            ),
        )
        self._conn.commit()

    async def record_pending_guild_follow(self, record: PendingGuildFollow) -> None:
        await self._run(self._record_pending_guild_follow, record)

    def _get_pending_guild_follow(self, follow_id: str) -> PendingGuildFollow | None:
        row = self._conn.execute(
            "SELECT * FROM pending_guild_follows WHERE follow_id = ?", (follow_id,)
        ).fetchone()
        if row is None:
            return None
        return PendingGuildFollow(
            follow_id=row["follow_id"], guild_actor_id=row["guild_actor_id"], username=row["username"],
            matrix_user_id=row["matrix_user_id"], invite_code=row["invite_code"], created_at=row["created_at"],
        )

    async def get_pending_guild_follow(self, follow_id: str) -> PendingGuildFollow | None:
        return await self._run(self._get_pending_guild_follow, follow_id)

    def _remove_pending_guild_follow(self, follow_id: str) -> None:
        self._conn.execute("DELETE FROM pending_guild_follows WHERE follow_id = ?", (follow_id,))
        self._conn.commit()

    async def remove_pending_guild_follow(self, follow_id: str) -> None:
        await self._run(self._remove_pending_guild_follow, follow_id)

    def _record_guild_membership(self, username: str, guild_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO guild_memberships (username, guild_actor_id) VALUES (?, ?)",
            (username, guild_actor_id),
        )
        self._conn.commit()

    async def record_guild_membership(self, username: str, guild_actor_id: str) -> None:
        await self._run(self._record_guild_membership, username, guild_actor_id)

    def _get_guild_membership(self, username: str, guild_actor_id: str) -> GuildMembership | None:
        row = self._conn.execute(
            "SELECT * FROM guild_memberships WHERE username = ? AND guild_actor_id = ?",
            (username, guild_actor_id),
        ).fetchone()
        if row is None:
            return None
        return GuildMembership(username=row["username"], guild_actor_id=row["guild_actor_id"])

    async def get_guild_membership(self, username: str, guild_actor_id: str) -> GuildMembership | None:
        return await self._run(self._get_guild_membership, username, guild_actor_id)

    def _is_guild_member(self, guild_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM guild_memberships WHERE guild_actor_id = ? LIMIT 1", (guild_actor_id,)
        ).fetchone()
        return row is not None

    async def is_guild_member(self, guild_actor_id: str) -> bool:
        return await self._run(self._is_guild_member, guild_actor_id)

    def _list_guild_members(self, guild_actor_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT username FROM guild_memberships WHERE guild_actor_id = ?", (guild_actor_id,)
        ).fetchall()
        return [row["username"] for row in rows]

    async def list_guild_members(self, guild_actor_id: str) -> list[str]:
        return await self._run(self._list_guild_members, guild_actor_id)

    def _remove_guild_membership(self, username: str, guild_actor_id: str) -> None:
        self._conn.execute(
            "DELETE FROM guild_memberships WHERE username = ? AND guild_actor_id = ?", (username, guild_actor_id),
        )
        self._conn.commit()

    async def remove_guild_membership(self, username: str, guild_actor_id: str) -> None:
        await self._run(self._remove_guild_membership, username, guild_actor_id)

    def _set_guild_space(self, guild_actor_id: str, space_room_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO guild_spaces (guild_actor_id, space_room_id) VALUES (?, ?)
            ON CONFLICT(guild_actor_id) DO UPDATE SET space_room_id = excluded.space_room_id
            """,
            (guild_actor_id, space_room_id),
        )
        self._conn.commit()

    async def set_guild_space(self, guild_actor_id: str, space_room_id: str) -> None:
        await self._run(self._set_guild_space, guild_actor_id, space_room_id)

    def _get_guild_space(self, guild_actor_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT space_room_id FROM guild_spaces WHERE guild_actor_id = ?", (guild_actor_id,)
        ).fetchone()
        return row["space_room_id"] if row else None

    async def get_guild_space(self, guild_actor_id: str) -> str | None:
        return await self._run(self._get_guild_space, guild_actor_id)

    def _record_guild_channels(self, guild_actor_id: str, channels: list[tuple[str, str]]) -> None:
        for channel_actor_id, name in channels:
            self._conn.execute(
                """
                INSERT INTO guild_channels (channel_actor_id, guild_actor_id, name) VALUES (?, ?, ?)
                ON CONFLICT(channel_actor_id) DO UPDATE SET guild_actor_id=excluded.guild_actor_id, name=excluded.name
                """,
                (channel_actor_id, guild_actor_id, name),
            )
        self._conn.commit()

    async def record_guild_channels(self, guild_actor_id: str, channels: list[tuple[str, str]]) -> None:
        await self._run(self._record_guild_channels, guild_actor_id, channels)

    def _get_guild_channel(self, channel_actor_id: str) -> GuildChannel | None:
        row = self._conn.execute(
            "SELECT * FROM guild_channels WHERE channel_actor_id = ?", (channel_actor_id,)
        ).fetchone()
        if row is None:
            return None
        return GuildChannel(
            channel_actor_id=row["channel_actor_id"], guild_actor_id=row["guild_actor_id"], name=row["name"]
        )

    async def get_guild_channel(self, channel_actor_id: str) -> GuildChannel | None:
        return await self._run(self._get_guild_channel, channel_actor_id)

    def _get_channel_room(self, channel_actor_id: str) -> ChannelRoom | None:
        row = self._conn.execute(
            "SELECT * FROM channel_rooms WHERE channel_actor_id = ?", (channel_actor_id,)
        ).fetchone()
        return self._row_to_channel_room(row) if row else None

    async def get_channel_room(self, channel_actor_id: str) -> ChannelRoom | None:
        return await self._run(self._get_channel_room, channel_actor_id)

    def _get_channel_room_by_room_id(self, room_id: str) -> ChannelRoom | None:
        row = self._conn.execute("SELECT * FROM channel_rooms WHERE room_id = ?", (room_id,)).fetchone()
        return self._row_to_channel_room(row) if row else None

    async def get_channel_room_by_room_id(self, room_id: str) -> ChannelRoom | None:
        return await self._run(self._get_channel_room_by_room_id, room_id)

    def _register_channel_room(self, record: ChannelRoom) -> None:
        self._conn.execute(
            """
            INSERT INTO channel_rooms (channel_actor_id, room_id, guild_actor_id, display_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_actor_id) DO UPDATE SET
                room_id=excluded.room_id, guild_actor_id=excluded.guild_actor_id, display_name=excluded.display_name
            """,
            (record.channel_actor_id, record.room_id, record.guild_actor_id, record.display_name),
        )
        self._conn.commit()

    async def register_channel_room(self, record: ChannelRoom) -> None:
        await self._run(self._register_channel_room, record)

    @staticmethod
    def _row_to_channel_room(row: sqlite3.Row) -> ChannelRoom:
        return ChannelRoom(
            channel_actor_id=row["channel_actor_id"], room_id=row["room_id"],
            guild_actor_id=row["guild_actor_id"], display_name=row["display_name"],
        )

    def _is_channel_member_known(self, room_id: str, member_actor_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM channel_room_members WHERE room_id = ? AND member_actor_id = ?",
            (room_id, member_actor_id),
        ).fetchone()
        return row is not None

    async def is_channel_member_known(self, room_id: str, member_actor_id: str) -> bool:
        return await self._run(self._is_channel_member_known, room_id, member_actor_id)

    def _record_channel_member(self, room_id: str, member_actor_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO channel_room_members (room_id, member_actor_id) VALUES (?, ?)",
            (room_id, member_actor_id),
        )
        self._conn.commit()

    async def record_channel_member(self, room_id: str, member_actor_id: str) -> None:
        await self._run(self._record_channel_member, room_id, member_actor_id)

    # -- bot DM rooms --------------------------------------------------------

    def _get_bot_dm_room(self, matrix_user_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT room_id FROM bot_dm_rooms WHERE matrix_user_id = ?", (matrix_user_id,)
        ).fetchone()
        return row["room_id"] if row else None

    async def get_bot_dm_room(self, matrix_user_id: str) -> str | None:
        return await self._run(self._get_bot_dm_room, matrix_user_id)

    def _register_bot_dm_room(self, matrix_user_id: str, room_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO bot_dm_rooms (matrix_user_id, room_id) VALUES (?, ?)
            ON CONFLICT(matrix_user_id) DO UPDATE SET room_id = excluded.room_id
            """,
            (matrix_user_id, room_id),
        )
        self._conn.commit()

    async def register_bot_dm_room(self, matrix_user_id: str, room_id: str) -> None:
        await self._run(self._register_bot_dm_room, matrix_user_id, room_id)

    def _get_bot_dm_room_owner(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT matrix_user_id FROM bot_dm_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["matrix_user_id"] if row else None

    async def get_bot_dm_room_owner(self, room_id: str) -> str | None:
        return await self._run(self._get_bot_dm_room_owner, room_id)

    # -- ghost DM rooms -------------------------------------------------------

    def _get_ghost_dm_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT room_id FROM ghost_dm_rooms WHERE actor_id = ? AND matrix_user_id = ?",
            (actor_id, matrix_user_id),
        ).fetchone()
        return row["room_id"] if row else None

    async def get_ghost_dm_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        return await self._run(self._get_ghost_dm_room, actor_id, matrix_user_id)

    def _register_ghost_dm_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO ghost_dm_rooms (actor_id, matrix_user_id, room_id) VALUES (?, ?, ?)
            ON CONFLICT(actor_id, matrix_user_id) DO UPDATE SET room_id = excluded.room_id
            """,
            (actor_id, matrix_user_id, room_id),
        )
        # Permanent history row -- see get_ghost_dm_room_history's
        # docstring. Clear the old "current" flag first so the partial
        # unique index never sees two current rows at once.
        self._conn.execute(
            """
            UPDATE ghost_dm_room_history SET is_current = 0
            WHERE actor_id = ? AND matrix_user_id = ? AND is_current = 1
            """,
            (actor_id, matrix_user_id),
        )
        self._conn.execute(
            """
            INSERT INTO ghost_dm_room_history (room_id, actor_id, matrix_user_id, is_current)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(room_id) DO UPDATE SET is_current = 1
            """,
            (room_id, actor_id, matrix_user_id),
        )
        self._conn.commit()

    async def register_ghost_dm_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        await self._run(self._register_ghost_dm_room, actor_id, matrix_user_id, room_id)

    def _get_ghost_dm_room_history(self, room_id: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT actor_id, matrix_user_id FROM ghost_dm_room_history WHERE room_id = ?", (room_id,)
        ).fetchone()
        return (row["actor_id"], row["matrix_user_id"]) if row else None

    async def get_ghost_dm_room_history(self, room_id: str) -> tuple[str, str] | None:
        return await self._run(self._get_ghost_dm_room_history, room_id)

    def _is_ghost_dm_room(self, room_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ghost_dm_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row is not None

    async def is_ghost_dm_room(self, room_id: str) -> bool:
        return await self._run(self._is_ghost_dm_room, room_id)

    def _get_ghost_dm_room_actor_id(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT actor_id FROM ghost_dm_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["actor_id"] if row else None

    async def get_ghost_dm_room_actor_id(self, room_id: str) -> str | None:
        return await self._run(self._get_ghost_dm_room_actor_id, room_id)

    def _get_ghost_dm_room_matrix_user_id(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT matrix_user_id FROM ghost_dm_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["matrix_user_id"] if row else None

    async def get_ghost_dm_room_matrix_user_id(self, room_id: str) -> str | None:
        return await self._run(self._get_ghost_dm_room_matrix_user_id, room_id)

    def _get_ghost_dm_room_ids_for_actor(self, actor_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT room_id FROM ghost_dm_rooms WHERE actor_id = ?", (actor_id,)
        ).fetchall()
        return [row["room_id"] for row in rows]

    async def get_ghost_dm_room_ids_for_actor(self, actor_id: str) -> list[str]:
        return await self._run(self._get_ghost_dm_room_ids_for_actor, actor_id)

    def _list_ghost_dm_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT room_id FROM ghost_dm_rooms WHERE matrix_user_id = ?", (matrix_user_id,)
        ).fetchall()
        return [row["room_id"] for row in rows]

    async def list_ghost_dm_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        return await self._run(self._list_ghost_dm_rooms_for_user, matrix_user_id)

    # -- ghost chat rooms (ActivityPub ChatMessage, distinct from DM) -------

    def _get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT room_id FROM ghost_chat_rooms WHERE actor_id = ? AND matrix_user_id = ?",
            (actor_id, matrix_user_id),
        ).fetchone()
        return row["room_id"] if row else None

    async def get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        return await self._run(self._get_ghost_chat_room, actor_id, matrix_user_id)

    def _register_ghost_chat_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO ghost_chat_rooms (actor_id, matrix_user_id, room_id) VALUES (?, ?, ?)
            ON CONFLICT(actor_id, matrix_user_id) DO UPDATE SET room_id = excluded.room_id
            """,
            (actor_id, matrix_user_id, room_id),
        )
        # Permanent history row -- see get_ghost_chat_room_history's
        # docstring. Clear the old "current" flag first so the partial
        # unique index never sees two current rows at once.
        self._conn.execute(
            """
            UPDATE ghost_chat_room_history SET is_current = 0
            WHERE actor_id = ? AND matrix_user_id = ? AND is_current = 1
            """,
            (actor_id, matrix_user_id),
        )
        self._conn.execute(
            """
            INSERT INTO ghost_chat_room_history (room_id, actor_id, matrix_user_id, is_current)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(room_id) DO UPDATE SET is_current = 1
            """,
            (room_id, actor_id, matrix_user_id),
        )
        self._conn.commit()

    async def register_ghost_chat_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        await self._run(self._register_ghost_chat_room, actor_id, matrix_user_id, room_id)

    def _get_ghost_chat_room_history(self, room_id: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT actor_id, matrix_user_id FROM ghost_chat_room_history WHERE room_id = ?", (room_id,)
        ).fetchone()
        return (row["actor_id"], row["matrix_user_id"]) if row else None

    async def get_ghost_chat_room_history(self, room_id: str) -> tuple[str, str] | None:
        return await self._run(self._get_ghost_chat_room_history, room_id)

    def _is_ghost_chat_room(self, room_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ghost_chat_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row is not None

    async def is_ghost_chat_room(self, room_id: str) -> bool:
        return await self._run(self._is_ghost_chat_room, room_id)

    def _get_ghost_chat_room_actor_id(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT actor_id FROM ghost_chat_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["actor_id"] if row else None

    async def get_ghost_chat_room_actor_id(self, room_id: str) -> str | None:
        return await self._run(self._get_ghost_chat_room_actor_id, room_id)

    def _get_ghost_chat_room_matrix_user_id(self, room_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT matrix_user_id FROM ghost_chat_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        return row["matrix_user_id"] if row else None

    async def get_ghost_chat_room_matrix_user_id(self, room_id: str) -> str | None:
        return await self._run(self._get_ghost_chat_room_matrix_user_id, room_id)

    def _get_ghost_chat_room_ids_for_actor(self, actor_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT room_id FROM ghost_chat_rooms WHERE actor_id = ?", (actor_id,)
        ).fetchall()
        return [row["room_id"] for row in rows]

    async def get_ghost_chat_room_ids_for_actor(self, actor_id: str) -> list[str]:
        return await self._run(self._get_ghost_chat_room_ids_for_actor, actor_id)

    def _list_ghost_chat_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT room_id FROM ghost_chat_rooms WHERE matrix_user_id = ?", (matrix_user_id,)
        ).fetchall()
        return [row["room_id"] for row in rows]

    async def list_ghost_chat_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        return await self._run(self._list_ghost_chat_rooms_for_user, matrix_user_id)
