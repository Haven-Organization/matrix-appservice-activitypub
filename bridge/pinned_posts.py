"""Bidirectional post-pinning: a local actor's own Matrix Profile Room
``m.room.pinned_events`` state mirrors (and is mirrored by) their AP actor's
``featured`` collection, via ``Add``/``Remove`` activities to followers --
the same mechanism Mastodon/Pleroma/Akkoma use for "pinned posts" on a
profile.

This module owns only the OUTBOUND half: a local user pinning/unpinning in
their own Profile Room, mirrored out as signed ``Add``/``Remove`` activities
delivered to their followers. The INBOUND half (a followed remote actor's
own ``Add``/``Remove`` updating their Remote User Room's own
``m.room.pinned_events``) lives in ``bridge.inbox_dispatch``
(``_handle_add``/``_handle_remove``), alongside every other inbound-activity
handler.

Neither direction needs a new database table: the "current" state on each
side already lives somewhere real -- Matrix's own ``m.room.pinned_events``,
and the existing ``federated_events`` table's Matrix-event<->AP-object
mapping -- so this reads/writes those instead of keeping its own copy.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from bridge.activitypub.models import Activity
from bridge.activitypub.urls import actor_url, featured_url, main_key_id
from bridge.note_mirroring import deliver_to_actor_or_followers
from bridge.repository import ActorRecord, ActorRepository

logger = logging.getLogger(__name__)


async def maybe_handle_pinned_events_change(request: Request, event: dict) -> bool:
    """Returns True if this event was an ``m.room.pinned_events`` change in a
    linked Profile Room (handled -- diffs the newly-pinned/unpinned Matrix
    event ids against the PREVIOUS state and sends one signed ``Add``/
    ``Remove`` per actually-changed item to the owner's followers, matching
    Mastodon/Pleroma's own incremental protocol rather than resending the
    whole list every time).

    The previous pinned list comes from ``unsigned.prev_content`` on the
    event itself -- Synapse includes the prior state content there for any
    AS-pushed state event (see ``bridge.membership``'s identical reliance on
    this for join-vs-profile-update detection); absent entirely only for the
    very first ``m.room.pinned_events`` this room has ever had, in which
    case treating it as "nothing was pinned before" is already correct.

    Only ever fires for a local actor's OWN Profile Room -- pinning
    something in a Remote User Room (a ghost's room) has no AP side of ours
    to update; that direction is driven by an inbound Add/Remove instead
    (see ``bridge.inbox_dispatch._handle_add``/``_handle_remove``)."""
    if event.get("type") != "m.room.pinned_events":
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False

    repository: ActorRepository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room -- e.g. a ghost's Remote User Room

    content = event.get("content") or {}
    new_pinned = content.get("pinned") or []
    prev_content = (event.get("unsigned") or {}).get("prev_content") or {}
    old_pinned = prev_content.get("pinned") or []

    added = [matrix_event_id for matrix_event_id in new_pinned if matrix_event_id not in old_pinned]
    removed = [matrix_event_id for matrix_event_id in old_pinned if matrix_event_id not in new_pinned]
    if not added and not removed:
        return True  # reordering only, or a redundant resend -- nothing AP needs to know

    base = request.app.state.config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    key_id = main_key_id(base, actor_record.username)
    target = featured_url(base, actor_record.username)

    for matrix_event_id in added:
        await _send_pin_activity(
            request, actor_record=actor_record, matrix_event_id=matrix_event_id,
            activity_type="Add", own_actor_id=own_actor_id, key_id=key_id, target=target,
        )
    for matrix_event_id in removed:
        await _send_pin_activity(
            request, actor_record=actor_record, matrix_event_id=matrix_event_id,
            activity_type="Remove", own_actor_id=own_actor_id, key_id=key_id, target=target,
        )
    return True


async def _send_pin_activity(
    request: Request, *, actor_record: ActorRecord, matrix_event_id: str, activity_type: str,
    own_actor_id: str, key_id: str, target: str,
) -> None:
    repository: ActorRepository = request.app.state.repository
    federated = await repository.get_federated_event_by_matrix_event(matrix_event_id)
    if federated is None:
        # Pinned/unpinned a Matrix event that was never actually distributed
        # as a post (e.g. a bot notice, or a reaction/membership event --
        # Element lets you pin any timeline event, not just m.room.message)
        # -- nothing to tell followers.
        logger.info(
            "Pinned/unpinned event %s has no federated post; not sending %s", matrix_event_id, activity_type
        )
        return

    activity = Activity(
        id=f"{own_actor_id}/{activity_type.lower()}s/{uuid.uuid4().hex}",
        type=activity_type,
        actor=own_actor_id,
        object=federated.ap_object_id,
        target=target,
    )
    await deliver_to_actor_or_followers(
        request, target_actor_id=own_actor_id, activity=activity.to_dict(),
        key_id=key_id, private_key_pem=actor_record.private_key_pem,
    )
