"""Fetching remote Actor documents (for follow, inbox resolution, profile metadata)."""

from __future__ import annotations

import httpx
from fastapi import Request

from bridge.activitypub.urls import username_from_actor_url


class RemoteActorFetchError(Exception):
    """Raised when a remote actor document can't be fetched or parsed."""


async def fetch_actor(http_client: httpx.AsyncClient, actor_id: str) -> dict:
    """``GET`` an actor's JSON-LD document."""
    try:
        response = await http_client.get(actor_id, headers={"Accept": "application/activity+json"})
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
        doc = await fetch_actor(request.app.state.http_client, actor_id)
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
