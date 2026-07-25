"""Constants and small lookup helpers for the PeerTube-channel feature (see
``bridge.activitypub.models.Video`` and the ``project_peertube_channels_scoping``
memory for the full agreed design).

Category and licence names/numbering below were verified live 2026-07-25
against a real, default-configured instance's own
``/api/v1/videos/{categories,licences}`` endpoints (framatube.org) rather
than assumed from secondhand summaries; these are PeerTube's own fixed,
upstream-shipped defaults (customizable per-instance via plugins in
principle, but the only thing worth designing against for this bridge).
"""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import AS_PUBLIC, Activity, Actor, PublicKey, Video, VideoIdentifier
from bridge.activitypub.remote_actor import resolve_actor_inbox
from bridge.activitypub.sanitize import plain_text_to_note_html
from bridge.activitypub.urls import (
    actor_url,
    followers_url,
    following_url,
    inbox_url,
    main_key_id,
    media_url,
    outbox_url,
    shared_inbox_url,
)
from bridge.media import build_ap_attachment
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

VIDEO_CATEGORIES: dict[int, str] = {
    1: "Music",
    2: "Films",
    3: "Vehicles",
    4: "Art",
    5: "Sports",
    6: "Travels",
    7: "Gaming",
    8: "People",
    9: "Comedy",
    10: "Entertainment",
    11: "News & Politics",
    12: "How To",
    13: "Education",
    14: "Activism",
    15: "Science & Technology",
    16: "Animals",
    17: "Kids",
    18: "Food",
}

VIDEO_LICENCES: dict[int, str] = {
    1: "Attribution",
    2: "Attribution - Share Alike",
    3: "Attribution - No Derivatives",
    4: "Attribution - Non Commercial",
    5: "Attribution - Non Commercial - Share Alike",
    6: "Attribution - Non Commercial - No Derivatives",
    7: "Public Domain Dedication",
    8: "Free of known copyright restrictions",
    9: "All Rights Reserved",
}

# Language is a much bigger (100+), messier list than category/licence
# (mostly ISO 639-1 with ISO 639-3 fallbacks, plus oddities like sign
# languages and constructed languages), not worth hardcoding in full (see
# the scoping memory). ``;publish``/``;edit`` accept a raw code directly
# (``en``, ``fr``, ``ja``, ...) and pass it straight through as the AP
# ``identifier``, which is all PeerTube actually needs to function
# correctly; this is only a convenience subset for resolving a
# human-readable ``name`` on the common case. A code outside this dict is
# still perfectly valid input; ``resolve_language`` just returns no name
# for it, same as PeerTube's own behavior for a language it doesn't
# recognize either.
COMMON_LANGUAGES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "el": "Greek",
    "cs": "Czech",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "he": "Hebrew",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "fa": "Persian",
}


def format_video_uuid(local_video_id: str) -> str:
    """Reformat this bridge's own internal video id (``uuid.uuid4().hex``,
    the same 32-character hex string embedded in every published video's
    own URL and used as its ``VIDEO_METADATA_STATE_TYPE`` state key) into a
    standard dashed RFC 4122 UUID string, for ``Video.uuid`` (see that
    field's own docstring for why PeerTube requires it). Deriving it from
    the id already in hand, rather than minting and separately storing a
    second value, keeps it trivially stable across ``;edit``/``;replace
    video``, which both reuse the same ``local_video_id``."""
    return f"{local_video_id[0:8]}-{local_video_id[8:12]}-{local_video_id[12:16]}-{local_video_id[16:20]}-{local_video_id[20:32]}"


def resolve_category(name: str) -> int | None:
    """The numeric AP identifier for a category given by ``name`` (matched
    case-insensitively, e.g. as typed into ``;publish category: ...``), or
    ``None`` if it doesn't match one of PeerTube's own fixed 18."""
    lowered = name.strip().lower()
    for identifier, category_name in VIDEO_CATEGORIES.items():
        if category_name.lower() == lowered:
            return identifier
    return None


def resolve_licence(name: str) -> int | None:
    """Same as ``resolve_category``, for one of PeerTube's own fixed 9
    licences. Matches against both the real API wording (``"Attribution -
    Share Alike"``) and its unspaced hyphenated shorthand (``"Attribution-
    ShareAlike"``), since a user typing this by hand is more likely to
    reach for the shorter form."""
    lowered = name.strip().lower()
    for identifier, licence_name in VIDEO_LICENCES.items():
        if licence_name.lower() == lowered:
            return identifier
        if licence_name.replace(" - ", "-").replace(" ", "").lower() == lowered.replace(" ", ""):
            return identifier
    return None


def resolve_language(code: str) -> tuple[str, str | None]:
    """``(identifier, name)`` for a raw language code. ``identifier`` is
    always ``code`` itself (lowercased, passed straight through as the AP
    ``identifier`` unchanged, since PeerTube only actually needs that to
    function), ``name`` is a human-readable label when ``code`` is in
    ``COMMON_LANGUAGES``, else ``None``."""
    normalized = code.strip().lower()
    return normalized, COMMON_LANGUAGES.get(normalized)


# Custom Matrix state events this bridge writes on a channel ROOM, never
# rendered by any client since they're unrecognized types (see the agreed
# design in project_peertube_channels_scoping for why this is Matrix state
# rather than a new DB table). state_key is the video's own local id for
# both.
#
# VIDEO_METADATA_STATE_TYPE: the full current metadata snapshot for one
# video (name/category/licence/language/tags/description/sensitive/
# commentsEnabled/thumbnail_mxc), rewritten wholesale on every
# ";publish"/";edit"/";replace video".
VIDEO_METADATA_STATE_TYPE = "software.haven.activitypub.video.metadata"
# VIDEO_VIEWS_STATE_TYPE: a durable {"count": N} for one video, incremented
# from inbound remote ``View`` activities and hits on its own watch page.
VIDEO_VIEWS_STATE_TYPE = "software.haven.activitypub.video.views"
# VIDEO_INDEX_STATE_TYPE: state_key "" (one per room), content
# {"video_ids": [...]}, every CURRENTLY-published video's local id for this
# channel, newest first. Deliberately NOT reconstructed by scanning room
# history the way the ordinary Note-outbox does (see get_video_index and
# friends below). Confirmed live 2026-07-25, a Matrix state update is
# ALSO a new timeline event every time, always, with no way to update "in
# place", so VIDEO_VIEWS_STATE_TYPE being written on every single view
# silently flooded the room's own timeline and pushed a real published
# video's own message out of any shallow, most-recent-N timeline scan long
# before its view count could plausibly matter (13 view-count writes vs. 8
# real messages after a handful of test views was enough to do it). Only
# ";publish"/";unpublish" touch this index, never a view, so it can't
# suffer the same fate; reading it is one cheap GET regardless of how
# popular a video ever gets, rather than degrading further with every view.
VIDEO_INDEX_STATE_TYPE = "software.haven.activitypub.video.index"


async def get_video_index(synapse: Any, *, room_id: str) -> list[str]:
    try:
        content = await synapse.get_room_state(room_id, VIDEO_INDEX_STATE_TYPE, "")
    except SynapseError:
        return []
    video_ids = content.get("video_ids")
    return list(video_ids) if isinstance(video_ids, list) else []


async def add_to_video_index(synapse: Any, *, room_id: str, local_video_id: str) -> None:
    video_ids = await get_video_index(synapse, room_id=room_id)
    if local_video_id in video_ids:
        return
    await synapse.send_state_event(room_id, VIDEO_INDEX_STATE_TYPE, "", {"video_ids": [local_video_id, *video_ids]})


async def remove_from_video_index(synapse: Any, *, room_id: str, local_video_id: str) -> None:
    video_ids = await get_video_index(synapse, room_id=room_id)
    if local_video_id not in video_ids:
        return
    await synapse.send_state_event(
        room_id, VIDEO_INDEX_STATE_TYPE, "", {"video_ids": [v for v in video_ids if v != local_video_id]}
    )

# Basic mimetype allowlist, not real codec inspection: deliberately, to
# avoid adding a new dependency (e.g. ffprobe) to a codebase that has so far
# hand-rolled just enough media parsing to avoid that (see the agreed design).
VIDEO_MIMETYPE_ALLOWLIST = {
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
    "video/x-matroska",
}

# Fallback thumbnail dimensions for when the Matrix video message's own
# info.thumbnail_info block doesn't carry width/height (confirmed live
# 2026-07-25: not every client sends one, and a thumbnail set later via
# ";edit thumbnail: mxc://..." never has one at all, since it's just a bare
# mxc reference with nothing describing its own dimensions). PeerTube's own
# remote-video validator (setValidRemoteIcon) REQUIRES both as valid
# integers or it drops the icon entirely, and an empty icon list fails the
# whole video's import outright -- silently, with no error surfaced back to
# this bridge, exactly like Video.uuid's own docstring describes for a
# missing uuid. An approximate 16:9 default is far better than the video
# never federating at all; the real image is still served correctly at
# whatever its actual dimensions are, this only affects the size HINT
# PeerTube keys its import validation on.
DEFAULT_THUMBNAIL_WIDTH = 320
DEFAULT_THUMBNAIL_HEIGHT = 180

VIDEO_FILE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/ogg": "ogv",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
}


def video_media_url(url: str, media_type: str) -> str:
    """Append a real file extension to a video file's own
    ``/media/{server}/{id}`` URL, matching ``media_type``.

    PeerTube's own web player (WebVideoPlugin, confirmed by reading their
    real source 2026-07-25) calls ``player.src()`` with a bare URL STRING,
    not a ``{src, type}`` pair, so video.js has to guess the playable type
    from the URL's own file extension. This bridge's media URLs are opaque
    Matrix media ids with no extension at all -- the same "nothing to infer
    a type from" problem ``Video.uuid``/the thumbnail-dimensions fallback
    above describe for PeerTube's import-side validators, just showing up
    client-side in the player instead of server-side on import. Without
    this, video.js can't determine ANY playable type and refuses to even
    attempt loading the source (confirmed live: no network request for the
    video file at all, "no compatible source" for both an mp4 and a webm
    upload, even after import itself succeeded and the file served
    correctly to a plain fetch). ``bridge.activitypub.routes.get_media``
    strips this same suffix back off before resolving the underlying
    Matrix media id."""
    ext = VIDEO_FILE_EXTENSIONS.get(media_type, "mp4")
    return f"{url}.{ext}"


def parse_key_value_command(body: str) -> tuple[dict[str, str], str]:
    """Parse the shared ``;publish``/``;edit`` body shape: a ``key: value``
    header block, then a blank line, then free-form description text
    (everything after the blank line, however many lines). Matches the
    git-commit/email-header convention on purpose (see the agreed command
    syntax in project_peertube_channels_scoping).

    ``body`` is the FULL Matrix message body, including its own leading
    ``;publish``/``;edit`` line: that first line is always discarded
    outright (whatever else is on it), with the header block starting on
    line 2, matching the documented example shape exactly:

    .. code-block::

        ;publish
        name: My Cool Video
        category: Comedy

        Description text here.

    Returns ``(fields, description)``. ``fields`` is keyed lowercase (only
    lines shaped like ``key: value`` are consumed as fields; the first line
    that ISN'T contributes nothing further and, if no blank line was seen
    yet, becomes part of an empty header block with no description).
    ``description`` is ``""`` when there was no blank line at all (no
    description section, not even an empty one)."""
    lines = body.split("\n")[1:]  # drop the ";publish"/";edit" line itself
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            break
        key, sep, value = line.partition(":")
        if not sep:
            break  # not a "key: value" line; header block ends here
        fields[key.strip().lower()] = value.strip()
        i += 1
    description = "\n".join(lines[i:]).strip()
    return fields, description


# One counted view per (video_id, dedup_key) within this window. dedup_key
# is the sending actor's own id for an inbound AP ``View`` activity, or the
# viewer's request IP for an anonymous watch-page hit (see
# record_video_view's own docstring for why those are the right key for
# each signal respectively; there's no single shared identity concept
# between "a server told us" and "a browser hit our page"). Same
# restart-loses-it-and-that's-fine in-memory pattern already used elsewhere
# in this bridge (bridge.reaction_bridge's own repost-card dedup).
_VIEW_DEDUP_WINDOW_SECONDS = 4 * 60 * 60
_recent_video_views: dict[tuple[str, str], float] = {}


def _should_count_view(video_id: str, dedup_key: str) -> bool:
    key = (video_id, dedup_key)
    now = time.monotonic()
    last_seen = _recent_video_views.get(key)
    if last_seen is not None and now - last_seen < _VIEW_DEDUP_WINDOW_SECONDS:
        return False
    _recent_video_views[key] = now
    return True


async def record_video_view(synapse: Any, *, room_id: str, local_video_id: str, dedup_key: str) -> None:
    """Increment the durable view count for a video: a ``{"count": N}``
    Matrix state event on its channel room (``VIDEO_VIEWS_STATE_TYPE``, see
    the agreed design in project_peertube_channels_scoping), unless
    ``dedup_key`` was already seen for this exact video within the last
    ``_VIEW_DEDUP_WINDOW_SECONDS``. Called from two places: an inbound AP
    ``View`` activity (``bridge.inbox_dispatch``, ``dedup_key`` = the
    sending actor's own id) and a hit on the video's own public watch page
    (``bridge.activitypub.routes``, ``dedup_key`` = the request's own IP).
    In-Matrix inline playback is never counted here at all; no
    server-visible signal exists for it, an accepted, real capability gap
    rather than a design choice."""
    if not _should_count_view(local_video_id, dedup_key):
        return
    try:
        current = await synapse.get_room_state(room_id, VIDEO_VIEWS_STATE_TYPE, local_video_id)
        count = int(current.get("count", 0))
    except SynapseError:
        count = 0
    await synapse.send_state_event(room_id, VIDEO_VIEWS_STATE_TYPE, local_video_id, {"count": count + 1})


def _matrix_ts_to_iso(origin_server_ts: int | None) -> str:
    if not origin_server_ts:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def reconstruct_video_activities(
    synapse: Any, *, base: str, channel: Any, owner: Any, federated: Any,
) -> tuple[Activity, Activity] | None:
    """Rebuild the Create+Announce activity pair for an already-published
    video, straight from its own Matrix message (media info) and its
    ``VIDEO_METADATA_STATE_TYPE`` state event (name/category/etc.), the
    same two sources ``;edit``/``;replace video`` already read from (see
    ``bridge.commands``). Used by the channel outbox (``bridge.activitypub.
    routes.get_outbox``) to reconstruct every currently-published video
    fresh on every fetch, matching PeerTube's own real two-actor
    choreography (a ``Create`` from the owning account, an ``Announce``
    from the channel, both referencing the same ``Video``, verified
    during scoping) rather than this bridge's own bare-object outbox
    convention used for ordinary Profile Room posts. Returns ``None`` if
    either source is currently unavailable (e.g. the video message itself
    was since redacted, or its state event was somehow never written)."""
    try:
        video_event = await synapse.get_event(federated.room_id, federated.event_id)
    except SynapseError:
        return None
    video_content = video_event.get("content") or {}
    attachment = build_ap_attachment(base, video_content)
    if attachment is None:
        return None

    local_video_id = federated.ap_object_id.rsplit("/", 1)[-1]
    try:
        existing = await synapse.get_room_state(channel.room_id, VIDEO_METADATA_STATE_TYPE, local_video_id)
    except SynapseError:
        return None
    name = (existing.get("name") or "").strip()
    if not name:
        return None

    info = video_content.get("info") or {}
    duration_ms = info.get("duration")
    duration_seconds = (
        round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None
    )

    category_name = existing.get("category")
    category = None
    if category_name:
        category_id = resolve_category(category_name)
        category = VideoIdentifier(str(category_id), category_name) if category_id is not None else None
    licence_name = existing.get("license")
    licence = None
    if licence_name:
        licence_id = resolve_licence(licence_name)
        licence = VideoIdentifier(str(licence_id), licence_name) if licence_id is not None else None
    language_code = existing.get("language")
    language = VideoIdentifier(*resolve_language(language_code)) if language_code else None
    thumbnail_mxc = existing.get("thumbnail_mxc")
    icon_url = None
    if thumbnail_mxc:
        try:
            icon_url = media_url(base, thumbnail_mxc)
        except ValueError:
            icon_url = None

    owner_actor_id = actor_url(base, owner.username)
    channel_actor_id = actor_url(base, channel.username)
    published_at = existing.get("published") or _matrix_ts_to_iso(video_event.get("origin_server_ts"))

    video = Video(
        id=federated.ap_object_id,
        uuid=format_video_uuid(local_video_id),
        name=name,
        attributed_to=[{"type": "Person", "id": owner_actor_id}, {"type": "Group", "id": channel_actor_id}],
        published=published_at,
        # No separate "last edited" timestamp is persisted anywhere, but
        # this can never be left unset -- PeerTube's own remote-video
        # validator requires "updated" to be a valid date unconditionally
        # (see bridge.commands._handle_publish's identical comment on why).
        # published_at is the closest available stand-in and never
        # overstates recency.
        updated=published_at,
        url_html=federated.ap_object_id,
        media_url=video_media_url(attachment["url"], attachment.get("mediaType") or "video/mp4"),
        media_type=attachment.get("mediaType") or "video/mp4",
        duration_seconds=duration_seconds,
        media_size=info.get("size"),
        media_width=info.get("w"),
        media_height=info.get("h"),
        icon_url=icon_url,
        icon_width=DEFAULT_THUMBNAIL_WIDTH,
        icon_height=DEFAULT_THUMBNAIL_HEIGHT,
        content=existing.get("description") or None,
        category=category,
        licence=licence,
        language=language,
        tags=list(existing.get("tags") or []),
        sensitive=bool(existing.get("sensitive", False)),
        comments_enabled=bool(existing.get("commentsEnabled", True)),
        to=[AS_PUBLIC, channel_actor_id],
        cc=[followers_url(base, owner.username)],
        audience=channel_actor_id,
    )
    # Deterministic ids (not a fresh uuid, unlike ;publish's own one-off
    # send): this reconstruction runs on every outbox fetch, and a stable
    # id here is what lets a remote server recognize it's the same Create/
    # Announce it may have already seen, not a new one each time.
    create_activity = Activity(
        id=f"{federated.ap_object_id}/activity", type="Create", actor=owner_actor_id, object=video,
        published=published_at, to=video.to, cc=video.cc,
    )
    announce_activity = Activity(
        id=f"{federated.ap_object_id}/announces/outbox", type="Announce", actor=channel_actor_id,
        object=federated.ap_object_id, published=published_at, to=video.to, cc=video.cc,
    )
    return create_activity, announce_activity


_ICON_MEDIA_TYPE_CACHE: dict[str, str] = {}


async def resolve_media_type(request: Any, public_media_url: str | None) -> str | None:
    """Best-effort content-type lookup for one of this bridge's own
    ``/media/{server}/{id}`` URLs (an avatar or banner), cached in-memory
    since an actor document is among the most frequently-fetched things
    this bridge serves, but avatars/banners change rarely -- see
    ``Actor.icon_media_type``'s own docstring for why this is needed at
    all. Safe to cache indefinitely by URL: a given Matrix media id is
    immutable, so an actual avatar change always produces a brand new
    URL rather than reusing this one with different content. None on any
    failure -- every caller already treats a missing mediaType as an
    acceptable (if PeerTube-unfriendly) degraded case, matching how a
    missing avatar was already handled before this fix existed.

    Lives here rather than in bridge.activitypub.routes (where the actor
    document is actually served from) so push_channel_update below, which
    builds its own Actor inline to avoid an import cycle with that module
    (see its own docstring), can share the exact same cache instead of
    silently going without a mediaType on every avatar-change push."""
    if not public_media_url:
        return None
    if public_media_url in _ICON_MEDIA_TYPE_CACHE:
        return _ICON_MEDIA_TYPE_CACHE[public_media_url]
    try:
        _prefix, server_name, media_id = public_media_url.rsplit("/", 2)
    except ValueError:
        return None
    try:
        result = await request.app.state.synapse.download_media(server_name, media_id)
    except SynapseError:
        return None
    _ICON_MEDIA_TYPE_CACHE[public_media_url] = result.content_type
    return result.content_type


async def push_channel_update(request: Any, channel: Any) -> None:
    """Send a signed ``Update(Group)`` (carrying ``channel``'s CURRENT
    display name/summary/avatar/banner, built the same way
    ``GET /actor/{username}`` itself would for a channel, see
    ``bridge.activitypub.routes._build_channel_actor``) to every one of the
    channel's own followers. Same "an already-cached copy on a follower's
    server otherwise silently goes stale" reasoning as
    ``bridge.note_mirroring.push_profile_update``, just for a PeerTube
    channel's own ``Group`` actor instead of an ordinary ``Person``
    profile. Builds its own ``Actor`` here rather than calling
    ``_build_channel_actor`` directly, for the same reason
    ``push_profile_update`` itself does that: ``bridge.activitypub.routes``
    already imports plenty from this module, so importing back from here
    would be a real cycle."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository = request.app.state.repository

    actor = Actor(
        id=actor_url(base, channel.username),
        type="Group",
        preferred_username=channel.username,
        name=channel.display_name or channel.username,
        summary=plain_text_to_note_html(channel.summary) if channel.summary else channel.summary,
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
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_activity = Activity(
        id=f"{actor.id}/updates/{uuid.uuid4().hex}",
        type="Update",
        actor=actor.id,
        object=actor.to_dict(),
        published=published,
        to=[AS_PUBLIC],
        cc=[followers_url(base, channel.username)],
    )
    followers = await repository.list_followers(channel.username)
    for follower_actor_id in followers:
        inbox = await resolve_actor_inbox(request, follower_actor_id)
        if inbox is None:
            logger.warning("No inbox known for %s; skipping channel-update delivery", follower_actor_id)
            continue
        try:
            await deliver_activity(
                request.app.state.http_client,
                inbox_url=inbox,
                activity=update_activity.to_dict(),
                key_id=main_key_id(base, channel.username),
                private_key_pem=channel.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver channel update to %s", follower_actor_id, exc_info=True)


async def maybe_handle_channel_topic_change(request: Any, event: dict) -> bool:
    """Returns True if this event was an ``m.room.topic`` change in a
    CHANNEL room (handled: keeps the channel's AP ``summary`` in sync with
    it, and pushes the change to its followers). Same trust model as the
    ordinary Profile Room equivalent
    (``bridge.profile_posts.maybe_handle_topic_change``): Matrix's own
    power levels already gated who could set it."""
    if event.get("type") != "m.room.topic":
        return False
    content = event.get("content") or {}
    topic = content.get("topic")
    if not isinstance(topic, str):
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    channel = await repository.get_peertube_channel_by_room_id(room_id)
    if channel is None:
        return False  # not a channel room

    if channel.summary != topic:
        channel = dataclasses.replace(channel, summary=topic)
        await repository.register_peertube_channel(channel)
        await push_channel_update(request, channel)
    return True


async def maybe_handle_channel_name_change(request: Any, event: dict) -> bool:
    """Same as ``maybe_handle_channel_topic_change``, for ``m.room.name``."""
    if event.get("type") != "m.room.name":
        return False
    content = event.get("content") or {}
    name = content.get("name")
    if not isinstance(name, str) or not name:
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    channel = await repository.get_peertube_channel_by_room_id(room_id)
    if channel is None:
        return False

    if channel.display_name != name:
        channel = dataclasses.replace(channel, display_name=name)
        await repository.register_peertube_channel(channel)
        await push_channel_update(request, channel)
    return True


async def maybe_handle_channel_avatar_change(request: Any, event: dict) -> bool:
    """Same as ``maybe_handle_channel_topic_change``, for ``m.room.avatar``.
    The new avatar is published through the bridge's own media proxy (see
    ``bridge.activitypub.routes.get_media``) before it's ever handed out as
    an ``icon_url``, same reasoning as the ordinary Profile Room
    equivalent, ``bridge.profile_posts.maybe_handle_room_avatar_change``."""
    if event.get("type") != "m.room.avatar":
        return False
    content = event.get("content") or {}
    avatar_mxc = content.get("url")
    if not isinstance(avatar_mxc, str) or not avatar_mxc:
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository = request.app.state.repository
    channel = await repository.get_peertube_channel_by_room_id(room_id)
    if channel is None:
        return False

    config = request.app.state.config
    try:
        icon_url = media_url(config.bridge.public_base_url, avatar_mxc)
    except ValueError:
        logger.info("Room avatar %r for channel %s is not an mxc:// URI; ignoring", avatar_mxc, room_id)
        return True

    if channel.icon_url != icon_url:
        await repository.mark_media_published(avatar_mxc)
        channel = dataclasses.replace(channel, icon_url=icon_url)
        await repository.register_peertube_channel(channel)
        await push_channel_update(request, channel)
    return True
