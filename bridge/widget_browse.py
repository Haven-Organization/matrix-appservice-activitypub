"""The widget's "Browse" surface: looking up a fediverse account, and
browsing a remote instance's own local/federated timeline for posts not
yet imported into Matrix.

Nothing here is part of core ActivityPub -- a site-wide timeline and a
search endpoint are both Mastodon client-API conventions (adopted
compatibly by Pleroma/Akkoma, GoToSocial, and a few other Mastodon forks),
not an AS2 concept. A bare ActivityPub server -- including another install
of this very bridge -- exposes neither, only a per-actor ``outbox``
(already covered by ``;backfill``). Every call here is therefore gated on
``bridge.activitypub.nodeinfo.remote_software_name`` reporting a software
name this module knows is Mastodon-API-compatible, and every failure
(wrong software, unreachable, the instance admin disabled public preview)
degrades to an empty/"unsupported" result, never a hard error -- the same
best-effort philosophy ``bridge.commands._discover_mastodon_api_reply_notes``
already established for using a remote instance's REST API purely as a
discovery layer.

Misskey and its forks (Calckey/Firefish/Sharkey/Iceshrimp) are
deliberately NOT supported here -- they expose a conceptually similar but
totally different, POST-based JSON-RPC-ish API, not this one. Lemmy and
PeerTube likewise have their own separate APIs this module doesn't speak.

A found/browsed post is matched against our own ``federated_events`` table
by its ``uri`` (the same field this bridge's own ``;import`` dedups
against -- see ``bridge.commands._handle_import``) so the widget can badge
a post as already-imported instead of offering to import it again. Posting
it into Matrix for real is a plain call to the EXISTING
``/widget/api/import`` action (``bridge.widget.widget_import``, itself a
thin wrapper around ``_handle_import``) -- nothing new to import with, only
new ways to discover a URL worth importing.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bridge import instance_auth
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.remote_actor import RemoteActorFetchError, fetch_actor
from bridge.activitypub.webfinger import WebfingerError, resolve_remote_actor_id
from bridge.instance_auth import InstanceAuthError
from bridge.widget import _session_user

logger = logging.getLogger(__name__)

router = APIRouter()

# Mastodon client-API-compatible software families (NodeInfo software.name,
# already lowercased by remote_software_name) -- confirmed live against a
# real instance of each: mastodon (fosstodon.org), pleroma (poa.st reports
# "pleroma" even though it may run the Akkoma fork -- same API either way),
# gotosocial and the listed Mastodon forks follow the same client API by
# design. NOT Misskey-family -- different API entirely, see module
# docstring.
_COMPATIBLE_SOFTWARE = {
    "mastodon", "pleroma", "akkoma", "hometown", "glitch-soc", "gotosocial", "sharkey-bridge",
}

# Matches the Mastodon API's own default page size for timelines/search.
_PAGE_SIZE = 20

_EMOJI_SHORTCODE_RE = re.compile(r":([a-zA-Z0-9_+-]+):")


def _substitute_emoji_html(body_html: str, emoji_map: dict[str, str]) -> str:
    """Replace every ``:shortcode:`` run in ``body_html`` that matches a key
    in ``emoji_map`` (shortcode -> image URL) with an inline ``<img>``,
    leaving an unrecognized ``:shortcode:`` (or plain text with none at
    all) untouched. Used for both Mastodon-API ``emojis`` lists and AS2
    ``tag`` arrays once normalized into the same shortcode->URL shape --
    see ``_mastodon_emoji_map``/``_as2_emoji_map``. Callers pass already-
    escaped/already-HTML text; this never escapes anything itself."""
    if not emoji_map:
        return body_html

    def _replace(match: re.Match[str]) -> str:
        code = match.group(1)
        url = emoji_map.get(code)
        if not url:
            return match.group(0)
        safe_url = html.escape(url, quote=True)
        return f'<img class="emoji" src="{safe_url}" alt=":{code}:" title=":{code}:">'

    return _EMOJI_SHORTCODE_RE.sub(_replace, body_html)


def _mastodon_emoji_map(emojis: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for e in emojis or []:
        if not isinstance(e, dict):
            continue
        code = e.get("shortcode")
        url = e.get("url") or e.get("static_url")
        if isinstance(code, str) and isinstance(url, str):
            result[code] = url
    return result


def _as2_emoji_map(doc: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for t in doc.get("tag") or []:
        if not isinstance(t, dict) or t.get("type") != "Emoji":
            continue
        name = t.get("name")
        icon = t.get("icon")
        url = icon.get("url") if isinstance(icon, dict) else None
        if isinstance(name, str) and isinstance(url, str):
            result[name.strip(":")] = url
    return result


def _as2_collection_count(value: Any) -> int:
    """``totalItems`` off an embedded AS2 collection (``likes``/``shares``/
    ``replies``) -- Mastodon inlines these with the count already present,
    no separate fetch needed; anything else (a bare URL string, missing,
    malformed) just counts as 0 rather than triggering an extra request."""
    if isinstance(value, dict):
        total = value.get("totalItems")
        if isinstance(total, int):
            return total
    return 0


def _display_name_html(raw_name: str, emoji_map: dict[str, str]) -> str:
    return _substitute_emoji_html(html.escape(raw_name), emoji_map)


async def _is_compatible(request: Request, domain: str) -> bool:
    name = await remote_software_name(request, domain)
    return name in _COMPATIBLE_SOFTWARE


def _normalize_status(status: dict[str, Any], *, instance_domain: str) -> dict[str, Any] | None:
    """A Mastodon-API status -> the shape the widget frontend renders.
    ``None`` if it's too malformed to show at all (missing the basics)."""
    reblog = status.get("reblog")
    base = reblog if isinstance(reblog, dict) else status
    if not isinstance(base, dict):
        return None
    account = base.get("account") if isinstance(base.get("account"), dict) else {}
    uri = base.get("uri")
    if not isinstance(uri, str) or not uri:
        return None
    media = [
        {
            "type": m.get("type"),
            "url": m.get("preview_url") or m.get("url"),
            "description": m.get("description") if isinstance(m.get("description"), str) else "",
        }
        for m in (base.get("media_attachments") or [])
        if isinstance(m, dict) and isinstance(m.get("url"), str)
    ]
    booster = status.get("account") if (reblog is not None and isinstance(status.get("account"), dict)) else None
    content_html = base.get("content") if isinstance(base.get("content"), str) else ""
    content_html = _substitute_emoji_html(content_html, _mastodon_emoji_map(base.get("emojis")))
    author_name = account.get("display_name") or account.get("username") or "(unknown)"
    booster_name = (booster.get("display_name") or booster.get("username") or "") if booster is not None else ""

    # A reply's author isn't in the status object itself (Mastodon only
    # gives `in_reply_to_id`, a numeric id meaningless off that instance) --
    # `mentions[0]` is the standard client-side heuristic for "who this is
    # replying to" (a reply's own content always mentions its parent's
    # author first), avoiding an extra fetch per reply in a feed. The
    # thread view (browse_thread) resolves the REAL parent via a fresh AS2
    # fetch regardless, so this is only ever a display hint, never load-
    # bearing for navigation -- opening a thread always uses this post's
    # own `uri`, not this hint.
    is_reply = bool(base.get("in_reply_to_id"))
    mentions = base.get("mentions") if isinstance(base.get("mentions"), list) else []
    first_mention = mentions[0] if mentions and isinstance(mentions[0], dict) else None
    reply_hint_handle = ("@" + first_mention["acct"]) if first_mention and isinstance(first_mention.get("acct"), str) else None

    pleroma = base.get("pleroma") if isinstance(base.get("pleroma"), dict) else {}
    reactions = [
        {"name": r.get("name") or "", "count": r.get("count") or 0, "url": r.get("url") if isinstance(r.get("url"), str) else None}
        for r in (pleroma.get("emoji_reactions") or [])
        if isinstance(r, dict)
    ]

    return {
        "uri": uri,
        "url": base.get("url") or uri,
        "created_at": base.get("created_at"),
        "content_html": content_html,
        "sensitive": bool(base.get("sensitive")),
        "spoiler_text": base.get("spoiler_text") if isinstance(base.get("spoiler_text"), str) else "",
        "author": {
            "actor_id": account.get("uri"),
            "display_name": author_name,
            "display_name_html": _display_name_html(author_name, _mastodon_emoji_map(account.get("emojis"))),
            "handle": account.get("acct") or account.get("username") or "",
            "avatar_url": account.get("avatar") or account.get("avatar_static"),
            "url": account.get("url"),
        },
        "media": media,
        "counts": {
            "replies": base.get("replies_count") or 0,
            "reblogs": base.get("reblogs_count") or 0,
            "favourites": base.get("favourites_count") or 0,
        },
        "reactions": reactions,
        "in_reply_to": is_reply,
        "reply_hint_display_name": None,
        "reply_hint_handle": reply_hint_handle,
        "boosted_by": (
            {
                "actor_id": booster.get("uri"),
                "display_name": booster_name,
                "display_name_html": _display_name_html(booster_name, _mastodon_emoji_map(booster.get("emojis"))),
                "handle": booster.get("acct") or "",
            }
            if booster is not None else None
        ),
        "instance": instance_domain,
    }


async def _auth_headers(request: Request, *, matrix_user_id: str, domain: str) -> dict[str, str]:
    token = await instance_auth.get_bearer_token(request, matrix_user_id=matrix_user_id, domain=domain)
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _fetch_timeline(
    request: Request, *, domain: str, local: bool, max_id: str | None, matrix_user_id: str
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """One page of ``domain``'s public timeline, normalized, plus the
    ``max_id`` to pass for the next page (``None`` once there's nothing
    more -- a short page is the only signal the Mastodon API gives for
    that, there's no total count), plus whether this instance needs a
    sign-in to view this at all (``401``/``403``/``422`` -- the last is
    what several real Pleroma/Akkoma instances actually return for
    "authorization required for timeline view", confirmed live). Attaches
    this bridge user's own stored login for ``domain``, if any, so a
    previously-signed-in user sees the same timeline a logged-out
    unauthenticated call would be refused."""
    http_client = request.app.state.http_client
    params: dict[str, Any] = {"local": "true" if local else "false", "limit": _PAGE_SIZE}
    if max_id:
        params["max_id"] = max_id
    headers = await _auth_headers(request, matrix_user_id=matrix_user_id, domain=domain)
    try:
        response = await http_client.get(f"https://{domain}/api/v1/timelines/public", params=params, headers=headers)
    except httpx.HTTPError:
        return [], None, False
    if response.status_code in (401, 403, 422):
        return [], None, True
    if response.status_code != 200:
        return [], None, False
    try:
        raw = response.json()
    except ValueError:
        return [], None, False
    if not isinstance(raw, list):
        return [], None, False
    posts = [p for p in (_normalize_status(s, instance_domain=domain) for s in raw if isinstance(s, dict)) if p is not None]
    next_max_id = raw[-1].get("id") if raw and isinstance(raw[-1], dict) else None
    return posts, (str(next_max_id) if len(raw) >= _PAGE_SIZE and next_max_id is not None else None), False


async def _mark_imported(request: Request, posts: list[dict[str, Any]]) -> None:
    repository = request.app.state.repository
    for post in posts:
        existing = await repository.get_federated_event_by_ap_object(post["uri"])
        post["already_imported"] = existing is not None
        post["matrix_event_id"] = existing.event_id if existing is not None else None
        post["matrix_room_id"] = existing.room_id if existing is not None else None


@router.get("/widget/api/browse/software")
async def browse_software(request: Request, domain: str) -> JSONResponse:
    _session_user(request)
    name = await remote_software_name(request, domain)
    return JSONResponse(
        {"domain": domain, "software": name, "supported": name in _COMPATIBLE_SOFTWARE},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/timeline")
async def browse_timeline(request: Request, domain: str, scope: str = "local", max_id: str | None = None) -> JSONResponse:
    matrix_user_id = _session_user(request)
    if scope not in ("local", "federated"):
        raise HTTPException(400, "scope must be 'local' or 'federated'")
    if not await _is_compatible(request, domain):
        return JSONResponse(
            {"supported": False, "needs_login": False, "posts": [], "next_max_id": None},
            headers={"Cache-Control": "no-store"},
        )
    posts, next_max_id, needs_login = await _fetch_timeline(
        request, domain=domain, local=(scope == "local"), max_id=max_id, matrix_user_id=matrix_user_id
    )
    await _mark_imported(request, posts)
    return JSONResponse(
        {"supported": True, "needs_login": needs_login, "posts": posts, "next_max_id": next_max_id},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/home")
async def browse_home(request: Request, max_id_map: str = "") -> JSONResponse:
    """The widget's default landing feed: every configured
    ``bridge.default_timeline_domains`` instance's OWN federated ("whole
    known network") timeline, merged by recency. Falls back to just this
    bridge's own domain if none are configured -- which, since this
    bridge's own domain never runs Mastodon-API-compatible software,
    correctly comes back empty rather than guessing at some other public
    instance to advertise by default.

    ``max_id_map`` is a JSON object of ``{domain: max_id}`` from the
    previous page, one cursor per source instance -- each source instance
    paginates independently, so "load more" asks each one to continue from
    wherever IT left off and merges the results again. This doesn't give
    perfectly gapless cross-instance ordering (no API gives us that), but
    keeps each instance's own order correct and never repeats or loses a
    whole page from any one source.
    """
    matrix_user_id = _session_user(request)
    config = request.app.state.config
    domains = config.bridge.default_timeline_domains or [config.bridge.domain]
    try:
        cursor_map: dict[str, Any] = json.loads(max_id_map) if max_id_map else {}
    except ValueError:
        cursor_map = {}

    async def _one(domain: str) -> tuple[str, list[dict[str, Any]], str | None, bool, bool]:
        if not await _is_compatible(request, domain):
            return domain, [], None, False, False
        posts, next_max_id, needs_login = await _fetch_timeline(
            request, domain=domain, local=False, max_id=cursor_map.get(domain), matrix_user_id=matrix_user_id
        )
        return domain, posts, next_max_id, True, needs_login

    results = await asyncio.gather(*(_one(d) for d in domains))
    merged: list[dict[str, Any]] = []
    next_cursor_map: dict[str, str] = {}
    any_supported = False
    needs_login_domains: list[str] = []
    for domain, posts, next_max_id, supported, needs_login in results:
        any_supported = any_supported or supported
        merged.extend(posts)
        if next_max_id:
            next_cursor_map[domain] = next_max_id
        if needs_login:
            needs_login_domains.append(domain)
    merged.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    await _mark_imported(request, merged)
    return JSONResponse(
        {
            "supported": any_supported,
            "domains": domains,
            "needs_login_domains": needs_login_domains,
            "posts": merged,
            "next_max_id_map": json.dumps(next_cursor_map) if next_cursor_map else "",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/search")
async def browse_search(request: Request, q: str, domain: str = "") -> JSONResponse:
    """Look up a fediverse account. An exact ``@user@host`` (or
    ``user@host``) is resolved via WebFinger, which works against any
    software at all. Anything else is treated as a fuzzy name to search
    for on ``domain`` (only if it's Mastodon-API-compatible) -- there is no
    such thing as "search the whole fediverse"; every real search API is
    scoped to one instance's own index."""
    matrix_user_id = _session_user(request)
    q = q.strip()
    if not q:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})

    handle = q[1:] if q.startswith("@") else q
    if "@" in handle and " " not in handle:
        http_client = request.app.state.http_client
        try:
            actor_id = await resolve_remote_actor_id(http_client, handle)
            doc = await fetch_actor(request, actor_id)
        except (WebfingerError, RemoteActorFetchError):
            return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
        name = doc.get("name") if isinstance(doc.get("name"), str) else None
        icon = doc.get("icon")
        avatar_url = icon.get("url") if isinstance(icon, dict) else None
        display_name = name or doc.get("preferredUsername") or handle
        return JSONResponse(
            {
                "accounts": [
                    {
                        "actor_id": actor_id,
                        "handle": "@" + handle.lstrip("@"),
                        "display_name": display_name,
                        "display_name_html": _display_name_html(display_name, _as2_emoji_map(doc)),
                        "avatar_url": avatar_url,
                    }
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    if not domain or not await _is_compatible(request, domain):
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    http_client = request.app.state.http_client
    headers = await _auth_headers(request, matrix_user_id=matrix_user_id, domain=domain)
    try:
        response = await http_client.get(
            f"https://{domain}/api/v2/search",
            params={"q": q, "type": "accounts", "limit": 8, "resolve": "false"},
            headers=headers,
        )
    except httpx.HTTPError:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    if response.status_code != 200:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    try:
        raw = response.json()
    except ValueError:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    accounts = []
    for a in raw.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        acct = a.get("acct") or a.get("username") or ""
        display_name = a.get("display_name") or a.get("username") or acct
        accounts.append(
            {
                "actor_id": a.get("uri") or a.get("url"),
                "handle": "@" + acct if "@" in acct else f"@{acct}@{domain}",
                "display_name": display_name,
                "display_name_html": _display_name_html(display_name, _mastodon_emoji_map(a.get("emojis"))),
                "avatar_url": a.get("avatar") or a.get("avatar_static"),
            }
        )
    return JSONResponse({"accounts": accounts}, headers={"Cache-Control": "no-store"})


# -- full profile view (avatar/banner/bio + their posts) -------------------
#
# Deliberately built on the raw AS2 actor document + outbox (fetch_actor,
# already signed + works against any software), not the Mastodon API --
# unlike timelines/search, "fetch one actor and page their own outbox" IS
# a real ActivityPub concept every server exposes, so this works for a
# browsed account on ANY software, not just the Mastodon-API-compatible
# subset the rest of this module is scoped to.


def _actor_handle(doc: dict[str, Any], actor_id: str) -> str:
    username = doc.get("preferredUsername") if isinstance(doc.get("preferredUsername"), str) else None
    domain = urlparse(actor_id).hostname or ""
    return f"@{username or actor_id}@{domain}" if domain else f"@{username or actor_id}"


def _normalize_ap_item(item: dict[str, Any], *, author: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A bare AS2 Note (or an Activity wrapping one, e.g. an outbox page's
    ``Create``) -> the widget's post shape. ``author`` is optional -- a
    profile's own outbox page has the frontend inject one shared author
    for every post (it's always the same account); the thread endpoint
    (``browse_thread``) instead resolves and passes a real per-post author
    here, since ancestors/replies can each be by someone different."""
    obj = item.get("object") if isinstance(item.get("object"), dict) else item
    if not isinstance(obj, dict):
        return None
    uri = obj.get("id")
    if not isinstance(uri, str) or not uri:
        return None
    media = []
    for a in obj.get("attachment") or []:
        if not isinstance(a, dict) or not isinstance(a.get("url"), str):
            continue
        mime = a.get("mediaType") if isinstance(a.get("mediaType"), str) else ""
        kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "unknown"
        media.append({"type": kind, "url": a["url"], "description": a.get("name") if isinstance(a.get("name"), str) else ""})
    summary = obj.get("summary") if isinstance(obj.get("summary"), str) else ""
    emoji_map = _as2_emoji_map(obj)
    content_html = obj.get("content") if isinstance(obj.get("content"), str) else ""
    content_html = _substitute_emoji_html(content_html, emoji_map)
    in_reply_to_uri = obj.get("inReplyTo") if isinstance(obj.get("inReplyTo"), str) else None
    return {
        "uri": uri,
        "url": obj.get("url") or uri,
        "created_at": obj.get("published"),
        "content_html": content_html,
        "sensitive": bool(obj.get("sensitive")) or bool(summary),
        "spoiler_text": summary,
        "author": author,
        "media": media,
        "counts": {
            "replies": _as2_collection_count(obj.get("replies")),
            "reblogs": _as2_collection_count(obj.get("shares")),
            "favourites": _as2_collection_count(obj.get("likes")),
        },
        # AS2 has no standard custom-reaction concept of its own (Pleroma's
        # emoji_reactions are exposed only via its Mastodon-compatible
        # status API, not on the bare Note -- see _normalize_status for
        # where those actually come from).
        "reactions": [],
        "in_reply_to": in_reply_to_uri is not None,
        "in_reply_to_uri": in_reply_to_uri,
        # No cheap per-item author hint here (unlike the Mastodon-API
        # mentions[0] heuristic) -- resolving the real parent's author
        # would mean an extra fetch per reply just to render a feed. Left
        # None; the thread view resolves and shows the real name once you
        # actually open it.
        "reply_hint_display_name": None,
        "reply_hint_handle": None,
        "boosted_by": None,
        "instance": urlparse(uri).hostname or "",
    }


async def _fetch_outbox_page(
    request: Request, *, actor_id: str | None, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        if cursor:
            page = await fetch_actor(request, cursor)
        else:
            actor_doc = await fetch_actor(request, actor_id)  # type: ignore[arg-type]
            outbox = actor_doc.get("outbox")
            if not isinstance(outbox, str):
                return [], None
            collection = await fetch_actor(request, outbox)
            if isinstance(collection.get("orderedItems"), list):
                page = collection
            elif isinstance(collection.get("first"), str):
                page = await fetch_actor(request, collection["first"])
            elif isinstance(collection.get("first"), dict):
                page = collection["first"]
            else:
                return [], None
    except RemoteActorFetchError:
        return [], None
    items = page.get("orderedItems") if isinstance(page.get("orderedItems"), list) else page.get("items")
    if not isinstance(items, list):
        return [], None
    posts = [p for p in (_normalize_ap_item(i) for i in items if isinstance(i, dict)) if p is not None]
    next_cursor = page.get("next") if isinstance(page.get("next"), str) else None
    return posts, next_cursor


# -- thread view (ancestors up to the root, plus immediate replies) --------
#
# Deliberately re-resolves everything via a fresh, generic AS2 fetch of the
# focused post's own `uri` -- never the Mastodon-API `in_reply_to_id` (only
# meaningful on the instance that issued it) and never whatever the
# frontend happened to already have cached. This is what makes thread
# opening work identically for a post discovered via a Mastodon-API
# timeline/search AND one discovered via a profile's own AS2 outbox page.

_THREAD_MAX_ANCESTORS = 15
_THREAD_MAX_REPLIES = 15


def _unwrap_activity(doc: dict[str, Any]) -> dict[str, Any]:
    """A fetched AS2 URL sometimes resolves to the bare object (the common
    case for an object's own canonical URL) and sometimes to an Activity
    wrapping it (a `Create`, if a server instead serves the activity at
    that URL) -- normalize to the bare object either way."""
    inner = doc.get("object")
    if doc.get("type") == "Create" and isinstance(inner, dict):
        return inner
    return doc


async def _resolve_as2_author(request: Request, attributed_to: Any) -> dict[str, Any] | None:
    if not isinstance(attributed_to, str):
        return None
    try:
        actor_doc = await fetch_actor(request, attributed_to)
    except RemoteActorFetchError:
        return {
            "actor_id": attributed_to, "display_name": "(unknown)", "display_name_html": "(unknown)",
            "handle": "", "avatar_url": None, "url": attributed_to,
        }
    icon = actor_doc.get("icon") if isinstance(actor_doc.get("icon"), dict) else {}
    name = actor_doc.get("name") if isinstance(actor_doc.get("name"), str) and actor_doc.get("name") else (actor_doc.get("preferredUsername") or attributed_to)
    return {
        "actor_id": attributed_to,
        "display_name": name,
        "display_name_html": _display_name_html(name, _as2_emoji_map(actor_doc)),
        "handle": _actor_handle(actor_doc, attributed_to),
        "avatar_url": icon.get("url"),
        "url": actor_doc.get("url") if isinstance(actor_doc.get("url"), str) else attributed_to,
    }


async def _normalize_as2_note_full(request: Request, note_doc: dict[str, Any]) -> dict[str, Any] | None:
    author = await _resolve_as2_author(request, note_doc.get("attributedTo"))
    return _normalize_ap_item(note_doc, author=author)


async def _fetch_as2_note(request: Request, uri: str) -> dict[str, Any] | None:
    try:
        doc = await fetch_actor(request, uri)
    except RemoteActorFetchError:
        return None
    return _unwrap_activity(doc)


async def _fetch_reply_uris(request: Request, note_doc: dict[str, Any]) -> list[str]:
    replies = note_doc.get("replies")
    if not isinstance(replies, dict):
        return []
    first = replies.get("first")
    page: dict[str, Any] | None = None
    if isinstance(first, dict):
        page = first
    elif isinstance(first, str):
        page = await _fetch_as2_note(request, first)
    if not isinstance(page, dict):
        return []
    items = page.get("items") if isinstance(page.get("items"), list) else page.get("orderedItems")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, str)][:_THREAD_MAX_REPLIES]


@router.get("/widget/api/browse/thread")
async def browse_thread(request: Request, uri: str) -> JSONResponse:
    _session_user(request)
    focus_doc = await _fetch_as2_note(request, uri)
    if focus_doc is None:
        raise HTTPException(502, "Couldn't load that post.")

    ancestors: list[dict[str, Any]] = []
    seen = {uri}
    current = focus_doc
    for _ in range(_THREAD_MAX_ANCESTORS):
        parent_uri = current.get("inReplyTo")
        if not isinstance(parent_uri, str) or parent_uri in seen:
            break
        seen.add(parent_uri)
        parent_doc = await _fetch_as2_note(request, parent_uri)
        if parent_doc is None:
            break
        ancestors.append(parent_doc)
        current = parent_doc
    ancestors.reverse()  # root first, immediate parent last

    reply_uris = await _fetch_reply_uris(request, focus_doc)
    reply_docs = [d for d in await asyncio.gather(*(_fetch_as2_note(request, u) for u in reply_uris)) if d is not None]

    normalized_ancestors = [p for p in await asyncio.gather(*(_normalize_as2_note_full(request, d) for d in ancestors)) if p is not None]
    normalized_focus = await _normalize_as2_note_full(request, focus_doc)
    normalized_replies = [p for p in await asyncio.gather(*(_normalize_as2_note_full(request, d) for d in reply_docs)) if p is not None]

    await _mark_imported(request, normalized_ancestors)
    if normalized_focus is not None:
        await _mark_imported(request, [normalized_focus])
    await _mark_imported(request, normalized_replies)

    if normalized_focus is None:
        raise HTTPException(502, "That post is missing something required to show it.")

    return JSONResponse(
        {"ancestors": normalized_ancestors, "post": normalized_focus, "replies": normalized_replies},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/profile")
async def browse_profile(request: Request, actor_id: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    try:
        doc = await fetch_actor(request, actor_id)
    except RemoteActorFetchError:
        raise HTTPException(502, "Couldn't reach that account's instance right now.")
    icon = doc.get("icon") if isinstance(doc.get("icon"), dict) else {}
    image = doc.get("image") if isinstance(doc.get("image"), dict) else {}
    emoji_map = _as2_emoji_map(doc)
    display_name = doc.get("name") if isinstance(doc.get("name"), str) and doc.get("name") else (doc.get("preferredUsername") or actor_id)
    bio_html = doc.get("summary") if isinstance(doc.get("summary"), str) else ""
    profile: dict[str, Any] = {
        "actor_id": actor_id,
        "display_name": display_name,
        "display_name_html": _display_name_html(display_name, emoji_map),
        "handle": _actor_handle(doc, actor_id),
        "avatar_url": icon.get("url"),
        "banner_url": image.get("url"),
        "bio_html": _substitute_emoji_html(bio_html, emoji_map),
        "url": doc.get("url") if isinstance(doc.get("url"), str) else actor_id,
        "has_posts": isinstance(doc.get("outbox"), str),
    }
    active = await repository.get_active_local_actor(matrix_user_id)
    if active is not None:
        profile["is_following"] = await repository.is_following(active.username, actor_id)
        profile["is_blocked"] = await repository.is_blocked(active.username, actor_id)
        profile["is_muted"] = await repository.is_muted(active.username, actor_id)
    else:
        profile["is_following"] = profile["is_blocked"] = profile["is_muted"] = False
    return JSONResponse(profile, headers={"Cache-Control": "no-store"})


@router.get("/widget/api/browse/profile/timeline")
async def browse_profile_timeline(request: Request, actor_id: str, cursor: str = "") -> JSONResponse:
    _session_user(request)
    posts, next_cursor = await _fetch_outbox_page(request, actor_id=actor_id, cursor=cursor or None)
    await _mark_imported(request, posts)
    return JSONResponse({"posts": posts, "next_cursor": next_cursor or ""}, headers={"Cache-Control": "no-store"})


# -- instance sign-in (OAuth + password-grant fallback) --------------------


@router.post("/widget/api/browse/login/start")
async def browse_login_start(request: Request) -> JSONResponse:
    """Begin an OAuth sign-in to ``body['domain']``. Returns a URL the
    widget opens in a new tab (its own iframe can't navigate top-level to
    a cross-origin page); the opened tab posts a message back and closes
    itself once ``browse_login_callback`` below completes."""
    matrix_user_id = _session_user(request)
    body = await request.json()
    domain = (body.get("domain") or "").strip().lower()
    if not domain:
        raise HTTPException(400, "domain is required")
    try:
        url = await instance_auth.start_oauth(request, matrix_user_id=matrix_user_id, domain=domain)
    except InstanceAuthError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"authorize_url": url}, headers={"Cache-Control": "no-store"})


@router.get("/widget/api/browse/login/callback")
async def browse_login_callback(request: Request, code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """The instance redirects the user's real browser tab here after they
    approve (or deny) the OAuth prompt -- this is a bare top-level page
    load, not a widget-authenticated API call, so it has no session of
    its own; ``state`` (minted by ``browse_login_start``) is what
    reconnects it to the bridge user who started the flow."""
    if error:
        message, ok = f"Sign-in was cancelled or denied ({error}).", False
    elif not code or not state:
        message, ok = "Missing sign-in response from the instance.", False
    else:
        try:
            _matrix_user_id, domain = await instance_auth.complete_oauth(request, state=state, code=code)
            message, ok = f"Signed in to {domain}. You can close this tab.", True
        except InstanceAuthError as exc:
            message, ok = str(exc), False
    ok_js = "true" if ok else "false"
    close_delay = "1200" if ok else "4000"
    body = (
        f"<p>{message}</p><script>"
        f"try {{ window.opener && window.opener.postMessage({{type:'fediverse-bridge-login', ok:{ok_js}}}, '*'); }} catch (e) {{}}"
        f"setTimeout(function() {{ window.close(); }}, {close_delay});"
        f"</script>"
    )
    return HTMLResponse(f"<!doctype html><html><body style='font-family:sans-serif;padding:2em'>{body}</body></html>")


@router.post("/widget/api/browse/login/password")
async def browse_login_password(request: Request) -> JSONResponse:
    matrix_user_id = _session_user(request)
    body = await request.json()
    domain = (body.get("domain") or "").strip().lower()
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not domain or not username or not password:
        raise HTTPException(400, "domain, username, and password are all required")
    try:
        await instance_auth.login_with_password(
            request, matrix_user_id=matrix_user_id, domain=domain, username=username, password=password
        )
    except InstanceAuthError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/widget/api/browse/login/list")
async def browse_login_list(request: Request) -> JSONResponse:
    matrix_user_id = _session_user(request)
    repository = request.app.state.repository
    logins = await repository.list_instance_logins(matrix_user_id)
    return JSONResponse(
        {
            "logins": [
                {
                    "domain": login.instance_domain,
                    "auth_method": login.auth_method,
                    "account_handle": login.account_handle,
                    "created_at": login.created_at,
                }
                for login in logins
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/widget/api/browse/login/logout")
async def browse_login_logout(request: Request) -> JSONResponse:
    """Logs out of (or just forgets, if the instance can't be reached to
    properly revoke -- revocation is best-effort, never blocking) one
    instance."""
    matrix_user_id = _session_user(request)
    body = await request.json()
    domain = (body.get("domain") or "").strip().lower()
    if not domain:
        raise HTTPException(400, "domain is required")
    repository = request.app.state.repository
    login = await repository.get_instance_login(matrix_user_id, domain)
    if login is not None:
        http_client = request.app.state.http_client
        try:
            await http_client.post(
                f"https://{domain}/oauth/revoke",
                data={"token": login.access_token},
                timeout=10.0,
            )
        except httpx.HTTPError:
            pass
    await repository.delete_instance_login(matrix_user_id, domain)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
