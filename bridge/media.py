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

import base64
import hashlib
import html
import logging
import mimetypes

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Request

from bridge.activitypub.urls import media_url, resolve_own_media_proxy_mxc
from bridge.repository import ActorRepository
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


_DIMENSIONS_PARSER_BY_CONTENT_TYPE = {}  # populated below, once the parsers themselves are defined


async def fetch_and_upload_media_with_dimensions(
    http_client: httpx.AsyncClient, synapse: SynapseClient, url: str, *, public_base_url: str | None = None
) -> tuple[str, int | None, int | None] | None:
    """Like ``fetch_and_upload_media``, but also returns ``(width, height)``
    for an image or video file it can determine them for -- ``(None,
    None)`` for anything else, or if they couldn't be determined.
    Pleroma's federated attachment objects don't carry a video's (or an
    image's) dimensions at all (confirmed against real ones), so a Matrix
    client has nothing to size a player/image with unless we work them out
    ourselves -- done here, from the same bytes already being uploaded,
    rather than a second fetch. Hand-rolled per-format header parsing
    (like the rest of this module's media handling) rather than a new
    image-processing dependency, since only the fixed-size dimension
    fields are needed, not real decoding.

    ``public_base_url``, if given, lets this recognize ``url`` as our OWN
    media proxy link (see ``resolve_own_media_proxy_mxc``) -- e.g. reposting
    a LOCAL user's post carries an attachment URL pointing right back at
    this bridge -- and reuse that already-uploaded ``mxc://`` directly
    (pulling bytes straight from Synapse's own authenticated media API,
    via ``SynapseClient.download_media``, just to determine dimensions)
    instead of round-tripping through our own public endpoint and
    re-uploading a brand new, wasteful, un-deduplicated copy of a file
    already on this homeserver (confirmed live 2026-07-10: reposting the
    same local post from N different remote accounts produced N distinct
    mxc:// copies of the identical video).

    Returns None (not a 3-tuple) if the fetch/upload itself failed --
    ``fetch_and_upload_media``'s simpler ``str | None`` isn't reused for
    this because most callers (avatars, banners) never need dimensions and
    shouldn't have to unpack a tuple to get the one thing they want.
    """
    own_mxc = resolve_own_media_proxy_mxc(public_base_url, url) if public_base_url else None
    if own_mxc:
        server_name, media_id = own_mxc.removeprefix("mxc://").split("/", 1)
        try:
            download = await synapse.download_media(server_name, media_id)
        except SynapseError:
            # Still worth reusing the mxc even if we couldn't re-fetch it
            # ourselves just now to work out dimensions -- better than
            # falling through to an unnecessary, un-deduplicated re-upload.
            logger.warning("Could not re-fetch own media %s for dimensions", own_mxc, exc_info=True)
            return own_mxc, None, None
        width, height = _dimensions_with_fallback(download.content, download.content_type)
        return own_mxc, width, height

    response = await _download_media(http_client, url)
    if response is None:
        return None
    content_type = _resolved_content_type(response)
    mxc_uri = await _upload_media(synapse, response, url, content_type)
    if mxc_uri is None:
        return None

    width, height = _dimensions_with_fallback(response.content, content_type)
    return mxc_uri, width, height


def _dimensions_with_fallback(data: bytes, content_type: str) -> tuple[int | None, int | None]:
    """Width/height for ``data``, trying ``content_type``'s own parser
    first and falling back to magic-byte sniffing if that comes up empty
    -- not just when ``content_type`` is one of ``_UNHELPFUL_CONTENT_TYPES``
    the way ``_resolved_content_type`` itself falls back, but ANY time the
    declared type's own parser found nothing, including a real, specific,
    just plain WRONG one. Confirmed live 2026-07-15: a real clew.lol video
    was served (both via its HTTP Content-Type header AND its filename)
    labeled ``video/webm``, while its actual bytes were a genuine MP4--
    the WebM parser naturally found nothing in real MP4 bytes, silently
    dropping the dimensions entirely, even though the file's own magic
    bytes unambiguously said MP4 the whole time. Never touches what gets
    uploaded/labeled as -- purely a second attempt at dimensions, using
    whichever parser the ACTUAL bytes call for instead of whichever the
    server happened to claim."""
    parser = _DIMENSIONS_PARSER_BY_CONTENT_TYPE.get(content_type)
    dimensions = parser(data) if parser else None
    if dimensions is None:
        sniffed_type = _sniff_content_type(data)
        if sniffed_type and sniffed_type != content_type:
            sniffed_parser = _DIMENSIONS_PARSER_BY_CONTENT_TYPE.get(sniffed_type)
            dimensions = sniffed_parser(data) if sniffed_parser else None
    return dimensions if dimensions is not None else (None, None)


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


def _png_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height from a PNG's mandatory-first IHDR chunk -- a fixed
    offset right after the 8-byte signature, no scanning needed."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _gif_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height from a GIF's fixed-layout logical screen descriptor,
    right after the 6-byte "GIF87a"/"GIF89a" signature."""
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return (width, height) if width and height else None


def _jpeg_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height from a JPEG's first SOFn (start-of-frame) marker.
    Unlike PNG/GIF this needs to scan the marker segments (each
    self-describes its own length) rather than read a fixed offset, since
    JPEG allows an arbitrary run of other segments (APPn/EXIF,
    quantization/Huffman tables, ...) before the SOFn that carries the
    actual dimensions."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    # Marker range 0xC0-0xCF is "start of frame or similar", but only
    # these are genuine SOFn frame headers -- 0xC4/0xC8/0xCC (DHT/JPG/DAC)
    # share the range without carrying dimensions the same way.
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            pos += 2  # markers with no length field (TEM, SOI, RSTn)
            continue
        if marker == 0xD9:  # EOI -- ran off the end without finding a SOFn
            return None
        if pos + 4 > len(data):
            return None
        segment_length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker in sof_markers:
            if pos + 9 > len(data):
                return None
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return (width, height) if width and height else None
        pos += 2 + segment_length
    return None


def _webp_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height from a WebP file -- the encoding differs by which of
    the three chunk types (simple lossy VP8, simple lossless VP8L, or
    extended VP8X) it actually is."""
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk_type = data[12:16]
    if chunk_type == b"VP8 ":
        # Lossy: each dimension is a 14-bit value in its own 16-bit
        # little-endian field (top 2 bits are an unrelated scaling flag).
        if len(data) < 30:
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk_type == b"VP8L":
        # Lossless: width-minus-1 and height-minus-1, 14 bits each, packed
        # into a 32-bit little-endian field right after a fixed signature byte.
        if len(data) < 25 or data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height) if width and height else None
    if chunk_type == b"VP8X":
        # Extended: width-minus-1/height-minus-1, 24 bits each, little-endian.
        if len(data) < 30:
            return None
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return (width, height) if width and height else None
    return None


_DIMENSIONS_PARSER_BY_CONTENT_TYPE.update(
    {
        "video/mp4": _mp4_video_dimensions,
        "video/webm": _webm_video_dimensions,
        "image/png": _png_image_dimensions,
        "image/jpeg": _jpeg_image_dimensions,
        "image/gif": _gif_image_dimensions,
        "image/webp": _webp_image_dimensions,
    }
)


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


# Encrypted attachments (content.file, not a plain content.url).
#
# A message like this is never itself an `m.room.encrypted` EVENT. That's
# Matrix's separate, whole-room Olm/Megolm encryption, which this bridge's
# ghosts/bot can't participate in at all (no device keys, no key exchange;
# see bridge.membership's own docstring) and which never reaches this far in
# the first place. This is specifically the case where the EVENT is ordinary
# plaintext (readable exactly like any other message) but references a
# separately, per-file-encrypted piece of media: typically a video/image
# forwarded out of a genuinely E2EE room by a client (confirmed: Element)
# that carries over the original encrypted upload rather than decrypting and
# re-uploading plaintext on forward.
#
# The bridge can't federate ciphertext. A remote fediverse server has no way
# to decrypt it, and never will (the AES key lives only in this one Matrix
# event's own content, never part of any AP payload). Making it federatable
# at all means decrypting it here and re-uploading a second, plaintext copy
# to Synapse, a real, human-noticeable action (roughly doubles that file's
# storage), so it's confirmation-gated rather than automatic. See
# bridge.appservice_routes.maybe_handle_encrypted_attachment_confirmation
# for the other half of this (the "confirm" reply recognizer and resume).

ENCRYPTED_ATTACHMENT_WARNING_MARKER = "this message is encrypted"


def unresolvable_encrypted_attachment_mxc(content: dict) -> str | None:
    """If ``content`` is media-shaped (``m.image``/``m.video``/``m.audio``/
    ``m.file``) but its file is per-file AES-encrypted (``content.file``,
    Matrix's EncryptedFile shape) rather than a plain ``content.url``
    string, returns the encrypted file's own ``mxc://``: the identifier
    the rest of this section decrypts/caches against. Returns None for an
    ordinary (unencrypted) attachment, a non-media message, or a malformed
    ``content.file`` with nothing usable to decrypt; there's nothing to
    resolve in any of those cases either way."""
    if content.get("msgtype") not in _MATRIX_MSGTYPE_TO_AP_TYPE:
        return None
    if isinstance(content.get("url"), str):
        return None  # already a plain, resolvable attachment
    encrypted_file = content.get("file")
    if not isinstance(encrypted_file, dict):
        return None
    mxc = encrypted_file.get("url")
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        return None
    key = (encrypted_file.get("key") or {}).get("k")
    if not isinstance(key, str) or not isinstance(encrypted_file.get("iv"), str):
        return None  # malformed, nothing usable to decrypt with either way
    return mxc


def _b64_decode(value: str, *, urlsafe: bool) -> bytes:
    """Matrix's EncryptedFile fields are unpadded base64. ``key.k``
    (a JSON Web Key) uses the URL-safe alphabet per the JWK spec; ``iv``/
    ``hashes.sha256`` use plain base64. Padding is re-added rather than
    assumed present, since senders vary on whether they include it."""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded)


async def decrypt_and_reupload_encrypted_attachment(
    http_client: httpx.AsyncClient, synapse: SynapseClient, repository: ActorRepository, content: dict,
) -> str | None:
    """Downloads, decrypts (AES-256-CTR), integrity-checks (the ciphertext's
    own SHA-256, per the EncryptedFile's ``hashes.sha256``), and re-uploads
    as plaintext the encrypted attachment ``unresolvable_encrypted_attachment_mxc``
    identifies in ``content``. Returns the resulting PLAINTEXT ``mxc://``,
    or None if ``content`` isn't an unresolved encrypted attachment at all,
    or the attempt itself failed at any step.

    Only ever called after the sender has explicitly confirmed (see
    ``bridge.appservice_routes.maybe_handle_encrypted_attachment_confirmation``).
    ``bridge.media.resolve_attachment_or_request_confirmation`` below is
    what gates the unconfirmed, first-pass case and never reaches here.

    Caches the result keyed by the encrypted file's own ``mxc://``, reusing
    ``ActorRepository.get_custom_emoji_mxc``/``record_custom_emoji_mxc``
    rather than a new table: the shape is identical to that cache's own
    purpose (an expensive external reference resolved once to our own
    already-uploaded mxc, never redone for the same source again), just
    keyed by an encrypted mxc:// here instead of a remote AP image URL. A
    cache hit (this same file already confirmed once before, e.g. a
    redelivered/retried confirmation) skips decrypting/uploading again
    entirely. Never mutates ``content`` itself; callers apply the result
    (e.g. by setting ``content["url"]``) themselves."""
    encrypted_mxc = unresolvable_encrypted_attachment_mxc(content)
    if encrypted_mxc is None:
        return None

    cached = await repository.get_custom_emoji_mxc(encrypted_mxc)
    if cached:
        return cached

    encrypted_file = content["file"]
    try:
        key_bytes = _b64_decode(encrypted_file["key"]["k"], urlsafe=True)
        iv_bytes = _b64_decode(encrypted_file["iv"], urlsafe=False)
        expected_sha256 = _b64_decode(encrypted_file["hashes"]["sha256"], urlsafe=False)
    except (KeyError, TypeError, ValueError):
        logger.warning("Malformed EncryptedFile key/iv/hash for %s", encrypted_mxc)
        return None

    server_name, media_id = encrypted_mxc.removeprefix("mxc://").split("/", 1)
    try:
        download = await synapse.download_media(server_name, media_id)
    except SynapseError:
        logger.warning("Failed to download encrypted attachment %s", encrypted_mxc, exc_info=True)
        return None

    if hashlib.sha256(download.content).digest() != expected_sha256:
        logger.warning("Encrypted attachment %s failed its own integrity check, refusing to decrypt", encrypted_mxc)
        return None

    try:
        decryptor = Cipher(algorithms.AES(key_bytes), modes.CTR(iv_bytes)).decryptor()
        plaintext = decryptor.update(download.content) + decryptor.finalize()
    except ValueError:
        logger.warning("Failed to decrypt attachment %s (bad key/iv length)", encrypted_mxc, exc_info=True)
        return None

    content_type = (content.get("info") or {}).get("mimetype") or download.content_type
    filename = (content.get("body") or "").strip() or media_id
    try:
        new_mxc = await synapse.upload_media(plaintext, content_type, filename)
    except SynapseError:
        logger.warning("Failed to re-upload decrypted attachment %s", encrypted_mxc, exc_info=True)
        return None

    await repository.record_custom_emoji_mxc(encrypted_mxc, new_mxc)
    return new_mxc


async def _send_encrypted_attachment_confirmation_request(
    request: Request, *, room_id: str, sender: str, trigger_event_id: str,
) -> None:
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    display_name = sender
    try:
        profile = await synapse.get_profile(sender)
        display_name = profile.get("displayname") or sender
    except SynapseError:
        pass

    warning = (
        f"⚠️{display_name}, {ENCRYPTED_ATTACHMENT_WARNING_MARKER}. To send it to the fediverse, I will "
        "need to upload a decrypted copy to your homeserver.\n\n"
        'Reply to this message with "confirm" to send the decrypted copy.'
    )
    pill = f'<a href="https://matrix.to/#/{html.escape(sender, quote=True)}">{html.escape(display_name)}</a>'
    formatted_warning = (
        f"<p>⚠️{pill}, {ENCRYPTED_ATTACHMENT_WARNING_MARKER}. To send it to the fediverse, I will "
        "need to upload a decrypted copy to your homeserver.</p>"
        '<p>Reply to this message with "confirm" to send the decrypted copy.</p>'
    )
    warning_content = {
        "msgtype": "m.text",
        "body": warning,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_warning,
        "m.mentions": {"user_ids": [sender]},
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": trigger_event_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": trigger_event_id},
        },
    }
    try:
        await synapse.send_message_event(room_id, warning_content, as_user_id=bot_mxid)
    except SynapseError:
        logger.warning("Failed to send encrypted-attachment confirmation request to %s", room_id, exc_info=True)


async def resolve_attachment_or_request_confirmation(
    request: Request, *, content: dict, room_id: str, sender: str, trigger_event_id: str,
) -> bool:
    """Call this right before building/sending an outbound post/reply/DM/
    edit/chat message whose attachment comes from ``content``. Every one
    of this bridge's outbound send paths shares this single gate (called
    from INSIDE each existing handler, after its own "is this event even
    relevant to me" checks, not as a blanket pre-filter at the top of
    dispatch: a pre-filter would fire a confirmation request even in a
    room where nothing was ever going to federate anyway).

    Returns True if it's safe to proceed: either there was nothing
    encrypted to resolve, or an already-confirmed plaintext copy was found
    in cache and applied to ``content["url"]`` in place (so
    ``build_ap_attachment`` just works afterward, unmodified). Returns
    False if a confirmation request was just sent as a thread reply to
    ``trigger_event_id`` (tagging ``sender``); the caller must send
    NOTHING for this event. See
    ``bridge.appservice_routes.maybe_handle_encrypted_attachment_confirmation``
    for how a later "confirm" reply resumes it."""
    encrypted_mxc = unresolvable_encrypted_attachment_mxc(content)
    if encrypted_mxc is None:
        return True  # nothing encrypted here at all, ordinary path, proceed as normal

    repository = request.app.state.repository
    cached = await repository.get_custom_emoji_mxc(encrypted_mxc)
    if cached:
        content["url"] = cached
        return True  # already confirmed and resolved earlier, apply it and proceed transparently

    await _send_encrypted_attachment_confirmation_request(
        request, room_id=room_id, sender=sender, trigger_event_id=trigger_event_id,
    )
    return False
