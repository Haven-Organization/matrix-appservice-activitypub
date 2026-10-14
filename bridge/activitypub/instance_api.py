"""A minimal Mastodon-compatible ``/api/v1/instance`` document.

Not aimed at real Mastodon API clients -- this bridge implements none of
the rest of that API surface (statuses, media, timelines) -- but several
directory/crawler sites (fedilist.com confirmed live 2026-10-14, also
fedidb.com) probe this specific path alongside NodeInfo before listing a
domain as a federated instance, and read ``contact_account`` for its admin
contact. ``software.name``/``software.version`` live in
``bridge.activitypub.nodeinfo`` -- reused here rather than duplicated.
"""

from __future__ import annotations

from typing import Any

from bridge.activitypub.nodeinfo import SOFTWARE_NAME, SOFTWARE_VERSION
from bridge.activitypub.urls import actor_url
from bridge.repository import ActorRecord


def build_mastodon_instance_document(
    *,
    domain: str,
    public_base_url: str,
    local_user_count: int,
    peer_domain_count: int,
    admin_actor: ActorRecord | None,
) -> dict[str, Any]:
    contact_account: dict[str, Any] | None = None
    if admin_actor is not None:
        contact_account = {
            "id": admin_actor.username,
            "username": admin_actor.username,
            "acct": f"{admin_actor.username}@{domain}",
            "display_name": admin_actor.display_name or admin_actor.username,
            "locked": False,
            "bot": False,
            "discoverable": True,
            "group": False,
            "note": admin_actor.summary or "",
            "url": actor_url(public_base_url, admin_actor.username),
            "avatar": admin_actor.icon_url or "",
            "avatar_static": admin_actor.icon_url or "",
            "header": "",
            "header_static": "",
        }

    description = f"{domain} is a Matrix homeserver bridged to the fediverse via {SOFTWARE_NAME}."
    return {
        "uri": domain,
        "title": domain,
        "short_description": description,
        "description": description,
        "email": "",
        "version": f"{SOFTWARE_NAME} {SOFTWARE_VERSION}",
        "urls": {},
        "stats": {
            "user_count": local_user_count,
            # Not tracked anywhere -- a real count would mean walking every
            # local actor's full Matrix history on every request, which this
            # project deliberately avoids caching separately from Matrix
            # itself (see FEATURES.md's outbox/followers/following note).
            "status_count": 0,
            "domain_count": peer_domain_count,
        },
        "thumbnail": None,
        "languages": ["en"],
        "registrations": False,
        "approval_required": False,
        "invites_enabled": False,
        "contact_account": contact_account,
    }
