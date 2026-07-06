"""FastAPI router implementing the public ActivityPub server surface.

Endpoints implemented here: WebFinger, Actor discovery, outbox, followers,
following, the inbox, and a media proxy. Outbox is populated live from the
linked Profile Room's Matrix history (re-reading `body` from Matrix rather
than caching it ourselves, per the data-sovereignty constraint), restricted
to messages that were actually distributed as posts (i.e. have a recorded
``FederatedEvent``, so the returned Note ``id`` matches exactly what
followers already received from ``bridge.profile_posts``). Followers/
following read live state from ``ActorRepository``. Both the per-actor inbox
(``/inbox/{username}``) and the shared inbox (``/inbox``, advertised as
``endpoints.sharedInbox`` and also used by some implementations that deliver
there regardless of what's advertised) verify HTTP signatures, then hand the
activity to ``bridge.inbox_dispatch`` for processing
(Follow/Accept/Create/Like/Announce/Undo/Delete) -- the shared inbox resolves
the target local actor from the activity body itself rather than the URL.
The media
proxy exists because Matrix media downloads require an access token
(MSC3916) that remote fediverse servers don't have -- the bridge fetches
from Synapse authenticated as itself and re-serves the bytes publicly, but
only for media on ``ActorRepository``'s published-media allowlist (avatars
and post attachments the bridge itself published), so it can't be used as
a general anonymous gateway to every other room's media on the homeserver.
It also supports single-range requests (RFC 7233) so video/audio is
seekable in remote clients.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from bridge.activitypub.models import (
    ACTIVITY_JSON_CONTENT_TYPE,
    AS_PUBLIC,
    JSON_LD_CONTEXT,
    Activity,
    Actor,
    Note,
    OrderedCollection,
    PublicKey,
)
from bridge.activitypub.sanitize import plain_text_to_note_html
from bridge.activitypub.signatures import SignatureError, verify_incoming_request
from bridge.custom_emoji import apply_resolved_emoji
from bridge.note_mirroring import build_repost_note_content, split_repost_caption
from bridge.activitypub.urls import (
    actor_url,
    followers_url,
    following_url,
    inbox_url,
    main_key_id,
    media_url,
    outbox_url,
    shared_inbox_url,
    username_from_actor_url,
)
from bridge.activitypub.webfinger import build_local_webfinger_document, parse_acct
from bridge.inbox_dispatch import handle_activity
from bridge.media import build_ap_attachment, media_caption
from bridge.reply_bridge import derive_in_reply_to
from bridge.repository import ActorRepository, FederatedEvent
from bridge.synapse_client import SynapseError
from bridge.web_views import (
    PersonRef,
    PostView,
    ReactionEvent,
    render_profile_page,
    render_thread_page,
    summarize_reactions,
)

logger = logging.getLogger(__name__)

router = APIRouter()
OUTBOX_HISTORY_LIMIT = 20
# Posts-per-page for the public thread HTML view (root + replies together --
# see _build_thread_post_views) -- pagination controls only appear once a
# thread actually exceeds this many posts.
THREAD_PAGE_SIZE = 20
# Actual posts-per-page target for the profile HTML view -- distinct from
# OUTBOX_HISTORY_LIMIT (a raw Matrix /messages batch size, for the JSON
# outbox, which has no pagination and no reason to match this). A raw batch
# routinely contains far fewer real posts than its own size once non-post
# traffic (reactions, thread replies from others, membership events, ...)
# is filtered out -- see _build_profile_post_views, which fetches
# PROFILE_FETCH_BATCH_SIZE-sized raw batches in a loop until this many
# actual posts are collected, rather than treating one raw batch as one page.
PROFILE_PAGE_SIZE = 10
PROFILE_FETCH_BATCH_SIZE = 50
# Soft cap on how many raw batches one page render will fetch once it has
# found AT LEAST ONE post -- bounds the cost of a page that's already
# non-blank, at the cost of occasionally coming up short of
# PROFILE_PAGE_SIZE. Deliberately NOT applied while zero posts have been
# found yet: stopping there would return an outright blank page with a
# "next" link, just moving the same problem to the next click instead of
# fixing it. PROFILE_HARD_FETCH_CAP is the real ceiling for that case --
# high enough that a real profile room should never hit it, existing only
# to bound a pathological one.
PROFILE_MAX_FETCH_BATCHES = 10
PROFILE_HARD_FETCH_CAP = 200


def _activity_json(payload: dict) -> JSONResponse:
    return JSONResponse(content=payload, media_type=ACTIVITY_JSON_CONTENT_TYPE)


async def _get_actor_record(request: Request, username: str):
    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No such actor: {username}")
    return record


def _build_actor(request: Request, record) -> Actor:
    base = request.app.state.config.bridge.public_base_url
    return Actor(
        id=actor_url(base, record.username),
        preferred_username=record.username,
        name=record.display_name or record.username,
        summary=record.summary,
        url=actor_url(base, record.username),
        inbox=inbox_url(base, record.username),
        outbox=outbox_url(base, record.username),
        followers=followers_url(base, record.username),
        following=following_url(base, record.username),
        icon_url=record.icon_url,
        image_url=record.banner_url,
        shared_inbox=shared_inbox_url(base),
        # Advertises the "Chat" option (see bridge.chat_bridge) on software
        # that checks for it -- every local actor accepts them, same as
        # every local actor already implicitly accepts ordinary DMs.
        accepts_chat_messages=True,
        public_key=PublicKey(
            id=main_key_id(base, record.username),
            owner=actor_url(base, record.username),
            public_key_pem=record.public_key_pem,
        ),
    )


@router.get("/.well-known/webfinger")
async def webfinger(request: Request, resource: str = Query(...)) -> JSONResponse:
    config = request.app.state.config
    try:
        parsed = parse_acct(resource)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if parsed.domain != config.bridge.domain:
        raise HTTPException(status_code=404, detail="Resource is not hosted on this domain")

    record = await request.app.state.repository.get_local_actor(parsed.username)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No such actor: {parsed.username}")

    document = build_local_webfinger_document(
        username=record.username,
        bridge_domain=config.bridge.domain,
        actor_url=actor_url(config.bridge.public_base_url, record.username),
    )
    return JSONResponse(content=document, media_type="application/jrd+json")


@router.get("/actor/{username}")
async def get_actor(request: Request, username: str) -> Response:
    record = await _get_actor_record(request, username)
    if _prefers_html(request):
        config = request.app.state.config
        repository: ActorRepository = request.app.state.repository
        from_token = request.query_params.get("before") or None
        posts, next_token = await _build_profile_post_views(request, record, from_token=from_token)
        older_posts_url = f"/actor/{username}?before={quote(next_token, safe='')}" if next_token else None

        # Same "count always real, list withheld when hidden" split as the
        # public AP collection endpoints (get_followers/get_following) --
        # this static HTML page is just as reachable by any anonymous remote
        # visitor, so it honors the exact same hide_followers/hide_following
        # setting. Resolving each member's identity is skipped entirely when
        # hidden -- nothing to show, and no reason to pay for it.
        follower_ids = await repository.list_followers(username)
        following_ids = await repository.list_following(username)
        followers = (
            [await _resolve_person_ref(request, actor_id) for actor_id in follower_ids]
            if not record.hide_followers else []
        )
        following = (
            [await _resolve_person_ref(request, actor_id) for actor_id in following_ids]
            if not record.hide_following else []
        )

        html_doc = render_profile_page(
            display_name=record.display_name or record.username,
            handle=f"@{record.username}@{config.bridge.domain}",
            summary_html=plain_text_to_note_html(record.summary) if record.summary else "",
            avatar_url=record.icon_url,
            banner_url=record.banner_url,
            posts=posts,
            older_posts_url=older_posts_url,
            followers_count=len(follower_ids),
            followers_hidden=record.hide_followers,
            followers=followers,
            following_count=len(following_ids),
            following_hidden=record.hide_following,
            following=following,
        )
        return HTMLResponse(html_doc)
    return _activity_json(_build_actor(request, record).to_dict())


def _matrix_ts_to_iso(origin_server_ts: int | None) -> str:
    if not origin_server_ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reconstruct_note_body(federated, event_content: dict, attachment: dict | None) -> tuple[str, str, str | None]:
    """``(body, content_html, quote_uri)`` for reconstructing a Note from
    the Matrix event that mirrors it (shared by ``get_outbox`` and
    ``get_note``).

    ``federated.boosted_object_id`` set means this is a ``;repost``'s own
    echo (see ``bridge.commands._handle_repost``) -- its Matrix event's
    ``body`` is NEVER "just a filename" the way an ordinary media post's
    is (see ``bridge.inbox_dispatch.build_preview_media_content``, which
    always keeps the full caption+notice text as ``body`` regardless of
    ``msgtype``), and its actual ActivityPub content differs from that
    Matrix-facing text entirely -- see ``build_repost_note_content``/
    ``split_repost_caption``. An ordinary post's ``body`` with an
    attachment is a genuine caption only under the caption convention
    (separate differing ``filename`` -- see ``bridge.media.media_caption``),
    else it's just the filename and is blanked."""
    if federated.boosted_object_id:
        body = (event_content.get("body") or "").strip()
        caption = split_repost_caption(body)
        return body, build_repost_note_content(caption, federated.boosted_object_id), federated.boosted_object_id
    body = media_caption(event_content) if attachment is not None else (event_content.get("body") or "").strip()
    return body, (plain_text_to_note_html(body) if body else ""), None


def _effective_event_content(matrix_event: dict) -> dict:
    """The content a post should be RENDERED from: the latest ``m.replace``
    edit's ``m.new_content`` when the event has been edited (Synapse bundles
    it into ``unsigned.m.relations``), else the event's own content.

    Every reconstruction path (``get_note``, the outbox, the HTML views)
    must use this instead of raw ``content`` -- after ``bridge.edit_bridge``
    federates an edit as an ``Update``, a served copy still built from the
    pre-edit body would re-introduce exactly the served-vs-delivered drift
    the inReplyTo/guest-mention incidents were (see memory/module notes).
    NOT for relation-derived fields: ``m.new_content`` never carries the
    original's ``m.relates_to``, so ``derive_in_reply_to`` must keep using
    the event's OWN content.
    """
    replacement = ((matrix_event.get("unsigned") or {}).get("m.relations") or {}).get("m.replace")
    if isinstance(replacement, dict):
        new_content = (replacement.get("content") or {}).get("m.new_content")
        if isinstance(new_content, dict):
            return new_content
    return matrix_event.get("content") or {}


async def _guest_post_owner_mention(request: Request, federated) -> tuple[str, str] | None:
    """``(owner_handle, owner_actor_id)`` when ``federated`` is a GUEST
    post -- authored by a different local actor than the Profile Room it
    was posted in belongs to (see ``bridge.profile_posts``) -- else None.

    The delivered ``Create`` had the owner's mention TACKED onto the front
    of its content: synthetic, so it's not in the Matrix event's own body,
    and every path that reconstructs the post from that body must re-tack
    it itself. Confirmed live (2026-07-04): a reply to a guest post made
    the other server re-fetch the note, and the mention-less reconstruction
    replaced its good delivered copy -- leading @mention gone remotely.
    Shared by ``get_note`` (the AP JSON other servers fetch) and
    ``_build_post_view`` (every HTML rendering) so the two can't drift.
    """
    config = request.app.state.config
    base = config.bridge.public_base_url
    room_owner = await request.app.state.repository.get_local_actor_by_room_id(federated.room_id)
    if room_owner is None:
        return None
    owner_actor_id = actor_url(base, room_owner.username)
    if owner_actor_id == federated.author_actor_id:
        return None
    return f"@{room_owner.username}@{config.bridge.domain}", owner_actor_id


_HTML_MEDIA_TYPES = ("text/html", "application/xhtml+xml")
_AP_MEDIA_TYPES = ("application/activity+json", "application/ld+json", "application/json")


def _prefers_html(request: Request) -> bool:
    """Whether ``request`` -- reaching one of the actor/note IRIs a remote
    fediverse client links to as a human-facing "View profile"/permalink --
    is a plain browser rather than an AP-speaking client dereferencing the
    same URL for its JSON.

    Walks the ``Accept`` header in order (ignoring q-values, which is good
    enough in practice: a browser always lists ``text/html`` before any
    JSON type it also accepts, and every AP implementation observed sends
    an explicit ``application/activity+json``/``application/ld+json``,
    never bare ``text/html``) and returns on the first decisive entry.
    Defaults to HTML for a missing/wildcard-only header, since a real AP
    fetcher reliably states its own preference explicitly -- see e.g.
    Mastodon's/Pleroma's own webfinger and actor documents, which do the
    same -- while a bare ``curl`` or browser navigation typically doesn't.
    """
    accept = request.headers.get("accept", "")
    if not accept:
        return True
    for part in accept.split(","):
        media_type = part.split(";", 1)[0].strip().lower()
        if media_type in _HTML_MEDIA_TYPES or media_type == "*/*":
            return True
        if media_type in _AP_MEDIA_TYPES:
            return False
    return True


async def _resolve_post_author(request: Request, actor_id: str) -> tuple[str, str, str, str | None, str | None]:
    """``(display_name, display_name_html, handle, avatar_url, profile_url)``
    for rendering a post card's byline -- covers a local actor's own post as
    well as one mirrored from a remote account (via its synced
    ``GhostProfile``), same lookup ``bridge.note_mirroring.actor_html_with_avatar``
    does for a Matrix pill, just returning a plain avatar URL instead of
    Matrix-pill HTML since this renders a public web page, not a Matrix
    message.

    ``display_name`` is always plain text (used for the page ``<title>`` and
    the no-avatar fallback initial -- see ``bridge.web_views``); ``display_name_html``
    is that same name pre-escaped with any custom-emoji ``:shortcode:`` (see
    ``bridge.custom_emoji``, resolved+persisted at ghost-sync time since the
    original AP ``tag`` data isn't kept around here) inlined as an ``<img>``,
    for ``_byline_html`` to use directly instead of re-escaping ``display_name``.

    ``profile_url`` is this bridge's own ``/actor/{username}`` page for a
    LOCAL actor, or the remote account's own ``actor_id`` (its real
    ActivityPub actor IRI) for a remote one -- NOT a guessed
    ``https://{domain}/@{user}``-shaped URL: there is no profile-page
    convention followed by every fediverse implementation (confirmed
    broken in practice -- e.g. clew.lol serves accounts at ``/users/{user}``,
    not ``/@{user}``, so a guessed link 404s there), whereas ``actor_id`` is
    guaranteed to be a real, dereferenceable URL for that exact account --
    it's how the account is identified at all. Mastodon-compatible software
    (the fediverse's dominant share) content-negotiates a proper HTML
    profile page at that same URL for a browser visiting it directly, same
    as this bridge's own actor pages now do; software that doesn't will
    just show that visitor raw JSON instead of a styled page, which is
    still strictly better than a dead link."""
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    base = config.bridge.public_base_url

    username = username_from_actor_url(base, actor_id)
    if username is not None:
        record = await repository.get_local_actor(username)
        if record is not None:
            display_name = record.display_name or record.username
            return (
                display_name,
                html.escape(display_name),
                f"@{record.username}@{config.bridge.domain}",
                record.icon_url,
                actor_url(base, record.username),
            )
        return actor_id, html.escape(actor_id), actor_id, None, None

    profile = await repository.get_ghost_profile(actor_id)
    if profile is not None:
        handle = profile.handle or actor_id
        display_name = profile.display_name or handle
        display_name_html = html.escape(display_name)
        resolved_emoji = await repository.get_resolved_emoji(actor_id)
        if resolved_emoji:
            # Public /media/ proxy URL, not the raw mxc:// -- same reasoning
            # as _build_post_view's identical conversion.
            resolved_urls = {shortcode: media_url(base, mxc) for shortcode, mxc in resolved_emoji.items()}
            display_name_html = apply_resolved_emoji(display_name_html, resolved_urls)
        return display_name, display_name_html, handle, profile.icon_url, actor_id

    domain = urlsplit(actor_id).hostname or ""
    localpart = actor_id.rstrip("/").rsplit("/", 1)[-1]
    handle = f"@{localpart}@{domain}" if domain else localpart
    return handle, html.escape(handle), handle, None, actor_id


async def _resolve_person_ref(request: Request, actor_id: str) -> PersonRef:
    """``PersonRef`` for ``actor_id`` -- just ``_resolve_post_author``'s same
    (local-actor-or-``GhostProfile``) resolution repackaged into the shape
    ``bridge.web_views``'s dependency-free renderer needs for a followers/
    following or reactor list entry, handle included (shown in small dim
    text under the display name -- see ``PersonRef.handle``)."""
    display_name, display_name_html, handle, avatar_url, profile_url = await _resolve_post_author(request, actor_id)
    return PersonRef(
        display_name=display_name,
        display_name_html=display_name_html,
        avatar_url=avatar_url,
        profile_url=profile_url,
        handle=handle,
    )


async def _resolve_reactor(request: Request, mxid: str) -> PersonRef:
    """``PersonRef`` for whoever's Matrix ``mxid`` sent a reaction -- a
    ghost's own fediverse identity, a local linked actor's own, or (a real
    Matrix user in the room who's neither) their plain Matrix profile with
    no fediverse link at all. Memoized per-request (``request.state``): the
    same follower/author routinely reacts to several posts on one profile
    page render, and this is otherwise a repository/Synapse round trip per
    reaction."""
    cache = getattr(request.state, "reactor_cache", None)
    if cache is None:
        cache = {}
        request.state.reactor_cache = cache
    if mxid in cache:
        return cache[mxid]

    repository: ActorRepository = request.app.state.repository
    base = request.app.state.config.bridge.public_base_url
    local_actor = await repository.get_local_actor_by_matrix_id(mxid)
    if local_actor is not None:
        person = await _resolve_person_ref(request, actor_url(base, local_actor.username))
    else:
        profile = await repository.get_ghost_profile_by_mxid(mxid)
        if profile is not None:
            person = await _resolve_person_ref(request, profile.actor_id)
        else:
            try:
                matrix_profile = await request.app.state.synapse.get_profile(mxid)
            except SynapseError:
                matrix_profile = {}
            name = matrix_profile.get("displayname") or mxid
            # A plain Matrix user's "@ id" is their MXID -- still worth
            # showing under the name (the renderer suppresses it when the
            # display name already IS the mxid).
            person = PersonRef(
                display_name=name, display_name_html=html.escape(name), avatar_url=None, profile_url=None, handle=mxid
            )
    cache[mxid] = person
    return person


_MATRIX_PILL_RE = re.compile(r'<a href="https://matrix\.to/#/([^"]+)">(.*?)</a>', re.DOTALL)
_INNER_TAG_RE = re.compile(r"<[^>]+>")


def _extract_pill_mentions(formatted_body: str) -> list[tuple[str, str]]:
    """``[(mxid, visible_text), ...]`` for each Matrix pill anchor in
    ``formatted_body`` -- both a local post's own mentions (see
    ``bridge.mentions.resolve_pill_mentions``, which rewrites the mentioned
    account's name/mxid to their handle in ``body`` and separately pills it
    in ``formatted_body``) and an inbound mirrored reply's (see
    ``bridge.activitypub.sanitize._SanitizingParser``, which pills a
    recognized ``@mention`` anchor using the exact same visible TEXT it
    keeps in the plain-text version) use this exact same convention, so one
    extraction covers both. Safe to parse with a plain regex rather than a
    real HTML parser -- this is always our OWN previously-sanitized/
    previously-built output, never untrusted markup being parsed for the
    first time here.

    A pill's inner HTML routinely isn't flat text -- e.g. a mirrored
    Mastodon-style mention anchor keeps its original ``@<span>q</span>``
    structure (``span`` is on ``bridge.activitypub.sanitize``'s own
    allow-list), which is nested markup, not the plain ``"@q"`` string
    that's actually in the plain ``body`` this gets matched against. Inner
    tags are stripped here for exactly that reason -- matching on the same
    flattened text the plain-text side already uses."""
    return [
        (mxid, _INNER_TAG_RE.sub("", inner_html))
        for mxid, inner_html in _MATRIX_PILL_RE.findall(formatted_body)
    ]


async def _resolve_mention_target_url(request: Request, mxid: str) -> str | None:
    """Where a Matrix pill's ``mxid`` should link to on the public web page
    -- this bridge's own actor page for a local user, or a ghost's own real
    ``actor_id`` (see ``_resolve_post_author``'s docstring for why that's
    preferred over a guessed profile-page URL) for a remote one. None if
    ``mxid`` resolves to neither (shouldn't normally happen for a pill this
    bridge itself generated, but left unlinked rather than guessing further
    if it somehow doesn't)."""
    repository: ActorRepository = request.app.state.repository
    local_actor = await repository.get_local_actor_by_matrix_id(mxid)
    if local_actor is not None:
        base = request.app.state.config.bridge.public_base_url
        return actor_url(base, local_actor.username)
    profile = await repository.get_ghost_profile_by_mxid(mxid)
    if profile is not None:
        return profile.actor_id
    return None


async def _linkify_post_mentions(request: Request, content_html: str, event_content: dict) -> str:
    """Turn each ``@mention`` already visible in ``content_html`` (built
    from the Matrix event's plain ``body`` -- see ``_reconstruct_note_body``,
    which never carries mention links of its own) into a link to that
    account's profile page, driven by the SAME event's ``formatted_body``
    Matrix pills rather than re-parsing the mention text itself -- the
    pill's own mxid is an unambiguous, already-resolved reference to who's
    actually mentioned, which a plain-text guess (e.g. a bare ``@q`` with no
    ``@domain`` at all, as an inbound mirrored reply's visible mention text
    routinely is -- see ``_SanitizingParser``) could never reliably be."""
    formatted_body = event_content.get("formatted_body")
    if not isinstance(formatted_body, str) or not formatted_body:
        return content_html
    mention_links: dict[str, str] = {}
    for mxid, visible_text in _extract_pill_mentions(formatted_body):
        if visible_text in mention_links:
            continue
        target_url = await _resolve_mention_target_url(request, mxid)
        if target_url:
            mention_links[visible_text] = target_url
    if not mention_links:
        return content_html
    for visible_text, href in mention_links.items():
        escaped_text = html.escape(visible_text)
        if escaped_text not in content_html:
            continue
        anchor = f'<a href="{html.escape(href, quote=True)}">{escaped_text}</a>'
        content_html = content_html.replace(escaped_text, anchor)
    return content_html


async def _is_private_room(repository: ActorRepository, room_id: str) -> bool:
    """Whether ``room_id`` is a private 1:1 room (a ghost DM or Chat) that
    should never be servable via any PUBLIC post-rendering route -- see
    ``get_note``'s own docstring for the real leak this closed."""
    return await repository.is_ghost_dm_room(room_id) or await repository.is_ghost_chat_room(room_id)


async def _build_post_view(
    request: Request, federated: FederatedEvent, *, matrix_event: dict | None = None
) -> "PostView | None":
    """Resolve a ``FederatedEvent`` into a ``PostView`` for the static HTML
    renderer -- reconstructing the same Note content ``get_outbox``/
    ``get_note`` do (see ``_reconstruct_note_body``), plus the author's
    byline and a summary of whatever ``m.reaction``s Matrix already holds
    on this event. ``matrix_event``, if already fetched by the caller (the
    profile timeline already has it from its own room-history call), is
    reused rather than fetched again. Returns None if the underlying
    Matrix event can no longer be fetched (e.g. redacted), turned out to
    carry no post content at all, or -- defense in depth alongside
    ``get_note``'s own identical check, since this is the shared chokepoint
    every HTML rendering path (profile page, thread page, ``get_note``'s
    own HTML branch) goes through -- lives in a private DM/Chat room."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    repository: ActorRepository = request.app.state.repository
    if await _is_private_room(repository, federated.room_id):
        return None

    if matrix_event is None:
        try:
            matrix_event = await request.app.state.synapse.get_event(
                federated.room_id, federated.event_id, as_user_id=bot_mxid
            )
        except SynapseError:
            return None

    event_content = _effective_event_content(matrix_event)
    attachment = build_ap_attachment(base, event_content)
    body, content_html, _quote_uri = _reconstruct_note_body(federated, event_content, attachment)
    # Guest posts re-tack the room owner's mention here too -- see
    # _guest_post_owner_mention; without this, the HTML views show the
    # stripped content even after get_note's JSON was fixed.
    guest_mention = await _guest_post_owner_mention(request, federated)
    if guest_mention is not None:
        owner_handle, owner_actor_id = guest_mention
        if owner_handle not in body:
            body = f"{owner_handle} {body}".strip()
            content_html = plain_text_to_note_html(body, {owner_handle: owner_actor_id})
    if not body and attachment is None:
        return None
    content_html = await _linkify_post_mentions(request, content_html, event_content)
    # HTML-rendering-only, like the video-thumbnail step just below -- this
    # PostView is never reused for real AP JSON output (see get_outbox/
    # get_note, which call _reconstruct_note_body directly instead), so
    # inlining custom-emoji images here can't leak into content actually
    # federated back out to other instances.
    resolved_emoji = await request.app.state.repository.get_resolved_emoji(federated.ap_object_id)
    if resolved_emoji:
        # Public /media/ proxy URL, not the raw mxc:// stored in the table --
        # a plain HTTP/browser caller (unlike a Matrix client, which resolves
        # mxc:// via its own authenticated media API) can't do anything with
        # that scheme at all.
        resolved_urls = {shortcode: media_url(base, mxc) for shortcode, mxc in resolved_emoji.items()}
        content_html = apply_resolved_emoji(content_html, resolved_urls)
    if attachment is not None and attachment.get("type") == "Video":
        # "thumbnail_url" is added here for HTML rendering only -- not part
        # of build_ap_attachment's own AP-facing contract (an ActivityStreams
        # Document/Link has no such property, and this dict is never reused
        # for real AP JSON output here -- only _build_post_view's own
        # PostView, unlike get_outbox/get_note which build their own
        # attachment dict independently). Without a poster, the <video>
        # element renders blank until playback starts; Matrix already
        # generates this thumbnail for every video upload, so there's no
        # reason not to use it. Same publish-allowlist step the main
        # attachment's own mxc already went through at post-time (see
        # bridge.profile_posts) -- the thumbnail's mxc is a DIFFERENT URI
        # never marked published anywhere else, so the public media proxy
        # would otherwise correctly refuse to serve it.
        thumbnail_mxc = (event_content.get("info") or {}).get("thumbnail_url")
        if isinstance(thumbnail_mxc, str) and thumbnail_mxc.startswith("mxc://"):
            repository: ActorRepository = request.app.state.repository
            await repository.mark_media_published(thumbnail_mxc)
            attachment = {**attachment, "thumbnail_url": media_url(base, thumbnail_mxc)}

    display_name, display_name_html, handle, avatar_url, profile_url = await _resolve_post_author(
        request, federated.author_actor_id
    )

    try:
        reaction_events = await request.app.state.synapse.get_relations(
            federated.room_id, federated.event_id, rel_type="m.annotation", event_type="m.reaction", as_user_id=bot_mxid
        )
    except SynapseError:
        reaction_events = []

    # Batched (not one query per reaction) lookup of which of these reaction
    # events carried a custom emoji -- see ActorRepository.get_custom_emoji_
    # by_reaction_event_ids and bridge.web_views.summarize_reactions. Public
    # /media/ proxy URL here (not the raw mxc:// -- unlike the notification's
    # inline image, this page is served to anonymous HTTP/AP callers with no
    # Matrix auth); already published at reaction-time by
    # bridge.custom_emoji.resolve_custom_emoji_image, so nothing to mark here.
    reaction_event_ids = [e["event_id"] for e in reaction_events if e.get("event_id")]
    repository: ActorRepository = request.app.state.repository
    custom_emoji_mxc_by_event = await repository.get_custom_emoji_by_reaction_event_ids(reaction_event_ids)
    custom_emoji_url_by_event = {
        event_id: media_url(base, mxc) for event_id, mxc in custom_emoji_mxc_by_event.items()
    }

    # Resolves each reaction's sender to a displayable identity (avatar/name/
    # profile link) up front -- bridge.web_views.summarize_reactions is kept
    # dependency-free/pure, so it only ever groups already-resolved people,
    # never does I/O of its own (same split as this function's own
    # display_name/avatar_url/profile_url resolution just above).
    resolved_reaction_events: list[ReactionEvent] = []
    for reaction_event in reaction_events:
        reaction_content = reaction_event.get("content") or {}
        key = (reaction_content.get("m.relates_to") or {}).get("key")
        sender = reaction_event.get("sender")
        if not isinstance(key, str) or not key or not sender:
            continue
        person = await _resolve_reactor(request, sender)
        resolved_reaction_events.append(
            ReactionEvent(key=key, event_id=reaction_event.get("event_id") or "", person=person)
        )

    source_url = event_content.get("external_url") or federated.ap_object_id

    return PostView(
        ap_object_id=federated.ap_object_id,
        source_url=source_url,
        author_display_name=display_name,
        author_display_name_html=display_name_html,
        author_handle=handle,
        author_avatar_url=avatar_url,
        author_profile_url=profile_url,
        origin_server_ts=matrix_event.get("origin_server_ts") or 0,
        content_html=content_html,
        attachment=attachment,
        reactions=summarize_reactions(resolved_reaction_events, custom_emoji_url_by_event),
    )


async def _build_profile_post_views(
    request: Request, record, *, from_token: str | None = None
) -> tuple[list[PostView], str | None]:
    """One page (``PROFILE_PAGE_SIZE`` posts, unless history runs out first)
    of the local actor's own recent posts, newest first -- same source
    (their Profile Room's Matrix history) and same distributed-only filter
    ``get_outbox`` uses, just rendered as ``PostView``s instead of AP JSON.
    Reuses each raw batch's own ``matrix_event`` instead of a redundant
    per-post ``get_event``.

    A raw Matrix ``/messages`` batch is NOT one page here -- reactions,
    other people's thread replies, membership events, etc. share the same
    room and count against a raw batch's own size without ever becoming a
    post, so treating one raw batch as one page produced wildly uneven (and
    sometimes entirely blank) pages on a room with a lot of non-post
    traffic mixed in. Instead this fetches ``PROFILE_FETCH_BATCH_SIZE``-sized
    raw batches in a loop, accumulating actual posts, until either
    ``PROFILE_PAGE_SIZE`` is reached or history genuinely runs out (bounded
    by ``PROFILE_MAX_FETCH_BATCHES`` either way) -- always finishing
    whichever raw batch is in progress rather than cutting it off mid-batch,
    so nothing in it is silently skipped on the next page.

    Returns ``(posts, next_token)`` -- ``next_token`` is Synapse's own
    pagination cursor (wherever the last raw batch actually consumed left
    off) for the caller to build an "Older posts" link from, or None once
    there's nothing further back in the room's history to page into.
    """
    if not record.room_id:
        return [], None
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    posts: list[PostView] = []
    token = from_token
    next_token: str | None = None
    batches_fetched = 0
    while batches_fetched < PROFILE_HARD_FETCH_CAP:
        batches_fetched += 1
        try:
            history = await request.app.state.synapse.get_room_messages(
                record.room_id, limit=PROFILE_FETCH_BATCH_SIZE, direction="b", from_token=token, as_user_id=bot_mxid
            )
        except (SynapseError, httpx.HTTPError):
            logger.warning("Failed to fetch history for %s's profile page", record.username, exc_info=True)
            next_token = None
            break

        chunk = history.get("chunk", [])
        for matrix_event in chunk:
            if matrix_event.get("type") != "m.room.message" or matrix_event.get("sender") != record.matrix_user_id:
                continue
            matrix_event_id = matrix_event.get("event_id")
            if not matrix_event_id:
                continue
            federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
            if federated is None:
                continue
            if federated.thread_root_event_id:
                # A reply -- q replying within their own thread (to their
                # own earlier post, to a remote reply mirrored into this
                # same Profile Room, ...), not a fresh top-level post. Same
                # convention as Twitter/Mastodon's own profile timeline:
                # replies clutter what should be "what this account posted"
                # and are still fully reachable via their own permalink/the
                # thread view (bridge.activitypub.routes._build_thread_post_views,
                # unaffected by this filter) -- just not listed here as if
                # each were its own fresh post.
                continue
            view = await _build_post_view(request, federated, matrix_event=matrix_event)
            if view is not None:
                posts.append(view)

        end_token = history.get("end")
        # This batch was empty, or Synapse's own cursor stopped advancing --
        # both signal "reached the start of the room". Without this, a
        # stale/repeating token would keep this loop (and, across page
        # loads, an "Older posts" link) going forever.
        reached_end = not chunk or not end_token or end_token == token
        token = end_token
        if reached_end:
            next_token = None
            break
        if len(posts) >= PROFILE_PAGE_SIZE:
            next_token = token
            break
        # The soft cap only applies once this page is already non-blank --
        # stopping here while still empty would just hand a blank page
        # (with a "next" link) to the caller, pushing the exact same
        # problem to the next click instead of solving it. Keep going,
        # bounded only by PROFILE_HARD_FETCH_CAP, until at least one real
        # post is found or history truly ends.
        if posts and batches_fetched >= PROFILE_MAX_FETCH_BATCHES:
            next_token = token
            break
    else:
        # Hit the hard cap -- an extreme, likely-pathological room. Still
        # offer whatever cursor was reached rather than silently dropping
        # the rest of its history.
        next_token = token

    return posts, next_token


async def _build_thread_post_views(request: Request, federated: FederatedEvent) -> list[PostView]:
    """Every post in ``federated``'s whole thread (its root, plus every
    reply threaded off that root -- Matrix threads are flat, so a reply's
    own ``m.relates_to`` always names the ROOT, not its immediate parent;
    see ``bridge.note_mirroring.thread_reply_relates_to``), oldest first --
    a Twitter/Soapbox-style conversation view, not just the one post
    requested. Only events this bridge actually tracks as a post (i.e. have
    their own ``FederatedEvent``) are included -- a Profile/Remote User
    Room can in principle hold other Matrix chatter alongside mirrored
    posts, which has no business appearing in a public thread view."""
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    root_event_id = federated.thread_root_event_id or federated.event_id

    try:
        related = await request.app.state.synapse.get_relations(
            federated.room_id, root_event_id, rel_type="m.thread", as_user_id=bot_mxid
        )
    except SynapseError:
        related = []

    event_ids = [root_event_id, *(e.get("event_id") for e in related if e.get("event_id"))]
    seen: set[str] = set()
    ordered_ids = [e for e in event_ids if e not in seen and not seen.add(e)]

    posts: list[PostView] = []
    for event_id in ordered_ids:
        fe = await repository.get_federated_event_by_matrix_event(event_id)
        if fe is None:
            continue
        view = await _build_post_view(request, fe)
        if view is not None:
            posts.append(view)
    posts.sort(key=lambda p: p.origin_server_ts)
    return posts


async def _fetch_room_outbox_notes(
    request: Request, *, room_id: str, base: str, username: str, owner_matrix_id: str, bot_mxid: str
) -> list[tuple[int, Note]]:
    """Notes actually distributed as posts from a single room, each paired
    with its raw Matrix ``origin_server_ts`` so ``get_outbox`` can merge
    several rooms' worth (current Profile Room plus any it replaced -- see
    ``ActorRepository.get_profile_room_history``) into one feed sorted by
    recency, instead of only ever showing whichever room is current."""
    repository: ActorRepository = request.app.state.repository
    try:
        history = await request.app.state.synapse.get_room_messages(
            room_id, limit=OUTBOX_HISTORY_LIMIT, direction="b", as_user_id=bot_mxid
        )
    except (SynapseError, httpx.HTTPError):
        logger.warning("Failed to fetch history for %s's outbox from %s", username, room_id, exc_info=True)
        return []

    notes: list[tuple[int, Note]] = []
    for matrix_event in history.get("chunk", []):
        if matrix_event.get("type") != "m.room.message" or matrix_event.get("sender") != owner_matrix_id:
            continue
        matrix_event_id = matrix_event.get("event_id")
        if not matrix_event_id:
            continue
        # Only events that were actually distributed as a post have a recorded
        # mapping -- reuse its `ap_object_id` so the Note `id` here matches
        # exactly what followers were already delivered.
        federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
        if federated is None:
            continue
        event_content = _effective_event_content(matrix_event)
        attachment = build_ap_attachment(base, event_content)
        body, content_html, quote_uri = _reconstruct_note_body(federated, event_content, attachment)
        if not body and attachment is None:
            continue
        origin_server_ts = matrix_event.get("origin_server_ts") or 0
        note = Note(
            id=federated.ap_object_id,
            attributed_to=actor_url(base, username),
            content=content_html,
            published=_matrix_ts_to_iso(origin_server_ts),
            to=[AS_PUBLIC],
            cc=[followers_url(base, username)],
            # Without this, a reply reconstructed here reads as a standalone
            # post to anyone fetching it -- see derive_in_reply_to.
            in_reply_to=await derive_in_reply_to(repository, event_content),
            attachment=[attachment] if attachment else [],
            quote_uri=quote_uri,
        )
        notes.append((origin_server_ts, note))
    return notes


@router.get("/outbox/{username}")
async def get_outbox(request: Request, username: str) -> JSONResponse:
    config = request.app.state.config
    base = config.bridge.public_base_url
    record = await _get_actor_record(request, username)
    repository: ActorRepository = request.app.state.repository
    dated_notes: list[tuple[int, Note]] = []

    if record.room_id:
        bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
        # profile_room_history already includes the current room (recorded
        # there the moment it became current), but the union guards against
        # the current one somehow missing from it -- see
        # project_profile_room_history_gap in memory for why that's not
        # purely hypothetical.
        room_ids = set(await repository.get_profile_room_history(username))
        room_ids.add(record.room_id)
        results = await asyncio.gather(
            *(
                _fetch_room_outbox_notes(
                    request, room_id=room_id, base=base, username=username,
                    owner_matrix_id=record.matrix_user_id, bot_mxid=bot_mxid,
                )
                for room_id in room_ids
            )
        )
        for room_notes in results:
            dated_notes.extend(room_notes)
        dated_notes.sort(key=lambda pair: pair[0], reverse=True)

    items = [note.to_dict() for _, note in dated_notes]
    collection = OrderedCollection(id=outbox_url(base, username), items=items)
    return _activity_json(collection.to_dict())


@router.get("/actor/{username}/notes/{note_id}")
async def get_note(request: Request, username: str, note_id: str) -> Response:
    """Serve a single distributed post at its own AP object IRI.

    Needed for anything that dereferences a Note by bare ``id`` string
    rather than embedding it inline -- notably an ``Announce`` of one of
    our own local posts, whose ``object`` some implementations (e.g.
    Pleroma/Akkoma) send as just the IRI, not the full Note. Without this
    route that fetch 404s, silently dropping the repost card
    ``bridge.inbox_dispatch._handle_announce`` would otherwise build in the
    booster's own Remote User Room (the DM notification to the post's
    owner still fires regardless, since that only needs our own
    ``FederatedEvent`` bookkeeping, not a live fetch of the object) --
    confirmed missing this route by that exact symptom.

    Looks the post up the same way ``get_outbox`` populates each of its own
    entries: reconstructs the same ``ap_object_id`` this route was reached
    at and re-derives the Note from whichever Matrix event actually mirrors
    it (see ``_reconstruct_note_body`` -- a ``;repost``'s own echo needs
    special handling here, since its Matrix-facing text isn't the same as
    what actually went out over ActivityPub for it), so this can never
    drift from what the outbox or original delivery already sent."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    await _get_actor_record(request, username)
    repository: ActorRepository = request.app.state.repository

    ap_object_id = f"{base}/actor/{username}/notes/{note_id}"
    federated = await repository.get_federated_event_by_ap_object(ap_object_id)
    if federated is None or federated.author_actor_id != actor_url(base, username):
        raise HTTPException(status_code=404, detail="Not Found")
    if await _is_private_room(repository, federated.room_id):
        # A DM/Chat's own outbound Note reuses this exact public note-IRI
        # scheme (bridge.reply_bridge._send_outbound_dm/note_mirroring.
        # mirror_chat_message both mint note_id the same way an ordinary
        # public post does) -- it's addressed privately over ActivityPub
        # (never AS_PUBLIC, so a real fediverse server correctly never
        # shows it on the sender's public timeline), but this route had no
        # equivalent check of its own and would happily serve the exact
        # same private content to anyone with the URL. Confirmed live
        # (2026-07-03) as a real leak, not just a hypothetical.
        raise HTTPException(status_code=404, detail="Not Found")

    if _prefers_html(request):
        posts = await _build_thread_post_views(request, federated)
        total_pages = max(1, (len(posts) + THREAD_PAGE_SIZE - 1) // THREAD_PAGE_SIZE)
        # Defaults to whichever page the actually-requested post falls on --
        # not always page 1 -- so a permalink to a specific reply deep in a
        # long thread still shows that reply by default; ?page= overrides
        # this for browsing the rest of the thread from there.
        focused_index = next((i for i, p in enumerate(posts) if p.ap_object_id == ap_object_id), 0)
        default_page = (focused_index // THREAD_PAGE_SIZE) + 1
        try:
            page = int(request.query_params.get("page", default_page))
        except ValueError:
            page = default_page
        page = min(max(page, 1), total_pages)

        start = (page - 1) * THREAD_PAGE_SIZE
        page_posts = posts[start : start + THREAD_PAGE_SIZE]
        prev_url = f"/actor/{username}/notes/{note_id}?page={page - 1}" if page > 1 else None
        next_url = f"/actor/{username}/notes/{note_id}?page={page + 1}" if page < total_pages else None

        html_doc = render_thread_page(
            posts=page_posts,
            focused_ap_object_id=ap_object_id,
            page=page,
            total_pages=total_pages,
            prev_url=prev_url,
            next_url=next_url,
        )
        return HTMLResponse(html_doc)

    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        matrix_event = await request.app.state.synapse.get_event(
            federated.room_id, federated.event_id, as_user_id=bot_mxid
        )
    except SynapseError:
        raise HTTPException(status_code=404, detail="Not Found")

    # Rendered fields come from the latest edit when there is one; the
    # inReplyTo derivation below deliberately keeps the RAW content (see
    # _effective_event_content's docstring).
    raw_event_content = matrix_event.get("content") or {}
    event_content = _effective_event_content(matrix_event)
    attachment = build_ap_attachment(base, event_content)
    _body, content_html, quote_uri = _reconstruct_note_body(federated, event_content, attachment)

    note_tags: list[dict] = []
    guest_mention = await _guest_post_owner_mention(request, federated)
    if guest_mention is not None:
        owner_handle, owner_actor_id = guest_mention
        tacked_body = _body if owner_handle in _body else f"{owner_handle} {_body}".strip()
        if tacked_body:
            content_html = plain_text_to_note_html(tacked_body, {owner_handle: owner_actor_id})
        note_tags.append({"type": "Mention", "href": owner_actor_id, "name": owner_handle})

    note = Note(
        id=ap_object_id,
        attributed_to=actor_url(base, username),
        content=content_html,
        published=_matrix_ts_to_iso(matrix_event.get("origin_server_ts")),
        to=[AS_PUBLIC],
        cc=[followers_url(base, username)],
        # Without this, a fetched copy of a reply reads as a standalone
        # post -- the delivered Create had inReplyTo but this
        # reconstruction didn't. See derive_in_reply_to.
        in_reply_to=await derive_in_reply_to(repository, raw_event_content),
        attachment=[attachment] if attachment else [],
        tag=note_tags,
        quote_uri=quote_uri,
    )
    # Note.to_dict() has no @context of its own -- normally fine, since a
    # Note is always embedded inside something that provides one (an
    # Activity, or this same outbox's own OrderedCollection), but THIS
    # route serves it as its own top-level document with nothing wrapping
    # it. Without @context, extension terms like quoteUri/_misskey_quote
    # aren't valid JSON-LD at all to a strict processor -- just unresolved,
    # ignorable keys -- which is exactly what was happening here: Misskey
    # fetching this same Note fresh (e.g. to resolve _misskey_quote's own
    # target) never saw quote data at all, even though the originally
    # delivered Create (which DOES carry @context, inherited by its
    # embedded object) had it right from the start.
    return _activity_json({"@context": JSON_LD_CONTEXT, **note.to_dict()})


def _collection_or_page(request: Request, collection: OrderedCollection) -> JSONResponse:
    """Serves ``collection`` itself (with its member IRIs embedded as
    ``first``), unless a ``?page=`` query param is present -- in which case
    serves that same ``OrderedCollectionPage`` standalone instead, needing
    its own ``@context`` since nothing then wraps it (same reasoning as
    ``get_note``'s identical addition). Every real collection this bridge
    produces fits on one page, so any ``page`` value gets that same one
    page -- see ``OrderedCollection``'s own docstring for why serving this
    at all (not just embedding it) matters: some remote implementations
    re-fetch ``first.id`` rather than trust what's embedded there."""
    if "page" in request.query_params:
        return _activity_json({"@context": JSON_LD_CONTEXT, **collection.first_page_dict()})
    return _activity_json(collection.to_dict())


@router.get("/followers/{username}")
async def get_followers(request: Request, username: str) -> JSONResponse:
    base = request.app.state.config.bridge.public_base_url
    record = await _get_actor_record(request, username)
    repository: ActorRepository = request.app.state.repository
    followers = await repository.list_followers(username)
    # Owner hid the list (see bridge.commands's `hide followers`) -- the
    # count stays real and public, only the member list is withheld.
    items = [] if record.hide_followers else followers
    collection = OrderedCollection(id=followers_url(base, username), items=items, total_items=len(followers))
    return _collection_or_page(request, collection)


@router.get("/following/{username}")
async def get_following(request: Request, username: str) -> JSONResponse:
    base = request.app.state.config.bridge.public_base_url
    record = await _get_actor_record(request, username)
    repository: ActorRepository = request.app.state.repository
    following = await repository.list_following(username)
    items = [] if record.hide_following else following
    collection = OrderedCollection(id=following_url(base, username), items=items, total_items=len(following))
    return _collection_or_page(request, collection)


async def _verify_and_parse_activity(request: Request) -> Activity:
    body = await request.body()
    config = request.app.state.config

    try:
        key_id = await verify_incoming_request(
            method=request.method,
            path=request.url.path,
            headers=dict(request.headers),
            body=body,
            resolve_public_key=request.app.state.key_cache.get,
            max_clock_skew=config.federation.max_clock_skew,
        )
    except SignatureError as exc:
        logger.warning("Inbox signature verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=f"Invalid HTTP signature: {exc}") from exc

    try:
        payload = await request.json()
        activity = Activity.from_dict(payload)
    except Exception as exc:
        # Logged, not just 400'd: the sender sees the rejection but WE
        # otherwise wouldn't -- a delivery that dies here is invisible in
        # the journal, which made a vanished inbound reply (2026-07-04)
        # undiagnosable after the fact. Same reasoning for the keyId
        # mismatch below.
        logger.warning("Rejecting inbox delivery with malformed activity JSON: %s", exc)
        raise HTTPException(status_code=400, detail=f"Malformed activity JSON: {exc}") from exc

    if activity.actor != key_id.split("#", 1)[0]:
        logger.warning(
            "Rejecting inbox delivery: signature keyId %s does not correspond to activity actor %s",
            key_id, activity.actor,
        )
        raise HTTPException(
            status_code=401,
            detail="Signature keyId does not correspond to the activity's actor",
        )
    return activity


@router.post("/inbox/{username}")
async def post_inbox(request: Request, username: str) -> Response:
    await _get_actor_record(request, username)
    activity = await _verify_and_parse_activity(request)
    # One INFO line per verified delivery -- the receive-side counterpart
    # of delivery logging. Successful processing is otherwise completely
    # silent, which made "did it never arrive, or did we silently drop
    # it?" unanswerable from the journal (2026-07-04, a vanished reply).
    logger.info("Inbox %s: %s from %s (object %s)", username, activity.type, activity.actor, activity.object_id())

    try:
        await handle_activity(request, username, activity)
    except Exception:
        logger.exception("Error handling %s activity from %s for %s", activity.type, activity.actor, username)

    return Response(status_code=202)


def _resolve_shared_inbox_target(activity: Activity, base: str) -> str | None:
    """Which local actor (by username) a shared-inbox-delivered activity is
    addressed to, for the types where that matters (Follow/Accept/Reject/
    Undo/Block -- everything else routes purely off ``activity.actor`` and
    doesn't need a target username at all, see ``bridge.inbox_dispatch``)."""
    target: Any = None
    if activity.type in ("Follow", "Block"):
        # Both name the targeted local actor directly as their object.
        target = activity.object_id()
    elif activity.type in ("Accept", "Reject"):
        inner = activity.object
        target = inner.get("actor") if isinstance(inner, dict) else None
    elif activity.type == "Undo":
        inner = activity.object
        if isinstance(inner, dict) and inner.get("type") == "Follow":
            target = inner.get("object")
            if isinstance(target, dict):
                target = target.get("id")
    if not isinstance(target, str):
        return None
    return username_from_actor_url(base, target)


@router.post("/inbox")
async def post_shared_inbox(request: Request) -> Response:
    """Shared inbox, for senders that deliver here instead of a per-actor inbox
    (e.g. via a guessed ``{origin}/inbox`` convention, which some ActivityPub
    implementations use even when we advertise ``endpoints.sharedInbox``
    explicitly). The target local actor is resolved from the activity body
    itself rather than the URL."""
    activity = await _verify_and_parse_activity(request)
    base = request.app.state.config.bridge.public_base_url
    username = _resolve_shared_inbox_target(activity, base) or ""
    # See post_inbox's identical line.
    logger.info("Shared inbox: %s from %s (object %s)", activity.type, activity.actor, activity.object_id())

    try:
        await handle_activity(request, username, activity)
    except Exception:
        logger.exception("Error handling shared-inbox %s activity from %s", activity.type, activity.actor)

    return Response(status_code=202)


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _apply_range(content: bytes, range_header: str | None) -> tuple[bytes, int, dict[str, str]]:
    """Honor a single-range ``Range`` header (RFC 7233) against in-memory ``content``.

    Returns ``(body, status_code, extra_headers)``. Needed for video/audio to be
    seekable in remote clients -- without 206 support, many players either refuse
    to play at all or can't scrub. Multi-range requests (rare for media playback)
    fall back to returning the full body with a 200.
    """
    total = len(content)
    if not range_header:
        return content, 200, {"Accept-Ranges": "bytes"}

    match = _RANGE_RE.fullmatch(range_header.strip())
    if not match or "," in range_header:
        return content, 200, {"Accept-Ranges": "bytes"}

    start_s, end_s = match.groups()
    if start_s == "" and end_s == "":
        return content, 200, {"Accept-Ranges": "bytes"}
    if start_s == "":
        # Suffix range (e.g. "bytes=-500" -> last 500 bytes).
        length = int(end_s)
        start, end = max(total - length, 0), total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s != "" else total - 1

    if total == 0 or start >= total or start > end:
        return b"", 416, {"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"}

    end = min(end, total - 1)
    return (
        content[start : end + 1],
        206,
        {"Content-Range": f"bytes {start}-{end}/{total}", "Accept-Ranges": "bytes"},
    )


@router.api_route("/media/{server_name}/{media_id}", methods=["GET", "HEAD"])
async def get_media(request: Request, server_name: str, media_id: str) -> Response:
    """Public, unauthenticated proxy for Matrix media -- see module docstring.

    Refuses anything not on ``ActorRepository``'s published-media allowlist, even
    if it genuinely exists in Synapse: without that check this would be an open,
    unauthenticated gateway to every room's media on the homeserver, not just the
    avatars/attachments the bridge has actually published to the fediverse. This
    is the sole authorization boundary -- ``server_name`` is deliberately *not*
    restricted to our own homeserver, since an mxc:// URI is namespaced by
    whichever homeserver originally hosted the media, not the room it's used
    in (e.g. media forwarded/copied from a room on another homeserver keeps
    its original mxc:// URI), and Synapse's own media download API already
    natively fetches other homeservers' media over federation on our behalf.

    Explicitly handles HEAD (FastAPI does not derive it from a GET route
    automatically) -- some AP implementations probe with HEAD before fetching.
    """
    repository: ActorRepository = request.app.state.repository
    if not await repository.is_media_published(f"mxc://{server_name}/{media_id}"):
        raise HTTPException(status_code=404, detail="Media not found")

    try:
        result = await request.app.state.synapse.download_media(server_name, media_id)
    except SynapseError as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc

    body, status_code, range_headers = _apply_range(result.content, request.headers.get("range"))

    headers = {
        # A given mxc:// ID's content is immutable in Matrix (edits/re-uploads get a
        # new ID), so this is safe to cache aggressively.
        "Cache-Control": "public, max-age=86400, immutable",
        "Access-Control-Allow-Origin": "*",
        "Content-Length": str(len(body)),
        **range_headers,
    }
    if result.content_disposition:
        headers["Content-Disposition"] = result.content_disposition

    response_body = b"" if request.method == "HEAD" else body
    return Response(content=response_body, media_type=result.content_type, status_code=status_code, headers=headers)
