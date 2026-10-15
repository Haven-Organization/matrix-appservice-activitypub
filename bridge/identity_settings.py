"""Per-ActivityPub-identity preference settings (the ``;set`` command).

Replaces the old pattern of one dedicated ``ActorRecord`` column + one
dedicated repository setter + one dedicated command per toggle (that's how
``hide_followers``/``hide_following`` used to work) with a single generic
mechanism: a small registry of known settings here, backed by
``ActorRepository``'s ``identity_settings`` table (``get_identity_settings``/
``set_identity_setting``/``clear_identity_setting``).

Settings are scoped per ActivityPub identity, not per Matrix user. A
Matrix user's own profile and any PeerTube channels they own are each a
distinct identity (distinct ``username``, see ``PeerTubeChannelRecord``'s
docstring on the shared global username namespace) and can each carry their
own overrides. Storage is sparse: a row only ever exists for a setting that
differs from its code default here; the registry's default IS the value for
every identity that has never touched it, not something copied into a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bridge.repository import ActorRepository

IdentityKind = str  # "profile" or "channel"


class SettingError(Exception):
    """Raised for an unknown key, a key that doesn't apply to this identity
    kind, or a value that fails to parse. Callers turn this straight into
    a ``_notice`` back to the room."""


def _parse_bool(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in ("on", "true", "yes", "1"):
        return True
    if normalized in ("off", "false", "no", "0"):
        return False
    raise SettingError(f'"{raw}" isn\'t a valid value, use "on" or "off".')


def _format_bool(value: bool) -> str:
    return "on" if value else "off"


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    default: Any
    description: str
    applies_to: frozenset[IdentityKind]
    parse: Callable[[str], Any]  # user-typed ";set <key> <this>" -> a value
    format: Callable[[Any], str]  # a value -> how ";set"/";set list" displays it
    serialize: Callable[[Any], str]  # a value -> how it's stored in identity_settings
    deserialize: Callable[[str], Any]  # a stored override -> a value


def _bool_setting(key: str, *, default: bool, description: str, applies_to: frozenset[IdentityKind]) -> SettingDefinition:
    return SettingDefinition(
        key=key, default=default, description=description, applies_to=applies_to,
        parse=_parse_bool, format=_format_bool,
        serialize=lambda value: "true" if value else "false",
        deserialize=lambda raw: raw == "true",
    )


_PROFILE_ONLY = frozenset({"profile"})

# Ordered so ";set"/";set list" has a stable, sensible display order.
_REGISTRY: dict[str, SettingDefinition] = {
    definition.key: definition
    for definition in (
        _bool_setting(
            "hide_followers", default=False, applies_to=_PROFILE_ONLY,
            description=(
                "Hide your public followers list from remote viewers. Your follower COUNT stays public "
                "either way, same as Mastodon's own \"hide network\" setting; only the member list "
                "itself is withheld."
            ),
        ),
        _bool_setting(
            "hide_following", default=False, applies_to=_PROFILE_ONLY,
            description="Same as hide_followers, for your public following list.",
        ),
        _bool_setting(
            "discoverable", default=True, applies_to=_PROFILE_ONLY,
            description=(
                "Whether other instances should include you in directories/search (the toot:discoverable "
                "AS2 extension). A PeerTube channel is always discoverable, since the whole feature only "
                "ever federates public, findable content, so this never applies there."
            ),
        ),
    )
}


def known_settings(identity_kind: IdentityKind) -> list[SettingDefinition]:
    """Every registered setting that applies to ``identity_kind``, in
    registry order."""
    return [definition for definition in _REGISTRY.values() if identity_kind in definition.applies_to]


async def get_effective_settings(
    repository: ActorRepository, username: str, identity_kind: IdentityKind
) -> dict[str, Any]:
    """Every setting that applies to this identity, each resolved to its
    stored override if one exists, else the registry default."""
    overrides = await repository.get_identity_settings(username)
    result: dict[str, Any] = {}
    for definition in known_settings(identity_kind):
        raw = overrides.get(definition.key)
        result[definition.key] = definition.deserialize(raw) if raw is not None else definition.default
    return result


async def get_effective_setting(
    repository: ActorRepository, username: str, identity_kind: IdentityKind, key: str
) -> Any:
    definition = _REGISTRY.get(key)
    if definition is None or identity_kind not in definition.applies_to:
        raise SettingError(f'Unknown setting "{key}".')
    overrides = await repository.get_identity_settings(username)
    raw = overrides.get(key)
    return definition.deserialize(raw) if raw is not None else definition.default


async def apply_set(
    repository: ActorRepository, username: str, identity_kind: IdentityKind, key: str, raw_value: str
) -> str:
    """Parse and store (or clear, if it now matches the default) one
    setting override. Returns a human-readable confirmation line. Raises
    ``SettingError`` for an unknown key, a key that doesn't apply to this
    identity kind, or a value that fails to parse."""
    definition = _REGISTRY.get(key)
    if definition is None:
        raise SettingError(f'Unknown setting "{key}". Run ";set list" to see what\'s available.')
    if identity_kind not in definition.applies_to:
        raise SettingError(f'"{key}" doesn\'t apply to a {identity_kind} identity.')

    value = definition.parse(raw_value)
    if value == definition.default:
        await repository.clear_identity_setting(username, key)
    else:
        await repository.set_identity_setting(username, key, definition.serialize(value))
    return f"{key} is now {definition.format(value)}."


async def reset_to_default(
    repository: ActorRepository, username: str, identity_kind: IdentityKind, key: str
) -> str:
    definition = _REGISTRY.get(key)
    if definition is None or identity_kind not in definition.applies_to:
        raise SettingError(f'Unknown setting "{key}".')
    await repository.clear_identity_setting(username, key)
    return f"{key} reset to its default ({definition.format(definition.default)})."
