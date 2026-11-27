"""Poster-frame thumbnails for bridged videos.

Fediverse video attachments almost never come with anything a Matrix client
can show before playback: Pleroma/Akkoma send no thumbnail at all, and a
client left to grab a frame itself can't always decode the video (confirmed
live 2026-11-26: an H.264 High 4:4:4 MKV that no Android decoder supports
showed as a blank box in Haven Android). So the bridge, which already has
the bytes in hand to re-upload them, decodes a frame with ffmpeg -- which
handles any codec -- and uploads it as the event's ``thumbnail_url``, with
``thumbnail_info``, the video's ``size`` and its ``duration``.

Results are cached per video ``mxc://`` URI, so the same upload reused for
a repost card or a cross-bridge copy gets the same thumbnail without being
decoded again. Every failure (no ffmpeg in the image, an undecodable file, a
timeout) just leaves the event without a thumbnail, exactly as before.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
from collections import OrderedDict

from PIL import Image, UnidentifiedImageError

from bridge.synapse_client import SynapseClient, SynapseError

logger = logging.getLogger(__name__)

# Above this the video isn't decoded just for a thumbnail (it is already in
# memory for the upload, but writing a huge file out again for ffmpeg isn't
# worth it).
MAX_SOURCE_BYTES = 200 * 1024 * 1024
# Longest side of the poster frame, like Element's own thumbnails.
THUMBNAIL_MAX_SIDE = 800
_FFMPEG_TIMEOUT_SECONDS = 60
_CACHE_SIZE = 512

_cache: OrderedDict[str, dict] = OrderedDict()
_ffmpeg_missing = False


def cached_video_info(mxc_uri: str) -> dict | None:
    """The thumbnail/size/duration ``info`` fields already worked out for
    ``mxc_uri``, or None if it hasn't been processed (in this process)."""
    fields = _cache.get(mxc_uri)
    if fields is not None:
        _cache.move_to_end(mxc_uri)
    return fields


def remember_video_info(mxc_uri: str, fields: dict) -> None:
    _cache[mxc_uri] = fields
    _cache.move_to_end(mxc_uri)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)


_source_cache: OrderedDict[str, dict] = OrderedDict()


def cached_source_thumbnail(icon_url: str) -> dict | None:
    """``thumbnail_url``/``thumbnail_info`` already re-hosted from a
    source-provided thumbnail image at ``icon_url``, if any."""
    fields = _source_cache.get(icon_url)
    if fields is not None:
        _source_cache.move_to_end(icon_url)
    return fields


def remember_source_thumbnail(icon_url: str, fields: dict) -> None:
    _source_cache[icon_url] = fields
    _source_cache.move_to_end(icon_url)
    while len(_source_cache) > _CACHE_SIZE:
        _source_cache.popitem(last=False)


async def video_info_fields(synapse: SynapseClient, mxc_uri: str, data: bytes | None = None) -> dict:
    """``info`` fields to add to an ``m.video`` event for ``mxc_uri``:
    ``thumbnail_url``, ``thumbnail_info`` (w/h/mimetype/size), ``size`` and
    ``duration`` (ms) -- whichever could be worked out, possibly none.

    ``data`` is the video's bytes if the caller already has them (a fresh
    upload); otherwise they're downloaded back from Synapse, unless this
    video was already processed."""
    cached = cached_video_info(mxc_uri)
    if cached is not None:
        return dict(cached)
    if data is None:
        if not mxc_uri.startswith("mxc://") or "/" not in mxc_uri.removeprefix("mxc://"):
            return {}
        server_name, media_id = mxc_uri.removeprefix("mxc://").split("/", 1)
        try:
            data = (await synapse.download_media(server_name, media_id)).content
        except SynapseError:
            # Not cached: a later attempt may well succeed.
            logger.warning("Could not fetch %s to make a video thumbnail", mxc_uri, exc_info=True)
            return {}
    fields: dict = {"size": len(data)}
    if len(data) <= MAX_SOURCE_BYTES:
        fields.update(await _thumbnail_fields(synapse, mxc_uri, data))
    remember_video_info(mxc_uri, fields)
    return dict(fields)


async def _thumbnail_fields(synapse: SynapseClient, mxc_uri: str, data: bytes) -> dict:
    global _ffmpeg_missing
    if _ffmpeg_missing:
        return {}
    fields: dict = {}
    with tempfile.TemporaryDirectory(prefix="video-thumb-") as tmp:
        path = os.path.join(tmp, "video")
        try:
            await asyncio.to_thread(_write, path, data)
            duration = await _probe_duration(path)
            # A little way in rather than the very first frame, which is often
            # black (a fade-in), but never past the middle of a short clip.
            offset = min(1.0, duration / 2) if duration else 0.0
            jpeg = await _grab_frame(path, offset)
            if not jpeg and offset:
                jpeg = await _grab_frame(path, 0.0)
        except FileNotFoundError:
            _ffmpeg_missing = True
            logger.warning("ffmpeg isn't installed: bridged videos won't get thumbnails")
            return {}
        except (TimeoutError, OSError):
            logger.warning("Making a thumbnail for %s failed", mxc_uri, exc_info=True)
            return {}
    if duration:
        fields["duration"] = int(duration * 1000)
    if not jpeg:
        logger.info("No frame could be decoded from %s for a thumbnail", mxc_uri)
        return fields
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError):
        return fields
    try:
        thumbnail_mxc = await synapse.upload_media(jpeg, "image/jpeg", "thumbnail.jpg")
    except SynapseError:
        logger.warning("Uploading a thumbnail for %s failed", mxc_uri, exc_info=True)
        return fields
    fields["thumbnail_url"] = thumbnail_mxc
    fields["thumbnail_info"] = {"w": width, "h": height, "mimetype": "image/jpeg", "size": len(jpeg)}
    return fields


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _run(*args: str) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), _FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout


async def _probe_duration(path: str) -> float | None:
    code, out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path)
    if code != 0:
        return None
    try:
        duration = float(json.loads(out)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    return duration if duration > 0 else None


async def _grab_frame(path: str, offset: float) -> bytes:
    side = THUMBNAIL_MAX_SIDE
    code, out = await _run(
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{offset:.3f}", "-i", path,
        "-frames:v", "1", "-an",
        "-vf", f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
        "-pix_fmt", "yuvj420p", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
    )
    return out if code == 0 else b""
