"""WebFinger (RFC 7033) resolution, both directions.

Outbound: turn ``user@instance.org`` into that user's Actor IRI, the first
step of the bot's ``follow @user@instance.org`` command.

Inbound: answer ``GET /.well-known/webfinger?resource=acct:user@bridge.domain``
for our own locally-linked profiles so remote servers can discover them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request

from bridge.activitypub.remote_actor import RemoteActorFetchError, fetch_actor

ACCT_RE = re.compile(r"^acct:([^@]+)@(.+)$")


class WebfingerError(Exception):
    """Raised when a webfinger lookup fails or returns an unusable document."""


class WebfingerNotFoundError(WebfingerError):
    """The server was actually reached and gave an authoritative answer:
    there's no such account. Either a 4xx status (most commonly 404), or a
    well-formed response with no matching ActivityPub link at all (e.g. a
    webfinger endpoint that only knows about email/OpenID identities, not a
    real fediverse account). Distinct from ``WebfingerUnreachableError``
    because a caller can tell the user "no such account" with confidence
    here, rather than "couldn't check right now"."""


class WebfingerUnreachableError(WebfingerError):
    """The server's answer, if it has one, couldn't be gotten at all: DNS
    failure, connection refused/timed out, TLS error, a 5xx (its own
    problem, not a verdict on the account), or a response that couldn't
    even be parsed. Distinct from ``WebfingerNotFoundError`` because this
    means genuinely not knowing whether the account exists, only that it
    couldn't be checked right now -- worth telling the user differently
    (try again later) than a confirmed "no such account"."""


@dataclass(frozen=True)
class ParsedAcct:
    username: str
    domain: str


def parse_acct(resource: str) -> ParsedAcct:
    """Parse an ``acct:user@domain`` resource string."""
    match = ACCT_RE.match(resource)
    if not match:
        raise WebfingerError(f"Not a well-formed acct: URI: {resource!r}")
    return ParsedAcct(username=match.group(1), domain=match.group(2))


async def resolve_remote_actor_id(http_client: httpx.AsyncClient, acct: str) -> str:
    """Resolve ``user@instance.org`` (with or without a leading ``@``) to an Actor IRI.

    Performs ``GET https://instance.org/.well-known/webfinger?resource=acct:user@instance.org``
    and extracts the ``self`` link with type ``application/activity+json``.

    Never lets a raw ``httpx`` exception escape -- every failure mode ends
    up as either ``WebfingerNotFoundError`` (the server confirmed there's
    no such account) or ``WebfingerUnreachableError`` (couldn't get an
    answer at all), so a caller can tell those apart and give the user an
    accurate message instead of an unhandled exception (and a bare 404, or
    a DNS/connection failure, showing up as a crash in the logs instead of
    something callers ever see or handle).
    """
    handle = acct[1:] if acct.startswith("@") else acct
    if "@" not in handle:
        raise WebfingerError(f"Expected user@domain, got {acct!r}")
    _username, domain = handle.split("@", 1)

    url = f"https://{domain}/.well-known/webfinger"
    # RFC 7033 mandates a JRD response but doesn't require the client to send
    # any particular Accept header -- most servers reply with JSON either
    # way, but at least one real one (Mastodon) strictly content-negotiates
    # on it and returns a bare 400 Bad Request to a request with no Accept
    # header at all, rather than defaulting to JSON.
    try:
        response = await http_client.get(
            url, params={"resource": f"acct:{handle}"}, headers={"Accept": "application/jrd+json, application/json"}
        )
    except httpx.RequestError as exc:
        # Never got a response at all -- DNS failure, connection refused,
        # timeout, TLS error, ... -- so this is "couldn't check", not "no
        # such account".
        raise WebfingerUnreachableError(f"Could not reach {domain}: {exc}") from exc

    if response.status_code >= 500:
        # The server's own problem, not a verdict on whether the account
        # exists.
        raise WebfingerUnreachableError(
            f"{domain} returned a server error ({response.status_code}) resolving {handle!r}"
        )
    if response.status_code >= 400:
        # An authoritative "no" from a server that IS actually there --
        # most commonly a plain 404 for an unknown user.
        raise WebfingerNotFoundError(f"{domain} has no account {handle!r} ({response.status_code})")

    try:
        document = response.json()
    except ValueError as exc:
        raise WebfingerUnreachableError(
            f"{domain} returned a webfinger response that isn't valid JSON for {handle!r}"
        ) from exc

    for link in document.get("links", []):
        if link.get("rel") == "self" and "activity+json" in (link.get("type") or ""):
            href = link.get("href")
            if href:
                return href

    raise WebfingerNotFoundError(f"No ActivityPub actor link found in webfinger response for {handle!r}")


async def resolve_invite_code(request: Request, code: str, domain: str) -> tuple[dict[str, Any], str]:
    """Resolve a Shoot guild invite code to ``(invite_code_object,
    qualified_mention)``, per FEP-bebd's optional WebFinger extension
    (``?resource=invite:CODE@domain``).

    ``qualified_mention`` is the exact ``code@domain`` string a joining
    ``Follow`` must carry in its ``instrument`` field -- confirmed live
    2026-07-14 against Shoot's own ``FollowActivityHandler`` source: it is
    NOT the InviteCode object's own IRI (an earlier version of this
    function assumed that and got a live 400 "Shoot only supports
    InviteCodes from itself"), and the ``domain`` half specifically must
    match the guild's ``federation.webapp_url`` hostname -- which can
    differ from whichever host actually answers WebFinger (confirmed live:
    ``chat.understars.dev`` serves WebFinger for codes canonically
    qualified as ``@understars.dev``). Rather than reconstruct this from
    whatever domain we happened to query, it's read straight back out of
    the WebFinger response's own ``subject`` field (stripping its
    ``invite:`` prefix), which reports the canonical qualification
    regardless of which host actually served the response.

    The object itself is fetched (a SIGNED GET -- unlike the WebFinger step
    itself, Shoot's own maintainer has said "basically everything is
    private and requires an authorised user (via http signatures) to
    access", see ``bridge.activitypub.remote_actor.fetch_actor``, already
    signs every GET as the bridge's own service actor) so its
    ``attributedTo`` (the guild being invited to) is available too.

    Raises the same ``WebfingerNotFoundError``/``WebfingerUnreachableError``
    split as ``resolve_remote_actor_id`` for the WebFinger step; a failure
    fetching the resolved IRI itself raises ``WebfingerUnreachableError``
    (wrapping the underlying ``RemoteActorFetchError``), since by that point
    WebFinger has already given an authoritative "the code resolves to
    this" answer -- any failure past that point is a reachability problem,
    not a "no such code" one.
    """
    url = f"https://{domain}/.well-known/webfinger"
    try:
        response = await request.app.state.http_client.get(
            url, params={"resource": f"invite:{code}@{domain}"},
            headers={"Accept": "application/jrd+json, application/json"},
        )
    except httpx.RequestError as exc:
        raise WebfingerUnreachableError(f"Could not reach {domain}: {exc}") from exc

    if response.status_code >= 500:
        raise WebfingerUnreachableError(
            f"{domain} returned a server error ({response.status_code}) resolving invite code {code!r}"
        )
    if response.status_code >= 400:
        raise WebfingerNotFoundError(f"{domain} has no invite code {code!r} ({response.status_code})")

    try:
        document = response.json()
    except ValueError as exc:
        raise WebfingerUnreachableError(
            f"{domain} returned a webfinger response that isn't valid JSON for invite code {code!r}"
        ) from exc

    invite_code_url: str | None = None
    for link in document.get("links", []):
        if link.get("rel") == "self" and "activity+json" in (link.get("type") or ""):
            href = link.get("href")
            if href:
                invite_code_url = href
                break
    if invite_code_url is None:
        raise WebfingerNotFoundError(f"No InviteCode link found in webfinger response for code {code!r}")

    subject = document.get("subject")
    qualified_mention = subject.removeprefix("invite:") if isinstance(subject, str) else f"{code}@{domain}"

    try:
        invite_code_doc = await fetch_actor(request, invite_code_url)
    except RemoteActorFetchError as exc:
        raise WebfingerUnreachableError(f"Could not fetch InviteCode object at {invite_code_url}: {exc}") from exc
    return invite_code_doc, qualified_mention


def build_local_webfinger_document(
    *, username: str, bridge_domain: str, actor_url: str
) -> dict[str, Any]:
    """Build the JRD document this bridge serves for one of its locally-linked profiles."""
    return {
        "subject": f"acct:{username}@{bridge_domain}",
        "aliases": [actor_url],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url,
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_url,
            },
        ],
    }
