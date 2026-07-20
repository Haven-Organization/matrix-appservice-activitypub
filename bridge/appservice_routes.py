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
federating a vote cast on a mirrored (externally-owned) poll, or that poll
being closed (see ``bridge.poll_bridge``); federating a redaction of one of
our own distributed posts as a signed
``Delete`` (see ``bridge.delete_bridge``); a bare "confirm" reply to one of
our own ``delete profile``/``leave unfollowed``/``allow homeserver``/
encrypted-attachment warnings (see ``bridge.commands.maybe_handle_delete_confirmation``
and friends, and this module's own ``maybe_handle_encrypted_attachment_confirmation``,
deliberately checked before bot-tagged commands below, since they're
recognized by reply target/content rather than being tagged/prefixed at
all); bot-tagged command handling (see ``bridge.commands``); then, via
``_dispatch_outbound_send``: federating it as a chat message (see
``bridge.chat_bridge``) if the room is a ghost chat room; federating it as
a reply to one; distributing a fresh post (or a new poll) in a linked
Profile Room to followers as a new ActivityPub ``Create``; and, if none of
the above claimed it, distributing it as a Shoot guild-Channel message
(see ``bridge.channel_bridge.maybe_distribute_channel_message``) if the
room is one. An outbound send whose attachment turns out to be encrypted
(``bridge.media.resolve_attachment_or_request_confirmation``) sends a
confirmation request instead of federating anything, until answered by
the "confirm" reply above.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from bridge.activitypub.sanitize import strip_reply_fallback
from bridge.channel_bridge import maybe_distribute_channel_message
from bridge.chat_bridge import maybe_federate_chat_message
from bridge.commands import (
    _reply_target_event_id,
    maybe_handle_allow_homeserver_confirmation,
    maybe_handle_command,
    maybe_handle_delete_confirmation,
    maybe_handle_leave_unfollowed_confirmation,
)
from bridge.delete_bridge import maybe_federate_delete
from bridge.edit_bridge import maybe_federate_edit
from bridge.media import ENCRYPTED_ATTACHMENT_WARNING_MARKER, decrypt_and_reupload_encrypted_attachment
from bridge.membership import maybe_accept_invite, maybe_handle_join, maybe_handle_knock, maybe_handle_leave
from bridge.poll_bridge import maybe_distribute_profile_poll, maybe_federate_poll_close, maybe_federate_poll_vote
from bridge.profile_posts import (
    maybe_distribute_profile_post,
    maybe_handle_room_avatar_change,
    maybe_handle_room_name_change,
    maybe_handle_topic_change,
)
from bridge.reaction_bridge import maybe_federate_reaction, maybe_federate_reaction_removal
from bridge.reply_bridge import maybe_federate_reply
from bridge.synapse_client import SynapseError

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


async def _dispatch_outbound_send(request: Request, event: dict) -> None:
    """The inner half of the outbound chain: edit, chat message,
    reply-or-DM, poll start, channel message, fresh profile post, in that
    order. Extracted out of ``_handle_transaction``'s own per-event loop
    so ``maybe_handle_encrypted_attachment_confirmation`` below can replay
    it against a historical event once its sender confirms, without
    duplicating this exact chain a second time. Each handler independently
    decides whether ``event`` is even relevant to it (room type, sender,
    ...), so replaying the whole chain against an arbitrary historical
    event is always safe: at most one of them will actually do anything."""
    handled_as_edit = await maybe_federate_edit(request, event)
    if not handled_as_edit:
        handled_as_chat = await maybe_federate_chat_message(request, event)
        if not handled_as_chat:
            handled_as_reply = await maybe_federate_reply(request, event)
            if not handled_as_reply:
                handled_as_poll_start = await maybe_distribute_profile_poll(request, event)
                if not handled_as_poll_start:
                    handled_as_channel_message = await maybe_distribute_channel_message(request, event)
                    if not handled_as_channel_message:
                        await maybe_distribute_profile_post(request, event)


async def maybe_handle_encrypted_attachment_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this event was a "confirm" reply to one of our own
    encrypted-attachment warnings (see
    ``bridge.media.resolve_attachment_or_request_confirmation``), handled
    whether or not it actually matched one, same stateless marker-based
    pattern as ``bridge.commands.maybe_handle_delete_confirmation`` (the
    original triggering event is recovered from the warning's OWN thread
    relation, since it was sent as a real thread reply to it, rather than
    any separately persisted "pending confirmation" state). Must run
    before ``maybe_handle_command``/``_dispatch_outbound_send`` in the
    dispatch chain, same reasoning as the other ``*_confirmation`` handlers.

    Restricted to the ORIGINAL event's own sender confirming, not whoever
    happens to reply "confirm" in the room: the action this triggers
    (decrypting and re-publishing someone's forwarded file to the
    fediverse) is sensitive enough that it shouldn't be triggerable by a
    bystander.

    Posts a plain "Sent." notice in the same thread once
    ``_dispatch_outbound_send`` returns, so the sender has clear,
    immediate confirmation it actually went out, matching the "Couldn't
    decrypt/upload" notice on the failure branch above it."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    if strip_reply_fallback(content.get("body") or "").strip().lower() != "confirm":
        return False
    target_event_id = _reply_target_event_id(content)
    if not target_event_id:
        return False

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not room_id or not sender:
        return False

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    synapse = request.app.state.synapse
    try:
        warning_event = await synapse.get_event(room_id, target_event_id, as_user_id=bot_mxid)
    except SynapseError:
        return False
    if warning_event.get("sender") != bot_mxid:
        return False
    warning_content = warning_event.get("content") or {}
    if ENCRYPTED_ATTACHMENT_WARNING_MARKER not in (warning_content.get("body") or ""):
        return False

    original_event_id = _reply_target_event_id(warning_content)
    if not original_event_id:
        return True  # our own warning, but somehow missing its own thread link, nothing to resume
    try:
        original_event = await synapse.get_event(room_id, original_event_id, as_user_id=bot_mxid)
    except SynapseError:
        return True

    if original_event.get("sender") != sender:
        return True  # someone else's file, claimed (never falls through to ordinary handling), but ignored

    original_content = original_event.get("content") or {}
    new_content = original_content.get("m.new_content")
    effective_content = new_content if isinstance(new_content, dict) else original_content
    resolved_mxc = await decrypt_and_reupload_encrypted_attachment(
        request.app.state.http_client, synapse, request.app.state.repository, effective_content,
    )
    if resolved_mxc is None:
        try:
            await synapse.send_message_event(
                room_id,
                {
                    "msgtype": "m.notice",
                    "body": "Couldn't decrypt/upload that file, nothing was sent to the fediverse.",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": original_event_id,
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": event.get("event_id") or original_event_id},
                    },
                },
                as_user_id=bot_mxid,
            )
        except SynapseError:
            logger.warning("Failed to send decrypt-failure notice to %s", room_id, exc_info=True)
        return True

    effective_content["url"] = resolved_mxc
    await _dispatch_outbound_send(request, original_event)
    try:
        await synapse.send_message_event(
            room_id,
            {
                "msgtype": "m.notice",
                "body": "✅ Sent.",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": original_event_id,
                    "is_falling_back": True,
                    "m.in_reply_to": {"event_id": event.get("event_id") or original_event_id},
                },
            },
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.warning("Failed to send sent-confirmation notice to %s", room_id, exc_info=True)
    return True


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
            handled_as_poll_vote = await maybe_federate_poll_vote(request, event)
            if handled_as_poll_vote:
                continue
            handled_as_poll_close = await maybe_federate_poll_close(request, event)
            if handled_as_poll_close:
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
            handled_as_allow_homeserver_confirmation = await maybe_handle_allow_homeserver_confirmation(
                request, event
            )
            if handled_as_allow_homeserver_confirmation:
                continue
            handled_as_encrypted_attachment_confirmation = await maybe_handle_encrypted_attachment_confirmation(
                request, event
            )
            if handled_as_encrypted_attachment_confirmation:
                continue
            handled_as_command = await maybe_handle_command(request, event)
            if not handled_as_command:
                # Edits MUST be intercepted before the chat/reply/post
                # paths: an m.replace event's own body is the "* ..."
                # fallback text, and any of those paths would federate it
                # as a fresh malformed post -- see bridge.edit_bridge.
                await _dispatch_outbound_send(request, event)
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
