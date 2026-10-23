"""Resolves ActivityPub custom-emoji (Pleroma/Misskey/Akkoma image/gif)
shortcodes against their source ``tag: [{type: Emoji, name, icon: {url}}]``
metadata, wherever a shortcode can show up: a reaction's own key, a post's
``content``, or a remote actor's display ``name``.

Every distinct emoji image is uploaded to Synapse's own media repo at most
ONCE regardless of where/how many times it's referenced -- see
``ActorRepository.get_custom_emoji_mxc``/``record_custom_emoji_mxc``, a
dedup cache keyed by the emoji's remote image URL (not its shortcode: the
same shortcode text means different images on different source instances).

Matrix has no image-reaction support and no rich (HTML) displayname
support, so a resolved emoji is never a wholesale replacement -- it's
always inlined as an ``<img>`` right next to its own shortcode text, which
stays as the readable fallback wherever the image doesn't render (see
``bridge.inbox_dispatch._notify_post_owner``'s own reasoning, confirmed
live: Element X doesn't render inline ``<img>`` in a message body at all).

Every resolving function here takes ``http_client``/``synapse``/``repository``
unpacked, matching ``bridge.media.fetch_and_upload_media``'s own convention,
rather than a FastAPI ``Request`` -- ``bridge.ghosts`` (where a ghost's
display-name emoji gets resolved) has no ``Request`` in scope, only these
three pieces.
"""

from __future__ import annotations

import html
import re

import httpx

from bridge.activitypub.urls import media_url
from bridge.media import fetch_and_upload_media
from bridge.repository import ActorRepository
from bridge.synapse_client import SynapseClient

_SHORTCODE_RE = re.compile(r":[\w+-]+:")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')


async def resolve_custom_emoji_image(
    http_client: httpx.AsyncClient, synapse: SynapseClient, repository: ActorRepository, tag: list[dict], key: str
) -> str | None:
    """If ``key`` (e.g. ``":blobcat:"``) matches a custom-emoji ``tag``
    entry, resolves it to an ``mxc://`` on Synapse's own media repo (via
    the dedup cache on a hit, or a fresh ``fetch_and_upload_media`` on a
    miss). Returns None for a plain unicode reaction/shortcode-less text,
    or if the image couldn't be resolved/fetched at all.

    Matched with surrounding colons stripped from BOTH sides before
    comparing ``key`` against ``tag[].name`` -- confirmed live against a
    real Akkoma instance (kiwifarms.cc) sending ``content: ":ablobcheer:"``
    but ``tag[].name: "ablobcheer"`` (no colons at all), which an exact-match
    comparison silently failed to resolve. A different real instance's own
    reaction (kiwifarms.cc again, different post) sent ``tag[].name:
    ":verified:"`` WITH colons, so neither convention can be assumed."""
    normalized_key = key.strip(":")
    icon_url = None
    emoji_name = None
    for tag_entry in tag:
        if tag_entry.get("type") == "Emoji" and tag_entry.get("name", "").strip(":") == normalized_key:
            icon_url = (tag_entry.get("icon") or {}).get("url")
            emoji_name = tag_entry.get("name")
            break
    if not icon_url:
        return None

    cached = await repository.get_custom_emoji_mxc(icon_url)
    if cached:
        if emoji_name:
            # Backfills a name onto a row cached before this bridge started
            # tracking emoji names at all (or any other gap): a no-op once
            # it's already set, matching record_custom_emoji_mxc's own
            # idempotent contract for everything else about this row.
            await repository.record_custom_emoji_mxc(icon_url, cached, emoji_name)
        return cached

    mxc = await fetch_and_upload_media(http_client, synapse, icon_url)
    if mxc:
        await repository.record_custom_emoji_mxc(icon_url, mxc, emoji_name)
        await repository.mark_media_published(mxc)
    return mxc


async def inline_custom_emoji(
    http_client: httpx.AsyncClient,
    synapse: SynapseClient,
    repository: ActorRepository,
    html_text: str,
    tag: list[dict],
    *,
    subject_id: str | None = None,
) -> str:
    """Scans ``html_text`` (already-sanitized Matrix message HTML, e.g.
    ``bridge.activitypub.sanitize.strip_to_matrix_message``'s own
    ``safe_html`` output) for every ``:shortcode:``-shaped substring,
    resolves each against ``tag``, and replaces it with an inline ``<img>``
    (``alt``/``title`` still carry the shortcode text -- confirmed live that
    clients which don't render an inline ``<img>`` at all, e.g. Element X,
    already fall back to showing that ``alt`` text on their own, so there's
    no need to ALSO leave the literal shortcode next to it -- unlike
    ``bridge.inbox_dispatch._notify_post_owner``'s reaction notification,
    which keeps the adjacent shortcode since it isn't itself the image's
    ``alt`` text). A shortcode with no matching ``tag`` entry, or whose
    image couldn't be fetched, is left untouched.

    If ``subject_id`` is given (a post's ``ap_object_id``, or a remote
    actor's own ``actor_id`` for a display name), every resolved shortcode
    is also persisted via ``ActorRepository.record_resolved_emoji`` -- see
    that method's docstring for why a later re-render needs this."""
    if not tag:
        return html_text
    shortcodes = set(_SHORTCODE_RE.findall(html_text))
    if not shortcodes:
        return html_text

    result = html_text
    for shortcode in shortcodes:
        mxc = await resolve_custom_emoji_image(http_client, synapse, repository, tag, shortcode)
        if not mxc:
            continue
        result = result.replace(shortcode, emoji_img_html(shortcode, mxc, with_text=False))
        if subject_id:
            await repository.record_resolved_emoji(subject_id, shortcode, mxc)
    return result


async def resolve_and_persist_emoji(
    http_client: httpx.AsyncClient,
    synapse: SynapseClient,
    repository: ActorRepository,
    text: str,
    tag: list[dict],
    subject_id: str,
) -> None:
    """Like ``inline_custom_emoji``, but for a plain-text subject that has
    no HTML representation to inline an ``<img>`` into at all -- a ghost's
    Matrix displayname, which the Matrix protocol itself only ever supports
    as plain text (no rich/HTML displaynames, unlike a message body). Only
    resolves+persists (via ``ActorRepository.record_resolved_emoji``) so the
    public web page's byline can render the image later; never touches
    ``text`` itself, and the ghost's actual Matrix displayname stays exactly
    the raw ``:shortcode:`` text it always has."""
    if not tag:
        return
    for shortcode in set(_SHORTCODE_RE.findall(text)):
        mxc = await resolve_custom_emoji_image(http_client, synapse, repository, tag, shortcode)
        if mxc:
            await repository.record_resolved_emoji(subject_id, shortcode, mxc)


def apply_resolved_emoji(html_text: str, resolved: dict[str, str]) -> str:
    """Like ``inline_custom_emoji``, but for a re-render that already has
    the shortcode -> ``mxc_url`` mapping in hand (via
    ``ActorRepository.get_resolved_emoji``) instead of the original AP
    ``tag`` data -- so this is a pure, synchronous substitution, no
    fetching/resolving/persisting. Used by the public web page rebuilding a
    post's content or a ghost's byline from stored data alone -- always
    WITHOUT the adjacent shortcode text ``inline_custom_emoji`` keeps, since
    that's only there as a fallback for Matrix clients that don't render
    inline ``<img>`` (Element X, confirmed live); a web page is our own
    controlled HTML with no such rendering risk, so the image alone is enough
    (the shortcode's still in the ``alt``/``title`` attributes)."""
    result = html_text
    for shortcode, mxc in resolved.items():
        if shortcode in result:
            result = result.replace(shortcode, emoji_img_html(shortcode, mxc, with_text=False))
    return result


def emoji_img_html(shortcode: str, mxc_or_url: str, *, with_text: bool = True) -> str:
    escaped = html.escape(shortcode)
    img = (
        f'<img src="{html.escape(mxc_or_url, quote=True)}" alt="{escaped}" title="{escaped}" '
        f'width="20" height="20" style="vertical-align:middle">'
    )
    return f"{img} {escaped}" if with_text else img


async def extract_outbound_emoji_tags(
    repository: ActorRepository, base: str, formatted_body: str | None,
) -> list[dict]:
    """Scans a Matrix message's own ``formatted_body`` for MSC2545 image-pack
    emoticons (``<img data-mx-emoticon ... src="mxc://..." alt=":name:">``,
    what a Matrix client auto-inserts when someone types a custom emoji
    shortcode with an image pack enabled) and builds the matching outbound
    AS2 ``Emoji`` tag entries -- the same ``{type: Emoji, name, icon: {type:
    Image, url}}`` shape a remote Pleroma/Misskey/Akkoma server needs to
    actually render the image instead of the literal ``:shortcode:``
    fallback text that's already in the post's plain-text ``body`` either
    way. Without this, a post/reply/edit written with a local image pack
    federated with no tag describing what the shortcode even meant --
    confirmed live 2026-10-22, reported from the REMOTE side (a Pleroma
    user asking whether the bare ``:shortcode:`` text they saw was "some
    ActivityPub thing" this bridge hadn't implemented, when actually this
    bridge had simply never sent the tag describing it at all).

    This is the OUTBOUND mirror of this module's other functions, which all
    resolve the inbound direction (an incoming Note's own ``tag`` data).
    Each distinct mxc is published through the bridge's own public media
    proxy (``mark_media_published``) and recorded via ``record_custom_emoji_mxc``
    UNLESS it's already a known local copy of some REMOTE emoji
    (``get_custom_emoji_source`` finds a row -- e.g. replying using the very
    custom emoji just seen in an incoming post), in which case the
    ORIGINAL remote icon URL and name are reused instead of minting a
    second, differently-hosted identity for the same image under this
    bridge's own domain -- same reasoning, and the same real Pleroma/Akkoma
    "treated as an entirely separate emoji" bug (2026-10-12), that
    ``bridge.reaction_bridge``'s own outbound custom-image-reaction
    handling already works around."""
    if not formatted_body:
        return []
    tags: list[dict] = []
    seen: set[str] = set()
    for img_tag in _IMG_TAG_RE.findall(formatted_body):
        if "data-mx-emoticon" not in img_tag:
            continue
        attrs = dict(_HTML_ATTR_RE.findall(img_tag))
        src = attrs.get("src")
        if not src or not src.startswith("mxc://"):
            continue
        raw_name = attrs.get("alt") or attrs.get("title")
        if not raw_name:
            continue
        name = html.unescape(raw_name).strip()
        if not name:
            continue
        name = name if name.startswith(":") and name.endswith(":") else f":{name.strip(':')}:"
        if name in seen:
            continue
        seen.add(name)

        remembered = await repository.get_custom_emoji_source(src)
        if remembered:
            remembered_icon_url, remembered_name = remembered
            tags.append({
                "type": "Emoji", "name": remembered_name or name,
                "icon": {"type": "Image", "url": remembered_icon_url},
            })
            continue

        await repository.mark_media_published(src)
        icon_url = media_url(base, src)
        await repository.record_custom_emoji_mxc(icon_url, src, name)
        tags.append({"type": "Emoji", "name": name, "icon": {"type": "Image", "url": icon_url}})
    return tags
