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
from bridge.activitypub.urls import actor_url, media_url
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
    widget_edit_video,
    widget_publish_video,
    widget_unpublish_video,
)
from bridge.identity_settings import get_effective_settings
from bridge.peertube import (
    COMMON_LANGUAGES,
    VIDEO_CATEGORIES,
    VIDEO_LICENCES,
    VIDEO_METADATA_STATE_TYPE,
    get_video_index,
)
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
            # Admin-curated Browse-instance shortlist (bridge.default_timeline_domains) --
            # the Browse domain dropdown merges this with the user's own
            # locally-remembered instances (see renderHomeTimeline's own
            # domainSelect, which is the only consumer of this field).
            "default_timeline_domains": config.bridge.default_timeline_domains,
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
    except _RoomListingUnreliable as exc:
        # str(exc) is _RoomListingUnreliable's own str(SynapseError) --
        # "Synapse error <status> <errcode>: <error>" -- safe to show
        # directly (no secrets, just the homeserver's own error shape) and
        # genuinely useful: distinguishes "the bridge's Synapse admin token
        # is stale/invalid" from a transient network blip, which "try again
        # shortly" alone can't (confirmed live 2026-10-24: a stale
        # synapse.admin_token produced exactly this generic message with no
        # way to tell what was actually wrong).
        return JSONResponse(
            {"rooms": [], "unreliable": True, "reason": str(exc)}, headers={"Cache-Control": "no-store"}
        )
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
    except _RoomListingUnreliable as exc:
        # Same underlying failure widget_leave_unfollowed_preview surfaces
        # with a reason string -- this is the EXECUTE side (e.g. the admin
        # API went stale again in the gap between a successful Preview and
        # clicking Confirm), which would otherwise propagate as a raw,
        # unexplained 500 instead of a clean error naming the real cause.
        logger.warning("Widget action's room listing was unreliable: %s", exc, exc_info=True)
        raise HTTPException(502, f"Couldn't reliably list your rooms: {exc}") from exc
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
    """``_handle_import`` itself only ever posts a matrix.to link into a
    Matrix room notice -- it has no return value for an API caller. A
    Browse-tab post card needs the resulting room/event id immediately
    (to flip its own "Imported" badge into a working matrix.to link
    without waiting for a full context/timeline reload), so look it up
    the same way the timeline's own already-imported badging does
    (``_mark_imported`` in ``bridge/widget_browse.py``): by AP object id,
    which for a browsed post is exactly the ``uri`` the card was built
    from and passes back here unchanged as ``url``."""
    body = await request.json()
    url = body.get("url", "")
    if not isinstance(url, str) or not url:
        raise HTTPException(400, "url is required")
    matrix_user_id = _session_user(request)
    try:
        await _handle_import(request, sender=matrix_user_id, room_id=body["room_id"], url=url)
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    repository = request.app.state.repository
    federated = await repository.get_federated_event_by_ap_object(url)
    return JSONResponse({
        "ok": True,
        "room_id": federated.room_id if federated is not None else None,
        "event_id": federated.event_id if federated is not None else None,
    })


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
    switcher rather than a specific room's own tab.

    Bespoke, not the generic ``_dispatch`` (which always returns
    ``{"ok": true}`` and leaves failure reasons in a room notice) --
    ``_handle_create_profile`` now returns ``(ok, message)`` specifically
    so the widget's own "create identity" page (see its own comment) can
    show the REAL rejection reason (reserved name, already taken, a real
    Matrix user's own name, ...) inline, since it has nowhere else to
    surface a room notice from."""
    body = await request.json()
    custom_id = (body.get("custom_id") or "").strip()
    matrix_user_id = _session_user(request)
    try:
        ok, message = await _handle_create_profile(
            request, sender=matrix_user_id, room_id=body.get("room_id", ""), custom_id=custom_id,
        )
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": ok, "message": message})


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


@router.get("/widget/api/video_options")
async def widget_video_options(request: Request) -> JSONResponse:
    """Static reference data for the Publish-video form's dropdowns --
    the exact same name strings ``;publish``/``;edit``'s own ``resolve_category``/
    ``resolve_licence`` accept as the raw ``category``/``license`` field
    value, so a widget-published video validates identically to a chat-
    command-published one. No language list -- see ``COMMON_LANGUAGES``'s
    own docstring for why that one stays a free-text ISO code input with
    just a placeholder hint instead of a dropdown that can't name every
    valid value anyway."""
    _session_user(request)
    return JSONResponse(
        {
            "categories": list(VIDEO_CATEGORIES.values()),
            "licences": list(VIDEO_LICENCES.values()),
            "common_languages": [{"code": k, "name": v} for k, v in COMMON_LANGUAGES.items()],
        },
        headers={"Cache-Control": "no-store"},
    )


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
            "icon_url": channel.icon_url,
            "handle": f"{channel.username}@{config.bridge.domain}",
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


@router.get("/widget/api/channels/{username}")
async def widget_channel_detail(request: Request, username: str) -> JSONResponse:
    """A single channel's own page -- avatar/name/summary plus whether the
    calling user is its owner (gates the management controls the page
    itself renders; this bridge double-checks ownership server-side again
    on every actual write below regardless, the same way every other
    owner-gated widget action already does)."""
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    repository = request.app.state.repository
    channel = await repository.get_peertube_channel(username)
    if channel is None:
        raise HTTPException(404, "No such channel.")
    base = config.bridge.public_base_url
    owner = await repository.get_local_actor(channel.owner_username)
    return JSONResponse(
        {
            "username": channel.username,
            "display_name": channel.display_name,
            "handle": f"{channel.username}@{config.bridge.domain}",
            "summary": channel.summary,
            "icon_url": channel.icon_url,
            "banner_url": channel.banner_url,
            "room_id": channel.room_id,
            "owner_username": channel.owner_username,
            "is_owner": owner is not None and owner.matrix_user_id == matrix_user_id,
            "public_url": actor_url(base, channel.username),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/channels/{username}/videos")
async def widget_channel_videos(request: Request, username: str) -> JSONResponse:
    """Video tiles for a channel's own page -- reads the SAME
    ``VIDEO_INDEX_STATE_TYPE``/``VIDEO_METADATA_STATE_TYPE`` room state
    ``;publish``/``;edit`` themselves already maintain (``bridge.peertube.
    get_video_index``), no separate tracking of its own. ``ap_object_id``
    is reconstructed deterministically from each video's own id shape
    (``_handle_publish``'s own ``video_id`` format), not stored again here,
    so a lookup against ``federated_events`` can resolve the matrix room/
    event a tile's "View in room"/management actions need."""
    _session_user(request)
    config = request.app.state.config
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    channel = await repository.get_peertube_channel(username)
    if channel is None:
        raise HTTPException(404, "No such channel.")
    base = config.bridge.public_base_url
    video_ids = await get_video_index(synapse, room_id=channel.room_id)
    videos = []
    for local_video_id in video_ids:
        try:
            meta = await synapse.get_room_state(channel.room_id, VIDEO_METADATA_STATE_TYPE, local_video_id)
        except SynapseError:
            continue
        ap_object_id = f"{base}/actor/{channel.username}/videos/{local_video_id}"
        federated = await repository.get_federated_event_by_ap_object(ap_object_id)
        thumbnail_mxc = meta.get("thumbnail_mxc")
        videos.append({
            "id": local_video_id,
            "name": meta.get("name") or "(untitled)",
            "thumbnail_url": media_url(base, thumbnail_mxc) if thumbnail_mxc else None,
            "published": meta.get("published"),
            "tags": meta.get("tags") or [],
            "sensitive": bool(meta.get("sensitive")),
            "description": meta.get("description") or "",
            "category": meta.get("category") or "",
            "license": meta.get("license") or "",
            "language": meta.get("language") or "",
            "comments_enabled": bool(meta.get("commentsEnabled", True)),
            "matrix_room_id": federated.room_id if federated else None,
            "matrix_event_id": federated.event_id if federated else None,
        })
    videos.sort(key=lambda v: v.get("published") or "", reverse=True)
    return JSONResponse({"videos": videos}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/channels/{username}/upload_video")
async def widget_upload_video(request: Request, username: str) -> JSONResponse:
    """Raw video bytes straight from the widget's own ``<input
    type=file>`` -- same shape as ``widget_upload_banner``, uploaded as
    the bot (the AS token can never upload "as" an arbitrary real human
    user), returning the resulting ``mxc://`` for the caller to then pass
    to ``/widget/api/channels/{username}/publish``. Ownership isn't
    checked here (uploading media commits nothing on its own); ``publish``
    below re-checks it before doing anything real."""
    _session_user(request)
    config = request.app.state.config
    data = await request.body()
    if not data:
        raise HTTPException(400, "No video data received")
    content_type = request.headers.get("content-type") or "video/mp4"
    try:
        mxc = await request.app.state.synapse.upload_media(
            data, content_type, f"{username}-video", as_user_id=_bot_mxid(config),
        )
    except SynapseError as exc:
        raise HTTPException(502, "Could not upload that video to the homeserver.") from exc
    return JSONResponse(
        {"mxc": mxc, "mimetype": content_type, "size": len(data)}, headers={"Cache-Control": "no-store"}
    )


@router.post("/widget/api/channels/{username}/upload_thumbnail")
async def widget_upload_thumbnail(request: Request, username: str) -> JSONResponse:
    """Same shape as ``widget_upload_video`` right above, for a video's
    thumbnail image instead -- either one the user picked by hand, or one
    the widget generated itself client-side from a frame of the video
    (see ``extractVideoThumbnail``), uploaded here exactly the same way
    either way. The resulting ``mxc://`` goes in ``thumbnail_mxc`` on
    ``/widget/api/channels/{username}/publish``, which
    ``_handle_publish`` already reads as a first-class thumbnail source
    (``info.get(\"thumbnail_url\")``) -- nothing new needed on that end,
    publishing from the widget just never had a way to set it before."""
    _session_user(request)
    config = request.app.state.config
    data = await request.body()
    if not data:
        raise HTTPException(400, "No image data received")
    content_type = request.headers.get("content-type") or "image/jpeg"
    try:
        mxc = await request.app.state.synapse.upload_media(
            data, content_type, f"{username}-thumbnail", as_user_id=_bot_mxid(config),
        )
    except SynapseError as exc:
        raise HTTPException(502, "Could not upload that thumbnail to the homeserver.") from exc
    return JSONResponse({"mxc": mxc}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/channels/{username}/publish")
async def widget_channel_publish(request: Request, username: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    body = await request.json()
    mxc = (body.get("mxc") or "").strip()
    if not mxc.startswith("mxc://"):
        raise HTTPException(400, "mxc must be a mxc:// URI -- upload a video or paste one first")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        ok, message = await widget_publish_video(
            request,
            matrix_user_id=matrix_user_id,
            channel_username=username,
            mxc=mxc,
            filename=(body.get("filename") or "").strip(),
            mimetype=(body.get("mimetype") or "").strip(),
            size=body.get("size") if isinstance(body.get("size"), int) else None,
            name=name,
            category=(body.get("category") or "").strip(),
            licence=(body.get("license") or "").strip(),
            language=(body.get("language") or "").strip(),
            tags=(body.get("tags") or "").strip(),
            sensitive=bool(body.get("sensitive")),
            comments_enabled=body.get("comments_enabled", True) is not False,
            description=(body.get("description") or "").strip(),
            thumbnail_mxc=(body.get("thumbnail_mxc") or "").strip(),
            duration_ms=body.get("duration_ms") if isinstance(body.get("duration_ms"), (int, float)) else None,
            width=body.get("width") if isinstance(body.get("width"), (int, float)) else None,
            height=body.get("height") if isinstance(body.get("height"), (int, float)) else None,
        )
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": ok, "message": message}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/channels/{username}/videos/{video_id}/edit")
async def widget_channel_edit_video(request: Request, username: str, video_id: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        ok, message = await widget_edit_video(
            request,
            matrix_user_id=matrix_user_id,
            channel_username=username,
            local_video_id=video_id,
            name=name,
            category=(body.get("category") or "").strip(),
            licence=(body.get("license") or "").strip(),
            language=(body.get("language") or "").strip(),
            tags=(body.get("tags") or "").strip(),
            sensitive=bool(body.get("sensitive")),
            comments_enabled=body.get("comments_enabled", True) is not False,
            description=(body.get("description") or "").strip(),
            thumbnail_mxc=(body.get("thumbnail_mxc") or "").strip(),
        )
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": ok, "message": message}, headers={"Cache-Control": "no-store"})


@router.post("/widget/api/channels/{username}/videos/{video_id}/unpublish")
async def widget_channel_unpublish_video(request: Request, username: str, video_id: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    try:
        ok, message = await widget_unpublish_video(
            request, matrix_user_id=matrix_user_id, channel_username=username, local_video_id=video_id,
        )
    except SynapseError as exc:
        raise HTTPException(502, "The homeserver rejected that action -- try again.") from exc
    return JSONResponse({"ok": ok, "message": message}, headers={"Cache-Control": "no-store"})


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
            "icon_url": community.icon_url,
            "handle": f"{community.username}@{config.bridge.domain}",
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


@router.get("/widget/api/communities/{username}")
async def widget_community_detail(request: Request, username: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    repository = request.app.state.repository
    community = await repository.get_lemmy_community(username)
    if community is None:
        raise HTTPException(404, "No such community.")
    base = config.bridge.public_base_url
    owner = await repository.get_local_actor(community.owner_username)
    return JSONResponse(
        {
            "username": community.username,
            "display_name": community.display_name,
            "handle": f"{community.username}@{config.bridge.domain}",
            "summary": community.summary,
            "icon_url": community.icon_url,
            "banner_url": community.banner_url,
            "room_id": community.room_id,
            "owner_username": community.owner_username,
            "is_owner": owner is not None and owner.matrix_user_id == matrix_user_id,
            "moderator_count": len(community.moderator_actor_ids),
            "public_url": actor_url(base, community.username),
        },
        headers={"Cache-Control": "no-store"},
    )


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
    font: 16px/1.5 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
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
    border: 1px solid var(--border); border-radius: var(--radius-inner); font-size: 13px; gap: 10px;
  }
  .list-item.clickable { cursor: pointer; }
  .list-item.clickable:hover { background: var(--hover); }
  .checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
  .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  .video-tile { border: 1px solid var(--border); border-radius: var(--radius-card); overflow: hidden; }
  .video-tile-thumb { position: relative; aspect-ratio: 16/9; background: var(--hover); }
  .video-tile-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .video-tile-thumb-empty { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; font-size: 28px; }
  .video-tile-badge {
    position: absolute; top: 6px; right: 6px; background: rgba(0,0,0,.65); color: #fff;
    font-size: 11px; font-weight: 600; padding: 2px 6px; border-radius: var(--radius-chip);
  }
  .video-tile-title { font-size: 13px; font-weight: 600; padding: 8px 10px 0; }
  .video-tile-meta { font-size: 12px; color: var(--muted); padding: 4px 10px 10px; }
  .video-tile-link { display: block; font-size: 12px; padding: 0 10px 10px; }
  .video-tile-actions { padding: 0 10px 10px; margin-bottom: 0; }
  .muted { color: var(--muted); }
  .spinner { text-align: center; color: var(--muted); padding: 40px 0; }
  .confirm-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
  .confirm-row .muted { font-size: 13px; }
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
  /* A hard, fixed-pixel cap (not a percentage) -- on a very wide monitor
     (not split side-by-side with anything else), nicecrew.digital's own
     timeline column stops growing well short of the full window and
     leaves the rest of the space to its own sidebar content, rather than
     letting the reading column itself balloon. We have no sidebar
     content to put there, so this just stops growing and leaves the
     space blank -- same effect, simpler cause. */
  #content { padding: 16px; flex: 1; min-width: 0; max-width: 700px; }
  .placeholder { text-align: center; color: var(--muted); padding: 48px 16px; font-size: 13px; }
  /* A source-level notice (needs sign-in / timed out) sits ABOVE a
     possibly-non-empty post list now, not alone on the page -- the full
     48px placeholder padding reads as too much dead space stacked on top
     of real posts, and it needs a visible break from what follows. */
  .browse-source-notice { padding: 16px; border-bottom: 1px solid var(--border); }

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
  .post-header { display: flex; align-items: baseline; gap: 6px; font-size: 14px; }
  .post-header-left { display: flex; align-items: baseline; gap: 6px; min-width: 0; overflow: hidden; }
  .post-header-right { margin-left: auto; flex-shrink: 0; display: flex; align-items: center; gap: 2px; }
  .post-header .name { font-weight: 600; color: var(--text); }
  .post-header .handle, .post-header .time { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .post-header .time { text-decoration: none; }
  .post-header .time:hover { text-decoration: underline; }
  .view-icon-btn { border: none; background: none; color: var(--muted); font-size: 13px; padding: 0 0 0 4px; cursor: pointer; flex-shrink: 0; line-height: 1; }
  .view-icon-btn:hover { color: var(--text); }
  .post-timestamp-full { font-size: 12px; color: var(--muted); margin: 6px 0; }
  .boosted-by { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .boosted-by img.emoji, .post-header .name img.emoji { width: 1em; height: 1em; }
  .reply-to-line { font-size: 12px; color: var(--muted); margin-bottom: 4px; cursor: pointer; }
  /* A connecting line from this card's avatar down into the next card's --
     the ancestor chain leading up to the thread root, and a direct reply
     that the focused post's own author continued replying to. No
     border-top divider between a chain-linked pair (previously ran
     RIGHT THROUGH the connector, and bottom: -1px stopped the line at
     this card's own edge rather than reaching the next card's avatar
     another 16px of padding down -- together reading as a broken,
     disconnected line rather than Haven's own unbroken avatar-to-avatar
     one, confirmed live 2026-10-25). */
  .post-card.chain-link + .post-card { border-top: none; }
  .post-card.chain-link::after {
    content: ""; position: absolute; left: 35px; top: 56px; bottom: -17px; width: 2px; background: var(--border);
  }
  .reply-to-line:hover { color: var(--accent); }
  .post-content { font-size: 16px; line-height: 1.5; margin: 4px 0; overflow-wrap: break-word; }
  .post-content p { margin: 0 0 8px; }
  .post-content p:last-child { margin-bottom: 0; }
  .post-content a { color: var(--accent); }
  img.emoji { width: 1.2em; height: 1.2em; vertical-align: -0.2em; object-fit: contain; }
  .post-media { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 8px 0; border-radius: var(--radius-inner); overflow: hidden; }
  .post-media img, .post-media video { width: 100%; max-height: 360px; object-fit: cover; display: block; background: var(--hover); }
  .post-media-audio { width: 100%; grid-column: 1 / -1; }
  /* A lone attachment: full width instead of boxed into half a 2-column
     grid, and uncropped (contain, not cover) with a taller cap so it
     actually reads as a real embed instead of a tiny cropped square --
     this is the single biggest gap vs. a page like nicecrew.digital's
     own timeline, which gives a solo image/video the whole row. */
  .post-media.single { grid-template-columns: 1fr; }
  .post-media.single img, .post-media.single video { max-height: 560px; object-fit: contain; }
  .post-counts { display: flex; gap: 14px; margin-top: 8px; font-size: 13px; color: var(--muted); }
  .post-counts .count-item { display: flex; align-items: center; gap: 4px; }
  .post-reactions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .reaction-chip {
    display: inline-flex; align-items: center; gap: 3px; background: var(--hover); border-radius: var(--radius-chip);
    padding: 2px 6px; font-size: 12px; color: var(--muted);
  }
  .reaction-chip img.emoji { width: 14px; height: 14px; }
  .post-actions { display: flex; gap: 4px; margin-top: 8px; }
  .post-actions button, .post-actions a {
    border: none; background: none; padding: 6px 8px; font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 4px; border-radius: var(--radius-chip);
    text-decoration: none; cursor: pointer;
  }
  .post-actions button:hover, .post-actions a:hover { background: var(--hover); color: var(--text); }
  .post-actions button.imported, .post-actions a.imported { color: var(--ok-text); }
  a.link-button {
    font: inherit; font-size: 13px; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: var(--radius-inner); padding: 8px 14px; cursor: pointer;
    display: inline-block; text-decoration: none;
  }
  a.link-button:hover { background: var(--hover); }
  .post-card.open-thread { cursor: pointer; }
  .post-card.thread-ancestor { opacity: .8; }
  .post-card.thread-focus { background: var(--hover); border-radius: var(--radius-card); cursor: default; }
  /* No outer .card border/background here -- each post-card in the chain
     already carries its own look (the focused one's own grey highlight,
     a border-top between siblings), so wrapping that in another bordered
     box just double-boxes the selected post. */
  .thread-chain { margin-bottom: 12px; }
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
  .form-error {
    color: #e5484d; font-size: 13px; margin-top: 8px; padding: 8px 10px;
    border: 1px solid #e5484d; border-radius: var(--radius-inner); background: rgba(229,72,77,.08);
  }
  .form-textarea {
    font: inherit; font-size: 13px; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: var(--radius-inner); padding: 8px 10px; width: 100%; resize: vertical;
  }
  .file-preview {
    display: flex; align-items: center; gap: 10px; padding: 8px 10px; margin-top: 6px;
    border: 1px solid var(--border); border-radius: var(--radius-inner);
  }
  .file-preview-icon { font-size: 22px; flex-shrink: 0; }
  .file-preview-thumb-img { width: 48px; height: 27px; object-fit: cover; border-radius: var(--radius-chip); flex-shrink: 0; background: var(--hover); }
  .file-preview-meta { flex: 1; min-width: 0; }
  .file-preview-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-preview-size { font-size: 12px; }
  .file-preview-remove {
    border: none; background: none; color: var(--muted); font-size: 14px; cursor: pointer; padding: 4px; flex-shrink: 0;
  }
  .file-preview-remove:hover { color: var(--danger); }

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
  .profile-page { max-width: 700px; }
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

  /* -- scroll-to-top (Element/Haven style: a round "^" button, top right) */
  .scroll-top-btn {
    position: fixed; top: 16px; right: 16px; width: 36px; height: 36px; border-radius: 50%;
    border: 1px solid var(--border); background: var(--card); color: var(--text); font: inherit; font-weight: 700;
    cursor: pointer; display: none; align-items: center; justify-content: center;
    box-shadow: 0 2px 8px rgba(0,0,0,.15); z-index: 40;
  }
  .scroll-top-btn.visible { display: flex; }
  .scroll-top-btn:hover { background: var(--hover); }
</style>
</head>
<body>
  <div id="shell">
    <div id="sidebar"></div>
    <div id="content"><div id="app"><div class="spinner">Connecting&hellip;</div></div></div>
  </div>
  <div id="toast" class="toast"></div>
  <button id="scroll-top-btn" class="scroll-top-btn" title="Scroll to top">^</button>

<script>
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const widgetId = params.get("widgetId") || "";
  const roomId = params.get("matrix_room_id") || "";
  const theme = params.get("theme") || "";
  if (theme.indexOf("dark") !== -1) document.documentElement.setAttribute("data-theme", "dark");
  else if (theme.indexOf("light") !== -1) document.documentElement.setAttribute("data-theme", "light");

  // -- Matrix Widget API (postMessage) -- minimal hand-rolled client.
  // Every bridge action still goes through our own backend API
  // (authenticated via the verified OpenID identity), never through a
  // widget room read/write capability -- the one capability this widget
  // DOES request, MSC2931 navigate, only ever lets the host jump to a
  // matrix.to link on the user's behalf (see matrixToUrl's click
  // handling below), nothing else.
  const NAVIGATE_CAPABILITY = "org.matrix.msc2931.navigate";
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
      window.parent.postMessage(Object.assign({}, msg, { response: { capabilities: [NAVIGATE_CAPABILITY] } }), "*");
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

  // timeoutMs is optional -- without it this can hang forever if the host
  // never replies at all (confirmed live: stuck on "Connecting…" with no
  // way back into the widget, after navigating away via MSC2931 navigate
  // and back to the room -- whatever the host's own reason, OUR side
  // should never have an unbounded wait with no recovery). Resolves
  // `null` on timeout rather than rejecting, so a caller that doesn't
  // pass one keeps today's exact behavior.
  function sendFromWidget(action, data, timeoutMs) {
    return new Promise((resolve) => {
      const requestId = "req-" + (reqCounter++) + "-" + Date.now();
      let settled = false;
      pending[requestId] = (response) => {
        if (settled) return;
        settled = true;
        resolve(response);
      };
      window.parent.postMessage({ api: "fromWidget", widgetId, requestId, action, data: data || {} }, "*");
      if (timeoutMs) {
        setTimeout(() => {
          if (settled) return;
          settled = true;
          delete pending[requestId];
          resolve(null);
        }, timeoutMs);
      }
    });
  }

  // MSC2931 navigate -- resolves {} on success, {error:{...}} if the host
  // rejected it (capability not approved, or it just doesn't support the
  // action), null on timeout.
  function navigateViaWidgetApi(uri) {
    return sendFromWidget(NAVIGATE_CAPABILITY, { uri }, 2000);
  }

  // One delegated listener for every matrix.to link in the widget (post
  // cards' "Imported" badge, "Manage in room" on Publish, ...) instead of
  // wiring each one individually -- asks the host to jump straight to it
  // (room, or room+event when there is one) via MSC2931 navigate, falling
  // back to opening it in a new browser tab/window exactly as before if
  // the host doesn't support/approve navigate or the call times out.
  document.addEventListener("click", (e) => {
    const anchor = e.target.closest('a[href^="https://matrix.to/#"]');
    if (!anchor) return;
    e.preventDefault();
    const uri = anchor.href;
    navigateViaWidgetApi(uri).then((response) => {
      if (!response || response.error) window.open(uri, "_blank", "noopener");
    });
  });

  let openIdResolve = null;

  async function getOpenIdToken() {
    // This first request previously had NO timeout at all -- confirmed
    // live: after navigating away via MSC2931 navigate and back to the
    // room, the client sometimes never answers it again, leaving the
    // widget stuck on "Connecting…" forever with nothing to even show an
    // error, let alone retry. 15s is generous for what should be a
    // near-instant reply.
    const first = await sendFromWidget("get_openid", {}, 15000);
    if (first === null) throw new Error("Timed out waiting for the client to respond. Try Cancel below, or closing and reopening this widget.");
    if (first.state === "allowed") return first;
    if (first.state === "blocked") throw new Error("This client denied the identity request.");
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

  // Establishes (or re-establishes) this widget's own session with the
  // bridge backend -- the same OpenID handshake start() runs at launch,
  // pulled out so api() can also call it on demand when the session it
  // already has goes stale mid-use.
  async function establishSession() {
    const creds = await getOpenIdToken();
    const session = await api("/widget/api/session", {
      method: "POST",
      body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
    });
    sessionId = session.session_id;
  }

  // Shared across every concurrent 401 so a burst of calls failing at
  // once (e.g. several in-flight timeline/identity/context requests)
  // triggers exactly one re-auth, not one per call.
  let sessionRefreshPromise = null;
  function refreshSession() {
    if (!sessionRefreshPromise) {
      sessionRefreshPromise = establishSession().finally(() => { sessionRefreshPromise = null; });
    }
    return sessionRefreshPromise;
  }

  async function api(path, opts, _isRetry) {
    opts = opts || {};
    const headers = Object.assign(opts.raw ? {} : { "Content-Type": "application/json" }, opts.headers || {});
    if (sessionId) headers["Authorization"] = "Bearer " + sessionId;
    const body = opts.raw ? opts.raw : (opts.body ? JSON.stringify(opts.body) : undefined);
    const res = await fetch(path, { method: opts.method || "GET", headers, body });
    // A stale widget session (the 1h TTL on OUR OWN session, not the
    // fediverse instance being browsed) previously surfaced as a dead-end
    // "Session expired or unknown" error on whatever action happened to
    // hit it, or silently as an empty "Nothing to show yet" wherever the
    // caller's own error handling swallowed it (confirmed live 2026-10-25:
    // this is what a Browse timeline load, a sign-in attempt, and opening
    // a post link all looked like once the widget had been open long
    // enough for its session to expire -- three different-looking
    // symptoms, one root cause). Transparently re-run the same OpenID
    // handshake start() does and retry this exact call once before
    // actually giving up -- excluding /widget/api/session itself, so a
    // genuinely broken session-establishment call fails directly instead
    // of recursing into itself.
    if (res.status === 401 && !_isRetry && path !== "/widget/api/session") {
      try {
        await refreshSession();
        return api(path, opts, true);
      } catch (e) { /* fall through to the normal error handling below */ }
    }
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(errorMessage(data, res.status));
    return data;
  }

  // A FastAPI error body's "detail" is usually a plain string, but a 422
  // validation failure makes it a LIST of {loc, msg, type} objects instead
  // -- new Error(thatArray) previously coerced straight to
  // "[object Object],[object Object]" via Array's own toString, shown to
  // the user verbatim (observed live opening a thread). Handle every
  // shape it can actually take.
  function errorMessage(data, status) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map((d) => (d && typeof d === "object" && d.msg) ? d.msg : JSON.stringify(d)).join("; ");
    }
    if (detail && typeof detail === "object") {
      try { return JSON.stringify(detail); } catch (e) {}
    }
    return "Request failed (" + status + ")";
  }

  function toast(message, ok) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = "toast"; }, 4000);
  }

  async function doAction(path, body, busyBtn) {
    let originalText = null;
    if (busyBtn) {
      busyBtn.disabled = true;
      originalText = busyBtn.textContent;
      busyBtn.textContent = "Working…";
    }
    try {
      await api(path, { method: "POST", body: Object.assign({ room_id: roomId }, body || {}) });
      toast("Done.", true);
      await loadContext();
    } catch (e) {
      toast(e.message, false);
    } finally {
      // busyBtn may already be a detached node by now if the action's own
      // loadContext() re-render replaced it -- touching a detached node is
      // harmless (a silent no-op), so no extra guard needed here.
      if (busyBtn) {
        busyBtn.disabled = false;
        if (originalText !== null) busyBtn.textContent = originalText;
      }
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

  // roomId/eventId go straight into the URL fragment per the matrix.to
  // spec -- encodeURIComponent each part separately (not the URL as a
  // whole) so a literal "/" inside, say, a room alias can't be confused
  // with the roomId/eventId separator.
  function matrixToUrl(roomId, eventId) {
    let url = "https://matrix.to/#/" + encodeURIComponent(roomId);
    if (eventId) url += "/" + encodeURIComponent(eventId);
    return url;
  }

  function linkButton(label, href) {
    return el("a", { class: "link-button", text: label, href: href, target: "_blank", rel: "noopener" });
  }

  let ctx = null;
  const NAV_ITEMS = [
    { id: "home", label: "Home", icon: "\u{1F3E0}" },
    { id: "channels", label: "Channels", icon: "\u{1F4FA}" },
    { id: "communities", label: "Communities", icon: "\u{1F465}" },
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
  // Same idea again, for the "create a new identity" form -- was a cramped
  // inline row in the sidebar's identity dropdown; moved to a real page so
  // there's room for a real error message (reserved name, already taken,
  // belongs to a real Matrix user, ...) instead of a toast easy to miss.
  let createIdentityView = false;
  // Same idea again, for a single channel's/community's own full page --
  // mutually exclusive with everything else above (opening one clears the
  // others, same convention as profileView/threadView).
  let channelView = null;
  let communityView = null;
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
        onclick: (e) => {
          e.stopPropagation();
          createNewIdentity("")
            .then((data) => {
              if (data && data.ok === false) { toast(data.message || "Could not create a profile.", false); return; }
              toast("Profile created.", true);
              return loadContext();
            })
            .catch((err) => toast(err.message, false));
        },
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
      // Every item below needs its own e.stopPropagation() -- the popover
      // is appended INSIDE the identity-card <button> (so it lines up
      // visually, anchored to it), which means an unstopped click bubbles
      // straight up to that button's own onclick and fires openMyProfile()
      // right after, same click. That's what made "+ New identity" look
      // like it "just opens my profile" -- both handlers were firing.
      otherIdentities.forEach((i) => {
        pop.appendChild(el("button", {
          class: "nav-item", text: i.display_name || i.handle,
          onclick: (e) => {
            e.stopPropagation();
            identityDropdownOpen = false;
            api("/widget/api/identities/switch", { method: "POST", body: { room_id: roomId, username: i.username } })
              .then(() => loadContext()).catch((e2) => toast(e2.message, false));
          },
        }));
      });
      if (ctx.custom_identities_enabled) {
        pop.appendChild(el("button", {
          class: "nav-item", text: "+ New identity",
          onclick: (e) => {
            e.stopPropagation();
            identityDropdownOpen = false;
            openCreateIdentity();
          },
        }));
      }
      card.appendChild(pop);
    }
    bar.appendChild(card);
  }

  function createNewIdentity(customId) {
    const needsId = ctx.has_profile; // the very first identity uses the bare form (your Matrix localpart)
    if (needsId && !customId.trim()) return Promise.reject(new Error("Choose an id first."));
    return api("/widget/api/create_profile", { method: "POST", body: { room_id: roomId, custom_id: customId.trim() } });
  }

  function openCreateIdentity() {
    createIdentityView = true;
    profileView = null;
    threadView = null;
    render();
  }

  function renderCreateIdentityPage(app) {
    app.appendChild(el("button", {
      class: "back-link", text: "← Back",
      onclick: () => { createIdentityView = false; render(); },
    }));
    const page = el("div", { class: "profile-page" });
    page.appendChild(el("h2", { text: "New identity" }));
    page.appendChild(el("div", { class: "sub", text: "Pick an id for this identity -- letters, numbers, and underscores only. This becomes your fediverse handle: id@" + (ctx.domain || "") + "." }));
    const errorBox = el("div", { class: "form-error", style: "display:none" });
    const input = el("input", {
      type: "text", placeholder: "id",
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); submitBtn.click(); } },
    });
    const submitBtn = primaryButton("Create", () => {
        errorBox.style.display = "none";
        submitBtn.disabled = true;
        const originalText = submitBtn.textContent;
        submitBtn.textContent = "Working…";
        createNewIdentity(input.value)
          .then((data) => {
            if (data && data.ok === false) {
              errorBox.textContent = data.message || "Could not create that identity.";
              errorBox.style.display = "";
              return;
            }
            toast("Identity created.", true);
            createIdentityView = false;
            return loadContext();
          })
          .catch((e) => { errorBox.textContent = e.message; errorBox.style.display = ""; })
          .finally(() => { submitBtn.disabled = false; submitBtn.textContent = originalText; });
    });
    page.appendChild(el("div", { class: "row" }, [input, submitBtn]));
    page.appendChild(errorBox);
    app.appendChild(page);
    setTimeout(() => input.focus(), 0);
  }

  function renderSidebar() {
    const shell = document.getElementById("shell");
    shell.classList.toggle("collapsed", sidebarCollapsed);
    const bar = document.getElementById("sidebar");
    bar.innerHTML = "";
    if (!ctx) return;
    const anyOverlayShowing = !!(profileView || threadView || createIdentityView || channelView || communityView);
    NAV_ITEMS.forEach((t) => {
      if (t.id === "admin" && !ctx.is_admin) return;
      if (t.id === "channels" && !ctx.peertube_channels_enabled) return;
      if (t.id === "communities" && !ctx.lemmy_community_hosting_enabled) return;
      bar.appendChild(el("button", {
        class: "nav-item" + (!anyOverlayShowing && t.id === currentTab ? " active" : ""),
        title: t.label,
        onclick: () => {
          currentTab = t.id;
          profileView = null; threadView = null; createIdentityView = false; channelView = null; communityView = null;
          render();
        },
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

    // The Home scroll listener (set up inside renderHomeTimeline) only
    // belongs attached while Home's own timeline is actually showing --
    // otherwise it would keep recording THIS page's scroll position into
    // homeState.scrollY, corrupting what "Back" should restore.
    const goingToHome = !profileView && !threadView && !createIdentityView && !channelView && !communityView && currentTab === "home";
    if (!goingToHome && window.__homeScrollHandler) {
      window.removeEventListener("scroll", window.__homeScrollHandler);
      window.__homeScrollHandler = null;
    }

    if (createIdentityView) { renderCreateIdentityPage(app); return; }
    if (profileView) { renderProfilePage(app, profileView); return; }
    if (threadView) { renderThreadPage(app, threadView); return; }
    if (channelView) { renderChannelPage(app, channelView); return; }
    if (communityView) { renderCommunityPage(app, communityView); return; }
    if (currentTab === "home") renderHomeTimeline(app);
    else if (currentTab === "channels") renderChannelsTab(app);
    else if (currentTab === "communities") renderCommunitiesTab(app);
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
      .then((data) => {
        toast("Imported.", true);
        post.already_imported = true;
        // Without these the "View on Matrix" link that replaces the Import
        // button would point at matrix.to/#/null until the next full
        // reload re-fetches this post with its real ids (see
        // widget_import's own docstring for why these come back here).
        post.matrix_room_id = data.room_id;
        post.matrix_event_id = data.event_id;
        // Transient, never persisted or re-fetched from the server --
        // "✓ Imported" is feedback for the click that JUST happened, not
        // a status label. A post that was already imported before this
        // page even loaded (already_imported came back true on the
        // initial fetch) goes straight to "View on Matrix" instead; see
        // renderPostCard's own branch for how the two render differently
        // from the exact same already_imported=true state.
        post._justImported = true;
        if (onDone) onDone();
      })
      .catch((e) => { toast(e.message, false); })
      .finally(() => { if (btn) btn.disabled = false; });
  }

  // A standalone card (a profile's own post list, a thread's ancestors/
  // replies) has nowhere to delegate a real re-render to the way the Home
  // feed's own cards do (passing renderFeedList as onChange -- see its own
  // call site -- rebuilds that whole list). Those callers used to pass a
  // no-op instead, so importPost's own post.already_imported/matrix_room_id/
  // matrix_event_id updates (see its own comment) never reached the DOM:
  // the button kept reading "Import" until the next full page load, even
  // though the import itself had already succeeded (reported live:
  // importing from a thread's focused post left the button unchanged until
  // navigating away and back). This swaps just that one card's own node in
  // place instead, independent of whatever page it's sitting in.
  function renderLiveCard(post, opts) {
    const holder = { node: null };
    function onChange() {
      const fresh = renderLiveCard(post, opts);
      holder.node.replaceWith(fresh);
      holder.node = fresh;
    }
    holder.node = renderPostCard(post, onChange, opts);
    return holder.node;
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
        onclick: (e) => { e.stopPropagation(); openThreadView(post.uri, null, post.local_id); },
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
      // A real <a> (not a click handler on a <span>) so right-click ->
      // "Copy Link" works too, not just a left-click open -- the ↗ button
      // right next to it only ever supported the latter.
      headerRightChildren.push(el("a", {
        class: "time", text: timeAgo(post.created_at), title: fullDateTime(post.created_at),
        href: post.url, target: "_blank", rel: "noopener",
      }));
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
      const mediaItems = post.media.slice(0, 4);
      // A single attachment gets its own full-width, uncropped layout
      // (see the .post-media.single CSS) instead of sitting cramped in
      // one half of a 2-column grid meant for multiple items -- that
      // grid shape made even a single large image render as a small
      // postage stamp with half the row left empty.
      const mediaClass = "post-media" + (mediaItems.length === 1 ? " single" : "");
      children.push(el("div", { class: mediaClass }, mediaItems.map((m) => {
        if (m.type === "video") return el("video", { src: m.url, controls: "", preload: "metadata" });
        if (m.type === "audio") return el("audio", { class: "post-media-audio", src: m.url, controls: "" });
        // full_url is the real, full-resolution file -- "url" here may be
        // a smaller preview/thumbnail chosen for fast feed loading (see
        // _normalize_status's own comment), so the link target and the
        // thumbnail shown aren't always the same URL.
        return el("a", { href: m.full_url || m.url, target: "_blank", rel: "noopener" }, [
          el("img", { src: m.url, alt: m.description || "", loading: "lazy" }),
        ]);
      })));
    }
    if (focused) {
      children.push(el("div", { class: "post-timestamp-full", text: fullDateTime(post.created_at) }));
    }
    const counts = post.counts || { replies: 0, reblogs: 0, favourites: 0 };
    children.push(el("div", { class: "post-counts" }, [
      el("span", { class: "count-item", text: "\u{1F4AC} " + counts.replies, title: "Replies" }),
      el("span", { class: "count-item", text: "\u{1F501} " + counts.reblogs, title: "Boosts" }),
      el("span", { class: "count-item", text: "⭐ " + counts.favourites, title: "Favourites" }),
    ]));
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
      // A real <a>, not a button+window.open -- the document-level
      // matrix.to click listener (MSC2931 navigate) only intercepts
      // actual anchor clicks; the post-card's own click-to-thread
      // handler already ignores anything matching closest("a"), so this
      // needs no onclick of its own at all. "✓ Imported" only for the
      // one render right after a live import click (post._justImported,
      // set by importPost) -- consumed (cleared) the instant it's read
      // here, so every OTHER render of an already-imported post -- on
      // load, or this same card redrawn again later for any other
      // reason -- reads "View on Matrix" instead. Past that first
      // moment, "Imported" reads as a status label nobody asked to
      // toggle, not feedback for an action that just happened.
      const justImported = post._justImported;
      post._justImported = false;
      actions.appendChild(el("a", {
        class: "imported", text: justImported ? "✓ Imported" : "View on Matrix",
        href: matrixToUrl(post.matrix_room_id, post.matrix_event_id),
        target: "_blank", rel: "noopener",
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
      openThreadView(post.uri, opts && opts.knownChildUri, post.local_id);
    });
    card.classList.add("open-thread");
    return card;
  }

  let homeState = null;

  // -- Browse instance picker: homeserver + admin shortlist + the user's
  // own remembered instances, persisted in localStorage (per-browser,
  // never sent to or read from the server -- purely a per-viewer
  // convenience, same category as sidebarCollapsed above). ----------------

  const BROWSE_INSTANCES_STORAGE_KEY = "fediverse-bridge-browse-instances";

  function loadUserBrowseInstances() {
    try {
      const raw = localStorage.getItem(BROWSE_INSTANCES_STORAGE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter((d) => typeof d === "string" && d) : [];
    } catch (e) { return []; }
  }
  function addUserBrowseInstance(domain) {
    const list = loadUserBrowseInstances();
    if (list.includes(domain)) return;
    list.push(domain);
    try { localStorage.setItem(BROWSE_INSTANCES_STORAGE_KEY, JSON.stringify(list)); } catch (e) {}
  }
  function removeUserBrowseInstance(domain) {
    const list = loadUserBrowseInstances().filter((d) => d !== domain);
    try { localStorage.setItem(BROWSE_INSTANCES_STORAGE_KEY, JSON.stringify(list)); } catch (e) {}
  }
  function normalizeDomain(raw) {
    return (raw || "").trim().replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
  }

  // Remembers which dropdown option was last picked -- "" for Whole Known
  // Network, a domain otherwise -- so it's restored on the next visit
  // instead of always resetting. Distinct from BROWSE_INSTANCES_STORAGE_KEY
  // (the instance LIST): this is just the one currently-selected value.
  // localStorage.getItem returns null (not "") for a key that was never
  // set at all, which is exactly how loadBrowseChoice tells "nothing
  // chosen yet, use the given default" apart from "explicitly chose Whole
  // Known Network" (stored as the empty string, not absent).
  const BROWSE_CHOICE_STORAGE_KEY = "fediverse-bridge-browse-domain";

  function loadBrowseChoice(defaultDomain) {
    try {
      const raw = localStorage.getItem(BROWSE_CHOICE_STORAGE_KEY);
      return raw === null ? defaultDomain : raw;
    } catch (e) { return defaultDomain; }
  }
  function saveBrowseChoice(domain) {
    try { localStorage.setItem(BROWSE_CHOICE_STORAGE_KEY, domain); } catch (e) {}
  }

  // Merges the homeserver + admin shortlist (bridge.default_timeline_domains,
  // see widget_context's own comment) + the user's own remembered instances
  // into one deduplicated option list, in that priority order, plus a
  // trailing "+ Add instance" action-option.
  function browseInstanceOptions(selectedDomain) {
    const seen = new Set();
    const options = [{ value: "", label: "Whole Known Network" }];
    function addDomain(d, suffix) {
      if (!d || seen.has(d)) return;
      seen.add(d);
      options.push({ value: d, label: d + (suffix || "") });
    }
    addDomain(ctx.domain, " (your homeserver)");
    (ctx.default_timeline_domains || []).forEach((d) => addDomain(d));
    loadUserBrowseInstances().forEach((d) => addDomain(d));
    // A domain picked via "+ Add instance" that isn't actually saved (the
    // user cancelled, or it failed to persist) still needs to show up as
    // the selected option right now, or the <select> would silently fall
    // back to its first option instead.
    addDomain(selectedDomain);
    options.push({ value: "__add__", label: "+ Add instance…" });
    return options;
  }

  function openAddBrowseInstanceModal(onAdded) {
    const body = el("div", { class: "card profile-modal" });
    const domainInput = el("input", { type: "text", placeholder: "instance.example" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Add an instance to browse" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "row" }, [domainInput]));
    const errorBox = el("div", { class: "form-error", style: "display:none" });
    body.appendChild(errorBox);
    function submit() {
      const domain = normalizeDomain(domainInput.value);
      if (!domain) return;
      addUserBrowseInstance(domain);
      closeModal();
      onAdded(domain);
    }
    domainInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    body.appendChild(el("div", { class: "row" }, [primaryButton("Add", submit)]));
    openModal(body);
    setTimeout(() => domainInput.focus(), 0);
  }

  // Only ever offered for an instance the USER added (never the homeserver
  // or one of the admin's bridge.default_timeline_domains -- see the
  // Remove button's own render condition) -- removing one of those isn't
  // this bridge's call to make. Checks sign-in status first since removing
  // an instance you're signed into would otherwise silently orphan that
  // login (still stored, never offered again through this dropdown) --
  // better to fold it into one explicit action the user actually sees.
  function removeBrowseInstance(domain, onRemoved) {
    api("/widget/api/browse/login/list")
      .then((data) => {
        const loggedIn = (data.logins || []).some((l) => l.domain === domain);
        if (loggedIn) openRemoveInstanceWarningModal(domain, onRemoved);
        else { removeUserBrowseInstance(domain); onRemoved(); }
      })
      .catch(() => { removeUserBrowseInstance(domain); onRemoved(); });
  }
  function openRemoveInstanceWarningModal(domain, onRemoved) {
    const body = el("div", { class: "card profile-modal" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Remove " + domain + "?" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "sub", text: "You're signed into " + domain + " -- removing it from this list will also sign you out there." }));
    body.appendChild(el("div", { class: "row" }, [
      dangerButton("Remove and sign out", (e) => {
        e.target.disabled = true;
        removeUserBrowseInstance(domain);
        api("/widget/api/browse/login/logout", { method: "POST", body: { domain: domain } })
          .catch(() => {}) // best-effort -- the instance is leaving the list either way
          .finally(() => { closeModal(); onRemoved(); });
      }),
      plainButton("Cancel", closeModal),
    ]));
    openModal(body);
  }

  function renderHomeTimeline(app) {
    if (!homeState) {
      // First visit ever (nothing in BROWSE_CHOICE_STORAGE_KEY yet)
      // defaults to the user's own homeserver, not Whole Known Network --
      // see loadBrowseChoice's own comment for how it tells that apart
      // from an explicit, remembered choice of Whole Known Network
      // (stored as "").
      const initialDomain = loadBrowseChoice(ctx.domain);
      homeState = {
        mode: initialDomain ? "instance" : "home", domain: initialDomain,
        scope: "federated", cursor: "", homeCursorMap: "", posts: [], loading: false, done: false,
        unsupported: false, needsLogin: false, needsLoginDomains: [], timedOutDomains: [], scrollY: 0,
      };
    }
    const state = homeState;

    const searchInput = el("input", { type: "text", placeholder: "Search @user@instance.org, a name, or paste a post link to import" });
    const searchResults = el("div", { class: "list" });
    function switchToDomain(d) {
      state.mode = d ? "instance" : "home";
      state.domain = d;
      state.posts = []; state.cursor = ""; state.homeCursorMap = ""; state.done = false; state.unsupported = false;
      state.needsLogin = false; state.needsLoginDomains = []; state.timedOutDomains = []; state.scrollY = 0;
      saveBrowseChoice(d);
      render();
    }
    const domainSelect = el("select", {
      onchange: () => {
        const v = domainSelect.value;
        if (v === "__add__") {
          openAddBrowseInstanceModal(switchToDomain);
          domainSelect.value = state.domain || ""; // revert until/unless the modal actually adds one
          return;
        }
        switchToDomain(v);
      },
    }, browseInstanceOptions(state.domain).map((o) => {
      const attrs = { value: o.value, text: o.label };
      if (o.value === (state.domain || "")) attrs.selected = "selected";
      return el("option", attrs);
    }));

    function scopeBtn(label, value) {
      return el("button", {
        class: state.scope === value ? "active" : "", text: label,
        onclick: () => {
          if (state.scope === value) return;
          state.scope = value;
          state.posts = []; state.cursor = ""; state.done = false; state.unsupported = false;
          state.needsLogin = false; state.needsLoginDomains = []; state.timedOutDomains = []; state.scrollY = 0;
          render();
        },
      });
    }

    async function runSearchOrImport() {
      const q = searchInput.value.trim();
      if (!q) return;
      searchResults.innerHTML = "";
      if (/^https?:\/\//i.test(q)) {
        // Open it in the widget (the same thread view a post click uses)
        // instead of importing it sight unseen -- lets the user look at
        // what they're about to pull in and decide for themselves.
        searchInput.value = "";
        openThreadView(q);
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
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearchOrImport(); });

    // One box, two jobs: a handle/name looks accounts up (as before); a
    // pasted https:// link is instead handed straight to the existing
    // import action -- "pull in anything from a link or handle" without
    // needing a separate "Import a post" card.
    const goBtn = primaryButton("Go", runSearchOrImport);
    const browseRow = [
      domainSelect,
      el("div", { class: "scope-toggle" }, [scopeBtn("Local", "local"), scopeBtn("Federated", "federated")]),
    ];
    // Only ever offered for an instance the user added themselves -- never
    // the homeserver (not this bridge's to remove) or one of the admin's
    // own bridge.default_timeline_domains (an admin decision, not a
    // per-user one) -- see removeBrowseInstance's own docstring.
    if (state.mode === "instance" && state.domain && loadUserBrowseInstances().includes(state.domain)) {
      browseRow.push(dangerButton("Remove", () => removeBrowseInstance(state.domain, () => switchToDomain(""))));
    }
    const controlsChildren = [
      el("h2", { text: "Browse the fediverse" }),
      el("div", { class: "row" }, [searchInput, goBtn]),
      searchResults,
      el("div", { class: "row" }, browseRow),
    ];
    if (state.mode === "instance") {
      controlsChildren.push(el("div", { class: "sub" }, [
        el("span", { text: "Browsing " + state.domain + " (" + state.scope + ") — " }),
        el("a", {
          href: "#", text: "back to Whole Known Network",
          onclick: (e) => { e.preventDefault(); switchToDomain(""); },
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
      // A single explicitly-browsed instance (state.mode === "instance")
      // needing sign-in really does mean "nothing can show here at all" --
      // there's only the one source. Whole Known Network ("home") is
      // different: several sources are merged, so one needing sign-in (or
      // timing out -- see _HOME_FEED_SOURCE_TIMEOUT's own comment) must
      // never hide the posts the OTHER sources already returned. Its
      // banner(s) render unconditionally, above whatever post list there
      // is (even an empty one, while still loading), instead of only
      // appearing in the old "nothing to show at all" branch below.
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
      let sourceNoticeShown = false;
      if (state.mode === "home" && state.needsLoginDomains.length) {
        sourceNoticeShown = true;
        const retry = () => { state.done = false; state.needsLoginDomains = []; loadMore(); };
        feed.appendChild(el("div", { class: "placeholder browse-source-notice" }, [
          el("div", { text: "Some of your feed sources need you to be signed in:", style: "margin-bottom:10px" }),
          ...state.needsLoginDomains.map((d) => el("div", { class: "row", style: "justify-content:center" }, [
            el("span", { text: d, style: "align-self:center" }),
            primaryButton("Sign in", () => openSignInModal(d, retry)),
          ])),
        ]));
      }
      if (state.mode === "home" && state.timedOutDomains.length) {
        sourceNoticeShown = true;
        const retry = () => { state.done = false; state.timedOutDomains = []; loadMore(); };
        feed.appendChild(el("div", { class: "placeholder browse-source-notice" }, [
          el("div", {
            text: (state.timedOutDomains.length === 1
              ? state.timedOutDomains[0] + " didn't respond in time."
              : "These sources didn't respond in time: " + state.timedOutDomains.join(", ")),
            style: "margin-bottom:10px",
          }),
          plainButton("Retry", retry),
        ]));
      }
      if (!state.posts.length && !state.loading) {
        if (sourceNoticeShown) return; // already explained why there's nothing (yet)
        feed.appendChild(el("div", {
          class: "placeholder",
          text: state.unsupported
            ? "Browsing isn't available there (it doesn't run compatible software, or public preview is turned off)."
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
          // extra_domains: the user's own added instances, merged into
          // Whole Known Network alongside bridge.default_timeline_domains
          // -- see browse_home's own docstring for why this bridge needs
          // telling every time rather than remembering it server-side.
          data = await api(
            "/widget/api/browse/home?max_id_map=" + encodeURIComponent(state.homeCursorMap || "") +
            "&extra_domains=" + encodeURIComponent(loadUserBrowseInstances().join(","))
          );
          state.homeCursorMap = data.next_max_id_map || "";
          state.done = !data.next_max_id_map;
          state.unsupported = !data.supported;
          state.needsLoginDomains = data.needs_login_domains || [];
          state.timedOutDomains = data.timed_out_domains || [];
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
        // Previously silent -- a failed load (e.g. this bridge's own
        // widget session expiring mid-use, before api()'s own retry-once
        // fix existed/if that retry also fails) just marked the feed
        // "done" with zero explanation, rendering as a bare "Nothing to
        // show yet" indistinguishable from an instance that's genuinely
        // empty (confirmed live 2026-10-25).
        state.done = true;
        toast(e.message, false);
      } finally {
        state.loading = false;
        renderFeedList();
      }
    }

    renderFeedList();

    function restoreScroll() {
      if (!state.scrollY) return;
      // Double rAF: the freshly-rendered posts need a layout pass before
      // the page is even tall enough to scroll to a saved position.
      requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, state.scrollY)));
    }
    if (!state.posts.length) {
      loadMore().then(restoreScroll);
    } else {
      restoreScroll();
    }

    if (window.__homeScrollHandler) window.removeEventListener("scroll", window.__homeScrollHandler);
    window.__homeScrollHandler = () => { state.scrollY = window.scrollY; };
    window.addEventListener("scroll", window.__homeScrollHandler);

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
          data.posts.forEach((post) => { post.author = author; feed.appendChild(renderLiveCard(post)); });
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

  function openThreadView(uri, knownChildUri, localId) {
    if (!uri) return;
    // knownChildUri: when navigating UP to an ancestor from a thread
    // already open, we just walked its own inReplyTo chain to get here,
    // so we already KNOW at least this one reply exists -- see
    // renderThreadPage's own chain.forEach, which supplies it. Passed
    // straight through to the backend so it's never at the mercy of
    // whatever reply-discovery happens to work (or doesn't) on that
    // instance (confirmed live 2026-10-25: a Pleroma post's own
    // Mastodon-API-context-search fallback can come up completely empty
    // even though a reply definitely exists, when the canonical AS2 id
    // clicked into isn't the same id its Mastodon API actually indexes
    // by -- see _fetch_context_descendant_pool's own comment).
    //
    // localId: this post's own Mastodon-API status id, when the post we
    // clicked from already knew it (a timeline card's own "local_id", or
    // one the PREVIOUS thread view's own counts resolution found for it
    // -- see _resolve_thread_counts). Lets the backend fetch guaranteed-
    // accurate reply/like/repost counts for this whole thread instead of
    // guessing an id from the URL, which only works for a pretty/flake-
    // id permalink, not a bare canonical AS2 id.
    threadView = { uri: uri, knownChildUri: knownChildUri || null, localId: localId || null };
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
      let originalText = null;
      if (btn) { btn.disabled = true; originalText = btn.textContent; btn.textContent = "Working…"; }
      api(path, { method: "POST", body: { room_id: roomId, handle: p.handle } })
        .then(() => { toast("Done.", true); return refresh(); })
        .catch((e) => toast(e.message, false))
        .finally(() => { if (btn) { btn.disabled = false; if (originalText !== null) btn.textContent = originalText; } });
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
          : dangerButton("Block", withConfirm(
              "Block this account? This cuts any follow and kicks you from their rooms.",
              (btn) => profileAction("/widget/api/block", p, btn)
            )),
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

    const qs = "uri=" + encodeURIComponent(view.uri) +
      (view.knownChildUri ? "&known_child_uri=" + encodeURIComponent(view.knownChildUri) : "") +
      (view.localId ? "&local_id=" + encodeURIComponent(view.localId) : "");
    api("/widget/api/browse/thread?" + qs).then((data) => {
      page.innerHTML = "";
      if (!data.post) {
        page.appendChild(el("div", { class: "muted", text: "Couldn't load that post." }));
        return;
      }
      // Ancestors + the focused post form one connected chain, lines
      // running avatar-to-avatar up to the thread root (or as far back
      // as this bridge could resolve).
      const rootChain = el("div", { class: "thread-chain" });
      const chain = (data.ancestors || []).concat([data.post]);
      chain.forEach((p, i) => {
        const isFocus = i === chain.length - 1;
        // The next link down this SAME chain is a real, already-known
        // reply to p -- see openThreadView's own comment for why this
        // gets threaded through on click instead of trusting reply-
        // discovery alone once p becomes the new focus.
        const knownChildUri = !isFocus && chain[i + 1] ? chain[i + 1].uri : null;
        // Defensive floor, on top of the backend's own more accurate
        // counts resolution (_resolve_thread_counts): if THAT couldn't
        // resolve real counts for this ancestor either, the chain itself
        // still proves at least one reply exists (the next link down),
        // so the displayed count should never read lower than that.
        if (knownChildUri && p.counts && (p.counts.replies || 0) < 1) {
          p.counts = Object.assign({}, p.counts, { replies: 1 });
        }
        const card = renderLiveCard(p, { focused: isFocus, knownChildUri: knownChildUri });
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
          const replyCard = renderLiveCard(reply);
          if (reply.op_reply) {
            replyCard.classList.add("chain-link");
            branch.appendChild(replyCard);
            branch.appendChild(renderLiveCard(reply.op_reply));
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

  // The one-click "Sign in" a Whole Known Network source notice offers
  // (see renderFeedList's own needsLoginDomains branch) has no room to
  // show both OAuth and username/password inline the way the single-
  // instance "needs sign-in" placeholder does -- this is that same choice
  // in a popup instead, reusing startOAuthLogin/openPasswordLoginForm
  // verbatim rather than a second, parallel sign-in implementation.
  function openSignInModal(domain, onDone) {
    const body = el("div", { class: "card profile-modal" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Sign in to " + domain }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "row" }, [
      primaryButton("Sign in with OAuth", (e) => startOAuthLogin(domain, e.target, () => { closeModal(); if (onDone) onDone(); })),
    ]));
    body.appendChild(el("div", { class: "row" }, [
      plainButton("Use username/password instead", () => { closeModal(); openPasswordLoginForm(domain, onDone); }),
    ]));
    openModal(body);
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
              const reasonText = data.reason
                ? "Couldn't reliably check right now: " + data.reason + " (ask the bridge operator to look into this -- it's not something you can fix yourself)."
                : "Couldn't reliably check right now -- try again shortly.";
              result.appendChild(el("div", { class: "muted", text: reasonText }));
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
                dangerButton(
                  "Confirm: leave " + data.rooms.length + " room" + (data.rooms.length === 1 ? "" : "s"),
                  withConfirm(
                    "Remove yourself from " + data.rooms.length + " room(s)? This cannot be undone.",
                    async (btn) => {
                      const original = btn.textContent;
                      btn.disabled = true;
                      btn.textContent = "Working…";
                      try {
                        await api("/widget/api/leave_unfollowed/execute", {
                          method: "POST", body: { room_id: roomId },
                        });
                        toast("Done.", true);
                        // Replace the now-stale room list + button directly --
                        // this panel is built outside the normal render()
                        // cycle, so loadContext() alone wouldn't touch it and
                        // it would otherwise keep showing the pre-leave list,
                        // looking exactly like the click had done nothing
                        // (confirmed live 2026-10-24).
                        result.innerHTML = "";
                        result.appendChild(el("div", { class: "muted", text: "Left " + data.rooms.length + " room(s)." }));
                      } catch (err) {
                        toast(err.message, false);
                        btn.disabled = false;
                        btn.textContent = original;
                      }
                    }
                  )
                ),
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

  // -- Channels / Communities: shared list+create pattern ---------------

  function openCreateEntityModal(title, sub, createPath, onCreated) {
    const idInput = el("input", { type: "text", placeholder: "id (e.g. my-show)" });
    const nameInput = el("input", { type: "text", placeholder: "Display name (optional)" });
    const identitySelect = identityState.identities.length > 1
      ? el("select", {}, identityState.identities.map((i) => el("option", { value: i.username, text: i.display_name || i.handle })))
      : null;
    const row = [idInput, nameInput];
    if (identitySelect) row.push(identitySelect);
    const body = el("div", { class: "card profile-modal" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: title }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "sub", text: sub }));
    body.appendChild(el("div", { class: "row" }, row));
    body.appendChild(el("div", { class: "row" }, [
      primaryButton("Create", (e) => {
        if (!idInput.value.trim()) return;
        doAction(createPath, {
          id: idInput.value.trim(),
          display_name: nameInput.value.trim(),
          as_identity: identitySelect ? identitySelect.value : "",
        }, e.target).then(() => { closeModal(); if (onCreated) onCreated(); });
      }),
    ]));
    openModal(body);
    setTimeout(() => idInput.focus(), 0);
  }

  function entityListItem(e, onOpen) {
    return el("div", { class: "list-item clickable", onclick: onOpen }, [
      avatarEl(e.icon_url, e.display_name || e.username, "sm"),
      el("div", { class: "meta", style: "flex:1;min-width:0;" }, [
        el("div", { class: "name", text: e.display_name || e.username }),
        el("div", { class: "handle", text: "@" + e.handle }),
      ]),
      linkButton("Manage in room", matrixToUrl(e.room_id, null)),
    ]);
  }

  function renderChannelsTab(app) {
    if (!ctx.peertube_channels_enabled) { placeholder(app, "PeerTube channels aren't enabled on this bridge."); return; }
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [
      el("div", { class: "row", style: "justify-content:space-between;align-items:center;flex-wrap:nowrap;" }, [
        el("h2", { text: "PeerTube Channels", style: "margin:0;" }),
        primaryButton("Create Channel", () => openCreateEntityModal(
          "Create a channel", "Hosts a PeerTube-compatible video channel as a Matrix room.",
          "/widget/api/channels/create", () => render(),
        )),
      ]),
      list,
    ]));
    api("/widget/api/channels").then((data) => {
      list.innerHTML = "";
      if (!data.channels.length) { list.appendChild(el("div", { class: "muted", text: "No channels yet." })); return; }
      data.channels.forEach((c) => {
        const row = entityListItem(c, (ev) => {
          if (ev.target.closest("a")) return; // "Manage in room" handles its own click
          openChannelPage(c.username);
        });
        list.appendChild(row);
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  function renderCommunitiesTab(app) {
    if (!ctx.lemmy_community_hosting_enabled) { placeholder(app, "Lemmy communities aren't enabled on this bridge."); return; }
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [
      el("div", { class: "row", style: "justify-content:space-between;align-items:center;flex-wrap:nowrap;" }, [
        el("h2", { text: "Lemmy Communities", style: "margin:0;" }),
        primaryButton("Create Community", () => openCreateEntityModal(
          "Create a community", "Hosts a Lemmy-compatible community as a Matrix room.",
          "/widget/api/communities/create", () => render(),
        )),
      ]),
      list,
    ]));
    api("/widget/api/communities").then((data) => {
      list.innerHTML = "";
      if (!data.communities.length) { list.appendChild(el("div", { class: "muted", text: "No communities yet." })); return; }
      data.communities.forEach((c) => {
        const row = entityListItem(c, (ev) => {
          if (ev.target.closest("a")) return;
          openCommunityPage(c.username);
        });
        list.appendChild(row);
      });
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  // -- Channel page: avatar/name/summary, owner controls, video tiles ---

  function openChannelPage(username) {
    channelView = { username: username };
    profileView = null; threadView = null; createIdentityView = false; communityView = null;
    render();
  }

  function renderVideoTile(v, channelUsername, isOwner, onChanged) {
    const tile = el("div", { class: "video-tile" });
    const thumbWrap = el("div", { class: "video-tile-thumb" });
    if (v.thumbnail_url) thumbWrap.appendChild(el("img", { src: v.thumbnail_url, alt: "" }));
    else thumbWrap.appendChild(el("div", { class: "video-tile-thumb-empty", text: "\u{1F3A5}" }));
    if (v.sensitive) thumbWrap.appendChild(el("span", { class: "video-tile-badge", text: "Sensitive" }));
    tile.appendChild(thumbWrap);
    tile.appendChild(el("div", { class: "video-tile-title", text: v.name }));
    const metaBits = [];
    if (v.published) metaBits.push(timeAgo(v.published));
    if (v.tags && v.tags.length) metaBits.push(v.tags.slice(0, 3).join(", "));
    tile.appendChild(el("div", { class: "video-tile-meta", text: metaBits.join(" · ") }));
    const links = el("div", { class: "video-tile-links" });
    if (v.matrix_room_id) {
      links.appendChild(el("a", {
        class: "video-tile-link", text: "View in room",
        href: matrixToUrl(v.matrix_room_id, v.matrix_event_id), target: "_blank", rel: "noopener",
      }));
    }
    tile.appendChild(links);
    if (isOwner) {
      tile.appendChild(el("div", { class: "row video-tile-actions" }, [
        plainButton("Edit", () => openEditVideoModal(channelUsername, v, onChanged)),
        dangerButton("Delete", () => openDeleteVideoModal(channelUsername, v, onChanged)),
      ]));
    }
    return tile;
  }

  function renderChannelPage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { channelView = null; currentTab = "channels"; render(); } }));
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    api("/widget/api/channels/" + encodeURIComponent(view.username)).then((c) => {
      page.innerHTML = "";
      page.appendChild(profileBanner(c.banner_url, null));
      const actions = el("div", { class: "row" }, [
        linkButton("Manage in room", matrixToUrl(c.room_id, null)),
        el("a", { class: "link-button", text: "View public page", href: c.public_url, target: "_blank", rel: "noopener" }),
      ]);
      appendProfileIdentity(page, c.icon_url, c.display_name || c.handle, "@" + c.handle, actions);
      if (c.summary) { const bio = el("div", { class: "profile-bio" }); bio.innerHTML = c.summary; page.appendChild(bio); }
      if (c.is_owner) {
        page.appendChild(el("div", { class: "row" }, [
          primaryButton("Publish Video", () => openPublishVideoModal(c.username, () => { loadVideos(); })),
        ]));
      }
      page.appendChild(el("h2", { text: "Videos" }));
      const grid = el("div", { class: "video-grid" }, [el("div", { class: "muted", text: "Loading…" })]);
      page.appendChild(grid);
      function loadVideos() {
        grid.innerHTML = "";
        api("/widget/api/channels/" + encodeURIComponent(view.username) + "/videos").then((data) => {
          grid.innerHTML = "";
          if (!data.videos.length) { grid.appendChild(el("div", { class: "muted", text: "No videos published yet." })); return; }
          data.videos.forEach((v) => grid.appendChild(renderVideoTile(v, view.username, c.is_owner, loadVideos)));
        }).catch(() => { grid.innerHTML = ""; grid.appendChild(el("div", { class: "muted", text: "Couldn't load videos." })); });
      }
      loadVideos();
    }).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load that channel." }));
    });
  }

  // -- Community page: avatar/name/summary, owner controls --------------

  function openCommunityPage(username) {
    communityView = { username: username };
    profileView = null; threadView = null; createIdentityView = false; channelView = null;
    render();
  }

  function renderCommunityPage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { communityView = null; currentTab = "communities"; render(); } }));
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    api("/widget/api/communities/" + encodeURIComponent(view.username)).then((c) => {
      page.innerHTML = "";
      page.appendChild(profileBanner(c.banner_url, null));
      const actions = el("div", { class: "row" }, [
        linkButton("Manage in room", matrixToUrl(c.room_id, null)),
        el("a", { class: "link-button", text: "View public page", href: c.public_url, target: "_blank", rel: "noopener" }),
      ]);
      appendProfileIdentity(page, c.icon_url, c.display_name || c.handle, "@" + c.handle, actions);
      if (c.summary) { const bio = el("div", { class: "profile-bio" }); bio.innerHTML = c.summary; page.appendChild(bio); }
      page.appendChild(el("div", { class: "sub", text: c.moderator_count + " moderator" + (c.moderator_count === 1 ? "" : "s") }));
      if (c.is_owner) {
        page.appendChild(el("div", { class: "sub", text: "You created this community. Manage posts, moderators, and settings from its room." }));
      }
    }).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load that community." }));
    });
  }

  // -- Publish video modal ------------------------------------------------

  function formatFileSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Reads duration/width/height off a locally-staged File via an
  // offscreen <video> element, the one piece of real video metadata a
  // raw-bytes upload can't carry on its own (unlike a native Matrix
  // client, which probes the file and sets these on content.info BEFORE
  // sending -- see widget_publish_video's own docstring for why this
  // matters: PeerTube's remote-video import silently discards a video
  // with no duration, the exact same "delivery succeeds, nothing ever
  // appears, no error anywhere" failure class as a missing thumbnail).
  function probeVideoMetadata(videoSrc) {
    return new Promise((resolve, reject) => {
      const video = document.createElement("video");
      video.preload = "metadata";
      video.src = videoSrc;
      video.addEventListener("loadedmetadata", () => {
        resolve({
          durationMs: isFinite(video.duration) ? Math.round(video.duration * 1000) : null,
          width: video.videoWidth || null,
          height: video.videoHeight || null,
        });
      });
      video.addEventListener("error", () => reject(new Error("Could not read that video's metadata.")));
    });
  }

  // Grabs one frame of videoSrc (an object URL for a locally-staged File,
  // or a real https:// media URL) as a JPEG Blob -- used both for the
  // thumbnail preview and the actual upload, so what's previewed is
  // exactly what gets published. Seeks a little way in rather than
  // grabbing frame zero: a video's very first frame is often a black
  // flash/fade-in, so ~1s (or 10% into a very short clip) is the usual
  // convention for an auto-generated thumbnail.
  function extractVideoThumbnail(videoSrc) {
    return new Promise((resolve, reject) => {
      const video = document.createElement("video");
      video.preload = "metadata";
      video.muted = true;
      video.playsInline = true;
      video.src = videoSrc;
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = Math.min(1, (video.duration || 2) * 0.1);
      });
      video.addEventListener("seeked", () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
          canvas.toBlob((blob) => {
            if (blob) resolve(blob);
            else reject(new Error("Could not generate a thumbnail from that video."));
          }, "image/jpeg", 0.85);
        } catch (e) { reject(e); }
      });
      video.addEventListener("error", () => reject(new Error("Could not load that video to generate a thumbnail.")));
    });
  }

  function openPublishVideoModal(channelUsername, onDone) {
    const body = el("div", { class: "card profile-modal publish-video-modal" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Publish a video" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));

    // The file picked here is only read into memory and staged -- nothing
    // is sent to the homeserver until Publish is actually clicked (see
    // doPublish below), so changing your mind (or picking a different
    // file) never wastes an upload.
    let selectedFile = null;
    // {durationMs, width, height} or null -- probed the instant a file is
    // chosen (see probeVideoMetadata's own docstring for why this needs
    // to happen at all).
    let selectedFileMeta = null;
    const mxcInput = el("input", { type: "text", placeholder: "mxc://... (paste an existing one)" });
    const fileInput = el("input", { type: "file", accept: "video/*" });
    const filePreview = el("div", { class: "file-preview", style: "display:none" });

    function clearFile() {
      selectedFile = null;
      selectedFileMeta = null;
      fileInput.value = "";
      filePreview.style.display = "none";
      filePreview.innerHTML = "";
      fileInput.style.display = "";
      mxcInput.disabled = false;
    }
    function showFilePreview(file) {
      filePreview.innerHTML = "";
      filePreview.style.display = "";
      filePreview.appendChild(el("span", { class: "file-preview-icon", text: "\u{1F3AC}" }));
      filePreview.appendChild(el("div", { class: "file-preview-meta" }, [
        el("div", { class: "file-preview-name", text: file.name }),
        el("div", { class: "file-preview-size muted", text: formatFileSize(file.size) }),
      ]));
      filePreview.appendChild(el("button", {
        class: "file-preview-remove", text: "✕", title: "Remove",
        onclick: (e) => { e.preventDefault(); clearFile(); },
      }));
      fileInput.style.display = "none";
    }
    fileInput.addEventListener("change", () => {
      const file = fileInput.files[0];
      if (!file) return;
      selectedFile = file;
      mxcInput.value = "";
      mxcInput.disabled = true;
      showFilePreview(file);
      const objectUrl = URL.createObjectURL(file);
      probeVideoMetadata(objectUrl)
        .then((meta) => { if (selectedFile === file) selectedFileMeta = meta; })
        .catch(() => {}) // best-effort -- publishing still proceeds without duration/dimensions
        .finally(() => URL.revokeObjectURL(objectUrl));
    });
    mxcInput.addEventListener("input", () => {
      fileInput.disabled = !!mxcInput.value.trim();
    });

    body.appendChild(el("div", { class: "sub", text: "Paste an mxc:// you already have, or upload a video file." }));
    body.appendChild(el("div", { class: "row" }, [mxcInput]));
    body.appendChild(el("div", { class: "row" }, [fileInput]));
    body.appendChild(filePreview);

    // Thumbnail: pick an image by hand, or leave it and one gets grabbed
    // automatically from a frame of the video at publish time (only
    // possible when uploading a file directly -- there's no client-side
    // way to generate one from a bare pasted mxc:// without fetching it
    // first, so that path stays manual-thumbnail-only or none at all,
    // same as before this feature existed).
    let thumbnailFile = null;
    const thumbnailInput = el("input", { type: "file", accept: "image/*" });
    const thumbnailPreview = el("div", { class: "file-preview", style: "display:none" });
    function clearThumbnail() {
      thumbnailFile = null;
      thumbnailInput.value = "";
      thumbnailPreview.style.display = "none";
      thumbnailPreview.innerHTML = "";
      thumbnailInput.style.display = "";
    }
    function showThumbnailPreview(file) {
      thumbnailPreview.innerHTML = "";
      thumbnailPreview.style.display = "";
      const img = el("img", { class: "file-preview-thumb-img", src: URL.createObjectURL(file), alt: "" });
      thumbnailPreview.appendChild(img);
      thumbnailPreview.appendChild(el("div", { class: "file-preview-meta" }, [
        el("div", { class: "file-preview-name", text: file.name }),
        el("div", { class: "file-preview-size muted", text: formatFileSize(file.size) }),
      ]));
      thumbnailPreview.appendChild(el("button", {
        class: "file-preview-remove", text: "✕", title: "Remove",
        onclick: (e) => { e.preventDefault(); clearThumbnail(); },
      }));
      thumbnailInput.style.display = "none";
    }
    thumbnailInput.addEventListener("change", () => {
      const file = thumbnailInput.files[0];
      if (!file) return;
      thumbnailFile = file;
      showThumbnailPreview(file);
    });
    body.appendChild(el("div", { class: "sub", text: "Thumbnail (optional) -- leave blank to grab one from the video automatically." }));
    body.appendChild(el("div", { class: "row" }, [thumbnailInput]));
    body.appendChild(thumbnailPreview);

    const nameInput = el("input", { type: "text", placeholder: "Title" });
    const descInput = el("textarea", { class: "form-textarea", placeholder: "Description (optional)", rows: "3" });
    const tagsInput = el("input", { type: "text", placeholder: "Tags, comma-separated (optional)" });
    const categorySelect = el("select", {}, [el("option", { value: "", text: "(no category)" })]);
    const licenseSelect = el("select", {}, [el("option", { value: "", text: "(no license)" })]);
    const languageInput = el("input", { type: "text", placeholder: "Language code, e.g. en (optional)" });
    const sensitiveCheck = el("input", { type: "checkbox" });
    const commentsCheck = el("input", { type: "checkbox", checked: "checked" });

    api("/widget/api/video_options").then((opts) => {
      opts.categories.forEach((c) => categorySelect.appendChild(el("option", { value: c, text: c })));
      opts.licences.forEach((l) => licenseSelect.appendChild(el("option", { value: l, text: l })));
    }).catch(() => {});

    body.appendChild(el("div", { class: "row" }, [nameInput]));
    body.appendChild(el("div", { class: "row" }, [descInput]));
    body.appendChild(el("div", { class: "row" }, [tagsInput]));
    body.appendChild(el("div", { class: "row" }, [categorySelect, licenseSelect, languageInput]));
    body.appendChild(el("div", { class: "row" }, [
      el("label", { class: "checkbox-label" }, [sensitiveCheck, el("span", { text: " Sensitive content" })]),
      el("label", { class: "checkbox-label" }, [commentsCheck, el("span", { text: " Comments enabled" })]),
    ]));
    const errorBox = el("div", { class: "form-error", style: "display:none" });
    body.appendChild(errorBox);

    const submitBtn = primaryButton("Publish", () => {
      errorBox.style.display = "none";
      const pastedMxc = (mxcInput.value || "").trim();
      if (!selectedFile && !pastedMxc.startsWith("mxc://")) {
        errorBox.textContent = "Paste an mxc:// or choose a video file first.";
        errorBox.style.display = "";
        return;
      }
      if (!nameInput.value.trim()) { errorBox.textContent = "A title is required."; errorBox.style.display = ""; return; }
      submitBtn.disabled = true;
      const originalText = submitBtn.textContent;

      function uploadThumbnail(blob, filename) {
        return api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/upload_thumbnail", {
          method: "POST", headers: { "Content-Type": blob.type || "image/jpeg" }, raw: blob,
        }).then((data) => data.mxc);
      }
      function resolveThumbnailMxc() {
        if (thumbnailFile) return uploadThumbnail(thumbnailFile, thumbnailFile.name);
        if (!selectedFile) return Promise.resolve(""); // paste-mxc path -- no local video to grab a frame from
        submitBtn.textContent = "Generating thumbnail…";
        const objectUrl = URL.createObjectURL(selectedFile);
        return extractVideoThumbnail(objectUrl)
          .then((blob) => uploadThumbnail(blob, "thumbnail.jpg"))
          // Best-effort: a failed auto-thumbnail should never block
          // publishing the video itself -- same "no thumbnail is just a
          // degraded case, not an error" fallback _handle_publish's own
          // confirmation message already describes.
          .catch(() => "")
          .finally(() => URL.revokeObjectURL(objectUrl));
      }

      function doPublish(mxc, filename, mimetype, size, thumbnailMxc, meta) {
        submitBtn.textContent = "Publishing…";
        return api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/publish", {
          method: "POST",
          body: {
            mxc: mxc, filename: filename || "", mimetype: mimetype || "", size: size || null,
            thumbnail_mxc: thumbnailMxc || "",
            duration_ms: meta ? meta.durationMs : null,
            width: meta ? meta.width : null,
            height: meta ? meta.height : null,
            name: nameInput.value.trim(),
            description: descInput.value.trim(),
            tags: tagsInput.value.trim(),
            category: categorySelect.value,
            license: licenseSelect.value,
            language: languageInput.value.trim(),
            sensitive: sensitiveCheck.checked,
            comments_enabled: commentsCheck.checked,
          },
        }).then((data) => {
          if (data && data.ok === false) { errorBox.textContent = data.message || "Could not publish that video."; errorBox.style.display = ""; return; }
          toast(data.message || "Submitted.", true);
          closeModal();
          if (onDone) onDone();
        });
      }

      let publishPromise;
      if (selectedFile) {
        submitBtn.textContent = "Uploading…";
        // selectedFileMeta is usually already populated (probed the instant
        // the file was chosen -- see fileInput's own change handler), but a
        // Publish click right after picking a file could beat that probe to
        // the punch; re-probing here if it's not ready yet (same function,
        // same file) is cheap and makes sure duration/width/height are
        // never silently skipped just from timing.
        const metaPromise = selectedFileMeta
          ? Promise.resolve(selectedFileMeta)
          : probeVideoMetadata(URL.createObjectURL(selectedFile)).catch(() => null);
        publishPromise = Promise.all([
          api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/upload_video", {
            method: "POST", headers: { "Content-Type": selectedFile.type || "video/mp4" }, raw: selectedFile,
          }),
          resolveThumbnailMxc(),
          metaPromise,
        ]).then(([data, thumbnailMxc, meta]) => doPublish(data.mxc, selectedFile.name, data.mimetype, data.size, thumbnailMxc, meta));
      } else {
        publishPromise = resolveThumbnailMxc().then((thumbnailMxc) => doPublish(pastedMxc, "", "", null, thumbnailMxc, null));
      }
      publishPromise
        .catch((e) => { errorBox.textContent = e.message; errorBox.style.display = ""; })
        .finally(() => { submitBtn.disabled = false; submitBtn.textContent = originalText; });
    });
    body.appendChild(el("div", { class: "row" }, [submitBtn]));
    openModal(body);
  }

  // -- Edit / delete a published video -----------------------------------

  function openEditVideoModal(channelUsername, video, onDone) {
    const modalBody = el("div", { class: "card profile-modal publish-video-modal" });
    modalBody.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Edit video" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    modalBody.appendChild(el("div", { class: "sub", text: "The video file itself can't be changed here -- everything else can." }));

    // Thumbnail: shows the current one (if any); picking a new file
    // replaces it, otherwise it's left exactly as it was (see
    // widget_edit_video's own docstring for why "didn't touch it" must
    // never be read as "clear it").
    let thumbnailFile = null;
    const thumbnailInput = el("input", { type: "file", accept: "image/*" });
    const thumbnailPreview = el("div", { class: "file-preview", style: video.thumbnail_url ? "" : "display:none" });
    function clearThumbnail() {
      thumbnailFile = null;
      thumbnailInput.value = "";
      thumbnailPreview.style.display = "none";
      thumbnailPreview.innerHTML = "";
      thumbnailInput.style.display = "";
    }
    function showThumbnailPreview(srcUrl, label) {
      thumbnailPreview.innerHTML = "";
      thumbnailPreview.style.display = "";
      thumbnailPreview.appendChild(el("img", { class: "file-preview-thumb-img", src: srcUrl, alt: "" }));
      thumbnailPreview.appendChild(el("div", { class: "file-preview-meta" }, [el("div", { class: "file-preview-name", text: label })]));
      thumbnailPreview.appendChild(el("button", {
        class: "file-preview-remove", text: "✕", title: "Remove",
        onclick: (e) => { e.preventDefault(); clearThumbnail(); },
      }));
      thumbnailInput.style.display = "none";
    }
    if (video.thumbnail_url) showThumbnailPreview(video.thumbnail_url, "Current thumbnail");
    thumbnailInput.addEventListener("change", () => {
      const file = thumbnailInput.files[0];
      if (!file) return;
      thumbnailFile = file;
      showThumbnailPreview(URL.createObjectURL(file), file.name);
    });
    modalBody.appendChild(el("div", { class: "sub", text: "Thumbnail" }));
    modalBody.appendChild(el("div", { class: "row" }, [thumbnailInput]));
    modalBody.appendChild(thumbnailPreview);

    const nameInput = el("input", { type: "text", placeholder: "Title", value: video.name || "" });
    const descInput = el("textarea", { class: "form-textarea", placeholder: "Description (optional)", rows: "3", text: video.description || "" });
    const tagsInput = el("input", { type: "text", placeholder: "Tags, comma-separated (optional)", value: (video.tags || []).join(", ") });
    const categorySelect = el("select", {}, [el("option", { value: "", text: "(no category)" })]);
    const licenseSelect = el("select", {}, [el("option", { value: "", text: "(no license)" })]);
    const languageInput = el("input", { type: "text", placeholder: "Language code, e.g. en (optional)", value: video.language || "" });
    const sensitiveCheck = el("input", Object.assign({ type: "checkbox" }, video.sensitive ? { checked: "checked" } : {}));
    const commentsCheck = el("input", Object.assign({ type: "checkbox" }, video.comments_enabled !== false ? { checked: "checked" } : {}));

    api("/widget/api/video_options").then((opts) => {
      opts.categories.forEach((c) => categorySelect.appendChild(el("option", { value: c, text: c })));
      opts.licences.forEach((l) => licenseSelect.appendChild(el("option", { value: l, text: l })));
      categorySelect.value = video.category || "";
      licenseSelect.value = video.license || "";
    }).catch(() => {});

    modalBody.appendChild(el("div", { class: "row" }, [nameInput]));
    modalBody.appendChild(el("div", { class: "row" }, [descInput]));
    modalBody.appendChild(el("div", { class: "row" }, [tagsInput]));
    modalBody.appendChild(el("div", { class: "row" }, [categorySelect, licenseSelect, languageInput]));
    modalBody.appendChild(el("div", { class: "row" }, [
      el("label", { class: "checkbox-label" }, [sensitiveCheck, el("span", { text: " Sensitive content" })]),
      el("label", { class: "checkbox-label" }, [commentsCheck, el("span", { text: " Comments enabled" })]),
    ]));
    const errorBox = el("div", { class: "form-error", style: "display:none" });
    modalBody.appendChild(errorBox);

    const submitBtn = primaryButton("Save Changes", () => {
      errorBox.style.display = "none";
      if (!nameInput.value.trim()) { errorBox.textContent = "A title is required."; errorBox.style.display = ""; return; }
      submitBtn.disabled = true;
      const originalText = submitBtn.textContent;

      function doEdit(thumbnailMxc) {
        submitBtn.textContent = "Saving…";
        return api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/videos/" + encodeURIComponent(video.id) + "/edit", {
          method: "POST",
          body: {
            thumbnail_mxc: thumbnailMxc || "",
            name: nameInput.value.trim(),
            description: descInput.value.trim(),
            tags: tagsInput.value.trim(),
            category: categorySelect.value,
            license: licenseSelect.value,
            language: languageInput.value.trim(),
            sensitive: sensitiveCheck.checked,
            comments_enabled: commentsCheck.checked,
          },
        }).then((data) => {
          if (data && data.ok === false) { errorBox.textContent = data.message || "Could not save those changes."; errorBox.style.display = ""; return; }
          toast(data.message || "Submitted.", true);
          closeModal();
          if (onDone) onDone();
        });
      }

      const editPromise = thumbnailFile
        ? api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/upload_thumbnail", {
            method: "POST", headers: { "Content-Type": thumbnailFile.type || "image/jpeg" }, raw: thumbnailFile,
          }).then((data) => doEdit(data.mxc))
        : doEdit("");
      editPromise
        .catch((e) => { errorBox.textContent = e.message; errorBox.style.display = ""; })
        .finally(() => { submitBtn.disabled = false; submitBtn.textContent = originalText; });
    });
    modalBody.appendChild(el("div", { class: "row" }, [submitBtn]));
    openModal(modalBody);
  }

  function openDeleteVideoModal(channelUsername, video, onDone) {
    const body = el("div", { class: "card profile-modal" });
    body.appendChild(el("div", { class: "row" }, [
      el("h2", { text: "Delete \"" + video.name + "\"?" }),
      el("button", { class: "modal-close", text: "✕", onclick: closeModal, style: "margin-left:auto" }),
    ]));
    body.appendChild(el("div", { class: "sub", text: "This retracts the video from federation. The underlying Matrix video message is left untouched, so you can publish it again afterward." }));
    body.appendChild(el("div", { class: "row" }, [
      dangerButton("Delete", (e) => {
        e.target.disabled = true;
        api("/widget/api/channels/" + encodeURIComponent(channelUsername) + "/videos/" + encodeURIComponent(video.id) + "/unpublish", { method: "POST", body: {} })
          .then((data) => { toast((data && data.message) || "Submitted.", true); })
          .catch((err) => { toast(err.message || "Could not delete that video.", false); })
          .finally(() => { closeModal(); if (onDone) onDone(); });
      }),
      plainButton("Cancel", closeModal),
    ]));
    openModal(body);
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

  // Replaces window.confirm() for a destructive action's click handler --
  // confirmed live 2026-10-24 that a native confirm() dialog produces NO
  // visible prompt and NO subsequent request at all when the widget runs
  // inside Haven Desktop's iframe (sandboxed without allow-modals, the
  // standard reason an embedded widget can't use blocking browser dialogs
  // -- every OTHER confirm()-gated button in this file had the exact same
  // silent-no-op bug, not just the one reported). Clicking the trigger
  // button disables it and inserts an inline "message [Yes] [Cancel]" row
  // right after it instead of blocking on a native dialog; "Yes" runs
  // `run(btn)` (expected to handle its own busy-state via doAction/
  // profileAction, which already disable+relabel btn while in flight).
  function withConfirm(message, run) {
    return (e) => {
      const btn = e.target;
      if (btn.dataset.confirmPending === "1") return;
      btn.dataset.confirmPending = "1";
      btn.disabled = true;
      const row = el("div", { class: "confirm-row" }, [
        el("span", { class: "muted", text: message }),
        dangerButton("Yes", () => {
          row.remove();
          Promise.resolve(run(btn)).finally(() => { delete btn.dataset.confirmPending; });
        }),
        plainButton("Cancel", () => {
          row.remove();
          btn.disabled = false;
          delete btn.dataset.confirmPending;
        }),
      ]);
      btn.insertAdjacentElement("afterend", row);
    };
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
          plainButton("Replace this room", withConfirm(
            "Create a fresh replacement room and retire this one?",
            (btn) => doAction("/widget/api/replace_room", {}, btn)
          )),
          dangerButton("Unlink profile", withConfirm(
            "Detach this room from your identity? You can relink later.",
            (btn) => doAction("/widget/api/unlink_profile", {}, btn)
          )),
          dangerButton("Delete profile", withConfirm(
            "Permanently delete your fediverse identity and notify followers? This cannot be undone.",
            (btn) => doAction("/widget/api/delete_profile", {}, btn)
          )),
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
        backfillRow.appendChild(plainButton("Replace this room", withConfirm(
          "Create a fresh replacement room for this account and retire this one?",
          (btn) => doAction("/widget/api/replace_room", {}, btn)
        )));
      }
      card.appendChild(backfillRow);
      app.appendChild(card);
    } else if (kind === "dm" || kind === "chat") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (kind === "dm" ? "Direct message" : "Chat") }),
        el("div", { class: "row" }, [
          plainButton("Mute", (e) => doAction("/widget/api/mute", {}, e.target)),
          dangerButton("Block", withConfirm(
            "Block this account?",
            (btn) => doAction("/widget/api/block", {}, btn)
          )),
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

  // Bumped on every start() call and on Cancel -- a stale attempt (the
  // get_openid call that already timed out, or one still in flight when
  // the user cancels) checks this before touching the DOM, so a late
  // response from an abandoned attempt can never clobber a newer one.
  let startGeneration = 0;

  function showConnectErrorState(message) {
    const app = document.getElementById("app");
    app.innerHTML = "";
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Couldn't connect" }),
      el("div", { class: "sub", text: message }),
      el("div", { class: "row" }, [primaryButton("Retry", () => start())]),
    ]));
  }

  function showConnectingState() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    const cancelBtn = plainButton("Cancel", () => {
      startGeneration++;
      showConnectErrorState("Cancelled.");
    });
    cancelBtn.style.display = "none";
    app.appendChild(el("div", { class: "spinner" }, [
      el("div", { text: "Connecting…" }),
      el("div", { style: "margin-top:12px" }, [cancelBtn]),
    ]));
    // Not shown immediately -- a normal connection is near-instant, and
    // a button that flashes for a fraction of a second on every load is
    // just noise. Still well before anyone would notice something's
    // actually wrong.
    setTimeout(() => { cancelBtn.style.display = ""; }, 3000);
  }

  async function start() {
    const myGeneration = ++startGeneration;
    showConnectingState();
    try {
      const creds = await getOpenIdToken();
      if (myGeneration !== startGeneration) return; // cancelled, or superseded by a newer attempt
      const session = await api("/widget/api/session", {
        method: "POST",
        body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
      });
      if (myGeneration !== startGeneration) return;
      sessionId = session.session_id;
      await loadContext();
    } catch (e) {
      if (myGeneration !== startGeneration) return;
      showConnectErrorState(e.message);
    }
  }

  // Shows once scrolled down a little, on any view -- Element/Haven-style
  // round "^" button, independent of whichever tab/profile/thread is
  // currently showing.
  (function initScrollTopButton() {
    const btn = document.getElementById("scroll-top-btn");
    if (!btn) return;
    btn.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    window.addEventListener("scroll", () => {
      btn.classList.toggle("visible", window.scrollY > 300);
    });
  })();

  start();
})();
</script>
</body>
</html>
"""
