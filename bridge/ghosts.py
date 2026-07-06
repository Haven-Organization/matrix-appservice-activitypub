"""Ghost (virtual Matrix user) identity helpers for remote ActivityPub actors.

Every remote fediverse account the bridge mirrors gets a ghosted Matrix user
under the AppService's reserved namespace (``appservice.user_prefix`` in
config), e.g. ``user@instance.org`` -> ``@ap_user_instance.org:bridge.domain``.
"""

from __future__ import annotations

import logging
import re

import httpx

from bridge.custom_emoji import resolve_and_persist_emoji
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


async def ensure_ghost_user(
    synapse: SynapseClient,
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

    return mxid


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


async def sync_ghost_profile(
    synapse: SynapseClient,
    http_client: httpx.AsyncClient,
    repository: ActorRepository,
    *,
    server_name: str,
    localpart: str,
    actor_id: str,
    display_name: str | None,
    icon_url: str | None,
    handle: str | None = None,
    tag: list[dict] | None = None,
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
