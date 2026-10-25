"""Login to a remote instance for the widget's Browse feature to view
timelines, search results, and profiles that instance requires
authentication for -- two unrelated flows for the two unrelated families
of software Browse supports (see ``bridge.widget_browse``'s own
``_COMPATIBLE_SOFTWARE``/``_MISSKEY_FAMILY_SOFTWARE``):

Mastodon-API-compatible instances get OAuth (plus a password-grant
fallback): dynamically registers a bridge-owned OAuth app on the target
domain (cached in memory -- see ``_APP_CACHE``'s own note) and runs the
standard authorization-code redirect flow. The password grant is an
explicit fallback for instances that still support it (many large
instances have removed it entirely; that degrades to a clear
``InstanceAuthError``, not a crash).

Misskey-family instances get MiAuth instead (``start_miauth``/
``complete_miauth``) -- a simpler redirect flow with no app-registration
step at all, and no password-grant equivalent exposed by this module (see
``login_with_password``'s own guard). Both land in the SAME callback URL
(``bridge.widget_browse.browse_login_callback``), which tells them apart
by which query params actually show up (MiAuth's own redirect carries
``session``, OAuth's carries ``code``/``state``) rather than needing two
separate registered callback paths.

Resulting tokens are durable either way -- ``ActorRepository.
save_instance_login``/``get_instance_login``/..., keyed by the bridge's
own ``matrix_user_id`` (see ``InstanceLoginRecord``'s docstring for why
that's the right key instead of a browser cookie: it's already how every
widget session resolves identity, so a login done once works from any
device the user later opens the widget from).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import Request

from bridge.repository import InstanceLoginRecord

logger = logging.getLogger(__name__)

_REDIRECT_PATH = "/widget/api/browse/login/callback"
_SCOPES = "read"
_APP_NAME = "Fediverse Bridge Widget"
_MIAUTH_PERMISSIONS = "read:account"

# Per-domain OAuth app registration -- (client_id, client_secret). In
# memory only, deliberately not persisted (see the "ask before adding a
# new DB table" standing policy -- the resulting USER tokens need their
# own table regardless, see InstanceLoginRecord, but this registration
# doesn't need to survive a restart the way those do). Losing it on
# restart just means the next login on that domain registers a fresh app,
# which every Mastodon-API-compatible instance allows freely. The one
# edge case this doesn't cover: a user mid-redirect (already sent to the
# instance) when the bridge restarts gets a login failure on their way
# back and has to retry -- accepted as rare and self-recovering.
_APP_CACHE: dict[str, tuple[str, str]] = {}

# One in-flight OAuth redirect, keyed by an unguessable per-flow token
# (the "state" param) -- same trust model as bridge.widget's own
# _sessions dict: opaque, unguessable, time-limited, consumed once. This
# is what reconnects the instance's bare browser-GET callback back to the
# bridge user who started the flow (that request carries no bridge
# session of its own).
_PENDING: dict[str, tuple[str, str, str, str, float]] = {}
_PENDING_TTL_SECONDS = 600.0

# Same idea as _PENDING, for a MiAuth session in flight -- (matrix_user_id,
# domain, expiry). No client_id/secret to carry here: unlike OAuth, MiAuth
# has no app-registration step at all.
_MIAUTH_PENDING: dict[str, tuple[str, str, float]] = {}
_MIAUTH_CALLBACK_PATH = _REDIRECT_PATH  # same URL as OAuth's -- see this module's own docstring


class InstanceAuthError(Exception):
    """Raised for any login failure meant to be shown to the user verbatim
    (wrong credentials, instance doesn't support this grant, unreachable,
    expired/reused login link, ...) -- never for a bug in this module."""


def _public_base_url(request: Request) -> str:
    return request.app.state.config.bridge.public_base_url.rstrip("/")


async def _register_app(request: Request, domain: str) -> tuple[str, str]:
    if domain in _APP_CACHE:
        return _APP_CACHE[domain]
    http_client: httpx.AsyncClient = request.app.state.http_client
    redirect_uri = f"{_public_base_url(request)}{_REDIRECT_PATH}"
    try:
        resp = await http_client.post(
            f"https://{domain}/api/v1/apps",
            data={
                "client_name": _APP_NAME,
                "redirect_uris": redirect_uri,
                "scopes": _SCOPES,
                "website": _public_base_url(request),
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        client_id = data["client_id"]
        client_secret = data["client_secret"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise InstanceAuthError(
            f"{domain} doesn't support app registration -- sign-in isn't available here."
        ) from exc
    _APP_CACHE[domain] = (client_id, client_secret)
    return client_id, client_secret


def _prune_pending() -> None:
    now = time.monotonic()
    for state in [s for s, (*_, exp) in _PENDING.items() if exp < now]:
        _PENDING.pop(state, None)
    for session in [s for s, (*_, exp) in _MIAUTH_PENDING.items() if exp < now]:
        _MIAUTH_PENDING.pop(session, None)


async def start_oauth(request: Request, *, matrix_user_id: str, domain: str) -> str:
    """Begin the authorization-code flow. Returns the URL the widget
    should open in a new tab/window -- the widget's own iframe can't
    navigate top-level to a cross-origin page itself."""
    _prune_pending()
    client_id, client_secret = await _register_app(request, domain)
    state = secrets.token_urlsafe(32)
    _PENDING[state] = (matrix_user_id, domain, client_id, client_secret, time.monotonic() + _PENDING_TTL_SECONDS)
    redirect_uri = f"{_public_base_url(request)}{_REDIRECT_PATH}"
    return (
        f"https://{domain}/oauth/authorize"
        f"?client_id={client_id}&redirect_uri={redirect_uri}"
        f"&response_type=code&scope={_SCOPES}&state={state}"
    )


async def complete_oauth(request: Request, *, state: str, code: str) -> tuple[str, str]:
    """Exchange the callback's ``code`` for a token and persist it.
    Returns ``(matrix_user_id, domain)`` so the callback page can render
    something sensible. Raises ``InstanceAuthError`` on failure, including
    an unrecognized/expired state (e.g. the bridge restarted mid-flow, or
    the link was already used)."""
    _prune_pending()
    pending = _PENDING.pop(state, None)
    if pending is None:
        raise InstanceAuthError("This sign-in link has expired or was already used. Please try again.")
    matrix_user_id, domain, client_id, client_secret, _ = pending
    redirect_uri = f"{_public_base_url(request)}{_REDIRECT_PATH}"
    token = await _token_request(
        request,
        domain,
        grant_type="authorization_code",
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
    await _save_token(request, matrix_user_id=matrix_user_id, domain=domain, auth_method="oauth", token=token)
    return matrix_user_id, domain


async def start_miauth(request: Request, *, matrix_user_id: str, domain: str) -> str:
    """Begin a MiAuth sign-in (the Misskey-family login flow -- see this
    module's own docstring). Unlike ``start_oauth``, there's no app pre-
    registration step at all -- just a random session id the instance
    remembers for the lifetime of this one login attempt. Returns the URL
    the widget should open in a new tab/window, same contract as
    ``start_oauth``."""
    _prune_pending()
    session = secrets.token_urlsafe(32)
    _MIAUTH_PENDING[session] = (matrix_user_id, domain, time.monotonic() + _PENDING_TTL_SECONDS)
    callback = f"{_public_base_url(request)}{_MIAUTH_CALLBACK_PATH}"
    return (
        f"https://{domain}/miauth/{session}"
        f"?name={quote(_APP_NAME)}&callback={quote(callback)}&permission={_MIAUTH_PERMISSIONS}"
    )


async def complete_miauth(request: Request, *, session: str) -> tuple[str, str]:
    """Exchange a completed MiAuth session for a token and persist it.
    Returns ``(matrix_user_id, domain)``, same contract as
    ``complete_oauth`` (including raising ``InstanceAuthError`` the same
    way on an unrecognized/expired session)."""
    _prune_pending()
    pending = _MIAUTH_PENDING.pop(session, None)
    if pending is None:
        raise InstanceAuthError("This sign-in link has expired or was already used. Please try again.")
    matrix_user_id, domain, _ = pending
    http_client: httpx.AsyncClient = request.app.state.http_client
    try:
        resp = await http_client.post(f"https://{domain}/api/miauth/{session}/check", timeout=15.0)
    except httpx.HTTPError as exc:
        raise InstanceAuthError(f"Couldn't reach {domain}.") from exc
    if resp.status_code >= 400:
        raise InstanceAuthError(f"{domain} rejected the login (HTTP {resp.status_code}).")
    try:
        body = resp.json()
    except ValueError as exc:
        raise InstanceAuthError(f"{domain} returned an unexpected response.") from exc
    if not body.get("ok") or not isinstance(body.get("token"), str):
        raise InstanceAuthError(f"{domain} didn't confirm the sign-in -- it may have been cancelled.")
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    account_handle = ("@" + user["username"]) if isinstance(user.get("username"), str) else ""
    now = datetime.now(timezone.utc).isoformat()
    repo = request.app.state.repository
    await repo.save_instance_login(
        InstanceLoginRecord(
            matrix_user_id=matrix_user_id,
            instance_domain=domain,
            auth_method="miauth",
            access_token=body["token"],
            refresh_token=None,
            token_type="misskey_i",
            scope=_MIAUTH_PERMISSIONS,
            account_handle=account_handle,
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    return matrix_user_id, domain


async def get_misskey_token(request: Request, *, matrix_user_id: str, domain: str) -> str | None:
    """The stored MiAuth token for this bridge user at this instance, or
    ``None`` if they aren't logged in there. Unlike ``get_bearer_token``,
    there's no refresh step -- a MiAuth token doesn't expire the way an
    OAuth access token does; it stays valid until the user revokes it from
    their own Misskey settings, at which point the next authenticated call
    simply gets refused and the caller falls back to "please sign in", the
    same as an unauthenticated request would."""
    repo = request.app.state.repository
    login = await repo.get_instance_login(matrix_user_id, domain)
    if login is None or login.auth_method != "miauth":
        return None
    return login.access_token


async def login_with_password(
    request: Request, *, matrix_user_id: str, domain: str, username: str, password: str
) -> None:
    """Password-grant fallback, offered when a user can't or doesn't want
    to use OAuth. Raises ``InstanceAuthError`` on wrong credentials, an
    instance that has removed this grant entirely (common on large
    instances), or unreachability."""
    client_id, client_secret = await _register_app(request, domain)
    token = await _token_request(
        request,
        domain,
        grant_type="password",
        username=username,
        password=password,
        client_id=client_id,
        client_secret=client_secret,
        scope=_SCOPES,
    )
    await _save_token(request, matrix_user_id=matrix_user_id, domain=domain, auth_method="password", token=token)


async def _token_request(request: Request, domain: str, **params: str) -> dict:
    http_client: httpx.AsyncClient = request.app.state.http_client
    try:
        resp = await http_client.post(f"https://{domain}/oauth/token", data=params, timeout=15.0)
    except httpx.HTTPError as exc:
        raise InstanceAuthError(f"Couldn't reach {domain}.") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            pass
        raise InstanceAuthError(detail or f"{domain} rejected the login (HTTP {resp.status_code}).")
    try:
        return resp.json()
    except ValueError as exc:
        raise InstanceAuthError(f"{domain} returned an unexpected response.") from exc


async def _save_token(request: Request, *, matrix_user_id: str, domain: str, auth_method: str, token: dict) -> None:
    repo = request.app.state.repository
    now = datetime.now(timezone.utc).isoformat()
    expires_at = None
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        expires_at = datetime.fromtimestamp(time.time() + float(expires_in), tz=timezone.utc).isoformat()
    account_handle = await _resolve_account_handle(request, domain, token["access_token"])
    await repo.save_instance_login(
        InstanceLoginRecord(
            matrix_user_id=matrix_user_id,
            instance_domain=domain,
            auth_method=auth_method,
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token"),
            token_type=token.get("token_type", "Bearer"),
            scope=token.get("scope", _SCOPES),
            account_handle=account_handle,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
    )


async def _resolve_account_handle(request: Request, domain: str, access_token: str) -> str:
    http_client: httpx.AsyncClient = request.app.state.http_client
    try:
        resp = await http_client.get(
            f"https://{domain}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("acct", "") or ""
    except (httpx.HTTPError, ValueError):
        return ""


async def get_bearer_token(request: Request, *, matrix_user_id: str, domain: str) -> str | None:
    """The valid access token for this bridge user at this instance, or
    ``None`` if they aren't logged in there. Attempts a refresh once if
    the stored token is past its ``expires_at`` and a refresh token is on
    file; if that fails (including because this process never re-cached
    the original app registration after a restart -- see ``_APP_CACHE``'s
    own note) the stale login is dropped and ``None`` is returned, so the
    caller falls back to the ordinary "please sign in" state rather than
    retrying a dead token forever."""
    repo = request.app.state.repository
    login = await repo.get_instance_login(matrix_user_id, domain)
    if login is None:
        return None
    if not login.expires_at:
        return login.access_token
    if datetime.fromisoformat(login.expires_at) > datetime.now(timezone.utc):
        return login.access_token
    if not login.refresh_token:
        await repo.delete_instance_login(matrix_user_id, domain)
        return None
    try:
        client_id, client_secret = await _register_app(request, domain)
        token = await _token_request(
            request,
            domain,
            grant_type="refresh_token",
            refresh_token=login.refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except InstanceAuthError:
        await repo.delete_instance_login(matrix_user_id, domain)
        return None
    await _save_token(request, matrix_user_id=matrix_user_id, domain=domain, auth_method=login.auth_method, token=token)
    return token["access_token"]
