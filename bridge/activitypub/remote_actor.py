"""Fetching remote Actor documents (for follow, inbox resolution, profile metadata)."""

from __future__ import annotations

import httpx
from fastapi import Request

from bridge.activitypub.signatures import sign_request
from bridge.activitypub.urls import main_key_id, username_from_actor_url


class RemoteActorFetchError(Exception):
    """Raised when a remote actor document can't be fetched or parsed."""


async def fetch_actor(request: Request, actor_id: str) -> dict:
    """``GET`` an actor's JSON-LD document, signed as the bridge's own
    persistent service actor (see ``bridge.service_actor``).

    Some servers require HTTP Signatures on actor-document GETs too, not
    just inbox POSTs ("authorized fetch"/secure mode -- Mastodon supports
    this as an admin-configurable mode; confirmed live 2026-07-14 that
    Shoot's own reference deployment at chat.understars.dev requires it
    unconditionally) -- an unsigned GET gets a flat ``400 Missing
    signature`` with no actor document recoverable from it at all, which
    broke EVERY use of this function against that server: inbound Follow/
    Create signature verification (``ActorKeyCache.get``, which fetches the
    sender's public key this same way and hit the identical wall), ghost
    provisioning, mention/quote resolution, all of it. Signing
    unconditionally costs nothing against a server that doesn't require
    it (the signature is simply ignored), so there's no reason to make it
    optional/conditional.
    """
    config = request.app.state.config
    service_actor = request.app.state.service_actor
    headers = {"Accept": "application/activity+json"}
    headers.update(
        sign_request(
            method="GET",
            url=actor_id,
            body=b"",
            key_id=main_key_id(config.bridge.public_base_url, service_actor.username),
            private_key_pem=service_actor.private_key_pem,
            # No body on a GET -- Digest doesn't apply (see deliver_activity's
            # own default DEFAULT_SIGNED_HEADERS, which DOES include Digest,
            # for the contrasting POST case).
            signed_headers=("(request-target)", "host", "date"),
        )
    )
    try:
        response = await request.app.state.http_client.get(actor_id, headers=headers)
    except httpx.HTTPError as exc:
        raise RemoteActorFetchError(f"Failed to fetch actor {actor_id}: {exc}") from exc

    if response.status_code >= 400:
        raise RemoteActorFetchError(f"Failed to fetch actor {actor_id}: HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        raise RemoteActorFetchError(f"Actor document at {actor_id} is not valid JSON") from exc


async def resolve_actor_inbox(request: Request, actor_id: str) -> str | None:
    """Resolve an actor's inbox URL, preferring a cached Remote User Room mapping
    (set up when we followed them) before fetching their actor document live.

    Refuses to resolve an inbox for one of our OWN local actors -- e.g. two
    local test/demo accounts following each other via a real AP handshake
    rather than just sharing a Matrix room. Delivering to such a "follower"
    would just be us POSTing to our own /inbox, which reprocesses the
    activity and can re-trigger the same outbound delivery again --
    an unbounded feedback loop for anything that federates on redelivery
    (e.g. a Delete that re-redacts and re-federates every round trip)."""
    base = request.app.state.config.bridge.public_base_url
    if username_from_actor_url(base, actor_id) is not None:
        return None
    remote_room = await request.app.state.repository.get_remote_actor_room(actor_id)
    if remote_room is not None:
        return remote_room.inbox_url
    try:
        doc = await fetch_actor(request, actor_id)
    except RemoteActorFetchError:
        return None
    return doc.get("inbox")


def _extract_image_field_url(image: object) -> str | None:
    """Shared shape-handling for an Actor's ``icon``/``image`` field: a
    single Image object (``{"type": "Image", "url": "..."}``), a bare URL
    string, or a list of either (some implementations send multiple
    sizes/formats -- we just use the first) -- and, within an Image
    object, ``url`` ITSELF being a list of Link objects/strings rather
    than a single one (legal per the AS2 spec as multiple equivalent
    representations of the same resource; seen in the wild on a Pleroma
    ``ChatMessage`` attachment -- see ``extract_attachments`` -- so worth
    handling the same way here too)."""
    if isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, list):
            url = url[0] if url else None
        if isinstance(url, dict):
            url = url.get("href") or url.get("url")
        return url if isinstance(url, str) else None
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        return _extract_image_field_url(image[0])
    return None


def extract_icon_url(actor_doc: dict) -> str | None:
    """Pull the avatar URL out of an Actor's ``icon`` field."""
    return _extract_image_field_url(actor_doc.get("icon"))


def extract_banner_url(actor_doc: dict) -> str | None:
    """Pull the banner/header URL out of an Actor's ``image`` field (AS2's
    convention for a profile's header/banner, as opposed to ``icon`` for
    the avatar -- see bridge.activitypub.models' own comment on this)."""
    return _extract_image_field_url(actor_doc.get("image"))


def extract_actor_url(actor_doc: dict) -> str | None:
    """The actor's own canonical, human-facing profile page (AS2's ``url``
    field), for MSC4503's ``m.external_handle.url`` -- distinct from
    ``id``, which is often an API endpoint rather than something a human
    can open (e.g. Pleroma/Akkoma's ``.../users/alice`` versus Mastodon's
    ``.../@alice``). Same list/Link-object/string handling as
    ``bridge.note_mirroring.source_post_url``'s identical field on a Note,
    just for an Actor's own top-level ``url`` instead. Falls back to
    ``id`` if no usable ``url`` is present, same reasoning as that
    function: some usable link is better than none."""
    url = actor_doc.get("url")
    if isinstance(url, list):
        url = url[0] if url else None
    if isinstance(url, dict):
        url = url.get("href") or url.get("url")
    if isinstance(url, str) and url:
        return url
    actor_id = actor_doc.get("id")
    return actor_id if isinstance(actor_id, str) else None


def extract_attachments(obj: dict) -> list[dict]:
    """Normalize a Note's ``attachment`` field to a list of ``{"url", "media_type", "name"}`` dicts.

    Handles both a single attachment object and a list of them, and the
    occasional implementation that nests the URL as ``{"href": ...}``.
    Also handles ``url`` itself being a LIST of Link objects/strings --
    legal per the AS2 spec (multiple equivalent representations of the
    same resource) and seen in the wild on a Pleroma ``ChatMessage``'s
    attachment, which carries the exact ``Object.data`` recorded at
    upload time rather than whatever simpler single-url shape that same
    instance's Note attachments happen to serialize -- the first entry is
    used, same as picking the first of a multi-attachment post elsewhere
    in this module.
    """
    raw = obj.get("attachment")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, list):
            url = url[0] if url else None
        if isinstance(url, dict):
            url = url.get("href") or url.get("url")
        if not isinstance(url, str) or not url:
            continue
        results.append(
            {
                "url": url,
                "media_type": item.get("mediaType") or "application/octet-stream",
                "name": item.get("name") or "",
            }
        )
    return results
