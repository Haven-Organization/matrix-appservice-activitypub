"""Federates Matrix reactions (and their removal) out to the fediverse post
they're annotating.

Triggered for every ``m.reaction`` event the AppService receives: if it
annotates a Matrix event that mirrors, or is itself chained from, a
fediverse post (tracked via ``ActorRepository.record_federated_event``),
this builds a ``Like`` (a bare "favorite", maximally Mastodon-compatible --
sent ONLY for the plain thumbs up emoji, no skin tone, since that's the one
Matrix reaction that most directly maps to what a favorite/like button
conventionally means) or ``EmojiReact`` (Pleroma/Misskey/Akkoma extension,
carries the actual emoji in ``content``, for every other emoji -- including
a skin-toned thumbs up -- so those clients still show the specific emoji
reacted with instead of it being silently collapsed into a generic
favorite) activity addressed to that post's author, signed with the
reacting Matrix user's own linked Profile Room actor. Reacting with the
clockwise-arrows emoji (see ``_BOOST_EMOJIS``) specifically is
this bridge's boost shorthand instead -- sent as a real ``Announce`` (see
``send_boost``, also shared with the ``;boost`` command in
``bridge.commands``) rather than a Like/EmojiReact, fanned out to the
booster's own followers AND delivered directly to the original author.

Also handles the reverse: a Matrix redaction of a reaction the bridge itself
sent out earlier is federated as an ``Undo``, so un-reacting (or un-boosting)
works too. Both directions record a ``ReactionRecord`` (see
``bridge.repository``) so the two can find each other -- this applies just
as well to a boost recorded via the ``;boost`` command, keyed to that
command message's own event id instead of a reaction event's, so redacting
either one undoes it.

The reacted-to post's author might be a DIFFERENT local bridge user rather
than a genuinely remote account (reacting inside their Profile Room, which
you can be a member of without owning it) -- delivered via
``bridge.note_mirroring.deliver_to_actor_or_followers``, which reaches
THEIR real followers instead of a self-HTTP round trip to their own inbox
(which would just loop back into our own inbound handler and be dropped
there; see that function's docstring). Delivery is accordingly always
best-effort now rather than gating on a single success/failure, so both a
reaction and its later removal always record their own bookkeeping
regardless of how delivery went.
"""

from __future__ import annotations

import html
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import Request

from bridge.activitypub.models import AS_PUBLIC, Activity
from bridge.activitypub.urls import actor_url, followers_url, main_key_id
from bridge.commands import is_third_party_still_allowed
from bridge.inbox_dispatch import _fetch_post_preview, build_preview_media_content
from bridge.matrix_links import matrix_to_link
from bridge.note_mirroring import actor_html_with_avatar, deliver_to_actor_or_followers
from bridge.repository import ActorRecord, FederatedEvent, ReactionRecord
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# The ONLY emoji sent as a plain `Like` (for maximum compatibility with
# software, e.g. Mastodon, that only understands a bare favorite with no
# choice of emoji) rather than an `EmojiReact` carrying the actual emoji --
# deliberately narrow, not a broad set of "positive" emoji this used to
# treat as equivalent: everything else, including a skin-toned thumbs up,
# is a distinct reaction and should be sent as one instead of being
# silently collapsed into a generic favorite.
_THUMBS_UP = "\U0001F44D"
_VARIATION_SELECTOR_16 = "️"

# The clockwise-arrows symbol boosts -- a common "retweet/boost" convention
# across other Matrix<->fediverse bridges and clients' own quick-react
# suggestions, and also the emoji this bridge's own repost/boost renderings
# are prefixed with (see bridge.commands._handle_repost,
# bridge.reaction_bridge.send_boost), so reacting with the same symbol the
# bridge itself already uses to mean "boosted" reads as the more
# discoverable/obvious choice. The recycling symbol (♻) used to also work
# here but was dropped -- keeping just one canonical boost emoji avoids any
# ambiguity about which reaction "really" means boost.
_BOOST_EMOJIS = ("\U0001F501",)


def _is_favorite_emoji(key: str) -> bool:
    """Whether ``key`` is the plain thumbs up -- optionally followed by the
    emoji-presentation variation selector (harmless, some clients append it
    even where it isn't needed), but NOT a skin-toned one, which is its own
    distinct emoji and should go out as an EmojiReact instead."""
    return key.replace(_VARIATION_SELECTOR_16, "") == _THUMBS_UP


def _is_boost_emoji(key: str) -> bool:
    """Whether ``key`` is one of ``_BOOST_EMOJIS`` (optionally followed by
    the variation selector -- see ``_is_favorite_emoji``), this bridge's
    reaction shorthand for boosting a post instead of merely reacting to
    it."""
    return key.replace(_VARIATION_SELECTOR_16, "") in _BOOST_EMOJIS


# Per MSC4501, a caption-less repost is TWO Matrix events for one logical
# action: the 🔁 reaction (which alone already triggers send_boost below)
# and a separate org.matrix.msc4501.social.relates_to record an MSC4501
# client posts into the booster's OWN Profile Room (see
# bridge.profile_posts._maybe_suppress_msc4501_repost_record). When that
# record lands in the SAME room send_boost would post its own "you
# boosted" card into, the two are redundant -- Haven's own native record
# already shows the boosted content there. Arrival order between the two
# isn't guaranteed, so this coordinates both directions with a short-lived
# in-process dict (not persisted -- losing it across a restart just means
# one redundant card slips through right after, harmless): keyed by
# (booster's own matrix user id, the BOOSTED post's own matrix event id --
# available as ``parent.event_id`` in send_boost, and directly as
# ``relates_to["event_id"]`` on the native record, so no extra lookup is
# needed on either side to agree on the same key).
_BOOST_CARD_DEDUP_WINDOW_SECONDS = 15.0
_native_repost_record_seen_at: dict[tuple[str, str], float] = {}
_pending_boost_card: dict[tuple[str, str], tuple[str, str, float]] = {}  # -> (room_id, card_event_id, posted_at)


async def note_msc4501_native_repost(request: Request, *, booster_matrix_user_id: str, boosted_event_id: str) -> None:
    """Called by ``bridge.profile_posts`` when an MSC4501-native repost
    record lands in the booster's own Profile Room. Records that this
    exact boost now has a native record covering it (for the case
    send_boost's own reaction-triggered call hasn't run yet), and
    retroactively redacts send_boost's own "you boosted" card if it
    already got posted moments earlier (the reaction having arrived
    first) -- either way, only one of the two ever ends up visible."""
    key = (booster_matrix_user_id, boosted_event_id)
    now = time.monotonic()
    _native_repost_record_seen_at[key] = now
    pending = _pending_boost_card.pop(key, None)
    if pending is None:
        return
    room_id, card_event_id, posted_at = pending
    if now - posted_at > _BOOST_CARD_DEDUP_WINDOW_SECONDS:
        return  # too long ago to safely assume it's the SAME boost action
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        await request.app.state.synapse.redact_event(
            room_id, card_event_id, reason="Redundant with MSC4501 native repost record", as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.warning("Failed to redact redundant boost card %s", card_event_id, exc_info=True)


async def send_boost(
    request: Request,
    *,
    actor_record: ActorRecord,
    parent: FederatedEvent,
    matrix_event_id: str | None,
    room_id: str,
    reactor_matrix_user_id: str,
) -> str:
    """Builds, delivers, and records an ``Announce`` of ``parent`` (a
    "boost"/"repost" in Mastodon's own terms) signed as ``actor_record``.

    Shared by both ways of triggering one: reacting to a federated post with
    the clockwise-arrows emoji (see ``maybe_federate_reaction``)
    and the ``;boost`` command (see ``bridge.commands._handle_boost``) --
    they only differ in what ``matrix_event_id`` ends up being (the
    reaction event itself, vs. the command message that triggered it).

    Also posts a "X boosted Y's post" rendering (bold names, pills -- see
    ``actor_html_with_avatar`` and ``bridge.commands._handle_repost``'s
    identical reasoning) into ``actor_record``'s OWN Profile Room,
    regardless of which room the boost was actually triggered from --
    reacting with the emoji happens wherever the post itself lives (often
    someone else's Remote User Room), which isn't the booster's own
    timeline and has none of their own followers watching it.

    Both of those -- ``matrix_event_id`` AND this Profile Room rendering's
    own event id -- are recorded together as a SINGLE ``ReactionRecord``
    (``event_id``/``secondary_event_id`` respectively, see that field's own
    docstring), so redacting EITHER one works to undo the boost: this is
    deliberate, not a shortcut, since a Profile Room follower has no way to
    redact a reaction/command message that happened in a different room
    entirely, and the person who boosted may not remember/still have
    access to it either. ``maybe_federate_reaction_removal`` un-does *any*
    tracked reaction purely by matrix event id, without needing to know it
    was actually an Announce.

    Returns the ``Announce`` activity's own id.
    """
    repository = request.app.state.repository
    config = request.app.state.config
    base = config.bridge.public_base_url

    # Same reasoning as maybe_federate_reaction: a mirrored repost's own
    # ap_object_id/author_actor_id name the Announce activity and its
    # booster, not the actual post being boosted -- boosting THAT message
    # again should still reach (and re-boost) the original post/author, not
    # the intermediate repost.
    target_object_id = parent.boosted_object_id or parent.ap_object_id
    target_author_actor_id = parent.boosted_author_actor_id or parent.author_actor_id

    own_actor_id = actor_url(base, actor_record.username)
    activity_id = f"{own_actor_id}/announces/{uuid.uuid4().hex}"
    announce_activity = Activity(
        id=activity_id,
        type="Announce",
        actor=own_actor_id,
        object=target_object_id,
        published=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        to=[followers_url(base, actor_record.username)],
        cc=[AS_PUBLIC, target_author_actor_id],
    )
    activity_dict = announce_activity.to_dict()

    # Fans out to the booster's OWN followers -- deliver_to_actor_or_followers
    # resolves a local actor id to exactly that (see its docstring) -- since
    # a boost is a public act that should show up for them, same as an
    # ordinary top-level post.
    await deliver_to_actor_or_followers(
        request,
        target_actor_id=own_actor_id,
        activity=activity_dict,
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )
    # Separately reaches the original author directly, so they're notified
    # of the boost even if they don't already follow the booster back (the
    # common case) -- deliver_to_actor_or_followers again handles the
    # original author turning out to be another local bridge user instead
    # of a genuinely remote one.
    if target_author_actor_id and target_author_actor_id != own_actor_id:
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=target_author_actor_id,
            activity=activity_dict,
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )

    # A visible record of the boost in the booster's OWN Profile Room --
    # not just the (otherwise silent, for the emoji-reaction trigger) AP
    # side -- so anyone following that room (see the room's own purpose:
    # it doubles as this user's public timeline) sees it happened, same
    # reasoning as bridge.commands._handle_repost's own profile notice.
    # Sent BEFORE record_reaction below so its own event id can be recorded
    # as ReactionRecord.secondary_event_id -- redacting THIS message undoes
    # the boost exactly like redacting the original reaction/;boost command
    # message would.
    notice_event_id: str | None = None
    boost_dedup_key = (reactor_matrix_user_id, parent.event_id)
    already_has_native_record = (
        time.monotonic() - _native_repost_record_seen_at.get(boost_dedup_key, 0.0) < _BOOST_CARD_DEDUP_WINDOW_SECONDS
    )
    if actor_record.room_id and not already_has_native_record:
        preview_target = await repository.get_federated_event_by_ap_object(target_object_id) or parent
        preview_text, _preview_full_content, preview_image, preview_video = await _fetch_post_preview(
            request, preview_target
        )
        post_link = matrix_to_link(preview_target.room_id, preview_target.event_id)

        # preview_text alongside preview media means the post had BOTH
        # media and a real caption (_fetch_post_preview only returns
        # caption-worthy text for media posts) -- quote the caption AND
        # attach the media preview, so neither half of the post vanishes
        # from the card (confirmed live 2026-07-03: a captioned image's
        # boost card showed neither the caption nor a working embed).
        quote_block_html = f"<blockquote>{html.escape(preview_text)}</blockquote>" if preview_text else ""

        _, booster_html = await actor_html_with_avatar(request, own_actor_id)
        original_handle, original_author_html = await actor_html_with_avatar(request, target_author_actor_id)

        plain_body = f"\U0001F501 boosted {original_handle}'s post:"
        if preview_text:
            plain_body += f"\n> {preview_text}"
        plain_body += f"\n{post_link}"

        post_pill_html = f'<a href="{html.escape(post_link, quote=True)}">post</a>'
        formatted_caption = (
            f"<p>\U0001F501 {booster_html} boosted {original_author_html}'s {post_pill_html}</p>{quote_block_html}"
        )
        # Deliberately NEVER sets org.matrix.msc4501.social.relates_to on
        # this notice (unlike _build_repost_message/_echo_reply_in_own_room,
        # which DO) -- relates_to's whole point is telling an MSC4501-aware
        # client "here's the boosted post's own real content, go render
        # that instead of duplicating it inline," but this notice already
        # embeds a full plain_body/formatted_caption/media preview of its
        # own (this IS the record of the boost, not raw mirrored content
        # that just happens to also carry a header) -- confirmed live
        # (2026-07-09) that carrying relates_to here just made ordinary
        # (non-MSC4501) clients redundantly render the same post twice, not
        # once. body is always the full plain_body (with quote + link),
        # never just a bare permalink.
        #
        # Deliberately NOT setting HAVEN_REMOVE_HEADER_FIELD either -- unlike
        # _build_repost_message/_echo_reply_in_own_room (both sent as the
        # relevant GHOST), this notice is sent as the bridge BOT (below,
        # as_user_id=bot_mxid) into the booster's own Profile Room, not as
        # a mirrored fediverse-authored post. See HAVEN_REMOVE_HEADER_FIELD's
        # own docstring for why that distinction matters.
        notice_content = build_preview_media_content(
            plain_body=plain_body,
            formatted_caption=formatted_caption,
            preview_image=preview_image, preview_video=preview_video,
        )
        bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
        try:
            notice_event_id = await request.app.state.synapse.send_message_event(
                actor_record.room_id, notice_content, as_user_id=bot_mxid,
            )
            # If the MSC4501 native record for this SAME boost shows up in
            # the next few seconds (see this module's own dedup-window
            # comment above), note_msc4501_native_repost finds this and
            # redacts it as redundant.
            _pending_boost_card[boost_dedup_key] = (actor_record.room_id, notice_event_id, time.monotonic())
        except SynapseError:
            logger.warning("Failed to post boost notice in %s", actor_record.room_id, exc_info=True)

    if matrix_event_id or notice_event_id:
        await repository.record_reaction(
            ReactionRecord(
                activity_id=activity_id,
                room_id=room_id,
                event_id=matrix_event_id or notice_event_id,
                target_ap_object_id=target_object_id,
                reactor_matrix_user_id=reactor_matrix_user_id,
                secondary_event_id=notice_event_id if matrix_event_id else None,
            )
        )

    return activity_id


async def maybe_federate_reaction(request: Request, event: dict) -> bool:
    """Returns True if this event was a reaction to a federated post (handled,
    successfully or not) -- callers should not process it any further."""
    if event.get("type") != "m.reaction":
        return False
    content = event.get("content") or {}
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") != "m.annotation":
        return False
    target_event_id = relates_to.get("event_id")
    key = relates_to.get("key")
    if not target_event_id or not key:
        return False

    config = request.app.state.config
    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return False  # never re-federate our own ghosts'/bot's own reactions

    repository = request.app.state.repository
    parent = await repository.get_federated_event_by_matrix_event(target_event_id)
    if parent is None:
        return False  # reacting to a purely-local Matrix message; nothing to federate

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        try:
            await request.app.state.synapse.send_message_event(
                room_id,
                {
                    "msgtype": "m.notice",
                    "body": f"This reaction wasn't sent to the fediverse: link a profile first by "
                    f'tagging me with "{bot_mxid} link profile".',
                },
                as_user_id=bot_mxid,
            )
        except Exception:
            logger.warning("Failed to send link-profile notice to %s", room_id, exc_info=True)
        return True

    # A local user's ActorRecord existing is enough (it could only have
    # come from an already-gated ;create/;link profile) -- but a
    # third-party identity's continued right to federate depends on still
    # being allowlisted RIGHT NOW, since revocation deliberately never
    # tears down an already-provisioned identity. Drop silently, same as
    # the "no linked profile" case, rather than erroring.
    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return True

    if _is_boost_emoji(key):
        # A boost is its own activity type (Announce), not a Like/EmojiReact
        # variant -- see send_boost, shared with the ";boost" command.
        # send_boost already posts a visible "you boosted so-and-so's post"
        # record into the booster's own Profile Room -- no separate
        # confirmation here on top of that.
        await send_boost(
            request,
            actor_record=actor_record,
            parent=parent,
            matrix_event_id=event.get("event_id"),
            room_id=room_id,
            reactor_matrix_user_id=sender,
        )
        return True

    # A mirrored repost's own ap_object_id/author_actor_id deliberately name
    # the Announce activity and its booster (see FederatedEvent's
    # docstring) -- a reaction to that message needs to target the actual
    # boosted post and reach its actual author, not "like" someone's boost
    # of it and notify the wrong account entirely.
    target_object_id = parent.boosted_object_id or parent.ap_object_id
    target_author_actor_id = parent.boosted_author_actor_id or parent.author_actor_id

    base = config.bridge.public_base_url
    activity_type = "Like" if _is_favorite_emoji(key) else "EmojiReact"
    activity_id = f"{actor_url(base, actor_record.username)}/reacts/{uuid.uuid4().hex}"
    reaction_activity = Activity(
        id=activity_id,
        type=activity_type,
        actor=actor_url(base, actor_record.username),
        object=target_object_id,
        content=key if activity_type == "EmojiReact" else None,
    )

    await deliver_to_actor_or_followers(
        request,
        target_actor_id=target_author_actor_id,
        activity=reaction_activity.to_dict(),
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )

    matrix_event_id = event.get("event_id")
    if matrix_event_id:
        # target_object_id, not parent.ap_object_id -- for a mirrored
        # repost this is the boosted post's own id (see above), so a later
        # Undo (maybe_federate_reaction_removal) looks up the right
        # FederatedEvent (the boosted post's own, from its ALSO having
        # been actually imported -- see _handle_announce) and re-derives
        # the same real author, not the booster.
        await repository.record_reaction(
            ReactionRecord(
                activity_id=activity_id,
                room_id=room_id,
                event_id=matrix_event_id,
                target_ap_object_id=target_object_id,
                reactor_matrix_user_id=sender,
            )
        )
    return True


async def maybe_federate_reaction_removal(request: Request, event: dict) -> bool:
    """Returns True if this event was a redaction of a reaction we know about
    (handled, successfully or not) -- callers should not process it further."""
    if event.get("type") != "m.room.redaction":
        return False
    redacted_event_id = event.get("redacts") or (event.get("content") or {}).get("redacts")
    if not redacted_event_id:
        return False

    repository = request.app.state.repository
    reaction = await repository.get_reaction_by_matrix_event(redacted_event_id)
    if reaction is None:
        return False  # not a reaction we're tracking at all

    if reaction.reactor_matrix_user_id is None:
        # An inbound-mirrored reaction was redacted locally only (e.g. room
        # moderation) -- nothing to tell the fediverse side, which never
        # asked for that.
        await repository.remove_reaction(reaction.activity_id)
        return True

    actor_record = await repository.get_local_actor_by_matrix_id(reaction.reactor_matrix_user_id)
    if actor_record is None:
        await repository.remove_reaction(reaction.activity_id)
        return True

    # Same live re-validation as maybe_federate_reaction -- a revoked
    # third-party sender's Undo doesn't go out either, just drop the local
    # record same as the "no linked profile" case above.
    if not await is_third_party_still_allowed(request, actor_record, room_id=reaction.room_id):
        await repository.remove_reaction(reaction.activity_id)
        return True

    parent = await repository.get_federated_event_by_ap_object(reaction.target_ap_object_id)
    if parent is None:
        await repository.remove_reaction(reaction.activity_id)
        return True

    # Same reasoning as maybe_federate_reaction: a repost's own
    # author_actor_id is the booster, not the boosted post's real author who
    # actually received the original reaction and needs the Undo.
    target_author_actor_id = parent.boosted_author_actor_id or parent.author_actor_id

    config = request.app.state.config
    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    undo = Activity(
        id=f"{own_actor_id}/undos/{uuid.uuid4().hex}",
        type="Undo",
        actor=own_actor_id,
        object=reaction.activity_id,
    )
    undo_dict = undo.to_dict()
    await deliver_to_actor_or_followers(
        request,
        target_actor_id=target_author_actor_id,
        activity=undo_dict,
        key_id=main_key_id(base, actor_record.username),
        private_key_pem=actor_record.private_key_pem,
    )
    # An Announce (see send_boost) is ALSO fanned out to the booster's own
    # followers, unlike a Like/EmojiReact (only ever delivered privately to
    # the target author) -- so undoing one has to reach them too, or their
    # timeline keeps showing a "boost" that's since been retracted. Told
    # apart from a Like/EmojiReact's activity_id by send_boost's own
    # "/announces/" minting convention (see maybe_federate_reaction).
    if "/announces/" in reaction.activity_id and target_author_actor_id != own_actor_id:
        await deliver_to_actor_or_followers(
            request,
            target_actor_id=own_actor_id,
            activity=undo_dict,
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )

    await repository.remove_reaction(reaction.activity_id)
    return True
