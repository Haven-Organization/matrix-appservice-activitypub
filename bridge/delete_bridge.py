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
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import Activity
from bridge.activitypub.remote_actor import resolve_actor_inbox
from bridge.activitypub.urls import main_key_id, username_from_actor_url

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
        return True  # a mirrored REMOTE account's post -- not ours to delete via AP

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
