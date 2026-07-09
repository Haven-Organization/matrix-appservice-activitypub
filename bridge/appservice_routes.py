"""Matrix Application Service transaction receiver.

This is the homeserver -> bridge half of the AS API: Synapse PUTs batches of
Matrix room events here whenever something happens in a room the bridge (or
one of its ghosted users) is a member of, authenticating itself with the
shared ``hs_token`` from the registration file.

It authenticates, deduplicates by transaction ID via ``ActorRepository``
(required for idempotency per the AS spec, since Synapse retries on
timeout/error -- persisted, so it's safe across a bridge restart too), then
for each event tries, in order: auto-accepting invites extended to our
ghosts/bot; auto-accepting a knock on a room the bridge manages, by
inviting the knocker (every fediverse-bridged room uses the knock join rule
rather than invite-only, precisely so this is possible); reacting to a
human leaving a Remote User Room (unfollowing, and dropping it from their
personal Fediverse space); reacting to a human joining a room the bridge
manages for them (adding it to that same space -- see
``bridge.membership``/``bridge.spaces``); reacting to a Profile Room's
topic/name/avatar changing (keeping the local actor's AP bio/display
name/avatar in sync with it, and pushing a signed ``Update`` out to
followers so an already-cached copy on their server refreshes too -- see
``bridge.profile_posts.maybe_handle_topic_change``/
``maybe_handle_room_name_change``/``maybe_handle_room_avatar_change``);
federating a reaction (or its removal) on a previously-mirrored post;
federating a redaction of one of our own distributed posts as a signed
``Delete`` (see ``bridge.delete_bridge``); a bare "confirm" reply to one of
our own ``delete profile`` warnings (see
``bridge.commands.maybe_handle_delete_confirmation`` -- deliberately
checked before bot-tagged commands below, since it's recognized by reply
target/content rather than being tagged/prefixed at all); bot-tagged
command handling (see ``bridge.commands``); federating it as a chat
message (see ``bridge.chat_bridge``) if the room is a ghost chat room;
federating it as a reply to one; and finally, if it's a fresh post in a
linked Profile Room, converting and distributing it to followers as a new
ActivityPub ``Create``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from bridge.chat_bridge import maybe_federate_chat_message
from bridge.commands import (
    maybe_handle_command,
    maybe_handle_delete_confirmation,
    maybe_handle_leave_unfollowed_confirmation,
)
from bridge.delete_bridge import maybe_federate_delete
from bridge.edit_bridge import maybe_federate_edit
from bridge.membership import maybe_accept_invite, maybe_handle_join, maybe_handle_knock, maybe_handle_leave
from bridge.profile_posts import (
    maybe_distribute_profile_post,
    maybe_handle_room_avatar_change,
    maybe_handle_room_name_change,
    maybe_handle_topic_change,
)
from bridge.reaction_bridge import maybe_federate_reaction, maybe_federate_reaction_removal
from bridge.reply_bridge import maybe_federate_reply

logger = logging.getLogger(__name__)

router = APIRouter()

_EMPTY_JSON_RESPONSE_KWARGS = {"status_code": 200, "content": "{}", "media_type": "application/json"}


def _check_hs_token(request: Request, authorization: str | None, access_token: str | None) -> None:
    """Verify the request is from our homeserver (per the AS API auth scheme).

    Synapse may present hs_token either as a Bearer Authorization header
    (current spec) or an ``access_token`` query parameter (older homeservers).
    """
    expected = request.app.state.config.appservice.hs_token
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[len("bearer "):]
    elif access_token:
        presented = access_token

    if not presented or presented != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing hs_token")


async def _handle_transaction(
    request: Request, txn_id: str, authorization: str | None, access_token: str | None
) -> Response:
    _check_hs_token(request, authorization, access_token)
    repository = request.app.state.repository

    # Synapse retries a transaction it didn't get a 200 for; per spec we must
    # treat a repeat of the same txnId as already-handled rather than reprocessing it.
    if await repository.has_processed_transaction(txn_id):
        logger.debug("Ignoring already-processed transaction %s", txn_id)
        return Response(**_EMPTY_JSON_RESPONSE_KWARGS)

    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed transaction body: {exc}") from exc

    for event in body.get("events", []):
        try:
            handled_as_invite = await maybe_accept_invite(request, event)
            if handled_as_invite:
                continue
            handled_as_knock = await maybe_handle_knock(request, event)
            if handled_as_knock:
                continue
            handled_as_leave = await maybe_handle_leave(request, event)
            if handled_as_leave:
                continue
            handled_as_join = await maybe_handle_join(request, event)
            if handled_as_join:
                continue
            handled_as_topic_change = await maybe_handle_topic_change(request, event)
            if handled_as_topic_change:
                continue
            handled_as_name_change = await maybe_handle_room_name_change(request, event)
            if handled_as_name_change:
                continue
            handled_as_avatar_change = await maybe_handle_room_avatar_change(request, event)
            if handled_as_avatar_change:
                continue
            handled_as_reaction = await maybe_federate_reaction(request, event)
            if handled_as_reaction:
                continue
            handled_as_reaction_removal = await maybe_federate_reaction_removal(request, event)
            if handled_as_reaction_removal:
                continue
            handled_as_delete = await maybe_federate_delete(request, event)
            if handled_as_delete:
                continue
            handled_as_delete_confirmation = await maybe_handle_delete_confirmation(request, event)
            if handled_as_delete_confirmation:
                continue
            handled_as_leave_unfollowed_confirmation = await maybe_handle_leave_unfollowed_confirmation(
                request, event
            )
            if handled_as_leave_unfollowed_confirmation:
                continue
            handled_as_command = await maybe_handle_command(request, event)
            if not handled_as_command:
                # Edits MUST be intercepted before the chat/reply/post
                # paths: an m.replace event's own body is the "* ..."
                # fallback text, and any of those paths would federate it
                # as a fresh malformed post -- see bridge.edit_bridge.
                handled_as_edit = await maybe_federate_edit(request, event)
                if not handled_as_edit:
                    handled_as_chat = await maybe_federate_chat_message(request, event)
                    if not handled_as_chat:
                        handled_as_reply = await maybe_federate_reply(request, event)
                        if not handled_as_reply:
                            await maybe_distribute_profile_post(request, event)
        except Exception:
            logger.exception(
                "Error handling event %s (%s) in %s",
                event.get("event_id"),
                event.get("type"),
                event.get("room_id"),
            )

    await repository.mark_transaction_processed(txn_id)
    return Response(**_EMPTY_JSON_RESPONSE_KWARGS)


@router.put("/_matrix/app/v1/transactions/{txn_id}")
async def put_transaction_v1(
    request: Request,
    txn_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    return await _handle_transaction(request, txn_id, authorization, access_token)


@router.put("/transactions/{txn_id}")
async def put_transaction_legacy(
    request: Request,
    txn_id: str,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
) -> Response:
    """Unprefixed pre-v1 path, kept for compatibility with older homeserver configs."""
    return await _handle_transaction(request, txn_id, authorization, access_token)
