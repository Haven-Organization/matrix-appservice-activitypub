"""HTTP Signatures (draft-cavage-http-signatures) for ActivityPub federation.

Mastodon, Pleroma and friends authenticate S2S requests with this scheme:
every outgoing activity is signed with the sending actor's RSA private key,
and every inbox MUST verify the signature against the actor's published
``publicKeyPem`` before trusting the payload. This module implements both
directions plus the supporting ``Digest`` header and a small TTL cache for
fetched remote public keys.

This is the single most security-critical module in the bridge: a bridge
that skips or weakens this check will accept forged activities from anyone.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from email.utils import format_datetime, parsedate_to_datetime
from typing import Awaitable, Callable, Mapping

import httpx

from bridge.crypto import load_private_key, load_public_key

DEFAULT_SIGNED_HEADERS = ("(request-target)", "host", "date", "digest")


class SignatureError(Exception):
    """Raised for any failure to produce or verify an HTTP signature."""


def compute_digest(body: bytes) -> str:
    """Return the ``Digest`` header value for a request body (SHA-256, per RFC 3230)."""
    digest = hashlib.sha256(body).digest()
    return "SHA-256=" + base64.b64encode(digest).decode("ascii")


def http_date(when: float | None = None) -> str:
    """RFC 7231 ``Date`` header value (e.g. ``Mon, 30 Jun 2026 12:00:00 GMT``)."""
    import datetime

    dt = datetime.datetime.fromtimestamp(when if when is not None else time.time(), tz=datetime.timezone.utc)
    return format_datetime(dt, usegmt=True)


def _build_signing_string(
    method: str, path: str, headers: Mapping[str, str], signed_headers: tuple[str, ...]
) -> str:
    lines = []
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in signed_headers:
        if name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
            continue
        if name not in lowered:
            raise SignatureError(f"Cannot sign/verify: missing header '{name}'")
        lines.append(f"{name}: {lowered[name]}")
    return "\n".join(lines)


@dataclass
class OutgoingSignature:
    """Headers to attach to an outgoing federated request."""

    date: str
    digest: str | None
    signature: str
    host: str


def sign_request(
    *,
    method: str,
    url: str,
    body: bytes,
    key_id: str,
    private_key_pem: str,
    signed_headers: tuple[str, ...] = DEFAULT_SIGNED_HEADERS,
) -> dict[str, str]:
    """Build the ``Date``/``Digest``/``Signature``/``Host`` headers for an outgoing request.

    ``key_id`` is the actor's public key IRI, conventionally ``{actor_id}#main-key``.
    """
    parsed = httpx.URL(url)
    host = parsed.netloc.decode("ascii") if isinstance(parsed.netloc, bytes) else str(parsed.netloc)
    path = parsed.raw_path.decode("ascii") if isinstance(parsed.raw_path, bytes) else str(parsed.raw_path)

    headers = {"host": host, "date": http_date()}
    needs_digest = "digest" in signed_headers
    digest = compute_digest(body) if needs_digest else None
    if digest:
        headers["digest"] = digest

    signing_string = _build_signing_string(method, path, headers, signed_headers)

    private_key = load_private_key(private_key_pem)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    raw_signature = private_key.sign(
        signing_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
    )
    signature_b64 = base64.b64encode(raw_signature).decode("ascii")

    signature_header = (
        f'keyId="{key_id}",algorithm="rsa-sha256",'
        f'headers="{" ".join(signed_headers)}",signature="{signature_b64}"'
    )

    result = {"Host": host, "Date": headers["date"], "Signature": signature_header}
    if digest:
        result["Digest"] = digest
    return result


_SIGNATURE_PARAM_RE = re.compile(r'(\w+)="?([^",]+)"?')


def _parse_signature_header(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for match in _SIGNATURE_PARAM_RE.finditer(value):
        params[match.group(1)] = match.group(2)
    if "keyId" not in params or "signature" not in params:
        raise SignatureError("Signature header missing required keyId/signature parameters")
    return params


def verify_signature_string(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    signature_header: str,
    public_key_pem: str,
) -> str:
    """Verify a raw ``Signature`` header value against ``public_key_pem``.

    Returns the ``keyId`` from the header on success. Raises ``SignatureError`` on
    any failure (malformed header, unsupported algorithm, bad signature, ...).
    """
    params = _parse_signature_header(signature_header)
    algorithm = params.get("algorithm", "rsa-sha256")
    if algorithm not in ("rsa-sha256", "hs2019"):
        raise SignatureError(f"Unsupported signature algorithm: {algorithm}")

    signed_headers = tuple((params.get("headers") or "date").split(" "))
    signing_string = _build_signing_string(method, path, headers, signed_headers)

    try:
        raw_signature = base64.b64decode(params["signature"])
    except (ValueError, TypeError) as exc:
        raise SignatureError("Signature is not valid base64") from exc

    public_key = load_public_key(public_key_pem)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key.verify(
            raw_signature, signing_string.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature as exc:
        raise SignatureError("Signature verification failed") from exc

    return params["keyId"]


def verify_digest(body: bytes, digest_header: str) -> None:
    """Raise ``SignatureError`` unless ``digest_header`` matches the SHA-256 of ``body``."""
    expected = compute_digest(body)
    # Digest headers are case-sensitive in the algorithm token but not the b64 value commonly;
    # compare the algorithm-normalized form to tolerate "sha-256=" vs "SHA-256=".
    if not digest_header.upper().replace(" ", "") == expected.upper().replace(" ", ""):
        raise SignatureError("Digest header does not match request body")


def verify_date_freshness(date_header: str, max_clock_skew: int) -> None:
    """Raise ``SignatureError`` if the ``Date`` header is missing, unparsable, or too old/skewed."""
    try:
        sent_at = parsedate_to_datetime(date_header)
    except (TypeError, ValueError) as exc:
        raise SignatureError("Date header is missing or unparsable") from exc
    if sent_at.tzinfo is None:
        import datetime

        sent_at = sent_at.replace(tzinfo=datetime.timezone.utc)
    skew = abs(time.time() - sent_at.timestamp())
    if skew > max_clock_skew:
        raise SignatureError(f"Date header skew of {skew:.0f}s exceeds max_clock_skew={max_clock_skew}")


class ActorKeyCache:
    """In-memory TTL cache mapping a ``keyId`` IRI to the actor's PEM public key.

    Fetches ``GET {actor_id}`` (the ``keyId`` IRI minus any ``#fragment``) and reads
    ``publicKey.publicKeyPem`` from the returned Actor JSON-LD document.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        ttl_seconds: int = 3600,
        *,
        signing_key_id: str,
        signing_private_key_pem: str,
    ) -> None:
        self._client = http_client
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, str]] = {}
        self._signing_key_id = signing_key_id
        self._signing_private_key_pem = signing_private_key_pem

    async def get(self, key_id: str) -> str:
        cached = self._cache.get(key_id)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]

        actor_url = key_id.split("#", 1)[0]
        # Signed the same way bridge.activitypub.remote_actor.fetch_actor is
        # (see that function's own docstring) -- some servers require HTTP
        # Signatures on this GET too, not just inbox POSTs, and this is the
        # one actor-document fetch in the whole codebase that ISN'T routed
        # through fetch_actor (it runs during signature verification itself,
        # before a full Request/app.state is necessarily the natural thing
        # to thread through), so it needs its own copy of the same fix.
        headers = {"Accept": "application/activity+json"}
        headers.update(
            sign_request(
                method="GET", url=actor_url, body=b"",
                key_id=self._signing_key_id, private_key_pem=self._signing_private_key_pem,
                signed_headers=("(request-target)", "host", "date"),
            )
        )
        # A remote actor can be gone (410, deleted account), unreachable, or
        # just not JSON -- any of that means we can't verify this request,
        # which is a normal, expected outcome (someone signing with a key
        # from a since-deleted account), not a bug: it must surface as
        # SignatureError so the caller rejects the request with a clean 401,
        # not an unhandled exception crashing the whole response with a 500.
        try:
            response = await self._client.get(actor_url, headers=headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise SignatureError(f"Could not fetch actor {actor_url}: {exc}") from exc
        except ValueError as exc:
            raise SignatureError(f"Actor {actor_url} did not return valid JSON") from exc

        public_key_pem = (data.get("publicKey") or {}).get("publicKeyPem")
        if not public_key_pem:
            raise SignatureError(f"Actor {actor_url} has no publicKey.publicKeyPem")

        self._cache[key_id] = (time.monotonic(), public_key_pem)
        return public_key_pem

    def invalidate(self, key_id: str) -> None:
        self._cache.pop(key_id, None)


PublicKeyResolver = Callable[[str], Awaitable[str]]


async def verify_incoming_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    resolve_public_key: PublicKeyResolver,
    max_clock_skew: int = 3600,
) -> str:
    """Full verification pipeline for an incoming federated request.

    ``resolve_public_key`` is an async callable (typically ``ActorKeyCache.get``)
    that maps a ``keyId`` to a PEM public key, potentially fetching it over the
    network. Returns the verified ``keyId`` on success; raises ``SignatureError``
    otherwise.
    """
    lowered = {k.lower(): v for k, v in headers.items()}

    signature_header = lowered.get("signature")
    if not signature_header:
        raise SignatureError("Request is missing the Signature header")

    if "date" in lowered:
        verify_date_freshness(lowered["date"], max_clock_skew)
    else:
        raise SignatureError("Request is missing the Date header")

    if body:
        digest_header = lowered.get("digest")
        if not digest_header:
            raise SignatureError("Request has a body but is missing the Digest header")
        verify_digest(body, digest_header)

    params = _parse_signature_header(signature_header)
    key_id = params["keyId"]
    public_key_pem = await resolve_public_key(key_id)

    verified_key_id = verify_signature_string(
        method=method,
        path=path,
        headers=headers,
        signature_header=signature_header,
        public_key_pem=public_key_pem,
    )
    return verified_key_id
