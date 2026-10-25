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

Misskey's own native API is a conceptually similar but totally different,
POST-based JSON one, not this one -- it's handled as a SECOND, parallel
family throughout this module (``_fetch_misskey_timeline``/
``_normalize_misskey_note``/``_MISSKEY_FAMILY_SOFTWARE``, dispatched via
``_browse_family``), not folded into the Mastodon-API path above. See
``_MISSKEY_FAMILY_SOFTWARE``'s own comment for which software is in it and
how confidently each one is verified -- "misskey" itself is live-verified,
Calckey/Firefish/Iceshrimp are included on shared-codebase-lineage
confidence only, not independently confirmed.

**Sharkey is the standing exception on the OTHER side**: unlike the rest
of the Misskey family, it ships a real, genuine Mastodon client-API-
compatible layer alongside its native one (an official, intended feature
-- Sharkey's own account lists "Mastodon API" among its features;
confirmed live 2026-10-24 against a real instance, federation.network,
software.name ``"sharkey"``: ``/api/v1/timelines/public``, ``/api/v2/
search``, ``/api/v1/accounts/lookup``, and ``/api/v1/statuses/{id}/
context`` all return real Mastodon-shaped JSON, unauthenticated) -- so it
lives in ``_COMPATIBLE_SOFTWARE`` instead of ``_MISSKEY_FAMILY_SOFTWARE``,
and goes through the Mastodon-API path, not the Misskey one. Lemmy and
PeerTube have their own separate APIs this module doesn't speak at all.

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
import dataclasses
import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from bridge import instance_auth
from bridge.commands import _discover_mastodon_api_reply_notes
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.remote_actor import RemoteActorFetchError, fetch_actor
from bridge.activitypub.routes import _build_profile_post_views
from bridge.activitypub.urls import username_from_actor_url
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
# design. "sharkey" is the one Misskey-family exception -- see module
# docstring for why, and don't add any OTHER Misskey fork here without the
# same live confirmation (their native API is a different, POST-based
# protocol this module doesn't speak at all).
_COMPATIBLE_SOFTWARE = {
    "mastodon", "pleroma", "akkoma", "hometown", "glitch-soc", "gotosocial", "sharkey",
}

# Native Misskey API family -- a totally different, POST-based JSON API
# from the Mastodon-API-compatible set above (see _fetch_misskey_timeline/
# _normalize_misskey_note/bridge.instance_auth's MiAuth functions).
# "misskey" itself is live-verified (confirmed 2026-10-25 against
# misskey.io): unauthenticated POST /api/notes/{local,global}-timeline and
# POST /api/users/search-by-username-and-host both work, and a note's own
# REST `id` composes deterministically into its real AP object id as
# https://{domain}/notes/{id} -- confirmed by fetching that exact URL with
# Accept: application/activity+json and checking the returned Note's own
# "id" field matches, since Misskey's REST API never exposes that URI
# directly itself. Calckey/Firefish/Iceshrimp are included on the strength
# of being direct source forks that have historically kept this same core
# REST surface (MiAuth, the *-timeline endpoints, the note/user JSON
# shape) -- NOT independently live-verified the same way (no reachable
# public instance of any of the three at the time this was written, unlike
# Sharkey's own verification in _COMPATIBLE_SOFTWARE's history). Remove
# any of the three here if one turns out to have actually diverged.
_MISSKEY_FAMILY_SOFTWARE = {"misskey", "calckey", "firefish", "iceshrimp"}

# Matches the Mastodon API's own default page size for timelines/search.
_PAGE_SIZE = 20

# Caps how long browse_home's merge waits for any ONE source instance
# before giving up on it for this page, deliberately much shorter than
# federation.request_timeout (15s, tuned for this bridge's own outbound AP
# delivery, where patience is the right tradeoff). A single slow/
# unreachable instance in the merge can otherwise stall EVERY other
# instance's results too -- browse_home awaits every source before
# returning anything at all (one REST response, not a stream), and
# _is_compatible's nodeinfo check plus _fetch_timeline's own request are
# two-to-three sequential round trips PER instance, each already allowed
# up to federation.request_timeout on its own -- so one bad instance could
# otherwise hold up the whole merged feed for closer to 45s than 15s
# (reported live 2026-10-24: Whole Known Network stuck on loading
# skeletons well past that). Deliberately NOT applied to browse_timeline
# (a single explicitly-chosen instance) -- there, the user picked that one
# instance on purpose and federation.request_timeout's own patience is the
# right call.
_HOME_FEED_SOURCE_TIMEOUT = 8.0

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


def _misskey_emoji_map(obj: dict[str, Any] | None) -> dict[str, str]:
    """Misskey's own ``emojis`` field -- present directly on BOTH a note
    (for a ``:shortcode:`` run in its own text/cw) and a user object (for
    one in that user's display name) -- is already a ``{shortcode: url}``
    dict, unlike Mastodon's list-of-objects shape (``_mastodon_emoji_map``
    needs to reshape that; this doesn't). Confirmed live against
    misskey.io 2026-10-25, on both a note's own ``emojis`` and a chained-
    custom-emoji display name's ``user.emojis`` -- contradicts
    ``_misskey_text_to_html``'s own previous assumption that resolving
    either needed a separate fetch no timeline response actually
    provides; it was already right there."""
    if not isinstance(obj, dict):
        return {}
    emojis = obj.get("emojis")
    if not isinstance(emojis, dict):
        return {}
    return {k: v for k, v in emojis.items() if isinstance(k, str) and isinstance(v, str)}


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


async def _is_misskey_family(request: Request, domain: str) -> bool:
    name = await remote_software_name(request, domain)
    return name in _MISSKEY_FAMILY_SOFTWARE


async def _browse_family(request: Request, domain: str) -> str | None:
    """Which of the two families of software Browse understands ``domain``
    runs, or ``None`` if it's neither (a bare AP server, Lemmy, PeerTube,
    vanilla Misskey-API-incompatible software, or simply unreachable) --
    the one check most callers actually want instead of asking
    ``_is_compatible``/``_is_misskey_family`` separately. ``remote_software_name``
    itself is already cached per-domain for an hour, so asking it twice
    here is cheap."""
    name = await remote_software_name(request, domain)
    if name in _COMPATIBLE_SOFTWARE:
        return "mastodon"
    if name in _MISSKEY_FAMILY_SOFTWARE:
        return "misskey"
    return None


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
    media = []
    for m in base.get("media_attachments") or []:
        if not isinstance(m, dict) or not isinstance(m.get("url"), str):
            continue
        # "gifv" is Mastodon's own type for a silent looping video (the
        # common "gif" meme format, actually served as an mp4) -- a real
        # video in every way that matters for playback here, just not
        # spelled "video". Left unmapped, renderPostCard's own type
        # switch fell through to its <img> fallback for every gifv
        # attachment, which can't play an mp4 at all (confirmed live
        # 2026-10-25: this is why "some videos" specifically, not all,
        # were failing to load -- true "video" attachments worked).
        kind = "video" if m.get("type") == "gifv" else m.get("type")
        media.append({
            "type": kind,
            # preview_url (a small static thumbnail) is the right choice
            # for an <img> tile, but a <video>/<audio> element needs the
            # actual playable file -- using preview_url for those meant
            # every video/audio attachment pointed a <video>/<audio> tag
            # at a static image instead of its real media URL, which
            # can't play at all (confirmed live 2026-10-25, the other
            # half of "some videos not loading").
            "url": m.get("url") if kind in ("video", "audio") else (m.get("preview_url") or m.get("url")),
            "description": m.get("description") if isinstance(m.get("description"), str) else "",
        })
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
        # The post's OWN origin, not instance_domain (the instance whose
        # timeline/search this came from) -- a federated-in post from a
        # Mastodon-API-compatible instance's timeline still carries its
        # real home server in its own url/uri (Mastodon never rewrites
        # these to the local instance), so "Open on ..." must read off
        # that instead. Using instance_domain here showed "Open on
        # federation.network" for a post actually authored on
        # infosec.exchange, just because federation.network's federated
        # timeline was what surfaced it (confirmed live 2026-10-25).
        "instance": urlparse(base.get("url") or uri).hostname or instance_domain,
    }


_BARE_URL_RE = re.compile(r"https?://\S+")


def _misskey_text_to_html(text: str, emoji_map: dict[str, str] | None = None) -> str:
    """Misskey's own ``text`` field is MFM (Misskey Flavored Markdown), not
    HTML -- nothing here parses it (no MFM parser exists in this codebase,
    and building one is out of scope for a widget preview card). Escaped,
    then only line breaks and bare URLs are turned into real markup, same
    as how a plain-text Mastodon post with no HTML would already render.
    A ``:shortcode:`` custom emoji in the text IS still substituted
    against ``emoji_map`` (see ``_misskey_emoji_map`` -- its own note
    carries this directly, no separate fetch needed, despite what an
    earlier version of this docstring assumed)."""
    if not text:
        return ""
    escaped = html.escape(text)
    linked = _BARE_URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" rel="nofollow noopener" target="_blank">{m.group(0)}</a>', escaped
    )
    with_breaks = linked.replace("\n", "<br>")
    return _substitute_emoji_html(with_breaks, emoji_map or {})


def _misskey_actor_id(user: dict[str, Any], *, instance_domain: str) -> str | None:
    """A LOCAL user's (``host`` is ``None``) actor id -- ``None`` for a
    remote one, whose Misskey-internal id is meaningless off this
    instance. See ``_normalize_misskey_note``'s own docstring for why."""
    if user.get("host"):
        return None
    uid = user.get("id")
    return f"https://{instance_domain}/users/{uid}" if isinstance(uid, str) and uid else None


def _misskey_profile_url(user: dict[str, Any], *, instance_domain: str) -> str | None:
    if user.get("host") or not isinstance(user.get("username"), str) or not user.get("username"):
        return None
    return f"https://{instance_domain}/@{user['username']}"


def _misskey_handle(user: dict[str, Any], *, instance_domain: str) -> str:
    username = user.get("username") if isinstance(user.get("username"), str) else ""
    if not username:
        return ""
    host = user.get("host") if isinstance(user.get("host"), str) and user.get("host") else instance_domain
    return f"@{username}@{host}"


def _normalize_misskey_note(note: dict[str, Any], *, instance_domain: str) -> dict[str, Any] | None:
    """Misskey-family equivalent of ``_normalize_status`` -- produces the
    exact same normalized shape, so ``renderPostCard`` on the frontend
    needs no changes to render either one. Relies on two facts confirmed
    live against a real instance (see ``_MISSKEY_FAMILY_SOFTWARE``'s own
    comment): a note's own REST ``id`` composes deterministically into its
    real AP object id as ``https://{domain}/notes/{id}``, and a LOCAL
    author's (``user.host`` is ``None``) actor id is
    ``https://{domain}/users/{id}`` the same way. A REMOTE author's
    Misskey-internal user id is meaningless off this instance (nothing in
    the timeline response exposes their real cross-instance actor URI), so
    ``actor_id`` is left ``None`` for one -- same degraded-but-safe
    fallback ``_normalize_status`` already uses when Mastodon's own
    ``account.uri`` happens to be missing; it just can't be clicked
    through to a profile from here.

    A pure renote (Misskey's boost, ``renoteId`` set and no added text of
    its own) is unwrapped the same way ``_normalize_status`` unwraps a
    Mastodon ``reblog``: ``base`` becomes the renoted note, and the outer
    note's own author becomes ``boosted_by``. A QUOTE renote (``renoteId``
    set AND the outer note has its own text) is deliberately NOT given the
    same quote-preview treatment Mastodon/Pleroma get elsewhere in this
    bridge -- out of scope here; it's shown as an ordinary standalone post
    instead, same as this function would render it with ``renote``
    ignored entirely."""
    renote = note.get("renote")
    is_pure_renote = isinstance(renote, dict) and not (note.get("text") or "").strip()
    base = renote if is_pure_renote else note
    if not isinstance(base, dict):
        return None
    base_id = base.get("id")
    if not isinstance(base_id, str) or not base_id:
        return None
    uri = f"https://{instance_domain}/notes/{base_id}"
    account = base.get("user") if isinstance(base.get("user"), dict) else {}
    booster = note.get("user") if (is_pure_renote and isinstance(note.get("user"), dict)) else None

    author_name = account.get("name") or account.get("username") or "(unknown)"
    booster_name = (booster.get("name") or booster.get("username") or "") if booster is not None else ""

    media = []
    for f in base.get("files") or []:
        if not isinstance(f, dict) or not isinstance(f.get("url"), str):
            continue
        mime = f.get("type") if isinstance(f.get("type"), str) else ""
        kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else None
        if kind is None:
            continue
        media.append({
            "type": kind, "url": f["url"],
            "description": f.get("comment") if isinstance(f.get("comment"), str) else "",
        })

    reactions = [
        {"name": name.strip(":"), "count": count, "url": None}
        for name, count in (base.get("reactions") or {}).items()
        if isinstance(name, str) and isinstance(count, int) and count > 0
    ]

    cw = base.get("cw") if isinstance(base.get("cw"), str) else None

    return {
        "uri": uri,
        "url": uri,
        "created_at": base.get("createdAt"),
        "content_html": _misskey_text_to_html(
            base.get("text") if isinstance(base.get("text"), str) else "", _misskey_emoji_map(base)
        ),
        "sensitive": bool(cw),
        "spoiler_text": cw or "",
        "author": {
            "actor_id": _misskey_actor_id(account, instance_domain=instance_domain),
            "display_name": author_name,
            "display_name_html": _display_name_html(author_name, _misskey_emoji_map(account)),
            "handle": _misskey_handle(account, instance_domain=instance_domain),
            "avatar_url": account.get("avatarUrl") if isinstance(account.get("avatarUrl"), str) else None,
            "url": _misskey_profile_url(account, instance_domain=instance_domain),
        },
        "media": media,
        "counts": {
            "replies": base.get("repliesCount") or 0,
            "reblogs": base.get("renoteCount") or 0,
            "favourites": base.get("reactionCount") or 0,
        },
        "reactions": reactions,
        "in_reply_to": bool(base.get("replyId")),
        "reply_hint_display_name": None,
        "reply_hint_handle": None,
        "boosted_by": (
            {
                "actor_id": _misskey_actor_id(booster, instance_domain=instance_domain),
                "display_name": booster_name,
                "display_name_html": _display_name_html(booster_name, _misskey_emoji_map(booster)),
                "handle": _misskey_handle(booster, instance_domain=instance_domain),
            }
            if booster is not None else None
        ),
        "instance": instance_domain,
    }


async def _misskey_auth_body(request: Request, *, matrix_user_id: str, domain: str) -> dict[str, Any]:
    token = await instance_auth.get_misskey_token(request, matrix_user_id=matrix_user_id, domain=domain)
    return {"i": token} if token else {}


async def _fetch_misskey_timeline(
    request: Request, *, domain: str, local: bool, max_id: str | None, matrix_user_id: str
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Misskey-family equivalent of ``_fetch_timeline`` -- same return
    shape, same meaning for each element (see that function's own
    docstring). Misskey's API is POST-based JSON throughout (never query
    params), and a sign-in token goes in the request BODY as ``i`` (see
    ``_misskey_auth_body``/``bridge.instance_auth``'s MiAuth functions),
    not an ``Authorization`` header. ``local`` maps to ``POST /api/notes/
    local-timeline``, ``federated`` to ``POST /api/notes/global-timeline``
    -- both confirmed live and unauthenticated against misskey.io (see
    ``_MISSKEY_FAMILY_SOFTWARE``'s own comment). There's no Misskey
    equivalent of Mastodon's authenticated-only "home" timeline fetched
    here, matching ``_fetch_timeline``'s own scope (only local/federated
    are ever requested by this module)."""
    http_client = request.app.state.http_client
    endpoint = "local-timeline" if local else "global-timeline"
    body: dict[str, Any] = {"limit": _PAGE_SIZE}
    body.update(await _misskey_auth_body(request, matrix_user_id=matrix_user_id, domain=domain))
    if max_id:
        body["untilId"] = max_id
    try:
        response = await http_client.post(f"https://{domain}/api/notes/{endpoint}", json=body)
    except httpx.HTTPError:
        return [], None, False
    if response.status_code in (401, 403):
        return [], None, True
    if response.status_code != 200:
        return [], None, False
    try:
        raw = response.json()
    except ValueError:
        return [], None, False
    if not isinstance(raw, list):
        return [], None, False
    posts = [
        p for p in (_normalize_misskey_note(n, instance_domain=domain) for n in raw if isinstance(n, dict))
        if p is not None
    ]
    next_max_id = raw[-1].get("id") if raw and isinstance(raw[-1], dict) else None
    return posts, (str(next_max_id) if len(raw) >= _PAGE_SIZE and next_max_id is not None else None), False


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


async def _fetch_timeline_any(
    request: Request, *, domain: str, family: str, local: bool, max_id: str | None, matrix_user_id: str
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Dispatches to whichever of ``_fetch_timeline``/``_fetch_misskey_timeline``
    matches ``family`` (as returned by ``_browse_family``) -- the one
    place that choice is made, so ``browse_timeline``/``browse_home`` don't
    each need their own copy of it."""
    if family == "misskey":
        return await _fetch_misskey_timeline(
            request, domain=domain, local=local, max_id=max_id, matrix_user_id=matrix_user_id
        )
    return await _fetch_timeline(request, domain=domain, local=local, max_id=max_id, matrix_user_id=matrix_user_id)


@router.get("/widget/api/browse/software")
async def browse_software(request: Request, domain: str) -> JSONResponse:
    _session_user(request)
    name = await remote_software_name(request, domain)
    return JSONResponse(
        {"domain": domain, "software": name, "supported": name in _COMPATIBLE_SOFTWARE or name in _MISSKEY_FAMILY_SOFTWARE},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/timeline")
async def browse_timeline(request: Request, domain: str, scope: str = "local", max_id: str | None = None) -> JSONResponse:
    matrix_user_id = _session_user(request)
    if scope not in ("local", "federated"):
        raise HTTPException(400, "scope must be 'local' or 'federated'")
    family = await _browse_family(request, domain)
    if family is None:
        return JSONResponse(
            {"supported": False, "needs_login": False, "posts": [], "next_max_id": None},
            headers={"Cache-Control": "no-store"},
        )
    posts, next_max_id, needs_login = await _fetch_timeline_any(
        request, domain=domain, family=family, local=(scope == "local"), max_id=max_id, matrix_user_id=matrix_user_id
    )
    await _mark_imported(request, posts)
    return JSONResponse(
        {"supported": True, "needs_login": needs_login, "posts": posts, "next_max_id": next_max_id},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/widget/api/browse/home")
async def browse_home(request: Request, max_id_map: str = "", extra_domains: str = "") -> JSONResponse:
    """The widget's "Whole Known Network" feed: every configured
    ``bridge.default_timeline_domains`` instance's OWN federated ("whole
    known network") timeline, PLUS every instance in ``extra_domains``
    (comma-separated), merged by recency. ``extra_domains`` is how this
    picks up the instances a given USER has added via the widget's own
    "+ Add instance" flow -- those live only in that browser's
    localStorage (see ``renderHomeTimeline``'s own comment), never
    persisted server-side, so the frontend sends the list along on every
    call rather than this bridge trying to remember it itself. Falls back
    to just this bridge's own domain if neither source has anything --
    which, since this bridge's own domain never runs Mastodon-API-
    compatible software, correctly comes back empty rather than guessing
    at some other public instance to advertise by default.

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
    configured_domains = list(config.bridge.default_timeline_domains or [])
    user_domains = [d.strip() for d in extra_domains.split(",") if d.strip()]
    seen: set[str] = set()
    domains: list[str] = []
    for d in configured_domains + user_domains:
        if d not in seen:
            seen.add(d)
            domains.append(d)
    if not domains:
        domains = [config.bridge.domain]
    try:
        cursor_map: dict[str, Any] = json.loads(max_id_map) if max_id_map else {}
    except ValueError:
        cursor_map = {}

    async def _one(domain: str) -> tuple[str, list[dict[str, Any]], str | None, bool, bool, bool]:
        try:
            async with asyncio.timeout(_HOME_FEED_SOURCE_TIMEOUT):
                family = await _browse_family(request, domain)
                if family is None:
                    return domain, [], None, False, False, False
                posts, next_max_id, needs_login = await _fetch_timeline_any(
                    request, domain=domain, family=family, local=False,
                    max_id=cursor_map.get(domain), matrix_user_id=matrix_user_id,
                )
                return domain, posts, next_max_id, True, needs_login, False
        except TimeoutError:
            # A slow/unresponsive source must never hold up every OTHER
            # source's results too -- see _HOME_FEED_SOURCE_TIMEOUT's own
            # comment. Reported as its own distinct state (not lumped into
            # needs_login_domains or treated as plain "unsupported") so the
            # frontend can tell a user "X is slow right now" apart from
            # "X needs you to sign in" or "X doesn't run compatible
            # software" -- three different things, three different next
            # steps for the user.
            return domain, [], None, True, False, True

    results = await asyncio.gather(*(_one(d) for d in domains))
    merged: list[dict[str, Any]] = []
    next_cursor_map: dict[str, str] = {}
    any_supported = False
    needs_login_domains: list[str] = []
    timed_out_domains: list[str] = []
    for domain, posts, next_max_id, supported, needs_login, timed_out in results:
        any_supported = any_supported or supported
        merged.extend(posts)
        if next_max_id:
            next_cursor_map[domain] = next_max_id
        if needs_login:
            needs_login_domains.append(domain)
        if timed_out:
            timed_out_domains.append(domain)
    merged.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    await _mark_imported(request, merged)
    return JSONResponse(
        {
            "supported": any_supported,
            "domains": domains,
            "needs_login_domains": needs_login_domains,
            "timed_out_domains": timed_out_domains,
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

    if not domain:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    family = await _browse_family(request, domain)
    if family is None:
        return JSONResponse({"accounts": []}, headers={"Cache-Control": "no-store"})
    http_client = request.app.state.http_client

    if family == "misskey":
        # POST /api/users/search-by-username-and-host -- confirmed live
        # and unauthenticated against misskey.io (see
        # _MISSKEY_FAMILY_SOFTWARE's own comment). No sign-in token is
        # attached here even if the user has one on file: a fuzzy account
        # search like this one is the same best-effort discovery tool
        # regardless of auth, unlike a timeline view an instance can
        # genuinely gate.
        try:
            response = await http_client.post(
                f"https://{domain}/api/users/search-by-username-and-host",
                json={"username": q, "limit": 8},
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
        for u in raw if isinstance(raw, list) else []:
            if not isinstance(u, dict):
                continue
            display_name = u.get("name") or u.get("username") or ""
            accounts.append({
                "actor_id": _misskey_actor_id(u, instance_domain=domain),
                "handle": _misskey_handle(u, instance_domain=domain),
                "display_name": display_name,
                "display_name_html": _display_name_html(display_name, _misskey_emoji_map(u)),
                "avatar_url": u.get("avatarUrl") if isinstance(u.get("avatarUrl"), str) else None,
            })
        return JSONResponse({"accounts": accounts}, headers={"Cache-Control": "no-store"})

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
_THREAD_MAX_NESTED_CANDIDATES = 3


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
    """Every immediate reply's own uri, as leniently as real-world AS2
    varies this: ``replies`` itself can be an embedded collection (the
    Mastodon convention, confirmed live) or a bare collection-id string
    needing its own fetch; that collection's first page can be embedded
    under ``first``, a ``first`` URL needing a fetch, or -- some software
    skips the page wrapper entirely -- items inlined directly on the
    collection. Each of those pages can name its items ``items`` or
    ``orderedItems``. Returns ``[]`` (never raises) for anything that
    doesn't match one of these, same best-effort posture as the rest of
    this module."""
    replies = note_doc.get("replies")
    collection: dict[str, Any] | None = None
    if isinstance(replies, dict):
        collection = replies
    elif isinstance(replies, str):
        collection = await _fetch_as2_note(request, replies)
    if not isinstance(collection, dict):
        return []
    first = collection.get("first")
    page: dict[str, Any] | None = None
    if isinstance(first, dict):
        page = first
    elif isinstance(first, str):
        page = await _fetch_as2_note(request, first)
    elif isinstance(collection.get("items"), list) or isinstance(collection.get("orderedItems"), list):
        page = collection
    if not isinstance(page, dict):
        return []
    items = page.get("items") if isinstance(page.get("items"), list) else page.get("orderedItems")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, str)][:_THREAD_MAX_REPLIES]


async def _fetch_direct_children(
    request: Request, parent_doc: dict[str, Any], *, descendant_pool: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Every direct reply to ``parent_doc``. Tries the Note's own AS2
    ``replies`` collection first; Pleroma/Akkoma notably don't populate
    this at all (confirmed live against a real blob.cat/Akkoma post --
    ``replies`` is simply absent, not just empty), so when that comes up
    with nothing, falls back to filtering ``descendant_pool`` (the whole
    subtree, already fetched once via the Mastodon-API ``/context``
    endpoint -- see ``_discover_mastodon_api_reply_notes``, the same
    known-unreliable-AS2-replies-collection fallback already used for
    ``;backfill``) down to whichever of those have THIS post as their own
    ``inReplyTo``."""
    parent_uri = parent_doc.get("id")
    reply_uris = await _fetch_reply_uris(request, parent_doc)
    if reply_uris:
        docs = await asyncio.gather(*(_fetch_as2_note(request, u) for u in reply_uris))
        return [d for d in docs if d is not None]
    if descendant_pool and isinstance(parent_uri, str):
        return [d for d in descendant_pool if d.get("inReplyTo") == parent_uri]
    return []


async def _fetch_context_descendant_pool(request: Request, *, origin_uri: str, fallback_object_id: str) -> list[dict[str, Any]]:
    """The whole descendant subtree via the Mastodon-API ``/context``
    endpoint, for when a post's own AS2 ``replies`` collection is empty
    or absent. Tries the ORIGINAL uri's own last path segment as a
    Mastodon-API status id directly first -- a Mastodon/Pleroma/Akkoma
    permalink (``/@user/<id>``, ``/notice/<id>``) literally IS that id in
    its own URL, which works even when an instance's unauthenticated
    ``/api/v1/search`` is disabled (confirmed live: blob.cat's Akkoma
    returns ``{"error":"Not implemented"}`` for search, but
    ``/api/v1/statuses/<id>/context`` with that same id works fine,
    unauthenticated, and finds real descendants search alone would have
    missed entirely). Falls back to ``_discover_mastodon_api_reply_notes``'s
    own search-based resolution (needed when the uri's last segment isn't
    a usable id -- an outbox-item URL, for instance) if that direct guess
    doesn't pan out."""
    http_client = request.app.state.http_client
    parsed = urlparse(origin_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidate_id = origin_uri.rstrip("/").rsplit("/", 1)[-1]
    if candidate_id:
        try:
            resp = await http_client.get(f"{origin}/api/v1/statuses/{candidate_id}/context")
            if resp.status_code == 200:
                descendants = resp.json().get("descendants") or []
                notes = []
                for status in descendants:
                    status_uri = status.get("uri") if isinstance(status, dict) else None
                    if not isinstance(status_uri, str):
                        continue
                    note = await _fetch_as2_note(request, status_uri)
                    if note is not None and note.get("type") == "Note":
                        notes.append(note)
                if notes:
                    return notes
        except (httpx.HTTPError, ValueError):
            pass
    return await _discover_mastodon_api_reply_notes(
        request, root_ap_object_id=fallback_object_id, limit=_THREAD_MAX_REPLIES * 4, already_seen=set()
    )


async def _find_op_reply(
    request: Request, reply_doc: dict[str, Any], focus_author_id: Any, *, descendant_pool: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The one reply-of-reply (if any) that's authored by the FOCUSED
    post's own author continuing the conversation with this particular
    direct reply -- the 'OP replying back' pattern Haven Desktop/Poa.st
    show nested with a connecting line, deliberately not a full nested
    reply tree (every other reply-of-reply, by anyone else, stays
    uncollapsed/unshown rather than growing this into a full thread
    tree)."""
    if not isinstance(focus_author_id, str):
        return None
    candidates = (await _fetch_direct_children(request, reply_doc, descendant_pool=descendant_pool))[:_THREAD_MAX_NESTED_CANDIDATES]
    for doc in candidates:
        if doc.get("attributedTo") == focus_author_id:
            return doc
    return None


async def _resolve_via_mastodon_status_lookup(request: Request, *, raw_uri: str) -> str | None:
    """Another way a 'pretty' URL isn't the real AS2 object: an
    instance's own cached/mirrored view of a post that actually lives
    elsewhere -- confirmed live, poa.st's own
    ``/@user@otherinstance/posts/<id>`` display route for a remote
    minds.com post. Unlike the search fallback below, this needs no
    auth and no instance-specific URL parsing: a Mastodon/Pleroma/
    Akkoma permalink's own trailing path segment IS that instance's
    local Mastodon-API status id (the same trick already used for
    ``_fetch_context_descendant_pool``'s reply discovery), and
    ``GET /api/v1/statuses/<id>`` -- unauthenticated, confirmed live --
    returns that status's real ``uri``, which for a mirrored/cached post
    is the genuine remote object's own id (confirmed live: exactly the
    working ``.../entities/urn:activity:...`` form a user had already
    found by hand for a poa.st-mirrored minds.com post)."""
    http_client = request.app.state.http_client
    parsed = urlparse(raw_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidate_id = raw_uri.rstrip("/").rsplit("/", 1)[-1]
    if not candidate_id:
        return None
    try:
        resp = await http_client.get(f"{origin}/api/v1/statuses/{candidate_id}")
        if resp.status_code != 200:
            return None
        status_uri = resp.json().get("uri")
    except (httpx.HTTPError, ValueError):
        return None
    return status_uri if isinstance(status_uri, str) else None


async def _resolve_pretty_url_via_search(request: Request, *, raw_uri: str, matrix_user_id: str) -> str | None:
    """A 'pretty' frontend URL isn't always a real, content-negotiable AS2
    object -- confirmed live: poa.st's own display route for a REMOTE
    account's post (``/@user@otherinstance/posts/<id>``) returns plain
    HTML even with ``Accept: application/activity+json``, since it's a
    client-side SPA route proxying the real post from wherever it's
    actually hosted, not a genuine ActivityPub permalink. That instance's
    own Mastodon-API search with ``resolve=true`` can still often resolve
    it to the real remote object -- but unauthenticated resolve commonly
    returns nothing (confirmed live against poa.st), so this attaches
    this bridge user's own stored login for that domain, if any, the
    same way ``browse_timeline``/``browse_search`` already do."""
    parsed = urlparse(raw_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    http_client = request.app.state.http_client
    headers = await _auth_headers(request, matrix_user_id=matrix_user_id, domain=parsed.netloc)
    try:
        resp = await http_client.get(
            f"{origin}/api/v1/search", params={"q": raw_uri, "resolve": "true", "type": "statuses"}, headers=headers,
        )
        resp.raise_for_status()
        statuses = resp.json().get("statuses") or []
    except (httpx.HTTPError, ValueError):
        return None
    for status in statuses:
        if isinstance(status, dict) and isinstance(status.get("uri"), str):
            return status["uri"]
    return None


@router.get("/widget/api/browse/thread")
async def browse_thread(request: Request, uri: str) -> JSONResponse:
    matrix_user_id = _session_user(request)
    focus_doc = await _fetch_as2_note(request, uri)
    if focus_doc is None:
        resolved_uri = await _resolve_via_mastodon_status_lookup(request, raw_uri=uri)
        if resolved_uri is not None:
            focus_doc = await _fetch_as2_note(request, resolved_uri)
    if focus_doc is None:
        resolved_uri = await _resolve_pretty_url_via_search(request, raw_uri=uri, matrix_user_id=matrix_user_id)
        if resolved_uri is not None:
            focus_doc = await _fetch_as2_note(request, resolved_uri)
    if focus_doc is None:
        raise HTTPException(
            502,
            "Couldn't load that post -- the link might not be a real ActivityPub permalink "
            "(e.g. a page that only works in that instance's own web app), or it needs you to be "
            "signed in to that instance first.",
        )

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

    focus_id = focus_doc.get("id") if isinstance(focus_doc.get("id"), str) else uri
    reply_docs = await _fetch_direct_children(request, focus_doc, descendant_pool=None)
    descendant_pool: list[dict[str, Any]] | None = None
    if not reply_docs:
        # The native AS2 replies collection came up empty (missing
        # entirely, on Pleroma/Akkoma, or genuinely zero replies) -- fetch
        # the whole subtree once via the Mastodon-API fallback and reuse
        # it both for this post's own direct replies and for each of
        # those replies' own op_reply lookup below, rather than one
        # extra fetch per candidate. Filtered against the post's own
        # CANONICAL id (post-redirect, e.g. a Pleroma/Akkoma /objects/
        # uuid) -- the original `uri` the caller passed (e.g. a /notice/
        # permalink) is a different string and would never match a
        # descendant's own inReplyTo.
        descendant_pool = await _fetch_context_descendant_pool(request, origin_uri=uri, fallback_object_id=focus_id)
        reply_docs = [d for d in descendant_pool if d.get("inReplyTo") == focus_id][:_THREAD_MAX_REPLIES]

    focus_author_id = focus_doc.get("attributedTo")
    op_reply_docs = await asyncio.gather(
        *(_find_op_reply(request, d, focus_author_id, descendant_pool=descendant_pool) for d in reply_docs)
    )

    normalized_ancestors = [p for p in await asyncio.gather(*(_normalize_as2_note_full(request, d) for d in ancestors)) if p is not None]
    normalized_focus = await _normalize_as2_note_full(request, focus_doc)
    normalized_replies = []
    for reply_doc, op_reply_doc in zip(reply_docs, op_reply_docs):
        normalized_reply = await _normalize_as2_note_full(request, reply_doc)
        if normalized_reply is None:
            continue
        normalized_reply["op_reply"] = await _normalize_as2_note_full(request, op_reply_doc) if op_reply_doc is not None else None
        normalized_replies.append(normalized_reply)

    await _mark_imported(request, normalized_ancestors)
    if normalized_focus is not None:
        await _mark_imported(request, [normalized_focus])
    flat_replies = normalized_replies + [r["op_reply"] for r in normalized_replies if r.get("op_reply") is not None]
    await _mark_imported(request, flat_replies)

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


def _post_view_to_widget_post(view: Any) -> dict[str, Any]:
    """Converts a ``bridge.web_views.PostView`` (built by
    ``bridge.activitypub.routes._build_post_view``, the same building
    block the HTML profile page's own post list uses) into the identical
    normalized post dict shape ``_normalize_status``/
    ``_normalize_misskey_note``/``_normalize_ap_item`` all already
    produce -- so ``renderPostCard`` on the frontend needs no changes to
    render one of these either."""
    media: list[dict[str, Any]] = []
    if view.attachment:
        ap_type = view.attachment.get("type") or ""
        kind = "image" if ap_type == "Image" else "video" if ap_type == "Video" else None
        if kind and isinstance(view.attachment.get("url"), str):
            media.append({"type": kind, "url": view.attachment["url"], "description": ""})
    favourites = view.reactions.like_count + sum(e.count for e in view.reactions.emojis)
    created_at = (
        datetime.fromtimestamp(view.origin_server_ts / 1000, tz=timezone.utc).isoformat()
        if view.origin_server_ts else None
    )
    return {
        "uri": view.ap_object_id,
        "url": view.source_url or view.ap_object_id,
        "created_at": created_at,
        "content_html": view.content_html,
        "sensitive": False,
        "spoiler_text": "",
        "author": {
            "actor_id": view.author_profile_url,
            "display_name": view.author_display_name,
            "display_name_html": view.author_display_name_html,
            "handle": view.author_handle,
            "avatar_url": view.author_avatar_url,
            "url": view.author_profile_url,
        },
        "media": media,
        # Not tracked per-post by PostView (the HTML page doesn't show
        # them either) -- favourites alone, from the reactions this bridge
        # DOES track, same degraded-but-honest shape _normalize_status
        # falls back to when a field genuinely isn't available.
        "counts": {"replies": 0, "reblogs": 0, "favourites": favourites},
        "reactions": [],
        "in_reply_to": False,
        "reply_hint_display_name": None,
        "reply_hint_handle": None,
        "boosted_by": None,
        "instance": urlparse(view.ap_object_id).hostname or "",
    }


async def _fetch_local_actor_profile_page(
    request: Request, *, record: Any, cursor_map: dict[str, str | None]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The widget's own equivalent of the public HTML profile page's post
    list (``bridge.activitypub.routes._build_profile_post_views``), but
    additionally merged across ``record``'s current Profile Room AND every
    room it replaced (``ActorRepository.get_profile_room_history`` --
    same source ``get_outbox`` merges from), neither of which alone
    covers both: ``get_outbox`` merges rooms but only ever reads ONE
    shallow batch per room (confirmed live: a profile with plenty of real
    history came back with only 2 posts), while
    ``_build_profile_post_views`` deep-paginates properly but only ever
    looks at the current room, missing anything posted before the
    Profile Room was last replaced. Reported live 2026-10-25: the
    widget's own "Posts" list (riding on the shallow ``get_outbox`` path
    via ``_fetch_outbox_page``) was missing posts the HTML page itself
    showed.

    ``cursor_map`` is ``{room_id: synapse_pagination_token_or_None}``,
    carried from the previous page (``None`` for a room whose own history
    is already exhausted -- never queried again after that). Each room is
    deep-paginated independently via ``_build_profile_post_views`` (reused
    verbatim, not reimplemented, via ``dataclasses.replace`` to point the
    same ``record`` at each room in turn), then every room's results are
    merged and re-sorted by recency -- same reasoning as ``get_outbox``'s
    own per-room-then-merge shape, just with real pagination depth this
    time."""
    repository = request.app.state.repository
    past_room_ids = await repository.get_profile_room_history(record.username)
    room_ids = [record.room_id] + [r for r in past_room_ids if r != record.room_id]

    views = []
    next_cursor_map: dict[str, str] = {}
    for room_id in room_ids:
        if room_id in cursor_map and cursor_map[room_id] is None:
            continue  # this room's own history was already exhausted on an earlier page
        room_record = dataclasses.replace(record, room_id=room_id)
        room_posts, next_token = await _build_profile_post_views(
            request, room_record, from_token=cursor_map.get(room_id)
        )
        views.extend(room_posts)
        if next_token:
            next_cursor_map[room_id] = next_token
    views.sort(key=lambda v: v.origin_server_ts, reverse=True)
    return [_post_view_to_widget_post(v) for v in views], next_cursor_map


@router.get("/widget/api/browse/profile/timeline")
async def browse_profile_timeline(request: Request, actor_id: str, cursor: str = "") -> JSONResponse:
    """``cursor`` means two different things depending on whether
    ``actor_id`` is local or remote -- opaque either way to the frontend,
    which only ever echoes back whatever ``next_cursor`` it was last
    given (see ``renderProfilePostFeed``'s own call site): a real AS2
    collection-page URL for a remote actor (``_fetch_outbox_page``'s own
    cursor), or a JSON-encoded ``{room_id: token}`` map for a local one
    (``_fetch_local_actor_profile_page``'s own cursor -- see its
    docstring for why a local actor needs an entirely different fetch
    path in the first place, not just a different cursor shape)."""
    _session_user(request)
    config = request.app.state.config
    repository = request.app.state.repository
    username = username_from_actor_url(config.bridge.public_base_url, actor_id)
    record = await repository.get_local_actor(username) if username else None
    if record is not None and record.room_id:
        try:
            cursor_map: dict[str, str | None] = json.loads(cursor) if cursor else {}
        except ValueError:
            cursor_map = {}
        posts, next_cursor_map = await _fetch_local_actor_profile_page(request, record=record, cursor_map=cursor_map)
        await _mark_imported(request, posts)
        return JSONResponse(
            {"posts": posts, "next_cursor": json.dumps(next_cursor_map) if next_cursor_map else ""},
            headers={"Cache-Control": "no-store"},
        )
    posts, next_cursor = await _fetch_outbox_page(request, actor_id=actor_id, cursor=cursor or None)
    await _mark_imported(request, posts)
    return JSONResponse({"posts": posts, "next_cursor": next_cursor or ""}, headers={"Cache-Control": "no-store"})


# -- instance sign-in (OAuth + password-grant fallback) --------------------


@router.post("/widget/api/browse/login/start")
async def browse_login_start(request: Request) -> JSONResponse:
    """Begin a sign-in to ``body['domain']`` -- OAuth for a Mastodon-API-
    compatible instance, MiAuth for a Misskey-family one (see
    ``_browse_family``), whichever ``remote_software_name`` says it is.
    Returns a URL the widget opens in a new tab (its own iframe can't
    navigate top-level to a cross-origin page); the opened tab posts a
    message back and closes itself once ``browse_login_callback`` below
    completes, for either flow alike."""
    matrix_user_id = _session_user(request)
    body = await request.json()
    domain = (body.get("domain") or "").strip().lower()
    if not domain:
        raise HTTPException(400, "domain is required")
    family = await _browse_family(request, domain)
    if family is None:
        raise HTTPException(400, f"Sign-in isn't available for {domain} -- it isn't a supported kind of instance.")
    try:
        if family == "misskey":
            url = await instance_auth.start_miauth(request, matrix_user_id=matrix_user_id, domain=domain)
        else:
            url = await instance_auth.start_oauth(request, matrix_user_id=matrix_user_id, domain=domain)
    except InstanceAuthError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"authorize_url": url}, headers={"Cache-Control": "no-store"})


@router.get("/widget/api/browse/login/callback")
async def browse_login_callback(
    request: Request, code: str = "", state: str = "", session: str = "", error: str = ""
) -> HTMLResponse:
    """The instance redirects the user's real browser tab here after they
    approve (or deny) the sign-in prompt -- this is a bare top-level page
    load, not a widget-authenticated API call, so it has no session of
    its own. OAuth's own redirect carries ``code``/``state`` (minted by
    ``start_oauth``); MiAuth's carries ``session`` (minted by
    ``start_miauth``) instead -- whichever one actually shows up is what
    tells the two flows apart here and reconnects it to the bridge user
    who started it, since both land on this same URL (see
    ``bridge.instance_auth``'s own module docstring for why one shared
    callback path is simpler than registering two)."""
    if error:
        message, ok = f"Sign-in was cancelled or denied ({error}).", False
    elif session:
        try:
            _matrix_user_id, domain = await instance_auth.complete_miauth(request, session=session)
            message, ok = f"Signed in to {domain}. You can close this tab.", True
        except InstanceAuthError as exc:
            message, ok = str(exc), False
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
    if await _is_misskey_family(request, domain):
        # Misskey-family has no password-grant equivalent exposed by this
        # module -- MiAuth (the "Sign in" button's own OAuth-equivalent
        # path) is the only sign-in method for one. Checked explicitly so
        # this fails with a clear reason instead of a confusing generic
        # HTTP error from login_with_password trying (and failing) to
        # register an OAuth app that doesn't exist on this kind of
        # instance.
        raise HTTPException(400, f"{domain} doesn't support username/password sign-in -- use the main Sign in button instead.")
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
