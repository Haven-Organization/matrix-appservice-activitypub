"""Shoot Guild Channel bridging.

A Channel (a Shoot ``Group`` actor, living inside a joined Guild --
``Organization`` actor, see ``bridge.commands._handle_joinguild``) mirrors to
its own Matrix room, added as a child of its guild's Space
(``bridge.spaces.ensure_guild_space``) -- unlike a Remote User Room (one
remote actor, one Matrix room), a Channel room has MANY different remote
member ghosts speaking in it, resolved per-message rather than fixed to the
room itself.

``ensure_channel_room`` is called both eagerly, right after a guild join is
Accepted (``bridge.inbox_dispatch._handle_guild_accept``, for every channel
already cached in ``guild_channels`` -- so a joined guild's Space shows its
channels immediately, not just as messages happen to arrive in them) and
lazily, by ``maybe_handle_channel_message`` below, for a channel that somehow
wasn't cached yet -- either way, get-or-create, safe to call repeatedly.

Message delivery (confirmed live 2026-07-14 against Shoot's own reference
deployment, chat.understars.dev) is ASYMMETRIC, and this bridge is only ever
on one side of it: Shoot itself always owns the Channel actor (we never do),
so an inbound channel message always arrives as an ``Announce`` whose
``actor`` IS the Channel -- fanned out by Shoot to each individual guild
member's own inbox, not delivered to some inbox of ours the Channel doesn't
have -- wrapping a bare-IRI reference to the real ``Note``, whose own
``attributedTo`` names the actual Shoot member who wrote it (NOT the
Channel). Outbound is the mirror image: we're always the non-owning side, so
a message we send is always a plain ``Create<Note>`` addressed directly to
the Channel's own inbox, never an ``Announce`` (that fan-out computation is
only ever the OWNING/home server's job). See ``maybe_handle_channel_message``/
``maybe_distribute_channel_message`` for each direction.

New channels created in a guild AFTER it was joined are invisible to Shoot's
own federation entirely -- confirmed by reading ``createGuildTextChannel``
in Shoot's own source (``src/util/entity/channel.ts``): it only ever emits
an internal ``CHANNEL_CREATE`` gateway event to Shoot's own native clients,
nothing crosses the ActivityPub boundary at all. So there's no event this
bridge could listen for either way -- ``_resolve_guild_channel`` below
instead re-syncs a guild's whole channel list (``sync_guild_channels``) the
moment a message arrives from a channel actor it doesn't recognize yet,
which self-heals the common case (a new channel gets its room the moment
anyone actually posts in it) with no extra polling infrastructure. An empty
new channel nobody's posted in yet stays invisible until then -- the
";refresh guild" command (``bridge.commands._handle_refresh_guild``) is the
manual escape hatch for that case.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import Activity, Note
from bridge.activitypub.nodeinfo import remote_software_name
from bridge.activitypub.remote_actor import RemoteActorFetchError, fetch_actor
from bridge.activitypub.sanitize import plain_text_to_note_html, strip_to_matrix_message
from bridge.activitypub.urls import actor_url, followers_url, main_key_id
from bridge.note_mirroring import resolve_and_invite_ghost, resolve_event_ts, resolve_mention_pills
from bridge.repository import ChannelRoom, FederatedEvent
from bridge.room_widget import add_bridge_widget
from bridge.spaces import add_channel_room_to_guild_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


async def ensure_channel_room(request: Request, *, channel_actor_id: str, guild_actor_id: str) -> ChannelRoom | None:
    """Get-or-create the Matrix room mirroring a Shoot Channel: bot-created/
    owned (not a ghost -- a channel has many different member-ghost
    speakers, not one, unlike a Remote User Room), added as a child of its
    guild's Space, with every CURRENT local member of that guild invited
    (``guild_memberships``' own small list -- not Shoot's guild membership,
    which would need parsing its custom ``Role`` objects, deliberately
    never done here or anywhere else in this feature -- see this module's
    own docstring). Returns the existing record unchanged if the room
    already exists. Best-effort, same as the rest of the bridge's room
    bookkeeping -- returns None only if room creation itself failed.
    """
    repository = request.app.state.repository
    existing = await repository.get_channel_room(channel_actor_id)
    if existing is not None:
        return existing

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    try:
        channel_doc = await fetch_actor(request, channel_actor_id)
    except RemoteActorFetchError:
        channel_doc = {}
    display_name = channel_doc.get("name") or channel_doc.get("preferredUsername") or "channel"

    # list_guild_members returns local actor USERNAMES (guild_memberships'
    # own key), not full Matrix user IDs -- create_room's invite= needs the
    # latter.
    member_usernames = await repository.list_guild_members(guild_actor_id)
    invite_mxids = []
    for member_username in member_usernames:
        actor_record = await repository.get_local_actor(member_username)
        if actor_record is not None:
            invite_mxids.append(actor_record.matrix_user_id)

    try:
        room_id = await request.app.state.synapse.create_room(
            as_user_id=bot_mxid,
            name=display_name,
            invite=invite_mxids,
        )
    except SynapseError:
        logger.warning("Could not create channel room for %s", channel_actor_id, exc_info=True)
        return None

    record = ChannelRoom(
        channel_actor_id=channel_actor_id, room_id=room_id, guild_actor_id=guild_actor_id,
        display_name=display_name,
    )
    await repository.register_channel_room(record)
    await add_channel_room_to_guild_space(request, guild_actor_id=guild_actor_id, child_room_id=room_id)
    await add_bridge_widget(request, room_id=room_id)
    return record


async def sync_guild_channels(request: Request, guild_actor_id: str) -> list[tuple[str, str]]:
    """Re-fetch ``guild_actor_id``'s own ``channels`` collection fresh and
    re-cache it (``record_guild_channels`` REPLACES the whole cached list
    for this guild -- see its own docstring -- so this always re-fetches
    the complete set, never patches in just one new entry), then
    get-or-creates a Matrix room for every channel in it -- ``ensure_channel_room``
    is already idempotent, so this is safe to call as often as needed: once
    right after a guild join (``bridge.inbox_dispatch._handle_guild_accept``,
    which duplicates this same fetch-and-cache logic inline rather than
    calling this, since it also needs the guild's ``name`` for its own
    notice and predates this function), lazily whenever an inbound channel
    message names a channel actor that isn't cached yet (see
    ``_resolve_guild_channel``), and from the ``;refresh guild`` command
    (``bridge.commands._handle_refresh_guild``). Returns the fresh
    ``(channel_actor_id, name)`` list, or an empty one if the guild couldn't
    be fetched at all."""
    repository = request.app.state.repository
    try:
        guild_doc = await fetch_actor(request, guild_actor_id)
    except RemoteActorFetchError:
        return []
    channels_url = guild_doc.get("channels")
    if not channels_url:
        return []

    # Deferred: bridge.commands already imports plenty from feature modules
    # like this one, so importing _collect_recent_items from there at
    # module level here would be circular -- same reasoning
    # bridge.inbox_dispatch._handle_guild_accept's own identical deferred
    # import gives.
    from bridge.commands import _collect_recent_items

    items = await _collect_recent_items(request, channels_url, limit=200)
    channels = [
        (item["id"], item.get("name") or item.get("preferredUsername") or "")
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    if not channels:
        return []

    await repository.record_guild_channels(guild_actor_id, channels)
    for channel_actor_id, _name in channels:
        await ensure_channel_room(request, channel_actor_id=channel_actor_id, guild_actor_id=guild_actor_id)
    return channels


async def _resolve_guild_channel(request: Request, actor_id: str) -> str | None:
    """Whether ``actor_id`` is a Shoot Channel belonging to a guild this
    bridge has joined -- the cached ``guild_channels`` table first (the
    common case, no network round trip), falling back to a live fetch +
    guild-membership check for a channel created AFTER its guild was
    joined, never cached at join time (see this module's own docstring on
    why there's no way to be told about one instead). A confirmed match
    triggers ``sync_guild_channels`` to re-cache the guild's WHOLE current
    channel list (not just this one entry -- see that function's own
    docstring for why), so a guild with several uncached new channels
    self-heals all of them from a single message in just one of them, not
    just the one that happened to trigger this."""
    repository = request.app.state.repository
    existing = await repository.get_guild_channel(actor_id)
    if existing is not None:
        return existing.guild_actor_id

    try:
        channel_doc = await fetch_actor(request, actor_id)
    except RemoteActorFetchError:
        return None
    if channel_doc.get("type") != "Group":
        return None

    guild_actor_id = channel_doc.get("context")
    if isinstance(guild_actor_id, dict):
        guild_actor_id = guild_actor_id.get("id")
    if not isinstance(guild_actor_id, str):
        # context is FEP-7888's own field for this (see this module's
        # docstring) and is what Shoot itself actually sets -- attributedTo
        # is only a fallback for a differently-behaved implementation that
        # uses it for containment instead (see buildAPActor's own split
        # between the two, confirmed live 2026-07-14 against Shoot itself
        # using context, not attributedTo, for a Channel's own guild link).
        attributed = channel_doc.get("attributedTo")
        guild_actor_id = attributed.get("id") if isinstance(attributed, dict) else attributed
    if not isinstance(guild_actor_id, str) or not await repository.is_guild_member(guild_actor_id):
        return None

    await sync_guild_channels(request, guild_actor_id)
    return guild_actor_id


async def maybe_handle_channel_message(request: Request, activity: Activity) -> bool:
    """Returns True if ``activity`` was Shoot's guild-Channel fan-out of an
    ordinary channel message (handled, successfully mirrored or not) --
    ``activity.actor`` IS the Channel itself, announcing on behalf of
    whichever guild member actually wrote it (see this module's own
    docstring on Shoot's asymmetric delivery split). Callers
    (``bridge.inbox_dispatch._handle_announce_locked``) MUST check this
    before their own repost-reaction logic runs -- a Channel fan-out is the
    same activity type (``Announce``) as an ordinary repost and would
    otherwise be misread as one, wrongly notifying/reacting as if the
    CHANNEL itself had reposted something."""
    guild_actor_id = await _resolve_guild_channel(request, activity.actor)
    if guild_actor_id is None:
        return False

    from bridge.inbox_dispatch import _note_author, _resolve_object

    obj = await _resolve_object(request, activity.object)
    if obj is None or obj.get("type") != "Note":
        return True  # recognized as a channel fan-out, but nothing renderable in it

    author_actor_id = _note_author(obj)
    if not isinstance(author_actor_id, str):
        return True

    channel_room = await ensure_channel_room(request, channel_actor_id=activity.actor, guild_actor_id=guild_actor_id)
    if channel_room is None:
        return True

    ap_object_id = obj.get("id")
    repository = request.app.state.repository
    if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
        return True  # already mirrored -- e.g. a redelivered transaction

    resolved = await resolve_and_invite_ghost(request, author_actor_id, channel_room.room_id)
    if resolved is None:
        logger.info("Could not resolve a ghost for channel message from %s; dropping", author_actor_id)
        return True
    ghost_mxid, _actor_doc = resolved

    synapse = request.app.state.synapse
    if not await repository.is_channel_member_known(channel_room.room_id, author_actor_id):
        # About to post as this ghost right now -- can't wait on the usual
        # invite-then-async-auto-accept path (see resolve_and_invite_ghost's
        # own docstring), so force the join synchronously, same as every
        # other "post as a ghost immediately" call site in this bridge.
        try:
            await synapse.join_room(channel_room.room_id, as_user_id=ghost_mxid)
        except SynapseError:
            logger.info(
                "Could not join channel-message ghost %s into %s", ghost_mxid, channel_room.room_id, exc_info=True,
            )
            return True
        await repository.record_channel_member(channel_room.room_id, author_actor_id)

    mentions = await resolve_mention_pills(request, room_id=channel_room.room_id, note=obj)
    plain, safe_html = strip_to_matrix_message(obj.get("content") or "", mention_pills=mentions.pills)
    if not plain and not safe_html:
        return True

    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            channel_room.room_id, message_content, as_user_id=ghost_mxid,
            ts=resolve_event_ts(
                {"published": obj.get("published")}, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Could not mirror channel message into %s", channel_room.room_id, exc_info=True)
        return True

    if ap_object_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id, room_id=channel_room.room_id, ap_object_id=ap_object_id,
                author_actor_id=author_actor_id,
            )
        )
    return True


async def maybe_distribute_channel_message(request: Request, event: dict) -> bool:
    """Returns True if ``event`` belonged to a Channel room (handled,
    successfully distributed or not) -- callers shouldn't process it
    further. Delivered as a plain ``Create<Note>`` straight to the
    Channel's own inbox, addressed directly to it (plus the sender's own
    followers, matching the shape Shoot's own channel messages use) with
    FEP-7888 ``context`` set to the Channel's id -- NEVER an ``Announce``,
    since that fan-out computation is only ever the channel-OWNING
    (home) server's job, and we never own a Channel ourselves (see this
    module's own docstring)."""
    if event.get("type") != "m.room.message":
        return False

    # Deferred: bridge.commands already imports from this module (and, via
    # bridge.inbox_dispatch, ends up importing from here too), so importing
    # message_addresses_bot from there at module level would be circular --
    # same reasoning as this module's other deferred imports.
    from bridge.commands import message_addresses_bot

    content = event.get("content") or {}
    config = request.app.state.config
    if message_addresses_bot(content, config):
        # bridge.commands.maybe_handle_command runs earlier in the
        # dispatch chain and normally intercepts anything tagging the bot
        # before we're ever even reached -- this is a second, independent
        # guarantee of our own (same pattern
        # bridge.profile_posts.maybe_distribute_profile_post's identical
        # check gives Profile Rooms): a "; refresh guild" (or any other)
        # command must never be mistaken for real channel chat and
        # federated out to Shoot, full stop.
        return True

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    matrix_event_id = event.get("event_id")
    if not room_id or not sender or not matrix_event_id:
        return False

    repository = request.app.state.repository
    channel_room = await repository.get_channel_room_by_room_id(room_id)
    if channel_room is None:
        return False  # not a Channel room at all

    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return True  # our own bot's/a ghost's echo of a mirrored message -- never re-federate

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        return True  # a Matrix user with no linked fediverse profile -- stays Matrix-only

    if await repository.get_federated_event_by_matrix_event(matrix_event_id) is not None:
        return True  # already distributed (e.g. a redelivered transaction)

    body = (content.get("body") or "").strip()
    if not body:
        # No media/attachment support for Channel messages yet -- a
        # caption-less image, for instance, has nothing left to send once
        # body's own filename-only convention is excluded here the same
        # way bridge.profile_posts._maybe_repost_forwarded_post's sibling
        # checks exclude it elsewhere. Left as a known, accepted gap for
        # this first pass rather than silently mis-sending an empty Note.
        return False

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)
    note_id = f"{own_actor_id}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Same fix as bridge.reply_bridge._send_outbound_dm's identical one for
    # DMs (confirmed live 2026-07-14 there first, now here too): Shoot
    # stores/displays a message's `content` completely raw, no HTML parsing
    # at all, so the usual <p>...</p> wrapping every other outbound Note
    # gets showed up as literal, visible tag text in its UI. Every Channel
    # is inherently Shoot-owned (nothing else implements this actor shape
    # yet), but detecting via NodeInfo rather than hardcoding "always raw"
    # keeps this consistent with the DM case and correct if that ever
    # changes.
    channel_domain = urlsplit(channel_room.channel_actor_id).hostname or ""
    software = await remote_software_name(request, channel_domain) if channel_domain else None
    content_html = body if software == "shoot" else plain_text_to_note_html(body)

    note = Note(
        id=note_id,
        attributed_to=own_actor_id,
        content=content_html,
        published=published,
        to=[channel_room.channel_actor_id, followers_url(base, actor_record.username)],
        cc=[],
        context=channel_room.channel_actor_id,
    )
    create_activity = Activity(
        id=f"{note_id}/activity", type="Create", actor=own_actor_id, object=note,
        published=published, to=note.to, cc=note.cc,
    )

    await repository.record_federated_event(
        FederatedEvent(
            event_id=matrix_event_id, room_id=room_id, ap_object_id=note_id, author_actor_id=own_actor_id,
        )
    )

    try:
        channel_doc = await fetch_actor(request, channel_room.channel_actor_id)
    except RemoteActorFetchError:
        logger.warning(
            "Could not fetch channel actor %s to deliver message", channel_room.channel_actor_id, exc_info=True,
        )
        return True
    channel_inbox = channel_doc.get("inbox")
    if not channel_inbox:
        logger.warning("Channel actor %s has no inbox", channel_room.channel_actor_id)
        return True

    try:
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=channel_inbox,
            activity=create_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver channel message to %s", channel_inbox, exc_info=True)
    return True
