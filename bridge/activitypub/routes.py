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
    VIDEO_JSON_LD_CONTEXT,
    Activity,
    Actor,
    Note,
    OrderedCollection,
    PublicKey,
)
from bridge.activitypub.instance_api import build_mastodon_instance_document
from bridge.activitypub.nodeinfo import (
    NODEINFO_SCHEMA_VERSIONS,
    build_nodeinfo_discovery_document,
    build_nodeinfo_document,
)
from bridge.activitypub.sanitize import plain_text_to_note_html
from bridge.activitypub.signatures import SignatureError, verify_incoming_request
from bridge.custom_emoji import apply_resolved_emoji
from bridge.identity_settings import get_effective_setting
from bridge.note_mirroring import (
    SOCIAL_REL_TYPE_REPLY,
    SOCIAL_RELATES_TO_FIELD,
    build_repost_note_content,
    build_sibling_reply_note_content,
    resolve_cross_bridge_handle,
    split_repost_caption,
)
from bridge.activitypub.urls import (
    actor_url,
    featured_url,
    followers_url,
    following_url,
    inbox_url,
    main_key_id,
    media_url,
    moderators_url,
    outbox_url,
    shared_inbox_url,
    username_from_actor_url,
)
from bridge.activitypub.webfinger import build_local_webfinger_document, parse_acct
from bridge.commands import _effective_third_party_mode, is_foreign_bridge_user
from bridge.inbox_dispatch import handle_activity
from bridge.media import media_caption, resolve_reconstructed_attachment
from bridge.mentions import reconstruct_note_mentions
from bridge.peertube import (
    VIDEO_METADATA_STATE_TYPE,
    VIDEO_VIEWS_STATE_TYPE,
    get_video_index,
    reconstruct_video_activities,
    record_video_view,
    resolve_category,
    resolve_licence,
    resolve_language,
    resolve_media_type,
)
from bridge.reply_bridge import derive_in_reply_to
from bridge.repository import (
    ActorRecord,
    ActorRepository,
    FederatedEvent,
    GroupRecord,
    LemmyCommunityRecord,
    PeerTubeChannelRecord,
)
from bridge.synapse_client import SynapseError
from bridge.web_views import (
    PersonRef,
    PostView,
    ReactionEvent,
    VideoCardView,
    VideoView,
    render_channel_page,
    render_profile_page,
    render_profile_rss,
    render_thread_page,
    render_video_page,
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


async def _get_actor_or_channel_record(
    request: Request, username: str
) -> ActorRecord | PeerTubeChannelRecord | LemmyCommunityRecord | GroupRecord:
    """Same as ``_get_actor_record``, but also resolves a PeerTube channel,
    a hosted Lemmy community, or a hosted Group username (see
    ``PeerTubeChannelRecord``'s/``LemmyCommunityRecord``'s/``GroupRecord``'s
    own docstrings on why all four share one global namespace) -- for
    routes that don't need to build a full Actor document, like
    ``get_followers``/``get_following``/``post_inbox``, and so work
    identically for any kind of local actor."""
    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is not None:
        return record
    channel = await repository.get_peertube_channel(username)
    if channel is not None:
        return channel
    community = await repository.get_lemmy_community(username)
    if community is not None:
        return community
    group = await repository.get_group(username)
    if group is not None:
        return group
    raise HTTPException(status_code=404, detail=f"No such actor: {username}")


async def _build_actor(request: Request, record) -> Actor:
    """Build the served Actor document for ``record``.

    A Follow-Only third-party identity (see ``_effective_third_party_mode``)
    is deliberately never self-controlled -- its served name/avatar/bio
    always live-mirror its CURRENT Matrix profile (fetched fresh here, not
    the possibly-stale ``record.display_name``/``icon_url`` columns the
    periodic sync timer only refreshes every so often) rather than anything
    it or a past Full period might have set, and it never advertises Chat
    (independently, ``;chat`` is already blocked outbound -- see
    ``bridge.commands``'s ``_FOLLOW_ONLY_BLOCKED_ANY_ARG``). A local user
    (or a Full-mode third party with a real Profile Room) is unchanged from
    before this feature existed: everything read straight from the stored
    ``ActorRecord``."""
    base = request.app.state.config.bridge.public_base_url
    mode = await _effective_third_party_mode(request, record)
    if mode == "follow_only":
        synapse = request.app.state.synapse
        try:
            profile = await synapse.get_profile(record.matrix_user_id)
        except SynapseError:
            profile = {}
        display_name = profile.get("displayname") or record.display_name or record.username
        matrix_avatar_mxc = profile.get("avatar_url")
        icon_url = None
        if matrix_avatar_mxc:
            try:
                icon_url = media_url(base, matrix_avatar_mxc)
            except ValueError:
                icon_url = None
        summary = f"{record.matrix_user_id} is a Matrix user."
        banner_url = None
        accepts_chat_messages = False
    else:
        display_name = record.display_name or record.username
        icon_url = record.icon_url
        # AS2's summary is conventionally HTML, same as a Note's content --
        # sending it as bare plaintext (record.summary itself, straight
        # from the Profile Room's m.room.topic) means a URL in someone's
        # bio has no <a href> anchor at all, so remote software renders it
        # as inert text instead of a clickable link (confirmed live
        # 2026-07-11). plain_text_to_note_html is the same escape/autolink/
        # paragraph-wrap helper already used for every mirrored post's own
        # content, for exactly this reason.
        summary = plain_text_to_note_html(record.summary) if record.summary else record.summary
        banner_url = record.banner_url
        # Advertises the "Chat" option (see bridge.chat_bridge) on software
        # that checks for it -- every local actor accepts them, same as
        # every local actor already implicitly accepts ordinary DMs.
        accepts_chat_messages = True
    repository: ActorRepository = request.app.state.repository
    discoverable = await get_effective_setting(repository, record.username, "profile", "discoverable")
    return Actor(
        id=actor_url(base, record.username),
        preferred_username=record.username,
        name=display_name,
        summary=summary,
        url=actor_url(base, record.username),
        inbox=inbox_url(base, record.username),
        outbox=outbox_url(base, record.username),
        followers=followers_url(base, record.username),
        following=following_url(base, record.username),
        featured_url=featured_url(base, record.username),
        discoverable=discoverable,
        icon_url=icon_url,
        icon_media_type=await resolve_media_type(request, icon_url),
        image_url=banner_url,
        image_media_type=await resolve_media_type(request, banner_url),
        shared_inbox=shared_inbox_url(base),
        accepts_chat_messages=accepts_chat_messages,
        public_key=PublicKey(
            id=main_key_id(base, record.username),
            owner=actor_url(base, record.username),
            public_key_pem=record.public_key_pem,
        ),
    )


async def _build_channel_actor(request: Request, channel: PeerTubeChannelRecord) -> Actor:
    """Build the served Actor document for a PeerTube channel: a
    ``Group``-typed actor, unlike ``_build_actor``'s ``Person``. No
    third-party live-mirroring concept applies here (a channel is never a
    Follow-Only third-party identity): every field comes straight from the
    stored ``PeerTubeChannelRecord``, same as a Full-mode local actor."""
    base = request.app.state.config.bridge.public_base_url
    display_name = channel.display_name or channel.username
    summary = plain_text_to_note_html(channel.summary) if channel.summary else channel.summary
    return Actor(
        id=actor_url(base, channel.username),
        type="Group",
        preferred_username=channel.username,
        name=display_name,
        summary=summary,
        url=actor_url(base, channel.username),
        inbox=inbox_url(base, channel.username),
        outbox=outbox_url(base, channel.username),
        followers=followers_url(base, channel.username),
        following=following_url(base, channel.username),
        playlists_url=f"{base}/playlists/{channel.username}",
        attributed_to_urls=[actor_url(base, channel.owner_username)],
        discoverable=True,
        icon_url=channel.icon_url,
        icon_media_type=await resolve_media_type(request, channel.icon_url),
        image_url=channel.banner_url,
        image_media_type=await resolve_media_type(request, channel.banner_url),
        shared_inbox=shared_inbox_url(base),
        public_key=PublicKey(
            id=main_key_id(base, channel.username),
            owner=actor_url(base, channel.username),
            public_key_pem=channel.public_key_pem,
        ),
    )


async def _build_community_actor(request: Request, community: LemmyCommunityRecord) -> Actor:
    """Build the served Actor document for a hosted Lemmy community: a
    ``Group``-typed actor, unlike ``_build_actor``'s ``Person`` --
    structurally close to ``_build_channel_actor`` above, with one real
    difference: ``attributedTo`` here is a single bare URL naming this
    community's own ``/moderators`` collection (see ``Actor.attributed_to``'s
    own docstring for why that differs from a PeerTube channel's
    ``attributed_to_urls``, a literal list of owning-account actor ids),
    matching real Lemmy's own convention."""
    base = request.app.state.config.bridge.public_base_url
    display_name = community.display_name or community.username
    summary = plain_text_to_note_html(community.summary) if community.summary else community.summary
    return Actor(
        id=actor_url(base, community.username),
        type="Group",
        preferred_username=community.username,
        name=display_name,
        summary=summary,
        url=actor_url(base, community.username),
        inbox=inbox_url(base, community.username),
        outbox=outbox_url(base, community.username),
        followers=followers_url(base, community.username),
        following=following_url(base, community.username),
        featured_url=featured_url(base, community.username),
        attributed_to=moderators_url(base, community.username),
        discoverable=True,
        icon_url=community.icon_url,
        icon_media_type=await resolve_media_type(request, community.icon_url),
        image_url=community.banner_url,
        image_media_type=await resolve_media_type(request, community.banner_url),
        shared_inbox=shared_inbox_url(base),
        public_key=PublicKey(
            id=main_key_id(base, community.username),
            owner=actor_url(base, community.username),
            public_key_pem=community.public_key_pem,
        ),
    )


async def _build_group_actor(request: Request, group: GroupRecord) -> Actor:
    """Build the served Actor document for a hosted FEP-1b12 Group: a
    ``Group``-typed actor, structurally close to ``_build_channel_actor``
    above (``attributed_to_urls``, a plain list naming the owning Profile)
    rather than ``_build_community_actor``'s single ``/moderators``
    collection URL -- FEP-1b12 defines no moderator-collection concept to
    advertise."""
    base = request.app.state.config.bridge.public_base_url
    display_name = group.display_name or group.username
    summary = plain_text_to_note_html(group.summary) if group.summary else group.summary
    return Actor(
        id=actor_url(base, group.username),
        type="Group",
        preferred_username=group.username,
        name=display_name,
        summary=summary,
        url=actor_url(base, group.username),
        inbox=inbox_url(base, group.username),
        outbox=outbox_url(base, group.username),
        followers=followers_url(base, group.username),
        following=following_url(base, group.username),
        attributed_to_urls=[actor_url(base, group.owner_username)],
        discoverable=True,
        icon_url=group.icon_url,
        icon_media_type=await resolve_media_type(request, group.icon_url),
        image_url=group.banner_url,
        image_media_type=await resolve_media_type(request, group.banner_url),
        shared_inbox=shared_inbox_url(base),
        public_key=PublicKey(
            id=main_key_id(base, group.username),
            owner=actor_url(base, group.username),
            public_key_pem=group.public_key_pem,
        ),
    )


def _iso_published_display(iso_or_matrix_ts: str | int | None) -> str:
    """A short "Jul 25, 2026" display date, from either a stored ISO 8601
    ``published`` string (see ``bridge.peertube``'s metadata state event)
    or a raw Matrix ``origin_server_ts`` (milliseconds), whichever a
    caller happens to have on hand."""
    if isinstance(iso_or_matrix_ts, str) and iso_or_matrix_ts:
        try:
            dt = datetime.strptime(iso_or_matrix_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return ""
        return dt.strftime("%b %-d, %Y")
    if isinstance(iso_or_matrix_ts, int) and iso_or_matrix_ts:
        return datetime.fromtimestamp(iso_or_matrix_ts / 1000, tz=timezone.utc).strftime("%b %-d, %Y")
    return ""


async def _build_channel_video_cards(request: Request, *, channel: PeerTubeChannelRecord) -> list[VideoCardView]:
    """One ``VideoCardView`` per currently-published video in ``channel``'s
    room, for ``render_channel_page``'s video grid. Reads each video's own
    metadata + view-count state events (see ``bridge.peertube``) on top of
    ``_list_channel_video_events``'s own index lookup."""
    synapse = request.app.state.synapse
    base = request.app.state.config.bridge.public_base_url
    cards: list[VideoCardView] = []
    for federated in await _list_channel_video_events(request, channel=channel):
        local_video_id = federated.ap_object_id.rsplit("/", 1)[-1]
        try:
            metadata = await synapse.get_room_state(channel.room_id, VIDEO_METADATA_STATE_TYPE, local_video_id)
        except SynapseError:
            continue
        name = (metadata.get("name") or "").strip()
        if not name:
            continue
        thumbnail_mxc = metadata.get("thumbnail_mxc")
        thumbnail_url = None
        if thumbnail_mxc:
            try:
                thumbnail_url = media_url(base, thumbnail_mxc)
            except ValueError:
                thumbnail_url = None
        video_event: dict | None = None
        try:
            video_event = await synapse.get_event(federated.room_id, federated.event_id)
        except SynapseError:
            pass
        duration_ms = (((video_event or {}).get("content") or {}).get("info") or {}).get("duration")
        duration_seconds = (
            round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None
        )
        try:
            views_state = await synapse.get_room_state(channel.room_id, VIDEO_VIEWS_STATE_TYPE, local_video_id)
            views = int(views_state.get("count", 0))
        except SynapseError:
            views = 0
        published_source = metadata.get("published") or (video_event or {}).get("origin_server_ts")
        cards.append(
            VideoCardView(
                video_id=local_video_id,
                name=name,
                watch_url=f"/actor/{channel.username}/videos/{local_video_id}",
                thumbnail_url=thumbnail_url,
                duration_seconds=duration_seconds,
                views=views,
                published_display=_iso_published_display(published_source),
            )
        )
    return cards


@router.get("/.well-known/nodeinfo")
async def nodeinfo_discovery(request: Request) -> JSONResponse:
    config = request.app.state.config
    document = build_nodeinfo_discovery_document(config.bridge.public_base_url)
    return JSONResponse(content=document, media_type="application/json")


@router.get("/nodeinfo/{schema_version}")
async def nodeinfo_document(request: Request, schema_version: str) -> JSONResponse:
    if schema_version not in NODEINFO_SCHEMA_VERSIONS:
        raise HTTPException(status_code=404, detail=f"Unsupported NodeInfo schema version: {schema_version!r}")

    repository: ActorRepository = request.app.state.repository
    usernames = await repository.list_local_usernames()
    document = build_nodeinfo_document(schema_version, local_user_count=len(usernames))
    media_type = f'application/json; profile="http://nodeinfo.diaspora.software/ns/schema/{schema_version}#"'
    return JSONResponse(content=document, media_type=media_type)


async def _known_peer_domains(repository: ActorRepository, local_usernames: list[str], own_domain: str) -> set[str]:
    """Every remote domain this instance has a live Follow relationship
    with, in either direction, derived from existing followers/following
    records rather than a separately-tracked table."""
    domains: set[str] = set()
    for username in local_usernames:
        for actor_id in (await repository.list_followers(username)) + (await repository.list_following(username)):
            host = urlsplit(actor_id).hostname
            if host:
                domains.add(host)
    domains.discard(own_domain)
    return domains


@router.get("/api/v1/instance")
async def mastodon_instance_document(request: Request) -> JSONResponse:
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    usernames = await repository.list_local_usernames()
    peer_domains = await _known_peer_domains(repository, usernames, config.bridge.domain)

    admin_actor: ActorRecord | None = None
    if config.bridge.instance_admin_username:
        admin_actor = await repository.get_local_actor(config.bridge.instance_admin_username)

    document = build_mastodon_instance_document(
        domain=config.bridge.domain,
        public_base_url=config.bridge.public_base_url,
        local_user_count=len(usernames),
        peer_domain_count=len(peer_domains),
        admin_actor=admin_actor,
    )
    return JSONResponse(content=document)


@router.get("/api/v1/instance/peers")
async def mastodon_instance_peers(request: Request) -> JSONResponse:
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    usernames = await repository.list_local_usernames()
    peer_domains = await _known_peer_domains(repository, usernames, config.bridge.domain)
    return JSONResponse(content=sorted(peer_domains))


@router.get("/.well-known/webfinger")
async def webfinger(request: Request, resource: str = Query(...)) -> JSONResponse:
    config = request.app.state.config
    try:
        parsed = parse_acct(resource)
    except Exception:
        parsed = None

    if parsed is not None:
        if parsed.domain != config.bridge.domain:
            raise HTTPException(status_code=404, detail="Resource is not hosted on this domain")
        username = parsed.username
    else:
        # `resource` isn't an acct: URI. WebFinger's own resource
        # parameter is any URI per RFC 7033, not just acct:, and some
        # implementations (confirmed live 2026-07-24: Misskey) query it
        # with the actor's own URL instead, as an extra identity
        # confirmation step before following/boosting. Reverse-resolve it
        # the same way an inbound activity's actor field would be, rather
        # than hard-rejecting a legitimate resource shape.
        username = username_from_actor_url(config.bridge.public_base_url, resource)
        if username is None:
            raise HTTPException(
                status_code=400, detail=f"Not a well-formed acct: URI or a known actor URL: {resource!r}"
            )

    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    resolved_username = record.username if record is not None else None
    if resolved_username is None:
        channel = await repository.get_peertube_channel(username)
        resolved_username = channel.username if channel is not None else None
    if resolved_username is None:
        community = await repository.get_lemmy_community(username)
        resolved_username = community.username if community is not None else None
    if resolved_username is None:
        group = await repository.get_group(username)
        resolved_username = group.username if group is not None else None
    if resolved_username is None:
        raise HTTPException(status_code=404, detail=f"No such actor: {username}")

    document = build_local_webfinger_document(
        username=resolved_username,
        bridge_domain=config.bridge.domain,
        actor_url=actor_url(config.bridge.public_base_url, resolved_username),
    )
    return JSONResponse(content=document, media_type="application/jrd+json")


# Declared BEFORE the plain /actor/{username} route below -- Starlette
# matches routes in declaration order, and {username} alone would
# otherwise swallow "q.rss" as the username itself (dots are ordinary
# path-segment characters, not a route boundary), so the more specific
# pattern has to come first or this route is never reached at all.
# Also explicitly handles HEAD -- FastAPI does not derive it from a GET
# route automatically (see get_media/get_actor's identical handling right
# below), so without this a HEAD request falls through to the generic
# /actor/{username} route instead, treating "q.rss" as a literal username
# and 404ing. Confirmed live 2026-08-08: Thunderbird's feed subscriber
# HEAD-probes a feed URL before GETting it, and truncated the imported
# feed to a single item once this returned 404, same class of bug as the
# Misskey HEAD-probe incident below.
@router.api_route("/actor/{username}.rss", methods=["GET", "HEAD"])
async def get_actor_rss(request: Request, username: str) -> Response:
    """RSS 2.0 feed of a local actor's own public posts -- the same
    ``/@user.rss`` convention real fediverse software (Mastodon
    confirmed) already serves by appending ``.rss`` to a profile's own
    URL. See ``bridge.web_views.render_profile_rss`` for the actual feed
    shape. PeerTube channels don't get one of these -- a video feed isn't
    the same shape as a post feed, and nothing's asked for that yet."""
    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No such actor: {username}")
    config = request.app.state.config
    posts, _next_token = await _build_profile_post_views(request, record)
    xml = render_profile_rss(
        display_name=record.display_name or record.username,
        handle=f"@{record.username}@{config.bridge.domain}",
        profile_url=actor_url(config.bridge.public_base_url, record.username),
        avatar_url=record.icon_url,
        posts=posts,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


# Explicitly handles HEAD (FastAPI does not derive it from a GET route
# automatically, same reasoning as get_media's identical handling).
# Confirmed live 2026-07-24: Misskey HEAD-probes an actor URL as part of
# its own resolution before following/boosting, and gave up (no inbox
# delivery ever followed) once this returned 405.
@router.api_route("/actor/{username}", methods=["GET", "HEAD"])
async def get_actor(request: Request, username: str) -> Response:
    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is None:
        channel = await repository.get_peertube_channel(username)
        if channel is None:
            community = await repository.get_lemmy_community(username)
            if community is None:
                group = await repository.get_group(username)
                if group is None:
                    raise HTTPException(status_code=404, detail=f"No such actor: {username}")
                # No dedicated HTML browse page for a hosted Group yet
                # either -- same reasoning as the community branch below.
                actor = await _build_group_actor(request, group)
                return _activity_json(actor.to_dict())
            # No dedicated HTML browse page for a hosted community yet
            # (unlike a Profile/channel) -- serves its AP document either
            # way for now; a real page is a reasonable future addition,
            # not something federation itself needs.
            actor = await _build_community_actor(request, community)
            return _activity_json(actor.to_dict())
        if _prefers_html(request):
            config = request.app.state.config
            owner = await repository.get_local_actor(channel.owner_username)
            owner_display_name = (owner.display_name or owner.username) if owner else channel.owner_username
            owner_handle = f"@{channel.owner_username}@{config.bridge.domain}"
            owner_url = actor_url(config.bridge.public_base_url, channel.owner_username)

            follower_ids = await repository.list_followers(username)
            following_ids = await repository.list_following(username)
            followers = [await _resolve_person_ref(request, actor_id) for actor_id in follower_ids]
            following = [await _resolve_person_ref(request, actor_id) for actor_id in following_ids]
            videos = await _build_channel_video_cards(request, channel=channel)

            html_doc = render_channel_page(
                display_name=channel.display_name or channel.username,
                handle=f"@{channel.username}@{config.bridge.domain}",
                summary_html=plain_text_to_note_html(channel.summary) if channel.summary else "",
                avatar_url=channel.icon_url,
                banner_url=channel.banner_url,
                owner_display_name=owner_display_name,
                owner_handle=owner_handle,
                owner_url=owner_url,
                videos=videos,
                followers_count=len(follower_ids),
                followers_hidden=False,
                followers=followers,
                following_count=len(following_ids),
                following_hidden=False,
                following=following,
            )
            return HTMLResponse(html_doc)
        actor = await _build_channel_actor(request, channel)
        return _activity_json(actor.to_dict())
    if _prefers_html(request):
        config = request.app.state.config
        from_token = request.query_params.get("before") or None
        posts, next_token = await _build_profile_post_views(request, record, from_token=from_token)
        # newer=<token> threads the CURRENT page's own from_token through to
        # the next page's "Newer posts" link, so paging forward N times and
        # then back N times returns to exactly where you started -- a plain
        # "go to page 1" link wouldn't for anyone more than one page deep.
        newer_token = request.query_params.get("newer") or None
        older_posts_url = (
            f"/actor/{username}?before={quote(next_token, safe='')}"
            f"&newer={quote(from_token, safe='') if from_token else ''}"
            if next_token else None
        )
        newer_posts_url = None
        if from_token:
            newer_posts_url = (
                f"/actor/{username}?before={quote(newer_token, safe='')}" if newer_token else f"/actor/{username}"
            )

        # Same "count always real, list withheld when hidden" split as the
        # public AP collection endpoints (get_followers/get_following) --
        # this static HTML page is just as reachable by any anonymous remote
        # visitor, so it honors the exact same hide_followers/hide_following
        # setting (see bridge.identity_settings). Resolving each member's
        # identity is skipped entirely when hidden: nothing to show, and
        # no reason to pay for it.
        hide_followers = await get_effective_setting(repository, username, "profile", "hide_followers")
        hide_following = await get_effective_setting(repository, username, "profile", "hide_following")
        follower_ids = await repository.list_followers(username)
        following_ids = await repository.list_following(username)
        followers = (
            [await _resolve_person_ref(request, actor_id) for actor_id in follower_ids]
            if not hide_followers else []
        )
        following = (
            [await _resolve_person_ref(request, actor_id) for actor_id in following_ids]
            if not hide_following else []
        )

        pinned_posts = await _fetch_featured_post_views(request, record=record)

        html_doc = render_profile_page(
            display_name=record.display_name or record.username,
            handle=f"@{record.username}@{config.bridge.domain}",
            summary_html=plain_text_to_note_html(record.summary) if record.summary else "",
            avatar_url=record.icon_url,
            banner_url=record.banner_url,
            posts=posts,
            pinned_posts=pinned_posts,
            older_posts_url=older_posts_url,
            newer_posts_url=newer_posts_url,
            followers_count=len(follower_ids),
            followers_hidden=hide_followers,
            followers=followers,
            following_count=len(following_ids),
            following_hidden=hide_following,
            following=following,
            rss_url=f"/actor/{username}.rss",
            profile_url=actor_url(config.bridge.public_base_url, username),
        )
        return HTMLResponse(html_doc)
    actor = await _build_actor(request, record)
    return _activity_json(actor.to_dict())


def _matrix_ts_to_iso(origin_server_ts: int | None) -> str:
    if not origin_server_ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reconstruct_note_body(
    federated, event_content: dict, attachment: dict | None, *, config, raw_event_content: dict | None = None,
) -> tuple[str, str, str | None]:
    """``(body, content_html, quote_uri)`` for reconstructing a Note from
    the Matrix event that mirrors it (shared by ``get_outbox`` and
    ``get_note``).

    ``federated.reposted_object_id`` set means this is a ``;repost``'s own
    echo (see ``bridge.commands._handle_repost``) -- its Matrix event's
    ``body`` is NEVER "just a filename" the way an ordinary media post's
    is (see ``bridge.inbox_dispatch.build_preview_media_content``, which
    always keeps the full caption+notice text as ``body`` regardless of
    ``msgtype``), and its actual ActivityPub content differs from that
    Matrix-facing text entirely -- see ``build_repost_note_content``/
    ``split_repost_caption``. An ordinary post's ``body`` with an
    attachment is a genuine caption only under the caption convention
    (separate differing ``filename`` -- see ``bridge.media.media_caption``),
    else it's just the filename and is blanked.

    A sibling-bridge Profile Room reply (see ``bridge.profile_posts.
    _maybe_handle_sibling_bridge_reply``) gets the same "reconstruct from
    the Matrix event, don't trust a separately-cached copy" treatment --
    its ``SOCIAL_RELATES_TO_FIELD`` lives directly on the event's own
    content, same as the live send path reads it from, so this just
    re-derives the identical header+link rather than falling through to
    the ordinary body-only reconstruction below (which would silently
    drop the "reply to X" framing on a re-fetch/outbox view). Uses
    ``raw_event_content`` (the ORIGINAL, pre-edit content), same
    raw-vs-effective split ``derive_in_reply_to`` already needs -- an
    edit's ``m.new_content`` never carries this field either."""
    if federated.reposted_object_id:
        body = (event_content.get("body") or "").strip()
        caption = split_repost_caption(body)
        return body, build_repost_note_content(caption, federated.reposted_object_id), federated.reposted_object_id
    relates_to = (raw_event_content or event_content).get(SOCIAL_RELATES_TO_FIELD)
    if isinstance(relates_to, dict) and relates_to.get("rel_type") == SOCIAL_REL_TYPE_REPLY:
        reply_sender = relates_to.get("sender") or ""
        if is_foreign_bridge_user(reply_sender, config):
            relates_to_content = relates_to.get("content") or {}
            external_url = relates_to_content.get("external_url")
            if isinstance(external_url, str) and external_url:
                body = (event_content.get("body") or "").strip()
                handle = resolve_cross_bridge_handle(relates_to_content, fallback=relates_to.get("displayname"))
                content_html = build_sibling_reply_note_content(body, external_url, handle)
                return body, content_html, None
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

    Scans every ``Accept`` header entry (ignoring q-values, which is good
    enough in practice) for a DECISIVE media type: an explicit AP type
    anywhere in the header wins even if a bare ``*/*`` appears earlier in
    it, and likewise for an explicit HTML type. This scans the whole
    header rather than stopping at the first entry the way this used to,
    which matters now that ``*/*`` alone is treated as merely ambiguous
    (see below) instead of an immediate "yes".

    Falls back to sniffing ``User-Agent`` for "mozilla" (present in
    essentially every real browser's UA string for historical reasons, and
    essentially never in a server-side HTTP client's) or "synapse" when
    the Accept header is missing, empty, or contains nothing more decisive
    than a bare ``*/*``. Confirmed live 2026-07-25: this used to default
    straight to HTML for that case on the theory that "a real AP fetcher
    reliably states its own preference explicitly", but PeerTube's own
    "resolve a remote URL" search feature does exactly this (no decisive
    Accept header at all), so a real channel actor could never actually be
    resolved by anyone searching for it from another PeerTube instance:
    this bridge served its own HTML page back instead of the AP JSON
    PeerTube needed, which PeerTube (correctly, from its own side) then
    treated as "not an ActivityPub actor at all".

    The "synapse" branch is for the exact same reason in reverse: Synapse's
    own URL-preview fetcher (``synapse.media.url_previewer.UrlPreviewer``,
    what builds a message's link-preview card for Element and friends) is
    equally non-decisive -- its ``get_file`` call (``synapse/http/client.py``)
    sends no ``Accept`` header at all, only a ``User-Agent`` of
    ``Synapse/<version>`` (optionally with a configured suffix). Without
    this it fell into the same "server-side, not a browser" bucket as a
    real AP crawler and got served raw ``application/ld+json`` back, which
    Synapse's previewer then flatly refuses to parse for OpenGraph tags
    (confirmed live 2026-10-22 against the homeserver's own
    ``/_matrix/media/v3/preview_url``: ``"Requested file's content type not
    allowed for this operation: application/ld+json; ..."``) -- so a note
    or profile permalink posted into a room never got a preview card at
    all, independent of whether this page's own OpenGraph tags were
    correct.
    """
    accept = request.headers.get("accept", "")
    for part in accept.split(","):
        media_type = part.split(";", 1)[0].strip().lower()
        if media_type in _AP_MEDIA_TYPES:
            return False
        if media_type in _HTML_MEDIA_TYPES:
            return True
        # A bare "*/*" is ambiguous, not decisive, unlike before: this no
        # longer returns True on the spot, but keeps scanning the rest of
        # the header for a real decisive entry, then falls through to the
        # User-Agent tiebreaker below if none ever shows up.
    user_agent = request.headers.get("user-agent", "").lower()
    return "mozilla" in user_agent or "synapse" in user_agent


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

    raw_event_content = matrix_event.get("content") or {}
    event_content = _effective_event_content(matrix_event)
    attachment = await resolve_reconstructed_attachment(request, event_content)
    body, content_html, _quote_uri = _reconstruct_note_body(
        federated, event_content, attachment, config=config, raw_event_content=raw_event_content
    )
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
        event_id = reaction_event.get("event_id") or ""
        if event_id and key.startswith("mxc://") and event_id not in custom_emoji_url_by_event:
            # A real MSC4027 reaction a Matrix client sent directly -- key
            # IS the image's own mxc:// URI already, no shortcode/DB
            # lookup needed to resolve it (see bridge.web_views._is_mxc_uri).
            # Already marked published at reaction-time by
            # bridge.reaction_bridge.maybe_federate_reaction (needed to
            # federate it out as a real EmojiReact icon), so nothing to
            # mark here either -- same reasoning as the shortcode case above.
            try:
                custom_emoji_url_by_event[event_id] = media_url(base, key)
            except ValueError:
                pass
        person = await _resolve_reactor(request, sender)
        resolved_reaction_events.append(ReactionEvent(key=key, event_id=event_id, person=person))

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


async def _reconstruct_note_from_matrix_event(
    request: Request, *, matrix_event: dict, base: str, username: str
) -> Note | None:
    """Rebuild the AP Note a single already-distributed Matrix message event
    represents -- ``None`` if it was never actually distributed as a post
    (no recorded mapping) or has nothing reconstructable.

    Shared by ``_fetch_room_outbox_notes`` (scanning a whole room's history)
    and ``_fetch_featured_notes`` (a handful of specifically-pinned events),
    so both build identical Note bodies for the same event rather than two
    separately-maintained implementations of the same logic."""
    repository: ActorRepository = request.app.state.repository
    matrix_event_id = matrix_event.get("event_id")
    if not matrix_event_id:
        return None
    # Only events that were actually distributed as a post have a recorded
    # mapping -- reuse its `ap_object_id` so the Note `id` here matches
    # exactly what followers were already delivered.
    federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
    if federated is None:
        return None
    # Same raw-vs-effective split as get_note: m.mentions/m.relates_to
    # are original-event-only fields an edit's m.new_content never
    # carries (see _effective_event_content's own docstring), so both
    # derive_in_reply_to and reconstruct_note_mentions need the RAW
    # content specifically, not the latest-edit one the body/HTML below
    # is otherwise correctly rendered from.
    raw_event_content = matrix_event.get("content") or {}
    event_content = _effective_event_content(matrix_event)
    attachment = await resolve_reconstructed_attachment(request, event_content)
    body, content_html, quote_uri = _reconstruct_note_body(
        federated, event_content, attachment,
        config=request.app.state.config, raw_event_content=raw_event_content,
    )
    if not body and attachment is None:
        return None
    in_reply_to = await derive_in_reply_to(repository, raw_event_content)

    note_tags: list[dict] = []
    if not federated.reposted_object_id and body:
        body, note_tags, mention_links = await reconstruct_note_mentions(
            request, body=body, event_content=raw_event_content,
            in_reply_to=in_reply_to, sender_actor_id=actor_url(base, username),
        )
        content_html = plain_text_to_note_html(body, mention_links)

    origin_server_ts = matrix_event.get("origin_server_ts") or 0
    return Note(
        id=federated.ap_object_id,
        attributed_to=actor_url(base, username),
        content=content_html,
        published=_matrix_ts_to_iso(origin_server_ts),
        to=[AS_PUBLIC],
        cc=[followers_url(base, username)],
        # Without this, a reply reconstructed here reads as a standalone
        # post to anyone fetching it -- see derive_in_reply_to.
        in_reply_to=in_reply_to,
        attachment=[attachment] if attachment else [],
        tag=note_tags,
        quote_uri=quote_uri,
    )


async def _fetch_room_outbox_notes(
    request: Request, *, room_id: str, base: str, username: str, owner_matrix_id: str, bot_mxid: str
) -> list[tuple[int, Note]]:
    """Notes actually distributed as posts from a single room, each paired
    with its raw Matrix ``origin_server_ts`` so ``get_outbox`` can merge
    several rooms' worth (current Profile Room plus any it replaced -- see
    ``ActorRepository.get_profile_room_history``) into one feed sorted by
    recency, instead of only ever showing whichever room is current."""
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
        note = await _reconstruct_note_from_matrix_event(request, matrix_event=matrix_event, base=base, username=username)
        if note is None:
            continue
        origin_server_ts = matrix_event.get("origin_server_ts") or 0
        notes.append((origin_server_ts, note))
    return notes


async def _fetch_pinned_room_events(request: Request, *, room_id: str) -> list[tuple[FederatedEvent, dict]]:
    """``(FederatedEvent, raw Matrix event)`` pairs for every post currently
    pinned in ``room_id`` (Matrix's ``m.room.pinned_events`` state), in that
    state's own order -- shared by ``_fetch_featured_notes`` (the AP
    ``featured`` collection) and ``_fetch_featured_post_views`` (the HTML
    profile page's own pinned-posts section), so both always agree on
    exactly which pinned entries actually resolve to a real distributed
    post (nothing stops pinning an arbitrary, never-federated timeline
    event in Element -- see ``bridge.pinned_posts``'s identical skip for
    that case on the outbound-Add-activity side).

    Reads live off Synapse each request rather than any bridge-maintained
    copy of its own (there isn't one -- see this feature's own design notes
    on reusing existing state instead of a new table), the same
    "reconstruct fresh every time" precedent ``_fetch_room_outbox_notes``
    above already sets for this room, just scoped to the handful of pinned
    events instead of the whole room history."""
    synapse = request.app.state.synapse
    repository: ActorRepository = request.app.state.repository
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        state = await synapse.get_room_state(room_id, "m.room.pinned_events", "", as_user_id=bot_mxid)
    except SynapseError:
        return []  # nothing pinned yet -- Synapse 404s rather than returning an empty list

    pairs: list[tuple[FederatedEvent, dict]] = []
    for matrix_event_id in state.get("pinned") or []:
        federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
        if federated is None:
            continue
        try:
            matrix_event = await synapse.get_event(room_id, matrix_event_id, as_user_id=bot_mxid)
        except SynapseError:
            continue
        pairs.append((federated, matrix_event))
    return pairs


async def _fetch_featured_notes(request: Request, *, record: ActorRecord, base: str) -> list[Note]:
    """``record``'s own Profile Room's currently-pinned posts, as AP Notes --
    the AP-side half of ``bridge.pinned_posts``'s bidirectional pin sync."""
    if not record.room_id:
        return []
    notes: list[Note] = []
    for federated, matrix_event in await _fetch_pinned_room_events(request, room_id=record.room_id):
        note = await _reconstruct_note_from_matrix_event(
            request, matrix_event=matrix_event, base=base, username=record.username
        )
        if note is not None:
            notes.append(note)
    return notes


async def _fetch_featured_post_views(request: Request, *, record: ActorRecord) -> list["PostView"]:
    """``record``'s own Profile Room's currently-pinned posts, as
    ``PostView``s -- for the "\U0001F4CC Pinned" section at the top of this
    bridge's own served ``/actor/{username}`` HTML profile page."""
    if not record.room_id:
        return []
    views: list["PostView"] = []
    for federated, matrix_event in await _fetch_pinned_room_events(request, room_id=record.room_id):
        view = await _build_post_view(request, federated, matrix_event=matrix_event)
        if view is not None:
            views.append(view)
    return views


async def _list_channel_video_events(request: Request, *, channel) -> list[FederatedEvent]:
    """Every currently-published video in ``channel``'s own room, newest
    first. Reads ``bridge.peertube``'s own video index (a small, rarely
    written state event only ``;publish``/``;unpublish`` ever touch) rather
    than scanning room history the way the ordinary Note-outbox does. See
    ``VIDEO_INDEX_STATE_TYPE``'s own comment for why: a channel room's
    timeline gets a brand new event on every single VIEW-COUNT write (a
    Matrix state update is always ALSO a new timeline event, with no way to
    update "in place"), which silently pushed a real published video's own
    message out of any shallow, most-recent-N timeline scan long before its
    view count could plausibly matter. Confirmed live 2026-07-25 on a
    genuinely published video whose own channel page had gone right back to
    showing "No videos published yet." Shared by the channel's own outbox
    (``_fetch_channel_outbox_items``) and its HTML video grid
    (``_build_channel_video_cards``), so both always agree on exactly which
    videos currently count as published."""
    repository: ActorRepository = request.app.state.repository
    synapse = request.app.state.synapse
    events: list[FederatedEvent] = []
    for local_video_id in await get_video_index(synapse, room_id=channel.room_id):
        video_id = f"{request.app.state.config.bridge.public_base_url}/actor/{channel.username}/videos/{local_video_id}"
        federated = await repository.get_federated_event_by_ap_object(video_id)
        if federated is not None:
            events.append(federated)
    return events


async def _fetch_channel_outbox_items(request: Request, *, channel, base: str) -> list[dict]:
    """A PeerTube channel's outbox: every currently-published video in its
    room, each reconstructed as its own Create+Announce activity pair (see
    ``bridge.peertube.reconstruct_video_activities`` for why that's the
    right shape here, unlike the bare-Note convention ``get_outbox`` uses
    for an ordinary Profile Room below)."""
    repository: ActorRepository = request.app.state.repository
    synapse = request.app.state.synapse
    owner = await repository.get_local_actor(channel.owner_username)
    if owner is None:
        return []

    items: list[dict] = []
    for federated in await _list_channel_video_events(request, channel=channel):
        reconstructed = await reconstruct_video_activities(
            synapse, base=base, channel=channel, owner=owner, federated=federated,
        )
        if reconstructed is None:
            continue
        create_activity, announce_activity = reconstructed
        items.append(create_activity.to_dict())
        items.append(announce_activity.to_dict())
    return items


@router.get("/outbox/{username}")
async def get_outbox(request: Request, username: str) -> JSONResponse:
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository: ActorRepository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is None:
        channel = await repository.get_peertube_channel(username)
        if channel is None:
            community = await repository.get_lemmy_community(username)
            if community is None:
                group = await repository.get_group(username)
                if group is None:
                    raise HTTPException(status_code=404, detail=f"No such actor: {username}")
                items = await _fetch_group_outbox_items(request, group=group, base=base)
                collection = OrderedCollection(id=outbox_url(base, username), items=items, embed_first=False)
                return _collection_or_page(request, collection)
            # Real-time delivery at post time reaches whoever already
            # follows us directly, but a profile page fetched fresh by a
            # remote server that has never followed us (or is only now
            # resolving us for the first time) reads THIS collection, not
            # that history -- confirmed live 2026-10-19: a real post
            # existed, but https://poa.st/@haven@glowers.club still showed
            # no posts at all, since this previously returned permanently
            # empty. See _fetch_community_outbox_items for what it actually
            # covers (local-member posts only, for now).
            items = await _fetch_community_outbox_items(request, community=community, base=base)
            collection = OrderedCollection(id=outbox_url(base, username), items=items, embed_first=False)
            return _collection_or_page(request, collection)
        items = await _fetch_channel_outbox_items(request, channel=channel, base=base)
        collection = OrderedCollection(id=outbox_url(base, username), items=items, embed_first=False)
        return _collection_or_page(request, collection)

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
    return _collection_or_page(request, collection)


@router.get("/actor/{username}/notes/{note_id}")
async def get_note(request: Request, username: str, note_id: str) -> Response:
    """Serve a single distributed post at its own AP object IRI.

    Needed for anything that dereferences a Note by bare ``id`` string
    rather than embedding it inline -- notably an ``Announce`` of one of
    our own local posts, whose ``object`` some implementations (e.g.
    Pleroma/Akkoma) send as just the IRI, not the full Note. Without this
    route that fetch 404s, silently dropping the repost card
    ``bridge.inbox_dispatch._handle_announce`` would otherwise build in the
    reposter's own Remote User Room (the DM notification to the post's
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
    attachment = await resolve_reconstructed_attachment(request, event_content)
    _body, content_html, quote_uri = _reconstruct_note_body(
        federated, event_content, attachment, config=config, raw_event_content=raw_event_content
    )

    # A redaction empties a Matrix event's `content` (Synapse still serves
    # the event itself -- `get_event` above only 404s if it's gone
    # entirely) rather than removing it, so a redacted post's own
    # FederatedEvent row is still there and still resolves here -- without
    # this check this route just re-served the same (now content-less)
    # Note forever, live-confirmed 2026-07-13 on a post whose Matrix side
    # had already been redacted (and whose followers -- if any existed --
    # already got a real Delete via bridge.delete_bridge): anyone
    # dereferencing this URL directly (a remote server rendering a
    # permalink, not relying on push delivery) kept getting a normal `200
    # Note` with blank content indefinitely, never a real "this is gone."
    # Same "nothing here" test get_outbox/_build_post_view already use
    # (_fetch_room_outbox_notes/_build_post_view's own `if not body and
    # attachment is None` checks) -- kept consistent rather than adding a
    # second, separately-drifting way to detect "empty."
    if not _body and attachment is None:
        raise HTTPException(status_code=410, detail="Gone")

    # Without this, a fetched copy of a reply reads as a standalone post --
    # the delivered Create had inReplyTo but this reconstruction didn't.
    # See derive_in_reply_to. Computed once here (rather than inline below,
    # its previous spot) since reconstruct_note_mentions also needs it, to
    # carry over the parent's own participants the same way a live send does.
    in_reply_to = await derive_in_reply_to(repository, raw_event_content)

    note_tags: list[dict] = []
    mention_links: dict[str, str] = {}
    if not federated.reposted_object_id and _body:
        # A repost echo's content is entirely synthetic (build_repost_note_
        # content, via _reconstruct_note_body's own repost branch above) --
        # never real user-typed text with mentions of its own to resolve.
        _body, note_tags, mention_links = await reconstruct_note_mentions(
            request, body=_body, event_content=raw_event_content,
            in_reply_to=in_reply_to, sender_actor_id=actor_url(base, username),
        )
        content_html = plain_text_to_note_html(_body, mention_links)

    guest_mention = await _guest_post_owner_mention(request, federated)
    if guest_mention is not None:
        owner_handle, owner_actor_id = guest_mention
        tacked_body = _body if owner_handle in _body else f"{owner_handle} {_body}".strip()
        if tacked_body:
            mention_links = {**mention_links, owner_handle: owner_actor_id}
            content_html = plain_text_to_note_html(tacked_body, mention_links)
        note_tags.append({"type": "Mention", "href": owner_actor_id, "name": owner_handle})

    note = Note(
        id=ap_object_id,
        attributed_to=actor_url(base, username),
        content=content_html,
        published=_matrix_ts_to_iso(matrix_event.get("origin_server_ts")),
        to=[AS_PUBLIC],
        cc=[followers_url(base, username)],
        in_reply_to=in_reply_to,
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


async def _reconstruct_lemmy_page(
    request: Request, *, federated: FederatedEvent, community: LemmyCommunityRecord, base: str,
) -> dict | None:
    """Rebuild a hosted community's top-level ``Page`` post from whichever
    Matrix event actually mirrors it -- shared by ``get_lemmy_post`` (a
    single post fetched by its own IRI) and ``_fetch_community_outbox_items``
    (every one of them, for the community's own outbox), so both always
    agree and an edit is reflected in either. Returns ``None`` if the
    underlying Matrix event is gone, or a redaction emptied it (see
    ``get_note``'s identical "nothing here" check -- same reasoning)."""
    bot_mxid = f"@{request.app.state.config.appservice.bot_localpart}:{request.app.state.config.synapse.server_name}"
    try:
        matrix_event = await request.app.state.synapse.get_event(
            federated.room_id, federated.event_id, as_user_id=bot_mxid
        )
    except SynapseError:
        return None

    event_content = _effective_event_content(matrix_event)
    from bridge.lemmy_bridge import _title_and_body_from_message

    title, body = _title_and_body_from_message((event_content.get("body") or "").strip())
    if not title:
        return None

    community_actor_id = actor_url(base, community.username)
    return {
        "id": federated.ap_object_id,
        "type": "Page",
        "attributedTo": federated.author_actor_id,
        "name": title,
        "content": plain_text_to_note_html(body) if body else "",
        "mediaType": "text/html",
        "published": _matrix_ts_to_iso(matrix_event.get("origin_server_ts")),
        "to": [community_actor_id, AS_PUBLIC],
        "cc": [],
        "audience": community_actor_id,
    }


async def _list_owned_community_post_events(
    request: Request, *, community: LemmyCommunityRecord, base: str,
) -> list[FederatedEvent]:
    """Every currently-live top-level post in ``community``'s own room,
    newest first -- the community-outbox counterpart to
    ``_fetch_room_outbox_notes``, same "scan recent room history, keep what
    resolves to a real distributed post" approach.

    Scoped to posts authored by one of OUR OWN linked members for now (a
    remote member's post imported via ``maybe_handle_owned_community_create``
    keeps the REMOTE author's own ``ap_object_id``/key, which this bridge
    has no way to re-sign a reconstructed ``Create`` for) -- an explicit,
    narrower version of this route's previous "permanently empty" scope cut,
    not the whole fix; a real reconstruction for remote-authored posts is a
    reasonable future addition if this turns out to matter in practice."""
    repository: ActorRepository = request.app.state.repository
    bot_mxid = f"@{request.app.state.config.appservice.bot_localpart}:{request.app.state.config.synapse.server_name}"
    try:
        history = await request.app.state.synapse.get_room_messages(
            community.room_id, limit=OUTBOX_HISTORY_LIMIT, direction="b", as_user_id=bot_mxid
        )
    except (SynapseError, httpx.HTTPError):
        logger.warning("Failed to fetch history for %s's outbox", community.username, exc_info=True)
        return []

    events: list[FederatedEvent] = []
    for matrix_event in history.get("chunk", []):
        if matrix_event.get("type") != "m.room.message":
            continue
        event_id = matrix_event.get("event_id")
        if not event_id:
            continue
        federated = await repository.get_federated_event_by_matrix_event(event_id)
        if federated is None or federated.thread_root_event_id is not None:
            continue
        if username_from_actor_url(base, federated.author_actor_id) is None:
            continue  # remote-authored import -- see this function's own docstring
        events.append(federated)
    return events


async def _fetch_community_outbox_items(
    request: Request, *, community: LemmyCommunityRecord, base: str,
) -> list[dict]:
    """A hosted Lemmy community's outbox: every currently-live local-member
    top-level post, each reconstructed as its own Create+Announce pair --
    same ``Announce{Create{Page}}`` shape real Lemmy itself uses for a
    community outbox (confirmed earlier against real captured
    ``sh.itjust.works`` data, see ``bridge.lemmy_bridge.
    _page_and_author_from_item``'s own Announce-unwrapping), with
    deterministic ids so a remote server recognizes a repeat fetch as the
    same activities rather than new ones (same reasoning as
    ``bridge.peertube.reconstruct_video_activities``)."""
    community_actor_id = actor_url(base, community.username)
    items: list[dict] = []
    for federated in await _list_owned_community_post_events(request, community=community, base=base):
        page = await _reconstruct_lemmy_page(request, federated=federated, community=community, base=base)
        if page is None:
            continue
        create_activity = Activity(
            id=f"{federated.ap_object_id}/activity", type="Create", actor=federated.author_actor_id,
            object=page, published=page["published"], to=[AS_PUBLIC], cc=[community_actor_id],
            audience=community_actor_id,
        )
        announce_activity = Activity(
            id=f"{federated.ap_object_id}/announces/outbox", type="Announce", actor=community_actor_id,
            object=federated.ap_object_id, published=page["published"], to=[AS_PUBLIC],
            cc=[followers_url(base, community.username)],
        )
        items.append(create_activity.to_dict())
        items.append(announce_activity.to_dict())
    return items


async def _fetch_group_outbox_items(request: Request, *, group: GroupRecord, base: str) -> list[dict]:
    """A hosted Group's outbox: every currently-tracked message in its
    room, each reconstructed as its own Create+Announce pair, same
    ``Announce{Create{Note}}`` shape ``bridge.group_bridge.
    maybe_distribute_group_message`` itself sends live. Unlike
    ``_fetch_room_outbox_notes`` (one fixed author per room),
    a Group room has MANY different authors, so this resolves each
    message's real author from its own recorded ``FederatedEvent`` rather
    than assuming a single owner -- deliberately a lighter reconstruction
    than ``_reconstruct_note_from_matrix_event``'s full pipeline (no
    mention-pill/attachment/edit handling yet): a remote server
    refetching this collection sees plain text only for now, an accepted
    limitation, not a correctness issue (nothing attributes the wrong
    author)."""
    group_actor_id = actor_url(base, group.username)
    repository: ActorRepository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        history = await synapse.get_room_messages(
            group.room_id, limit=OUTBOX_HISTORY_LIMIT, direction="b", as_user_id=bot_mxid
        )
    except (SynapseError, httpx.HTTPError):
        logger.warning("Failed to fetch history for group %s's outbox", group.username, exc_info=True)
        return []

    items: list[dict] = []
    for matrix_event in history.get("chunk", []):
        if matrix_event.get("type") != "m.room.message":
            continue
        matrix_event_id = matrix_event.get("event_id")
        if not matrix_event_id:
            continue
        federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
        if federated is None:
            continue
        content = matrix_event.get("content") or {}
        body = (content.get("body") or "").strip()
        if not body:
            continue
        published = datetime.fromtimestamp(
            (matrix_event.get("origin_server_ts") or 0) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        note_dict = {
            "id": federated.ap_object_id, "type": "Note", "attributedTo": federated.author_actor_id,
            "content": plain_text_to_note_html(body), "mediaType": "text/html", "published": published,
            "to": [AS_PUBLIC], "cc": [group_actor_id], "audience": group_actor_id,
        }
        create_activity = Activity(
            id=f"{federated.ap_object_id}/activity", type="Create", actor=federated.author_actor_id,
            object=note_dict, published=published, to=[AS_PUBLIC], cc=[group_actor_id], audience=group_actor_id,
        )
        announce_activity = Activity(
            id=f"{federated.ap_object_id}/announces/outbox", type="Announce", actor=group_actor_id,
            object=federated.ap_object_id, published=published, to=[AS_PUBLIC],
            cc=[followers_url(base, group.username)],
        )
        items.append(create_activity.to_dict())
        items.append(announce_activity.to_dict())
    return items


@router.api_route("/actor/{username}/posts/{post_id}", methods=["GET", "HEAD"])
async def get_lemmy_post(request: Request, username: str, post_id: str) -> Response:
    """Serve a single hosted-Lemmy-community top-level post at its own AP
    object IRI -- the ``Page`` counterpart to ``get_note`` above, needed for
    the exact same reason: a plain ``;repost`` of one of these (send_repost's
    own Announce references the post by bare ``id`` string, same as any
    other post) 404s without this route, confirmed live 2026-10-19 (a real
    repost of a real Haven-community test post never showed up on poa.st --
    this was the missing piece, not a delivery/signature problem; a direct
    re-delivery of the real Announce to a real poa.st inbox got a clean 2xx).

    Reconstructs the Page via ``_reconstruct_lemmy_page`` -- see that
    function for why an edit is reflected here too, same as ``get_note``."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    await _get_actor_record(request, username)
    repository: ActorRepository = request.app.state.repository

    ap_object_id = f"{base}/actor/{username}/posts/{post_id}"
    federated = await repository.get_federated_event_by_ap_object(ap_object_id)
    if federated is None or federated.author_actor_id != actor_url(base, username):
        raise HTTPException(status_code=404, detail="Not Found")

    community = await repository.get_lemmy_community_by_room_id(federated.room_id)
    if community is None or await _is_private_room(repository, federated.room_id):
        raise HTTPException(status_code=404, detail="Not Found")

    page = await _reconstruct_lemmy_page(request, federated=federated, community=community, base=base)
    if page is None:
        # A redaction empties `content` the same way get_note's own comment
        # explains -- the FederatedEvent row survives, so this route must
        # stop serving the vanished post rather than re-serve it blank
        # forever (same "nothing here" test as get_note/get_outbox).
        raise HTTPException(status_code=410, detail="Gone")

    return _activity_json({"@context": JSON_LD_CONTEXT, **page})


async def _build_video_view(
    request: Request, *, channel: PeerTubeChannelRecord, owner, federated: FederatedEvent,
) -> VideoView | None:
    """Resolve a published video's ``FederatedEvent`` into a ``VideoView``
    for ``render_video_page``: same two sources
    ``bridge.peertube.reconstruct_video_activities`` reads (the video's own
    Matrix message for media info, its ``VIDEO_METADATA_STATE_TYPE`` state
    event for name/category/etc.), plus display-formatted category/licence/
    language names, the real Like/Dislike counts already mirrored as
    ``m.reaction``s on the video's own Matrix event (see
    ``bridge.reaction_bridge``'s outbound Dislike support), and its
    comments (ordinary AP reply Notes threaded off it, see
    ``_build_post_view``)."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    synapse = request.app.state.synapse
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        video_event = await synapse.get_event(federated.room_id, federated.event_id, as_user_id=bot_mxid)
    except SynapseError:
        return None
    video_content = video_event.get("content") or {}
    attachment = await resolve_reconstructed_attachment(request, video_content)
    if attachment is None:
        return None

    local_video_id = federated.ap_object_id.rsplit("/", 1)[-1]
    try:
        metadata = await synapse.get_room_state(channel.room_id, VIDEO_METADATA_STATE_TYPE, local_video_id)
    except SynapseError:
        return None
    name = (metadata.get("name") or "").strip()
    if not name:
        return None

    info = video_content.get("info") or {}
    duration_ms = info.get("duration")
    duration_seconds = (
        round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None
    )
    thumbnail_mxc = metadata.get("thumbnail_mxc")
    thumbnail_url = None
    if thumbnail_mxc:
        try:
            thumbnail_url = media_url(base, thumbnail_mxc)
        except ValueError:
            thumbnail_url = None

    raw_category = metadata.get("category")
    category_name = raw_category if raw_category and resolve_category(raw_category) is not None else None
    raw_licence = metadata.get("license")
    licence_name = raw_licence if raw_licence and resolve_licence(raw_licence) is not None else None
    language_code = metadata.get("language")
    language_name = resolve_language(language_code)[1] if language_code else None

    try:
        views_state = await synapse.get_room_state(channel.room_id, VIDEO_VIEWS_STATE_TYPE, local_video_id)
        views = int(views_state.get("count", 0))
    except SynapseError:
        views = 0

    try:
        reaction_events = await synapse.get_relations(
            federated.room_id, federated.event_id, rel_type="m.annotation", event_type="m.reaction",
            as_user_id=bot_mxid,
        )
    except SynapseError:
        reaction_events = []
    like_count = 0
    dislike_count = 0
    for reaction_event in reaction_events:
        key = ((reaction_event.get("content") or {}).get("m.relates_to") or {}).get("key") or ""
        normalized = key.replace("️", "")
        if normalized == "\U0001F44D":
            like_count += 1
        elif normalized == "\U0001F44E":
            dislike_count += 1

    comments = await _build_video_comments(request, federated)

    owner_display_name = owner.display_name or owner.username
    owner_handle = f"@{owner.username}@{config.bridge.domain}"
    channel_display_name = channel.display_name or channel.username
    channel_handle = f"@{channel.username}@{config.bridge.domain}"
    published_at = metadata.get("published") or _matrix_ts_to_iso(video_event.get("origin_server_ts"))

    return VideoView(
        video_id=local_video_id,
        name=name,
        description_html=plain_text_to_note_html(metadata.get("description")) if metadata.get("description") else "",
        media_url=attachment["url"],
        media_type=attachment.get("mediaType") or "video/mp4",
        thumbnail_url=thumbnail_url,
        duration_seconds=duration_seconds,
        views=views,
        channel_display_name=channel_display_name,
        channel_handle=channel_handle,
        channel_url=actor_url(base, channel.username),
        channel_avatar_url=channel.icon_url,
        owner_display_name=owner_display_name,
        owner_handle=owner_handle,
        owner_url=actor_url(base, owner.username),
        published_display=_iso_published_display(published_at),
        category_name=category_name,
        licence_name=licence_name,
        language_name=language_name,
        tags=list(metadata.get("tags") or []),
        sensitive=bool(metadata.get("sensitive", False)),
        like_count=like_count,
        dislike_count=dislike_count,
        comments=comments,
        comments_enabled=bool(metadata.get("commentsEnabled", True)),
    )


async def _build_video_comments(request: Request, federated: FederatedEvent) -> list[PostView]:
    """Every comment (an ordinary AP reply Note) on a published video, oldest
    first: same Matrix-thread-relations lookup ``_build_thread_post_views``
    uses for an ordinary post's own replies, but EXCLUDING the video's own
    root event (never itself a ``PostView``; ``_build_post_view`` expects
    Note-shaped content, not a video message)."""
    config = request.app.state.config
    repository: ActorRepository = request.app.state.repository
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        related = await request.app.state.synapse.get_relations(
            federated.room_id, federated.event_id, rel_type="m.thread", as_user_id=bot_mxid
        )
    except SynapseError:
        related = []
    comments: list[PostView] = []
    for related_event in related:
        event_id = related_event.get("event_id")
        if not event_id:
            continue
        fe = await repository.get_federated_event_by_matrix_event(event_id)
        if fe is None:
            continue
        view = await _build_post_view(request, fe)
        if view is not None:
            comments.append(view)
    comments.sort(key=lambda p: p.origin_server_ts)
    return comments


@router.get("/actor/{username}/videos/{video_id}")
async def get_video(request: Request, username: str, video_id: str) -> Response:
    """Serves a published video: the real AP ``Video`` object to an
    AP-speaking caller, or the tube-style watch page (``render_video_page``)
    to a plain browser, same content-negotiation pattern as ``get_note``."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository: ActorRepository = request.app.state.repository
    channel = await repository.get_peertube_channel(username)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"No such channel: {username}")
    owner = await repository.get_local_actor(channel.owner_username)
    if owner is None:
        raise HTTPException(status_code=404, detail="Not Found")

    ap_object_id = f"{base}/actor/{username}/videos/{video_id}"
    federated = await repository.get_federated_event_by_ap_object(ap_object_id)
    if federated is None:
        raise HTTPException(status_code=404, detail="Not Found")

    if _prefers_html(request):
        video = await _build_video_view(request, channel=channel, owner=owner, federated=federated)
        if video is None:
            raise HTTPException(status_code=410, detail="Gone")
        # The watch page's own hit is one of the two signals the agreed
        # design counts a view from (the other is an inbound AP View
        # activity, see bridge.inbox_dispatch._handle_view), only here,
        # in the HTML branch: a plain JSON/AP fetch of this same URL (a
        # remote server resolving the object, not a human watching it) is
        # not a view. request.client can be None behind some proxy setups,
        # in which case there's no real per-viewer signal to dedup against
        # at all, so this just skips counting rather than false-sharing one
        # dedup bucket across every such visitor.
        if request.client is not None:
            await record_video_view(
                request.app.state.synapse, room_id=channel.room_id, local_video_id=video_id,
                dedup_key=request.client.host,
            )
        return HTMLResponse(render_video_page(video))

    reconstructed = await reconstruct_video_activities(
        request.app.state.synapse, base=base, channel=channel, owner=owner, federated=federated,
    )
    if reconstructed is None:
        raise HTTPException(status_code=410, detail="Gone")
    create_activity, _announce_activity = reconstructed
    video_dict = create_activity.to_dict()["object"]
    return _activity_json({"@context": VIDEO_JSON_LD_CONTEXT, **video_dict})


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
    record = await _get_actor_or_channel_record(request, username)
    repository: ActorRepository = request.app.state.repository
    followers = await repository.list_followers(username)
    # Owner hid the list (see bridge.identity_settings, ";set hide_followers
    # on"). The count stays real and public, only the member list is
    # withheld. "hide_followers" doesn't apply to a channel or a Lemmy
    # community identity (see the registry's applies_to), so neither's
    # list is ever hidden.
    identity_kind = (
        "channel" if isinstance(record, PeerTubeChannelRecord)
        else "community" if isinstance(record, LemmyCommunityRecord)
        else "group" if isinstance(record, GroupRecord)
        else "profile"
    )
    hidden = identity_kind == "profile" and await get_effective_setting(
        repository, username, identity_kind, "hide_followers"
    )
    items = [] if hidden else followers
    collection = OrderedCollection(
        id=followers_url(base, username), items=items, total_items=len(followers),
        embed_first=not isinstance(record, PeerTubeChannelRecord),
    )
    return _collection_or_page(request, collection)


@router.get("/following/{username}")
async def get_following(request: Request, username: str) -> JSONResponse:
    base = request.app.state.config.bridge.public_base_url
    record = await _get_actor_or_channel_record(request, username)
    repository: ActorRepository = request.app.state.repository
    following = await repository.list_following(username)
    identity_kind = (
        "channel" if isinstance(record, PeerTubeChannelRecord)
        else "community" if isinstance(record, LemmyCommunityRecord)
        else "group" if isinstance(record, GroupRecord)
        else "profile"
    )
    hidden = identity_kind == "profile" and await get_effective_setting(
        repository, username, identity_kind, "hide_following"
    )
    items = [] if hidden else following
    collection = OrderedCollection(
        id=following_url(base, username), items=items, total_items=len(following),
        embed_first=not isinstance(record, PeerTubeChannelRecord),
    )
    return _collection_or_page(request, collection)


@router.api_route("/moderators/{username}", methods=["GET", "HEAD"])
async def get_moderators(request: Request, username: str) -> JSONResponse:
    """A hosted Lemmy community's own real moderator list, as Lemmy's own
    ``attributedTo``-named collection convention (see
    ``bridge.lemmy_bridge``'s module docstring) -- this route is what that
    field actually points at. Permanently empty for anything that isn't a
    hosted community (a Profile or a PeerTube channel has no equivalent
    concept)."""
    base = request.app.state.config.bridge.public_base_url
    record = await _get_actor_or_channel_record(request, username)
    if not isinstance(record, LemmyCommunityRecord):
        collection = OrderedCollection(id=moderators_url(base, username), items=[], embed_first=False)
        return _collection_or_page(request, collection)

    collection = OrderedCollection(id=moderators_url(base, username), items=list(record.moderator_actor_ids))
    return _collection_or_page(request, collection)


@router.api_route("/featured/{username}", methods=["GET", "HEAD"])
async def get_featured(request: Request, username: str) -> JSONResponse:
    """A local actor's pinned posts, as Mastodon/Pleroma/Akkoma's own
    ``featured`` collection convention -- see ``bridge.pinned_posts`` for
    the OUTBOUND (Matrix pin -> Add/Remove to followers) half of this
    feature; this route and ``_fetch_featured_notes`` are the other half
    (AP collection reflecting current ``m.room.pinned_events`` state).

    Permanently empty for a PeerTube channel, a hosted Lemmy community, or
    a hosted Group (all ``Group``-typed actors) -- pinning is scoped to
    ordinary Person profiles only for v1, same as ``get_playlists``'s
    identical stub-for-the-wrong-actor-kind reasoning."""
    base = request.app.state.config.bridge.public_base_url
    record = await _get_actor_or_channel_record(request, username)
    if isinstance(record, (PeerTubeChannelRecord, LemmyCommunityRecord, GroupRecord)):
        collection = OrderedCollection(id=featured_url(base, username), items=[], embed_first=False)
        return _collection_or_page(request, collection)

    notes = await _fetch_featured_notes(request, record=record, base=base)
    collection = OrderedCollection(id=featured_url(base, username), items=[note.to_dict() for note in notes])
    return _collection_or_page(request, collection)


@router.get("/playlists/{username}")
async def get_playlists(request: Request, username: str) -> JSONResponse:
    """A PeerTube channel's playlists collection: permanently empty for
    v1 (see the agreed design in project_peertube_channels_scoping).
    Playlists are a fully separate, optional, curated-list feature,
    unrelated to video discovery (that's the channel's own outbox, see
    ``get_outbox`` above). Served at all, rather than 404ing, purely so
    a real PeerTube instance that checks for this collection's existence
    doesn't treat its absence as an error; only a Profile/channel username
    resolves here, same 404 as everywhere else for anything else."""
    record = await _get_actor_or_channel_record(request, username)
    base = request.app.state.config.bridge.public_base_url
    collection = OrderedCollection(
        id=f"{base}/playlists/{username}", items=[], embed_first=not isinstance(record, PeerTubeChannelRecord),
    )
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


_RUNNING_INBOX_ACTIVITIES: set = set()


def _process_activity_in_background(request: Request, username: str, activity: Activity, *, log_label: str) -> None:
    """Fire-and-forget wrapper for ``handle_activity`` -- see
    ``post_inbox``/``post_shared_inbox``'s own reasoning for why they
    return 202 before this even starts, rather than awaiting it. Same
    "strong reference until done" pattern as ``bridge.commands``'
    ``_RUNNING_BACKFILLS``, so the task isn't garbage-collected mid-flight.
    ``request`` is safe to keep using after the response has already been
    sent -- only ``request.app.state`` (a long-lived reference, not
    anything tied to this one HTTP exchange) is ever touched from here,
    same as every other backgrounded task in this codebase already does."""
    async def _run() -> None:
        try:
            await handle_activity(request, username, activity)
        except Exception:
            logger.exception("Error handling %s activity from %s for %s", activity.type, activity.actor, log_label)

    task = asyncio.get_running_loop().create_task(_run())
    _RUNNING_INBOX_ACTIVITIES.add(task)
    task.add_done_callback(_RUNNING_INBOX_ACTIVITIES.discard)


@router.post("/inbox/{username}")
async def post_inbox(request: Request, username: str) -> Response:
    await _get_actor_or_channel_record(request, username)
    activity = await _verify_and_parse_activity(request)
    # One INFO line per verified delivery -- the receive-side counterpart
    # of delivery logging. Successful processing is otherwise completely
    # silent, which made "did it never arrive, or did we silently drop
    # it?" unanswerable from the journal (2026-07-04, a vanished reply).
    logger.info("Inbox %s: %s from %s (object %s)", username, activity.type, activity.actor, activity.object_id())

    # Acknowledge immediately, once the signature's verified -- actually
    # bridging (Synapse calls, media fetches, ghost/room provisioning, ...)
    # runs in the background instead of being awaited here. Measured live
    # 2026-09-23: awaiting it inline meant the sender waited for ALL of
    # that before getting a response, and Pleroma/Akkoma give up and
    # retry at ~5s -- about 1 in 10 deliveries took 4.5s or more, so
    # events routinely arrived one or more retry backoffs late (median
    # 72s, p90 ~20min end to end). A 202 this fast removes that retry
    # cycle entirely; handle_activity's own per-activity activity_lock
    # (see its docstring) covers the resulting risk of a genuine
    # redelivery now running concurrently with the original instead of
    # queued safely behind a slow synchronous response.
    _process_activity_in_background(request, username, activity, log_label=username)
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

    # See post_inbox's identical reasoning for acking before processing.
    _process_activity_in_background(request, username, activity, log_label="shared inbox")
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


@router.api_route("/media/{server_name}/{media_id}", methods=["GET", "HEAD", "OPTIONS"])
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

    Also explicitly handles OPTIONS, answered directly here rather than via
    a global CORS middleware (this is the only route in the whole app that's
    ever fetched cross-origin by a browser rather than server-to-server, so
    scoping it here avoids touching CORS behavior anywhere else). A browser
    <video> element sends a CORS preflight OPTIONS before any cross-origin
    Range GET -- confirmed live 2026-07-25: this route previously had no
    OPTIONS handler at all, so that preflight got a bare 405 with no CORS
    headers, and the browser silently discarded the follow-up GET entirely.
    A remote PeerTube page loading one of this bridge's videos saw that as
    "no compatible source", even though the file itself, and a plain
    same-origin fetch of it, were both completely fine.
    """
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "Range",
                "Access-Control-Max-Age": "86400",
            },
        )

    # A video file's own URL carries a real file extension (see
    # bridge.peertube.video_media_url's docstring for why) that isn't part
    # of the underlying Matrix media id -- stripped back off here before
    # any lookup, recognized by suffix rather than a fixed set of video
    # extensions specifically, since this route serves every media type
    # (avatars, banners, thumbnails, attachments), not just videos.
    real_media_id = media_id.rsplit(".", 1)[0] if "." in media_id else media_id

    repository: ActorRepository = request.app.state.repository
    if not await repository.is_media_published(f"mxc://{server_name}/{real_media_id}"):
        raise HTTPException(status_code=404, detail="Media not found")

    try:
        result = await request.app.state.synapse.download_media(server_name, real_media_id)
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
