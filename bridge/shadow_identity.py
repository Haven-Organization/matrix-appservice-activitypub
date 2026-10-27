"""Shadow-identity lifecycle: tier resolution and universal auto-provisioning.

A "shadow" identity is an ``ActorRecord`` with an empty ``room_id`` -- no
self-controlled Profile Room, display name/avatar always mirroring the
owning Matrix user's live profile, no self-service profile commands. A
"full" identity is the opposite: a real Profile Room, static display
fields the owner controls, full command access. Both first- and
third-party users go through a shadow phase; a first-party user graduates
to full the moment they run ``;create profile``/``;link profile`` (see
``bridge.commands._handle_create_profile``'s reattach logic, which this
module's ``default_actor_username`` makes work correctly for third parties
too, matching what their shadow identity was already minted under); a
third-party user's effective tier can also be revoked/downgraded live by an
admin at any time via ``;allow``/``;disallow`` (see ``effective_identity_tier``).

Before this module existed, shadow-equivalent auto-provisioning only
existed for third parties, and only on ``;follow``
(``bridge.commands._auto_provision_third_party_actor``) -- every other
outbound chokepoint (chat, reply, reaction, poll, channel/community/group
posting) instead rejected ANY sender with no existing identity, first- or
third-party alike, with a "link a profile first" notice.
``ensure_shadow_actor`` is the single replacement for all of that: every
one of those call sites now auto-provisions a shadow identity instead of
rejecting, for both first- and third-party senders (gated appropriately
for third parties -- see its own docstring).

A third party's eligibility (see ``ensure_shadow_actor``/
``is_third_party_still_allowed``) can come from an admin-granted ``mxid``
or ``homeserver`` allow, OR from a ``room``-scoped allow on whichever
already-bridged room the action is actually happening in -- letting
anyone federate passive content out of one specific room without having
to vet each sender or their homeserver individually. A room grant is
always "shadow" tier (never "full" -- there's no sense in which being in
a room lets you self-provision a profile), is re-checked per-action
against the CURRENT room every time (never cached, never carried over to
any other room), and deliberately does NOT satisfy the command-dispatch
gate (``bridge.commands.maybe_handle_command``) -- see
``ThirdPartyAllowRecord``'s docstring for why commands stay mxid/homeserver
-only even in a room-allowed room.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, NamedTuple

from bridge.activitypub.urls import media_url
from bridge.crypto import generate_keypair
from bridge.ghosts import sanitize_localpart_component, third_party_username_from_mxid
from bridge.identity_resolution import resolve_acting_identity
from bridge.repository import ActorRecord
from bridge.synapse_client import SynapseError

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


def homeserver_of(mxid: str) -> str:
    """The domain half of a Matrix user ID (``@user:example.org`` ->
    ``example.org``), or ``""`` if ``mxid`` has no ``:`` at all. The one
    shared helper for an idiom that used to be repeated inline at several
    call sites in ``bridge.commands``/``bridge.membership``."""
    return mxid.split(":", 1)[1] if ":" in mxid else ""


def default_actor_username(mxid: str, *, is_third_party: bool) -> str:
    """The default AP ``username`` for ``mxid`` -- the SAME rule used both
    to mint a shadow identity (``ensure_shadow_actor`` below) and to decide
    what ``;create profile``'s bare/no-custom-id form should use
    (``bridge.commands._handle_create_profile``). Keeping both call sites
    on this one function is what makes "graduating" a shadow identity in
    place actually work: ``_handle_create_profile``'s existing reattach
    logic finds an ``existing`` identity to upgrade by matching on
    ``username`` alone, which only succeeds if the username it's about to
    use is spelled identically to whatever a shadow identity for the same
    ``mxid`` would already have been minted under.

    First-party (this bridge's own homeserver): just the sanitized Matrix
    localpart -- collisions are impossible within our own homeserver's
    namespace, which is already unique. Third-party: encodes BOTH
    localpart and origin homeserver domain
    (``third_party_username_from_mxid``), since the same localpart can
    exist on many different homeservers and all of them share this
    bridge's one global username namespace."""
    if is_third_party:
        return third_party_username_from_mxid(mxid)
    localpart = mxid.lstrip("@").split(":", 1)[0]
    return sanitize_localpart_component(localpart)


async def resolve_third_party_tier(repository, *, mxid: str, homeserver: str, room_id: str = "") -> str | None:
    """Thin pass-through to ``ActorRepository.resolve_third_party_tier`` --
    kept here, not called directly, so every caller goes through one
    module for this feature rather than reaching into the repository
    layer by hand. ``room_id`` defaults to ``""`` (never consult a room
    grant) -- pass the acting room explicitly wherever a room-level grant
    should actually be able to apply (see that method's own docstring)."""
    return await repository.resolve_third_party_tier(mxid=mxid, homeserver=homeserver, room_id=room_id)


async def effective_identity_tier(request: "Request", record: ActorRecord) -> Literal["full", "shadow"]:
    """The CURRENT tier ``record`` behaves as, right now -- live, never
    cached (see ``ActorRecord.is_third_party``'s docstring for why).
    Replaces the old, third-party-only ``_effective_third_party_mode``.

    - No Profile Room at all yet (``room_id == ""``): always "shadow",
      first- or third-party alike -- there's no static profile data to
      read even if an admin has granted "full" tier (full tier is only
      ever permission to self-provision one via ``;create profile``, not
      an automatic grant of one -- see this module's own docstring).
    - A first-party identity WITH a Profile Room: always "full" -- there's
      no allow-list concept for first-party users at all; making their own
      profile is unconditionally allowed.
    - A third-party identity WITH a Profile Room: "full" only if
      ``bridge.third_party_access`` is currently on AND their current
      ``;allow`` grant still resolves to "full" -- otherwise "shadow",
      even though the Profile Room still exists. This is the demotion
      path: a parked, previously-full identity whose grant is downgraded,
      banned, or whose homeserver's grant is removed, falls back to live
      Matrix data the next time anything reads this, with its Profile Room
      left untouched in case it's ever restored.
    - The bridge's own service actor (``bridge.service_actor``) ALSO has
      an empty ``room_id`` (it isn't tied to any one room, by design, not
      because nobody's gotten around to ``;create profile`` for it) --
      excluded from the first rule above by its ``matrix_user_id`` always
      being the bot's own mxid, so it's served from its stored static
      fields like any other full identity, never live-Matrix-mirrored."""
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if record.matrix_user_id == bot_mxid:
        return "full"
    if not record.room_id:
        return "shadow"
    if not record.is_third_party:
        return "full"
    if not config.bridge.third_party_access:
        return "shadow"
    repository = request.app.state.repository
    tier = await repository.resolve_third_party_tier(
        mxid=record.matrix_user_id, homeserver=homeserver_of(record.matrix_user_id), room_id="",
    )
    return "full" if tier == "full" else "shadow"


async def is_third_party_still_allowed(request: "Request", record: ActorRecord, *, room_id: str) -> bool:
    """Whether ``record`` is still allowed to federate AT ALL right now, IN
    ``room_id`` specifically -- not just which tier it gets (see
    ``effective_identity_tier`` for that). A local (first-party)
    ``ActorRecord`` existing is always sufficient. A third-party
    identity's continued right to federate ANYTHING -- reactions, replies,
    DMs, Chat, post distribution, not just new commands -- depends on
    ``bridge.third_party_access`` being on AND their current
    ``;allow``/``;disallow`` grant (mxid, homeserver, OR a room-level
    grant for ``room_id`` specifically) still resolving to "full" or
    "shadow" (not banned/absent) -- revoking a grant deliberately never
    tears down an already-provisioned identity (see
    ``ActorRepository.remove_third_party_allow``'s docstring), so this has
    to be re-checked live at every point that resolves an acting
    ``ActorRecord``, not just once. Getting in via one room's grant gives
    NO standing in any other room -- always pass the room the ACTUAL
    action is happening in, not wherever the identity was first minted;
    treat ``False`` the same as "no linked profile" (drop the action
    silently)."""
    if not record.is_third_party:
        return True
    config = request.app.state.config
    if not config.bridge.third_party_access:
        return False
    repository = request.app.state.repository
    tier = await repository.resolve_third_party_tier(
        mxid=record.matrix_user_id, homeserver=homeserver_of(record.matrix_user_id), room_id=room_id,
    )
    return tier is not None


class ShadowResolution(NamedTuple):
    """Result of ``ensure_shadow_actor``. ``created`` is True only when this
    call just minted a brand new identity (never for one that already
    existed, from this room or any other) -- callers that want to tell the
    user about their new shadow identity check this flag; everyone else
    ignores it."""

    record: ActorRecord | None
    created: bool


async def ensure_shadow_actor(request: "Request", *, sender: str, room_id: str) -> ShadowResolution:
    """The universal "who is acting, minting a shadow identity if nobody
    is yet" resolver. Every outbound chokepoint that used to call
    ``repository.get_local_actor_by_matrix_id(sender)`` (or
    ``resolve_acting_identity`` directly) and reject on ``None`` should
    call this instead.

    1. ``resolve_acting_identity`` first -- an existing identity bound to
       this room, an owned channel/community/group, a ghost DM/Chat
       binding, or the sender's Active identity from elsewhere. Found ->
       ``(record, created=False)``.
    2. Otherwise, decide eligibility to auto-provision a NEW shadow
       identity: a first-party sender (their homeserver IS this bridge's
       own) is always eligible. A third-party sender is eligible only if
       ``bridge.third_party_access`` is on AND their mxid/homeserver/
       room-level grant for THIS ``room_id`` currently resolves to "full"
       or "shadow" (not banned/absent) -- ``(None, created=False)``
       otherwise, so the caller can send the right notice (disabled vs.
       not-allowed; see ``bridge.commands``'s existing message styles for
       either case). The room-level fallback here is exactly as safe for
       ``;follow`` (also a caller of this function, via
       ``bridge.commands._handle_follow``) as for every passive chokepoint:
       a sender with ONLY a room grant never reaches that handler's body
       at all, since ``maybe_handle_command``'s own dispatch gate rejects
       them first using a room-blind check (``room_id=""``) -- see
       ``ThirdPartyAllowRecord``'s docstring for why that's deliberate.
    3. If eligible, mint+register a fresh ``ActorRecord`` with
       ``room_id=""``, seeding ``display_name``/``icon_url`` from a live
       Matrix profile fetch (one-time seed, not kept live by this
       function -- see ``bridge.third_party_sync`` for what keeps a shadow
       identity's profile live afterward) -> ``(record, created=True)``.
    """
    existing = await resolve_acting_identity(request, sender=sender, room_id=room_id)
    if existing is not None:
        return ShadowResolution(existing, False)

    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse

    homeserver = homeserver_of(sender)
    is_third_party = homeserver != config.synapse.server_name
    if is_third_party:
        if not config.bridge.third_party_access:
            return ShadowResolution(None, False)
        tier = await repository.resolve_third_party_tier(mxid=sender, homeserver=homeserver, room_id=room_id)
        if tier is None:
            return ShadowResolution(None, False)

    # Collision-proof username pick -- register_local_actor is a pure
    # upsert keyed on username with no uniqueness check, so a collision
    # with an already-registered DIFFERENT matrix_user_id (vanishingly
    # unlikely, not impossible) falls back to a numeric suffix rather than
    # silently hijacking the existing identity. Mirrors the collision loop
    # the old, third-party-only _auto_provision_third_party_actor had.
    base_username = default_actor_username(sender, is_third_party=is_third_party)
    username = base_username
    suffix = 2
    while True:
        existing_owner = await repository.get_local_actor(username)
        if existing_owner is None or existing_owner.matrix_user_id == sender:
            break
        username = f"{base_username}_{suffix}"
        suffix += 1

    try:
        profile = await synapse.get_profile(sender)
    except SynapseError:
        profile = {}
    matrix_display_name = profile.get("displayname")
    matrix_avatar_mxc = profile.get("avatar_url")
    display_name = matrix_display_name or sender
    icon_url = None
    if matrix_avatar_mxc:
        try:
            icon_url = media_url(config.bridge.public_base_url, matrix_avatar_mxc)
        except ValueError:
            icon_url = None
        else:
            await repository.mark_media_published(matrix_avatar_mxc)

    private_key_pem, public_key_pem = generate_keypair()
    record = ActorRecord(
        username=username,
        matrix_user_id=sender,
        room_id="",
        public_key_pem=public_key_pem,
        private_key_pem=private_key_pem,
        display_name=display_name,
        summary=f"{sender} is a Matrix user.",
        icon_url=icon_url,
        is_third_party=is_third_party,
    )
    await repository.register_local_actor(record)
    return ShadowResolution(record, True)


def shadow_created_notice(request: "Request", record: ActorRecord) -> str:
    """The one-time notice sent the moment ``ensure_shadow_actor`` actually
    creates a new identity (``ShadowResolution.created`` True) -- fires
    uniformly everywhere that happens, not just on ``;follow``, since
    every chokepoint now goes through the same function."""
    config = request.app.state.config
    prefix = config.bridge.command_prefix
    return (
        f"I've created a shadow fediverse identity for you: @{record.username}@{config.bridge.domain}. "
        "It mirrors your Matrix display name/avatar automatically, and has no profile of its own yet. "
        f'Run "{prefix}create profile" anytime to turn it into a full, customizable profile.'
    )
