"""Loader for the bridge's ``config.yaml``.

Parses the YAML file described in ``config.example.yaml`` into typed,
validated dataclasses so the rest of the codebase never touches raw dicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
CONFIG_PATH_ENV_VAR = "BRIDGE_CONFIG"


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing required fields or malformed."""


@dataclass(frozen=True)
class BridgeSection:
    domain: str
    public_base_url: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8090
    # Address Synapse should push AppService transactions to. Defaults to
    # http://{listen_host}:{listen_port} -- i.e. directly, bypassing the public
    # internet/reverse proxy entirely, since that traffic never needs to leave
    # the host (or trusted network) Synapse and the bridge run on. Override
    # this only if Synapse and the bridge are on different hosts and can't
    # reach each other directly.
    internal_base_url: str | None = None
    # Whether a knock on a Remote User Room from a Matrix user on a DIFFERENT
    # homeserver is auto-accepted the way a local user's is (see
    # bridge.membership.maybe_handle_knock). Off by default: mirrored posts
    # are public fediverse content, but who gets auto-admitted into this
    # server's rooms is this server's call -- a federated user can still be
    # invited manually by someone already inside either way.
    accept_federated_knocks: bool = False
    # Default number of posts ``;backfill`` pulls from a remote actor's own
    # outbox (or, run inside a Matrix thread mirroring an AP conversation,
    # from that post's own replies) when no explicit count is given -- see
    # bridge.commands._handle_backfill. Only a Matrix server admin can
    # override this with an explicit argument; anyone else always gets this
    # default.
    backfill_default_count: int = 15
    # Whether to set org.matrix.msc4501.social.profile_room_id (MSC4133
    # Extensible Profiles) on every ghost, pointing at its Remote User
    # Room -- see bridge.note_mirroring.set_ghost_profile_room_id. Off by
    # default, matching Synapse's own default for MSC4133 itself: it
    # requires experimental_features.msc4133_enabled: true in
    # homeserver.yaml (confirmed against Synapse's own source, 2026-07-08)
    # -- not set by default even there, so a fresh bridge deployment
    # shouldn't eat a guaranteed-failing request on every ghost
    # registration until the operator has actually opted into MSC4133 on
    # the homeserver side too. Turn on once that's done.
    set_msc4501_profile_room_id: bool = False
    # Whether to set org.matrix.msc4501.social.relates_to (rel_type-tagged,
    # same convention as Matrix's own m.relates_to) on every mirrored boost
    # (Announce), quote-post, and cross-posted reply echo -- see
    # bridge.inbox_dispatch's _handle_announce/_handle_create/
    # _echo_reply_in_own_room. On by default: unlike
    # use_msc4501_post_event_type below, this is purely additive content
    # on an ordinary m.room.message event, so a client with no idea what
    # MSC4501 is just ignores the extra field and renders the message
    # exactly as it always has.
    set_msc4501_relates_to: bool = True
    # For a mirrored boost specifically (never a quote-post or reply, which
    # always carry real commentary of their own -- see
    # bridge.inbox_dispatch._build_repost_message's own docstring): whether
    # relates_to asserts content_inline (the mirrored event's own content
    # already IS the boosted post's content, so relates_to.content would
    # just be a second copy of the same thing) instead of duplicating that
    # content into relates_to.content a second time. On by default -- less
    # redundant storage per event. Set to false to duplicate into
    # relates_to.content instead (the original behavior, before
    # content_inline existed), e.g. if some client you care about doesn't
    # yet handle content_inline.
    use_msc4501_content_inline: bool = True
    # Whether a quote-post's TARGET (the post it quotes, when we don't
    # already have a local mirror of it -- see
    # bridge.inbox_dispatch._quoted_post_render) gets actually imported
    # into its own Remote User Room, the same on-demand
    # ghost/room-provisioning bridge.note_mirroring.import_note already
    # does for an untracked reply's root. Doing so is what lets
    # set_msc4501_relates_to above actually populate for a quote of a post
    # nobody here already tracks -- MSC4501 requires a real room_id/event_id
    # to point at, and there's nothing to reference without a local mirror.
    # Unlike a reply's root (unambiguously wanted conversational context),
    # a quote is frequently adversarial ("quote-dunking" someone to mock
    # them, not to amplify them) -- importing it unconditionally means
    # provisioning a room/ghost for a total stranger purely because someone
    # we follow made fun of them.
    #   - "known" (default): only import if the quoted post's author
    #     already has a Remote User Room for some other reason (followed,
    #     replied to, previously imported, ...) -- never provisions a
    #     brand-new room/ghost purely from being quoted.
    #   - "always": import unconditionally, maximizing relates_to coverage.
    #   - "never": the old read-only behavior -- fetch a preview over AP,
    #     never mirror the quoted post, relates_to stays unset.
    quote_import_policy: str = "known"
    # Whether to mirror a remote fediverse account's posts (their own new
    # posts, replies, and boosts -- never DMs/Chats, which aren't "posts")
    # using org.matrix.msc4501.social.post as the event TYPE, instead of
    # the ordinary m.room.message every other client already understands.
    # Off by default, and NOT recommended to turn on until Phase 2 of
    # MSC4501's own message-interoperability rollout plan -- unlike
    # set_msc4501_relates_to above, this changes the event's actual type,
    # so a client that has never heard of MSC4501 won't render the event
    # at all (not even as a blank bubble), rather than just ignoring a
    # field it doesn't recognize.
    use_msc4501_post_event_type: bool = False
    # Default join_rule for a Remote User Room (a ghost's mirror of a
    # remote fediverse account) -- "knock" (default) lets anyone locked
    # out (e.g. after a `;replace room`) ask their way back in without an
    # admin, see bridge.membership.maybe_handle_knock. "invite"/"public"
    # are the only other values this bridge actually supports without
    # further plumbing -- "restricted"/"knock_restricted" need a space
    # membership condition this bridge never sets up.
    ghost_room_join_rule: str = "knock"
    # Same as ghost_room_join_rule, but for a local user's own Profile
    # Room (`;create profile`/`;link profile`/`;replace room`). Separate
    # setting since a local user's own room and a ghost's mirror of a
    # stranger's account are different trust situations -- e.g. an
    # operator might want their own users' profiles knockable but a
    # remote account's mirror invite-only, or vice versa.
    local_profile_room_join_rule: str = "knock"
    # Whether an admin-allowlisted user on a DIFFERENT Matrix homeserver
    # (see the `;allow`/`;disallow`/`;allowed` commands) gets treated as a
    # full local user ("full": self-service `;create profile`/`;link
    # profile`, their own Profile Room, DM, Chat, everything) or as
    # follow-and-interact-only ("follow_only", default: no Profile Room, no
    # `;dm`/`;chat`/`;banner`/`;replace room`/`;backfill`/`;repost` -- their
    # AP identity is minted automatically on first `;follow`, with a
    # bridge-held keypair and a profile that always mirrors their live
    # Matrix display name/avatar, never self-controlled).
    #
    # This is a single deployment-wide setting, not per-grant -- every
    # currently-allowed third party gets whichever mode this says, live,
    # not whatever mode was in effect when they were first allowed or first
    # interacted. An allowlisted user with no linked identity yet who was
    # previously blocked from `;create profile`/`;link profile` under
    # follow_only immediately gets access to those the moment this flips to
    # full, with no migration step; someone who already has a Profile Room
    # from a past "full" period keeps it if this flips back to follow_only,
    # it just stops being used to represent them on the fediverse (stops
    # sending posts out, their AP profile reverts to mirroring Matrix)
    # until this flips to full again -- nothing about an existing identity
    # is ever deleted or reassigned by changing this alone.
    third_party_access_mode: str = "follow_only"

    def resolved_internal_base_url(self) -> str:
        return self.internal_base_url or f"http://{self.listen_host}:{self.listen_port}"


@dataclass(frozen=True)
class SynapseSection:
    base_url: str
    server_name: str
    admin_token: str


@dataclass(frozen=True)
class AppserviceSection:
    registration_path: str
    id: str
    hs_token: str
    as_token: str
    user_prefix: str = "ap_"
    bot_localpart: str = "bridgebot"
    # Optional display name / avatar for the bot's own Matrix profile, applied
    # (idempotently) on every startup. Leave unset to not manage either.
    bot_display_name: str | None = None
    # An mxc:// URI -- upload an image with any Matrix client (or reuse one
    # already on your server) and use its mxc:// URI here.
    bot_avatar_mxc: str | None = None


@dataclass(frozen=True)
class PostgresSection:
    """Connection details for ``storage.backend: postgresql``.

    Either ``dsn`` (a full ``postgresql://user:pass@host:port/dbname``
    connection string) or the discrete fields below may be given -- ``dsn``
    wins if both are present. The discrete form exists mainly so a secret
    password doesn't have to be hand-assembled into a URL-escaped DSN string
    in the config file.
    """

    dsn: str | None = None
    host: str = "localhost"
    port: int = 5432
    database: str = "matrix_appservice_activitypub"
    user: str = "matrix_appservice_activitypub"
    password: str = ""
    min_pool_size: int = 1
    max_pool_size: int = 10

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        return (
            f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{quote(self.database, safe='')}"
        )


@dataclass(frozen=True)
class StorageSection:
    # "sqlite" (default -- a single file under data_dir) or "postgresql".
    backend: str = "sqlite"
    data_dir: str = "./data"
    postgres: PostgresSection = field(default_factory=PostgresSection)


@dataclass(frozen=True)
class FederationSection:
    request_timeout: float = 15.0
    actor_key_cache_ttl: int = 3600
    max_clock_skew: int = 3600
    # How far in the past a mirrored post's own "published" time is trusted
    # for Matrix "timestamp massaging" (bridge.note_mirroring.resolve_event_ts)
    # before falling back to "now" instead. This is NOT primarily about
    # trusting the remote server -- it's a safety margin against Synapse's
    # own retention.default_policy.max_lifetime (if the homeserver has one
    # configured): confirmed live that a custom origin_server_ts older than
    # that policy's max_lifetime is silently accepted (200 OK, a real
    # event_id) but then never actually reachable -- not via a later GET,
    # not via /messages, nothing -- effectively a silent post-drop, which is
    # a strictly worse outcome than just showing "now" as this bridge has
    # always done. There is no Client-Server API to discover a homeserver's
    # own retention policy, so this must be set (comfortably below) it by
    # hand if one is configured; leave generous (or raise) if the homeserver
    # has no retention policy limiting history at all.
    max_backdate_days: int = 730


@dataclass(frozen=True)
class LoggingSection:
    level: str = "INFO"


@dataclass(frozen=True)
class BridgeConfig:
    bridge: BridgeSection
    synapse: SynapseSection
    appservice: AppserviceSection
    storage: StorageSection
    federation: FederationSection
    logging: LoggingSection


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    if key not in section or section[key] in (None, ""):
        raise ConfigError(f"Missing required config field: {section_name}.{key}")
    return section[key]


def load_config(path: str | os.PathLike[str] | None = None) -> BridgeConfig:
    """Load and validate ``config.yaml``.

    Resolution order for the path: explicit ``path`` argument, then the
    ``BRIDGE_CONFIG`` environment variable, then ``./config.yaml``.
    """
    resolved = Path(path or os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH))
    if not resolved.is_file():
        raise ConfigError(
            f"Config file not found at {resolved}. Copy config.example.yaml to "
            f"{DEFAULT_CONFIG_PATH} and fill in your values."
        )

    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"{resolved} must contain a YAML mapping at the top level")

    try:
        bridge_raw = raw["bridge"]
        synapse_raw = raw["synapse"]
        appservice_raw = raw["appservice"]
    except KeyError as exc:
        raise ConfigError(f"Missing required top-level config section: {exc}") from exc

    internal_base_url = bridge_raw.get("internal_base_url")
    quote_import_policy = bridge_raw.get("quote_import_policy", "known")
    if quote_import_policy not in ("always", "known", "never"):
        raise ConfigError(
            f"bridge.quote_import_policy must be 'always', 'known', or 'never', got {quote_import_policy!r}"
        )
    _valid_join_rules = ("public", "invite", "knock")
    ghost_room_join_rule = bridge_raw.get("ghost_room_join_rule", "knock")
    if ghost_room_join_rule not in _valid_join_rules:
        raise ConfigError(
            f"bridge.ghost_room_join_rule must be one of {_valid_join_rules}, got {ghost_room_join_rule!r}"
        )
    local_profile_room_join_rule = bridge_raw.get("local_profile_room_join_rule", "knock")
    if local_profile_room_join_rule not in _valid_join_rules:
        raise ConfigError(
            f"bridge.local_profile_room_join_rule must be one of {_valid_join_rules}, "
            f"got {local_profile_room_join_rule!r}"
        )
    third_party_access_mode = bridge_raw.get("third_party_access_mode", "follow_only")
    if third_party_access_mode not in ("follow_only", "full"):
        raise ConfigError(
            f"bridge.third_party_access_mode must be 'follow_only' or 'full', got {third_party_access_mode!r}"
        )
    bridge_section = BridgeSection(
        domain=_require(bridge_raw, "domain", "bridge"),
        public_base_url=_require(bridge_raw, "public_base_url", "bridge").rstrip("/"),
        listen_host=bridge_raw.get("listen_host", "127.0.0.1"),
        listen_port=int(bridge_raw.get("listen_port", 8090)),
        internal_base_url=internal_base_url.rstrip("/") if internal_base_url else None,
        accept_federated_knocks=bool(bridge_raw.get("accept_federated_knocks", False)),
        backfill_default_count=int(bridge_raw.get("backfill_default_count", 15)),
        set_msc4501_profile_room_id=bool(bridge_raw.get("set_msc4501_profile_room_id", False)),
        set_msc4501_relates_to=bool(bridge_raw.get("set_msc4501_relates_to", True)),
        use_msc4501_content_inline=bool(bridge_raw.get("use_msc4501_content_inline", True)),
        quote_import_policy=quote_import_policy,
        use_msc4501_post_event_type=bool(bridge_raw.get("use_msc4501_post_event_type", False)),
        ghost_room_join_rule=ghost_room_join_rule,
        local_profile_room_join_rule=local_profile_room_join_rule,
        third_party_access_mode=third_party_access_mode,
    )

    synapse_section = SynapseSection(
        base_url=_require(synapse_raw, "base_url", "synapse").rstrip("/"),
        server_name=_require(synapse_raw, "server_name", "synapse"),
        admin_token=_require(synapse_raw, "admin_token", "synapse"),
    )

    appservice_section = AppserviceSection(
        registration_path=_require(appservice_raw, "registration_path", "appservice"),
        id=_require(appservice_raw, "id", "appservice"),
        hs_token=_require(appservice_raw, "hs_token", "appservice"),
        as_token=_require(appservice_raw, "as_token", "appservice"),
        user_prefix=appservice_raw.get("user_prefix", "ap_"),
        bot_localpart=appservice_raw.get("bot_localpart", "bridgebot"),
        bot_display_name=appservice_raw.get("bot_display_name"),
        bot_avatar_mxc=appservice_raw.get("bot_avatar_mxc"),
    )

    storage_raw = raw.get("storage", {}) or {}
    backend = storage_raw.get("backend", "sqlite")
    if backend not in ("sqlite", "postgresql"):
        raise ConfigError(f"storage.backend must be 'sqlite' or 'postgresql', got {backend!r}")
    postgres_raw = storage_raw.get("postgres", {}) or {}
    postgres_section = PostgresSection(
        dsn=postgres_raw.get("dsn"),
        host=postgres_raw.get("host", "localhost"),
        port=int(postgres_raw.get("port", 5432)),
        database=postgres_raw.get("database", "matrix_appservice_activitypub"),
        user=postgres_raw.get("user", "matrix_appservice_activitypub"),
        password=postgres_raw.get("password", ""),
        min_pool_size=int(postgres_raw.get("min_pool_size", 1)),
        max_pool_size=int(postgres_raw.get("max_pool_size", 10)),
    )
    storage_section = StorageSection(
        backend=backend,
        data_dir=storage_raw.get("data_dir", "./data"),
        postgres=postgres_section,
    )

    federation_raw = raw.get("federation", {}) or {}
    federation_section = FederationSection(
        request_timeout=float(federation_raw.get("request_timeout", 15.0)),
        actor_key_cache_ttl=int(federation_raw.get("actor_key_cache_ttl", 3600)),
        max_clock_skew=int(federation_raw.get("max_clock_skew", 3600)),
        max_backdate_days=int(federation_raw.get("max_backdate_days", 730)),
    )

    logging_raw = raw.get("logging", {}) or {}
    logging_section = LoggingSection(level=logging_raw.get("level", "INFO"))

    return BridgeConfig(
        bridge=bridge_section,
        synapse=synapse_section,
        appservice=appservice_section,
        storage=storage_section,
        federation=federation_section,
        logging=logging_section,
    )
