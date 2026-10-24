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

from bridge.activitypub.sanitize import plain_text_to_note_html
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
    followers = await repository.list_followers(actor.username)
    following = await repository.list_following(actor.username)
    return JSONResponse(
        {
            "username": actor.username,
            "display_name": actor.display_name,
            # actor.summary is stored as plain text (the Profile Room's
            # topic, verbatim) -- the real AP actor document only ever
            # gets an HTML version of it via plain_text_to_note_html at
            # serve time (see bridge.note_mirroring's own actor-building
            # code), never pre-rendered into the stored field itself.
            # Reuse that exact conversion here so the widget's bio
            # rendering matches what a remote viewer actually sees.
            "summary": plain_text_to_note_html(actor.summary) if actor.summary else actor.summary,
            "handle": f"@{actor.username}@{config.bridge.domain}",
            "actor_id": actor_url(config.bridge.public_base_url, actor.username),
            "icon_url": actor.icon_url,
            "banner_url": actor.banner_url,
            "public_url": actor_url(config.bridge.public_base_url, actor.username),
            "room_id": actor.room_id,
            "followers_count": len(followers),
            "following_count": len(following),
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
  input[type=text], input[type=number], input[type=password], select {
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
  #content { padding: 16px; flex: 1; min-width: 0; max-width: 640px; }
  .placeholder { text-align: center; color: var(--muted); padding: 48px 16px; font-size: 13px; }

  /* -- post cards (timeline) ----------------------------------------------- */
  .post-card { display: flex; gap: 12px; padding: 16px; position: relative; }
  .post-card + .post-card { border-top: 1px solid var(--border); }
  .avatar { width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; background: var(--hover); object-fit: cover; }
  .avatar.sm { width: 24px; height: 24px; }
  .avatar-fallback {
    display: flex; align-items: center; justify-content: center; background: var(--accent); color: var(--accent-text);
    font-weight: 700; font-size: 1.1em;
  }
  .post-body { flex: 1; min-width: 0; }
  .post-header { display: flex; align-items: baseline; gap: 6px; font-size: 13px; }
  .post-header-left { display: flex; align-items: baseline; gap: 6px; min-width: 0; overflow: hidden; }
  .post-header-right { margin-left: auto; flex-shrink: 0; display: flex; align-items: center; gap: 2px; }
  .post-header .name { font-weight: 600; color: var(--text); }
  .post-header .handle, .post-header .time { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .view-icon-btn { border: none; background: none; color: var(--muted); font-size: 13px; padding: 0 0 0 4px; cursor: pointer; flex-shrink: 0; line-height: 1; }
  .view-icon-btn:hover { color: var(--text); }
  .post-timestamp-full { font-size: 12px; color: var(--muted); margin: 6px 0; }
  .boosted-by { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .boosted-by img.emoji, .post-header .name img.emoji { width: 1em; height: 1em; }
  .reply-to-line { font-size: 12px; color: var(--muted); margin-bottom: 4px; cursor: pointer; }
  /* A connecting line from this card's avatar down into the next card's --
     the ancestor chain leading up to the thread root, and a direct reply
     that the focused post's own author continued replying to. */
  .post-card.chain-link::after {
    content: ""; position: absolute; left: 35px; top: 56px; bottom: -1px; width: 2px; background: var(--border);
  }
  .reply-to-line:hover { color: var(--accent); }
  .post-content { font-size: 15px; line-height: 1.5; margin: 4px 0; overflow-wrap: break-word; }
  .post-content p { margin: 0 0 8px; }
  .post-content p:last-child { margin-bottom: 0; }
  .post-content a { color: var(--accent); }
  img.emoji { width: 1.2em; height: 1.2em; vertical-align: -0.2em; object-fit: contain; }
  .post-media { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 8px 0; border-radius: var(--radius-inner); overflow: hidden; }
  .post-media img { width: 100%; max-height: 300px; object-fit: cover; display: block; background: var(--hover); }
  .post-counts { display: flex; gap: 14px; margin-top: 8px; font-size: 12px; color: var(--muted); }
  .post-counts .count-item { display: flex; align-items: center; gap: 4px; }
  .post-reactions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .reaction-chip {
    display: inline-flex; align-items: center; gap: 3px; background: var(--hover); border-radius: var(--radius-chip);
    padding: 2px 6px; font-size: 12px; color: var(--muted);
  }
  .reaction-chip img.emoji { width: 14px; height: 14px; }
  .post-actions { display: flex; gap: 4px; margin-top: 8px; }
  .post-actions button {
    border: none; background: none; padding: 6px 8px; font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 4px; border-radius: var(--radius-chip);
  }
  .post-actions button:hover { background: var(--hover); color: var(--text); }
  .post-actions button.imported { color: var(--ok-text); }
  .post-card.open-thread { cursor: pointer; }
  .post-card.thread-ancestor { opacity: .8; }
  .post-card.thread-focus { background: var(--hover); border-radius: var(--radius-card); cursor: default; }
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

  /* -- sidebar identity card ------------------------------------------------ */
  .identity-card {
    display: flex; align-items: center; gap: 8px; padding: 8px; margin-top: auto;
    border-radius: var(--radius-inner); cursor: pointer; position: relative; width: 100%;
    border: none; background: none; text-align: left;
  }
  .identity-card:hover { background: var(--hover); }
  .identity-card .avatar { width: 28px; height: 28px; }
  .identity-meta { flex: 1; min-width: 0; overflow: hidden; }
  .identity-meta .name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .identity-meta .handle { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #shell.collapsed .identity-meta, #shell.collapsed .identity-dropdown-btn { display: none; }
  #shell.collapsed .identity-card { justify-content: center; }
  .identity-dropdown-btn {
    border: none; background: none; color: var(--muted); padding: 4px; cursor: pointer; font-size: 12px; flex-shrink: 0;
  }
  .identity-dropdown {
    position: absolute; bottom: 100%; left: 8px; right: 8px; margin-bottom: 4px;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-inner);
    box-shadow: 0 4px 16px rgba(0,0,0,.18); padding: 4px; z-index: 20;
  }
  #shell.collapsed .identity-dropdown { left: 4px; right: auto; width: 200px; }

  /* -- modals (following list, password login) ----------------------------- */
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center;
    justify-content: center; z-index: 50; padding: 16px;
  }
  .modal-backdrop .card { max-width: 480px; width: 100%; max-height: 85vh; overflow-y: auto; margin-bottom: 0; }
  .modal-close { border: none; background: none; color: var(--muted); font-size: 16px; padding: 4px 8px; cursor: pointer; }
  .clickable { cursor: pointer; }

  /* -- full-page profile (own identity or a browsed/followed account) ------ */
  .back-link {
    border: none; background: none; color: var(--text); font-weight: 600; font-size: 13px;
    cursor: pointer; padding: 4px 0 12px; display: block;
  }
  .back-link:hover { color: var(--accent); }
  .profile-page { max-width: 640px; }
  .profile-banner-wrap {
    position: relative; width: 100%; height: 180px; border-radius: var(--radius-card);
    overflow: hidden; background: var(--hover); margin-bottom: 44px;
  }
  .profile-banner-empty { background: linear-gradient(135deg, var(--hover), var(--border)); }
  .profile-banner-img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .banner-edit-btn {
    position: absolute; top: 10px; right: 10px; width: 32px; height: 32px; border-radius: 50%;
    border: none; background: rgba(0,0,0,.55); color: #fff; cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
  }
  .profile-header-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
  .profile-avatar-xl {
    width: 88px; height: 88px; font-size: 32px; border: 4px solid var(--bg);
    margin-top: -66px; position: relative; z-index: 1; flex-shrink: 0;
  }
  .profile-stats { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; padding-bottom: 8px; }
  .profile-stat { color: var(--muted); font-size: 13px; }
  .profile-name { font-size: 20px; font-weight: 700; margin: 4px 0 0; }
  .profile-handle { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; margin: 2px 0 10px; }
  .copy-btn { border: none; background: none; color: var(--muted); cursor: pointer; padding: 2px; font-size: 12px; }
  .copy-btn:hover { color: var(--text); }
  .profile-bio { font-size: 14px; line-height: 1.5; margin-bottom: 10px; }
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

  // An <img> for url, falling back to an initial-letter circle both when
  // url is empty/missing (the common case -- most accounts never set an
  // avatar) and when it's present but fails to load (dead link, blocked
  // host, ...), rather than a silently broken image either way.
  function avatarEl(url, label, extraClass, style, onclick) {
    const cls = "avatar" + (extraClass ? " " + extraClass : "");
    const initial = ((label || "").trim().charAt(0) || "?").toUpperCase();
    const attrs = { class: cls + " avatar-fallback", text: initial, style: style || "" };
    if (onclick) attrs.onclick = onclick;
    if (!url) return el("div", attrs);
    const imgAttrs = { class: cls, src: url, alt: "", style: style || "" };
    if (onclick) imgAttrs.onclick = onclick;
    const img = el("img", imgAttrs);
    img.addEventListener("error", () => { img.replaceWith(el("div", attrs)); }, { once: true });
    return img;
  }

  // A display name that may carry inline custom-emoji <img> tags (see
  // bridge/widget_browse.py's _display_name_html) -- renders the HTML
  // version when present, otherwise falls back to plain text exactly like
  // el()'s own "text" attribute would.
  function nameNode(tag, cls, plain, nameHtml, onclick) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (nameHtml) node.innerHTML = nameHtml; else node.textContent = plain;
    if (onclick) node.addEventListener("click", onclick);
    return node;
  }

  let ctx = null;
  const NAV_ITEMS = [
    { id: "home", label: "Home", icon: "\u{1F3E0}" },
    { id: "publish", label: "Publish", icon: "\u{1F4E1}" },
    { id: "settings", label: "Settings", icon: "\u{2699}️" },
    { id: "admin", label: "Admin", icon: "\u{1F6E0}️" },
  ];
  let currentTab = "home";
  // Non-null while a full-page profile (own or remote) is drilled into --
  // overrides currentTab's own content in render() without changing which
  // sidebar item is "active", so "Back" returns to exactly where you were.
  let profileView = null;
  // Same idea as profileView, for a post's thread (ancestors + the post +
  // its immediate replies). Mutually exclusive with profileView -- opening
  // one clears the other.
  let threadView = null;
  let sidebarCollapsed = (function () {
    try { return localStorage.getItem("fediverse-bridge-sidebar-collapsed") === "1"; } catch (e) { return false; }
  })();

  // -- modal ------------------------------------------------------------------

  function openModal(contentNode) {
    closeModal();
    const backdrop = el("div", {
      id: "modal-backdrop", class: "modal-backdrop",
      onclick: (e) => { if (e.target.id === "modal-backdrop") closeModal(); },
    }, [contentNode]);
    document.body.appendChild(backdrop);
  }
  function closeModal() {
    const existing = document.getElementById("modal-backdrop");
    if (existing) existing.remove();
  }

  // -- identity (sidebar card + "My Profile") --------------------------------

  let identityState = { identities: [], active: null };

  async function loadIdentityState() {
    if (!ctx || !ctx.has_profile) { identityState = { identities: [], active: null }; return; }
    const data = await api("/widget/api/identities");
    identityState.identities = data.identities;
    const active = data.identities.find((i) => i.is_active) || data.identities[0];
    identityState.active = active ? await api("/widget/api/identities/settings?username=" + encodeURIComponent(active.username)) : null;
  }

  let identityDropdownOpen = false;

  function renderIdentityCard(bar) {
    if (!ctx) return;
    if (!ctx.has_profile) {
      bar.appendChild(el("button", {
        class: "identity-card", title: "Create your fediverse profile",
        onclick: (e) => { e.stopPropagation(); createNewIdentity(""); },
      }, [
        el("span", { class: "icon", text: "\u{2795}" }),
        el("div", { class: "identity-meta" }, [el("div", { class: "name", text: "Create profile" })]),
      ]));
      return;
    }
    const active = identityState.active;
    const otherIdentities = identityState.identities.filter((i) => !i.is_active);
    // Nothing to put in the dropdown when there's only this one identity
    // and this bridge doesn't allow creating more -- showing the chevron
    // (and an empty popover when clicked) would just be a dead end.
    const hasDropdownContent = otherIdentities.length > 0 || ctx.custom_identities_enabled;
    const cardChildren = [
      avatarEl(active && active.icon_url, active && (active.display_name || active.handle)),
      el("div", { class: "identity-meta" }, [
        el("div", { class: "name", text: active ? (active.display_name || active.handle) : "…" }),
        el("div", { class: "handle muted", text: active ? active.handle : "" }),
      ]),
    ];
    if (hasDropdownContent) {
      cardChildren.push(el("span", {
        class: "identity-dropdown-btn", text: "⌄",
        onclick: (e) => { e.stopPropagation(); identityDropdownOpen = !identityDropdownOpen; renderSidebar(); },
      }));
    }
    const card = el("button", {
      class: "identity-card", title: active ? active.handle : "",
      onclick: () => { identityDropdownOpen = false; openMyProfile(); },
    }, cardChildren);
    if (hasDropdownContent && identityDropdownOpen) {
      const pop = el("div", { class: "identity-dropdown" });
      otherIdentities.forEach((i) => {
        pop.appendChild(el("button", {
          class: "nav-item", text: i.display_name || i.handle,
          onclick: () => {
            identityDropdownOpen = false;
            api("/widget/api/identities/switch", { method: "POST", body: { room_id: roomId, username: i.username } })
              .then(() => loadContext()).catch((e) => toast(e.message, false));
          },
        }));
      });
      if (ctx.custom_identities_enabled) {
        pop.appendChild(el("button", {
          class: "nav-item", text: "+ New identity",
          onclick: () => { identityDropdownOpen = false; createNewIdentity(); },
        }));
      }
      card.appendChild(pop);
    }
    bar.appendChild(card);
  }

  function createNewIdentity(promptOverride) {
    const needsId = ctx.has_profile; // the very first identity uses the bare form (your Matrix localpart)
    let customId = "";
    if (needsId) {
      customId = (promptOverride !== undefined ? promptOverride : prompt("Choose an id for your new identity (letters, numbers, underscores):")) || "";
      if (!customId.trim()) return;
    }
    api("/widget/api/create_profile", { method: "POST", body: { room_id: roomId, custom_id: customId.trim() } })
      .then(() => { toast("Identity created.", true); return loadContext(); })
      .catch((e) => toast(e.message, false));
  }

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
        class: "nav-item" + (!profileView && !threadView && t.id === currentTab ? " active" : ""),
        title: t.label,
        onclick: () => { currentTab = t.id; profileView = null; threadView = null; render(); },
      }, [
        el("span", { class: "icon", text: t.icon }),
        el("span", { class: "label", text: t.label }),
      ]));
    });
    renderIdentityCard(bar);
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

    if (profileView) { renderProfilePage(app, profileView); return; }
    if (threadView) { renderThreadPage(app, threadView); return; }
    if (currentTab === "home") renderHomeTimeline(app);
    else if (currentTab === "publish") renderPublishTab(app);
    else if (currentTab === "settings") renderSettingsTab(app);
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

  function fullDateTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: "long", timeStyle: "short" });
    } catch (e) {
      return new Date(iso).toString();
    }
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

  function renderPostCard(post, onChange, opts) {
    const focused = !!(opts && opts.focused);
    const children = [];
    if (post.boosted_by) {
      const b = post.boosted_by;
      const line = document.createElement("div");
      line.className = "boosted-by" + (b.actor_id ? " clickable" : "");
      line.appendChild(document.createTextNode("\u{1F501} "));
      line.appendChild(nameNode("span", "", b.display_name || b.handle, b.display_name_html, null));
      line.appendChild(document.createTextNode(" boosted"));
      if (b.actor_id) line.addEventListener("click", (e) => { e.stopPropagation(); openAccountProfile(b.actor_id); });
      children.push(line);
    }
    if (post.in_reply_to) {
      const hint = post.reply_hint_display_name || post.reply_hint_handle;
      const replyLine = el("div", {
        class: "reply-to-line",
        text: "↩ Reply to " + (hint ? hint + "'s post" : "a post"),
        onclick: (e) => { e.stopPropagation(); openThreadView(post.uri); },
      });
      children.push(replyLine);
    }
    const hasAuthorProfile = !!post.author.actor_id;
    const openAuthor = (e) => { if (e) e.stopPropagation(); if (hasAuthorProfile) openAccountProfile(post.author.actor_id); };
    const headerLeft = el("div", { class: "post-header-left" }, [
      nameNode("span", "name" + (hasAuthorProfile ? " clickable" : ""), post.author.display_name, post.author.display_name_html, openAuthor),
      el("span", { class: "handle" + (hasAuthorProfile ? " clickable" : ""), text: post.author.handle, onclick: openAuthor }),
    ]);
    const headerRightChildren = [];
    if (!focused) {
      headerRightChildren.push(el("span", { class: "time", text: timeAgo(post.created_at), title: fullDateTime(post.created_at) }));
    }
    headerRightChildren.push(el("button", {
      class: "view-icon-btn", text: "↗", title: "Open on " + (post.instance || "their instance"),
      onclick: (e) => { e.stopPropagation(); window.open(post.url, "_blank", "noopener"); },
    }));
    const header = el("div", { class: "post-header" }, [headerLeft, el("div", { class: "post-header-right" }, headerRightChildren)]);
    const content = el("div", { class: "post-content" });
    content.innerHTML = post.content_html || "";
    children.push(header, content);
    if (post.media && post.media.length) {
      children.push(el("div", { class: "post-media" }, post.media.slice(0, 4).map((m) =>
        el("img", { src: m.url, alt: m.description || "", loading: "lazy" })
      )));
    }
    if (focused) {
      children.push(el("div", { class: "post-timestamp-full", text: fullDateTime(post.created_at) }));
    }
    const counts = post.counts || { replies: 0, reblogs: 0, favourites: 0 };
    if (counts.replies || counts.reblogs || counts.favourites) {
      children.push(el("div", { class: "post-counts" }, [
        el("span", { class: "count-item", text: "\u{1F4AC} " + counts.replies }),
        el("span", { class: "count-item", text: "\u{1F501} " + counts.reblogs }),
        el("span", { class: "count-item", text: "⭐ " + counts.favourites }),
      ]));
    }
    if (post.reactions && post.reactions.length) {
      children.push(el("div", { class: "post-reactions" }, post.reactions.map((r) => {
        const chip = el("span", { class: "reaction-chip" });
        chip.appendChild(r.url ? el("img", { class: "emoji", src: r.url, alt: r.name }) : document.createTextNode(r.name));
        chip.appendChild(document.createTextNode(" " + r.count));
        return chip;
      })));
    }
    const actions = el("div", { class: "post-actions" });
    if (post.already_imported) {
      actions.appendChild(el("button", {
        class: "imported", text: "✓ Imported",
        onclick: (e) => { e.stopPropagation(); window.open("https://matrix.to/#/" + post.matrix_room_id, "_blank", "noopener"); },
      }));
    } else {
      actions.appendChild(el("button", {
        text: "⬇ Import",
        onclick: (e) => { e.stopPropagation(); importPost(post, e.target, onChange); },
      }));
    }
    children.push(actions);
    const card = el("div", { class: "post-card" }, [
      avatarEl(post.author.avatar_url, post.author.display_name, hasAuthorProfile ? "clickable" : "", "", openAuthor),
      el("div", { class: "post-body" }, children),
    ]);
    // The whole card opens this post's thread -- except a real link inside
    // the content (a mention, a URL) or anything with its own handler
    // (avatar/name, the view/import buttons, a reaction chip's image),
    // which should do its own thing instead.
    card.addEventListener("click", (e) => {
      if (e.target.closest("a, button, .clickable")) return;
      openThreadView(post.uri);
    });
    card.classList.add("open-thread");
    return card;
  }

  let homeState = null;

  function renderHomeTimeline(app) {
    if (!homeState) homeState = { mode: "home", domain: "", scope: "federated", cursor: "", homeCursorMap: "", posts: [], loading: false, done: false, unsupported: false, needsLogin: false, needsLoginDomains: [] };
    const state = homeState;

    const searchInput = el("input", { type: "text", placeholder: "Search @user@instance.org, a name, or paste a post link to import" });
    const searchResults = el("div", { class: "list" });
    const domainInput = el("input", { type: "text", value: state.domain, placeholder: "instance.example (blank = your default feed)" });

    function scopeBtn(label, value) {
      return el("button", {
        class: state.scope === value ? "active" : "", text: label,
        onclick: () => {
          if (state.scope === value) return;
          state.scope = value;
          state.posts = []; state.cursor = ""; state.done = false; state.unsupported = false; state.needsLogin = false;
          render();
        },
      });
    }

    async function runSearchOrImport() {
      const q = searchInput.value.trim();
      if (!q) return;
      searchResults.innerHTML = "";
      if (/^https?:\/\//i.test(q)) {
        goBtn.disabled = true;
        try {
          await api("/widget/api/import", { method: "POST", body: { url: q, room_id: roomId } });
          toast("Imported.", true);
          searchInput.value = "";
        } catch (err) {
          toast(err.message, false);
        } finally {
          goBtn.disabled = false;
        }
        return;
      }
      try {
        const data = await api("/widget/api/browse/search?q=" + encodeURIComponent(q) + "&domain=" + encodeURIComponent(state.domain || ctx.domain));
        if (!data.accounts.length) { searchResults.appendChild(el("div", { class: "muted", text: "No accounts found." })); return; }
        data.accounts.forEach((a) => {
          searchResults.appendChild(el("div", {
            class: "account-card clickable", onclick: () => openAccountProfile(a.actor_id),
          }, [
            avatarEl(a.avatar_url, a.display_name, "sm"),
            el("div", { class: "meta" }, [
              el("div", { class: "name", text: a.display_name }),
              el("div", { class: "handle", text: a.handle }),
            ]),
          ]));
        });
      } catch (err) { toast(err.message, false); }
    }
    function runBrowse() {
      const d = domainInput.value.trim().replace(/^https?:\/\//, "").replace(/\/$/, "");
      state.mode = d ? "instance" : "home";
      state.domain = d;
      state.posts = []; state.cursor = ""; state.homeCursorMap = ""; state.done = false; state.unsupported = false; state.needsLogin = false;
      render();
    }
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearchOrImport(); });
    domainInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runBrowse(); });

    // One box, two jobs: a handle/name looks accounts up (as before); a
    // pasted https:// link is instead handed straight to the existing
    // import action -- "pull in anything from a link or handle" without
    // needing a separate "Import a post" card.
    const goBtn = primaryButton("Go", runSearchOrImport);
    const controlsChildren = [
      el("h2", { text: "Browse the fediverse" }),
      el("div", { class: "row" }, [searchInput, goBtn]),
      searchResults,
      el("div", { class: "row" }, [
        domainInput,
        el("div", { class: "scope-toggle" }, [scopeBtn("Local", "local"), scopeBtn("Federated", "federated")]),
        primaryButton("Browse", runBrowse),
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
        if (state.needsLogin && state.mode === "instance") {
          const retry = () => { state.done = false; state.needsLogin = false; loadMore(); };
          feed.appendChild(el("div", { class: "placeholder" }, [
            el("div", { text: state.domain + " needs you to be signed in to view this.", style: "margin-bottom:10px" }),
            primaryButton("Sign in with OAuth", (e) => startOAuthLogin(state.domain, e.target, retry)),
            el("div", { style: "margin-top:8px" }, [
              plainButton("Use username/password instead", () => openPasswordLoginForm(state.domain, retry)),
            ]),
          ]));
          return;
        }
        if (state.mode === "home" && state.needsLoginDomains.length) {
          const retry = () => { state.done = false; state.needsLoginDomains = []; loadMore(); };
          feed.appendChild(el("div", { class: "placeholder" }, [
            el("div", { text: "Some of your feed sources need you to be signed in:", style: "margin-bottom:10px" }),
            ...state.needsLoginDomains.map((d) => el("div", { class: "row", style: "justify-content:center" }, [
              el("span", { text: d, style: "align-self:center" }),
              primaryButton("Sign in", (e) => startOAuthLogin(d, e.target, retry)),
            ])),
          ]));
          return;
        }
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
          state.needsLoginDomains = data.needs_login_domains || [];
        } else {
          data = await api(
            "/widget/api/browse/timeline?domain=" + encodeURIComponent(state.domain) + "&scope=" + state.scope +
            (state.cursor ? "&max_id=" + encodeURIComponent(state.cursor) : "")
          );
          state.cursor = data.next_max_id || "";
          state.done = !data.next_max_id;
          state.unsupported = !data.supported;
          state.needsLogin = !!data.needs_login;
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

  // -- full-page profile (own identity, or any browsed/followed account) ----
  //
  // Takes over the content area like an ordinary social-app profile page
  // (not a popup) -- opened from a Browse search result, a post author/
  // booster, a Following-list entry, or the sidebar identity card.
  // currentTab itself never changes while this is showing (see render()),
  // so "Back" always returns to wherever you came from.

  function profileBanner(bannerUrl, onUpload) {
    const wrap = el("div", { class: "profile-banner-wrap" + (bannerUrl ? "" : " profile-banner-empty") });
    if (bannerUrl) wrap.appendChild(el("img", { class: "profile-banner-img", src: bannerUrl, alt: "" }));
    if (onUpload) {
      const fileInput = el("input", { type: "file", accept: "image/*", style: "display:none" });
      fileInput.addEventListener("change", () => { if (fileInput.files[0]) onUpload(fileInput.files[0]); });
      wrap.appendChild(el("button", { class: "banner-edit-btn", text: "✎", title: "Change banner", onclick: () => fileInput.click() }));
      wrap.appendChild(fileInput);
    }
    return wrap;
  }

  function appendProfileIdentity(page, avatarUrl, name, handle, actionsNode) {
    page.appendChild(el("div", { class: "profile-header-row" }, [
      avatarEl(avatarUrl, name, "profile-avatar-xl"),
      actionsNode || el("div", {}),
    ]));
    page.appendChild(el("div", { class: "profile-name", text: name }));
    page.appendChild(el("div", { class: "profile-handle" }, [
      el("span", { text: handle }),
      el("button", {
        class: "copy-btn", text: "⧉", title: "Copy handle",
        onclick: () => { try { navigator.clipboard.writeText(handle); toast("Copied.", true); } catch (e) {} },
      }),
    ]));
  }

  function renderProfilePostFeed(page, actorId, author) {
    page.appendChild(el("h2", { text: "Posts" }));
    const feed = el("div", {});
    const sentinel = el("div");
    page.appendChild(feed);
    page.appendChild(sentinel);
    let cursor = "", loading = false, done = false;
    function loadMorePosts() {
      if (loading || done) return;
      loading = true;
      api("/widget/api/browse/profile/timeline?actor_id=" + encodeURIComponent(actorId) + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""))
        .then((data) => {
          data.posts.forEach((post) => { post.author = author; feed.appendChild(renderPostCard(post, function () {})); });
          cursor = data.next_cursor || "";
          done = !cursor;
        })
        .catch(() => { done = true; })
        .finally(() => { loading = false; });
    }
    loadMorePosts();
    const observer = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMorePosts(); });
    observer.observe(sentinel);
  }

  function renderImportFollowsCard(page, targetRoomId) {
    const fileInput = el("input", { type: "file", accept: ".csv,.txt" });
    page.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Import a follows list" }),
      el("div", { class: "sub", text: "A Pleroma/Akkoma export (one @user@instance.org per line) or a Mastodon follows CSV." }),
      el("div", { class: "row" }, [fileInput]),
      el("div", { class: "row" }, [
        primaryButton("Import follows", async (e) => {
          const file = fileInput.files[0];
          if (!file) { toast("Choose a file first.", false); return; }
          e.target.disabled = true;
          try {
            const result = await api("/widget/api/import_follows?room_id=" + encodeURIComponent(targetRoomId), {
              method: "POST", headers: { "Content-Type": "text/plain" }, raw: file,
            });
            toast("Importing " + result.count + " follows -- a summary will be posted to your Profile Room when it's done.", true);
          } catch (err) {
            toast(err.message, false);
          } finally {
            e.target.disabled = false;
          }
        }),
      ]),
    ]));
  }

  function openAccountProfile(actorId) {
    if (!actorId) return;
    profileView = { mode: "remote", actorId: actorId };
    threadView = null;
    render();
  }

  function openMyProfile() {
    profileView = { mode: "own" };
    threadView = null;
    render();
  }

  function openThreadView(uri) {
    if (!uri) return;
    threadView = { uri: uri };
    profileView = null;
    render();
  }

  function renderProfilePage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { profileView = null; render(); } }));
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);

    if (view.mode === "own") {
      function refresh() {
        return loadIdentityState().then(() => renderOwn(identityState.active));
      }
      function renderOwn(s) {
        page.innerHTML = "";
        if (!s) { page.appendChild(el("div", { class: "muted", text: "No identity yet." })); return; }
        page.appendChild(profileBanner(s.banner_url, async (file) => {
          try {
            await api("/widget/api/upload_banner?room_id=" + encodeURIComponent(s.room_id), {
              method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, raw: file,
            });
            toast("Banner updated.", true);
            refresh();
          } catch (err) { toast(err.message, false); }
        }));
        const followingStat = el("div", { class: "profile-stat clickable", text: "Following: " + s.following_count + "  ›" });
        followingStat.addEventListener("click", openFollowingList);
        const statsBox = el("div", { class: "profile-stats" }, [
          el("div", { class: "profile-stat", text: "Followers: " + s.followers_count }),
          followingStat,
        ]);
        appendProfileIdentity(page, s.icon_url, s.display_name || s.handle, s.handle, statsBox);
        if (s.summary) { const bio = el("div", { class: "profile-bio" }); bio.innerHTML = s.summary; page.appendChild(bio); }

        function visAction(btn, which, hidden, value) {
          btn.disabled = true;
          const payload = { username: s.username, which: which };
          if (hidden !== undefined) payload.hidden = hidden;
          if (value !== undefined) payload.value = value;
          api("/widget/api/visibility", { method: "POST", body: payload })
            .then(() => { toast("Done.", true); refresh(); })
            .catch((e) => toast(e.message, false))
            .finally(() => { btn.disabled = false; });
        }
        page.appendChild(el("div", { class: "row" }, [
          toggleButton(s.hide_followers ? "Followers: hidden" : "Followers: visible", s.hide_followers, (e) => visAction(e.target, "followers", !s.hide_followers, undefined)),
          toggleButton(s.hide_following ? "Following: hidden" : "Following: visible", s.hide_following, (e) => visAction(e.target, "following", !s.hide_following, undefined)),
          toggleButton(s.discoverable ? "Discoverable" : "Not discoverable", s.discoverable, (e) => visAction(e.target, "discoverable", undefined, !s.discoverable)),
        ]));
        if (s.public_url) page.appendChild(el("div", { class: "sub" }, [el("a", { href: s.public_url, target: "_blank", text: "View public profile →" })]));

        renderImportFollowsCard(page, s.room_id);
        renderProfilePostFeed(page, s.actor_id, { actor_id: s.actor_id, display_name: s.display_name || s.handle, handle: s.handle, avatar_url: s.icon_url, url: s.public_url });
      }
      refresh().catch(() => {
        page.innerHTML = "";
        page.appendChild(el("div", { class: "muted", text: "Couldn't load your profile." }));
      });
      return;
    }

    // remote account
    const actorId = view.actorId;
    function profileAction(path, p, btn) {
      if (btn) btn.disabled = true;
      api(path, { method: "POST", body: { room_id: roomId, handle: p.handle } })
        .then(() => { toast("Done.", true); return refresh(); })
        .catch((e) => toast(e.message, false))
        .finally(() => { if (btn) btn.disabled = false; });
    }
    function renderRemote(p) {
      page.innerHTML = "";
      page.appendChild(profileBanner(p.banner_url, null));
      const actions = el("div", { class: "row" }, [
        p.is_following
          ? dangerButton("Unfollow", (e) => profileAction("/widget/api/unfollow", p, e.target))
          : primaryButton("Follow", (e) => profileAction("/widget/api/follow", p, e.target)),
        plainButton("DM", (e) => profileAction("/widget/api/dm", p, e.target)),
        plainButton("Chat", (e) => profileAction("/widget/api/chat", p, e.target)),
      ]);
      appendProfileIdentity(page, p.avatar_url, p.display_name, p.handle, actions);
      if (p.bio_html) { const bio = el("div", { class: "profile-bio" }); bio.innerHTML = p.bio_html; page.appendChild(bio); }
      page.appendChild(el("div", { class: "row" }, [
        toggleButton(p.is_muted ? "Muted" : "Mute", p.is_muted, (e) =>
          profileAction(p.is_muted ? "/widget/api/unmute" : "/widget/api/mute", p, e.target)),
        p.is_blocked
          ? plainButton("Unblock", (e) => profileAction("/widget/api/unblock", p, e.target))
          : dangerButton("Block", (e) => {
              if (confirm("Block this account? This cuts any follow and kicks you from their rooms.")) profileAction("/widget/api/block", p, e.target);
            }),
      ]));
      page.appendChild(el("div", { class: "sub" }, [el("a", { href: p.url, target: "_blank", text: "View on their instance →" })]));
      if (p.has_posts) {
        renderProfilePostFeed(page, p.actor_id, { actor_id: p.actor_id, display_name: p.display_name, handle: p.handle, avatar_url: p.avatar_url, url: p.url });
      }
    }
    function refresh() {
      return api("/widget/api/browse/profile?actor_id=" + encodeURIComponent(actorId)).then((p) => { renderRemote(p); return p; });
    }
    refresh().catch(() => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: "Couldn't load that profile." }));
    });
  }

  // -- thread view (ancestors up to the root, the post, its replies) -------

  function renderThreadPage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { threadView = null; render(); } }));
    const page = el("div", { class: "profile-page thread-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);

    api("/widget/api/browse/thread?uri=" + encodeURIComponent(view.uri)).then((data) => {
      page.innerHTML = "";
      if (!data.post) {
        page.appendChild(el("div", { class: "muted", text: "Couldn't load that post." }));
        return;
      }
      // Ancestors + the focused post form one connected chain, lines
      // running avatar-to-avatar up to the thread root (or as far back
      // as this bridge could resolve).
      const rootChain = el("div", { class: "card" });
      const chain = (data.ancestors || []).concat([data.post]);
      chain.forEach((p, i) => {
        const isFocus = i === chain.length - 1;
        const card = renderPostCard(p, function () {}, { focused: isFocus });
        card.classList.add(isFocus ? "thread-focus" : "thread-ancestor");
        if (!isFocus) card.classList.add("chain-link");
        rootChain.appendChild(card);
      });
      page.appendChild(rootChain);

      if (data.replies && data.replies.length) {
        page.appendChild(el("h2", { text: "Replies" }));
        // Each direct reply is its own card (or pair of cards, line-
        // connected) -- NOT one shared list -- so a reply the focused
        // post's author continued replying to reads as its own
        // conversation branch, not a flat series.
        data.replies.forEach((reply) => {
          const branch = el("div", { class: "card" });
          const replyCard = renderPostCard(reply, function () {});
          if (reply.op_reply) {
            replyCard.classList.add("chain-link");
            branch.appendChild(replyCard);
            branch.appendChild(renderPostCard(reply.op_reply, function () {}));
          } else {
            branch.appendChild(replyCard);
          }
          page.appendChild(branch);
        });
      }
    }).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load that thread." }));
    });
  }

  function openFollowingList() {
    const body = el("div", { class: "card profile-modal" }, [el("div", { class: "muted", text: "Loading…" })]);
    openModal(body);
    function load() {
      api("/widget/api/following").then((data) => {
        body.innerHTML = "";
        body.appendChild(el("div", { class: "row" }, [
          el("h2", { text: "Following" }),
          el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
        ]));
        if (!data.following.length) { body.appendChild(el("div", { class: "muted", text: "Not following anyone yet." })); return; }
        body.appendChild(el("div", { class: "list" }, data.following.map((f) =>
          el("div", { class: "list-item" }, [
            el("span", { class: "clickable", text: f.display_name || f.handle, onclick: () => openAccountProfile(f.actor_id) }),
            plainButton("Unfollow", (e) => doAction("/widget/api/unfollow", { handle: f.handle }, e.target).then(load)),
          ])
        )));
      }).catch(() => { body.innerHTML = ""; body.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }
    load();
  }

  // -- instance sign-in (OAuth + password fallback) --------------------------

  function startOAuthLogin(domain, btn, onDone) {
    if (!domain) { toast("Enter an instance domain first.", false); return; }
    if (btn) btn.disabled = true;
    api("/widget/api/browse/login/start", { method: "POST", body: { domain: domain } })
      .then((data) => {
        window.open(data.authorize_url, "fediverse-bridge-login", "width=500,height=700");
        let settled = false;
        let pollTimer = null;
        function finish(ok) {
          if (settled) return;
          settled = true;
          window.removeEventListener("message", onMessage);
          if (pollTimer) clearInterval(pollTimer);
          if (btn) btn.disabled = false;
          if (ok) { toast("Signed in.", true); if (onDone) onDone(); }
        }
        function onMessage(ev) {
          if (!ev.data || ev.data.type !== "fediverse-bridge-login") return;
          if (ev.data.ok) finish(true);
          else { toast("Sign-in failed or was cancelled.", false); finish(false); }
        }
        window.addEventListener("message", onMessage);
        // The OAuth tab posts a message back on success, but that relies
        // on window.opener/postMessage surviving a sandboxed widget
        // iframe, which isn't always reliable -- observed live: the tab
        // closed itself but the timeline never refreshed until Browse was
        // clicked again. Poll for the login actually landing as a
        // fallback instead of only ever waiting to be told.
        let attempts = 0;
        pollTimer = setInterval(() => {
          attempts++;
          if (attempts > 60) { finish(false); return; }
          api("/widget/api/browse/login/list").then((d) => {
            if ((d.logins || []).some((l) => l.domain === domain)) finish(true);
          }).catch(() => {});
        }, 2000);
      })
      .catch((e) => { toast(e.message, false); if (btn) btn.disabled = false; });
  }

  function openPasswordLoginForm(domain, onDone) {
    const body = el("div", { class: "card profile-modal" });
    const domainInput = el("input", { type: "text", value: domain || "", placeholder: "instance.example" });
    const userInput = el("input", { type: "text", placeholder: "username" });
    const passInput = el("input", { type: "password", placeholder: "password" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Sign in with username & password" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "sub", text: "Prefer OAuth above when the instance supports it -- your password goes directly to that instance over HTTPS and is never stored here, only the resulting access token is." }));
    body.appendChild(el("div", { class: "row" }, [domainInput]));
    body.appendChild(el("div", { class: "row" }, [userInput, passInput]));
    body.appendChild(el("div", { class: "row" }, [
      primaryButton("Sign in", (e) => {
        if (!domainInput.value.trim() || !userInput.value || !passInput.value) return;
        e.target.disabled = true;
        api("/widget/api/browse/login/password", {
          method: "POST",
          body: { domain: domainInput.value.trim(), username: userInput.value, password: passInput.value },
        })
          .then(() => { toast("Signed in.", true); closeModal(); if (onDone) onDone(); })
          .catch((err) => toast(err.message, false))
          .finally(() => { e.target.disabled = false; });
      }),
    ]));
    openModal(body);
  }

  function renderInstanceLoginsCard(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    function loadLogins() {
      api("/widget/api/browse/login/list").then((data) => {
        list.innerHTML = "";
        if (!data.logins.length) { list.appendChild(el("div", { class: "muted", text: "Not signed into any instance." })); return; }
        data.logins.forEach((l) => {
          list.appendChild(el("div", { class: "list-item" }, [
            el("span", { text: l.domain + (l.account_handle ? " (" + l.account_handle + ")" : "") + " — " + l.auth_method }),
            plainButton("Log out", (e) => doAction("/widget/api/browse/login/logout", { domain: l.domain }, e.target).then(loadLogins)),
          ]));
        });
      }).catch(() => {});
    }
    const domainInput = el("input", { type: "text", placeholder: "instance.example" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Instance sign-ins" }),
      el("div", { class: "sub", text: "Sign in to view a locked-down instance's timeline or search in the Browse/Home tabs." }),
      list,
      el("div", { class: "row" }, [
        domainInput,
        primaryButton("Sign in with OAuth", (e) => startOAuthLogin(domainInput.value.trim(), e.target, loadLogins)),
      ]),
      el("div", { class: "row" }, [
        plainButton("Use username/password instead", () => openPasswordLoginForm(domainInput.value.trim(), loadLogins)),
      ]),
    ]));
    loadLogins();
  }

  // -- Settings ---------------------------------------------------------------
  //
  // Everything left over once identity switching/creation moved to the
  // sidebar card and profile display/visibility/banner moved to "My
  // Profile": account-wide, not-room-bound, not identity-display tools.

  function renderSettingsTab(app) {
    if (!ctx.has_profile) {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Get started" }),
        el("div", { class: "sub", text: "You don't have a fediverse profile yet." }),
        el("div", { class: "row" }, [
          primaryButton("Create my profile", (e) => doAction("/widget/api/create_profile", {}, e.target)),
        ]),
      ]));
      return;
    }

    renderThisRoomSection(app);
    renderInstanceLoginsCard(app);

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
  }

  // -- Publish (PeerTube channels + Lemmy communities) ------------------------

  function renderCreateForm(app, title, sub, onCreate) {
    const idInput = el("input", { type: "text", placeholder: "id (e.g. my-show)" });
    const nameInput = el("input", { type: "text", placeholder: "Display name (optional)" });
    const identitySelect = identityState.identities.length > 1
      ? el("select", {}, identityState.identities.map((i) => el("option", { value: i.username, text: i.display_name || i.handle })))
      : null;
    const row = [idInput, nameInput];
    if (identitySelect) row.push(identitySelect);
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: title }),
      el("div", { class: "sub", text: sub }),
      el("div", { class: "row" }, row),
      el("div", { class: "row" }, [
        primaryButton("Create", (e) => {
          if (!idInput.value.trim()) return;
          onCreate({
            id: idInput.value.trim(),
            display_name: nameInput.value.trim(),
            as_identity: identitySelect ? identitySelect.value : "",
          }, e.target);
        }),
      ]),
    ]));
  }

  function renderChannelsSection(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [el("h2", { text: "PeerTube channels" }), list]));
    api("/widget/api/channels").then((data) => {
      list.innerHTML = "";
      if (!data.channels.length) { list.appendChild(el("div", { class: "muted", text: "No channels yet." })); return; }
      data.channels.forEach((c) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("span", { text: c.display_name || c.username }),
          plainButton("Manage in room", () => window.open("https://matrix.to/#/" + c.room_id, "_blank", "noopener")),
        ]));
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    renderCreateForm(app, "Create a channel", "Hosts a PeerTube-compatible video channel as a Matrix room.", (vals, btn) => {
      doAction("/widget/api/channels/create", vals, btn);
    });
  }

  function renderCommunitiesSection(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [el("h2", { text: "Lemmy communities" }), list]));
    api("/widget/api/communities").then((data) => {
      list.innerHTML = "";
      if (!data.communities.length) { list.appendChild(el("div", { class: "muted", text: "No communities yet." })); return; }
      data.communities.forEach((c) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("span", { text: c.display_name || c.username }),
          plainButton("Manage in room", () => window.open("https://matrix.to/#/" + c.room_id, "_blank", "noopener")),
        ]));
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    renderCreateForm(app, "Create a community", "Hosts a Lemmy-compatible community as a Matrix room.", (vals, btn) => {
      doAction("/widget/api/communities/create", vals, btn);
    });
  }

  function renderPublishTab(app) {
    if (!ctx.peertube_channels_enabled && !ctx.lemmy_community_hosting_enabled) {
      placeholder(app, "Publishing isn't enabled on this bridge.");
      return;
    }
    if (ctx.peertube_channels_enabled) renderChannelsSection(app);
    if (ctx.lemmy_community_hosting_enabled) renderCommunitiesSection(app);
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

  // Whatever's specific to the room the widget happens to be embedded in
  // right now -- there's no more standalone "This Room" tab, so this is
  // folded into Settings instead (see renderSettingsTab). Room-bound
  // because each action's own meaning depends on which Matrix room it
  // runs in (replace/unlink/delete THIS profile room, backfill THIS
  // account, mute/block THIS DM partner) -- unlike everything else in
  // Settings, which is account-wide regardless of room.
  function renderThisRoomSection(app) {
    const kind = ctx.room.kind;
    const r = ctx.room;
    if (kind === "profile") {
      const card = el("div", { class: "card" }, [el("h2", { text: "This room: your profile" })]);
      if (r.is_owner) {
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
    } else if (kind === "remote_actor") {
      const card = el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (r.display_name || "Fediverse account") }),
        el("div", { class: "row" }, [
          plainButton("View full profile", () => openAccountProfile(r.actor_id)),
          r.is_following ? el("span", { class: "badge", text: "Following" }) : null,
        ].filter(Boolean)),
      ]);
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
    } else if (kind === "dm" || kind === "chat") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (kind === "dm" ? "Direct message" : "Chat") }),
        el("div", { class: "row" }, [
          plainButton("Mute", (e) => doAction("/widget/api/mute", {}, e.target)),
          dangerButton("Block", (e) => {
            if (confirm("Block this account?")) doAction("/widget/api/block", {}, e.target);
          }),
        ]),
      ]));
    } else if (kind === "notifications") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: Notifications" }),
        el("div", { class: "sub", text: "Your Fediverse Notifications DM with the bridge bot -- nothing to configure here." }),
      ]));
    }
    // kind === "unmanaged": nothing room-specific to show.
  }

  async function loadContext() {
    ctx = await api("/widget/api/context?room_id=" + encodeURIComponent(roomId));
    await loadIdentityState();
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
