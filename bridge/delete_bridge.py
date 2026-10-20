"""Federates a Matrix redaction of a local user's own distributed post (or
reply) out to the fediverse as a signed ``Delete``, so followers' servers
actually remove it instead of being left with a post that silently
disappeared only on the Matrix side.

Triggered for every ``m.room.redaction`` event the AppService receives:
looks the redacted event up in ``ActorRepository``'s federated-event map (the
same one ``bridge.profile_posts``/``bridge.reply_bridge`` populate when
distributing a post). Only fires for posts attributed to one of our own
local actors -- a redaction of a *mirrored* post from a followed account
(tracked the same way, just with a remote ``author_actor_id``) is left
alone, since that's not our content to delete via AP; the remote account's
own ``Delete`` (already handled in ``bridge.inbox_dispatch``) is what
actually removes those. A reaction's redaction is a separate concern (see
``bridge.reaction_bridge``) and never appears in this map at all, so there's
no overlap between the two.

The one exception to "a remote-authored post is never ours to delete" is a
Matrix mod redacting someone ELSE's post/comment inside a mirrored Lemmy
community room (see ``bridge.lemmy_bridge``) -- confirmed against Lemmy's
own federation docs (join-lemmy.org) that a mod removing another user's
content there is signed as a ``Delete`` by the MOD's own actor (not the
original author), with an optional ``summary`` reason, not a ``Remove``.
Synapse enforces the room's own ``redact`` power level before this event
ever reaches the AppService, so the sender's authority is already verified
by the time ``_maybe_federate_community_mod_delete`` runs.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import Activity
from bridge.activitypub.remote_actor import resolve_actor_inbox
from bridge.activitypub.urls import actor_url, main_key_id, username_from_actor_url
from bridge.repository import FederatedEvent

logger = logging.getLogger(__name__)


async def maybe_federate_delete(request: Request, event: dict) -> bool:
    """Returns True if this event was a redaction of a post we distributed
    (handled, regardless of whether it was ours to delete via AP)."""
    if event.get("type") != "m.room.redaction":
        return False
    redacted_event_id = event.get("redacts") or (event.get("content") or {}).get("redacts")
    if not redacted_event_id:
        return False

    repository = request.app.state.repository
    federated = await repository.get_federated_event_by_matrix_event(redacted_event_id)
    if federated is None:
        return False  # not a post/reply we ever distributed at all

    config = request.app.state.config
    base = config.bridge.public_base_url
    username = username_from_actor_url(base, federated.author_actor_id)
    if username is None:
        await _maybe_federate_community_mod_delete(request, event=event, federated=federated)
        return True  # a mirrored REMOTE account's post -- not ours to delete via AP (unless handled just above)

    actor_record = await repository.get_local_actor(username)
    if actor_record is None:
        return True  # the actor has since been unlinked -- nothing to sign a Delete with

    delete_activity = Activity(
        id=f"{federated.author_actor_id}/deletes/{uuid.uuid4().hex}",
        type="Delete",
        actor=federated.author_actor_id,
        object=federated.ap_object_id,
    )
    http_client = request.app.state.http_client
    followers = await repository.list_followers(username)
    for follower_actor_id in followers:
        inbox = await resolve_actor_inbox(request, follower_actor_id)
        if inbox is None:
            continue
        try:
            await deliver_activity(
                http_client,
                inbox_url=inbox,
                activity=delete_activity.to_dict(),
                key_id=main_key_id(base, username),
                private_key_pem=actor_record.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver Delete(post) to follower %s", follower_actor_id, exc_info=True)
    return True


async def _maybe_federate_community_mod_delete(request: Request, *, event: dict, federated: FederatedEvent) -> None:
    """``federated`` names a remote-authored post/comment -- if it lives in
    a mirrored Lemmy community room, the redaction's SENDER (not
    ``federated.author_actor_id``) may be a local Matrix mod exercising
    real moderation power. See this module's own docstring."""
    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room_by_room_id(federated.room_id)
    if community_room is None:
        return

    sender = event.get("sender", "")
    if not sender:
        return
    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return  # no local AP identity to sign a Delete with

    config = request.app.state.config
    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    delete_activity = Activity(
        id=f"{own_actor_id}/deletes/{uuid.uuid4().hex}", type="Delete", actor=own_actor_id,
        object=federated.ap_object_id,
    )
    inbox = await resolve_actor_inbox(request, community_room.community_actor_id)
    if inbox is None:
        return
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=delete_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning(
            "Failed to deliver mod Delete(%s) to community %s", federated.ap_object_id,
            community_room.community_actor_id, exc_info=True,
        )
