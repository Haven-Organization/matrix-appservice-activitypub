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

import json
import logging
import time
from datetime import datetime, timezone

import asyncpg

from bridge.repository import (
    ActorRecord,
    ChannelRoom,
    FederatedEvent,
    GhostProfile,
    GroupRecord,
    GroupRoom,
    GuildChannel,
    GuildMembership,
    InstanceLoginRecord,
    LemmyCommunityRecord,
    LemmyCommunityRoom,
    PeerTubeChannelRecord,
    PendingGuildFollow,
    PollVoteRecord,
    ReactionRecord,
    RemoteActorRoom,
    ThirdPartyAllowRecord,
)

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = [
    # matrix_user_id is deliberately NOT UNIQUE here -- a fresh database
    # creates this table without the constraint from the start, since a
    # Matrix user may own several identities once
    # bridge.custom_identities_enabled is on. See the
    # "DROP CONSTRAINT IF EXISTS local_actors_matrix_user_id_key" migration
    # statement below for dropping it from an EXISTING database that still
    # has it (same technique already used for
    # federated_events_ap_object_id_key just above), and
    # idx_local_actors_matrix_user_id for the lookup index that replaces
    # what the UNIQUE constraint used to provide for free.
    """
    CREATE TABLE IF NOT EXISTS local_actors (
        username TEXT PRIMARY KEY,
        matrix_user_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        hide_followers BOOLEAN NOT NULL DEFAULT FALSE,
        hide_following BOOLEAN NOT NULL DEFAULT FALSE,
        is_third_party BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_local_actors_matrix_user_id ON local_actors (matrix_user_id)",
    # Sparse: a row only exists for a Matrix user who currently has >1
    # identity or has ever explicitly switched via ";id" -- see
    # ActorRepository.get_active_local_actor's docstring for the fallback
    # when no row exists.
    """
    CREATE TABLE IF NOT EXISTS active_identity (
        matrix_user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL
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
    # One bridge user's login to a remote instance (widget Browse feature
    # -- see InstanceLoginRecord's own docstring). Timestamps stored as
    # plain TEXT (ISO 8601, written by the application) rather than
    # TIMESTAMPTZ, matching local_actors.created_at's existing convention
    # -- the value always originates as a Python isoformat() string, so
    # there's no round-trip conversion to get right either way.
    """
    CREATE TABLE IF NOT EXISTS instance_logins (
        matrix_user_id TEXT NOT NULL,
        instance_domain TEXT NOT NULL,
        auth_method TEXT NOT NULL,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        token_type TEXT NOT NULL DEFAULT 'Bearer',
        scope TEXT NOT NULL DEFAULT '',
        account_handle TEXT NOT NULL DEFAULT '',
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (matrix_user_id, instance_domain)
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
        reposted_object_id TEXT,
        reposted_author_actor_id TEXT,
        is_primary_event BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_transactions (
        txn_id TEXT PRIMARY KEY,
        processed_at DOUBLE PRECISION NOT NULL
    )
    """,
    # Per-Matrix-event idempotency -- see has_processed_matrix_event's own
    # docstring for why this exists ALONGSIDE processed_transactions above,
    # not as a mere duplicate of it: Synapse's own AppService txn_id
    # counter (on a Synapse instance backed by sqlite) resets to 1 after a
    # Synapse restart, so a brand new transaction can be handed a txn_id
    # that collides with an ancient one already recorded above -- gating
    # on txn_id alone would then silently discard every event in it.
    # event_id is a real content-derived identifier Synapse never reuses,
    # restart or not.
    """
    CREATE TABLE IF NOT EXISTS processed_matrix_events (
        event_id TEXT PRIMARY KEY,
        processed_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_media (
        mxc_uri TEXT PRIMARY KEY,
        published_at DOUBLE PRECISION NOT NULL
    )
    """,
    # Append-only log of automatic outage-recovery sweeps -- see
    # bridge.outage_recovery. detected_at (this bridge's own wall-clock
    # time) is what the recovery loop's cooldown checks against, so a
    # detected outage cluster never triggers a second sweep within the
    # cooldown window even if the destinations bookkeeping this reads
    # from wobbles slightly between checks.
    """
    CREATE TABLE IF NOT EXISTS outage_recoveries (
        id BIGSERIAL PRIMARY KEY,
        detected_at DOUBLE PRECISION NOT NULL,
        outage_started_at BIGINT NOT NULL,
        destinations_affected INTEGER NOT NULL
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
    # Idempotency bookkeeping for poll votes, both directions -- see
    # PollVoteRecord's own docstring for why this table exists and what it
    # deliberately does NOT store (which option was chosen).
    """
    CREATE TABLE IF NOT EXISTS poll_votes (
        vote_activity_id TEXT PRIMARY KEY,
        question_ap_object_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        voter_actor_id TEXT,
        matrix_user_id TEXT,
        matrix_event_id TEXT UNIQUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_poll_votes_question_user ON poll_votes (question_ap_object_id, matrix_user_id)",
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
    # local_username is the identity that was Active (or room-bound) when
    # this DM room was first started -- stamped once at creation, never
    # changed afterward, even by a later ";id" Active switch (see
    # ActorRepository.register_ghost_dm_room's docstring). Nullable: a row
    # predating this column (backfilled during migration below) or one
    # somehow never stamped falls back to None, not an error.
    """
    CREATE TABLE IF NOT EXISTS ghost_dm_rooms (
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        local_username TEXT,
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
        local_username TEXT,
        PRIMARY KEY (actor_id, matrix_user_id)
    )
    """,
    # Dedup cache for custom-emoji reaction images (see
    # ActorRepository.get_custom_emoji_mxc) -- source_url is the remote
    # emoji image's own URL (unique per emoji per source instance), mxc_url
    # is where it was uploaded to Synapse's media repo. Checked before ever
    # fetching one, so the same emoji is only ever downloaded/uploaded once
    # no matter how many reactions use it. name is the emoji's own AP
    # shortcode, kept so get_custom_emoji_source can recover the original
    # identity of an already-downloaded emoji from mxc_url alone.
    """
    CREATE TABLE IF NOT EXISTS custom_emoji (
        source_url TEXT PRIMARY KEY,
        mxc_url TEXT NOT NULL,
        name TEXT
    )
    """,
    # Per-ActivityPub-identity preference overrides (see
    # bridge.identity_settings). username is either an ActorRecord's or a
    # PeerTubeChannelRecord's; the two share one global namespace (see
    # _handle_create_channel). Sparse: a row only exists when its value
    # differs from that key's code default: the absence of a row IS the
    # default, not an unset state to distinguish.
    """
    CREATE TABLE IF NOT EXISTS identity_settings (
        username TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (username, key)
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
        is_current BOOLEAN NOT NULL DEFAULT FALSE,
        local_username TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ghost_chat_room_history (
        room_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT FALSE,
        local_username TEXT
    )
    """,
    # Shoot Guild/Channel bridging (see bridge.channel_bridge) -- an
    # outstanding FEP-bebd guild-join Follow, between sending it and the
    # guild's Accept/Reject arriving back over the inbox. See
    # ActorRepository.record_pending_guild_follow's docstring.
    """
    CREATE TABLE IF NOT EXISTS pending_guild_follows (
        follow_id TEXT PRIMARY KEY,
        guild_actor_id TEXT NOT NULL,
        username TEXT NOT NULL,
        matrix_user_id TEXT NOT NULL,
        invite_code TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_memberships (
        username TEXT NOT NULL,
        guild_actor_id TEXT NOT NULL,
        PRIMARY KEY (username, guild_actor_id)
    )
    """,
    # Separate from guild_memberships -- the Space is one-per-guild, shared
    # by every local member, not one-per-membership-row. Same shape as
    # user_spaces, just keyed by guild instead of by Matrix user.
    """
    CREATE TABLE IF NOT EXISTS guild_spaces (
        guild_actor_id TEXT PRIMARY KEY,
        space_room_id TEXT NOT NULL
    )
    """,
    # A joined guild's own flat `channels` collection, cached once right
    # after the join Accept -- see ActorRepository.get_guild_channel's
    # docstring for why this makes inbound Announce disambiguation a table
    # lookup instead of a live fetch per message.
    """
    CREATE TABLE IF NOT EXISTS guild_channels (
        channel_actor_id TEXT PRIMARY KEY,
        guild_actor_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_guild_channels_guild ON guild_channels (guild_actor_id)",
    # One Matrix room per Shoot Channel -- deliberately no single
    # ghost_user_id column the way remote_actor_rooms has one, since a
    # channel's messages come from many different guild-member ghosts,
    # resolved per-message.
    """
    CREATE TABLE IF NOT EXISTS channel_rooms (
        channel_actor_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL UNIQUE,
        guild_actor_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT ''
    )
    """,
    # Which member ghosts have already been seen/invited in a channel room
    # -- pure optimization (resolve_and_invite_ghost is already idempotent
    # on its own), not a correctness dependency.
    """
    CREATE TABLE IF NOT EXISTS channel_room_members (
        room_id TEXT NOT NULL,
        member_actor_id TEXT NOT NULL,
        PRIMARY KEY (room_id, member_actor_id)
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
    "ALTER TABLE local_actors ADD COLUMN IF NOT EXISTS created_at TEXT NOT NULL DEFAULT ''",
    # Multi-identity support: drops the OLD UNIQUE constraint an existing
    # database may still have on matrix_user_id (Postgres's default name
    # for a single-column inline UNIQUE is "{table}_{column}_key" -- same
    # technique as federated_events_ap_object_id_key's drop above). IF
    # EXISTS makes this a no-op both against a database that's already
    # been through this migration and against a brand new one (whose
    # CREATE TABLE above never declares the constraint in the first place).
    "ALTER TABLE local_actors DROP CONSTRAINT IF EXISTS local_actors_matrix_user_id_key",
    # Backfill created_at for any row that doesn't have one yet. The
    # "0000-00-00-legacy-" prefix sorts BEFORE any real ISO-8601 created_at
    # (which always starts with a 4-digit year), so every legacy row still
    # counts as "oldest" per ActorRecord.created_at's documented ordering
    # contract; ctid (this row's current physical location) is the
    # least-bad available proxy for relative insertion order on a database
    # old enough to have no real created_at at all -- a fresh database
    # never has empty values here since _register_local_actor always
    # stamps a real one.
    "UPDATE local_actors SET created_at = '0000-00-00-legacy-' || ctid::text WHERE created_at = ''",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS icon_url TEXT",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS banner_url TEXT",
    "ALTER TABLE remote_actor_rooms ADD COLUMN IF NOT EXISTS pending_backfill BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS thread_root_event_id TEXT",
    # Renamed 2026-07-11 (boost -> repost terminology, project-wide) -- a
    # DO block (not a plain ADD COLUMN IF NOT EXISTS like the others here)
    # since this needs to actually preserve existing data via RENAME
    # COLUMN, not just ensure the column exists; guarded so it's a no-op
    # both on a fresh database (created with reposted_object_id directly,
    # never had the old name at all) and on one that's already been
    # through this migration once.
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'federated_events' AND column_name = 'boosted_object_id'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'federated_events' AND column_name = 'reposted_object_id'
        ) THEN
            ALTER TABLE federated_events RENAME COLUMN boosted_object_id TO reposted_object_id;
            ALTER TABLE federated_events RENAME COLUMN boosted_author_actor_id TO reposted_author_actor_id;
        END IF;
    END $$;
    """,
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS reposted_object_id TEXT",
    "ALTER TABLE federated_events ADD COLUMN IF NOT EXISTS reposted_author_actor_id TEXT",
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
    "ALTER TABLE custom_emoji ADD COLUMN IF NOT EXISTS name TEXT",
    "CREATE INDEX IF NOT EXISTS idx_custom_emoji_mxc_url ON custom_emoji (mxc_url)",
    "CREATE INDEX IF NOT EXISTS idx_custom_emoji_name ON custom_emoji (name)",
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
    # Same reasoning -- mark_matrix_event_processed's own opportunistic
    # cleanup runs on every single Matrix event this bridge is ever handed.
    "CREATE INDEX IF NOT EXISTS idx_processed_matrix_events_processed_at "
    "ON processed_matrix_events (processed_at)",
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
    "ALTER TABLE ghost_dm_rooms ADD COLUMN IF NOT EXISTS local_username TEXT",
    "ALTER TABLE ghost_chat_rooms ADD COLUMN IF NOT EXISTS local_username TEXT",
    "ALTER TABLE ghost_dm_room_history ADD COLUMN IF NOT EXISTS local_username TEXT",
    "ALTER TABLE ghost_chat_room_history ADD COLUMN IF NOT EXISTS local_username TEXT",
    # Backfilled from each row's matrix_user_id's single identity AT
    # MIGRATION TIME -- the only thing it could possibly have been, since
    # multiple identities per user didn't exist before this feature.
    """
    UPDATE ghost_dm_rooms r SET local_username = a.username
    FROM local_actors a
    WHERE a.matrix_user_id = r.matrix_user_id AND r.local_username IS NULL
    """,
    """
    UPDATE ghost_chat_rooms r SET local_username = a.username
    FROM local_actors a
    WHERE a.matrix_user_id = r.matrix_user_id AND r.local_username IS NULL
    """,
    """
    UPDATE ghost_dm_room_history h SET local_username = a.username
    FROM local_actors a
    WHERE a.matrix_user_id = h.matrix_user_id AND h.local_username IS NULL
    """,
    """
    UPDATE ghost_chat_room_history h SET local_username = a.username
    FROM local_actors a
    WHERE a.matrix_user_id = h.matrix_user_id AND h.local_username IS NULL
    """,
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
    # A PeerTube-compatible video channel (see PeerTubeChannelRecord's own
    # docstring for why this is a separate table from local_actors, and why
    # it's named with the "peertube_" prefix rather than bare "channels";
    # channel_rooms/guild_channels above are Shoot's own, unrelated concept).
    """
    CREATE TABLE IF NOT EXISTS peertube_channels (
        username TEXT PRIMARY KEY,
        owner_username TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_peertube_channels_owner ON peertube_channels (owner_username)",
    # A real Lemmy-compatible community HOSTED by this bridge (see
    # LemmyCommunityRecord's own docstring for how this differs from
    # lemmy_community_rooms below, which only ever mirrors a REMOTE one).
    # moderator_actor_ids is a JSON-encoded list of AP actor ids.
    """
    CREATE TABLE IF NOT EXISTS lemmy_communities (
        username TEXT PRIMARY KEY,
        owner_username TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        moderator_actor_ids TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lemmy_communities_owner ON lemmy_communities (owner_username)",
    # A mirrored Lemmy community (see LemmyCommunityRoom's own docstring for
    # why it has no ghost_user_id column, unlike remote_actor_rooms).
    """
    CREATE TABLE IF NOT EXISTS lemmy_community_rooms (
        community_actor_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        moderators_url TEXT,
        pending_backfill BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    "ALTER TABLE lemmy_community_rooms ADD COLUMN IF NOT EXISTS pending_backfill BOOLEAN NOT NULL DEFAULT FALSE",
    # A real FEP-1b12 Group HOSTED by this bridge (see GroupRecord's own
    # docstring -- a flat-chat Group, structurally like peertube_channels,
    # not lemmy_communities: no moderator_actor_ids equivalent).
    """
    CREATE TABLE IF NOT EXISTS hosted_groups (
        username TEXT PRIMARY KEY,
        owner_username TEXT NOT NULL,
        room_id TEXT NOT NULL UNIQUE,
        public_key_pem TEXT NOT NULL,
        private_key_pem TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hosted_groups_owner ON hosted_groups (owner_username)",
    # A mirrored REMOTE FEP-1b12 Group (see GroupRoom's own docstring --
    # same shape as lemmy_community_rooms minus moderators_url).
    """
    CREATE TABLE IF NOT EXISTS group_rooms (
        group_actor_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        icon_url TEXT,
        banner_url TEXT,
        pending_backfill BOOLEAN NOT NULL DEFAULT FALSE
    )
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
        # ORDER BY created_at picks the oldest deterministically when a
        # user has >1 identity -- see this method's Protocol docstring
        # (implementation-defined at >1, never an error).
        row = await self._pool.fetchrow(
            "SELECT * FROM local_actors WHERE matrix_user_id = $1 ORDER BY created_at ASC LIMIT 1", matrix_user_id
        )
        return self._row_to_actor(row) if row else None

    async def list_local_actors_by_matrix_id(self, matrix_user_id: str) -> list[ActorRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM local_actors WHERE matrix_user_id = $1 ORDER BY created_at ASC", matrix_user_id
        )
        return [self._row_to_actor(row) for row in rows]

    async def get_active_local_actor(self, matrix_user_id: str) -> ActorRecord | None:
        row = await self._pool.fetchrow(
            "SELECT username FROM active_identity WHERE matrix_user_id = $1", matrix_user_id
        )
        if row is not None:
            actor = await self.get_local_actor(row["username"])
            if actor is not None:
                return actor
        identities = await self.list_local_actors_by_matrix_id(matrix_user_id)
        return identities[0] if len(identities) == 1 else None

    async def set_active_identity(self, matrix_user_id: str, username: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO active_identity (matrix_user_id, username) VALUES ($1, $2)
            ON CONFLICT (matrix_user_id) DO UPDATE SET username = EXCLUDED.username
            """,
            matrix_user_id, username,
        )

    async def clear_active_identity(self, matrix_user_id: str) -> None:
        await self._pool.execute("DELETE FROM active_identity WHERE matrix_user_id = $1", matrix_user_id)

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
        # hide_followers/hide_following columns still exist (never dropped,
        # see this project's additive-only schema convention) but are no
        # longer read or written. That per-identity state now lives in
        # identity_settings, see bridge.identity_settings.
        #
        # created_at is NEVER overwritten by the ON CONFLICT branch below
        # (deliberately absent from its SET list) -- see
        # SqliteActorRepository._register_local_actor's identical reasoning.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT 1 FROM local_actors WHERE username = $1", record.username)
                existing_is_new = existing is None
                created_at = record.created_at or datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    """
                    INSERT INTO local_actors
                        (username, matrix_user_id, room_id, public_key_pem, private_key_pem,
                         display_name, summary, icon_url, banner_url, is_third_party, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (username) DO UPDATE SET
                        matrix_user_id = EXCLUDED.matrix_user_id,
                        room_id = EXCLUDED.room_id,
                        public_key_pem = EXCLUDED.public_key_pem,
                        private_key_pem = EXCLUDED.private_key_pem,
                        display_name = EXCLUDED.display_name,
                        summary = EXCLUDED.summary,
                        icon_url = EXCLUDED.icon_url,
                        banner_url = EXCLUDED.banner_url,
                        is_third_party = EXCLUDED.is_third_party
                    """,
                    record.username, record.matrix_user_id, record.room_id,
                    record.public_key_pem, record.private_key_pem,
                    record.display_name, record.summary, record.icon_url, record.banner_url,
                    record.is_third_party, created_at,
                )
                if existing_is_new:
                    # Always has an Active identity once it has at least
                    # one -- see SqliteActorRepository._register_local_actor's
                    # identical reasoning, including the "an additional
                    # identity makes an implicit single-identity Active
                    # explicit" branch.
                    other_identities = await conn.fetch(
                        "SELECT username FROM local_actors WHERE matrix_user_id = $1 AND username <> $2 "
                        "ORDER BY created_at ASC",
                        record.matrix_user_id, record.username,
                    )
                    if not other_identities:
                        await conn.execute(
                            """
                            INSERT INTO active_identity (matrix_user_id, username) VALUES ($1, $2)
                            ON CONFLICT (matrix_user_id) DO NOTHING
                            """,
                            record.matrix_user_id, record.username,
                        )
                    else:
                        has_active_row = await conn.fetchrow(
                            "SELECT 1 FROM active_identity WHERE matrix_user_id = $1", record.matrix_user_id
                        )
                        if has_active_row is None:
                            await conn.execute(
                                "INSERT INTO active_identity (matrix_user_id, username) VALUES ($1, $2)",
                                record.matrix_user_id, other_identities[0]["username"],
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
                existing = await conn.fetchrow("SELECT matrix_user_id FROM local_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM local_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM followers WHERE username = $1", username)
                await conn.execute("DELETE FROM following WHERE username = $1", username)
                await conn.execute("DELETE FROM blocked_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM muted_actors WHERE username = $1", username)
                await conn.execute("DELETE FROM identity_settings WHERE username = $1", username)
                if existing is not None:
                    matrix_user_id = existing["matrix_user_id"]
                    active_row = await conn.fetchrow(
                        "SELECT username FROM active_identity WHERE matrix_user_id = $1", matrix_user_id
                    )
                    if active_row is not None and active_row["username"] == username:
                        remaining = await conn.fetchrow(
                            "SELECT username FROM local_actors WHERE matrix_user_id = $1 "
                            "ORDER BY created_at ASC LIMIT 1",
                            matrix_user_id,
                        )
                        if remaining is not None:
                            # Always has an Active identity once it has at
                            # least one -- promote the oldest remaining one.
                            await conn.execute(
                                "UPDATE active_identity SET username = $1 WHERE matrix_user_id = $2",
                                remaining["username"], matrix_user_id,
                            )
                        else:
                            await conn.execute(
                                "DELETE FROM active_identity WHERE matrix_user_id = $1", matrix_user_id
                            )

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
            is_third_party=row["is_third_party"],
            created_at=row["created_at"],
        )

    # -- PeerTube channels ----------------------------------------------------

    @staticmethod
    def _row_to_peertube_channel(row: asyncpg.Record) -> PeerTubeChannelRecord:
        return PeerTubeChannelRecord(
            username=row["username"],
            owner_username=row["owner_username"],
            room_id=row["room_id"],
            public_key_pem=row["public_key_pem"],
            private_key_pem=row["private_key_pem"],
            display_name=row["display_name"],
            summary=row["summary"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
        )

    async def get_peertube_channel(self, username: str) -> PeerTubeChannelRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM peertube_channels WHERE username = $1", username)
        return self._row_to_peertube_channel(row) if row else None

    async def get_peertube_channel_by_room_id(self, room_id: str) -> PeerTubeChannelRecord | None:
        if not room_id:
            return None
        row = await self._pool.fetchrow("SELECT * FROM peertube_channels WHERE room_id = $1", room_id)
        return self._row_to_peertube_channel(row) if row else None

    async def list_peertube_channels_by_owner(self, owner_username: str) -> list[PeerTubeChannelRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM peertube_channels WHERE owner_username = $1 ORDER BY username", owner_username
        )
        return [self._row_to_peertube_channel(row) for row in rows]

    async def register_peertube_channel(self, record: PeerTubeChannelRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO peertube_channels
                (username, owner_username, room_id, public_key_pem, private_key_pem,
                 display_name, summary, icon_url, banner_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (username) DO UPDATE SET
                owner_username = EXCLUDED.owner_username,
                room_id = EXCLUDED.room_id,
                public_key_pem = EXCLUDED.public_key_pem,
                private_key_pem = EXCLUDED.private_key_pem,
                display_name = EXCLUDED.display_name,
                summary = EXCLUDED.summary,
                icon_url = EXCLUDED.icon_url,
                banner_url = EXCLUDED.banner_url
            """,
            record.username, record.owner_username, record.room_id,
            record.public_key_pem, record.private_key_pem,
            record.display_name, record.summary, record.icon_url, record.banner_url,
        )

    async def unregister_peertube_channel(self, username: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM peertube_channels WHERE username = $1", username)
                await conn.execute("DELETE FROM followers WHERE username = $1", username)

    # -- hosted Lemmy communities ---------------------------------------------

    @staticmethod
    def _row_to_lemmy_community(row: asyncpg.Record) -> LemmyCommunityRecord:
        return LemmyCommunityRecord(
            username=row["username"],
            owner_username=row["owner_username"],
            room_id=row["room_id"],
            public_key_pem=row["public_key_pem"],
            private_key_pem=row["private_key_pem"],
            display_name=row["display_name"],
            summary=row["summary"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
            moderator_actor_ids=tuple(json.loads(row["moderator_actor_ids"] or "[]")),
        )

    async def get_lemmy_community(self, username: str) -> LemmyCommunityRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM lemmy_communities WHERE username = $1", username)
        return self._row_to_lemmy_community(row) if row else None

    async def get_lemmy_community_by_room_id(self, room_id: str) -> LemmyCommunityRecord | None:
        if not room_id:
            return None
        row = await self._pool.fetchrow("SELECT * FROM lemmy_communities WHERE room_id = $1", room_id)
        return self._row_to_lemmy_community(row) if row else None

    async def list_lemmy_communities_by_owner(self, owner_username: str) -> list[LemmyCommunityRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM lemmy_communities WHERE owner_username = $1 ORDER BY username", owner_username
        )
        return [self._row_to_lemmy_community(row) for row in rows]

    async def register_lemmy_community(self, record: LemmyCommunityRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO lemmy_communities
                (username, owner_username, room_id, public_key_pem, private_key_pem,
                 display_name, summary, icon_url, banner_url, moderator_actor_ids)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (username) DO UPDATE SET
                owner_username = EXCLUDED.owner_username,
                room_id = EXCLUDED.room_id,
                public_key_pem = EXCLUDED.public_key_pem,
                private_key_pem = EXCLUDED.private_key_pem,
                display_name = EXCLUDED.display_name,
                summary = EXCLUDED.summary,
                icon_url = EXCLUDED.icon_url,
                banner_url = EXCLUDED.banner_url,
                moderator_actor_ids = EXCLUDED.moderator_actor_ids
            """,
            record.username, record.owner_username, record.room_id,
            record.public_key_pem, record.private_key_pem,
            record.display_name, record.summary, record.icon_url, record.banner_url,
            json.dumps(list(record.moderator_actor_ids)),
        )

    async def unregister_lemmy_community(self, username: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM lemmy_communities WHERE username = $1", username)
                await conn.execute("DELETE FROM followers WHERE username = $1", username)

    # -- hosted Groups (FEP-1b12) ---------------------------------------------

    @staticmethod
    def _row_to_group(row: asyncpg.Record) -> GroupRecord:
        return GroupRecord(
            username=row["username"],
            owner_username=row["owner_username"],
            room_id=row["room_id"],
            public_key_pem=row["public_key_pem"],
            private_key_pem=row["private_key_pem"],
            display_name=row["display_name"],
            summary=row["summary"],
            icon_url=row["icon_url"],
            banner_url=row["banner_url"],
        )

    async def get_group(self, username: str) -> GroupRecord | None:
        row = await self._pool.fetchrow("SELECT * FROM hosted_groups WHERE username = $1", username)
        return self._row_to_group(row) if row else None

    async def get_group_by_room_id(self, room_id: str) -> GroupRecord | None:
        if not room_id:
            return None
        row = await self._pool.fetchrow("SELECT * FROM hosted_groups WHERE room_id = $1", room_id)
        return self._row_to_group(row) if row else None

    async def list_groups_by_owner(self, owner_username: str) -> list[GroupRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM hosted_groups WHERE owner_username = $1 ORDER BY username", owner_username
        )
        return [self._row_to_group(row) for row in rows]

    async def register_group(self, record: GroupRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO hosted_groups
                (username, owner_username, room_id, public_key_pem, private_key_pem,
                 display_name, summary, icon_url, banner_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (username) DO UPDATE SET
                owner_username = EXCLUDED.owner_username,
                room_id = EXCLUDED.room_id,
                public_key_pem = EXCLUDED.public_key_pem,
                private_key_pem = EXCLUDED.private_key_pem,
                display_name = EXCLUDED.display_name,
                summary = EXCLUDED.summary,
                icon_url = EXCLUDED.icon_url,
                banner_url = EXCLUDED.banner_url
            """,
            record.username, record.owner_username, record.room_id,
            record.public_key_pem, record.private_key_pem,
            record.display_name, record.summary, record.icon_url, record.banner_url,
        )

    async def unregister_group(self, username: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM hosted_groups WHERE username = $1", username)
                await conn.execute("DELETE FROM followers WHERE username = $1", username)

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

    async def get_identity_settings(self, username: str) -> dict[str, str]:
        rows = await self._pool.fetch(
            "SELECT key, value FROM identity_settings WHERE username = $1", username
        )
        return {row["key"]: row["value"] for row in rows}

    async def set_identity_setting(self, username: str, key: str, value: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO identity_settings (username, key, value) VALUES ($1, $2, $3)
            ON CONFLICT (username, key) DO UPDATE SET value = EXCLUDED.value
            """,
            username, key, value,
        )

    async def clear_identity_setting(self, username: str, key: str) -> None:
        await self._pool.execute(
            "DELETE FROM identity_settings WHERE username = $1 AND key = $2", username, key
        )

    async def list_third_party_records(self) -> list[ActorRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM local_actors WHERE is_third_party AND room_id = ''"
        )
        return [self._row_to_actor(row) for row in rows]

    async def list_local_usernames(self) -> list[str]:
        # room_id != '' -- a genuine local user with a real Profile Room and
        # matrix_user_id, not a Follow-Only third-party placeholder (see
        # list_third_party_records's identical room_id filter, inverted).
        rows = await self._pool.fetch("SELECT username FROM local_actors WHERE room_id != ''")
        return [row["username"] for row in rows]

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

    # -- instance logins -------------------------------------------------------

    async def save_instance_login(self, record: InstanceLoginRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO instance_logins (
                matrix_user_id, instance_domain, auth_method, access_token,
                refresh_token, token_type, scope, account_handle, expires_at,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (matrix_user_id, instance_domain) DO UPDATE SET
                auth_method = EXCLUDED.auth_method,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_type = EXCLUDED.token_type,
                scope = EXCLUDED.scope,
                account_handle = EXCLUDED.account_handle,
                expires_at = EXCLUDED.expires_at,
                updated_at = EXCLUDED.updated_at
            """,
            record.matrix_user_id,
            record.instance_domain,
            record.auth_method,
            record.access_token,
            record.refresh_token,
            record.token_type,
            record.scope,
            record.account_handle,
            record.expires_at,
            record.created_at,
            record.updated_at,
        )

    @staticmethod
    def _row_to_instance_login(row) -> InstanceLoginRecord:
        return InstanceLoginRecord(
            matrix_user_id=row["matrix_user_id"],
            instance_domain=row["instance_domain"],
            auth_method=row["auth_method"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            token_type=row["token_type"],
            scope=row["scope"],
            account_handle=row["account_handle"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_instance_login(
        self, matrix_user_id: str, instance_domain: str
    ) -> InstanceLoginRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM instance_logins WHERE matrix_user_id = $1 AND instance_domain = $2",
            matrix_user_id, instance_domain,
        )
        return self._row_to_instance_login(row) if row else None

    async def list_instance_logins(self, matrix_user_id: str) -> list[InstanceLoginRecord]:
        rows = await self._pool.fetch(
            "SELECT * FROM instance_logins WHERE matrix_user_id = $1 ORDER BY instance_domain",
            matrix_user_id,
        )
        return [self._row_to_instance_login(row) for row in rows]

    async def delete_instance_login(self, matrix_user_id: str, instance_domain: str) -> None:
        await self._pool.execute(
            "DELETE FROM instance_logins WHERE matrix_user_id = $1 AND instance_domain = $2",
            matrix_user_id, instance_domain,
        )

    # -- remote actor rooms --------------------------------------------------

    async def get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM remote_actor_rooms WHERE actor_id = $1", actor_id)
        return self._row_to_remote_room(row) if row else None

    async def get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM remote_actor_rooms WHERE room_id = $1", room_id)
        return self._row_to_remote_room(row) if row else None

    async def list_all_remote_actor_room_ids(self) -> list[str]:
        rows = await self._pool.fetch("SELECT room_id FROM remote_actor_rooms")
        return [row["room_id"] for row in rows]

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
                 reposted_object_id, reposted_author_actor_id, is_primary_event)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (event_id) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                ap_object_id = EXCLUDED.ap_object_id,
                author_actor_id = EXCLUDED.author_actor_id,
                thread_root_event_id = EXCLUDED.thread_root_event_id,
                reposted_object_id = EXCLUDED.reposted_object_id,
                reposted_author_actor_id = EXCLUDED.reposted_author_actor_id,
                is_primary_event = EXCLUDED.is_primary_event
            """,
            record.event_id, record.room_id, record.ap_object_id,
            record.author_actor_id, record.thread_root_event_id,
            record.reposted_object_id, record.reposted_author_actor_id,
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

    async def delete_federated_event(self, event_id: str) -> None:
        await self._pool.execute("DELETE FROM federated_events WHERE event_id = $1", event_id)

    @staticmethod
    def _row_to_federated_event(row: asyncpg.Record) -> FederatedEvent:
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

    async def has_processed_matrix_event(self, event_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM processed_matrix_events WHERE event_id = $1", event_id
        )
        return row is not None

    async def mark_matrix_event_processed(self, event_id: str) -> None:
        now = time.time()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO processed_matrix_events (event_id, processed_at) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING",
                    event_id, now,
                )
                # Opportunistic cleanup -- same pattern as
                # mark_transaction_processed's identical cleanup.
                await conn.execute(
                    "DELETE FROM processed_matrix_events WHERE processed_at < $1",
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

    # -- outage recovery -------------------------------------------------------

    async def get_last_outage_recovery_at(self) -> float | None:
        return await self._pool.fetchval("SELECT max(detected_at) FROM outage_recoveries")

    async def record_outage_recovery(
        self, *, detected_at: float, outage_started_at: int, destinations_affected: int
    ) -> None:
        await self._pool.execute(
            "INSERT INTO outage_recoveries (detected_at, outage_started_at, destinations_affected) "
            "VALUES ($1, $2, $3)",
            detected_at, outage_started_at, destinations_affected,
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

    async def claim_reaction(self, record: ReactionRecord) -> bool:
        claimed_event_id = await self._pool.fetchval(
            """
            INSERT INTO reactions
                (activity_id, room_id, event_id, target_ap_object_id,
                 reactor_ghost_mxid, reactor_matrix_user_id, secondary_event_id, custom_emoji_mxc)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            record.activity_id, record.room_id, record.event_id, record.target_ap_object_id,
            record.reactor_ghost_mxid, record.reactor_matrix_user_id, record.secondary_event_id,
            record.custom_emoji_mxc,
        )
        return claimed_event_id is not None

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

    # -- poll votes -----------------------------------------------------------

    async def record_poll_vote(self, record: PollVoteRecord) -> None:
        await self._pool.execute(
            """
            INSERT INTO poll_votes
                (vote_activity_id, question_ap_object_id, room_id,
                 voter_actor_id, matrix_user_id, matrix_event_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (vote_activity_id) DO UPDATE SET
                question_ap_object_id = EXCLUDED.question_ap_object_id,
                room_id = EXCLUDED.room_id,
                voter_actor_id = EXCLUDED.voter_actor_id,
                matrix_user_id = EXCLUDED.matrix_user_id,
                matrix_event_id = EXCLUDED.matrix_event_id
            """,
            record.vote_activity_id, record.question_ap_object_id, record.room_id,
            record.voter_actor_id, record.matrix_user_id, record.matrix_event_id,
        )

    async def get_poll_vote_by_activity_id(self, vote_activity_id: str) -> PollVoteRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM poll_votes WHERE vote_activity_id = $1", vote_activity_id
        )
        return self._row_to_poll_vote(row) if row else None

    async def get_poll_vote_by_matrix_user(
        self, question_ap_object_id: str, matrix_user_id: str
    ) -> PollVoteRecord | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM poll_votes WHERE question_ap_object_id = $1 AND matrix_user_id = $2",
            question_ap_object_id, matrix_user_id,
        )
        return self._row_to_poll_vote(row) if row else None

    async def get_custom_emoji_mxc(self, source_url: str) -> str | None:
        row = await self._pool.fetchrow("SELECT mxc_url FROM custom_emoji WHERE source_url = $1", source_url)
        return row["mxc_url"] if row else None

    async def record_custom_emoji_mxc(self, source_url: str, mxc_url: str, name: str | None = None) -> None:
        await self._pool.execute(
            """
            INSERT INTO custom_emoji (source_url, mxc_url, name) VALUES ($1, $2, $3)
            ON CONFLICT (source_url) DO UPDATE SET name = COALESCE(custom_emoji.name, EXCLUDED.name)
            """,
            source_url, mxc_url, name,
        )

    async def get_custom_emoji_source(self, mxc_url: str) -> tuple[str, str | None] | None:
        row = await self._pool.fetchrow(
            "SELECT source_url, name FROM custom_emoji WHERE mxc_url = $1 LIMIT 1", mxc_url,
        )
        return (row["source_url"], row["name"]) if row else None

    async def get_custom_emoji_mxc_by_source_prefix_and_name(self, source_prefix: str, name: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT mxc_url FROM custom_emoji WHERE source_url LIKE $1 AND name = $2 LIMIT 1",
            f"{source_prefix}%", name,
        )
        return row["mxc_url"] if row else None

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

    @staticmethod
    def _row_to_poll_vote(row: asyncpg.Record) -> PollVoteRecord:
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

    # -- Shoot guild/channel bridging ----------------------------------------

    async def record_pending_guild_follow(self, record: PendingGuildFollow) -> None:
        await self._pool.execute(
            """
            INSERT INTO pending_guild_follows
                (follow_id, guild_actor_id, username, matrix_user_id, invite_code, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (follow_id) DO UPDATE SET
                guild_actor_id = EXCLUDED.guild_actor_id,
                username = EXCLUDED.username,
                matrix_user_id = EXCLUDED.matrix_user_id,
                invite_code = EXCLUDED.invite_code,
                created_at = EXCLUDED.created_at
            """,
            record.follow_id, record.guild_actor_id, record.username,
            record.matrix_user_id, record.invite_code, record.created_at,
        )

    async def get_pending_guild_follow(self, follow_id: str) -> PendingGuildFollow | None:
        row = await self._pool.fetchrow("SELECT * FROM pending_guild_follows WHERE follow_id = $1", follow_id)
        if row is None:
            return None
        return PendingGuildFollow(
            follow_id=row["follow_id"], guild_actor_id=row["guild_actor_id"], username=row["username"],
            matrix_user_id=row["matrix_user_id"], invite_code=row["invite_code"], created_at=row["created_at"],
        )

    async def remove_pending_guild_follow(self, follow_id: str) -> None:
        await self._pool.execute("DELETE FROM pending_guild_follows WHERE follow_id = $1", follow_id)

    async def record_guild_membership(self, username: str, guild_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO guild_memberships (username, guild_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            username, guild_actor_id,
        )

    async def get_guild_membership(self, username: str, guild_actor_id: str) -> GuildMembership | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM guild_memberships WHERE username = $1 AND guild_actor_id = $2", username, guild_actor_id
        )
        if row is None:
            return None
        return GuildMembership(username=row["username"], guild_actor_id=row["guild_actor_id"])

    async def list_guild_memberships(self, username: str) -> list[GuildMembership]:
        rows = await self._pool.fetch("SELECT * FROM guild_memberships WHERE username = $1", username)
        return [GuildMembership(username=row["username"], guild_actor_id=row["guild_actor_id"]) for row in rows]

    async def is_guild_member(self, guild_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM guild_memberships WHERE guild_actor_id = $1 LIMIT 1", guild_actor_id
        )
        return row is not None

    async def list_guild_members(self, guild_actor_id: str) -> list[str]:
        rows = await self._pool.fetch("SELECT username FROM guild_memberships WHERE guild_actor_id = $1", guild_actor_id)
        return [row["username"] for row in rows]

    async def remove_guild_membership(self, username: str, guild_actor_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM guild_memberships WHERE username = $1 AND guild_actor_id = $2", username, guild_actor_id,
        )

    async def set_guild_space(self, guild_actor_id: str, space_room_id: str) -> None:
        await self._pool.execute(
            """
            INSERT INTO guild_spaces (guild_actor_id, space_room_id) VALUES ($1, $2)
            ON CONFLICT (guild_actor_id) DO UPDATE SET space_room_id = EXCLUDED.space_room_id
            """,
            guild_actor_id, space_room_id,
        )

    async def get_guild_space(self, guild_actor_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT space_room_id FROM guild_spaces WHERE guild_actor_id = $1", guild_actor_id)
        return row["space_room_id"] if row else None

    async def get_guild_by_space_room_id(self, space_room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT guild_actor_id FROM guild_spaces WHERE space_room_id = $1", space_room_id)
        return row["guild_actor_id"] if row else None

    async def record_guild_channels(self, guild_actor_id: str, channels: list[tuple[str, str]]) -> None:
        for channel_actor_id, name in channels:
            await self._pool.execute(
                """
                INSERT INTO guild_channels (channel_actor_id, guild_actor_id, name) VALUES ($1, $2, $3)
                ON CONFLICT (channel_actor_id) DO UPDATE SET
                    guild_actor_id = EXCLUDED.guild_actor_id, name = EXCLUDED.name
                """,
                channel_actor_id, guild_actor_id, name,
            )

    async def get_guild_channel(self, channel_actor_id: str) -> GuildChannel | None:
        row = await self._pool.fetchrow("SELECT * FROM guild_channels WHERE channel_actor_id = $1", channel_actor_id)
        if row is None:
            return None
        return GuildChannel(
            channel_actor_id=row["channel_actor_id"], guild_actor_id=row["guild_actor_id"], name=row["name"]
        )

    @staticmethod
    def _row_to_channel_room(row) -> ChannelRoom:
        return ChannelRoom(
            channel_actor_id=row["channel_actor_id"], room_id=row["room_id"],
            guild_actor_id=row["guild_actor_id"], display_name=row["display_name"],
        )

    async def get_channel_room(self, channel_actor_id: str) -> ChannelRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM channel_rooms WHERE channel_actor_id = $1", channel_actor_id)
        return self._row_to_channel_room(row) if row else None

    async def get_channel_room_by_room_id(self, room_id: str) -> ChannelRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM channel_rooms WHERE room_id = $1", room_id)
        return self._row_to_channel_room(row) if row else None

    async def register_channel_room(self, record: ChannelRoom) -> None:
        await self._pool.execute(
            """
            INSERT INTO channel_rooms (channel_actor_id, room_id, guild_actor_id, display_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (channel_actor_id) DO UPDATE SET
                room_id = EXCLUDED.room_id, guild_actor_id = EXCLUDED.guild_actor_id,
                display_name = EXCLUDED.display_name
            """,
            record.channel_actor_id, record.room_id, record.guild_actor_id, record.display_name,
        )

    async def is_channel_member_known(self, room_id: str, member_actor_id: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM channel_room_members WHERE room_id = $1 AND member_actor_id = $2", room_id, member_actor_id
        )
        return row is not None

    async def record_channel_member(self, room_id: str, member_actor_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO channel_room_members (room_id, member_actor_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            room_id, member_actor_id,
        )

    @staticmethod
    def _row_to_lemmy_community_room(row) -> LemmyCommunityRoom:
        return LemmyCommunityRoom(
            community_actor_id=row["community_actor_id"], room_id=row["room_id"],
            display_name=row["display_name"], icon_url=row["icon_url"], banner_url=row["banner_url"],
            moderators_url=row["moderators_url"], pending_backfill=row["pending_backfill"],
        )

    async def get_lemmy_community_room(self, community_actor_id: str) -> LemmyCommunityRoom | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM lemmy_community_rooms WHERE community_actor_id = $1", community_actor_id
        )
        return self._row_to_lemmy_community_room(row) if row else None

    async def get_lemmy_community_room_by_room_id(self, room_id: str) -> LemmyCommunityRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM lemmy_community_rooms WHERE room_id = $1", room_id)
        return self._row_to_lemmy_community_room(row) if row else None

    async def register_lemmy_community_room(self, record: LemmyCommunityRoom) -> None:
        # pending_backfill is deliberately NOT in the DO UPDATE SET below --
        # same reasoning as RemoteActorRoom's identical omission: only
        # mark_lemmy_community_backfill_pending_done is allowed to change it
        # after the row's first creation.
        await self._pool.execute(
            """
            INSERT INTO lemmy_community_rooms
                (community_actor_id, room_id, display_name, icon_url, banner_url, moderators_url, pending_backfill)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (community_actor_id) DO UPDATE SET
                room_id = EXCLUDED.room_id, display_name = EXCLUDED.display_name,
                icon_url = EXCLUDED.icon_url, banner_url = EXCLUDED.banner_url,
                moderators_url = EXCLUDED.moderators_url
            """,
            record.community_actor_id, record.room_id, record.display_name,
            record.icon_url, record.banner_url, record.moderators_url, record.pending_backfill,
        )

    async def mark_lemmy_community_backfill_pending_done(self, room_id: str) -> None:
        await self._pool.execute(
            "UPDATE lemmy_community_rooms SET pending_backfill = FALSE WHERE room_id = $1", room_id
        )

    @staticmethod
    def _row_to_group_room(row) -> GroupRoom:
        return GroupRoom(
            group_actor_id=row["group_actor_id"], room_id=row["room_id"],
            display_name=row["display_name"], icon_url=row["icon_url"], banner_url=row["banner_url"],
            pending_backfill=row["pending_backfill"],
        )

    async def get_group_room(self, group_actor_id: str) -> GroupRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM group_rooms WHERE group_actor_id = $1", group_actor_id)
        return self._row_to_group_room(row) if row else None

    async def get_group_room_by_room_id(self, room_id: str) -> GroupRoom | None:
        row = await self._pool.fetchrow("SELECT * FROM group_rooms WHERE room_id = $1", room_id)
        return self._row_to_group_room(row) if row else None

    async def register_group_room(self, record: GroupRoom) -> None:
        # pending_backfill deliberately NOT in the DO UPDATE SET -- same
        # reasoning as register_lemmy_community_room's identical omission.
        await self._pool.execute(
            """
            INSERT INTO group_rooms (group_actor_id, room_id, display_name, icon_url, banner_url, pending_backfill)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (group_actor_id) DO UPDATE SET
                room_id = EXCLUDED.room_id, display_name = EXCLUDED.display_name,
                icon_url = EXCLUDED.icon_url, banner_url = EXCLUDED.banner_url
            """,
            record.group_actor_id, record.room_id, record.display_name,
            record.icon_url, record.banner_url, record.pending_backfill,
        )

    async def mark_group_room_backfill_pending_done(self, room_id: str) -> None:
        await self._pool.execute("UPDATE group_rooms SET pending_backfill = FALSE WHERE room_id = $1", room_id)

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

    async def register_ghost_dm_room(
        self, actor_id: str, matrix_user_id: str, room_id: str, local_username: str
    ) -> None:
        # local_username is deliberately absent from both ON CONFLICT SET
        # clauses below -- stamped once, at this room's first-ever
        # registration, and never overwritten by a later call for the same
        # (actor_id, matrix_user_id). See register_ghost_dm_room's Protocol
        # docstring.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO ghost_dm_rooms (actor_id, matrix_user_id, room_id, local_username)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (actor_id, matrix_user_id) DO UPDATE SET room_id = EXCLUDED.room_id
                    """,
                    actor_id, matrix_user_id, room_id, local_username,
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
                    INSERT INTO ghost_dm_room_history (room_id, actor_id, matrix_user_id, is_current, local_username)
                    VALUES ($1, $2, $3, TRUE, $4)
                    ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                    """,
                    room_id, actor_id, matrix_user_id, local_username,
                )

    async def get_ghost_dm_room_local_username(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT local_username FROM ghost_dm_rooms WHERE room_id = $1", room_id)
        return row["local_username"] if row else None

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

    async def list_ghost_dm_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT room_id FROM ghost_dm_rooms WHERE matrix_user_id = $1", matrix_user_id
        )
        return [row["room_id"] for row in rows]

    # -- ghost chat rooms (ActivityPub ChatMessage, distinct from DM) -------

    async def get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        row = await self._pool.fetchrow(
            "SELECT room_id FROM ghost_chat_rooms WHERE actor_id = $1 AND matrix_user_id = $2",
            actor_id, matrix_user_id,
        )
        return row["room_id"] if row else None

    async def register_ghost_chat_room(
        self, actor_id: str, matrix_user_id: str, room_id: str, local_username: str
    ) -> None:
        # local_username semantics match register_ghost_dm_room exactly --
        # see its comment above.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO ghost_chat_rooms (actor_id, matrix_user_id, room_id, local_username)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (actor_id, matrix_user_id) DO UPDATE SET room_id = EXCLUDED.room_id
                    """,
                    actor_id, matrix_user_id, room_id, local_username,
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
                    INSERT INTO ghost_chat_room_history (room_id, actor_id, matrix_user_id, is_current, local_username)
                    VALUES ($1, $2, $3, TRUE, $4)
                    ON CONFLICT (room_id) DO UPDATE SET is_current = TRUE
                    """,
                    room_id, actor_id, matrix_user_id, local_username,
                )

    async def get_ghost_chat_room_local_username(self, room_id: str) -> str | None:
        row = await self._pool.fetchrow("SELECT local_username FROM ghost_chat_rooms WHERE room_id = $1", room_id)
        return row["local_username"] if row else None

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

    async def list_ghost_chat_rooms_for_user(self, matrix_user_id: str) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT room_id FROM ghost_chat_rooms WHERE matrix_user_id = $1", matrix_user_id
        )
        return [row["room_id"] for row in rows]
