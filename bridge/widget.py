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
    _bot_mxid,
    _COMMAND_PREFIX,
    _handle_backfill,
    _handle_banner,
    _handle_block,
    _handle_chat,
    _handle_create_profile,
    _handle_delete_profile,
    _handle_dm,
    _handle_follow,
    _handle_import,
    _handle_link_profile,
    _handle_mute,
    _handle_replace_room,
    _handle_set_collection_visibility,
    _handle_unblock,
    _handle_unfollow,
    _handle_unlink_profile,
    _handle_unmute,
    _is_matrix_admin,
    _parse_follows_export,
    _run_follows_import,
    _RUNNING_FOLLOWS_IMPORTS,
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
        room = {
            "kind": "profile",
            "is_owner": is_owner,
            "owner_matrix_user_id": profile_owner,
            "username": owner_record.username if owner_record else None,
            "display_name": owner_record.display_name if owner_record else None,
            "hide_followers": owner_record.hide_followers if owner_record else False,
            "hide_following": owner_record.hide_following if owner_record else False,
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
    body = await request.json()
    return await _dispatch(request, body["room_id"], _handle_create_profile)


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
    body = await request.json()
    which = body.get("which")
    if which not in ("followers", "following"):
        raise HTTPException(400, "which must be 'followers' or 'following'")
    return await _dispatch(
        request, body["room_id"], _handle_set_collection_visibility,
        hidden=bool(body.get("hidden")), argument=which,
    )


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
    --bg: #f5f6f8;
    --card: #ffffff;
    --border: #e2e4e9;
    --text: #1c1e21;
    --muted: #6b7280;
    --accent: #6d28d9;
    --accent-text: #ffffff;
    --danger: #dc2626;
    --danger-bg: #fef2f2;
    --ok-bg: #f0fdf4;
    --ok-text: #15803d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181c; --card: #1f2025; --border: #2e3038; --text: #e7e8ea;
      --muted: #9aa0ab; --accent: #a78bfa; --accent-text: #17181c;
      --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
    }
  }
  :root[data-theme="dark"] {
    --bg: #17181c; --card: #1f2025; --border: #2e3038; --text: #e7e8ea;
    --muted: #9aa0ab; --accent: #a78bfa; --accent-text: #17181c;
    --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
  }
  :root[data-theme="light"] {
    --bg: #f5f6f8; --card: #ffffff; --border: #e2e4e9; --text: #1c1e21;
    --muted: #6b7280; --accent: #6d28d9; --accent-text: #ffffff;
    --danger: #dc2626; --danger-bg: #fef2f2; --ok-bg: #f0fdf4; --ok-text: #15803d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh;
  }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; margin-bottom: 12px;
  }
  h1 { font-size: 15px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; word-break: break-all; }
  .badge {
    display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: var(--accent); color: var(--accent-text); font-weight: 600;
  }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .row:last-child { margin-bottom: 0; }
  button {
    font: inherit; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: 8px; padding: 8px 14px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  button.danger { color: var(--danger); border-color: var(--danger); }
  button:disabled { opacity: .5; cursor: default; }
  button:not(:disabled):active { transform: translateY(1px); }
  input[type=text], input[type=number] {
    font: inherit; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: 8px; padding: 8px 10px; flex: 1; min-width: 120px;
  }
  .toast {
    position: fixed; left: 16px; right: 16px; bottom: 16px; padding: 10px 14px; border-radius: 10px;
    font-size: 13px; font-weight: 600; display: none; z-index: 10;
  }
  .toast.ok { background: var(--ok-bg); color: var(--ok-text); display: block; }
  .toast.err { background: var(--danger-bg); color: var(--danger); display: block; }
  .kv { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; }
  .kv span:first-child { color: var(--muted); }
  .list { display: flex; flex-direction: column; gap: 6px; }
  .list-item {
    display: flex; justify-content: space-between; align-items: center; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: 8px; font-size: 13px;
  }
  .muted { color: var(--muted); }
  .spinner { text-align: center; color: var(--muted); padding: 40px 0; }
  a { color: var(--accent); }
  .toggle-on { background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-bg); }
</style>
</head>
<body>
  <div id="app"><div class="spinner">Connecting&hellip;</div></div>
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

  function render() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    if (!ctx) return;

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
