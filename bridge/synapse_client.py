"""Async wrapper around the Synapse Client-Server and Admin APIs.

Every interaction this bridge has with Matrix goes through this client --
Constraint #3 in the project brief requires Client-Server/Admin API access
as the primary integration path, with direct database access only as a
documented, abstracted fallback (not implemented here; none of today's
endpoints need it).

The client is intentionally a thin, typed wrapper: it does not know about
ActivityPub, profile rooms, or ghosts -- that logic belongs in the Phase 3/4
bridge handlers that call these methods.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class SynapseError(Exception):
    """Raised when Synapse returns an error response."""

    def __init__(self, status_code: int, errcode: str | None, error: str | None) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.error = error
        super().__init__(f"Synapse error {status_code} {errcode}: {error}")


@dataclass(frozen=True)
class MediaDownload:
    """Result of fetching a piece of media from Synapse's authenticated media API."""

    content: bytes
    content_type: str
    content_disposition: str | None = None


def _room_version_is_v12_or_later(version: str) -> bool:
    """Numeric room versions ("1".."12"+) compare as ints; a handful of
    historical/experimental versions are non-numeric strings (e.g. an
    unstable MSC identifier some servers used before a version got its
    real number) -- treated as pre-v12 rather than raising, since that's
    the older, more conservative power-levels behavior and by far the
    common case for anything not cleanly numeric."""
    try:
        return int(version) >= 12
    except ValueError:
        return False


class SynapseClient:
    """Wraps the subset of the Client-Server and Admin APIs the bridge needs.

    Two tokens are relevant: the AppService's ``as_token`` (used for all
    Client-Server calls, impersonating ghosted users via ``user_id=``) and
    the homeserver admin token (used only for the ``/_synapse/admin`` API).
    Pass whichever is appropriate per call site; most bridge code will use
    the AppService token.
    """

    def __init__(
        self,
        base_url: str,
        *,
        as_token: str | None = None,
        admin_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._as_token = as_token
        self._admin_token = admin_token
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)
        # Fetched once and cached (a running homeserver's own configured
        # default doesn't change) -- see _resolve_room_version_for_creation.
        self._default_room_version: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SynapseClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
        as_user_id: str | None = None,
    ) -> Any:
        # Most C-S API responses are JSON objects, but a few (e.g. the full
        # room state endpoint) are top-level JSON arrays -- Any rather than
        # dict[str, Any] so both are honestly represented; callers that
        # expect an object still get one in practice.
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if content_type:
            headers["Content-Type"] = content_type

        request_params = dict(params or {})
        if as_user_id:
            request_params["user_id"] = as_user_id

        response = await self._client.request(
            method,
            path,
            headers=headers,
            params=request_params,
            json=json_body if content is None else None,
            content=content,
        )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise SynapseError(response.status_code, body.get("errcode"), body.get("error"))
        if response.content:
            return response.json()
        return {}

    # -- Client-Server API (as the AppService) ---------------------------------

    async def whoami(self, *, as_user_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", "/_matrix/client/v3/account/whoami", token=self._as_token, as_user_id=as_user_id
        )

    async def get_versions(self) -> dict[str, Any]:
        """``GET /_matrix/client/versions`` -- public, unauthenticated, and
        side-effect-free, unlike ``register_appservice_user`` and friends.
        Used purely as a startup readiness probe: whether Synapse is
        actually up and answering requests yet, not whether any particular
        credential works."""
        return await self._request("GET", "/_matrix/client/versions")

    async def get_capabilities(self) -> dict[str, Any]:
        """``GET /_matrix/client/v3/capabilities`` -- unlike ``get_versions``,
        this one's authenticated (any valid access token; the AS token
        itself, representing the appservice's own sender user, works fine
        without an explicit ``as_user_id``)."""
        return await self._request("GET", "/_matrix/client/v3/capabilities", token=self._as_token)

    async def _resolve_room_version_for_creation(self, room_version: str | None) -> str:
        """The room version ``create_room`` will actually end up using --
        ``room_version`` itself if given, otherwise this homeserver's own
        configured default (fetched once via ``get_capabilities`` and
        cached, since that's not something that changes while this process
        is running). ``create_room`` needs to know this ahead of the
        request, not just accept whatever Synapse ends up doing, because
        the correct SHAPE of ``power_level_content_override``/
        ``additional_creators`` for "give someone full creator-tier parity"
        is opposite between pre-v12 and v12+ rooms (see ``create_room``'s
        own docstring) -- silently guessing wrong produces a real,
        confirmed-live 400 from Synapse, not a degraded-but-working room."""
        if room_version:
            return room_version
        if self._default_room_version is None:
            try:
                caps = await self.get_capabilities()
                self._default_room_version = (
                    caps.get("capabilities", {}).get("m.room_versions", {}).get("default") or "1"
                )
            except SynapseError:
                # Matches every other version-uncertain default in this
                # file: assume the more restrictive/older behavior (pre-v12
                # power-levels semantics) rather than the newer one, since
                # that's what the overwhelming majority of rooms actually
                # are and it degrades to an explicit, catchable SynapseError
                # rather than silently doing the wrong thing either way.
                self._default_room_version = "1"
        return self._default_room_version

    async def register_appservice_user(self, localpart: str) -> str:
        """Provision a ghost user via the AppService registration flow. Returns the full MXID."""
        body = await self._request(
            "POST",
            "/_matrix/client/v3/register",
            token=self._as_token,
            json_body={"type": "m.login.application_service", "username": localpart},
        )
        return body["user_id"]

    async def set_display_name(self, user_id: str, display_name: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/displayname",
            token=self._as_token,
            json_body={"displayname": display_name},
            as_user_id=user_id,
        )

    async def set_avatar_url(self, user_id: str, mxc_uri: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/avatar_url",
            token=self._as_token,
            json_body={"avatar_url": mxc_uri},
            as_user_id=user_id,
        )

    async def get_profile(self, user_id: str) -> dict[str, Any]:
        """Returns ``{"displayname": ..., "avatar_url": "mxc://..."}`` (keys may be absent)."""
        return await self._request(
            "GET", f"/_matrix/client/v3/profile/{user_id}", token=self._as_token
        )

    async def set_profile_field(self, user_id: str, key: str, value: Any) -> None:
        """Set an arbitrary profile field via MSC4133 (Extensible Profiles)
        -- same endpoint shape as ``set_display_name``/``set_avatar_url``,
        just with a caller-chosen key instead of one of the two fixed ones
        those hardcode. Requires the homeserver to actually support
        MSC4133; callers should expect a ``SynapseError`` on a homeserver
        that doesn't (there's no capability check here, since
        ``/_matrix/client/versions``' advertised unstable feature flag for
        this varies by deployment -- e.g. some Synapse versions use
        ``uk.tcpip.msc4133`` -- so the caller is better positioned to decide
        whether/how to surface that than this generic wrapper is)."""
        await self._request(
            "PUT",
            f"/_matrix/client/v3/profile/{user_id}/{quote(key, safe='')}",
            token=self._as_token,
            json_body={key: value},
            as_user_id=user_id,
        )

    async def delete_profile_field(self, user_id: str, key: str) -> None:
        """Remove an arbitrary profile field via MSC4133 -- the DELETE
        counterpart to ``set_profile_field``, same endpoint shape and same
        homeserver-support caveat. Used to retract a field that's no
        longer supposed to be set (e.g. ``;refresh`` clearing
        ``m.external_handle`` after ``bridge.msc4503_external_handle`` was
        turned off) rather than leaving a stale value in place forever."""
        await self._request(
            "DELETE",
            f"/_matrix/client/v3/profile/{user_id}/{quote(key, safe='')}",
            token=self._as_token,
            as_user_id=user_id,
        )

    async def create_room(
        self,
        *,
        as_user_id: str | None = None,
        name: str | None = None,
        topic: str | None = None,
        invite: list[str] | None = None,
        is_direct: bool = False,
        preset: str = "private_chat",
        avatar_mxc: str | None = None,
        room_type: str | None = None,
        room_version: str | None = None,
        predecessor: dict[str, Any] | None = None,
        join_rule: str | None = None,
        power_level_content_override: dict[str, Any] | None = None,
        additional_creators: list[str] | None = None,
    ) -> str:
        """Create a room. Returns the room ID.

        ``room_version``, if omitted, falls back to whatever this
        homeserver's own configured default room version is -- this method
        resolves that default itself (see ``_resolve_room_version_for_creation``)
        precisely so ``additional_creators`` (below) can always do the right
        thing regardless, without every caller needing to know or guess
        which version applies.

        ``additional_creators`` gives someone OTHER than the room's own
        creator (``as_user_id`` above) genuine creator-tier parity --
        permanent, unrevocable-by-ordinary-moderation status, for a peer
        that should stand alongside the creator (e.g. the bridge bot,
        historically kept at the same level as whichever ghost created the
        room) rather than someone who should merely be a highly privileged
        but still-revocable member (plain ``power_level_content_override``
        instead). This is expressed completely differently depending on
        the room's actual version, which is why this method handles the
        translation itself instead of leaving it to callers:

        - Room v12+: sets ``creation_content.additional_creators`` -- v12's
          own dedicated mechanism, since v12 reserves numeric power level
          100 for creators specifically and rejects a ``power_levels``
          event listing a creator (or anyone else claiming 100 outside
          this mechanism) at all.
        - Pre-v12: no such mechanism exists; expressed instead as an
          explicit ``power_level_content_override.users`` grant of 100 for
          each of ``additional_creators``, ALWAYS restating ``as_user_id``
          itself at 100 in the same override too. That restating is
          required, not cosmetic: Synapse rejects a ``power_level_content_override``
          that grants anyone power level 100 without the actual room
          creator ALSO appearing in it at 100 (confirmed live 2026-07-07 --
          not itself a version-specific rule, just a general sanity check
          that doesn't otherwise come up, since a plain ``users`` override
          not involving level 100 at all is unaffected).

        ``room_type`` sets ``creation_content.type`` -- a room's type is
        immutable once created (unlike ordinary state), so this is the only
        point it can ever be set; there's no way to retrofit it onto an
        already-existing room afterwards.

        ``predecessor`` sets ``creation_content.predecessor`` (``{"room_id":
        ..., "event_id": ...}``), the same field a native ``/upgraderoom``
        populates -- it's what makes bridge-aware clients show "this room
        continues from an earlier room" pointing back at the old room, the
        mirror image of the ``m.room.tombstone`` sent in the old room
        pointing forward. Just as immutable/one-shot as ``room_type``.

        ``join_rule``, if given (e.g. ``"knock"``), overrides the preset's
        own default join rule via an explicit ``m.room.join_rules`` entry in
        ``initial_state`` -- per the C-S API spec, an explicit
        ``initial_state`` entry for a given event type takes priority over
        whatever the preset would otherwise have generated for it.

        ``power_level_content_override`` is applied on top of the preset's
        normally-computed ``m.room.power_levels`` content (per the C-S API
        spec), not a full replacement -- e.g. ``{"users": {mxid: 99}}``
        grants that one user near-admin while every other default (redact
        threshold, invite threshold, ...) stays whatever the preset would
        normally set. Deliberately NOT the right tool for granting someone
        exactly level 100 unless they're the actual creator -- see
        ``additional_creators`` above for that.
        """
        effective_version = await self._resolve_room_version_for_creation(room_version)
        is_v12_plus = _room_version_is_v12_or_later(effective_version)

        override = dict(power_level_content_override) if power_level_content_override else None
        if additional_creators and not is_v12_plus:
            override = dict(override or {})
            users_override = dict(override.get("users") or {})
            if as_user_id:
                users_override.setdefault(as_user_id, 100)
            for peer in additional_creators:
                users_override[peer] = 100
            override["users"] = users_override

        body: dict[str, Any] = {"preset": preset, "is_direct": is_direct}
        if room_version:
            body["room_version"] = room_version
        if name:
            body["name"] = name
        if topic:
            body["topic"] = topic
        if invite:
            body["invite"] = invite
        initial_state: list[dict[str, Any]] = []
        if avatar_mxc:
            initial_state.append({"type": "m.room.avatar", "state_key": "", "content": {"url": avatar_mxc}})
        if join_rule:
            initial_state.append({"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": join_rule}})
        if initial_state:
            body["initial_state"] = initial_state
        additional_creators_for_v12 = additional_creators if (additional_creators and is_v12_plus) else None
        if room_type or predecessor or additional_creators_for_v12:
            creation_content: dict[str, Any] = {}
            if room_type:
                creation_content["type"] = room_type
            if predecessor:
                creation_content["predecessor"] = predecessor
            if additional_creators_for_v12:
                creation_content["additional_creators"] = additional_creators_for_v12
            body["creation_content"] = creation_content
        if override:
            body["power_level_content_override"] = override
        result = await self._request(
            "POST", "/_matrix/client/v3/createRoom", token=self._as_token, json_body=body, as_user_id=as_user_id
        )
        return result["room_id"]

    async def get_joined_members(self, room_id: str, *, as_user_id: str | None = None) -> list[str]:
        """``GET .../joined_members`` -- the user IDs currently joined to
        ``room_id``. ``as_user_id`` must itself already be joined (this is
        an ordinary Client-Server endpoint, not an admin one), which every
        bridge-created room's bot/ghost creator always is."""
        result = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{room_id}/joined_members", token=self._as_token, as_user_id=as_user_id
        )
        return list(result.get("joined", {}).keys())

    async def invite_user(self, room_id: str, user_id: str, *, as_user_id: str | None = None) -> None:
        await self._request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/invite",
            token=self._as_token,
            json_body={"user_id": user_id},
            as_user_id=as_user_id,
        )

    async def join_room(self, room_id_or_alias: str, *, as_user_id: str | None = None) -> str:
        result = await self._request(
            "POST",
            f"/_matrix/client/v3/join/{room_id_or_alias}",
            token=self._as_token,
            as_user_id=as_user_id,
        )
        return result["room_id"]

    async def kick_user(
        self, room_id: str, user_id: str, *, as_user_id: str | None = None, reason: str | None = None
    ) -> None:
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        await self._request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/kick",
            token=self._as_token,
            json_body=body,
            as_user_id=as_user_id,
        )

    async def send_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
        *,
        as_user_id: str | None = None,
    ) -> str:
        # state_key is a single path *segment* per the C-S API, but ours are
        # often full IRIs (e.g. "activitypub://https://instance/users/x") --
        # loaded with literal "/"s that, left unescaped, split the path into
        # extra segments Synapse can't route, 404ing every single call.
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{quote(state_key, safe='')}",
            token=self._as_token,
            json_body=content,
            as_user_id=as_user_id,
        )
        return result["event_id"]

    async def get_room_state(
        self, room_id: str, event_type: str, state_key: str = "", *, as_user_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/state/{event_type}/{quote(state_key, safe='')}",
            token=self._as_token,
            as_user_id=as_user_id,
        )

    async def get_full_room_state(self, room_id: str, *, as_user_id: str | None = None) -> list[dict[str, Any]]:
        """``GET .../state`` -- every current state event in the room, as a
        flat list. For when the specific event type/state_key isn't known
        in advance -- e.g. recovering which ActivityPub actor an old room
        represents from its own permanent ``m.bridge`` state, without
        already knowing the exact state_key to fetch it directly via
        ``get_room_state``."""
        return await self._request(
            "GET", f"/_matrix/client/v3/rooms/{room_id}/state", token=self._as_token, as_user_id=as_user_id
        )

    async def send_message_event(
        self,
        room_id: str,
        content: dict[str, Any],
        *,
        event_type: str = "m.room.message",
        as_user_id: str | None = None,
        txn_id: str | None = None,
        ts: int | None = None,
    ) -> str:
        """Send a message event. ``ts`` (Unix milliseconds) uses the
        Application Service API's "timestamp massaging" (the ``?ts=`` query
        parameter, no special registration capability needed) to set the
        event's displayed ``origin_server_ts`` to something other than
        "now" -- e.g. an ActivityPub post's own ``published`` time when
        mirroring it, so it shows the time it was actually posted rather
        than the time the bridge happened to process it. This does NOT
        change the event's actual position in the room's DAG (per spec, it
        is still appended at the tip as if sent "now") -- only its
        displayed timestamp."""
        txn_id = txn_id or uuid.uuid4().hex
        params: dict[str, Any] | None = {"ts": ts} if ts is not None else None
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/send/{event_type}/{txn_id}",
            token=self._as_token,
            json_body=content,
            params=params,
            as_user_id=as_user_id,
        )
        return result["event_id"]

    async def get_event(self, room_id: str, event_id: str, *, as_user_id: str | None = None) -> dict[str, Any]:
        """Fetch a single event's full content -- used to build a compact
        preview of a post being reacted to/reposted (see
        ``bridge.inbox_dispatch._notify_post_owner``) without needing to
        re-derive it from anywhere else, since a notification about it is
        sent into a different room (the bot's DM with the owner) than the
        one the actual post event lives in."""
        return await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/event/{event_id}",
            token=self._as_token,
            as_user_id=as_user_id,
        )

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
        *,
        reason: str | None = None,
        as_user_id: str | None = None,
        txn_id: str | None = None,
    ) -> str:
        txn_id = txn_id or uuid.uuid4().hex
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{txn_id}",
            token=self._as_token,
            json_body=body,
            as_user_id=as_user_id,
        )
        return result["event_id"]

    async def get_relations(
        self,
        room_id: str,
        event_id: str,
        *,
        rel_type: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        as_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /_matrix/client/v1/rooms/{roomId}/relations/{eventId}[/{relType}[/{eventType}]]``,
        flattened across pagination (bounded to 10 pages -- a single post's
        reactions or thread replies realistically never need more).

        Used to read back a post's ``m.reaction`` annotations (``rel_type=
        "m.annotation", event_type="m.reaction"``) and its thread replies
        (``rel_type="m.thread"``) for the public HTML rendering of a post/
        profile (see ``bridge.activitypub.routes``) -- nothing else in the
        bridge needs to read relations back out of Matrix, since every other
        flow tracks what it needs itself via ``ActorRepository``.
        """
        path = f"/_matrix/client/v1/rooms/{room_id}/relations/{event_id}"
        if rel_type:
            path += f"/{quote(rel_type, safe='')}"
            if event_type:
                path += f"/{quote(event_type, safe='')}"

        events: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": limit}
        for _ in range(10):
            result = await self._request("GET", path, token=self._as_token, params=params, as_user_id=as_user_id)
            events.extend(result.get("chunk", []))
            next_batch = result.get("next_batch")
            if not next_batch:
                break
            params = {"limit": limit, "from": next_batch}
        return events

    async def get_room_messages(
        self,
        room_id: str,
        *,
        limit: int = 20,
        direction: str = "b",
        from_token: str | None = None,
        as_user_id: str | None = None,
    ) -> dict[str, Any]:
        """``from_token``, if given, resumes pagination from a previous
        call's own ``"end"`` token -- lets a caller (the public profile
        page's "Older posts" pagination -- see ``bridge.activitypub.routes``)
        page through a room's full history a request at a time, using
        Synapse's own native cursor rather than the bridge re-fetching (or
        storing) anything about history it's already paged through."""
        params: dict[str, Any] = {"limit": limit, "dir": direction}
        if from_token:
            params["from"] = from_token
        return await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages",
            token=self._as_token,
            params=params,
            as_user_id=as_user_id,
        )

    async def upload_media(
        self, data: bytes, content_type: str, filename: str, *, as_user_id: str | None = None
    ) -> str:
        """Upload media to the homeserver's media repository. Returns the ``mxc://`` URI."""
        result = await self._request(
            "POST",
            "/_matrix/media/v3/upload",
            token=self._as_token,
            content=data,
            content_type=content_type,
            params={"filename": filename},
            as_user_id=as_user_id,
        )
        return result["content_uri"]

    async def download_media(self, server_name: str, media_id: str) -> MediaDownload:
        """Download media via the (authenticated) Client-Server media API, using our own
        ``as_token``.

        This exists so the bridge can re-serve media to the public internet itself (see
        ``GET /media/{server}/{id}`` in ``bridge.activitypub.routes``): since Matrix media
        downloads require an access token (MSC3916), a remote fediverse server has no way
        to fetch ``.../​_matrix/client/v1/media/download/...`` directly. Always fetches the
        full file -- Range/206 handling for the public-facing response is the route's job.
        """
        response = await self._client.get(
            f"/_matrix/client/v1/media/download/{server_name}/{media_id}",
            headers={"Authorization": f"Bearer {self._as_token}"},
        )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise SynapseError(response.status_code, body.get("errcode"), body.get("error"))
        return MediaDownload(
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            content_disposition=response.headers.get("content-disposition"),
        )

    async def verify_openid_token(self, access_token: str) -> str | None:
        """Verify a Matrix widget's OpenID token (obtained via the Widget
        API's ``get_openid_token`` action) and return the ``@user:server``
        it belongs to, or None if it's invalid/expired.

        Hits Synapse's own federation endpoint
        (``/_matrix/federation/v1/openid/userinfo``) directly at
        ``base_url`` rather than doing real federation server-discovery
        (well-known/SRV) against ``matrix_server_name`` -- this only ever
        verifies tokens for THIS homeserver's own users (see
        ``bridge.widget``'s caller, which checks ``matrix_server_name``
        equals our own ``server_name`` before ever calling this), and a
        typical single-process Synapse serves both the client-server and
        federation APIs off the same listener/port, so hitting our own
        already-configured ``base_url`` reaches it directly with no
        certificate/DNS-delegation complexity at all. This endpoint is
        deliberately unauthenticated/public per the Matrix spec (any
        relying party can verify any token), so no token/header of our own
        is needed for the call itself."""
        response = await self._client.get(
            "/_matrix/federation/v1/openid/userinfo", params={"access_token": access_token}
        )
        if response.status_code >= 400:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        sub = body.get("sub")
        return sub if isinstance(sub, str) else None

    # -- Admin API ---------------------------------------------------------

    async def admin_register_appservice_user(self, localpart: str) -> str:
        """Admin-API equivalent of ``register_appservice_user``, for use outside AS auth."""
        body = await self._request(
            "POST",
            "/_matrix/client/v3/register",
            token=self._admin_token,
            json_body={"type": "m.login.application_service", "username": localpart},
        )
        return body["user_id"]

    async def admin_whois(self, user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/_synapse/admin/v1/whois/{user_id}", token=self._admin_token
        )

    async def admin_deactivate_user(self, user_id: str, *, erase: bool = False) -> None:
        await self._request(
            "POST",
            f"/_synapse/admin/v1/deactivate/{user_id}",
            token=self._admin_token,
            json_body={"erase": erase},
        )

    async def admin_is_server_admin(self, user_id: str) -> bool:
        """Whether ``user_id`` is flagged as a Synapse server admin (the
        ``admin`` field on the Admin API's user-details endpoint) -- used to
        gate bridge commands that manage rooms representing someone *else's*
        (a remote fediverse account's) identity to actual homeserver admins,
        not just anyone who happens to be in the room."""
        result = await self._request(
            "GET", f"/_synapse/admin/v2/users/{user_id}", token=self._admin_token
        )
        return bool(result.get("admin"))

    async def admin_list_joined_rooms(self, user_id: str) -> list[str]:
        """Every room ``user_id`` currently has ``join`` membership in --
        used by ``delete profile`` to sweep every bridge-managed room
        (Remote User Rooms, ghost DM/chat rooms) a user is in, without
        needing our own bookkeeping to already know the full list up front."""
        result = await self._request(
            "GET", f"/_synapse/admin/v1/users/{user_id}/joined_rooms", token=self._admin_token
        )
        return result.get("joined_rooms") or []
