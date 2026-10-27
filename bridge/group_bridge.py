"""FEP-1b12 Group bridging: mirroring a remote ``Group`` this bridge
doesn't own (this section of the module) and hosting one it does (see
this module's own "Hosting our own Groups" section below).

A ``Group`` is a flat-chat FEP-1b12 actor -- the "message in a room: Note,
join: Follow" shape, used (per FEP-1b12's own adoption notes) by software
like Mitra, Friendica, Hubzilla, and Smithereen for ordinary group chat.
This is deliberately NOT the same thing as a Lemmy community
(``bridge.lemmy_bridge``), even though both are ``Group``-typed AP actors:
a Lemmy community is forum-shaped (every top-level message is a titled
``Page``, replies are ``Note`` comments, there's a real ``moderators``
collection); a Group here is flat -- every message, reply or not, is a
plain ``Note``, and FEP-1b12 itself defines no moderator-collection
concept at all. ``is_generic_group_actor`` is the gate that tells a Group
apart from a Lemmy community or a PeerTube channel (both also
``Group``-typed) when deciding how to mirror a REMOTE one.

A hosted Group's room gets NO MSC4501 ``room_type`` at all -- unlike a
Profile Room/PeerTube channel (``org.matrix.msc4501.social.profile``) or
a Lemmy community (``org.matrix.msc4501.social.group``), neither of which
describes a flat chat room; it's created/adopted as a plain Matrix room,
same as a DM/Chat ghost room.

Structurally this follows ``bridge.lemmy_bridge``'s own split closely
(mirroring vs. hosting, each with inbound/outbound halves), including
reusing ``channel_room_members``/``is_channel_member_known``/
``record_channel_member`` for per-room member tracking, for the same
reason Lemmy communities and Shoot Channels do: many different remote
ghosts can post in one shared room, resolved per-message rather than
fixed to the room itself.

For a MIRRORED Group (one we don't own):
- Inbound messages always arrive as an ``Announce`` whose ``actor`` IS the
  Group, wrapping a ``Note`` -- ``maybe_handle_group_announce``.
- Outbound (a Matrix member posting inside the mirrored room) is always a
  plain ``Create<Note>`` addressed directly to the Group's own inbox,
  never an ``Announce`` (that fan-out is only ever the Group's own home
  instance's job) -- ``maybe_distribute_group_room_message``. Unlike a
  mirrored Lemmy community, there's no reply-only restriction: a Group has
  no Page/title convention, so a brand new top-level message is just as
  valid as a reply.

For a Group we HOST ourselves (";create group"/";link group"), see the
"Hosting our own Groups" section below -- the real new responsibility
there (same as Lemmy's owned-community section) is fanning content out to
our own followers ourselves, since nobody else does it for us.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import AS_PUBLIC, Activity, Actor, Note, PublicKey
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.remote_actor import (
    RemoteActorFetchError,
    extract_banner_url,
    extract_icon_url,
    fetch_actor,
    resolve_actor_inbox,
)
from bridge.activitypub.sanitize import plain_text_to_note_html, strip_to_matrix_message
from bridge.activitypub.urls import actor_url, followers_url, main_key_id, media_url, username_from_actor_url
from bridge.crypto import generate_keypair
from bridge.identity_resolution import resolve_acting_identity
from bridge.note_mirroring import (
    KNOCK_JOIN_RULE,
    PROFILE_BANNER_STATE_TYPE,
    attach_media_to_content,
    deliver_to_actor_or_followers,
    mirrored_post_event_type,
    notify_mentioned_locals,
    resolve_and_invite_ghost,
    resolve_event_ts,
    resolve_mention_pills,
    send_bridge_info,
    set_ghost_room_banner,
)
from bridge.repository import ActorRecord, FederatedEvent, GroupRecord, GroupRoom
from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


# ============================================================================
# Mirroring a remote Group
# ============================================================================


async def is_generic_group_actor(request: Request, actor_doc: dict) -> bool:
    """Best-effort: is ``actor_doc`` a plain, flat-chat FEP-1b12 Group, as
    opposed to a Lemmy community or a PeerTube channel (both also
    ``Group``-typed)? Checked via NodeInfo software name, same cheap
    cached lookup ``is_lemmy_community_actor`` uses -- "lemmy"/"peertube"
    are excluded explicitly (they get their own, more specific mirroring),
    any OTHER software (Mitra, Friendica, Hubzilla, Smithereen, or simply
    unrecognized) falls into this generic bucket, matching FEP-1b12's own
    "adopted compatibly by several different implementations, no shared
    software signal" reality. Callers must check this AFTER
    ``bridge.lemmy_bridge.is_lemmy_community_actor`` returns False, same
    precedence ``;follow`` already uses between Lemmy and the plain
    remote-person fallback."""
    if not isinstance(actor_doc, dict) or actor_doc.get("type") != "Group":
        return False
    actor_id = actor_doc.get("id")
    domain = urlsplit(actor_id).hostname if isinstance(actor_id, str) else None
    if domain:
        software = await remote_software_name(request, domain)
        if software in ("lemmy", "peertube"):
            return False  # handled by their own, more specific mirroring
    return True


async def establish_group_follow(
    request: Request, *, sender: str, actor_record: ActorRecord, group_actor_id: str, actor_doc: dict, handle: str,
) -> tuple[str | None, str | None]:
    """The Group counterpart of ``bridge.commands._establish_remote_follow``
    / ``bridge.lemmy_bridge.establish_lemmy_community_follow``: get-or-
    create the shared Group room, invite ``sender`` into it, and deliver a
    signed ``Follow`` from ``actor_record`` to the Group. Returns
    ``(room_id, None)`` on success, ``(room_id, reason)`` if the room
    exists but the Follow delivery failed, or ``(None, reason)`` if
    nothing could be established at all."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    inbox = actor_doc.get("inbox")
    if not inbox:
        return None, "the Group's own actor document has no inbox"

    group_room = await ensure_group_room(request, group_actor_id=group_actor_id, actor_doc=actor_doc)
    if group_room is None:
        return None, "could not create a room for this Group"

    try:
        await synapse.invite_user(group_room.room_id, sender, as_user_id=bot_mxid)
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            logger.warning("Could not invite %s into Group room %s: %s", sender, group_room.room_id, exc)

    base = config.bridge.public_base_url
    follow_activity = Activity(
        id=f"{actor_url(base, actor_record.username)}/follows/{uuid.uuid4().hex}",
        type="Follow", actor=actor_url(base, actor_record.username), object=group_actor_id,
    )
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=follow_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError as exc:
        return group_room.room_id, str(exc)

    await repository.add_following(actor_record.username, group_actor_id)
    return group_room.room_id, None


async def _fetch_and_upload_media_if_any(request: Request, url: str | None) -> str | None:
    if not url:
        return None
    from bridge.media import fetch_and_upload_media

    return await fetch_and_upload_media(request.app.state.http_client, request.app.state.synapse, url)


async def ensure_group_room(
    request: Request, *, group_actor_id: str, actor_doc: dict | None = None,
) -> GroupRoom | None:
    """Get-or-create the Matrix room mirroring ``group_actor_id``.
    Idempotent -- safe to call on every follow. No ``room_type`` at all --
    see this module's own docstring."""
    repository = request.app.state.repository
    existing = await repository.get_group_room(group_actor_id)
    if existing is not None:
        return existing

    if actor_doc is None:
        try:
            actor_doc = await fetch_actor(request, group_actor_id)
        except RemoteActorFetchError:
            return None

    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    display_name = actor_doc.get("name") or actor_doc.get("preferredUsername") or "group"
    icon_url = extract_icon_url(actor_doc)
    avatar_mxc = await _fetch_and_upload_media_if_any(request, icon_url)
    banner_url = extract_banner_url(actor_doc)

    try:
        room_id = await synapse.create_room(
            as_user_id=bot_mxid,
            name=display_name,
            topic=actor_doc.get("summary") or f"Group: {display_name}",
            avatar_mxc=avatar_mxc,
            join_rule=KNOCK_JOIN_RULE,
        )
    except SynapseError:
        logger.warning("Could not create Group room for %s", group_actor_id, exc_info=True)
        return None

    record = GroupRoom(
        group_actor_id=group_actor_id, room_id=room_id, display_name=display_name,
        icon_url=icon_url, banner_url=banner_url,
        # Only ever True for this very first creation -- same reasoning as
        # LemmyCommunityRoom's identical field.
        pending_backfill=True,
    )
    await repository.register_group_room(record)
    await send_bridge_info(
        request, room_id=room_id, actor_id=group_actor_id, display_name=display_name,
        avatar_mxc=avatar_mxc, as_user_id=bot_mxid,
    )
    await add_bridge_widget(request, room_id=room_id)
    return record


def _note_and_author_from_group_item(item) -> tuple[dict, str] | None:
    """Normalize a raw Group outbox item into ``(note, author_actor_id)``.
    A Group's own outbox entries are an ``Announce`` wrapping the original
    ``Create<Note>`` -- same one-level-unwrap convention as Lemmy's own
    ``_page_and_author_from_item`` (FEP-1b12's Announce fan-out), falling
    back to a bare ``Create``/``Note`` for any implementation that doesn't
    wrap its outbox that way."""
    if not isinstance(item, dict):
        return None
    if item.get("type") == "Announce":
        inner = item.get("object")
        if isinstance(inner, dict) and inner.get("type") in ("Create", "Note"):
            item = inner
        else:
            return None
    if item.get("type") == "Create":
        obj = item.get("object")
    elif item.get("type") == "Note":
        obj = item
    else:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "Note":
        return None
    from bridge.inbox_dispatch import _note_author

    author_actor_id = _note_author(obj)
    if not isinstance(author_actor_id, str):
        return None
    return obj, author_actor_id


async def _import_unthreaded_group_note(request: Request, *, note: dict, author_actor_id: str, room_id: str) -> FederatedEvent | None:
    """A Group message with no trackable parent -- mirror it as a plain,
    flat message in the room (never a thread reply) rather than a reduced-
    fidelity fallback, since an untracked top-level Note is simply the
    NORMAL case for a flat-chat Group, not a race/edge-case the way an
    untracked Lemmy comment's parent is (contrast
    ``bridge.lemmy_bridge._import_unthreaded_comment``, which really is a
    fallback there). Returns the FederatedEvent recorded, or None on
    failure."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    resolved = await resolve_and_invite_ghost(request, author_actor_id, room_id)
    if resolved is None:
        return None
    ghost_mxid, _actor_doc = resolved
    try:
        await synapse.join_room(room_id, as_user_id=ghost_mxid)
    except SynapseError:
        logger.info("Could not join Group message ghost %s into %s", ghost_mxid, room_id, exc_info=True)
        return None

    if not await repository.is_channel_member_known(room_id, author_actor_id):
        await repository.record_channel_member(room_id, author_actor_id)

    mentions = await resolve_mention_pills(request, room_id=room_id, note=note)
    plain, safe_html = strip_to_matrix_message(
        note.get("content") or "", mention_pills=mentions.pills, media_type=note.get("mediaType"),
    )
    if not plain and not safe_html:
        return None
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html
    if mentions.mentioned_locals:
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}

    from bridge.activitypub.remote_actor import extract_attachments

    message_content, _ = await attach_media_to_content(request, message_content, extract_attachments(note))

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            room_id, message_content, as_user_id=ghost_mxid,
            event_type=mirrored_post_event_type(request.app.state.config),
            ts=resolve_event_ts(
                note, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror Group message from %s into %s", author_actor_id, room_id, exc_info=True)
        return None

    federated_event = None
    ap_object_id = note.get("id")
    if isinstance(ap_object_id, str):
        federated_event = FederatedEvent(
            event_id=event_id, room_id=room_id, ap_object_id=ap_object_id, author_actor_id=author_actor_id,
        )
        await repository.record_federated_event(federated_event)

    await notify_mentioned_locals(
        request, mentioned=mentions.mentioned_locals, room_id=room_id, event_id=event_id,
        author_actor_id=author_actor_id,
    )
    return federated_event


async def maybe_handle_group_announce(request: Request, activity: Activity) -> bool:
    """Returns True if ``activity`` was a mirrored Group's own ``Announce``
    fan-out of a message (handled, successfully mirrored or not). Callers
    (``bridge.inbox_dispatch._handle_announce_locked``) must check this
    before their own repost-reaction logic runs, same reasoning as
    ``bridge.lemmy_bridge.maybe_handle_community_announce``."""
    repository = request.app.state.repository
    group_room = await repository.get_group_room(activity.actor)
    if group_room is None:
        return False

    from bridge.inbox_dispatch import _note_author, _resolve_object

    obj = await _resolve_object(request, activity.object)
    if obj is None:
        return True
    if obj.get("type") != "Note":
        return True  # recognized as Group fan-out, nothing renderable in it

    author_actor_id = _note_author(obj)
    if not isinstance(author_actor_id, str):
        return True

    ap_object_id = obj.get("id")
    if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
        return True  # already mirrored -- e.g. a redelivered transaction

    in_reply_to = obj.get("inReplyTo")
    parent = await repository.get_federated_event_by_ap_object(in_reply_to) if isinstance(in_reply_to, str) else None
    if parent is not None and parent.room_id == group_room.room_id:
        from bridge.inbox_dispatch import _mirror_note_as_reply

        await _mirror_note_as_reply(request, obj, parent, author_actor_id)
        return True

    await _import_unthreaded_group_note(request, note=obj, author_actor_id=author_actor_id, room_id=group_room.room_id)
    return True


async def maybe_distribute_group_room_message(request: Request, event: dict) -> bool:
    """Returns True if ``event`` belonged to a mirrored Group room
    (handled, distributed or not). Unlike a mirrored Lemmy community, ANY
    message -- reply or not -- is sent on, as a plain ``Create<Note>``
    addressed directly to the Group (never an ``Announce`` -- that fan-out
    is only ever the Group's own home instance's job): a Group has no
    Page/title convention, so there's no "starting a brand new post isn't
    supported" restriction to apply here."""
    if event.get("type") != "m.room.message":
        return False

    from bridge.commands import is_foreign_bot_command, is_foreign_bridge_user, message_addresses_bot

    content = event.get("content") or {}
    config = request.app.state.config
    if message_addresses_bot(content, config) or is_foreign_bot_command(content, config):
        return True

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    group_room = await repository.get_group_room_by_room_id(room_id)
    if group_room is None:
        return False

    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if (
        sender == bot_mxid
        or sender.startswith(f"@{config.appservice.user_prefix}")
        or is_foreign_bridge_user(sender, config)
    ):
        return True  # our own bot's/a ghost's echo -- or a sibling bridge's own ghost -- never re-federate

    from bridge.shadow_identity import ensure_shadow_actor, is_third_party_still_allowed, shadow_created_notice

    resolution = await ensure_shadow_actor(request, sender=sender, room_id=room_id)
    actor_record = resolution.record
    if actor_record is None:
        return True  # not eligible for even a shadow identity -- stays Matrix-only
    if resolution.created:
        try:
            await request.app.state.synapse.send_message_event(
                room_id,
                {"msgtype": "m.notice", "body": shadow_created_notice(request, actor_record)},
                as_user_id=bot_mxid,
            )
        except Exception:
            logger.warning("Failed to send shadow-identity-created notice to %s", room_id, exc_info=True)

    # Re-validated live on every message, not just at creation -- see
    # channel_bridge.maybe_handle_channel_message's identical comment.
    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return True

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed

    body = (content.get("body") or "").strip()
    if not body:
        return False

    from bridge.reply_bridge import _extract_reply_target_event_id

    target_event_id = _extract_reply_target_event_id(content)
    parent = await repository.get_federated_event_by_matrix_event(target_event_id) if target_event_id else None
    is_reply = parent is not None and parent.room_id == room_id

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    note_id = f"{own_actor_id}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    note = Note(
        id=note_id, attributed_to=own_actor_id, content=plain_text_to_note_html(body), published=published,
        to=[AS_PUBLIC], cc=[group_room.group_actor_id, followers_url(base, actor_record.username)],
        in_reply_to=parent.ap_object_id if is_reply else None,
        audience=group_room.group_actor_id, tag=[{"type": "Mention", "href": group_room.group_actor_id}],
    )

    inbox = await resolve_actor_inbox(request, group_room.group_actor_id)
    if inbox is None:
        return True

    create_activity = Activity(
        id=f"{own_actor_id}/activities/{uuid.uuid4().hex}", type="Create", actor=own_actor_id,
        object=note.to_dict(), to=note.to, cc=note.cc, published=published, audience=group_room.group_actor_id,
    )
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver message to Group %s", group_room.group_actor_id, exc_info=True)
        return True

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id, room_id=room_id, ap_object_id=note_id, author_actor_id=own_actor_id,
            thread_root_event_id=(parent.thread_root_event_id or parent.event_id) if is_reply else None,
        )
    )
    return True


async def backfill_group_posts(request: Request, *, group_room: GroupRoom, count: int) -> tuple[int, int, int]:
    """Pull the Group's own outbox -- its latest messages -- the same way
    ``bridge.lemmy_bridge.backfill_community_posts`` does for a mirrored
    Lemmy community, just landing each message in the shared Group room
    via ``_import_unthreaded_group_note`` instead of ``import_lemmy_page``.
    Returns ``(imported, already, failed)``."""
    try:
        actor_doc = await fetch_actor(request, group_room.group_actor_id)
    except RemoteActorFetchError:
        return 0, 0, 0
    outbox = actor_doc.get("outbox")
    if not outbox:
        return 0, 0, 0

    from bridge.commands import _collect_recent_items

    raw_items = await _collect_recent_items(request, outbox, limit=count)
    repository = request.app.state.repository

    imported = already = failed = 0
    for item in reversed(raw_items):  # oldest of the collected batch first
        parsed = _note_and_author_from_group_item(item)
        if parsed is None:
            failed += 1
            continue
        note, author_actor_id = parsed
        ap_object_id = note.get("id")
        if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
            already += 1
            continue
        try:
            result = await _import_unthreaded_group_note(
                request, note=note, author_actor_id=author_actor_id, room_id=group_room.room_id,
            )
        except Exception:
            logger.warning("Group backfill failed to mirror %s", ap_object_id, exc_info=True)
            result = None
        if result is not None:
            imported += 1
        else:
            failed += 1
    return imported, already, failed


# ============================================================================
# Hosting our own Groups
# ============================================================================
#
# Unlike everything above (which only ever MIRRORS a remote Group we don't
# own), this bridge can also BE a Group's authoritative home instance
# (";create group"/";link group", see bridge.commands). That carries the
# same real new responsibility Lemmy's owned-community section has: when a
# message arrives addressed directly to one of our own Groups (from a
# local Matrix member, or a genuinely remote fediverse user posting into
# it), WE are the only one who can fan it out to our own followers.


async def mint_owned_group(
    request: Request, *, owner: ActorRecord, group_id: str, room_id: str,
    display_name: str, summary: str, icon_mxc: str | None, banner_mxc: str | None,
) -> GroupRecord:
    """Generate a keypair, persist the record, and tag the room. Shared by
    both ";create group" (a brand new room) and ";link group" (an already-
    existing one) -- see ``bridge.lemmy_bridge.mint_owned_community``'s own
    docstring for why this is power-level-agnostic."""
    config = request.app.state.config
    repository = request.app.state.repository
    base = config.bridge.public_base_url
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    icon_url = media_url(base, icon_mxc) if icon_mxc else None
    banner_url = media_url(base, banner_mxc) if banner_mxc else None

    private_key_pem, public_key_pem = generate_keypair()
    record = GroupRecord(
        username=group_id, owner_username=owner.username, room_id=room_id,
        public_key_pem=public_key_pem, private_key_pem=private_key_pem,
        display_name=display_name, summary=summary, icon_url=icon_url, banner_url=banner_url,
    )
    await repository.register_group(record)
    await send_bridge_info(
        request, room_id=room_id, actor_id=actor_url(base, group_id),
        display_name=display_name, avatar_mxc=icon_mxc, as_user_id=bot_mxid,
    )
    await add_bridge_widget(request, room_id=room_id)
    return record


async def push_group_update(request: Request, group: GroupRecord) -> None:
    """Send a signed ``Update(Group)`` carrying ``group``'s CURRENT display
    name/summary/avatar/banner to every one of its followers -- same
    "an already-cached copy on a follower's server otherwise silently goes
    stale" reasoning as ``bridge.lemmy_bridge.push_community_update``.
    Self-signed by the Group actor itself (unlike Lemmy's own convention,
    which Lemmy specifically requires be signed by the editing moderator --
    FEP-1b12 defines no such requirement, and a Group here has no single
    "moderator" distinct from its owner anyway)."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository = request.app.state.repository

    actor = Actor(
        id=actor_url(base, group.username),
        type="Group",
        preferred_username=group.username,
        name=group.display_name or group.username,
        summary=plain_text_to_note_html(group.summary) if group.summary else group.summary,
        url=actor_url(base, group.username),
        inbox=f"{base}/inbox/{group.username}",
        outbox=f"{base}/outbox/{group.username}",
        followers=followers_url(base, group.username),
        following=f"{base}/following/{group.username}",
        discoverable=True,
        icon_url=group.icon_url,
        image_url=group.banner_url,
        shared_inbox=f"{base}/inbox",
        public_key=PublicKey(
            id=main_key_id(base, group.username), owner=actor_url(base, group.username),
            public_key_pem=group.public_key_pem,
        ),
    )
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_activity = Activity(
        id=f"{actor.id}/updates/{uuid.uuid4().hex}", type="Update", actor=actor.id, object=actor.to_dict(),
        published=published, to=[AS_PUBLIC], cc=[actor.id], audience=actor.id,
    )
    followers = await repository.list_followers(group.username)
    for follower_actor_id in followers:
        inbox = await resolve_actor_inbox(request, follower_actor_id)
        if inbox is None:
            continue
        try:
            await deliver_activity(
                request.app.state.http_client, inbox_url=inbox, activity=update_activity.to_dict(),
                key_id=main_key_id(base, group.username), private_key_pem=group.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver Group update to %s", follower_actor_id, exc_info=True)


# -- room-state -> Group actor field sync ------------------------------------


async def maybe_handle_owned_group_name_change(request: Request, event: dict) -> bool:
    """Same as ``bridge.lemmy_bridge.maybe_handle_owned_community_name_change``,
    for a hosted Group room's ``m.room.name``."""
    if event.get("type") != "m.room.name":
        return False
    content = event.get("content") or {}
    name = content.get("name")
    if not isinstance(name, str) or not name:
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False
    repository = request.app.state.repository
    group = await repository.get_group_by_room_id(room_id)
    if group is None:
        return False
    if group.display_name != name:
        group = replace(group, display_name=name)
        await repository.register_group(group)
        await push_group_update(request, group)
    return True


async def maybe_handle_owned_group_topic_change(request: Request, event: dict) -> bool:
    """Same as above, for ``m.room.topic`` (-> the Group's summary)."""
    if event.get("type") != "m.room.topic":
        return False
    content = event.get("content") or {}
    topic = content.get("topic")
    if not isinstance(topic, str):
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False
    repository = request.app.state.repository
    group = await repository.get_group_by_room_id(room_id)
    if group is None:
        return False
    if group.summary != topic:
        group = replace(group, summary=topic)
        await repository.register_group(group)
        await push_group_update(request, group)
    return True


async def maybe_handle_owned_group_avatar_change(request: Request, event: dict) -> bool:
    """Same as above, for ``m.room.avatar``."""
    if event.get("type") != "m.room.avatar":
        return False
    content = event.get("content") or {}
    avatar_mxc = content.get("url")
    if not isinstance(avatar_mxc, str) or not avatar_mxc:
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False
    repository = request.app.state.repository
    group = await repository.get_group_by_room_id(room_id)
    if group is None:
        return False
    config = request.app.state.config
    try:
        icon_url = media_url(config.bridge.public_base_url, avatar_mxc)
    except ValueError:
        return True
    if group.icon_url != icon_url:
        await repository.mark_media_published(avatar_mxc)
        group = replace(group, icon_url=icon_url)
        await repository.register_group(group)
        await push_group_update(request, group)
    return True


async def maybe_handle_owned_group_banner_change(request: Request, event: dict) -> bool:
    """Same as above, for ``PROFILE_BANNER_STATE_TYPE``."""
    if event.get("type") != PROFILE_BANNER_STATE_TYPE:
        return False
    content = event.get("content") or {}
    banner_mxc = content.get("url")
    if not isinstance(banner_mxc, str) or not banner_mxc:
        return False
    room_id = event.get("room_id", "")
    if not room_id:
        return False
    repository = request.app.state.repository
    group = await repository.get_group_by_room_id(room_id)
    if group is None:
        return False
    config = request.app.state.config
    try:
        banner_url = media_url(config.bridge.public_base_url, banner_mxc)
    except ValueError:
        return True
    if group.banner_url != banner_url:
        await repository.mark_media_published(banner_mxc)
        group = replace(group, banner_url=banner_url)
        await repository.register_group(group)
        await push_group_update(request, group)
    return True


# -- outbound: a Matrix member posting in one of our own Group rooms -------


async def maybe_distribute_group_message(request: Request, event: dict) -> bool:
    """A Matrix member's new message inside one of OUR OWN hosted Group
    rooms -- federates out as a real ``Create<Note>`` (signed by the
    POSTER's own linked profile) PLUS an ``Announce`` (signed by the GROUP
    itself) to the Group's own followers, same choreography as
    ``bridge.lemmy_bridge.maybe_distribute_owned_community_message`` minus
    the Page/title split: every message here is a plain ``Note``, reply or
    not."""
    if event.get("type") != "m.room.message":
        return False

    from bridge.commands import is_foreign_bot_command, is_foreign_bridge_user, message_addresses_bot

    content = event.get("content") or {}
    config = request.app.state.config
    if message_addresses_bot(content, config) or is_foreign_bot_command(content, config):
        return True

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    group = await repository.get_group_by_room_id(room_id)
    if group is None:
        return False

    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if (
        sender == bot_mxid
        or sender.startswith(f"@{config.appservice.user_prefix}")
        or is_foreign_bridge_user(sender, config)
    ):
        return True  # our own bot's/a ghost's echo of a mirrored post -- or a sibling bridge's own ghost

    from bridge.shadow_identity import ensure_shadow_actor, is_third_party_still_allowed, shadow_created_notice

    resolution = await ensure_shadow_actor(request, sender=sender, room_id=room_id)
    actor_record = resolution.record
    if actor_record is None:
        from bridge.commands import _notice

        await _notice(
            request, room_id,
            "You need a linked profile before posting here -- run \";link profile\" in your own room first.",
        )
        return True
    if resolution.created:
        from bridge.commands import _notice

        await _notice(request, room_id, shadow_created_notice(request, actor_record))

    # Re-validated live on every message, not just at creation -- see
    # channel_bridge.maybe_handle_channel_message's identical comment.
    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return True

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed

    body = (content.get("body") or "").strip()

    from bridge.media import build_ap_attachment, resolve_attachment_or_request_confirmation

    if not await resolve_attachment_or_request_confirmation(
        request, content=content, room_id=room_id, sender=sender, trigger_event_id=matrix_event_id,
    ):
        return True  # an encrypted attachment, confirmation requested, nothing federates yet

    attachment = build_ap_attachment(config.bridge.public_base_url, content)
    if not body and attachment is None:
        return False

    from bridge.reply_bridge import _extract_reply_target_event_id

    target_event_id = _extract_reply_target_event_id(content)
    parent = await repository.get_federated_event_by_matrix_event(target_event_id) if target_event_id else None
    is_reply = parent is not None and parent.room_id == room_id

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    own_object_id = f"{own_actor_id}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    group_actor_id = actor_url(base, group.username)

    obj_dict: dict = {
        "id": own_object_id, "type": "Note", "attributedTo": own_actor_id,
        "content": plain_text_to_note_html(body) if body else "", "mediaType": "text/html",
        "published": published, "to": [AS_PUBLIC], "cc": [group_actor_id], "audience": group_actor_id,
        "tag": [{"type": "Mention", "href": group_actor_id}],
    }
    thread_root_event_id = None
    if is_reply:
        obj_dict["inReplyTo"] = parent.ap_object_id
        thread_root_event_id = parent.thread_root_event_id or parent.event_id

    if attachment is not None:
        await repository.mark_media_published(content["url"])
        obj_dict["attachment"] = [attachment]

    create_activity = Activity(
        id=f"{own_object_id}/activity", type="Create", actor=own_actor_id, object=obj_dict,
        published=published, to=[AS_PUBLIC], cc=[group_actor_id], audience=group_actor_id,
    )
    announce_activity = Activity(
        id=f"{own_object_id}/announces/{uuid.uuid4().hex}", type="Announce", actor=group_actor_id,
        object=create_activity.to_dict(), published=published, to=[AS_PUBLIC],
        cc=[followers_url(base, group.username)],
    )

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id, room_id=room_id, ap_object_id=own_object_id, author_actor_id=own_actor_id,
            thread_root_event_id=thread_root_event_id,
        )
    )

    followers = await repository.list_followers(group.username)
    for recipient_actor_id in followers:
        try:
            await deliver_to_actor_or_followers(
                request, target_actor_id=recipient_actor_id, activity=announce_activity.to_dict(),
                key_id=main_key_id(base, group.username), private_key_pem=group.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver Group Announce to %s", recipient_actor_id, exc_info=True)
    return True


async def maybe_handle_owned_group_create(request: Request, activity: Activity) -> bool:
    """A ``Create<Note>`` arriving addressed DIRECTLY to one of OUR OWN
    hosted Groups -- never Announce-wrapped (nobody else announces for us,
    same reasoning as Lemmy's owned-community section). If ``inReplyTo``
    resolves to something we track in one of our own Group rooms, it
    threads there via the same generic ``_mirror_note_as_reply`` machinery
    every other reply in this bridge uses; otherwise (no ``inReplyTo``, or
    it doesn't resolve) it's just a flat top-level message -- NORMAL for a
    Group, unlike Lemmy's Page-only top-level posts, so this never refuses
    it the way Lemmy's equivalent does. Either way, re-``Announce``s it to
    our own followers afterward."""
    from bridge.inbox_dispatch import _resolve_object

    repository = request.app.state.repository
    obj = await _resolve_object(request, activity.object)
    if not isinstance(obj, dict) or obj.get("type") != "Note":
        return False

    config = request.app.state.config
    audience = obj.get("audience")
    if not isinstance(audience, str):
        return False
    username = username_from_actor_url(config.bridge.public_base_url, audience)
    if username is None:
        return False
    group = await repository.get_group(username)
    if group is None:
        return False

    ap_object_id = obj.get("id")
    if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
        return True

    author_actor_id = obj.get("attributedTo")
    if not isinstance(author_actor_id, str):
        return True

    in_reply_to = obj.get("inReplyTo")
    parent = await repository.get_federated_event_by_ap_object(in_reply_to) if isinstance(in_reply_to, str) else None
    if parent is not None and parent.room_id == group.room_id:
        from bridge.inbox_dispatch import _mirror_note_as_reply

        federated = await _mirror_note_as_reply(request, obj, parent, author_actor_id)
    else:
        federated = await _import_unthreaded_group_note(
            request, note=obj, author_actor_id=author_actor_id, room_id=group.room_id,
        )

    if federated is not None:
        await _reannounce_owned_group_content(request, group=group, create_obj=obj, author_actor_id=author_actor_id)
    return True


async def _reannounce_owned_group_content(
    request: Request, *, group: GroupRecord, create_obj: dict, author_actor_id: str,
) -> None:
    base = request.app.state.config.bridge.public_base_url
    group_actor_id = actor_url(base, group.username)
    published = create_obj.get("published") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    create_activity = Activity(
        id=f"{create_obj.get('id', '')}/activity", type="Create", actor=author_actor_id, object=create_obj,
        published=published, to=[AS_PUBLIC], cc=[group_actor_id], audience=group_actor_id,
    )
    announce_activity = Activity(
        id=f"{group_actor_id}/announces/{uuid.uuid4().hex}", type="Announce", actor=group_actor_id,
        object=create_activity.to_dict(), published=published, to=[AS_PUBLIC],
        cc=[followers_url(base, group.username)],
    )
    repository = request.app.state.repository
    followers = await repository.list_followers(group.username)
    for recipient_actor_id in followers:
        if recipient_actor_id == author_actor_id:
            continue  # the poster already has their own copy -- no need to announce it back to them
        try:
            await deliver_to_actor_or_followers(
                request, target_actor_id=recipient_actor_id, activity=announce_activity.to_dict(),
                key_id=main_key_id(base, group.username), private_key_pem=group.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to re-announce inbound Group content to %s", recipient_actor_id, exc_info=True)
