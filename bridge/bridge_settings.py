"""Bridge-wide behavior settings, DB-overridable from the widget's
Admin -> Settings page.

Mirrors ``bridge.identity_settings``'s "sparse DB override over a code
default" pattern, but global instead of per-identity, and merges into a
real ``BridgeConfig`` dataclass tree instead of a flat dict -- every other
read site in the codebase keeps reading ``app.state.config`` exactly as it
always has, now potentially carrying a database override instead of just
whatever ``config.yaml`` says.

Every field in ``bridge.config`` is registered here, including the
config-file-only and sensitive ones (``editable=False``[, ``sensitive=True``])
-- that's what lets the Settings page double as live documentation of
every setting, not just the ones it can actually change. A sensitive
definition's value is never read or serialized anywhere in this module,
regardless of ``editable``.

``BridgeSection.allow_db_setting_overrides`` is this mechanism's own
deployment-wide lockdown switch. It is enforced in ``effective_config``
below (read straight off the raw, never-merged config an admin session can
never touch) and, separately, in the widget routes before they ever call
``apply_override``/``clear_override`` -- never inside those two functions
themselves.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from bridge.config import BridgeConfig
from bridge.repository import ActorRepository

logger = logging.getLogger(__name__)


class SettingError(Exception):
    """Raised for an unknown key, a key that's config.yaml-only, or a value
    that fails to parse/validate. Callers turn this into an HTTP 400."""


@dataclass(frozen=True)
class BridgeSettingDefinition:
    key: str  # dotted, e.g. "bridge.third_party_access" -- the DB key and API id
    section: str  # attribute name on BridgeConfig, e.g. "bridge", "federation"
    field_name: str  # attribute name within that section; dotted for a nested field (e.g. "postgres.dsn")
    label: str
    description: str  # 1-3 sentences, shown in the admin UI
    value_type: str  # "bool" | "int" | "float" | "str" | "enum" | "list_str"
    enum_choices: tuple[str, ...] | None
    editable: bool  # False => config.yaml only; still registered, so it's still documented
    sensitive: bool  # True => value is NEVER read or serialized, anywhere, regardless of editable
    parse: Callable[[Any], Any]  # incoming API JSON value -> validated native value; raises SettingError
    serialize: Callable[[Any], str]  # native value -> DB TEXT
    deserialize: Callable[[str], Any]  # DB TEXT -> native value


def _get_value(config: BridgeConfig, definition: BridgeSettingDefinition) -> Any:
    value: Any = getattr(config, definition.section)
    for part in definition.field_name.split("."):
        value = getattr(value, part)
    return value


def _replace_path(obj: Any, path_parts: list[str], value: Any) -> Any:
    if len(path_parts) == 1:
        return dataclasses.replace(obj, **{path_parts[0]: value})
    child = getattr(obj, path_parts[0])
    return dataclasses.replace(obj, **{path_parts[0]: _replace_path(child, path_parts[1:], value)})


def _with_value(config: BridgeConfig, definition: BridgeSettingDefinition, value: Any) -> BridgeConfig:
    section_obj = getattr(config, definition.section)
    new_section_obj = _replace_path(section_obj, definition.field_name.split("."), value)
    return dataclasses.replace(config, **{definition.section: new_section_obj})


# -- per-type factory helpers, same shape as identity_settings._bool_setting --


def _bool_field(
    key: str, section: str, field_name: str, label: str, description: str,
    *, editable: bool = True, sensitive: bool = False,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> bool:
        if not isinstance(value, bool):
            raise SettingError(f'"{key}" must be true or false.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="bool", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=lambda v: "true" if v else "false", deserialize=lambda raw: raw == "true",
    )


def _int_field(
    key: str, section: str, field_name: str, label: str, description: str,
    *, editable: bool = True, sensitive: bool = False, minimum: int | None = None,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingError(f'"{key}" must be an integer.')
        if minimum is not None and value < minimum:
            raise SettingError(f'"{key}" must be at least {minimum}.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="int", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=str, deserialize=int,
    )


def _float_field(
    key: str, section: str, field_name: str, label: str, description: str,
    *, editable: bool = True, sensitive: bool = False, minimum: float | None = None,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingError(f'"{key}" must be a number.')
        parsed = float(value)
        if minimum is not None and parsed < minimum:
            raise SettingError(f'"{key}" must be at least {minimum}.')
        return parsed

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="float", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=str, deserialize=float,
    )


def _str_field(
    key: str, section: str, field_name: str, label: str, description: str,
    *, editable: bool = True, sensitive: bool = False, nullable: bool = False, allow_empty: bool = True,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> str | None:
        if value is None:
            if nullable:
                return None
            raise SettingError(f'"{key}" can\'t be empty.')
        if not isinstance(value, str):
            raise SettingError(f'"{key}" must be a string.')
        if value == "":
            if nullable:
                return None
            if not allow_empty:
                raise SettingError(f'"{key}" can\'t be empty.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="str", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse,
        serialize=lambda v: v or "",
        deserialize=(lambda raw: raw or None) if nullable else (lambda raw: raw),
    )


def _enum_field(
    key: str, section: str, field_name: str, label: str, description: str, choices: tuple[str, ...],
    *, editable: bool = True, sensitive: bool = False,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> str:
        if not isinstance(value, str) or value not in choices:
            raise SettingError(f'"{key}" must be one of: {", ".join(choices)}.')
        return value

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="enum", enum_choices=choices, editable=editable, sensitive=sensitive,
        parse=_parse, serialize=lambda v: v, deserialize=lambda raw: raw,
    )


def _list_str_field(
    key: str, section: str, field_name: str, label: str, description: str,
    *, editable: bool = True, sensitive: bool = False, container: type = list,
) -> BridgeSettingDefinition:
    def _parse(value: Any) -> Any:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SettingError(f'"{key}" must be a list of strings.')
        return container(value)

    return BridgeSettingDefinition(
        key=key, section=section, field_name=field_name, label=label, description=description,
        value_type="list_str", enum_choices=None, editable=editable, sensitive=sensitive,
        parse=_parse,
        serialize=lambda v: json.dumps(list(v)),
        deserialize=lambda raw: container(json.loads(raw)),
    )


# Ordered to roughly match bridge/config.py's own field order, grouped by section.
_REGISTRY: dict[str, BridgeSettingDefinition] = {
    definition.key: definition
    for definition in (
        # -- bridge: topology, config.yaml-only --
        _str_field("bridge.domain", "bridge", "domain", "Domain", "This bridge's own ActivityPub domain. Set once at deployment; never change on a live instance.", editable=False),
        _str_field("bridge.public_base_url", "bridge", "public_base_url", "Public base URL", "The public https:// URL this bridge is reachable at. Set once at deployment.", editable=False),
        _str_field("bridge.listen_host", "bridge", "listen_host", "Listen host", "The local address the bridge's HTTP server binds to.", editable=False),
        _int_field("bridge.listen_port", "bridge", "listen_port", "Listen port", "The local port the bridge's HTTP server binds to.", editable=False),
        _str_field("bridge.internal_base_url", "bridge", "internal_base_url", "Internal base URL", "Address Synapse pushes AppService transactions to, if different from the default direct host:port.", editable=False, nullable=True),
        # -- bridge: editable feature flags / policy --
        _bool_field("bridge.accept_federated_knocks", "bridge", "accept_federated_knocks", "Accept federated knocks", "Whether a knock on a Remote User Room from a user on a different homeserver is auto-accepted the same way a local user's is."),
        _int_field("bridge.backfill_default_count", "bridge", "backfill_default_count", "Default backfill count", "How many posts \";backfill\" pulls by default when no explicit count is given.", minimum=1),
        _bool_field("bridge.set_msc4501_profile_room", "bridge", "set_msc4501_profile_room", "Set MSC4501 profile room", "Whether to set the MSC4133 profile-room field on every ghost, pointing at its Remote User Room. Requires the homeserver to have MSC4133 enabled."),
        _enum_field("bridge.msc4503_external_handle", "bridge", "msc4503_external_handle", "MSC4503 external handle", "Where to carry a ghost's real ActivityPub handle (e.g. @alice@example.org): \"off\", \"profile\" (needs MSC4133), \"events\", or \"both\".", ("off", "profile", "events", "both")),
        _bool_field("bridge.msc4027_custom_reactions", "bridge", "msc4027_custom_reactions", "MSC4027 custom reactions", "Whether an inbound custom-emoji reaction is mirrored as a real MSC4027 custom-image reaction instead of plain text. Off by default since it replaces what every client renders for the reaction."),
        _bool_field("bridge.msc4144_per_message_profiles", "bridge", "msc4144_per_message_profiles", "MSC4144 per-message profiles", "Whether a bridge-sent message on a local user's behalf carries a real MSC4144 per-message profile, so it visually reads as that user's own action."),
        _bool_field("bridge.set_msc4501_relates_to", "bridge", "set_msc4501_relates_to", "Set MSC4501 relates_to", "Whether to set the MSC4501 relates_to field on mirrored reposts, quote-posts, and cross-posted reply echoes. Purely additive."),
        _bool_field("bridge.use_msc4501_content_inline", "bridge", "use_msc4501_content_inline", "MSC4501 content_inline for reposts", "For a mirrored repost, whether relates_to asserts content_inline instead of duplicating the reposted content a second time."),
        _enum_field("bridge.quote_import_policy", "bridge", "quote_import_policy", "Quote-post import policy", "Whether a quote-post's target gets imported into its own room when not already tracked: \"known\" (only if its author is already known), \"always\", or \"never\".", ("known", "always", "never")),
        _bool_field("bridge.use_msc4501_post_event_type", "bridge", "use_msc4501_post_event_type", "MSC4501 post event type", "Whether a mirrored post uses the MSC4501 event type instead of an ordinary m.room.message. Not recommended until MSC4501's own rollout reaches this phase -- a client with no idea what MSC4501 is won't render the event at all."),
        _enum_field("bridge.ghost_room_join_rule", "bridge", "ghost_room_join_rule", "Remote User Room join rule", "Default join_rule for a ghost's Remote User Room.", ("knock", "invite", "public")),
        _enum_field("bridge.local_profile_room_join_rule", "bridge", "local_profile_room_join_rule", "Profile Room join rule", "Default join_rule for a local user's own Profile Room.", ("knock", "invite", "public")),
        _bool_field("bridge.world_readable_remote_rooms", "bridge", "world_readable_remote_rooms", "World-readable remote rooms", "Whether a Remote User Room gets history_visibility: world_readable, letting anyone on this homeserver preview it without joining."),
        _bool_field("bridge.third_party_access", "bridge", "third_party_access", "Third-party access", "Master on/off switch for letting any user on a different Matrix homeserver interact with this bridge at all. Who specifically is allowed is still governed by ;allow/;disallow grants."),
        _int_field("bridge.poll_default_duration_days", "bridge", "poll_default_duration_days", "Poll duration", "Days after creation used as a Matrix poll's synthesized endTime, since Mastodon-family servers refuse a poll with none.", minimum=1),
        # -- bridge: security-sensitive, deliberately config.yaml-only --
        _list_str_field("bridge.admins", "bridge", "admins", "Admins", "Matrix user IDs always treated as bridge admins, regardless of real Synapse server-admin status. Config.yaml only: editing the admin list through the very feature it gates would be a privilege-escalation path.", editable=False),
        _bool_field("bridge.use_synapse_admin_api", "bridge", "use_synapse_admin_api", "Use Synapse Admin API", "Whether the bridge may call Synapse's own Admin API to check admin status and sweep a user's rooms. Config.yaml only, for the same reason as the admin list above.", editable=False),
        _bool_field("bridge.allow_db_setting_overrides", "bridge", "allow_db_setting_overrides", "Allow database setting overrides", "Master switch for this whole mechanism. Config.yaml only, and always read from the raw config file value -- a database override could never disable this lockdown itself.", editable=False),
        # -- bridge: more editable feature flags / policy --
        _bool_field("bridge.peertube_channels_enabled", "bridge", "peertube_channels_enabled", "PeerTube channels enabled", "Whether Matrix rooms can become PeerTube-compatible video channels at all. A real, substantial feature with its own bandwidth cost."),
        _enum_field("bridge.follow_video_media_mode", "bridge", "follow_video_media_mode", "Followed-video media mode", "How a followed PeerTube channel's videos show up: \"link\" (text/thumbnail card only) or \"embed\" (download and re-host natively, falling back to a link above the size cap below).", ("link", "embed")),
        _int_field("bridge.follow_video_max_embed_mb", "bridge", "follow_video_max_embed_mb", "Follow video max embed size (MB)", "In \"embed\" mode, the file-size cutoff above which a followed video falls back to a link card instead.", minimum=1),
        _bool_field("bridge.lemmy_communities_enabled", "bridge", "lemmy_communities_enabled", "Lemmy communities enabled", "Whether following a remote Lemmy community mirrors it as a shared Matrix room. A real, substantial feature."),
        _int_field("bridge.lemmy_community_resync_min_interval_seconds", "bridge", "lemmy_community_resync_min_interval_seconds", "Lemmy community resync interval (seconds)", "Minimum time between automatic resyncs of the same mirrored Lemmy community room. \";refresh\" always bypasses this.", minimum=0),
        _bool_field("bridge.lemmy_community_hosting_enabled", "bridge", "lemmy_community_hosting_enabled", "Lemmy community hosting enabled", "Whether a Matrix room can become a real, federating Lemmy-compatible community hosted by this bridge. A real, substantial feature."),
        _bool_field("bridge.groups_enabled", "bridge", "groups_enabled", "Groups enabled", "Whether FEP-1b12 Groups (hosting and following) are enabled at all. A real, substantial feature."),
        _bool_field("bridge.backstage_space_enabled", "bridge", "backstage_space_enabled", "Backstage space enabled", "Whether ghosts join a shared backstage space so they can self-join Remote User/Profile/Group/Lemmy community rooms instead of needing an invite every time. Never affects DM/Chat/Notification rooms."),
        _enum_field("bridge.reply_cross_post_mode", "bridge", "reply_cross_post_mode", "Reply cross-posting", "Whether a reply into an already-tracked conversation also gets an annotated echo posted into the replier's own room: \"off\", \"followed\" (only if already known), or \"always\".", ("off", "followed", "always")),
        _str_field("bridge.instance_admin_username", "bridge", "instance_admin_username", "Instance admin username", "Local username advertised as this instance's admin contact in the Mastodon-compatible instance document. Must already be a registered local actor.", nullable=True),
        _bool_field("bridge.custom_identities_enabled", "bridge", "custom_identities_enabled", "Custom identities enabled", "Whether a Matrix user may own more than one ActivityPub identity with a custom id, instead of exactly one tied to their Matrix localpart."),
        _int_field("bridge.max_identities_per_user", "bridge", "max_identities_per_user", "Max identities per user", "Per-user cap on how many identities they may create once custom identities are enabled. Bridge admins are never subject to this cap.", minimum=1),
        _list_str_field("bridge.reserved_usernames", "bridge", "reserved_usernames", "Reserved usernames", "Usernames (case-insensitive) nobody may claim for a new identity, channel, or community, to guard against impersonating staff-style names."),
        _list_str_field("bridge.default_timeline_domains", "bridge", "default_timeline_domains", "Default timeline domains", "Instance domain(s) whose federated timeline feeds the widget's own default Home view."),
        _str_field("bridge.command_prefix", "bridge", "command_prefix", "Command prefix", "The character(s) that start a bot command in chat (e.g. \";\"). Applied once at startup.", allow_empty=False),
        _list_str_field("bridge.other_bot_prefixes", "bridge", "other_bot_prefixes", "Other bot prefixes", "Other bots'/bridges' own command prefixes sharing a room with this one, so this bridge never federates their commands as ordinary posts."),
        _list_str_field("bridge.other_bridge_users", "bridge", "other_bridge_users", "Other bridge users", "Regex patterns matching other bridges' own ghost/bot mxid namespaces sharing a room with this one, so this bridge recognizes them as puppets, never as unlinked humans."),
        # -- synapse: config.yaml-only --
        _str_field("synapse.base_url", "synapse", "base_url", "Synapse base URL", "The homeserver's own base URL. Set once at deployment.", editable=False),
        _str_field("synapse.server_name", "synapse", "server_name", "Synapse server name", "The homeserver's server_name. Set once at deployment.", editable=False),
        _str_field("synapse.admin_token", "synapse", "admin_token", "Synapse admin token", "Admin API token for this homeserver.", editable=False, sensitive=True, nullable=True),
        # -- appservice: config.yaml-only --
        _str_field("appservice.registration_path", "appservice", "registration_path", "Registration file path", "Path to the AppService registration YAML. Set once at deployment.", editable=False),
        _str_field("appservice.id", "appservice", "id", "AppService id", "This AppService's registered id. Set once at deployment.", editable=False),
        _str_field("appservice.hs_token", "appservice", "hs_token", "Homeserver token", "The AppService registration's hs_token.", editable=False, sensitive=True),
        _str_field("appservice.as_token", "appservice", "as_token", "AppService token", "The AppService registration's as_token.", editable=False, sensitive=True),
        _str_field("appservice.user_prefix", "appservice", "user_prefix", "Ghost user prefix", "Localpart prefix for every ghost this bridge registers. Set once at deployment.", editable=False),
        _str_field("appservice.bot_localpart", "appservice", "bot_localpart", "Bot localpart", "The bridge bot's own localpart. Set once at deployment.", editable=False),
        _str_field("appservice.bot_display_name", "appservice", "bot_display_name", "Bot display name", "Display name pushed to the bot's own Matrix profile. Applied immediately on change (see bridge.ghosts.sync_bot_profile), not just at startup. Leave unset to not manage it -- unsetting never clears an existing name, it just stops updating it.", nullable=True),
        _str_field("appservice.bot_avatar_mxc", "appservice", "bot_avatar_mxc", "Bot avatar", "An mxc:// URI pushed to the bot's own Matrix profile. Same immediate-apply and \"never clears\" behavior as the display name above.", nullable=True),
        # -- storage: config.yaml-only (backend choice + connection details) --
        _str_field("storage.backend", "storage", "backend", "Storage backend", "\"sqlite\" or \"postgresql\". Set once at deployment -- switching requires a real data migration.", editable=False),
        _str_field("storage.data_dir", "storage", "data_dir", "Data directory", "Where sqlite's own data file (and other on-disk state) lives.", editable=False),
        _str_field("storage.postgres.dsn", "storage", "postgres.dsn", "Postgres DSN", "Full Postgres connection string, if used instead of the discrete fields below.", editable=False, sensitive=True, nullable=True),
        _str_field("storage.postgres.host", "storage", "postgres.host", "Postgres host", "Postgres connection host.", editable=False),
        _int_field("storage.postgres.port", "storage", "postgres.port", "Postgres port", "Postgres connection port.", editable=False),
        _str_field("storage.postgres.database", "storage", "postgres.database", "Postgres database", "Postgres database name.", editable=False),
        _str_field("storage.postgres.user", "storage", "postgres.user", "Postgres user", "Postgres connection user.", editable=False),
        _str_field("storage.postgres.password", "storage", "postgres.password", "Postgres password", "Postgres connection password.", editable=False, sensitive=True),
        _int_field("storage.postgres.min_pool_size", "storage", "postgres.min_pool_size", "Postgres min pool size", "Minimum size of the Postgres connection pool.", editable=False),
        _int_field("storage.postgres.max_pool_size", "storage", "postgres.max_pool_size", "Postgres max pool size", "Maximum size of the Postgres connection pool.", editable=False),
        # -- federation: editable tuning knobs --
        _float_field("federation.request_timeout", "federation", "request_timeout", "Request timeout (seconds)", "Timeout for outbound ActivityPub HTTP requests.", minimum=0.1),
        _int_field("federation.actor_key_cache_ttl", "federation", "actor_key_cache_ttl", "Actor key cache TTL (seconds)", "How long a fetched remote actor's public key is cached before being re-fetched.", minimum=0),
        _int_field("federation.max_clock_skew", "federation", "max_clock_skew", "Max clock skew (seconds)", "Maximum allowed clock skew when verifying signed federation requests.", minimum=0),
        _int_field("federation.max_backdate_days", "federation", "max_backdate_days", "Max backdate (days)", "How far in the past a mirrored post's own published time is trusted before falling back to \"now\" -- mainly a safety margin against a homeserver retention policy silently dropping old-timestamped events.", minimum=0),
        # -- logging --
        _enum_field("logging.level", "logging", "level", "Log level", "The bridge's own log verbosity.", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
        # -- encrypted_media --
        _list_str_field("encrypted_media.bridges", "encrypted_media", "bridges", "Encrypted media sibling bridges", "Bot MXIDs of sibling bridges on this server sharing encrypted-attachment confirmation, in the order they're asked to confirm.", container=tuple),
        _float_field("encrypted_media.wait_seconds", "encrypted_media", "wait_seconds", "Encrypted media wait (seconds)", "How long to wait for a sibling bridge's confirmation before this bridge asks on its own.", minimum=0),
    )
}


def known_settings() -> list[BridgeSettingDefinition]:
    """Every registered setting, in registry order."""
    return list(_REGISTRY.values())


def value_of(config: BridgeConfig, definition: BridgeSettingDefinition) -> Any:
    """``definition``'s current value in ``config``. Never call this for a
    ``sensitive`` definition when building a response that leaves this
    process -- callers are responsible for that check, not this function."""
    return _get_value(config, definition)


def build_effective_config(base_config: BridgeConfig, overrides: dict[str, str]) -> BridgeConfig:
    """``base_config`` (exactly what config.yaml says) with every editable
    override folded in. An override for an unknown or non-editable key is
    skipped with a warning -- a stale DB row from a since-removed/disabled
    setting must never silently apply."""
    config = base_config
    for key, raw in overrides.items():
        definition = _REGISTRY.get(key)
        if definition is None:
            logger.warning("Ignoring stored bridge setting override for unknown key %r.", key)
            continue
        if not definition.editable:
            logger.warning("Ignoring stored bridge setting override for non-editable key %r.", key)
            continue
        try:
            value = definition.deserialize(raw)
        except (ValueError, TypeError):
            logger.warning("Ignoring unparseable stored bridge setting override for %r: %r", key, raw)
            continue
        config = _with_value(config, definition, value)
    return config


async def effective_config(base_config: BridgeConfig, repository: ActorRepository) -> BridgeConfig:
    """``base_config`` with every stored override folded in -- unless
    ``bridge.allow_db_setting_overrides`` is off in that same raw config,
    in which case this returns ``base_config`` unchanged without even
    reading the overrides table. Call once at startup and again after
    every settings mutation."""
    if not base_config.bridge.allow_db_setting_overrides:
        return base_config
    overrides = await repository.get_bridge_settings()
    return build_effective_config(base_config, overrides)


async def apply_override(repository: ActorRepository, key: str, raw_value: Any) -> Any:
    """Validate and store one setting override. Returns the stored
    (native) value. Raises ``SettingError`` for an unknown key, a
    non-editable key, or a value that fails to parse. Callers are
    responsible for checking ``allow_db_setting_overrides`` first -- this
    function does not re-check it."""
    definition = _REGISTRY.get(key)
    if definition is None:
        raise SettingError(f'Unknown setting "{key}".')
    if not definition.editable:
        raise SettingError(f'"{key}" can only be changed in config.yaml.')
    value = definition.parse(raw_value)
    await repository.set_bridge_setting(key, definition.serialize(value))
    return value


async def clear_override(repository: ActorRepository, key: str) -> None:
    """Remove one stored override, reverting ``key`` to its config.yaml
    value. No-op if no override was stored. Raises ``SettingError`` for an
    unknown or non-editable key. Callers are responsible for checking
    ``allow_db_setting_overrides`` first."""
    definition = _REGISTRY.get(key)
    if definition is None:
        raise SettingError(f'Unknown setting "{key}".')
    if not definition.editable:
        raise SettingError(f'"{key}" can only be changed in config.yaml.')
    await repository.clear_bridge_setting(key)


def apply_runtime_settings(state: Any, config: BridgeConfig) -> None:
    """Push the settings that long-lived objects captured at startup into
    those objects, so a database override takes effect at startup and on
    every later change: the outbound HTTP timeout, the actor key cache TTL
    and the log level."""
    timeout = config.federation.request_timeout
    http_client = getattr(state, "http_client", None)
    if http_client is not None:
        http_client.timeout = httpx.Timeout(timeout)
    synapse = getattr(state, "synapse", None)
    if synapse is not None:
        synapse.set_timeout(timeout)
    key_cache = getattr(state, "key_cache", None)
    if key_cache is not None:
        key_cache.set_ttl(config.federation.actor_key_cache_ttl)
    logging.getLogger().setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
