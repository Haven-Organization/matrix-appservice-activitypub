"""The bridge's single, global "backstage" Matrix space (``m.space``) that
every ghost is a member of once ``bridge.backstage_space_enabled`` is on.

Every eligible room's ``m.room.join_rules`` is pointed at this space via a
``restricted``/``knock_restricted`` ``allow`` condition (MSC3083/MSC3787), so
a ghost that's already a member of the space can self-join that room with a
single ``join`` event instead of costing an ``invite`` + ``join`` pair every
time. "Eligible" is deliberately narrow: Remote User Rooms, local Profile
Rooms, Group rooms, and Lemmy community rooms, never DM, Chat, or
Notification rooms, since a space-restricted allow-list is "anyone already in
the space may self-join," which would be a real privacy regression for a
room meant for two specific parties.

Every function here takes the same raw ``synapse``/``repository``/``config``
triple (not a ``Request``), matching ``bridge.ghosts``'s own convention,
since that's the module with the one hook (``ensure_ghost_user``) that has
no ``Request`` available at all. Callers that do have a ``Request`` just
pass ``request.app.state.synapse`` etc.

Entry points, used from different places:

- ``room_creation_join_kwargs``: for a BRAND NEW room, computes the right
  ``create_room(...)`` kwargs so the room is born with the space-restriction
  already in place (no separate upgrade step needed; the bridge always has
  full power on its own freshly-created rooms).
- ``maybe_upgrade_join_rule``: for an EXISTING room (one being swept by
  the one-off backfill script, or a room linked in via ``;link profile``/
  ``;link community``/``;link group`` that the bridge didn't create),
  best-effort adds the same restriction on top of whatever the room's
  CURRENT ``join_rules`` already are, read live. Silently does nothing if
  the bridge lacks the power to change it, the room just keeps working
  via invite, exactly as it does today.
- ``maybe_fast_join_ghost``: the cheap primitive: tries a plain self-join
  for a ghost into a room, nothing else. Returns ``False`` on anything but
  success, most commonly because the room was never upgraded or the ghost
  isn't yet a backstage-space member.
- ``ensure_ghost_access``: what an actual ghost-invite call site should
  call: tries ``maybe_fast_join_ghost`` first, and if that fails, does
  *right then, lazily* what a backfill would have done for this one
  room/ghost pair (joins the ghost to the space, upgrades the room's
  ``join_rules``) and retries once. This is deliberately how a deployment
  that flips the feature on but never runs the one-off backfill script
  still ends up fully converted over time, every room a ghost would
  otherwise have been invited into gets upgraded the first time that
  would happen, in place of the invite, not just at creation or at
  ``;link`` time. Returns ``False`` only when even the retry is rejected
  (e.g. the bridge genuinely has no power over this particular room, most
  commonly a ``;link``-ed room its mods never granted that to), the
  caller's ordinary invite flow is the correct, unchanged fallback then,
  exactly as it is today.

A ghost becomes a backstage-space member for the first time in
``ensure_ghost_user``/``sync_ghost_profile`` (``bridge/ghosts.py``), well
before its first room-join attempt, ``ensure_ghost_access``'s own
membership check is a defensive, idempotent repeat of that, not the
primary path.
"""

from __future__ import annotations

import logging

from bridge.repository import ActorRepository
from bridge.synapse_client import SynapseClient, SynapseError

logger = logging.getLogger(__name__)

BACKSTAGE_SPACE_NAME = "Bridge Ghosts"
_SPACE_ROOM_TYPE = "m.space"
_RESTRICTABLE_JOIN_RULES = ("restricted", "knock_restricted")


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def ensure_backstage_space(synapse: SynapseClient, repository: ActorRepository, config) -> str | None:
    """Get-or-create the bridge's one backstage space. Returns its room ID,
    or None if creation failed, best-effort, same as ``bridge.spaces
    .ensure_user_space``. Callers must still check
    ``config.bridge.backstage_space_enabled`` themselves; this function
    doesn't gate on it so it stays usable from a one-off backfill script
    even before the flag is flipped on."""
    existing = await repository.get_backstage_space()
    if existing is not None:
        return existing

    try:
        space_room_id = await synapse.create_room(
            as_user_id=_bot_mxid(config),
            name=BACKSTAGE_SPACE_NAME,
            avatar_mxc=config.appservice.bot_avatar_mxc,
            room_type=_SPACE_ROOM_TYPE,
        )
    except SynapseError:
        logger.warning("Could not create backstage space", exc_info=True)
        return None

    await repository.register_backstage_space(space_room_id)
    return space_room_id


async def open_backstage_space_publicly(synapse: SynapseClient, config, space_room_id: str) -> str:
    """Temporarily set the backstage space's own ``join_rule`` to ``public``,
    returning whatever it was before (to restore via
    ``close_backstage_space`` once done). Used ONLY by the one-off backfill
    script's ``--public-join`` option: with the space public, every ghost
    can self-join it with a single ``join`` event instead of costing an
    ``invite`` + ``join`` pair (or depending on
    ``bridge.use_synapse_admin_api``) one ghost at a time, a real win when
    joining thousands of ghosts in one pass, not worth it for the ordinary
    one-ghost-at-a-time runtime path (``join_ghost_to_backstage_space``),
    which never calls this.

    This briefly exposes the space to anyone on this (or a federating)
    homeserver who learns its room ID, membership in it grants direct
    join access to every room that trusts it, exactly the same consequence
    the admin widget's own "Get invited" button warns about, so the
    backfill script's own public-join window is kept as short as possible
    (just the ghost-joining phase, not the whole room-upgrade sweep) and
    always closed again in a ``finally``, even if something in between
    raises."""
    current = await synapse.get_room_state(space_room_id, "m.room.join_rules", "")
    previous_join_rule = current.get("join_rule") or "invite"
    await synapse.send_state_event(
        space_room_id, "m.room.join_rules", "", {"join_rule": "public"}, as_user_id=_bot_mxid(config),
    )
    return previous_join_rule


async def close_backstage_space(synapse: SynapseClient, config, space_room_id: str, join_rule: str) -> None:
    """Restore the backstage space's ``join_rule`` to ``join_rule`` (what
    ``open_backstage_space_publicly`` reported it was before), locks it
    back down at the end of a ``--public-join`` backfill run."""
    await synapse.send_state_event(
        space_room_id, "m.room.join_rules", "", {"join_rule": join_rule}, as_user_id=_bot_mxid(config),
    )


async def join_ghost_to_backstage_space(
    synapse: SynapseClient, repository: ActorRepository, config, ghost_mxid: str,
) -> None:
    """Make ``ghost_mxid`` a member of the backstage space. No-op if the
    feature is off. Uses the Admin API force-join (no invite round-trip at
    all) when ``bridge.use_synapse_admin_api`` is on; otherwise falls back
    to an ordinary bot-invite + ghost-join, same 2-event cost as today's
    per-room invite, just paid once per ghost instead of once per room.
    Idempotent, joining an already-joined room is a harmless no-op, so
    this is safe to call on every ghost provisioning, not just the first."""
    if not config.bridge.backstage_space_enabled:
        return
    space_room_id = await ensure_backstage_space(synapse, repository, config)
    if space_room_id is None:
        return

    try:
        if config.bridge.use_synapse_admin_api:
            await synapse.admin_join_room(space_room_id, ghost_mxid)
        else:
            await synapse.invite_user(space_room_id, ghost_mxid, as_user_id=_bot_mxid(config))
            await synapse.join_room(space_room_id, as_user_id=ghost_mxid)
    except SynapseError:
        logger.warning("Could not join ghost %s to backstage space", ghost_mxid, exc_info=True)


async def maybe_fast_join_ghost(synapse: SynapseClient, config, *, room_id: str, ghost_mxid: str) -> bool:
    """Try to self-join ``ghost_mxid`` into ``room_id`` directly, skipping
    the invite. Returns True on success (caller should NOT also invite).
    Returns False if the feature is off, or the join was rejected
    (``M_FORBIDDEN``, the room was never upgraded, or the ghost isn't yet
    a backstage-space member), the caller's existing invite flow is the
    correct fallback either way, with no extra bookkeeping needed to know
    which case applies. Any other error re-raises, same as every other
    "swallow one specific expected errcode" call in this codebase."""
    if not config.bridge.backstage_space_enabled:
        return False
    try:
        await synapse.join_room(room_id, as_user_id=ghost_mxid)
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            raise
        return False
    return True


async def maybe_upgrade_join_rule(synapse: SynapseClient, repository: ActorRepository, config, room_id: str) -> bool:
    """Best-effort: add the backstage-space ``allow`` condition to
    ``room_id``'s CURRENT ``join_rules``, read live, not assumed,
    (``invite``->``restricted``, ``knock``->``knock_restricted``, or append
    to an existing ``restricted``/``knock_restricted`` condition list
    without disturbing whatever else is already there, e.g. a Group
    room's own guild-space restriction). Never touches an already-
    ``public``/``public_chat`` room. Silently does nothing on a permission
    error, the room just keeps working exactly as it does today, which is
    the explicit "bridge was never granted ``m.room.join_rules`` power
    here" fallback the user asked for, relevant both for a room linked in
    via ``;link profile``/``;link community``/``;link group`` that the
    bridge didn't create, and for the lazy on-demand upgrade attempt in
    ``ensure_ghost_access`` below.

    Returns True if ``room_id`` ends this call restricted to the backstage
    space (already was, or was just upgraded), used by the one-off
    backfill script to report successes vs. permission-denied skips.
    Returns False otherwise (feature off, space missing, state unreadable,
    an already-public/unrecognized room left alone on purpose, or a
    permission denial), which every other caller is free to ignore."""
    if not config.bridge.backstage_space_enabled:
        return False
    space_room_id = await ensure_backstage_space(synapse, repository, config)
    if space_room_id is None:
        return False

    try:
        current = await synapse.get_room_state(room_id, "m.room.join_rules", "")
    except SynapseError:
        logger.debug("Could not read join_rules for %s", room_id, exc_info=True)
        return False

    condition = {"type": "m.room_membership", "room_id": space_room_id}
    current_rule = current.get("join_rule")
    if current_rule in _RESTRICTABLE_JOIN_RULES:
        allow = list(current.get("allow") or [])
        if condition in allow:
            return True
        allow.append(condition)
        new_rule = current_rule
    elif current_rule == "invite":
        new_rule, allow = "restricted", [condition]
    elif current_rule == "knock":
        new_rule, allow = "knock_restricted", [condition]
    else:
        return False  # public/public_chat/unrecognized, never tighten or guess

    try:
        await synapse.send_state_event(
            room_id, "m.room.join_rules", "", {"join_rule": new_rule, "allow": allow},
            as_user_id=_bot_mxid(config),
        )
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            raise
        logger.info(
            "No permission to add the backstage-space restriction to %s, leaving join_rules as-is", room_id,
        )
        return False
    return True


async def ensure_ghost_access(
    synapse: SynapseClient, repository: ActorRepository, config, *, room_id: str, ghost_mxid: str,
) -> bool:
    """The one thing a ghost-invite call site actually needs: try the fast
    self-join first; if that fails because this particular room was never
    upgraded (or flipping the feature on happened without a backfill ever
    running), opportunistically do right now, lazily, exactly what a
    backfill would have done for this one room/ghost pair, add the room
    to the backstage space's allow-list and make sure the ghost is a
    space member, then retry the self-join once. Returns True if the
    ghost ended up joined (caller must NOT also invite); returns False if
    even that retry was rejected (e.g. the bridge genuinely has no power
    over this room's ``join_rules``, such as a ``;link``-ed room mods never
    granted it), in which case the caller's existing invite fallback is
    still exactly correct and still runs. This is how the backstage space
    fills in gradually over time, room by room and ghost by ghost, for a
    deployment that enables the feature but never runs the one-off backfill
    script at all."""
    if not config.bridge.backstage_space_enabled:
        return False
    if await maybe_fast_join_ghost(synapse, config, room_id=room_id, ghost_mxid=ghost_mxid):
        return True
    await join_ghost_to_backstage_space(synapse, repository, config, ghost_mxid)
    await maybe_upgrade_join_rule(synapse, repository, config, room_id)
    return await maybe_fast_join_ghost(synapse, config, room_id=room_id, ghost_mxid=ghost_mxid)


async def room_creation_join_kwargs(
    synapse: SynapseClient, repository: ActorRepository, config, *, base_join_rule: str,
) -> dict[str, object]:
    """``create_room(...)`` kwargs for a brand new room of ``base_join_rule``
    ("knock"/"invite"/"public"), already including the backstage-space
    restriction when the feature is on. Returns plain ``{"join_rule":
    base_join_rule}`` (today's behavior) when the feature is off, the base
    rule is "public" (nothing to gain by restricting an open room), or the
    space couldn't be created."""
    if not config.bridge.backstage_space_enabled or base_join_rule == "public":
        return {"join_rule": base_join_rule}
    space_room_id = await ensure_backstage_space(synapse, repository, config)
    if space_room_id is None:
        return {"join_rule": base_join_rule}
    return {"restricted_to_room_id": space_room_id, "knock_restricted": base_join_rule == "knock"}
