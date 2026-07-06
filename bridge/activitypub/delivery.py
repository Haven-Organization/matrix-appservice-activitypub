"""Outbound ActivityPub delivery: sign and POST an activity to a remote inbox."""

from __future__ import annotations

import json
import logging

import httpx

from bridge.activitypub.signatures import sign_request

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Raised when delivering a signed activity to a remote inbox fails."""


async def deliver_activity(
    http_client: httpx.AsyncClient,
    *,
    inbox_url: str,
    activity: dict,
    key_id: str,
    private_key_pem: str,
) -> None:
    """Sign ``activity`` as ``key_id`` and POST it to ``inbox_url``.

    Raises ``DeliveryError`` on any non-2xx response or transport failure.
    """
    body = json.dumps(activity).encode("utf-8")
    headers = sign_request(
        method="POST", url=inbox_url, body=body, key_id=key_id, private_key_pem=private_key_pem
    )
    headers["Content-Type"] = "application/activity+json"

    try:
        response = await http_client.post(inbox_url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise DeliveryError(f"Delivery to {inbox_url} failed: {exc}") from exc

    if response.status_code >= 300:
        raise DeliveryError(
            f"Delivery to {inbox_url} failed: HTTP {response.status_code} {response.text[:200]}"
        )
