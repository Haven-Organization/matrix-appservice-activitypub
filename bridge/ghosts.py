"""Ghost (virtual Matrix user) identity helpers for remote ActivityPub actors.

Every remote fediverse account the bridge mirrors gets a ghosted Matrix user
under the AppService's reserved namespace (``appservice.user_prefix`` in
config), e.g. ``user@instance.org`` -> ``@ap_user_instance.org:bridge.domain``.
"""

from __future__ import annotations

import logging
import re

import httpx

from bridge.backstage_space import join_ghost_to_backstage_space
from bridge.custom_emoji import (
    FORMATTED_DISPLAYNAME_FIELD,
    FORMATTED_ROOM_NAME_FIELD,
    build_formatted_displayname,
    resolve_and_persist_emoji,
    room_name_content,
)
from bridge.media import fetch_and_upload_media
from bridge.repository import ActorRepository, GhostProfile
from bridge.synapse_client import SynapseClient, SynapseError

logger = logging.getLogger(__name__)

_UNSAFE_CHARS_RE = re.compile(r"[^a-z0-9._=\-/]")


def sanitize_localpart_component(value: str) -> str:
    """Matrix localparts are restricted to a limited character set; lowercase
    and replace anything outside it with ``_``."""
    return _UNSAFE_CHARS_RE.sub("_", value.lower())


def ghost_localpart(user_prefix: str, actor_username: str, actor_domain: str) -> str:
    return (
        f"{user_prefix}"
        f"{sanitize_localpart_component(actor_username)}_{sanitize_localpart_component(actor_domain)}"
    )


def ghost_mxid(user_prefix: str, actor_username: str, actor_domain: str, server_name: str) -> str:
    return f"@{ghost_localpart(user_prefix, actor_username, actor_domain)}:{server_name}"


def third_party_username_from_mxid(mxid: str) -> str:
    """The AP ``username`` (see ``bridge.repository.ActorRecord.username``)
    minted for a third-party (different-homeserver) user's auto-provisioned
    identity -- see ``bridge.commands``'s allowlist/auto-provisioning logic.

    Unlike ``ghost_localpart`` (which just replaces any unsafe char with a
    single ``_``, fine there since a ghost's localpart is never parsed back
    apart), this must keep two DIFFERENT MXIDs from ever mangling to the
    SAME username -- ``:`` is the field separator, so any literal ``_``
    already present in either half is first escaped to ``__`` before joining
    with a single un-escaped ``_``, making the join collision-proof:
    ``@forgot_password:matrix.org`` -> ``forgot__password_matrix.org`` vs.
    ``@forgot:password_matrix.org`` -> ``forgot_password__matrix.org``.
    """
    localpart, domain = mxid.lstrip("@").split(":", 1)
    escaped_localpart = sanitize_localpart_component(localpart).replace("_", "__")
    escaped_domain = sanitize_localpart_component(domain).replace("_", "__")
    return f"{escaped_localpart}_{escaped_domain}"


async def ensure_ghost_user(
    synapse: SynapseClient,
    repository: ActorRepository,
    config,
    *,
    server_name: str,
    localpart: str,
    display_name: str | None = None,
    avatar_mxc: str | None = None,
) -> str:
    """Idempotently provision a ghosted Matrix user. Returns its MXID.

    Registration is attempted unconditionally and ``M_USER_IN_USE`` is treated
    as success (the ghost already exists from a previous follow) -- the AS
    registration flow has no separate "does this user exist" check.
    """
    mxid = f"@{localpart}:{server_name}"
    try:
        await synapse.register_appservice_user(localpart)
    except SynapseError as exc:
        if exc.errcode != "M_USER_IN_USE":
            raise

    if display_name:
        try:
            await synapse.set_display_name(mxid, display_name)
        except SynapseError:
            logger.debug("Could not set display name for ghost %s", mxid, exc_info=True)

    if avatar_mxc:
        try:
            await synapse.set_avatar_url(mxid, avatar_mxc)
        except SynapseError:
            logger.debug("Could not set avatar for ghost %s", mxid, exc_info=True)

    await join_ghost_to_backstage_space(synapse, repository, config, mxid)
    return mxid


async def sync_bot_profile(
    synapse: SynapseClient, *, server_name: str, localpart: str,
    display_name: str | None, avatar_mxc: str | None,
) -> None:
    """Push the bridge bot's own display name/avatar to its Matrix profile,
    if either differs from what's there now. ``None`` for either field
    means "don't manage this one" -- it's left exactly as it is, never
    cleared (matches ``AppserviceSection.bot_display_name``/
    ``bot_avatar_mxc``'s own documented semantics).

    Compare-first, not just a bare call to ``ensure_ghost_user``: a
    redundant PUT makes Synapse schedule an ``update_join_states`` task
    rewriting the bot's member event in every room it's in, and an avatar
    PUT right after a displayname one cancels that still-running task,
    tripping a real Synapse double-pop bug (a ``KeyError`` traceback,
    diagnosed live 2026-07-04). Originally only ever called once at
    startup for exactly that reason, but the compare-first guard itself
    has no startup-only state -- safe to call again any time either field
    changes live (see ``bridge.bridge_settings``), not just once per
    process lifetime.

    Doesn't delegate to ``ensure_ghost_user`` (even though the bot is also
    technically an AS-namespace user), this manages the BOT's own
    profile, not a ghost's, and has no ``repository``/``config`` to pass
    along; joining the bot itself to the backstage space would be
    meaningless (it already has unrestricted access everywhere) and
    ``ensure_ghost_user``'s signature requires both now.
    """
    if not display_name and not avatar_mxc:
        return
    mxid = f"@{localpart}:{server_name}"
    try:
        bot_profile = await synapse.get_profile(mxid)
    except SynapseError:
        bot_profile = {}
    if display_name and bot_profile.get("displayname") != display_name:
        try:
            await synapse.set_display_name(mxid, display_name)
        except SynapseError:
            logger.debug("Could not set display name for bot %s", mxid, exc_info=True)
    if avatar_mxc and bot_profile.get("avatar_url") != avatar_mxc:
        try:
            await synapse.set_avatar_url(mxid, avatar_mxc)
        except SynapseError:
            logger.debug("Could not set avatar for bot %s", mxid, exc_info=True)


async def _sync_room_avatars(
    synapse: SynapseClient, repository: ActorRepository, *, actor_id: str, mxid: str, avatar_mxc: str
) -> None:
    """Push a freshly-changed avatar out to every room this ghost's
    identity is reflected in as a room avatar too -- their Remote User
    Room, and every DM/Chat room they've got open with a local user (there
    can be more than one of each, one per local user who's started one
    with them) -- reusing ``avatar_mxc`` as-is rather than
    fetching/uploading the same file again per room. A failure in one room
    (e.g. the ghost somehow lost its power level there) doesn't stop the
    others from being updated."""
    room_ids: list[str] = []
    remote_actor_room = await repository.get_remote_actor_room(actor_id)
    if remote_actor_room is not None:
        room_ids.append(remote_actor_room.room_id)
    room_ids.extend(await repository.get_ghost_dm_room_ids_for_actor(actor_id))
    room_ids.extend(await repository.get_ghost_chat_room_ids_for_actor(actor_id))

    for room_id in room_ids:
        try:
            await synapse.send_state_event(room_id, "m.room.avatar", "", {"url": avatar_mxc}, as_user_id=mxid)
        except SynapseError:
            logger.debug("Could not update room avatar for %s in %s", actor_id, room_id, exc_info=True)


async def _sync_formatted_displayname_rooms(
    synapse: SynapseClient, repository: ActorRepository, *,
    actor_id: str, mxid: str, display_name: str, formatted_body: str | None,
) -> None:
    """Pushes ``FORMATTED_DISPLAYNAME_FIELD`` into every room this ghost is
    a member of, as a sibling of its own ``displayname`` in that room's
    ``m.room.member`` content -- per MSC4589, unlike every OTHER MSC4133
    custom profile field (see ``set_profile_field``), this one is NOT
    auto-replicated by the homeserver into member events the way plain
    ``displayname``/``avatar_url`` are, so it has to be pushed explicitly,
    room by room, same room set ``_sync_room_avatars`` already enumerates.

    Reads each room's CURRENT member content first and only adds/removes
    this one field -- never reconstructing ``membership``/``avatar_url``
    from scratch, so nothing else already on the member event is lost.
    ``formatted_body=None`` clears the field (the name no longer has any
    resolvable shortcode, or the feature was turned back off) rather than
    leaving a stale value whose own emoji no longer match ``displayname``.

    Enumerated via ``synapse.get_joined_rooms(mxid)`` -- the real,
    complete list of every room this ghost is actually in right now --
    rather than ``_sync_room_avatars``'s own room-category bookkeeping
    (Remote User Room + DM/Chat rooms only). Confirmed live 2026-11-27
    (Haven): a ghost is routinely also a member of OTHER rooms entirely
    (someone else's Remote User Room they replied in, a shared room, ...)
    that this bridge never created FOR them and so has no bookkeeping
    category for at all -- but per MSC4589 the field should travel
    everywhere ``displayname`` does, which Synapse already does for
    EVERY room a user is joined to, not just ones of a particular kind."""
    try:
        room_ids = await synapse.get_joined_rooms(mxid)
    except SynapseError:
        logger.debug("Could not list joined rooms for ghost %s", mxid, exc_info=True)
        return

    for room_id in room_ids:
        try:
            current = await synapse.get_room_state(room_id, "m.room.member", mxid, as_user_id=mxid)
        except SynapseError:
            continue  # not actually a member here (anymore) -- nothing to update
        if current.get("membership") != "join":
            continue
        content = dict(current)
        content["displayname"] = display_name
        if formatted_body is not None:
            content[FORMATTED_DISPLAYNAME_FIELD] = {
                "format": "org.matrix.custom.html", "formatted_body": formatted_body,
            }
        else:
            content.pop(FORMATTED_DISPLAYNAME_FIELD, None)
        try:
            await synapse.send_state_event(room_id, "m.room.member", mxid, content, as_user_id=mxid)
        except SynapseError:
            logger.debug("Could not update formatted displayname for %s in %s", actor_id, room_id, exc_info=True)


async def maybe_add_formatted_displayname_on_join(synapse: SynapseClient, config, event: dict) -> None:
    """When one of the bridge's ghosts joins a room, copy its MSC4589
    formatted displayname from its global profile onto that new member
    event. Synapse fills a join's ``displayname`` from the global profile
    but not this field, so without this every room a ghost joins after its
    last name change (a reply thread, the backstage space, a room it
    self-joins) would be missing it. Never claims the event; does nothing
    unless the feature is on, the joiner is a ghost, the field is missing,
    and the profile's plain name still matches the member event's."""
    if not config.bridge.use_msc4589_formatted_displayname or event.get("type") != "m.room.member":
        return
    content = event.get("content") or {}
    mxid = event.get("state_key") or ""
    room_id = event.get("room_id")
    if (
        content.get("membership") != "join"
        or event.get("sender") != mxid
        or FORMATTED_DISPLAYNAME_FIELD in content
        or not room_id
        or not mxid.startswith(f"@{config.appservice.user_prefix}")
        or not mxid.endswith(f":{config.synapse.server_name}")
    ):
        return
    try:
        profile = await synapse.get_profile(mxid)
    except SynapseError:
        return
    formatted = profile.get(FORMATTED_DISPLAYNAME_FIELD)
    if not formatted or profile.get("displayname") != content.get("displayname"):
        return
    try:
        current = await synapse.get_room_state(room_id, "m.room.member", mxid, as_user_id=mxid)
        if current.get("membership") != "join" or FORMATTED_DISPLAYNAME_FIELD in current:
            return
        await synapse.send_state_event(
            room_id, "m.room.member", mxid, {**current, FORMATTED_DISPLAYNAME_FIELD: formatted}, as_user_id=mxid,
        )
    except SynapseError:
        logger.debug("Could not add the formatted displayname for %s in %s", mxid, room_id, exc_info=True)


async def _add_formatted_room_name(
    synapse: SynapseClient, room_id: str, mxid: str, display_name: str, formatted_body: str,
) -> None:
    """Give a ghost's room its formatted name (MSC4591), only while the
    room is still named exactly after the ghost and doesn't have it yet."""
    try:
        current = await synapse.get_room_state(room_id, "m.room.name", as_user_id=mxid) or {}
        if current.get("name") != display_name or FORMATTED_ROOM_NAME_FIELD in current:
            return
        await synapse.send_state_event(
            room_id, "m.room.name", "", {**current, **room_name_content(display_name, formatted_body)}, as_user_id=mxid,
        )
    except SynapseError:
        logger.debug("Could not add the formatted room name in %s", room_id, exc_info=True)


async def maybe_add_formatted_room_name(synapse: SynapseClient, config, event: dict) -> None:
    """When one of the bridge's ghosts names a room after itself (a Remote
    User Room being made, or a rename from any path) without the MSC4591
    formatted name, add the ghost's formatted display name from its global
    profile, so the room name and the displayname are formatted together.
    Never claims the event; does nothing unless the feature is on, the
    sender is a ghost, the name is exactly its displayname, and the field
    is missing."""
    if not config.bridge.use_msc4589_formatted_displayname or event.get("type") != "m.room.name":
        return
    content = event.get("content") or {}
    sender = event.get("sender") or ""
    room_id = event.get("room_id")
    if (
        event.get("state_key") != ""
        or FORMATTED_ROOM_NAME_FIELD in content
        or not content.get("name")
        or not room_id
        or not sender.startswith(f"@{config.appservice.user_prefix}")
        or not sender.endswith(f":{config.synapse.server_name}")
    ):
        return
    try:
        profile = await synapse.get_profile(sender)
    except SynapseError:
        return
    formatted = (profile.get(FORMATTED_DISPLAYNAME_FIELD) or {}).get("formatted_body")
    if not formatted or profile.get("displayname") != content.get("name"):
        return
    await _add_formatted_room_name(synapse, room_id, sender, content["name"], formatted)


async def sync_ghost_profile(
    synapse: SynapseClient,
    http_client: httpx.AsyncClient,
    repository: ActorRepository,
    config,
    *,
    server_name: str,
    localpart: str,
    actor_id: str,
    display_name: str | None,
    icon_url: str | None,
    handle: str | None = None,
    tag: list[dict] | None = None,
    formatted_displayname_enabled: bool = False,
) -> str:
    """Like ``ensure_ghost_user``, but only actually touches the ghost's
    Matrix display name/avatar when the remote actor's own value has changed
    since the last time we synced it (per ``repository``'s ``GhostProfile``
    cache, keyed by ``actor_id``). A changed avatar is also pushed out to
    every room that reflects this ghost's identity as a room avatar -- see
    ``_sync_room_avatars`` -- so a Remote User Room/DM/Chat room's avatar
    doesn't go stale the moment it's created and never update again.

    Every interaction from a remote actor (a reply, a reaction, ...) re-runs
    this, since a ghost might not exist yet or might be stale -- but calling
    ``ensure_ghost_user`` directly with a freshly re-fetched-and-reuploaded
    avatar on every single one of those would re-upload the same image and
    call ``set_avatar_url``/``set_display_name`` every time regardless,
    which Synapse renders as a "changed their profile picture/name" event in
    every room the ghost is in -- flooding a room for an account that
    interacts often even though nothing about them has changed at all.

    ``handle`` (an ``@user@instance.org`` string, if the caller has it) is
    recorded alongside the mxid so a later Matrix mention of this ghost can
    be resolved back to both the actor and its handle (see
    ``bridge.mentions``) without needing to refetch anything.
    """
    mxid = f"@{localpart}:{server_name}"
    try:
        await synapse.register_appservice_user(localpart)
    except SynapseError as exc:
        if exc.errcode != "M_USER_IN_USE":
            raise
    await join_ghost_to_backstage_space(synapse, repository, config, mxid)

    cached = await repository.get_ghost_profile(actor_id)
    cached_display_name = cached.display_name if cached else None
    cached_icon_url = cached.icon_url if cached else None
    cached_mxid = cached.mxid if cached else None
    cached_handle = cached.handle if cached else None

    if display_name and display_name != cached_display_name:
        try:
            await synapse.set_display_name(mxid, display_name)
        except SynapseError:
            logger.debug("Could not set display name for ghost %s", mxid, exc_info=True)

        formatted_body = None
        if formatted_displayname_enabled:
            formatted_body = await build_formatted_displayname(http_client, synapse, repository, display_name, tag or [])
            if formatted_body is not None:
                try:
                    await synapse.set_profile_field(
                        mxid, FORMATTED_DISPLAYNAME_FIELD,
                        {"format": "org.matrix.custom.html", "formatted_body": formatted_body},
                    )
                except SynapseError:
                    logger.debug("Could not set formatted display name for ghost %s", mxid, exc_info=True)
                await _sync_formatted_displayname_rooms(
                    synapse, repository, actor_id=actor_id, mxid=mxid,
                    display_name=display_name, formatted_body=formatted_body,
                )
            else:
                # Nothing to set now -- but the name change might be exactly
                # what dropped the last shortcode from a PREVIOUSLY formatted
                # name (e.g. went plaintext-only), in which case the stale
                # field needs clearing everywhere it was pushed. Checked via
                # one profile GET rather than unconditionally DELETE-ing and
                # sweeping every room on every single name change: cheap for
                # the common case (most actors never use custom emoji in
                # their name at all, so there's never anything to clear),
                # correct for the migration case (the field really is still
                # there from before).
                try:
                    current_profile = await synapse.get_profile(mxid)
                except SynapseError:
                    current_profile = {}
                if FORMATTED_DISPLAYNAME_FIELD in current_profile:
                    try:
                        await synapse.delete_profile_field(mxid, FORMATTED_DISPLAYNAME_FIELD)
                    except SynapseError:
                        logger.debug("Could not clear formatted display name for ghost %s", mxid, exc_info=True)
                    await _sync_formatted_displayname_rooms(
                        synapse, repository, actor_id=actor_id, mxid=mxid,
                        display_name=display_name, formatted_body=None,
                    )
        # The Remote User Room's name follows the ghost's displayname, with
        # its formatted version (MSC4591) when there is one, in one event:
        # this is the path that picks up a rename through an ordinary
        # interaction when the remote server never sends an Update.
        remote_actor_room = await repository.get_remote_actor_room(actor_id)
        if remote_actor_room is not None:
            try:
                await synapse.send_state_event(
                    remote_actor_room.room_id, "m.room.name", "", room_name_content(display_name, formatted_body),
                    as_user_id=mxid,
                )
            except SynapseError:
                logger.debug("Could not update room name for ghost %s", mxid, exc_info=True)
    elif formatted_displayname_enabled and display_name and tag and ":" in display_name:
        # The name hasn't changed, but a ghost last renamed before MSC4589
        # was turned on (or whose emoji couldn't be resolved then) never got
        # a formatted name at all. One profile read, only for names that
        # contain a shortcode, fills it in.
        try:
            current_profile = await synapse.get_profile(mxid)
        except SynapseError:
            current_profile = None
        if current_profile is not None and FORMATTED_DISPLAYNAME_FIELD not in current_profile:
            formatted_body = await build_formatted_displayname(http_client, synapse, repository, display_name, tag)
            if formatted_body is not None:
                try:
                    await synapse.set_profile_field(
                        mxid, FORMATTED_DISPLAYNAME_FIELD,
                        {"format": "org.matrix.custom.html", "formatted_body": formatted_body},
                    )
                except SynapseError:
                    logger.debug("Could not set formatted display name for ghost %s", mxid, exc_info=True)
                await _sync_formatted_displayname_rooms(
                    synapse, repository, actor_id=actor_id, mxid=mxid,
                    display_name=display_name, formatted_body=formatted_body,
                )
                remote_actor_room = await repository.get_remote_actor_room(actor_id)
                if remote_actor_room is not None:
                    await _add_formatted_room_name(
                        synapse, remote_actor_room.room_id, mxid, display_name, formatted_body,
                    )

    if display_name and tag:
        # Unconditional on whether the name actually changed (unlike the
        # Matrix-facing set_display_name call above) -- this only resolves
        # and persists which of the name's own :shortcode:s have an image
        # (bridge.custom_emoji.resolve_and_persist_emoji), for the public
        # web page byline to render later (bridge.web_views); a ghost whose
        # name never changes again after this ships would otherwise never
        # get backfilled. Cheap on a repeat: the dedup cache is checked
        # before any network fetch, and the persisted mapping is itself
        # idempotent.
        await resolve_and_persist_emoji(http_client, synapse, repository, display_name, tag, actor_id)

    if icon_url and icon_url != cached_icon_url:
        avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url)
        if avatar_mxc:
            try:
                await synapse.set_avatar_url(mxid, avatar_mxc)
            except SynapseError:
                logger.debug("Could not set avatar for ghost %s", mxid, exc_info=True)
            await _sync_room_avatars(synapse, repository, actor_id=actor_id, mxid=mxid, avatar_mxc=avatar_mxc)

    if (
        display_name != cached_display_name
        or icon_url != cached_icon_url
        or mxid != cached_mxid
        or (handle and handle != cached_handle)
    ):
        await repository.record_ghost_profile(
            GhostProfile(
                actor_id=actor_id, display_name=display_name, icon_url=icon_url,
                mxid=mxid, handle=handle or cached_handle,
            )
        )

    return mxid
