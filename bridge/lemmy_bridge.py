"""Lemmy community bridging (Phase 1: mirroring an existing remote community).

A Lemmy community is a ``Group`` actor, same AP type PeerTube uses for a
channel -- but unlike a PeerTube channel (which only ever posts as itself,
so an ordinary ``RemoteActorRoom``/single-ghost mapping is enough, see
``bridge.peertube``), a community's posts and comments come from many
different Lemmy ``Person`` actors, fanned out by the community itself via
``Announce``. That's structurally identical to Shoot's own guild-Channel
model (see ``bridge.channel_bridge``'s module docstring) -- a shared Matrix
room with no single ``ghost_user_id``, resolving the real author's ghost
per-post/comment -- so this module follows that one closely, including
reusing its ``channel_room_members`` table (``is_channel_member_known``/
``record_channel_member``) for member tracking: that table is already keyed
purely by ``(room_id, member_actor_id)``, nothing Shoot-specific about it.

We never own a community ourselves in Phase 1 (that's the separate,
not-yet-built Phase 2) -- we're always the non-owning side, so:

- Inbound posts/comments always arrive as an ``Announce`` whose ``actor`` IS
  the community, wrapping a ``Page`` (post) or ``Note`` (comment) --
  ``maybe_handle_community_announce``.
- Outbound (a Matrix member replying inside the mirrored room) is always a
  plain ``Create<Note>`` addressed directly to the community's own inbox,
  never an ``Announce`` -- ``maybe_distribute_community_message``. A brand
  new top-level post from the Matrix side isn't supported yet (Lemmy's
  ``Page`` requires a title, which a plain ``m.room.message`` has no
  convention for) -- that reply gets a notice instead of being dropped
  silently.

A real Lemmy moderator of the community is mapped to Matrix power level 50
(``_MOD_POWER_LEVEL``) in the room -- a LOCAL Matrix user who is a genuine
remote mod (their own AP actor is listed in the community's own
``moderators`` collection) can then exercise real moderation power: Synapse
itself enforces the power-level check before a ban/redaction event ever
reaches this bridge, so by the time ``maybe_handle_community_ban``/
``bridge.delete_bridge``'s own community branch sees one, authority has
already been verified on the Matrix side. Whether Lemmy's own instance
actually honors a ``Block``/``Delete`` signed by that actor is Lemmy's own
independent judgment call, not something this bridge can force.
"""

from __future__ import annotations

import logging
import mimetypes
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import AS_PUBLIC, Activity, Note
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.remote_actor import (
    RemoteActorFetchError,
    extract_banner_url,
    extract_icon_url,
    fetch_actor,
    resolve_actor_inbox,
)
from bridge.activitypub.sanitize import plain_text_to_note_html, strip_to_matrix_message
from bridge.activitypub.urls import actor_url, followers_url, main_key_id
from bridge.note_mirroring import (
    KNOCK_JOIN_RULE,
    SOCIAL_GROUP_ROOM_TYPE,
    attach_media_to_content,
    mirrored_post_event_type,
    notify_mentioned_locals,
    resolve_and_invite_ghost,
    resolve_event_ts,
    resolve_mention_pills,
    send_bridge_info,
    source_post_url,
)
from bridge.repository import FederatedEvent, LemmyCommunityRoom
from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# Real Lemmy mods are mapped to this exact Matrix power level, not merely
# "above default" -- so a resync's own diff (see sync_community_moderators)
# can tell "a real mod we granted this" apart from some other, unrelated
# elevated grant a room admin made by hand.
_MOD_POWER_LEVEL = 50

# Self-cleaning, refcounted per-room resync lock/debounce state, same
# pattern as bridge.note_mirroring.activity_lock (reused directly, keyed
# under a namespaced prefix so it can never collide with a real activity
# id) -- see maybe_resync_community.
_last_resync_at: dict[str, float] = {}


async def is_lemmy_community_actor(request: Request, actor_doc: dict) -> bool:
    """Best-effort: does ``actor_doc`` look like a Lemmy community, as
    opposed to some other ``Group``-typed actor (most notably a PeerTube
    channel, see ``bridge.peertube``)? Checked via NodeInfo software name
    first (cheap, already cached per-domain -- see ``remote_software_name``)
    falling back to "does its own ``attributedTo`` look like a moderators
    collection URL" -- confirmed live 2026-10-20 against a real community
    actor document that Lemmy's own moderators collection is named via
    ``attributedTo``, NOT a dedicated ``moderators`` field (unlike what an
    earlier, wrong reading of join-lemmy.org's docs had this checking)."""
    if not isinstance(actor_doc, dict) or actor_doc.get("type") != "Group":
        return False
    actor_id = actor_doc.get("id")
    domain = None
    if isinstance(actor_id, str):
        from urllib.parse import urlsplit

        domain = urlsplit(actor_id).hostname
    if domain:
        software = await remote_software_name(request, domain)
        if software == "lemmy":
            return True
        if software is not None:
            return False  # a known OTHER software's Group actor (e.g. peertube)
    attributed_to = actor_doc.get("attributedTo")
    return isinstance(attributed_to, str) and "moderators" in attributed_to.lower()


async def establish_lemmy_community_follow(
    request: Request, *, sender: str, actor_record, community_actor_id: str, actor_doc: dict, handle: str,
) -> tuple[str | None, str | None]:
    """The community counterpart of ``bridge.commands._establish_remote_follow``:
    get-or-create the shared community room, invite ``sender`` into it, and
    deliver a signed ``Follow`` from ``actor_record`` to the community.
    Returns ``(room_id, None)`` on success, ``(room_id, reason)`` if the
    room exists but the Follow delivery failed, or ``(None, reason)`` if
    nothing could be established at all."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    inbox = actor_doc.get("inbox")
    if not inbox:
        return None, "the community's own actor document has no inbox"

    community_room = await ensure_lemmy_community_room(request, community_actor_id=community_actor_id, actor_doc=actor_doc)
    if community_room is None:
        return None, "could not create a room for this community"

    try:
        await synapse.invite_user(community_room.room_id, sender, as_user_id=bot_mxid)
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            logger.warning("Could not invite %s into community room %s: %s", sender, community_room.room_id, exc)

    follow_activity = Activity(
        id=f"{actor_url(config.bridge.public_base_url, actor_record.username)}/follows/{uuid.uuid4().hex}",
        type="Follow", actor=actor_url(config.bridge.public_base_url, actor_record.username),
        object=community_actor_id,
    )
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=follow_activity.to_dict(),
            key_id=main_key_id(config.bridge.public_base_url, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError as exc:
        return community_room.room_id, str(exc)

    await repository.add_following(actor_record.username, community_actor_id)
    return community_room.room_id, None


async def ensure_lemmy_community_room(
    request: Request, *, community_actor_id: str, actor_doc: dict | None = None,
) -> LemmyCommunityRoom | None:
    """Get-or-create the Matrix room mirroring ``community_actor_id``.
    Idempotent -- safe to call on every follow/resync."""
    repository = request.app.state.repository
    existing = await repository.get_lemmy_community_room(community_actor_id)
    if existing is not None:
        return existing

    if actor_doc is None:
        try:
            actor_doc = await fetch_actor(request, community_actor_id)
        except RemoteActorFetchError:
            return None

    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    display_name = actor_doc.get("name") or actor_doc.get("preferredUsername") or "community"
    icon_url = extract_icon_url(actor_doc)
    avatar_mxc = await fetch_and_upload_media_if_any(request, icon_url)
    banner_url = extract_banner_url(actor_doc)
    # Lemmy names its moderators collection via attributedTo, not a
    # dedicated "moderators" field -- see is_lemmy_community_actor's own
    # docstring.
    moderators_url = actor_doc.get("attributedTo")
    moderators_url = moderators_url if isinstance(moderators_url, str) else None

    try:
        room_id = await synapse.create_room(
            as_user_id=bot_mxid,
            name=display_name,
            topic=actor_doc.get("summary") or f"Lemmy community: {display_name}",
            avatar_mxc=avatar_mxc,
            room_type=SOCIAL_GROUP_ROOM_TYPE,
            join_rule=KNOCK_JOIN_RULE,
        )
    except SynapseError:
        logger.warning("Could not create community room for %s", community_actor_id, exc_info=True)
        return None

    record = LemmyCommunityRoom(
        community_actor_id=community_actor_id, room_id=room_id, display_name=display_name,
        icon_url=icon_url, banner_url=banner_url, moderators_url=moderators_url,
        # Only ever True for this very first creation -- an already-
        # existing community room reused by a later follower never reaches
        # this line at all (the `existing is not None` early return above).
        pending_backfill=True,
    )
    await repository.register_lemmy_community_room(record)
    await send_bridge_info(
        request, room_id=room_id, actor_id=community_actor_id, display_name=display_name,
        avatar_mxc=avatar_mxc, as_user_id=bot_mxid,
    )
    await add_bridge_widget(request, room_id=room_id)
    await sync_community_moderators(request, record, actor_doc=actor_doc)
    return record


async def fetch_and_upload_media_if_any(request: Request, url: str | None) -> str | None:
    if not url:
        return None
    from bridge.media import fetch_and_upload_media

    return await fetch_and_upload_media(request.app.state.http_client, request.app.state.synapse, url)


async def sync_community_moderators(
    request: Request, community_room: LemmyCommunityRoom, *, actor_doc: dict | None = None,
) -> None:
    """Fetch the community's own ``moderators`` collection and bring the
    room's power levels into line with it: every actor currently listed
    gets ``_MOD_POWER_LEVEL``, and any ghost CURRENTLY at exactly that level
    who is no longer listed is demoted back to the room default. Best-effort
    throughout, same as the rest of this bridge's room bookkeeping."""
    moderators_url = community_room.moderators_url
    if moderators_url is None and actor_doc is not None:
        candidate = actor_doc.get("attributedTo")
        moderators_url = candidate if isinstance(candidate, str) else None
    if moderators_url is None:
        return

    from bridge.commands import _collect_recent_items  # deferred: avoids a module-load cycle, see channel_bridge.py

    items = await _collect_recent_items(request, moderators_url, limit=1000)
    mod_actor_ids: set[str] = set()
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            mod_actor_ids.add(item["id"])
        elif isinstance(item, str):
            mod_actor_ids.add(item)

    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    repository = request.app.state.repository

    try:
        power_levels = await synapse.get_room_state(
            community_room.room_id, "m.room.power_levels", "", as_user_id=bot_mxid
        )
    except SynapseError:
        logger.info("Could not read power levels for community room %s", community_room.room_id, exc_info=True)
        return
    users = dict(power_levels.get("users") or {})

    # Demote anyone currently at exactly _MOD_POWER_LEVEL whose actor id we
    # can reverse-resolve (via their ghost profile) and who isn't in the
    # fresh moderator list any more.
    changed = False
    for mxid, level in list(users.items()):
        if level != _MOD_POWER_LEVEL:
            continue
        ghost_profile = await repository.get_ghost_profile_by_mxid(mxid)
        if ghost_profile is None or ghost_profile.actor_id in mod_actor_ids:
            continue
        del users[mxid]
        changed = True

    for mod_actor_id in mod_actor_ids:
        resolved = await resolve_and_invite_ghost(request, mod_actor_id, community_room.room_id)
        if resolved is None:
            continue
        ghost_mxid, _actor_doc = resolved
        if users.get(ghost_mxid) != _MOD_POWER_LEVEL:
            users[ghost_mxid] = _MOD_POWER_LEVEL
            changed = True

    if not changed:
        return
    try:
        await synapse.send_state_event(
            community_room.room_id, "m.room.power_levels", "", {**power_levels, "users": users},
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.warning("Could not update moderator power levels in %s", community_room.room_id, exc_info=True)


async def maybe_resync_community(request: Request, *, room_id: str, reason: str, bypass_debounce: bool = False) -> bool:
    """Returns True if ``room_id`` was a mirrored Lemmy community (resynced
    or not), False if it wasn't one at all. Debounced (see
    ``bridge.config.BridgeSection.lemmy_community_resync_min_interval_seconds``)
    for anything but a manual ``;refresh`` -- ``reason`` is purely for
    logging. Always serialized per-room via ``activity_lock`` (reused
    directly, keyed under a namespaced prefix) so two triggers landing at
    the same moment (e.g. a mod-list Add immediately followed by an Update)
    never run the actual resync concurrently."""
    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room_by_room_id(room_id)
    if community_room is None:
        return False

    config = request.app.state.config
    if not bypass_debounce:
        last = _last_resync_at.get(room_id)
        if last is not None and time.monotonic() - last < config.bridge.lemmy_community_resync_min_interval_seconds:
            logger.debug("Skipping debounced Lemmy community resync for %s (reason=%s)", room_id, reason)
            return True

    from bridge.note_mirroring import activity_lock

    async with activity_lock(f"lemmy-resync:{room_id}"):
        # Re-check inside the lock: another waiter may have just finished
        # a resync for this exact room while we were waiting our turn.
        if not bypass_debounce:
            last = _last_resync_at.get(room_id)
            if last is not None and time.monotonic() - last < config.bridge.lemmy_community_resync_min_interval_seconds:
                return True
        await _resync_community_locked(request, community_room, reason=reason)
        _last_resync_at[room_id] = time.monotonic()
    return True


async def _resync_community_locked(request: Request, community_room: LemmyCommunityRoom, *, reason: str) -> None:
    logger.info("Resyncing Lemmy community room %s (reason=%s)", community_room.room_id, reason)
    try:
        actor_doc = await fetch_actor(request, community_room.community_actor_id)
    except RemoteActorFetchError:
        logger.info("Could not re-fetch community %s for resync", community_room.community_actor_id, exc_info=True)
        return

    await _apply_community_actor_changes(request, community_room, actor_doc)
    refreshed = await request.app.state.repository.get_lemmy_community_room(community_room.community_actor_id)
    if refreshed is not None:
        await sync_community_moderators(request, refreshed, actor_doc=actor_doc)


async def _apply_community_actor_changes(request: Request, community_room: LemmyCommunityRoom, actor_doc: dict) -> None:
    new_name = actor_doc.get("name") or actor_doc.get("preferredUsername") or community_room.display_name
    new_icon_url = extract_icon_url(actor_doc)
    new_banner_url = extract_banner_url(actor_doc)
    moderators_candidate = actor_doc.get("attributedTo")
    new_moderators_url = moderators_candidate if isinstance(moderators_candidate, str) else community_room.moderators_url

    name_changed = new_name != community_room.display_name
    icon_changed = new_icon_url != community_room.icon_url
    banner_changed = new_banner_url != community_room.banner_url

    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    if name_changed or icon_changed:
        avatar_mxc = await fetch_and_upload_media_if_any(request, new_icon_url) if icon_changed else None
        try:
            if name_changed:
                await synapse.send_state_event(
                    community_room.room_id, "m.room.name", "", {"name": new_name}, as_user_id=bot_mxid
                )
            if icon_changed and avatar_mxc:
                await synapse.send_state_event(
                    community_room.room_id, "m.room.avatar", "", {"url": avatar_mxc}, as_user_id=bot_mxid
                )
        except SynapseError:
            logger.warning("Could not apply profile changes to community room %s", community_room.room_id, exc_info=True)

    if banner_changed and new_banner_url:
        from bridge.note_mirroring import set_ghost_room_banner

        await set_ghost_room_banner(
            request, room_id=community_room.room_id, ghost_user_id=bot_mxid, banner_mxc=new_banner_url,
        )

    await request.app.state.repository.register_lemmy_community_room(
        replace(
            community_room, display_name=new_name, icon_url=new_icon_url, banner_url=new_banner_url,
            moderators_url=new_moderators_url,
        )
    )


# -- inbound: Announce fan-out (posts/comments) ------------------------------


async def maybe_handle_community_announce(request: Request, activity: Activity) -> bool:
    """Returns True if ``activity`` was a mirrored community's own
    ``Announce`` fan-out of a post/comment (handled, successfully mirrored
    or not). Callers (``bridge.inbox_dispatch._handle_announce_locked``)
    must check this before their own repost-reaction logic runs, same
    reasoning as ``bridge.channel_bridge.maybe_handle_channel_message``."""
    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room(activity.actor)
    if community_room is None:
        return False

    from bridge.inbox_dispatch import _note_author, _resolve_object

    obj = await _resolve_object(request, activity.object)
    if obj is None:
        return True
    obj_type = obj.get("type")
    if obj_type not in ("Page", "Note"):
        return True  # recognized as community fan-out, nothing renderable in it

    author_actor_id = _note_author(obj)
    if not isinstance(author_actor_id, str):
        return True

    ap_object_id = obj.get("id")
    if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
        return True  # already mirrored -- e.g. a redelivered transaction

    if obj_type == "Page":
        await import_lemmy_page(request, page=obj, author_actor_id=author_actor_id, community_room=community_room)
        return True

    # A Note (comment): thread it under its parent if we have one tracked,
    # otherwise fall back to a plain unthreaded message in the community
    # room -- same accepted limitation as ordinary replies arriving before
    # their parent is mirrored (see project_reply_ordering_race), not worth
    # building ancestor-chain-walking infrastructure for here.
    in_reply_to = obj.get("inReplyTo")
    parent = await repository.get_federated_event_by_ap_object(in_reply_to) if isinstance(in_reply_to, str) else None
    if parent is not None and parent.room_id == community_room.room_id:
        from bridge.inbox_dispatch import _mirror_note_as_reply

        await _mirror_note_as_reply(request, obj, parent, author_actor_id)
        return True

    await _import_unthreaded_comment(request, note=obj, author_actor_id=author_actor_id, community_room=community_room)
    return True


def _page_attachments(page: dict) -> list[dict]:
    """Normalize a Lemmy ``Page``'s own media into
    ``attach_media_to_content``'s ``{"url", "media_type", "name"}`` shape
    -- deliberately NOT ``bridge.activitypub.remote_actor.extract_attachments``
    (a generic, cross-protocol helper): confirmed against join-lemmy.org's
    own federation docs, Lemmy's ``attachment`` entries are AS2 ``Link``
    objects whose URL lives in ``href`` (not ``url``, what that generic
    helper reads) and routinely carry no ``mediaType`` at all, which left a
    real post's image silently downgraded to a generic ``m.file`` instead
    of an inline ``m.image`` (confirmed live 2026-10-19). Lemmy's own
    separate ``image`` field -- an AS2 ``Image`` object, present only when
    the post's link actually points at an image -- is the authoritative
    "this is an image" signal and is preferred when present; ``attachment``
    is only the fallback for a non-image link post. Either way, a missing
    ``mediaType`` is guessed from the URL's own extension rather than
    defaulting to ``application/octet-stream``, which would otherwise force
    ``m.file`` regardless of what this function already knows the media
    actually is."""
    image = page.get("image")
    if isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, list):
            url = url[0] if url else None
        if isinstance(url, dict):
            url = url.get("href") or url.get("url")
        if isinstance(url, str) and url:
            media_type = image.get("mediaType") or mimetypes.guess_type(url)[0] or "image/jpeg"
            return [{"url": url, "media_type": media_type, "name": ""}]

    raw = page.get("attachment")
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("href") or item.get("url")
        if isinstance(url, list):
            url = url[0] if url else None
        if isinstance(url, dict):
            url = url.get("href") or url.get("url")
        if not isinstance(url, str) or not url:
            continue
        media_type = item.get("mediaType") or mimetypes.guess_type(url)[0] or "application/octet-stream"
        results.append({"url": url, "media_type": media_type, "name": item.get("name") or ""})
    return results


async def import_lemmy_page(
    request: Request, *, page: dict, author_actor_id: str, community_room: LemmyCommunityRoom,
) -> FederatedEvent | None:
    """Mirror an inbound Lemmy ``Page`` (post) into ``community_room`` --
    the ``Page`` counterpart of ``bridge.note_mirroring.import_video``,
    whose "bold heading + body underneath" title convention this reuses
    (no new MSC field invented for the title -- see this feature's own
    design discussion), minus import_video's own emoji prefix (removed per
    explicit user request 2026-10-19 -- a title alone is already visually
    distinct as a bold first line)."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    resolved = await resolve_and_invite_ghost(request, author_actor_id, community_room.room_id)
    if resolved is None:
        logger.info("Could not resolve a ghost for Lemmy post from %s; dropping", author_actor_id)
        return None
    ghost_mxid, _actor_doc = resolved
    try:
        await synapse.join_room(community_room.room_id, as_user_id=ghost_mxid)
    except SynapseError:
        logger.info("Could not join post ghost %s into %s", ghost_mxid, community_room.room_id, exc_info=True)
        return None

    repository_known = await repository.is_channel_member_known(community_room.room_id, author_actor_id)
    if not repository_known:
        await repository.record_channel_member(community_room.room_id, author_actor_id)

    title = (page.get("name") or "").strip() or "Untitled post"
    mentions = await resolve_mention_pills(request, room_id=community_room.room_id, note=page)
    plain_body, safe_html = strip_to_matrix_message(
        page.get("content") or "", mention_pills=mentions.pills, media_type=page.get("mediaType"),
    )

    plain = title + (f"\n\n{plain_body}" if plain_body else "")
    import html as html_module

    formatted = f"<p><strong>{html_module.escape(title)}</strong></p>"
    if safe_html:
        formatted += safe_html

    message_content: dict = {"msgtype": "m.text", "body": plain}
    page_url = page.get("url") if isinstance(page.get("url"), str) else None
    src = source_post_url(page) or page_url
    if src:
        # Discreet "view original" affordance only -- no visible link text
        # in body/formatted_body (removed per explicit user request: a
        # real client already renders its own link-preview card from the
        # post's own attached image/link, making a second textual "View on
        # <host>" line redundant).
        message_content["external_url"] = src
    message_content["body"] = plain
    message_content["format"] = "org.matrix.custom.html"
    message_content["formatted_body"] = formatted
    if mentions.mentioned_locals:
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}

    attachments = _page_attachments(page)
    message_content, _ = await attach_media_to_content(request, message_content, attachments)

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            community_room.room_id, message_content, as_user_id=ghost_mxid,
            event_type=mirrored_post_event_type(request.app.state.config),
            ts=resolve_event_ts(
                page, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror Lemmy post from %s into %s", author_actor_id, community_room.room_id, exc_info=True)
        return None

    federated_event = None
    ap_object_id = page.get("id")
    if isinstance(ap_object_id, str):
        federated_event = FederatedEvent(
            event_id=event_id, room_id=community_room.room_id, ap_object_id=ap_object_id,
            author_actor_id=author_actor_id,
        )
        await repository.record_federated_event(federated_event)

    await notify_mentioned_locals(
        request, mentioned=mentions.mentioned_locals, room_id=community_room.room_id,
        event_id=event_id, author_actor_id=author_actor_id,
    )
    return federated_event


async def _import_unthreaded_comment(
    request: Request, *, note: dict, author_actor_id: str, community_room: LemmyCommunityRoom,
) -> None:
    """A comment whose parent (post or comment) isn't tracked yet -- mirror
    it as a plain, unthreaded message rather than dropping it, same
    reduced-fidelity fallback other importers use for the same race."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    resolved = await resolve_and_invite_ghost(request, author_actor_id, community_room.room_id)
    if resolved is None:
        return
    ghost_mxid, _actor_doc = resolved
    try:
        await synapse.join_room(community_room.room_id, as_user_id=ghost_mxid)
    except SynapseError:
        return

    mentions = await resolve_mention_pills(request, room_id=community_room.room_id, note=note)
    plain, safe_html = strip_to_matrix_message(
        note.get("content") or "", mention_pills=mentions.pills, media_type=note.get("mediaType"),
    )
    if not plain and not safe_html:
        return
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            community_room.room_id, message_content, as_user_id=ghost_mxid,
            ts=resolve_event_ts(
                note, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror unthreaded Lemmy comment into %s", community_room.room_id, exc_info=True)
        return

    ap_object_id = note.get("id")
    if isinstance(ap_object_id, str):
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id, room_id=community_room.room_id, ap_object_id=ap_object_id,
                author_actor_id=author_actor_id,
            )
        )


# -- backfill -----------------------------------------------------------


def _page_and_author_from_item(item: dict) -> tuple[dict, str] | None:
    """Normalize a raw Lemmy outbox item into ``(page, author_actor_id)``.
    A community's own outbox entries are an ``Announce`` wrapping the
    original ``Create<Page>`` -- confirmed live 2026-10-20 against a real
    community (sh.itjust.works/c/funny): the outbox records the
    COMMUNITY's own fan-out activity, not the post's own Create directly,
    which previously made every single real-world backfill item fail to
    parse (``;backfill`` reporting "0 post(s) mirrored... N couldn't be
    mirrored" for every item). Unwrapped here (one level only -- Lemmy
    doesn't nest any deeper) before falling through to the bare
    ``Create<Page>``/``Page`` handling, which stays as a fallback for any
    instance that serializes its outbox differently. Lemmy always embeds
    its ``Create``'s own object (confirmed against join-lemmy.org's own
    federation docs) -- no GoToSocial-style bare-IRI handling needed the
    way ``bridge.commands._note_from_item`` has for ordinary accounts
    (which also doesn't accept ``Page`` at all -- that function is shared
    with the generic ``_handle_create`` path, which has no idea what to do
    with one)."""
    if item.get("type") == "Announce":
        inner = item.get("object")
        if not isinstance(inner, dict):
            return None
        item = inner

    if item.get("type") == "Create":
        obj = item.get("object")
        actor = item.get("actor")
    elif item.get("type") == "Page":
        obj = item
        actor = None
    else:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "Page":
        return None
    author = obj.get("attributedTo") or actor
    return (obj, author) if isinstance(author, str) else None


async def backfill_community_posts(
    request: Request, *, community_room: LemmyCommunityRoom, count: int,
) -> tuple[int, int, int]:
    """Pull the community's own outbox -- its latest posts -- the same way
    ``;backfill``/the first-follow auto-backfill does for an ordinary
    account (``bridge.commands._resolve_backfill_source``'s outbox branch),
    just landing each post in the shared community room via
    ``import_lemmy_page`` instead of a 1:1 Remote User Room. Returns
    ``(imported, already, failed)``."""
    try:
        actor_doc = await fetch_actor(request, community_room.community_actor_id)
    except RemoteActorFetchError:
        return 0, 0, 0
    outbox = actor_doc.get("outbox")
    if not outbox:
        return 0, 0, 0

    from bridge.commands import _collect_recent_items  # deferred: see sync_community_moderators

    raw_items = await _collect_recent_items(request, outbox, limit=count)
    repository = request.app.state.repository

    imported = already = failed = 0
    for item in reversed(raw_items):  # oldest of the collected batch first
        parsed = _page_and_author_from_item(item)
        if parsed is None:
            failed += 1
            continue
        page, author_actor_id = parsed
        ap_object_id = page.get("id")
        if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
            already += 1
            continue
        try:
            result = await import_lemmy_page(
                request, page=page, author_actor_id=author_actor_id, community_room=community_room,
            )
        except Exception:
            logger.warning("Community backfill failed to mirror %s", ap_object_id, exc_info=True)
            result = None
        if result is not None:
            imported += 1
        else:
            failed += 1
    return imported, already, failed


async def backfill_community_thread(
    request: Request, *, community_room: LemmyCommunityRoom, root_event: FederatedEvent, count: int,
) -> tuple[int, int, int]:
    """Pull one specific mirrored post/comment's own ``replies``
    collection -- same thread-mode backfill
    ``bridge.commands._resolve_backfill_source`` already does for an
    ordinary account's thread (including its Mastodon-API discovery
    fallback for Pleroma/Akkoma's known-unreliable ``replies`` collection),
    just landing each comment in the shared community room (via
    ``_mirror_note_as_reply``) instead of a 1:1 Remote User Room. Returns
    ``(imported, already, failed)``."""
    try:
        root_note = await fetch_actor(request, root_event.ap_object_id)
    except RemoteActorFetchError:
        return 0, 0, 0
    replies = root_note.get("replies")
    if not replies:
        return 0, 0, 0

    from bridge.commands import _collect_recent_items, _discover_mastodon_api_reply_notes, _item_ap_object_id, _note_from_item
    from bridge.inbox_dispatch import _mirror_note_as_reply

    raw_items = await _collect_recent_items(request, replies, limit=count)
    if len(raw_items) < count:
        already_seen = {aid for item in raw_items if (aid := _item_ap_object_id(item))}
        raw_items = raw_items + await _discover_mastodon_api_reply_notes(
            request, root_ap_object_id=root_event.ap_object_id, limit=count - len(raw_items),
            already_seen=already_seen,
        )

    repository = request.app.state.repository
    imported = already = failed = 0
    for item in reversed(raw_items):  # oldest of the collected batch first
        activity_json = await _note_from_item(request, item, fallback_author=None)
        note = activity_json.get("object") if activity_json else None
        author_actor_id = activity_json.get("actor") if activity_json else None
        if not isinstance(note, dict) or not isinstance(author_actor_id, str):
            failed += 1
            continue
        ap_object_id = note.get("id")
        if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
            already += 1
            continue
        parent = None
        in_reply_to = note.get("inReplyTo")
        if isinstance(in_reply_to, str):
            parent = await repository.get_federated_event_by_ap_object(in_reply_to)
        if parent is None or parent.room_id != community_room.room_id:
            parent = root_event  # ancestor untracked -- flatten directly under the backfilled root
        try:
            result = await _mirror_note_as_reply(request, note, parent, author_actor_id)
        except Exception:
            logger.warning("Community thread backfill failed to mirror %s", ap_object_id, exc_info=True)
            result = None
        if result is not None:
            imported += 1
        else:
            failed += 1
    return imported, already, failed


# -- inbound: community Update / moderators Add/Remove -----------------------


async def maybe_handle_community_actor_update(request: Request, obj: dict) -> bool:
    """Returns True if ``obj`` (the object of an inbound ``Update``) was a
    mirrored community's own Actor document (handled, synced or not).
    Called from ``bridge.inbox_dispatch._handle_update``."""
    actor_id = obj.get("id")
    if not isinstance(actor_id, str):
        return False
    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room(actor_id)
    if community_room is None:
        return False
    await maybe_resync_community(request, room_id=community_room.room_id, reason="update")
    return True


async def maybe_handle_community_mod_list_change(request: Request, activity: Activity, *, added: bool) -> bool:
    """Returns True if ``activity`` was an Add/Remove targeting a mirrored
    community's own ``moderators`` collection (handled either way). Called
    from ``bridge.inbox_dispatch``'s generalized pin/mod Add-Remove
    dispatch. Applies the single change immediately (so the affected mod
    doesn't wait on the debounce window) and then also requests a full
    resync (debounced) per the user's own request, to catch anything a
    single Add/Remove might have missed."""
    target = activity.target_id()
    if not target or "moderators" not in target.lower():
        return False

    repository = request.app.state.repository
    community_room = await _resolve_community_room_from_target(request, target)
    if community_room is None:
        return False

    object_id = activity.object_id()
    if isinstance(object_id, str):
        if added:
            resolved = await resolve_and_invite_ghost(request, object_id, community_room.room_id)
            if resolved is not None:
                ghost_mxid, _ = resolved
                await _grant_power_level(request, community_room.room_id, ghost_mxid, _MOD_POWER_LEVEL)
        else:
            ghost_profile = await repository.get_ghost_profile(object_id)
            if ghost_profile is not None and ghost_profile.mxid is not None:
                await _grant_power_level(request, community_room.room_id, ghost_profile.mxid, None)

    await maybe_resync_community(request, room_id=community_room.room_id, reason="mod_change")
    return True


async def _resolve_community_room_from_target(request: Request, target: str) -> LemmyCommunityRoom | None:
    """``target`` (an Add/Remove's own ``target`` field) names a community's
    ``moderators`` collection URL, not the community's own actor id --
    strip it back down to find which community room (if any) this is."""
    repository = request.app.state.repository
    # The community actor id is everything before "/moderators" for every
    # real Lemmy deployment confirmed so far (join-lemmy.org's own
    # federation docs) -- best-effort text match, not a hard protocol
    # guarantee, so this falls back to a live fetch+compare if it doesn't
    # immediately resolve.
    candidate = target.rsplit("/moderators", 1)[0]
    community_room = await repository.get_lemmy_community_room(candidate)
    if community_room is not None:
        return community_room
    try:
        target_doc = await fetch_actor(request, target)
    except RemoteActorFetchError:
        return None
    attributed_to = target_doc.get("attributedTo")
    if isinstance(attributed_to, str):
        return await repository.get_lemmy_community_room(attributed_to)
    return None


async def _grant_power_level(request: Request, room_id: str, mxid: str, level: int | None) -> None:
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        power_levels = await synapse.get_room_state(room_id, "m.room.power_levels", "", as_user_id=bot_mxid)
    except SynapseError:
        return
    users = dict(power_levels.get("users") or {})
    if level is None:
        if mxid not in users:
            return
        del users[mxid]
    else:
        if users.get(mxid) == level:
            return
        users[mxid] = level
    try:
        await synapse.send_state_event(
            room_id, "m.room.power_levels", "", {**power_levels, "users": users}, as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.warning("Could not update power level for %s in %s", mxid, room_id, exc_info=True)


# -- outbound: Matrix member replying in a mirrored community room ----------


async def maybe_distribute_community_message(request: Request, event: dict) -> bool:
    """Returns True if ``event`` belonged to a mirrored Lemmy community room
    (handled, distributed or not). Only a genuine reply (to a tracked post
    or comment in this exact room) is sent on, as a plain ``Create<Note>``
    addressed directly to the community -- never an ``Announce`` (that
    fan-out is only ever the community's own home instance's job, same
    reasoning as ``bridge.channel_bridge.maybe_distribute_channel_message``).
    A bare top-level message gets an explanatory notice instead of being
    silently dropped or mis-sent as a titleless ``Page``."""
    if event.get("type") != "m.room.message":
        return False

    from bridge.commands import message_addresses_bot

    content = event.get("content") or {}
    config = request.app.state.config
    if message_addresses_bot(content, config):
        return True

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room_by_room_id(room_id)
    if community_room is None:
        return False

    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return True  # our own bot's/a ghost's echo of a mirrored post -- never re-federate

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return True  # no linked fediverse profile -- stays Matrix-only

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed

    from bridge.reply_bridge import _extract_reply_target_event_id

    target_event_id = _extract_reply_target_event_id(content)
    parent = (
        await repository.get_federated_event_by_matrix_event(target_event_id) if target_event_id else None
    )
    if parent is None or parent.room_id != room_id:
        from bridge.commands import _notice

        await _notice(
            request, room_id,
            "Starting a brand new post isn't supported here yet -- reply to an existing post/comment to "
            "send a comment.",
        )
        return True

    body = (content.get("body") or "").strip()
    if not body:
        return False

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    note_id = f"{own_actor_id}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    content_html = plain_text_to_note_html(body)
    note = Note(
        id=note_id, attributed_to=own_actor_id, content=content_html, published=published,
        to=[AS_PUBLIC], cc=[community_room.community_actor_id, followers_url(base, actor_record.username)],
        in_reply_to=parent.ap_object_id, audience=community_room.community_actor_id,
        tag=[{"type": "Mention", "href": community_room.community_actor_id}],
    )

    inbox = await resolve_actor_inbox(request, community_room.community_actor_id)
    if inbox is None:
        return True

    create_activity = Activity(
        id=f"{own_actor_id}/activities/{uuid.uuid4().hex}", type="Create", actor=own_actor_id,
        object=note.to_dict(), to=note.to, cc=note.cc, published=published,
    )
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver comment to community %s", community_room.community_actor_id, exc_info=True)
        return True

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id, room_id=room_id, ap_object_id=note_id, author_actor_id=own_actor_id,
            thread_root_event_id=parent.thread_root_event_id or parent.event_id,
        )
    )
    return True


# -- outbound: a Matrix mod banning a ghost ----------------------------------


async def maybe_handle_community_ban(request: Request, event: dict) -> bool:
    """Returns True if this event was a ban in a mirrored Lemmy community
    room (handled, delivered or not). Synapse enforces the ``ban`` power
    level before this event ever reaches the AppService, so by the time
    we're here the sender's authority has already been checked on the
    Matrix side -- see this module's own docstring."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    if content.get("membership") != "ban":
        return False

    room_id = event.get("room_id", "")
    banned_mxid = event.get("state_key", "")
    sender = event.get("sender", "")
    if not room_id or not banned_mxid or not sender:
        return False

    repository = request.app.state.repository
    community_room = await repository.get_lemmy_community_room_by_room_id(room_id)
    if community_room is None:
        return False

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return True  # no local AP identity to sign a Block with

    ghost_profile = await repository.get_ghost_profile_by_mxid(banned_mxid)
    if ghost_profile is None:
        return True  # not a ghost at all (e.g. banning another Matrix user) -- nothing to federate

    config = request.app.state.config
    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    block_activity = Activity(
        id=f"{own_actor_id}/blocks/{uuid.uuid4().hex}", type="Block", actor=own_actor_id,
        object=ghost_profile.actor_id, target=community_room.community_actor_id,
    )

    inbox = await resolve_actor_inbox(request, community_room.community_actor_id)
    if inbox is None:
        return True
    http_client = request.app.state.http_client
    try:
        await deliver_activity(
            http_client, inbox_url=inbox, activity=block_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning(
            "Failed to deliver Block(%s) to community %s", ghost_profile.actor_id,
            community_room.community_actor_id, exc_info=True,
        )
    return True
