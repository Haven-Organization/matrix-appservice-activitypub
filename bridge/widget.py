"""A Matrix room widget for this bridge -- the same actions ``;follow``,
``;block``, ``;replace room``, etc. offer as chat commands, as a small
web UI any widget-capable Matrix client can embed in a room.

Authentication never trusts the widget's own URL parameters (a client
substitutes ``$matrix_user_id``/``$matrix_room_id`` into the widget URL,
but nothing stops a hand-crafted URL from claiming to be anyone) -- the
widget instead performs the standard Matrix Widget API "get_openid_token"
handshake with its host client, then this module verifies that token
against Synapse's own federation ``openid/userinfo`` endpoint
(``SynapseClient.verify_openid_token``) to get a real, server-vouched
``@user:server`` before establishing a session. Every ``/widget/api/*``
action endpoint below requires that session (``Authorization: Bearer
<session_id>``) and re-derives the room's context from the bridge's own
repository -- never from anything the client-side JS asserts about
itself.

Every action reuses the exact same ``bridge.commands`` handler a chat
command would call (``_handle_follow``, ``_handle_block``, ...) -- same
validation, same room notices posted as user-facing feedback, same
dedup/rate-limit behavior. The widget itself only ever renders state and
triggers these; it holds no independent bridge logic of its own.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from bridge.activitypub.urls import actor_url
from bridge.commands import (
    _ALLOW_RULE_TYPES,
    _bot_mxid,
    _COMMAND_PREFIX,
    _handle_allow,
    _handle_backfill,
    _handle_banner,
    _handle_block,
    _handle_chat,
    _handle_create_channel,
    _handle_create_community,
    _handle_create_profile,
    _execute_leave_unfollowed,
    _handle_delete_profile,
    _handle_disallow,
    _handle_dm,
    _handle_follow,
    _handle_id,
    _handle_import,
    _handle_link_profile,
    _handle_mute,
    _handle_rejoin,
    _list_unfollowed_ghost_rooms,
    _RoomListingUnreliable,
    _handle_replace_room,
    _handle_set,
    _handle_unblock,
    _handle_unfollow,
    _handle_unlink_profile,
    _handle_unmute,
    _is_matrix_admin,
    _parse_follows_export,
    _run_follows_import,
    _RUNNING_FOLLOWS_IMPORTS,
)
from bridge.identity_settings import get_effective_settings
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

router = APIRouter()

# -- sessions ---------------------------------------------------------------
#
# In-memory only (same tradeoff as InMemoryActorRepository/_RUNNING_BACKFILLS
# elsewhere): a restart just makes every open widget silently re-auth on its
# next action, which is a non-issue since the OpenID handshake is fast and
# invisible to the user. Never persisted -- there is no reason a widget
# session should outlive the process, and keeping it that way means a leaked
# session id is only ever useful for as long as this process has been up
# since the session was minted.
_SESSION_TTL_SECONDS = 3600
_sessions: dict[str, tuple[str, float]] = {}  # session_id -> (matrix_user_id, expires_at)


def _new_session(matrix_user_id: str) -> str:
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = (matrix_user_id, time.monotonic() + _SESSION_TTL_SECONDS)
    return session_id


def _session_user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing session -- call /widget/api/session first.")
    session_id = auth[len("bearer "):].strip()
    entry = _sessions.get(session_id)
    if entry is None:
        raise HTTPException(401, "Session expired or unknown -- re-authenticate.")
    matrix_user_id, expires_at = entry
    if time.monotonic() > expires_at:
        del _sessions[session_id]
        raise HTTPException(401, "Session expired -- re-authenticate.")
    return matrix_user_id


# -- auth --------------------------------------------------------------------


@router.post("/widget/api/session")
async def widget_session(request: Request) -> JSONResponse:
    body: dict[str, Any] = await request.json()
    access_token = body.get("access_token")
    matrix_server_name = body.get("matrix_server_name")
    if not access_token or not matrix_server_name:
        raise HTTPException(400, "access_token and matrix_server_name are required")

    config = request.app.state.config
    if matrix_server_name != config.synapse.server_name:
        # This widget is only ever registered in rooms on our own
        # homeserver -- a token vouching for some other server would mean
        # either a misconfigured client or something actively suspicious.
        # No real federation lookup is even attempted for it.
        raise HTTPException(403, "This widget only works for accounts on this homeserver.")

    matrix_user_id = await request.app.state.synapse.verify_openid_token(access_token)
    if matrix_user_id is None:
        raise HTTPException(403, "Could not verify your identity with the homeserver.")

    session_id = _new_session(matrix_user_id)
    return JSONResponse({"session_id": session_id, "matrix_user_id": matrix_user_id})


# -- context -------------------------------------------------------------------


@router.get("/widget/api/context")
async def widget_context(request: Request, room_id: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    repository = request.app.state.repository
    domain = config.bridge.domain
    base = config.bridge.public_base_url

    own_actor = await repository.get_local_actor_by_matrix_id(matrix_user_id)
    is_admin = await _is_matrix_admin(request, matrix_user_id)

    room: dict[str, Any] = {"kind": "unmanaged"}

    profile_owner = await repository.get_profile_room_owner(room_id)
    remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
    dm_actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
    chat_actor_id = await repository.get_ghost_chat_room_actor_id(room_id)
    notification_owner = await repository.get_bot_dm_room_owner(room_id)

    if profile_owner is not None:
        is_owner = profile_owner == matrix_user_id
        owner_record = (
            own_actor if is_owner and own_actor is not None
            else await repository.get_local_actor_by_matrix_id(profile_owner)
        )
        owner_settings = (
            await get_effective_settings(repository, owner_record.username, "profile")
            if owner_record else {}
        )
        room = {
            "kind": "profile",
            "is_owner": is_owner,
            "owner_matrix_user_id": profile_owner,
            "username": owner_record.username if owner_record else None,
            "display_name": owner_record.display_name if owner_record else None,
            "hide_followers": owner_settings.get("hide_followers", False),
            "hide_following": owner_settings.get("hide_following", False),
            "public_url": actor_url(base, owner_record.username) if owner_record else None,
        }
    elif remote_room is not None:
        handle_domain = urlsplit(remote_room.actor_id).hostname or ""
        is_following = await repository.is_following(own_actor.username, remote_room.actor_id) if own_actor else False
        is_blocked = await repository.is_blocked(own_actor.username, remote_room.actor_id) if own_actor else False
        is_muted = await repository.is_muted(own_actor.username, remote_room.actor_id) if own_actor else False
        room = {
            "kind": "remote_actor",
            "actor_id": remote_room.actor_id,
            "display_name": remote_room.display_name,
            "handle_domain": handle_domain,
            "is_following": is_following,
            "is_blocked": is_blocked,
            "is_muted": is_muted,
        }
    elif dm_actor_id is not None:
        room = {"kind": "dm", "actor_id": dm_actor_id}
    elif chat_actor_id is not None:
        room = {"kind": "chat", "actor_id": chat_actor_id}
    elif notification_owner is not None:
        room = {"kind": "notifications", "is_owner": notification_owner == matrix_user_id}

    return JSONResponse(
        {
            "matrix_user_id": matrix_user_id,
            "domain": domain,
            "is_admin": is_admin,
            "has_profile": own_actor is not None,
            "own_username": own_actor.username if own_actor else None,
            "backfill_default_count": config.bridge.backfill_default_count,
            "command_prefix": _COMMAND_PREFIX,
            "custom_identities_enabled": config.bridge.custom_identities_enabled,
            "peertube_channels_enabled": config.bridge.peertube_channels_enabled,
            "lemmy_community_hosting_enabled": config.bridge.lemmy_community_hosting_enabled,
            "room": room,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/following")
async def widget_following(request: Request) -> JSONResponse:
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    own_actor = await repository.get_local_actor_by_matrix_id(matrix_user_id)
    if own_actor is None:
        return JSONResponse({"following": []}, headers={"Cache-Control": "no-store"})
    entries = []
    for actor_id in await repository.list_following(own_actor.username):
        remote_room = await repository.get_remote_actor_room(actor_id)
        ghost_profile = await repository.get_ghost_profile(actor_id)
        # A WebFinger-resolvable "@user@domain" handle, never the raw
        # actor_id URL -- _handle_unfollow's `handle` argument always goes
        # through resolve_remote_actor_id (WebFinger), which expects
        # exactly this shape, not a URL. Falls back to deriving one from
        # the actor_id itself (same convention used when a Remote User
        # Room is first created) if this actor's ghost profile was never
        # synced for some reason.
        handle = (ghost_profile.handle if ghost_profile else None) or (
            f"@{actor_id.rstrip('/').rsplit('/', 1)[-1]}@{urlsplit(actor_id).hostname}"
        )
        entries.append(
            {
                "actor_id": actor_id,
                "handle": handle,
                "display_name": remote_room.display_name if remote_room else None,
                "room_id": remote_room.room_id if remote_room else None,
            }
        )
    return JSONResponse({"following": entries}, headers={"Cache-Control": "no-store"})


@router.get("/widget/api/identities")
async def widget_identities(request: Request) -> JSONResponse:
    """Every ActivityPub identity the session's Matrix user owns, and which
    one is Active -- the widget's own equivalent of ``;id list``, global
    (not room-scoped) same as the command itself. Works, and lists exactly
    one entry, even for an account that's never touched
    ``custom_identities_enabled`` at all."""
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    config = request.app.state.config
    identities = await repository.list_local_actors_by_matrix_id(matrix_user_id)
    active = await repository.get_active_local_actor(matrix_user_id)
    active_username = active.username if active is not None else None
    entries = [
        {
            "username": a.username,
            "display_name": a.display_name,
            "handle": f"@{a.username}@{config.bridge.domain}",
            "is_active": a.username == active_username,
        }
        for a in identities
    ]
    return JSONResponse({"identities": entries}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/identities/switch")
async def widget_identities_switch(request: Request) -> JSONResponse:
    body = await request.json()
    username = body.get("username", "")
    if not isinstance(username, str) or not username:
        raise HTTPException(400, "username is required")
    # _handle_id doesn't use room_id for anything but posting its own
    # confirmation notice -- any room this session's widget happens to be
    # in works; it's never consulted to resolve WHICH identity, unlike
    # _handle_set below.
    return await _dispatch(request, body.get("room_id", ""), _handle_id, argument=username)


@router.get("/widget/api/identities/settings")
async def widget_identity_settings(request: Request, username: str) -> JSONResponse:
    """One identity's own profile-level settings + public info, keyed by
    username rather than room -- the Identity tab isn't tied to any one
    Profile Room the way the Home tab's room-kind view is."""
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    config = request.app.state.config
    actor = await repository.get_local_actor(username)
    if actor is None or actor.matrix_user_id != matrix_user_id:
        raise HTTPException(404, "No such identity on this account")
    settings = await get_effective_settings(repository, actor.username, "profile")
    return JSONResponse(
        {
            "username": actor.username,
            "display_name": actor.display_name,
            "handle": f"@{actor.username}@{config.bridge.domain}",
            "public_url": actor_url(config.bridge.public_base_url, actor.username),
            "room_id": actor.room_id,
            "hide_followers": settings.get("hide_followers", False),
            "hide_following": settings.get("hide_following", False),
            "discoverable": settings.get("discoverable", True),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/leave_unfollowed/preview")
async def widget_leave_unfollowed_preview(request: Request) -> JSONResponse:
    """Read-only half of ";leave unfollowed"'s two-step confirm flow --
    the widget shows this list itself and gets its own explicit confirm
    click, so it calls _execute_leave_unfollowed directly below rather
    than going through _handle_leave_unfollowed's chat-reply-based
    "confirm" mechanic, which the widget has no way to satisfy."""
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    identities = await repository.list_local_actors_by_matrix_id(matrix_user_id)
    if not identities:
        return JSONResponse({"rooms": [], "unreliable": False}, headers={"Cache-Control": "no-store"})
    try:
        unfollowed = await _list_unfollowed_ghost_rooms(
            request, sender=matrix_user_id, usernames=[i.username for i in identities]
        )
    except _RoomListingUnreliable:
        return JSONResponse({"rooms": [], "unreliable": True}, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"rooms": [{"handle": handle} for _room_id, _actor_id, handle in unfollowed], "unreliable": False},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/widget/api/leave_unfollowed/execute")
async def widget_leave_unfollowed_execute(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(request, body.get("room_id", ""), _execute_leave_unfollowed)


@router.post("/widget/api/rejoin")
async def widget_rejoin(request: Request) -> JSONResponse:
    """";rejoin profile" + ";rejoin space" back to back -- the self-service
    recovery shortcut, global like the Identity tab it lives in rather
    than needing a specific room's own context. Never targets anyone but
    the caller (no `[@other]` support here) -- that admin-only escape
    hatch stays chat-only, same scoping decision as the original widget
    plan's gap audit made for it."""
    matrix_user_id = _session_user(request)
    body = await request.json()
    target_room_id = body.get("room_id", "")
    try:
        await _handle_rejoin(request, sender=matrix_user_id, room_id=target_room_id, argument="profile")
        await _handle_rejoin(request, sender=matrix_user_id, room_id=target_room_id, argument="space")
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": True})


@router.get("/widget/api/admin/grants")
async def widget_admin_grants(request: Request) -> JSONResponse:
    """Every current third-party access grant -- the widget's own
    equivalent of ``;allowed``, read directly from the repository (same
    ``widget_following``-style pattern) rather than parsing a chat notice.
    Admin-gated here too, not just client-side by hiding the tab -- same
    defense in depth every other admin-only widget read/action already
    has via the handler it calls, since nothing stops a crafted request
    straight at this endpoint."""
    matrix_user_id = _session_user(request)
    if not await _is_matrix_admin(request, matrix_user_id):
        raise HTTPException(403, "Only a Matrix server admin can view third-party access grants.")
    repository = request.app.state.repository
    config = request.app.state.config
    grants = await repository.list_third_party_allows()
    return JSONResponse(
        {
            "mode": config.bridge.third_party_access_mode,
            "grants": [
                {"rule_type": g.rule_type, "value": g.value, "granted_by": g.granted_by, "granted_at": g.granted_at}
                for g in grants
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/widget/api/admin/allow")
async def widget_admin_allow(request: Request) -> JSONResponse:
    body = await request.json()
    rule_type = body.get("rule_type", "")
    value = body.get("value", "")
    if rule_type not in _ALLOW_RULE_TYPES or not isinstance(value, str) or not value.strip():
        raise HTTPException(400, f"rule_type must be one of {_ALLOW_RULE_TYPES} and value is required")
    return await _dispatch(request, body.get("room_id", ""), _handle_allow, argument=f"{rule_type} {value.strip()}")


@router.post("/widget/api/admin/disallow")
async def widget_admin_disallow(request: Request) -> JSONResponse:
    body = await request.json()
    rule_type = body.get("rule_type", "")
    value = body.get("value", "")
    if rule_type not in _ALLOW_RULE_TYPES or not isinstance(value, str) or not value.strip():
        raise HTTPException(400, f"rule_type must be one of {_ALLOW_RULE_TYPES} and value is required")
    return await _dispatch(request, body.get("room_id", ""), _handle_disallow, argument=f"{rule_type} {value.strip()}")


# -- actions -------------------------------------------------------------------
#
# Every action below reuses its chat-command counterpart verbatim (same
# validation, same room notice as user-facing feedback) and just returns
# {"ok": true} unconditionally -- the widget re-fetches /widget/api/context
# right after to reflect whatever actually happened; a failure is visible in
# the room's own timeline exactly the same as if ";follow" had been typed.


async def _dispatch(request: Request, room_id: str, handler, /, **kwargs) -> JSONResponse:
    matrix_user_id = _session_user(request)
    try:
        await handler(request, sender=matrix_user_id, room_id=room_id, **kwargs)
    except SynapseError as exc:
        logger.warning("Widget action failed in %s: %s", room_id, exc, exc_info=True)
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": True})


@router.post("/widget/api/follow")
async def widget_follow(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_follow, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/unfollow")
async def widget_unfollow(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_unfollow, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/block")
async def widget_block(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_block, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/unblock")
async def widget_unblock(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_unblock, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/mute")
async def widget_mute(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_mute, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/unmute")
async def widget_unmute(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_unmute, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/dm")
async def widget_dm(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_dm, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/chat")
async def widget_chat(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(
        request, body["room_id"], _handle_chat, handle=body.get("handle", ""), content={},
    )


@router.post("/widget/api/import")
async def widget_import(request: Request) -> JSONResponse:
    body = await request.json()
    url = body.get("url", "")
    if not isinstance(url, str) or not url:
        raise HTTPException(400, "url is required")
    matrix_user_id = _session_user(request)
    try:
        await _handle_import(request, sender=matrix_user_id, room_id=body["room_id"], url=url)
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": True})


@router.post("/widget/api/replace_room")
async def widget_replace_room(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(request, body["room_id"], _handle_replace_room)


@router.post("/widget/api/backfill")
async def widget_backfill(request: Request) -> JSONResponse:
    body = await request.json()
    count = body.get("count")
    argument = str(int(count)) if isinstance(count, (int, float)) and count else ""
    return await _dispatch(
        request, body["room_id"], _handle_backfill, argument=argument, content={},
    )


@router.post("/widget/api/create_profile")
async def widget_create_profile(request: Request) -> JSONResponse:
    """Creates a brand-new identity -- the bare form (no ``custom_id``)
    for a first-ever profile, or an additional one when
    ``custom_identities_enabled``. ``room_id`` is only ever used by
    ``_handle_create_profile`` for its own confirmation notice (silently
    swallowed if the room doesn't make sense for it), never to decide
    WHICH identity gets created -- safe to call from any room the widget
    happens to be embedded in, including from the sidebar identity
    switcher rather than a specific room's own tab."""
    body = await request.json()
    custom_id = (body.get("custom_id") or "").strip()
    return await _dispatch(
        request, body.get("room_id", ""), _handle_create_profile, custom_id=custom_id
    )


@router.post("/widget/api/link_profile")
async def widget_link_profile(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(request, body["room_id"], _handle_link_profile)


@router.post("/widget/api/unlink_profile")
async def widget_unlink_profile(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(request, body["room_id"], _handle_unlink_profile)


@router.post("/widget/api/delete_profile")
async def widget_delete_profile(request: Request) -> JSONResponse:
    body = await request.json()
    return await _dispatch(request, body["room_id"], _handle_delete_profile)


# -- Publish tab: PeerTube channels + Lemmy communities -----------------------
#
# Create + list only -- publish/unpublish/edit a video/post and
# replace-video all require replying to an already-uploaded Matrix media
# event INSIDE the channel/community's own room (_handle_publish etc. take
# a Matrix `content` dict, not a free-form argument), which needs a real
# Matrix media upload the widget has no access token to drive itself. Those
# stay "manage in the room" -- see the channel/community's own room_id,
# surfaced below for a plain matrix.to deep link.


def _build_create_argument(identifier: str, as_identity: str, display_name: str) -> str:
    parts = [identifier]
    if as_identity:
        parts += ["as", as_identity]
    if display_name:
        parts.append(display_name)
    return " ".join(parts)


@router.get("/widget/api/channels")
async def widget_channels(request: Request) -> JSONResponse:
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    if not config.bridge.peertube_channels_enabled:
        return JSONResponse({"enabled": False, "channels": []}, headers={"Cache-Control": "no-store"})
    repository = request.app.state.repository
    identities = await repository.list_local_actors_by_matrix_id(matrix_user_id)
    channels = [
        {
            "username": channel.username,
            "display_name": channel.display_name,
            "owner_username": channel.owner_username,
            "room_id": channel.room_id,
        }
        for identity in identities
        for channel in await repository.list_peertube_channels_by_owner(identity.username)
    ]
    return JSONResponse({"enabled": True, "channels": channels}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/channels/create")
async def widget_create_channel(request: Request) -> JSONResponse:
    body = await request.json()
    channel_id = (body.get("id") or "").strip()
    if not channel_id:
        raise HTTPException(400, "id is required")
    argument = _build_create_argument(
        channel_id, (body.get("as_identity") or "").strip(), (body.get("display_name") or "").strip()
    )
    return await _dispatch(request, body.get("room_id", ""), _handle_create_channel, argument=argument)


@router.get("/widget/api/communities")
async def widget_communities(request: Request) -> JSONResponse:
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    if not config.bridge.lemmy_community_hosting_enabled:
        return JSONResponse({"enabled": False, "communities": []}, headers={"Cache-Control": "no-store"})
    repository = request.app.state.repository
    identities = await repository.list_local_actors_by_matrix_id(matrix_user_id)
    communities = [
        {
            "username": community.username,
            "display_name": community.display_name,
            "owner_username": community.owner_username,
            "room_id": community.room_id,
        }
        for identity in identities
        for community in await repository.list_lemmy_communities_by_owner(identity.username)
    ]
    return JSONResponse({"enabled": True, "communities": communities}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/communities/create")
async def widget_create_community(request: Request) -> JSONResponse:
    body = await request.json()
    community_id = (body.get("id") or "").strip()
    if not community_id:
        raise HTTPException(400, "id is required")
    argument = _build_create_argument(
        community_id, (body.get("as_identity") or "").strip(), (body.get("display_name") or "").strip()
    )
    return await _dispatch(request, body.get("room_id", ""), _handle_create_community, argument=argument)


@router.post("/widget/api/banner")
async def widget_banner(request: Request) -> JSONResponse:
    body = await request.json()
    mxc = body.get("mxc", "")
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        raise HTTPException(400, "mxc must be a mxc:// URI")
    return await _dispatch(request, body["room_id"], _handle_banner, argument=mxc)


@router.post("/widget/api/upload_banner")
async def widget_upload_banner(request: Request, room_id: str) -> JSONResponse:
    """Like ``/widget/api/banner``, but takes the raw image bytes directly
    (the widget's own ``<input type=file>``) instead of requiring the user
    to already have an ``mxc://`` URI in hand -- the bot's own account
    uploads it (``upload_media``'s ``as_user_id=bot_mxid``; the AS token
    can impersonate the bot but not an arbitrary local human, so it can
    never upload "as" the profile owner) and ``_handle_banner`` doesn't
    care who uploaded the underlying media, only that the URI resolves."""
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    data = await request.body()
    if not data:
        raise HTTPException(400, "No image data received")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    try:
        mxc = await request.app.state.synapse.upload_media(
            data, content_type, "banner", as_user_id=_bot_mxid(config),
        )
    except SynapseError as exc:
        raise HTTPException(502, "Could not upload that image to the homeserver.") from exc
    try:
        await _handle_banner(request, sender=matrix_user_id, room_id=room_id, argument=mxc)
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": True})


@router.post("/widget/api/import_follows")
async def widget_import_follows(request: Request, room_id: str) -> JSONResponse:
    """Like ``;import follows``, minus the "must be a reply to an
    already-uploaded file" mechanic -- a widget can just hand over the raw
    file bytes directly, so there's no need to fake a Matrix reply chain
    to satisfy that requirement."""
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(matrix_user_id)
    if actor_record is None:
        raise HTTPException(400, "You need a linked profile before importing follows.")
    data = await request.body()
    handles = _parse_follows_export(data.decode("utf-8", errors="replace"))
    if not handles:
        raise HTTPException(
            400,
            "That file doesn't look like a follows export -- expected one @user@instance.org "
            'handle per line (Pleroma/Akkoma), or a CSV with an "Account address" column (Mastodon).',
        )
    task = asyncio.get_running_loop().create_task(
        _run_follows_import(
            request, sender=matrix_user_id, room_id=room_id, actor_record=actor_record, handles=handles,
        )
    )
    _RUNNING_FOLLOWS_IMPORTS.add(task)
    task.add_done_callback(_RUNNING_FOLLOWS_IMPORTS.discard)
    return JSONResponse({"ok": True, "count": len(handles)})


@router.post("/widget/api/visibility")
async def widget_visibility(request: Request) -> JSONResponse:
    # Reuses _handle_set, the exact same handler ";set hide_followers on"
    # dispatches to. See this module's own docstring on why every widget
    # action goes through bridge.commands's real handlers rather than
    # talking to the repository directly.
    #
    # _handle_set resolves WHICH identity from room context alone (see
    # _resolve_owned_identity in bridge.commands) -- it has no explicit
    # selector argument the way _handle_id does. For a multi-identity
    # account the Identity tab isn't bound to any one identity's own room,
    # so an optional `username` here is resolved to THAT identity's own
    # Profile Room and used as the dispatch room_id instead of the widget's
    # embedding room -- a legitimate use of the exact same room-ownership
    # check _resolve_owned_identity already does, not a workaround of it.
    body = await request.json()
    which = body.get("which")
    target_room_id = body.get("room_id", "")
    username = body.get("username")
    if username:
        repository = request.app.state.repository
        identity = await repository.get_local_actor(username)
        if identity is not None and identity.room_id:
            target_room_id = identity.room_id
    if which == "discoverable":
        value = "on" if body.get("value") else "off"
        return await _dispatch(request, target_room_id, _handle_set, argument=f"discoverable {value}")
    if which not in ("followers", "following"):
        raise HTTPException(400, "which must be 'followers', 'following', or 'discoverable'")
    key = "hide_followers" if which == "followers" else "hide_following"
    value = "on" if body.get("hidden") else "off"
    return await _dispatch(request, target_room_id, _handle_set, argument=f"{key} {value}")


# -- icon ----------------------------------------------------------------------


@router.get("/widget/icon")
async def widget_icon(request: Request) -> Response:
    """The bridge bot's own avatar, proxied so it can be used as the
    widget's favicon -- ``config.appservice.bot_avatar_mxc`` points at
    Synapse's internal ``base_url`` (often ``http://localhost:8008``),
    unreachable from a browser loading this page directly, so it has to
    be fetched server-side and streamed back rather than linked to
    directly."""
    config = request.app.state.config
    avatar_mxc = config.appservice.bot_avatar_mxc
    if not avatar_mxc or not avatar_mxc.startswith("mxc://"):
        raise HTTPException(404, "No bot avatar configured")
    server_name, _, media_id = avatar_mxc.removeprefix("mxc://").partition("/")
    try:
        download = await request.app.state.synapse.download_media(server_name, media_id)
    except SynapseError as exc:
        raise HTTPException(502, "Could not fetch the bot's avatar") from exc
    return Response(content=download.content, media_type=download.content_type)


# -- the widget page itself ----------------------------------------------------


@router.get("/widget")
async def widget_page() -> HTMLResponse:
    # Loaded directly into a client's iframe -- with no explicit
    # Cache-Control at all, a browser/webview is free to apply its own
    # heuristic caching to what looks like an ordinary static document,
    # which is exactly what made an earlier deploy invisible even after
    # removing and re-adding the widget (the iframe's *document* was
    # served from cache, never re-fetched at all).
    return HTMLResponse(_WIDGET_HTML, headers={"Cache-Control": "no-store, must-revalidate"})


_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fediverse Bridge</title>
<link rel="icon" href="/widget/icon">
<style>
  :root {
    --bg: #ffffff; --card: #ffffff; --border: #e1e6ec;
    --text: #1b1d22; --muted: #656d77; --muted2: #818a95;
    --accent: #007a61; --accent-text: #ffffff; --hover: #f0f2f5;
    --danger: #dc2626; --danger-bg: #fef2f2; --ok-bg: #f0fdf4; --ok-text: #15803d;
    --radius-card: 12px; --radius-inner: 8px; --radius-chip: 6px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101317; --card: #101317; --border: #26282d; --text: #ebeef2;
      --muted: #808994; --muted2: #656c76; --accent: #129a78; --accent-text: #101317;
      --hover: #1d1f24; --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
    }
  }
  :root[data-theme="dark"] {
    --bg: #101317; --card: #101317; --border: #26282d; --text: #ebeef2;
    --muted: #808994; --muted2: #656c76; --accent: #129a78; --accent-text: #101317;
    --hover: #1d1f24; --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --card: #ffffff; --border: #e1e6ec; --text: #1b1d22;
    --muted: #656d77; --muted2: #818a95; --accent: #007a61; --accent-text: #ffffff;
    --hover: #f0f2f5; --danger: #dc2626; --danger-bg: #fef2f2; --ok-bg: #f0fdf4; --ok-text: #15803d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh;
  }
  #content { padding: 16px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-card);
    padding: 16px; margin-bottom: 12px;
  }
  h1 { font-size: 15px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 12px; word-break: break-all; }
  .badge {
    display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: var(--accent); color: var(--accent-text); font-weight: 600;
  }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  .row:last-child { margin-bottom: 0; }
  button {
    font: inherit; font-size: 13px; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: var(--radius-inner); padding: 8px 14px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  button.danger { color: var(--danger); border-color: var(--danger); }
  button:disabled { opacity: .5; cursor: default; }
  button:not(:disabled):hover { background: var(--hover); }
  button.primary:not(:disabled):hover { filter: brightness(1.08); }
  button:not(:disabled):active { transform: translateY(1px); }
  input[type=text], input[type=number] {
    font: inherit; font-size: 13px; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: var(--radius-inner); padding: 8px 10px; flex: 1; min-width: 120px;
  }
  .toast {
    position: fixed; left: 16px; right: 16px; bottom: 16px; padding: 10px 14px; border-radius: var(--radius-inner);
    font-size: 13px; font-weight: 600; display: none; z-index: 10;
  }
  .toast.ok { background: var(--ok-bg); color: var(--ok-text); display: block; }
  .toast.err { background: var(--danger-bg); color: var(--danger); display: block; }
  .kv { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; }
  .kv span:first-child { color: var(--muted); }
  .list { display: flex; flex-direction: column; gap: 6px; }
  .list-item {
    display: flex; justify-content: space-between; align-items: center; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: var(--radius-inner); font-size: 13px;
  }
  .muted { color: var(--muted); }
  .spinner { text-align: center; color: var(--muted); padding: 40px 0; }
  a { color: var(--accent); }
  .toggle-on { background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-bg); }

  /* -- app shell: collapsible left sidebar, Haven Desktop style ----------- */
  #shell { display: flex; min-height: 100vh; align-items: stretch; }
  #sidebar {
    display: flex; flex-direction: column; gap: 2px; padding: 12px 8px; width: 176px; flex-shrink: 0;
    border-right: 1px solid var(--border); background: var(--card);
    position: sticky; top: 0; align-self: flex-start; height: 100vh; overflow-y: auto;
    transition: width .15s ease;
  }
  #shell.collapsed #sidebar { width: 56px; }
  .nav-item {
    display: flex; align-items: center; gap: 10px; font: inherit; font-size: 13px; font-weight: 600;
    color: var(--muted); background: none; border: none; border-radius: var(--radius-inner);
    padding: 10px; cursor: pointer; white-space: nowrap; text-align: left; width: 100%; overflow: hidden;
  }
  .nav-item:hover { background: var(--hover); color: var(--text); }
  .nav-item.active { background: var(--hover); color: var(--accent); }
  .nav-item .icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-size: 16px; }
  .nav-item .label { overflow: hidden; text-overflow: ellipsis; }
  #shell.collapsed .nav-item .label { display: none; }
  #shell.collapsed .nav-item { justify-content: center; }
  #sidebar-toggle { margin-top: auto; }
  #content { padding: 16px; flex: 1; min-width: 0; max-width: 640px; }
  .placeholder { text-align: center; color: var(--muted); padding: 48px 16px; font-size: 13px; }

  /* -- post cards (timeline) ----------------------------------------------- */
  .post-card { display: flex; gap: 12px; padding: 16px; }
  .post-card + .post-card { border-top: 1px solid var(--border); }
  .avatar { width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; background: var(--hover); object-fit: cover; }
  .avatar.sm { width: 24px; height: 24px; }
  .post-body { flex: 1; min-width: 0; }
  .post-header { display: flex; align-items: baseline; gap: 6px; font-size: 13px; }
  .post-header .name { font-weight: 600; color: var(--text); }
  .post-header .handle, .post-header .time { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .post-header .time { margin-left: auto; flex-shrink: 0; }
  .boosted-by { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .post-content { font-size: 15px; line-height: 1.5; margin: 4px 0; overflow-wrap: break-word; }
  .post-content p { margin: 0 0 8px; }
  .post-content p:last-child { margin-bottom: 0; }
  .post-content a { color: var(--accent); }
  .post-media { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 8px 0; border-radius: var(--radius-inner); overflow: hidden; }
  .post-media img { width: 100%; max-height: 300px; object-fit: cover; display: block; background: var(--hover); }
  .post-actions { display: flex; gap: 4px; margin-top: 8px; }
  .post-actions button {
    border: none; background: none; padding: 6px 8px; font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 4px; border-radius: var(--radius-chip);
  }
  .post-actions button:hover { background: var(--hover); color: var(--text); }
  .post-actions button.imported { color: var(--ok-text); }
  .skel { background: var(--border); opacity: .5; border-radius: 4px; height: 12px; }
  .skel-card { display: flex; gap: 12px; padding: 16px; }
  .skel-card + .skel-card { border-top: 1px solid var(--border); }
  .skel-lines { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .account-card { display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-inner); }
  .account-card .meta { flex: 1; min-width: 0; }
  .account-card .name { font-weight: 600; font-size: 13px; }
  .account-card .handle { color: var(--muted); font-size: 12px; }
  .scope-toggle { display: flex; gap: 2px; background: var(--hover); border-radius: var(--radius-inner); padding: 2px; }
  .scope-toggle button { border: none; background: none; }
  .scope-toggle button.active { background: var(--card); box-shadow: 0 1px 2px rgba(0,0,0,.08); }
</style>
</head>
<body>
  <div id="shell">
    <div id="sidebar"></div>
    <div id="content"><div id="app"><div class="spinner">Connecting&hellip;</div></div></div>
  </div>
  <div id="toast" class="toast"></div>

<script>
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const widgetId = params.get("widgetId") || "";
  const roomId = params.get("matrix_room_id") || "";
  const theme = params.get("theme") || "";
  if (theme.indexOf("dark") !== -1) document.documentElement.setAttribute("data-theme", "dark");
  else if (theme.indexOf("light") !== -1) document.documentElement.setAttribute("data-theme", "light");

  // -- Matrix Widget API (postMessage) -- minimal hand-rolled client, just
  // enough to do the OpenID handshake. No other widget capability is ever
  // requested: every bridge action goes through our own backend API
  // (authenticated via the verified OpenID identity), not through the
  // widget's own room read/write capabilities.
  let reqCounter = 0;
  const pending = {};

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.widgetId && widgetId && msg.widgetId !== widgetId) return;

    if (msg.api === "fromWidget" && msg.requestId && pending[msg.requestId] && "response" in msg) {
      const resolve = pending[msg.requestId];
      delete pending[msg.requestId];
      resolve(msg.response);
      return;
    }
    if (msg.api === "toWidget" && msg.action === "capabilities") {
      window.parent.postMessage(Object.assign({}, msg, { response: { capabilities: [] } }), "*");
      return;
    }
    if (msg.api === "toWidget" && msg.action === "openid_credentials") {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
      if (openIdResolve) {
        const resolve = openIdResolve;
        openIdResolve = null;
        resolve(msg.data);
      }
      return;
    }
    if (msg.api === "toWidget" && msg.requestId) {
      // Acknowledge anything else we don't specifically handle so the
      // client doesn't sit waiting on a reply that'll never come.
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
    }
  });

  function sendFromWidget(action, data) {
    return new Promise((resolve) => {
      const requestId = "req-" + (reqCounter++) + "-" + Date.now();
      pending[requestId] = resolve;
      window.parent.postMessage({ api: "fromWidget", widgetId, requestId, action, data: data || {} }, "*");
    });
  }

  let openIdResolve = null;

  async function getOpenIdToken() {
    const first = await sendFromWidget("get_openid", {});
    if (first && first.state === "allowed") return first;
    if (first && first.state === "blocked") throw new Error("This client denied the identity request.");
    // state === "request" -- the client is asking the user now; the
    // actual credentials arrive later as an unsolicited toWidget message.
    return new Promise((resolve, reject) => {
      openIdResolve = resolve;
      setTimeout(() => {
        if (openIdResolve) { openIdResolve = null; reject(new Error("Timed out waiting for approval.")); }
      }, 60000);
    });
  }

  // Also send content_loaded, per the widget API convention, so clients
  // that gate anything on it (loading spinners, etc.) know we're up.
  sendFromWidget("content_loaded", {});

  // -- bridge backend API -----------------------------------------------

  let sessionId = null;

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign(opts.raw ? {} : { "Content-Type": "application/json" }, opts.headers || {});
    if (sessionId) headers["Authorization"] = "Bearer " + sessionId;
    const body = opts.raw ? opts.raw : (opts.body ? JSON.stringify(opts.body) : undefined);
    const res = await fetch(path, { method: opts.method || "GET", headers, body });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }

  function toast(message, ok) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = "toast"; }, 4000);
  }

  async function doAction(path, body, busyBtn) {
    if (busyBtn) busyBtn.disabled = true;
    try {
      await api(path, { method: "POST", body: Object.assign({ room_id: roomId }, body || {}) });
      toast("Done.", true);
      await loadContext();
    } catch (e) {
      toast(e.message, false);
    } finally {
      if (busyBtn) busyBtn.disabled = false;
    }
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else if (k.indexOf("on") === 0) node.addEventListener(k.slice(2), attrs[k]);
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  let ctx = null;
  const NAV_ITEMS = [
    { id: "home", label: "Home", icon: "\u{1F3E0}" },
    { id: "room", label: "This Room", icon: "\u{1F9E9}" },
    { id: "identity", label: "Identity", icon: "\u{1F464}" },
    { id: "publish", label: "Publish", icon: "\u{1F4E1}" },
    { id: "admin", label: "Admin", icon: "\u{1F6E0}️" },
  ];
  let currentTab = "home";
  let sidebarCollapsed = (function () {
    try { return localStorage.getItem("fediverse-bridge-sidebar-collapsed") === "1"; } catch (e) { return false; }
  })();

  function renderSidebar() {
    const shell = document.getElementById("shell");
    shell.classList.toggle("collapsed", sidebarCollapsed);
    const bar = document.getElementById("sidebar");
    bar.innerHTML = "";
    if (!ctx) return;
    NAV_ITEMS.forEach((t) => {
      if (t.id === "admin" && !ctx.is_admin) return;
      if (t.id === "publish" && !ctx.peertube_channels_enabled && !ctx.lemmy_community_hosting_enabled) return;
      bar.appendChild(el("button", {
        class: "nav-item" + (t.id === currentTab ? " active" : ""),
        title: t.label,
        onclick: () => { currentTab = t.id; render(); },
      }, [
        el("span", { class: "icon", text: t.icon }),
        el("span", { class: "label", text: t.label }),
      ]));
    });
    bar.appendChild(el("button", {
      id: "sidebar-toggle", class: "nav-item", title: sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar",
      onclick: () => {
        sidebarCollapsed = !sidebarCollapsed;
        try { localStorage.setItem("fediverse-bridge-sidebar-collapsed", sidebarCollapsed ? "1" : "0"); } catch (e) {}
        renderSidebar();
      },
    }, [
      el("span", { class: "icon", text: sidebarCollapsed ? "»" : "«" }),
      el("span", { class: "label", text: "Collapse" }),
    ]));
  }

  function placeholder(app, text) {
    app.appendChild(el("div", { class: "placeholder", text: text }));
  }

  function render() {
    renderSidebar();
    const app = document.getElementById("app");
    app.innerHTML = "";
    if (!ctx) return;

    if (currentTab === "home") renderHomeTimeline(app);
    else if (currentTab === "room") renderRoomTab(app);
    else if (currentTab === "identity") renderIdentityTab(app);
    else if (currentTab === "publish") placeholder(app, "Channel/community publishing is coming soon.");
    else if (currentTab === "admin") renderAdminTab(app);
  }

  // -- Home: fediverse timeline -------------------------------------------

  function timeAgo(iso) {
    if (!iso) return "";
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return "now";
    if (diff < 3600) return Math.floor(diff / 60) + "m";
    if (diff < 86400) return Math.floor(diff / 3600) + "h";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + "d";
    return new Date(iso).toLocaleDateString();
  }

  function renderSkeletonCard() {
    return el("div", { class: "skel-card" }, [
      el("div", { class: "avatar skel" }),
      el("div", { class: "skel-lines" }, [
        el("div", { class: "skel", style: "width:40%" }),
        el("div", { class: "skel", style: "width:90%" }),
        el("div", { class: "skel", style: "width:65%" }),
      ]),
    ]);
  }

  function importPost(post, btn, onDone) {
    if (btn) btn.disabled = true;
    api("/widget/api/import", { method: "POST", body: { url: post.uri, room_id: roomId } })
      .then(() => { toast("Imported.", true); post.already_imported = true; if (onDone) onDone(); })
      .catch((e) => { toast(e.message, false); })
      .finally(() => { if (btn) btn.disabled = false; });
  }

  function renderPostCard(post, onChange) {
    const children = [];
    if (post.boosted_by) {
      children.push(el("div", {
        class: "boosted-by",
        text: "\u{1F501} " + (post.boosted_by.display_name || post.boosted_by.handle) + " boosted",
      }));
    }
    const header = el("div", { class: "post-header" }, [
      el("span", { class: "name", text: post.author.display_name }),
      el("span", { class: "handle", text: post.author.handle }),
      el("span", { class: "time", text: timeAgo(post.created_at) }),
    ]);
    const content = el("div", { class: "post-content" });
    content.innerHTML = post.content_html || "";
    children.push(header, content);
    if (post.media && post.media.length) {
      children.push(el("div", { class: "post-media" }, post.media.slice(0, 4).map((m) =>
        el("img", { src: m.url, alt: m.description || "", loading: "lazy" })
      )));
    }
    const actions = el("div", { class: "post-actions" }, [
      el("button", { text: "↗ View", onclick: () => window.open(post.url, "_blank", "noopener") }),
    ]);
    if (post.already_imported) {
      actions.appendChild(el("button", {
        class: "imported", text: "✓ Imported",
        onclick: () => window.open("https://matrix.to/#/" + post.matrix_room_id, "_blank", "noopener"),
      }));
    } else {
      actions.appendChild(el("button", {
        text: "⬇ Import",
        onclick: (e) => importPost(post, e.target, onChange),
      }));
    }
    children.push(actions);
    return el("div", { class: "post-card" }, [
      el("img", { class: "avatar", src: post.author.avatar_url || "", alt: "" }),
      el("div", { class: "post-body" }, children),
    ]);
  }

  let homeState = null;

  function renderHomeTimeline(app) {
    if (!homeState) homeState = { mode: "home", domain: "", scope: "federated", cursor: "", homeCursorMap: "", posts: [], loading: false, done: false, unsupported: false };
    const state = homeState;

    const searchInput = el("input", { type: "text", placeholder: "Search @user@instance.org, or a name" });
    const searchResults = el("div", { class: "list" });
    const domainInput = el("input", { type: "text", value: state.domain, placeholder: "instance.example (blank = your default feed)" });

    function scopeBtn(label, value) {
      return el("button", {
        class: state.scope === value ? "active" : "", text: label,
        onclick: () => { state.scope = value; render(); },
      });
    }

    const controlsChildren = [
      el("h2", { text: "Browse the fediverse" }),
      el("div", { class: "row" }, [
        searchInput,
        primaryButton("Search", async () => {
          const q = searchInput.value.trim();
          if (!q) return;
          searchResults.innerHTML = "";
          try {
            const data = await api("/widget/api/browse/search?q=" + encodeURIComponent(q) + "&domain=" + encodeURIComponent(state.domain || ctx.domain));
            if (!data.accounts.length) { searchResults.appendChild(el("div", { class: "muted", text: "No accounts found." })); return; }
            data.accounts.forEach((a) => {
              searchResults.appendChild(el("div", { class: "account-card" }, [
                el("img", { class: "avatar sm", src: a.avatar_url || "", alt: "" }),
                el("div", { class: "meta" }, [
                  el("div", { class: "name", text: a.display_name }),
                  el("div", { class: "handle", text: a.handle }),
                ]),
                plainButton("Follow", (e) => doAction("/widget/api/follow", { handle: a.handle }, e.target)),
              ]));
            });
          } catch (e) { toast(e.message, false); }
        }),
      ]),
      searchResults,
      el("div", { class: "row" }, [
        domainInput,
        el("div", { class: "scope-toggle" }, [scopeBtn("Local", "local"), scopeBtn("Federated", "federated")]),
        primaryButton("Browse", () => {
          const d = domainInput.value.trim().replace(/^https?:\/\//, "").replace(/\/$/, "");
          state.mode = d ? "instance" : "home";
          state.domain = d;
          state.posts = []; state.cursor = ""; state.homeCursorMap = ""; state.done = false; state.unsupported = false;
          render();
        }),
      ]),
    ];
    if (state.mode === "instance") {
      controlsChildren.push(el("div", { class: "sub" }, [
        el("span", { text: "Browsing " + state.domain + " (" + state.scope + ") — " }),
        el("a", {
          href: "#", text: "back to your default feed",
          onclick: (e) => { e.preventDefault(); state.mode = "home"; state.domain = ""; state.posts = []; state.cursor = ""; state.homeCursorMap = ""; state.done = false; state.unsupported = false; render(); },
        }),
      ]));
    }
    app.appendChild(el("div", { class: "card" }, controlsChildren));

    const feed = el("div", { class: "card" });
    app.appendChild(feed);
    const sentinel = el("div");
    app.appendChild(sentinel);

    function renderFeedList() {
      feed.innerHTML = "";
      if (!state.posts.length && !state.loading) {
        feed.appendChild(el("div", {
          class: "placeholder",
          text: state.unsupported
            ? "Browsing isn't available there (it doesn't run Mastodon-compatible software, or public preview is turned off)."
            : "Nothing to show yet.",
        }));
        return;
      }
      state.posts.forEach((p) => feed.appendChild(renderPostCard(p, renderFeedList)));
      if (state.loading) { for (let i = 0; i < 3; i++) feed.appendChild(renderSkeletonCard()); }
    }

    async function loadMore() {
      if (state.loading || state.done) return;
      state.loading = true;
      renderFeedList();
      try {
        let data;
        if (state.mode === "home") {
          data = await api("/widget/api/browse/home?max_id_map=" + encodeURIComponent(state.homeCursorMap || ""));
          state.homeCursorMap = data.next_max_id_map || "";
          state.done = !data.next_max_id_map;
          state.unsupported = !data.supported;
        } else {
          data = await api(
            "/widget/api/browse/timeline?domain=" + encodeURIComponent(state.domain) + "&scope=" + state.scope +
            (state.cursor ? "&max_id=" + encodeURIComponent(state.cursor) : "")
          );
          state.cursor = data.next_max_id || "";
          state.done = !data.next_max_id;
          state.unsupported = !data.supported;
        }
        state.posts = state.posts.concat(data.posts);
      } catch (e) {
        state.done = true;
      } finally {
        state.loading = false;
        renderFeedList();
      }
    }

    renderFeedList();
    if (!state.posts.length) loadMore();

    if (window.__timelineObserver) window.__timelineObserver.disconnect();
    window.__timelineObserver = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMore(); });
    window.__timelineObserver.observe(sentinel);
  }

  // -- Identity -------------------------------------------------------------

  function renderIdentityTab(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [el("h2", { text: "Your identities" }), list]));

    api("/widget/api/identities").then((data) => {
      list.innerHTML = "";
      if (!data.identities.length) {
        list.appendChild(el("div", { class: "muted", text: "No identity yet." }));
        app.appendChild(el("div", { class: "card" }, [
          primaryButton("Create my profile", (e) => doAction("/widget/api/create_profile", {}, e.target)),
        ]));
        return;
      }
      data.identities.forEach((idn) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("span", { text: (idn.display_name || idn.handle) + (idn.is_active ? "  • active" : "") }),
          idn.is_active ? null : plainButton("Make active", (e) => doAction("/widget/api/identities/switch", { username: idn.username }, e.target)),
        ].filter(Boolean)));
      });
      const active = data.identities.find((i) => i.is_active) || data.identities[0];
      api("/widget/api/identities/settings?username=" + encodeURIComponent(active.username)).then((s) => {
        app.appendChild(el("div", { class: "card" }, [
          el("h2", { text: "Settings for " + s.handle }),
          el("div", { class: "kv" }, [el("span", { text: "Display name" }), el("span", { text: s.display_name || "—" })]),
          el("div", { class: "sub" }, [el("a", { href: s.public_url, target: "_blank", text: "View public profile →" })]),
          el("div", { class: "row" }, [
            toggleButton(s.hide_followers ? "Followers: hidden" : "Followers: visible", s.hide_followers, (e) =>
              doAction("/widget/api/visibility", { which: "followers", hidden: !s.hide_followers, username: s.username }, e.target)),
            toggleButton(s.hide_following ? "Following: hidden" : "Following: visible", s.hide_following, (e) =>
              doAction("/widget/api/visibility", { which: "following", hidden: !s.hide_following, username: s.username }, e.target)),
            toggleButton(s.discoverable ? "Discoverable" : "Not discoverable", s.discoverable, (e) =>
              doAction("/widget/api/visibility", { which: "discoverable", value: !s.discoverable, username: s.username }, e.target)),
          ]),
        ]));
      }).catch(() => {});
      if (ctx.custom_identities_enabled) {
        app.appendChild(el("div", { class: "card" }, [
          el("h2", { text: "Add another identity" }),
          el("div", { class: "sub", text: "Only works inside a room you want to become that identity's own Profile Room -- switch to the \"This Room\" tab there and use \"Create profile\"." }),
        ]));
      }
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Account recovery" }),
        el("div", { class: "sub", text: "Locked out of your Profile Room or Space (e.g. after a Matrix account deactivation force-left you)? Re-invite yourself." }),
        el("div", { class: "row" }, [
          plainButton("Rejoin my rooms", (e) => doAction("/widget/api/rejoin", {}, e.target)),
        ]),
      ]));

      const leaveCard = el("div", { class: "card" }, [
        el("h2", { text: "Leave unfollowed rooms" }),
        el("div", { class: "sub", text: "Remove yourself from every Remote User Room for an account none of your identities follow." }),
        el("div", { class: "row" }, [
          plainButton("Preview", async (e) => {
            e.target.disabled = true;
            try {
              const data = await api("/widget/api/leave_unfollowed/preview");
              const old = leaveCard.querySelector("#leave-unfollowed-result");
              if (old) old.remove();
              const result = el("div", { id: "leave-unfollowed-result" });
              if (data.unreliable) {
                result.appendChild(el("div", { class: "muted", text: "Couldn't reliably check right now -- try again shortly." }));
              } else if (!data.rooms.length) {
                result.appendChild(el("div", { class: "muted", text: "Nothing to leave." }));
              } else {
                const shown = data.rooms.slice(0, 10);
                result.appendChild(el("div", { class: "list" }, shown.map((r) =>
                  el("div", { class: "list-item" }, [el("span", { text: r.handle })])
                )));
                if (data.rooms.length > shown.length) {
                  result.appendChild(el("div", { class: "muted", text: "...and " + (data.rooms.length - shown.length) + " more" }));
                }
                result.appendChild(el("div", { class: "row" }, [
                  dangerButton("Confirm: leave " + data.rooms.length + " room" + (data.rooms.length === 1 ? "" : "s"), (e2) => {
                    if (confirm("Remove yourself from " + data.rooms.length + " room(s)? This cannot be undone.")) {
                      doAction("/widget/api/leave_unfollowed/execute", {}, e2.target);
                    }
                  }),
                ]));
              }
              leaveCard.appendChild(result);
            } catch (err) {
              toast(err.message, false);
            } finally {
              e.target.disabled = false;
            }
          }),
        ]),
      ]);
      app.appendChild(leaveCard);
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  // -- Admin ------------------------------------------------------------------

  function renderAdminTab(app) {
    const RULE_TYPES = ["mxid", "room", "homeserver"];
    const typeSelect = el("select", { style: "font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;" },
      RULE_TYPES.map((t) => el("option", { value: t, text: t })));
    const valueInput = el("input", { type: "text", placeholder: "@user:example.org, !room:example.org, or example.org" });

    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);

    function loadGrants() {
      api("/widget/api/admin/grants").then((data) => {
        list.innerHTML = "";
        if (!data.grants.length) {
          list.appendChild(el("div", { class: "muted", text: "No grants yet — mode: " + data.mode + "." }));
          return;
        }
        data.grants.forEach((g) => {
          list.appendChild(el("div", { class: "list-item" }, [
            el("span", { text: g.rule_type + ": " + g.value }),
            plainButton("Revoke", (e) =>
              doAction("/widget/api/admin/disallow", { rule_type: g.rule_type, value: g.value }, e.target).then(loadGrants)),
          ]));
        });
      }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }

    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Third-party access" }),
      el("div", { class: "sub", text: "Let a user, room, or whole homeserver other than your own use this bridge." }),
      el("div", { class: "row" }, [
        typeSelect, valueInput,
        primaryButton("Grant", (e) => {
          if (!valueInput.value.trim()) return;
          doAction("/widget/api/admin/allow", { rule_type: typeSelect.value, value: valueInput.value.trim() }, e.target)
            .then(() => { valueInput.value = ""; loadGrants(); });
        }),
      ]),
      list,
    ]));
    loadGrants();
  }

  // -- This Room (room-kind actions, unchanged from before) -----------------

  function renderRoomTab(app) {
    app.appendChild(el("div", { class: "card" }, [
      el("h1", {}, [
        el("span", { text: "\u{1F30D} Fediverse Bridge" }),
        el("span", { class: "badge", text: kindLabel(ctx.room.kind) }),
      ]),
      el("div", { class: "sub", text: ctx.matrix_user_id + (ctx.is_admin ? "  ·  admin" : "") }),
    ]));

    if (!ctx.has_profile) {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Get started" }),
        el("div", { class: "sub", text: "You don't have a fediverse profile yet." }),
        el("div", { class: "row" }, [
          primaryButton("Create my profile", (e) => doAction("/widget/api/create_profile", {}, e.target)),
        ]),
      ]));
    }

    const kind = ctx.room.kind;
    if (kind === "profile") renderProfile(app);
    else if (kind === "remote_actor") renderRemoteActor(app);
    else if (kind === "dm" || kind === "chat") renderDmChat(app);
    else if (kind === "notifications") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Notifications" }),
        el("div", { class: "sub", text: "Your Fediverse Notifications DM with the bridge bot -- nothing to configure here." }),
      ]));
    } else {
      app.appendChild(el("div", { class: "card" }, [
        el("div", { class: "sub", text: "This room isn't one the bridge manages, or I can't tell yet." }),
      ]));
    }

    renderFollowing(app);
    renderImportPost(app);
    if (ctx.has_profile) {
      // Follow/block/mute-by-handle and follows-import work from ANY
      // room, same as their ";follow"/";block"/";mute"/";import follows"
      // command counterparts -- only the banner (which needs to run
      // inside your own Profile Room specifically, per _handle_banner)
      // stays scoped to renderProfile's is_owner branch.
      renderManageAccount(app);
      renderImportFollows(app);
    }

    app.appendChild(el("div", { class: "card" }, [
      el("div", { class: "sub", text: "Every button here does the same thing as its " + ctx.command_prefix + "command -- both always work." }),
    ]));
  }

  function renderImportPost(app) {
    const input = el("input", { type: "text", placeholder: "https://instance.example/@user/12345" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Import a post" }),
      el("div", { class: "sub", text: "Fetch and mirror a single fediverse post by its URL, regardless of whether you follow its author." }),
      el("div", { class: "row" }, [
        input,
        primaryButton("Import", (e) => {
          if (!input.value) return;
          doAction("/widget/api/import", { url: input.value }, e.target).then(() => { input.value = ""; });
        }),
      ]),
    ]));
  }

  function kindLabel(kind) {
    return {
      profile: "Your profile", remote_actor: "Fediverse account", dm: "Direct message",
      chat: "Chat", notifications: "Notifications", unmanaged: "Unmanaged",
    }[kind] || kind;
  }

  function primaryButton(label, handler) {
    return el("button", { class: "primary", onclick: handler, text: label });
  }
  function plainButton(label, handler) {
    return el("button", { onclick: handler, text: label });
  }
  function dangerButton(label, handler) {
    return el("button", { class: "danger", onclick: handler, text: label });
  }
  function toggleButton(label, on, handler) {
    return el("button", { class: on ? "toggle-on" : "", onclick: handler, text: label });
  }

  function renderProfile(app) {
    const r = ctx.room;
    const card = el("div", { class: "card" }, [
      el("h2", { text: "Your profile" }),
      el("div", { class: "kv" }, [el("span", { text: "Handle" }), el("span", { text: "@" + r.username + "@" + ctx.domain })]),
      el("div", { class: "kv" }, [el("span", { text: "Display name" }), el("span", { text: r.display_name || "—" })]),
    ]);
    if (r.public_url) {
      card.appendChild(el("div", { class: "sub" }, [
        el("a", { href: r.public_url, target: "_blank", text: "View public profile →" }),
      ]));
    }
    if (r.is_owner) {
      card.appendChild(el("div", { class: "row" }, [
        toggleButton(r.hide_followers ? "Followers: hidden" : "Followers: visible", r.hide_followers, (e) =>
          doAction("/widget/api/visibility", { which: "followers", hidden: !r.hide_followers }, e.target)),
        toggleButton(r.hide_following ? "Following: hidden" : "Following: visible", r.hide_following, (e) =>
          doAction("/widget/api/visibility", { which: "following", hidden: !r.hide_following }, e.target)),
      ]));
      card.appendChild(el("div", { class: "row" }, [
        plainButton("Replace this room", (e) => {
          if (confirm("Create a fresh replacement room and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
        }),
        dangerButton("Unlink profile", (e) => {
          if (confirm("Detach this room from your identity? You can relink later.")) doAction("/widget/api/unlink_profile", {}, e.target);
        }),
        dangerButton("Delete profile", (e) => {
          if (confirm("Permanently delete your fediverse identity and notify followers? This cannot be undone.")) doAction("/widget/api/delete_profile", {}, e.target);
        }),
      ]));
    } else {
      card.appendChild(el("div", { class: "row" }, [
        plainButton("Link this room to my profile", (e) => doAction("/widget/api/link_profile", {}, e.target)),
      ]));
    }
    app.appendChild(card);

    if (r.is_owner) {
      renderBanner(app);
    }
  }

  function renderManageAccount(app) {
    const input = el("input", { type: "text", placeholder: "@user@instance.org" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Follow, block, or mute an account" }),
      el("div", { class: "row" }, [input]),
      el("div", { class: "row" }, [
        primaryButton("Follow", (e) => { if (input.value) doAction("/widget/api/follow", { handle: input.value }, e.target); }),
        dangerButton("Block", (e) => {
          if (input.value && confirm("Block " + input.value + "? This cuts any follow and kicks you from their rooms.")) doAction("/widget/api/block", { handle: input.value }, e.target);
        }),
        plainButton("Mute", (e) => { if (input.value) doAction("/widget/api/mute", { handle: input.value }, e.target); }),
      ]),
    ]));
  }

  function renderBanner(app) {
    const fileInput = el("input", { type: "file", accept: "image/*" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Profile banner" }),
      el("div", { class: "row" }, [fileInput]),
      el("div", { class: "row" }, [
        primaryButton("Upload banner", async (e) => {
          const file = fileInput.files[0];
          if (!file) { toast("Choose an image first.", false); return; }
          e.target.disabled = true;
          try {
            await api("/widget/api/upload_banner?room_id=" + encodeURIComponent(roomId), {
              method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, raw: file,
            });
            toast("Banner updated.", true);
            await loadContext();
          } catch (err) {
            toast(err.message, false);
          } finally {
            e.target.disabled = false;
          }
        }),
      ]),
    ]));
  }

  function renderImportFollows(app) {
    const fileInput = el("input", { type: "file", accept: ".csv,.txt" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Import a follows list" }),
      el("div", { class: "sub", text: "A Pleroma/Akkoma export (one @user@instance.org per line) or a Mastodon follows CSV." }),
      el("div", { class: "row" }, [fileInput]),
      el("div", { class: "row" }, [
        primaryButton("Import follows", async (e) => {
          const file = fileInput.files[0];
          if (!file) { toast("Choose a file first.", false); return; }
          e.target.disabled = true;
          try {
            const result = await api("/widget/api/import_follows?room_id=" + encodeURIComponent(roomId), {
              method: "POST", headers: { "Content-Type": "text/plain" }, raw: file,
            });
            toast("Importing " + result.count + " follows -- a summary will be posted here when it's done.", true);
          } catch (err) {
            toast(err.message, false);
          } finally {
            e.target.disabled = false;
          }
        }),
      ]),
    ]));
  }

  function renderRemoteActor(app) {
    const r = ctx.room;
    const card = el("div", { class: "card" }, [
      el("h2", { text: r.display_name || "Fediverse account" }),
      el("div", { class: "sub", text: r.actor_id }),
    ]);
    if (!ctx.has_profile) { app.appendChild(card); return; }
    card.appendChild(el("div", { class: "row" }, [
      r.is_following
        ? dangerButton("Unfollow", (e) => doAction("/widget/api/unfollow", {}, e.target))
        : primaryButton("Follow", (e) => doAction("/widget/api/follow", {}, e.target)),
      plainButton("DM", (e) => doAction("/widget/api/dm", {}, e.target)),
      plainButton("Chat", (e) => doAction("/widget/api/chat", {}, e.target)),
    ]));
    card.appendChild(el("div", { class: "row" }, [
      toggleButton(r.is_muted ? "Muted" : "Mute", r.is_muted, (e) =>
        doAction(r.is_muted ? "/widget/api/unmute" : "/widget/api/mute", {}, e.target)),
      r.is_blocked
        ? plainButton("Unblock", (e) => doAction("/widget/api/unblock", {}, e.target))
        : dangerButton("Block", (e) => {
            if (confirm("Block this account? This cuts any follow and kicks you from their rooms.")) doAction("/widget/api/block", {}, e.target);
          }),
    ]));

    const backfillRow = el("div", { class: "row" }, [
      primaryButton("Backfill latest " + ctx.backfill_default_count + " posts", (e) => doAction("/widget/api/backfill", {}, e.target)),
    ]);
    if (ctx.is_admin) {
      const countInput = el("input", { type: "number", min: "1", placeholder: "custom count" });
      backfillRow.appendChild(countInput);
      backfillRow.appendChild(plainButton("Backfill custom amount", (e) =>
        doAction("/widget/api/backfill", { count: parseInt(countInput.value || "0", 10) }, e.target)));
      backfillRow.appendChild(plainButton("Replace this room", (e) => {
        if (confirm("Create a fresh replacement room for this account and retire this one?")) doAction("/widget/api/replace_room", {}, e.target);
      }));
    }
    card.appendChild(backfillRow);
    app.appendChild(card);
  }

  function renderDmChat(app) {
    const r = ctx.room;
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: ctx.room.kind === "dm" ? "Direct message" : "Chat" }),
      el("div", { class: "sub", text: r.actor_id }),
      el("div", { class: "row" }, [
        plainButton("Mute", (e) => doAction("/widget/api/mute", {}, e.target)),
        dangerButton("Block", (e) => {
          if (confirm("Block this account?")) doAction("/widget/api/block", {}, e.target);
        }),
      ]),
    ]));
  }

  function renderFollowing(app) {
    if (!ctx.has_profile) return;
    const card = el("div", { class: "card" }, [el("h2", { text: "Following" })]);
    const list = el("div", { class: "list", id: "following-list" }, [el("div", { class: "muted", text: "Loading…" })]);
    card.appendChild(list);
    app.appendChild(card);
    api("/widget/api/following").then((data) => {
      list.innerHTML = "";
      if (!data.following.length) { list.appendChild(el("div", { class: "muted", text: "Not following anyone yet." })); return; }
      data.following.forEach((f) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("span", { text: f.display_name || f.actor_id }),
          plainButton("Unfollow", (e) => doAction("/widget/api/unfollow", { handle: f.handle }, e.target)),
        ]));
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  async function loadContext() {
    ctx = await api("/widget/api/context?room_id=" + encodeURIComponent(roomId));
    render();
  }

  async function start() {
    try {
      const creds = await getOpenIdToken();
      const session = await api("/widget/api/session", {
        method: "POST",
        body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
      });
      sessionId = session.session_id;
      await loadContext();
    } catch (e) {
      document.getElementById("app").innerHTML = "";
      document.getElementById("app").appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Couldn't connect" }),
        el("div", { class: "sub", text: e.message }),
      ]));
    }
  }

  start();
})();
</script>
</body>
</html>
"""
