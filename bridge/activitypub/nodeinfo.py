"""NodeInfo (http://nodeinfo.diaspora.software/) lookups for a remote domain.

Used to detect a peer that isn't part of the Mastodon-HTML-content family --
first need: Shoot (github.com/MaddyUnderStars/shoot) stores/displays a
message's Note ``content`` completely raw, with no HTML parsing at all (see
``bridge.reply_bridge._send_outbound_dm``'s own docstring for the live bug
this was written for) -- so the ``<p>...</p>`` wrapping every other outbound
Note gets (``plain_text_to_note_html``) shows up as literal, visible tag
text in Shoot's own UI instead of being rendered. NodeInfo's ``software.name``
is the standard, purpose-built way to ask "what does this domain run"
without guessing from actor-document shape (which carries nothing
Shoot-specific -- confirmed live, its actor documents are otherwise a
completely ordinary ``Person``).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import Request

_TTL_SECONDS = 3600.0
_cache: dict[str, tuple[float, str | None]] = {}  # domain -> (fetched_at, software_name_or_None)

# This bridge's own reported identity in the NodeInfo documents it serves.
# There's no real release/version tracking anywhere in this project yet
# (no VERSION file, no git tags), so this is a placeholder until one exists.
SOFTWARE_NAME = "matrix-appservice-activitypub"
SOFTWARE_VERSION = "0.1.0"

NODEINFO_SCHEMA_VERSIONS = ("2.0", "2.1")


async def remote_software_name(request: Request, domain: str) -> str | None:
    """The ``software.name`` a domain's own NodeInfo document reports (e.g.
    ``"shoot"``, ``"mastodon"``, ``"pleroma"``), or ``None`` if it can't be
    determined (no NodeInfo support, unreachable, malformed) -- treated the
    same as "assume Mastodon-family HTML content" by callers, since that's
    already the overwhelming majority of the fediverse and the previously
    unconditional behavior. Cached per-domain (NodeInfo doesn't change
    software mid-flight) rather than fetched on every message.
    """
    cached = _cache.get(domain)
    if cached is not None and (time.monotonic() - cached[0]) < _TTL_SECONDS:
        return cached[1]

    http_client = request.app.state.http_client
    software_name: str | None = None
    try:
        discovery = await http_client.get(f"https://{domain}/.well-known/nodeinfo")
        discovery.raise_for_status()
        links = discovery.json().get("links") or []
        # Prefer the highest schema version offered, same convention every
        # NodeInfo consumer uses -- newer schema versions are a superset,
        # not a breaking change, so this never needs to fall back further.
        nodeinfo_url = next(
            (link.get("href") for link in reversed(links) if isinstance(link, dict) and link.get("href")),
            None,
        )
        if nodeinfo_url:
            document = await http_client.get(nodeinfo_url)
            document.raise_for_status()
            name = (document.json().get("software") or {}).get("name")
            software_name = name.lower() if isinstance(name, str) else None
    except (httpx.HTTPError, ValueError):
        software_name = None

    _cache[domain] = (time.monotonic(), software_name)
    return software_name


def build_nodeinfo_discovery_document(public_base_url: str) -> dict[str, Any]:
    """The ``/.well-known/nodeinfo`` document. Just links to the actual
    per-schema-version documents below, per the NodeInfo discovery spec.
    Directory/crawler sites (fedidb, fedilist, the-federation.info) start
    here to confirm a domain is a federated instance at all before looking
    at anything else."""
    return {
        "links": [
            {
                "rel": f"http://nodeinfo.diaspora.software/ns/schema/{version}",
                "href": f"{public_base_url}/nodeinfo/{version}",
            }
            for version in NODEINFO_SCHEMA_VERSIONS
        ]
    }


def build_nodeinfo_document(schema_version: str, *, local_user_count: int) -> dict[str, Any]:
    """The actual NodeInfo document for one schema version. ``openRegistrations``
    is always ``False`` since this is an appservice mirroring an existing
    homeserver's own accounts, not something a stranger can sign up on."""
    software: dict[str, Any] = {"name": SOFTWARE_NAME, "version": SOFTWARE_VERSION}
    if schema_version != "2.0":
        # 2.1 added these two optional fields to the "software" object.
        software["repository"] = "https://github.com/Haven-Organization/matrix-appservice-activitypub"
    return {
        "version": schema_version,
        "software": software,
        "protocols": ["activitypub"],
        "services": {"outbound": [], "inbound": []},
        "usage": {"users": {"total": local_user_count}},
        "openRegistrations": False,
        "metadata": {},
    }
