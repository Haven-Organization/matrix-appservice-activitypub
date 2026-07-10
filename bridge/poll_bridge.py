"""Bidirectional bridging of Matrix polls (MSC3381) <-> ActivityPub
``Question`` objects (Mastodon/Pleroma-style polls).

Outbound: a Profile Room's own ``org.matrix.msc3381.poll.start`` becomes a
signed ``Create{Question}`` (``maybe_distribute_profile_poll``), a vote
(``org.matrix.msc3381.poll.response``) on a MIRRORED (externally-owned) poll
becomes a private ``Create{Answer}`` addressed only to the poll's real
author (``maybe_federate_poll_vote`` -- see ``bridge.activitypub.models.Note.name``
for the exact wire shape and why it's sent as an ``Answer``, not a bare
``Note``), and closing one's own poll (``org.matrix.msc3381.poll.end``)
becomes ``Update{Question, closed: ...}`` (``maybe_federate_poll_close``).

Inbound mirroring/vote-casting lives in ``bridge.note_mirroring``
(``import_question``) and ``bridge.inbox_dispatch``
(``_maybe_handle_poll_vote``/``_handle_question_update``) instead, next to
the Note-mirroring machinery they're built on top of.

Deliberately asymmetric, matching a structural fact about how Mastodon-style
polls actually work, not a limitation of this bridge: a poll vote is sent
PRIVATELY, addressed only to the poll's own author -- never broadcast to
followers or other observers. So per-voter fidelity (a remote voter's own
ghost casting a visible Matrix vote) is only possible for a poll THIS bridge
authored (see ``bridge.inbox_dispatch._maybe_handle_poll_vote`` -- we
receive each vote directly, being the addressee). For a poll mirrored from
someone else, other remote voters' tallies are only ever visible to us as an
anonymized aggregate (periodic ``Update{Question}`` tally refreshes) with no
individual attribution at all, structurally -- Matrix's own poll widget only
tallies real per-user response events, so there is no way to inject an
arbitrary count, and this bridge doesn't try to (a mirrored poll's Matrix-side
tally only ever reflects Matrix-side voters, full stop -- see
``bridge.inbox_dispatch._handle_question_update``, which no-ops on a plain
tally-refresh ``Update`` for exactly this reason). Showing the fediverse's own
aggregate as separate informational text is a natural future extension, not
implemented here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Request

from bridge.activitypub.models import AS_PUBLIC, Activity, Note, Question
from bridge.activitypub.sanitize import plain_text_to_note_html
from bridge.activitypub.urls import actor_url, followers_url, main_key_id, username_from_actor_url
from bridge.commands import _effective_third_party_mode, is_third_party_still_allowed
from bridge.note_mirroring import deliver_to_actor_or_followers, refresh_poll_tallies
from bridge.repository import FederatedEvent, PollVoteRecord
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

_POLL_START_EVENT_TYPE = "org.matrix.msc3381.poll.start"
_POLL_RESPONSE_EVENT_TYPE = "org.matrix.msc3381.poll.response"
_POLL_END_EVENT_TYPE = "org.matrix.msc3381.poll.end"


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


async def maybe_distribute_profile_poll(request: Request, event: dict) -> bool:
    """Returns True if this event was a poll-start in a linked Profile
    Room (handled, successfully distributed or not) -- callers shouldn't
    process it further. Mirrors ``maybe_distribute_profile_post``'s own
    gates almost exactly -- see that function for the fuller reasoning on
    each one; only the AP-object-building step (a ``Question``, not a
    ``Note``) actually differs.

    Unlike ordinary posts, a GUEST poll (a different local user posting a
    poll into someone else's Profile Room) is not supported -- kept out of
    scope deliberately: a poll's options don't carry a mention/participant
    line the way ordinary post text does, so the guest-post owner-mention
    convention has nothing natural to attach to here."""
    if event.get("type") != _POLL_START_EVENT_TYPE:
        return False

    content = event.get("content") or {}
    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    config = request.app.state.config
    actor_record = await repository.get_local_actor_by_room_id(room_id)
    if actor_record is None:
        return False  # not a linked Profile Room
    if actor_record.matrix_user_id != sender:
        return False  # guest polls unsupported -- see this function's own docstring

    if await _effective_third_party_mode(request, actor_record) == "follow_only":
        return True

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed (e.g. a redelivered transaction)

    poll_start = content.get(_POLL_START_EVENT_TYPE) or {}
    question_text = (poll_start.get("question") or {}).get("org.matrix.msc1767.text") or ""
    raw_answers = poll_start.get("answers") or []
    max_selections = poll_start.get("max_selections") or 1
    # Positional mapping, not stored anywhere: oneOf[i]/anyOf[i] <-> this
    # Matrix event's own answers[i] is stable in both directions since we
    # mint the AP side directly from this same array, in the same order.
    options = [
        {"type": "Note", "name": (a.get("org.matrix.msc1767.text") or "").strip()}
        for a in raw_answers
        if isinstance(a, dict) and (a.get("org.matrix.msc1767.text") or "").strip()
    ]
    if not question_text or not options:
        return True  # nothing usable to federate -- stays Matrix-only

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    question_id = f"{own_actor_id}/polls/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc)
    end_time = published + timedelta(days=config.bridge.poll_default_duration_days)

    question = Question(
        id=question_id,
        attributed_to=own_actor_id,
        content=plain_text_to_note_html(question_text, {}),
        published=published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        to=[AS_PUBLIC],
        cc=[followers_url(base, actor_record.username)],
        one_of=options if max_selections <= 1 else [],
        any_of=options if max_selections > 1 else [],
        end_time=end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    create_activity = Activity(
        id=f"{question_id}/activity",
        type="Create",
        actor=own_actor_id,
        object=question,
        published=question.published,
        to=question.to,
        cc=question.cc,
    )

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id, room_id=room_id, ap_object_id=question_id, author_actor_id=own_actor_id,
        )
    )

    for recipient_actor_id in await repository.list_followers(actor_record.username):
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=recipient_actor_id,
            activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    return True


async def maybe_federate_poll_vote(request: Request, event: dict) -> bool:
    """Returns True if this event was a poll response (handled, whether or
    not anything actually federated) -- callers shouldn't process it
    further.

    A vote on a poll a LOCAL actor authored (even by a different local
    user, in that actor's own Profile Room) needs no federation at all --
    both parties already share the Matrix room, and Matrix's own poll
    widget already tallies it; this is the crux of the whole feature's
    scope, see this module's docstring."""
    if event.get("type") != _POLL_RESPONSE_EVENT_TYPE:
        return False

    content = event.get("content") or {}
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") != "m.reference":
        return False
    target_event_id = relates_to.get("event_id")
    answers = (content.get(_POLL_RESPONSE_EVENT_TYPE) or {}).get("answers") or []
    if not target_event_id or not answers:
        return False

    config = request.app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    bot_mxid = _bot_mxid(config)
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return False  # never re-federate our own ghosts'/bot's own events

    repository = request.app.state.repository
    target = await repository.get_federated_event_by_matrix_event(target_event_id)
    if target is None:
        return False  # voting on a purely-local Matrix poll; nothing to federate

    base = config.bridge.public_base_url
    if username_from_actor_url(base, target.author_actor_id) is not None:
        return True  # a LOCAL actor's own poll -- Matrix already natively tallies this

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        try:
            await request.app.state.synapse.send_message_event(
                room_id,
                {
                    "msgtype": "m.notice",
                    "body": f"This vote wasn't sent to the fediverse: link a profile first by "
                    f'tagging me with "{bot_mxid} link profile".',
                },
                as_user_id=bot_mxid,
            )
        except Exception:
            logger.warning("Failed to send link-profile notice to %s", room_id, exc_info=True)
        return True

    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return True

    if await repository.get_poll_vote_by_matrix_user(target.ap_object_id, sender) is not None:
        # Mastodon-family software doesn't support changing a vote once
        # cast -- the first vote already went out; silently re-sending a
        # second one would just be ignored or error on their end, so tell
        # the user plainly instead of pretending it worked.
        try:
            await request.app.state.synapse.send_message_event(
                room_id,
                {
                    "msgtype": "m.notice",
                    "body": "This poll doesn't support changing your vote on the fediverse side -- "
                    "your first vote already went out and can't be revised.",
                },
                as_user_id=bot_mxid,
            )
        except Exception:
            logger.warning("Failed to send no-revote notice to %s", room_id, exc_info=True)
        return True

    synapse = request.app.state.synapse
    try:
        poll_event = await synapse.get_event(target.room_id, target.event_id, as_user_id=bot_mxid)
    except SynapseError:
        return True
    poll_start = (poll_event.get("content") or {}).get(_POLL_START_EVENT_TYPE) or {}
    option_text = next(
        (a.get("org.matrix.msc1767.text") for a in poll_start.get("answers", []) if a.get("id") == answers[0]), None
    )
    if not option_text:
        return True

    own_actor_id = actor_url(base, actor_record.username)
    vote_id = f"{own_actor_id}/votes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    vote_note = Note(
        id=vote_id, attributed_to=own_actor_id, content="", published=published,
        to=[target.author_actor_id], cc=[], in_reply_to=target.ap_object_id, name=option_text,
        # Pleroma/Akkoma ONLY count a vote if the object's own type is
        # literally "Answer" (confirmed in their side_effects.ex --
        # handle_object_creation pattern-matches on "Answer" specifically
        # to call Object.increase_vote_count; a bare "Note" falls into the
        # generic branch and is silently never counted as a vote at all).
        # Mastodon doesn't gate on type here (poll_vote? just checks for
        # inReplyTo + a name matching one of the poll's options), so
        # sending "Answer" is safe/correct for both, not just Pleroma.
        type="Answer",
    )
    create_activity = Activity(
        id=f"{vote_id}/activity", type="Create", actor=own_actor_id, object=vote_note,
        published=published, to=vote_note.to, cc=[],
    )
    await deliver_to_actor_or_followers(
        request,
        target_actor_id=target.author_actor_id,
        activity=create_activity.to_dict(),
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )
    await repository.record_poll_vote(
        PollVoteRecord(
            vote_activity_id=vote_id, question_ap_object_id=target.ap_object_id,
            room_id=room_id, matrix_user_id=sender,
        )
    )
    # Some remote implementations (confirmed for Pleroma/Akkoma) never push
    # a live Update at all, live or at close -- so rather than only ever
    # waiting for one, actively pull the poll's current state right after
    # voting, same mechanism as ";poll refresh" (see
    # bridge.note_mirroring.refresh_poll_tallies's own docstring).
    # Best-effort: the vote itself already succeeded above regardless.
    await refresh_poll_tallies(request, target=target)
    return True


async def maybe_federate_poll_close(request: Request, event: dict) -> bool:
    """Returns True if this event was a poll end (handled, whether or not
    anything actually federated) -- callers shouldn't process it further.

    Mastodon expects a FULL ``Question`` object on the ``Update``, not a
    diff -- same reasoning as ``bridge.edit_bridge``'s Note ``Update``
    re-sending the whole Note -- so this re-fetches the live Matrix
    poll-start event's own content and rebuilds from it, rather than
    storing anything (same "read live state back out of Synapse" pattern
    ``edit_bridge.py`` already uses for ``in_reply_to``/``published``)."""
    if event.get("type") != _POLL_END_EVENT_TYPE:
        return False

    content = event.get("content") or {}
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") != "m.reference":
        return False
    target_event_id = relates_to.get("event_id")
    if not target_event_id:
        return False

    config = request.app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    bot_mxid = _bot_mxid(config)
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return False

    repository = request.app.state.repository
    target = await repository.get_federated_event_by_matrix_event(target_event_id)
    if target is None:
        return False  # ending a purely-local Matrix poll; nothing to federate

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return True  # no linked profile -- nothing to sign an Update with

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    if target.author_actor_id != own_actor_id:
        return True  # not this poll's own author -- silently drop, same as edit_bridge's ownership gate

    synapse = request.app.state.synapse
    try:
        poll_event = await synapse.get_event(target.room_id, target.event_id, as_user_id=bot_mxid)
    except SynapseError:
        return True
    poll_start = (poll_event.get("content") or {}).get(_POLL_START_EVENT_TYPE) or {}
    question_text = (poll_start.get("question") or {}).get("org.matrix.msc1767.text") or ""
    max_selections = poll_start.get("max_selections") or 1
    options = [
        {"type": "Note", "name": a.get("org.matrix.msc1767.text") or ""}
        for a in poll_start.get("answers", [])
        if a.get("org.matrix.msc1767.text")
    ]

    closed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    question = Question(
        id=target.ap_object_id,  # SAME id -- this is an update, not a new poll
        attributed_to=own_actor_id,
        content=plain_text_to_note_html(question_text, {}),
        published=closed,
        to=[AS_PUBLIC],
        cc=[followers_url(base, actor_record.username)],
        one_of=options if max_selections <= 1 else [],
        any_of=options if max_selections > 1 else [],
        closed=closed,
    )
    update_activity = Activity(
        id=f"{target.ap_object_id}#updates/{uuid.uuid4().hex}",
        type="Update", actor=own_actor_id, object=question, published=closed, to=question.to, cc=question.cc,
    )
    for recipient_actor_id in await repository.list_followers(actor_record.username):
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=recipient_actor_id,
            activity=update_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    return True
