"""Canonical URL builders for this bridge's ActivityPub endpoints.

Centralized so the Actor IRI scheme (``{base}/actor/{username}``, etc.) is
defined exactly once and stays consistent across routes, webfinger
documents, and outgoing signed requests.
"""

from __future__ import annotations


def actor_url(base_url: str, username: str) -> str:
    return f"{base_url}/actor/{username}"


def inbox_url(base_url: str, username: str) -> str:
    return f"{base_url}/inbox/{username}"


def outbox_url(base_url: str, username: str) -> str:
    return f"{base_url}/outbox/{username}"


def followers_url(base_url: str, username: str) -> str:
    return f"{base_url}/followers/{username}"


def following_url(base_url: str, username: str) -> str:
    return f"{base_url}/following/{username}"


def main_key_id(base_url: str, username: str) -> str:
    return f"{actor_url(base_url, username)}#main-key"


def shared_inbox_url(base_url: str) -> str:
    return f"{base_url}/inbox"


def username_from_actor_url(base_url: str, actor_id: str) -> str | None:
    """Reverse of ``actor_url`` -- the local username an actor IRI refers to,
    or None if it isn't one of ours (e.g. a remote actor's IRI)."""
    prefix = f"{base_url}/actor/"
    if not actor_id.startswith(prefix):
        return None
    rest = actor_id[len(prefix):]
    if not rest or "/" in rest:
        return None
    return rest


def media_url(base_url: str, mxc_uri: str) -> str:
    """Public URL for the bridge's own media proxy (``GET /media/{server}/{id}``).

    Deliberately *not* a direct link to Synapse's media API -- that requires an
    access token (MSC3916 authenticated media) that remote fediverse servers
    don't have. The bridge fetches on their behalf instead; see
    ``bridge.synapse_client.SynapseClient.download_media``.
    """
    if not mxc_uri.startswith("mxc://"):
        raise ValueError(f"Not an mxc:// URI: {mxc_uri}")
    server_and_id = mxc_uri.removeprefix("mxc://")
    return f"{base_url}/media/{server_and_id}"


def resolve_own_media_proxy_mxc(base_url: str, url: str) -> str | None:
    """Reverse of ``media_url`` -- if ``url`` is our OWN media proxy link
    (i.e. we minted it ourselves for some earlier post's attachment, see
    ``build_ap_attachment``), returns the exact ``mxc://`` URI it encodes;
    ``None`` for anything else (a genuinely-remote URL).

    Used to recognize a reposted/quoted post's attachment as media we
    ALREADY have on this homeserver -- e.g. a local user's own post,
    reposted by a remote follower, carries an attachment URL pointing right
    back at this same proxy -- so it can be reused as-is instead of being
    downloaded through our own public endpoint and re-uploaded to Synapse
    as a brand new, wasteful, un-deduplicated copy (confirmed live
    2026-07-10: reposting the same local post from N different remote
    accounts produced N distinct mxc:// copies of the identical file).
    """
    prefix = f"{base_url}/media/"
    if not url.startswith(prefix):
        return None
    server_and_id = url.removeprefix(prefix)
    if server_and_id.count("/") != 1 or not all(server_and_id.split("/")):
        return None
    return f"mxc://{server_and_id}"
