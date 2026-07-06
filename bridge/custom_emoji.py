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

from bridge.media import fetch_and_upload_media
from bridge.repository import ActorRepository
from bridge.synapse_client import SynapseClient

_SHORTCODE_RE = re.compile(r":[\w+-]+:")


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
    for tag_entry in tag:
        if tag_entry.get("type") == "Emoji" and tag_entry.get("name", "").strip(":") == normalized_key:
            icon_url = (tag_entry.get("icon") or {}).get("url")
            break
    if not icon_url:
        return None

    cached = await repository.get_custom_emoji_mxc(icon_url)
    if cached:
        return cached

    mxc = await fetch_and_upload_media(http_client, synapse, icon_url)
    if mxc:
        await repository.record_custom_emoji_mxc(icon_url, mxc)
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
