"""Helpers for getting remote media into Synapse's media repository as an
``mxc://`` URI, and for converting between AP and Matrix media conventions.

Used to mirror a remote ActivityPub actor's avatar, and the attachments on
their posts, locally: the bridge re-hosts the bytes via Synapse rather than
storing them itself, consistent with the project's data-sovereignty
constraint -- no media lives anywhere but Matrix.

``build_ap_attachment`` is the single place that turns a Matrix media
message into an AP attachment dict, used both for live distribution
(``bridge.profile_posts``) and outbox history reconstruction
(``bridge.activitypub.routes``) so the two can't drift.
"""

from __future__ import annotations

import logging
import mimetypes

import httpx

from bridge.activitypub.urls import media_url
from bridge.synapse_client import SynapseClient, SynapseError

logger = logging.getLogger(__name__)

_MATRIX_MSGTYPE_TO_AP_TYPE = {
    "m.image": "Image",
    "m.video": "Video",
    "m.audio": "Audio",
    "m.file": "Document",
}

# Generic/unhelpful Content-Types some remote servers serve media with
# regardless of what the file actually is (e.g. an avatar uploaded without
# an extension, so the server has nothing to guess from either) -- seen in
# the wild on a Pleroma instance serving a real PNG avatar as this. Trusted
# at face value, this would upload to Synapse (and get served back) the
# same generic way, and most Matrix clients -- Element included -- refuse
# to render something as an image/video regardless of its actual bytes
# unless the media repo's own Content-Type says so, silently blanking out
# an avatar or attachment that's genuinely a real image/video underneath.
_UNHELPFUL_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream", ""}


def _sniff_content_type(data: bytes) -> str | None:
    """Best-effort magic-byte detection for the common web image/video
    formats, used as a fallback only when the server's own Content-Type
    is one of ``_UNHELPFUL_CONTENT_TYPES`` -- deliberately narrow (just
    enough to cover what turns up in practice) rather than a general
    file-type sniffing library, to avoid a new dependency for this."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


async def _download_media(http_client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        response = await http_client.get(url)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Failed to fetch remote media %s", url, exc_info=True)
        return None
    return response


def _resolved_content_type(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "").split(";")[0].strip() or "application/octet-stream"
    if content_type in _UNHELPFUL_CONTENT_TYPES:
        content_type = _sniff_content_type(response.content) or content_type
    return content_type


async def _upload_media(synapse: SynapseClient, response: httpx.Response, url: str, content_type: str) -> str | None:
    filename = url.rsplit("/", 1)[-1] or "file"
    try:
        return await synapse.upload_media(response.content, content_type, filename)
    except SynapseError:
        logger.warning("Failed to upload remote media %s", url, exc_info=True)
        return None


async def fetch_and_upload_media(
    http_client: httpx.AsyncClient, synapse: SynapseClient, url: str
) -> str | None:
    """Download a remote file and re-upload it to Synapse's media repo. Returns its ``mxc://`` URI."""
    response = await _download_media(http_client, url)
    if response is None:
        return None
    content_type = _resolved_content_type(response)
    return await _upload_media(synapse, response, url, content_type)


async def fetch_and_upload_media_with_dimensions(
    http_client: httpx.AsyncClient, synapse: SynapseClient, url: str
) -> tuple[str, int | None, int | None] | None:
    """Like ``fetch_and_upload_media``, but also returns ``(width, height)``
    for a video file it can determine them for -- ``(None, None)`` for
    anything else, or if they couldn't be determined. Pleroma's federated
    attachment objects don't carry a video's dimensions at all (confirmed
    against a real one), so a Matrix client has nothing to size a player
    with unless we work them out ourselves -- done here, from the same
    bytes already being uploaded, rather than a second fetch.

    Returns None (not a 3-tuple) if the fetch/upload itself failed --
    ``fetch_and_upload_media``'s simpler ``str | None`` isn't reused for
    this because most callers (avatars, banners) never need dimensions and
    shouldn't have to unpack a tuple to get the one thing they want.
    """
    response = await _download_media(http_client, url)
    if response is None:
        return None
    content_type = _resolved_content_type(response)
    mxc_uri = await _upload_media(synapse, response, url, content_type)
    if mxc_uri is None:
        return None

    width = height = None
    if content_type == "video/mp4":
        dimensions = _mp4_video_dimensions(response.content)
    elif content_type == "video/webm":
        dimensions = _webm_video_dimensions(response.content)
    else:
        dimensions = None
    if dimensions is not None:
        width, height = dimensions
    return mxc_uri, width, height


def _mp4_video_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort width/height from an MP4/QuickTime container's first
    video track -- read directly from its ``moov/trak/tkhd`` box (a fixed
    ISOBMFF layout: version+flags, then times/track_ID/duration, then
    layer/volume/matrix, then width/height as 16.16 fixed-point) rather
    than the more precise but more deeply-nested ``stsd`` sample entry --
    enough to stop a Matrix client guessing a wrong aspect ratio, not a
    full moov parser. An audio-only track's ``tkhd`` has width=height=0,
    so those are skipped in favor of the first track that has both.
    """

    def iter_boxes(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(data[pos : pos + 4], "big")
            box_type = data[pos + 4 : pos + 8]
            header_size = 8
            if size == 1:
                if pos + 16 > end:
                    return
                size = int.from_bytes(data[pos + 8 : pos + 16], "big")
                header_size = 16
            elif size == 0:
                size = end - pos
            if size < header_size or pos + size > end:
                return
            yield box_type, pos + header_size, pos + size
            pos += size

    def find_box(start: int, end: int, target: bytes) -> tuple[int, int] | None:
        for box_type, cstart, cend in iter_boxes(start, end):
            if box_type == target:
                return cstart, cend
        return None

    moov = find_box(0, len(data), b"moov")
    if moov is None:
        return None

    for box_type, trak_start, trak_end in iter_boxes(*moov):
        if box_type != b"trak":
            continue
        tkhd = find_box(trak_start, trak_end, b"tkhd")
        if tkhd is None:
            continue
        t_start, t_end = tkhd
        if t_end <= t_start:
            continue
        version = data[t_start]
        # version 0 uses 4-byte times/duration, version 1 uses 8-byte --
        # everything after that (layer..matrix) is the same fixed size.
        fixed_fields_size = 72 if version == 0 else 84
        width_off = t_start + 4 + fixed_fields_size
        if width_off + 8 > t_end:
            continue
        width = int.from_bytes(data[width_off : width_off + 4], "big") >> 16
        height = int.from_bytes(data[width_off + 4 : width_off + 8], "big") >> 16
        if width and height:
            return width, height
    return None


def _webm_video_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort PixelWidth/PixelHeight from a WebM/Matroska container's
    first video track -- an EBML document, structured very differently
    from MP4's fixed-field boxes (variable-length IDs and sizes), but the
    same "just enough to stop Element guessing an aspect ratio" scope as
    ``_mp4_video_dimensions``.
    """

    def vint_length(first_byte: int) -> int:
        for i in range(8):
            if first_byte & (0x80 >> i):
                return i + 1
        return 0

    def read_id(pos: int) -> tuple[int, int] | None:
        if pos >= len(data):
            return None
        length = vint_length(data[pos])
        if length == 0 or pos + length > len(data):
            return None
        return int.from_bytes(data[pos : pos + length], "big"), length

    def read_size(pos: int) -> tuple[int | None, int] | None:
        if pos >= len(data):
            return None
        first = data[pos]
        length = vint_length(first)
        if length == 0 or pos + length > len(data):
            return None
        value = first & (0xFF >> length)
        for b in data[pos + 1 : pos + length]:
            value = (value << 8) | b
        if value == (1 << (7 * length)) - 1:
            return None, length  # "unknown size" marker -- not handled
        return value, length

    def find_child(start: int, end: int, target_id: int) -> tuple[int, int] | None:
        pos = start
        while pos < end:
            id_result = read_id(pos)
            if id_result is None:
                return None
            elem_id, id_len = id_result
            size_result = read_size(pos + id_len)
            if size_result is None:
                return None
            size, size_len = size_result
            content_start = pos + id_len + size_len
            if size is None:
                return None
            content_end = content_start + size
            if content_end > end:
                return None
            if elem_id == target_id:
                return content_start, content_end
            pos = content_end
        return None

    ID_SEGMENT = 0x18538067
    ID_TRACKS = 0x1654AE6B
    ID_TRACKENTRY = 0xAE
    ID_VIDEO = 0xE0
    ID_PIXEL_WIDTH = 0xB0
    ID_PIXEL_HEIGHT = 0xBA

    segment = find_child(0, len(data), ID_SEGMENT)
    if segment is None:
        return None
    tracks = find_child(*segment, ID_TRACKS)
    if tracks is None:
        return None

    tr_start, tr_end = tracks
    pos = tr_start
    while pos < tr_end:
        id_result = read_id(pos)
        if id_result is None:
            return None
        elem_id, id_len = id_result
        size_result = read_size(pos + id_len)
        if size_result is None:
            return None
        size, size_len = size_result
        content_start = pos + id_len + size_len
        if size is None:
            return None
        content_end = content_start + size
        if content_end > tr_end:
            return None
        if elem_id == ID_TRACKENTRY:
            video = find_child(content_start, content_end, ID_VIDEO)
            if video is not None:
                width_box = find_child(*video, ID_PIXEL_WIDTH)
                height_box = find_child(*video, ID_PIXEL_HEIGHT)
                if width_box is not None and height_box is not None:
                    width = int.from_bytes(data[width_box[0] : width_box[1]], "big")
                    height = int.from_bytes(data[height_box[0] : height_box[1]], "big")
                    if width and height:
                        return width, height
        pos = content_end
    return None


def filename_with_extension(filename: str, media_type: str) -> str:
    """Append an extension guessed from ``media_type`` if ``filename``
    doesn't already have one.

    Needed because a Mastodon-family attachment's ``name`` field is
    actually alt text/an accessibility description, not a real filename --
    used as the base filename anyway (see
    ``bridge.note_mirroring.merge_attachment_into_content``) since it's
    the more human-readable of the two, but it commonly has no extension
    at all (e.g. "Secret Tip"). Element (and other clients) rely on a
    filename's own extension, not just the separate ``info.mimetype``, to
    decide whether/how to render an attachment inline -- without one, a
    genuine image can silently fail to preview as one at all."""
    if "." in filename:
        return filename
    ext = mimetypes.guess_extension(media_type) or ""
    return f"{filename}{ext}"


def matrix_msgtype_for_mimetype(mimetype: str) -> str:
    """Map a MIME type to the Matrix ``msgtype`` to use for a media message."""
    if mimetype.startswith("image/"):
        return "m.image"
    if mimetype.startswith("video/"):
        return "m.video"
    if mimetype.startswith("audio/"):
        return "m.audio"
    return "m.file"


def media_caption(content: dict) -> str:
    """A media message's genuine caption text, or ``""`` when its ``body``
    is just the filename.

    Matrix's caption convention (MSC2530, long since part of the spec):
    when a media event carries a separate ``filename`` field that differs
    from ``body``, ``body`` is a real caption -- Element X posts an
    image-with-caption exactly this way. Without a ``filename`` (or when
    the two match), ``body`` IS the filename and shouldn't be treated as
    post text. Every outbound federation path (post/reply/DM/edit) and the
    serving reconstruction must use this instead of blanking a media
    message's body outright -- confirmed live (2026-07-04): an Element X
    image+caption post federated with its caption silently dropped, since
    the old ``body = "" if attachment`` logic predates caption support.
    The inbound mirror direction already handles this shape (see
    ``bridge.inbox_dispatch._fetch_post_preview``'s identical distinction).
    """
    body = (content.get("body") or "").strip()
    filename = (content.get("filename") or "").strip()
    if not filename or body == filename:
        return ""
    return body


def build_ap_attachment(base_url: str, content: dict) -> dict | None:
    """Build an AP attachment dict from a Matrix media message's ``content``,
    or ``None`` if it isn't a recognized media message.

    The returned ``url`` points at the bridge's own ``/media/{server}/{id}``
    proxy. Building this dict does not by itself make the media fetchable --
    callers that intend to actually publish it must also call
    ``ActorRepository.mark_media_published`` with the same ``mxc://`` URI, or
    the proxy will correctly refuse to serve it.
    """
    mxc_url = content.get("url")
    ap_type = _MATRIX_MSGTYPE_TO_AP_TYPE.get(content.get("msgtype", ""))
    if not ap_type or not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
        return None
    try:
        public_url = media_url(base_url, mxc_url)
    except ValueError:
        return None
    attachment = {"type": ap_type, "url": public_url}
    mimetype = (content.get("info") or {}).get("mimetype")
    if mimetype:
        attachment["mediaType"] = mimetype
    return attachment
