"""Shared primitives for mirroring an ActivityStreams Note into a Matrix
Remote User Room, tagging/room-shaping the rooms the bridge creates, and
pushing a local actor's own identity changes out over ActivityPub.

Used by both ``bridge.commands`` (the ``import`` command, the various
room-creation flows, and the ``banner`` command) and ``bridge.inbox_dispatch``
(mirroring a followed account's own posts/replies, and -- via ``import_note``
-- importing whatever a followed account boosts), and by
``bridge.profile_posts`` (keeping a local actor's name/bio/avatar in sync
with their Profile Room's own Matrix state via ``push_profile_update``).
Lives here, in none of those, because ``commands`` and ``inbox_dispatch``
already depend on each other in one direction (``commands`` uses several of
``inbox_dispatch``'s reply-threading helpers) and ``profile_posts`` depends
on ``commands`` (for ``message_addresses_bot``) -- so anything needed by
more than one of them has to live somewhere none of them is, to avoid an
import cycle.
"""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import AS_PUBLIC, Activity, Actor, PublicKey
from bridge.activitypub.remote_actor import (
    RemoteActorFetchError,
    extract_attachments,
    extract_banner_url,
    extract_icon_url,
    fetch_actor,
    resolve_actor_inbox,
)
from bridge.activitypub.sanitize import mention_pill_key, plain_text_to_note_html, strip_to_matrix_message
from bridge.custom_emoji import inline_custom_emoji, resolve_and_persist_emoji
from bridge.activitypub.urls import (
    actor_url,
    followers_url,
    following_url,
    inbox_url,
    main_key_id,
    outbox_url,
    shared_inbox_url,
    username_from_actor_url,
)
from bridge.ghosts import ensure_ghost_user, ghost_localpart, ghost_mxid, sync_ghost_profile
from bridge.matrix_links import matrix_to_link
from bridge.media import (
    fetch_and_upload_media,
    fetch_and_upload_media_with_dimensions,
    filename_with_extension,
    matrix_msgtype_for_mimetype,
)
from bridge.notifications import notification_actor_html, notify_user
from bridge.repository import ActorRecord, ActorRepository, FederatedEvent, GhostProfile, RemoteActorRoom
from bridge.room_widget import add_bridge_widget
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# MSC4501 ("Rooms as Social Media Pages"), spiritual successor to the
# abandoned MSC3639:
# https://github.com/matrix-org/matrix-spec-proposals/pull/4501
# Set on every room the bridge itself creates to represent an ActivityPub
# profile -- a Remote User Room mirroring someone else's, or a local user's
# own via `create profile`. A room's type is immutable once created, so an
# already-existing room bound via `link profile` can never retroactively get
# this -- there's no way around that; it's Matrix's own constraint, not this
# bridge's choice.
SOCIAL_PROFILE_ROOM_TYPE = "org.matrix.msc4501.social.profile"

# MSC4501's other piece: a per-user profile field (via MSC4133 Extensible
# Profiles) pointing at which room is "the" official profile for that
# account, since a room merely being SOCIAL_PROFILE_ROOM_TYPE doesn't by
# itself mean it's the current one (see resolve_old_remote_actor_room for
# the same "room type alone isn't enough" problem on the read side). Set
# ONLY for ghosts (a Remote User Room mirroring someone else's fediverse
# account) -- never for a local user's own linked Profile Room, since a
# real Matrix account's own MSC4133 profile fields are that user's own to
# set, not this bridge's to puppet on their behalf, and per MSC4501 itself
# a local user may deliberately want a different room entirely as their
# general Matrix-wide social profile.
SOCIAL_PROFILE_ROOM_ID_FIELD = "org.matrix.msc4501.social.profile_room_id"

# The inverse of SOCIAL_PROFILE_ROOM_ID_FIELD: a room-level state event
# (state_key "", content {"user_id": "@alice:example.org"}) asserting
# which Matrix user a Profile Room actually belongs to -- added to
# MSC4501 specifically for a case this bridge hits on every single
# Profile Room it creates: m.room.create's own "creator" is the bridge's
# bot, never the actual owner, so "creator" alone can never be trusted to
# answer "whose profile is this". Set ONLY on local Profile Rooms
# (bridge.commands._handle_create_profile/_handle_link_profile/
# _replace_profile_room) -- never Remote User Rooms, where the ghost
# already legitimately IS both the room's creator and the Matrix user it
# represents, so there's no creator/owner mismatch to resolve. Per the
# MSC, this event isn't one m.room.power_levels gives its own default
# override for, so it falls back to state_default (50, "Moderator") if
# nothing else is done -- meaning any Moderator, not just the room's
# actual owner, could otherwise reassign a profile out from under them.
# See SOCIAL_PROFILE_USER_ID_POWER_LEVEL below for the fix.
SOCIAL_PROFILE_USER_ID_STATE_TYPE = "org.matrix.msc4501.social.profile_user_id"

# The power level SOCIAL_PROFILE_USER_ID_STATE_TYPE should require to set,
# per the MSC's own explicit guidance -- keyed under the SAME unstable
# prefix actually being sent as the event type (not the future stable
# m.social.profile_user_id name the MSC's own JSON example shows): Matrix
# power-level enforcement matches on an event's literal type string, so a
# power_levels override keyed by the stable name would protect nothing
# while this bridge is still sending the unstable one, the same
# "don't use the future stable name early" reasoning as everywhere else
# in this file.
SOCIAL_PROFILE_USER_ID_POWER_LEVEL = 100

# Marks a mirrored event's content as relating to another event via some
# social-media-shaped relation -- MSC4501's own replacement for the earlier,
# repost-only ``social.repost_of`` field, generalized (same "rel_type"
# discriminator convention as Matrix's own ``m.relates_to``) to also cover
# SOCIAL_REL_TYPE_REPLY below. See bridge.inbox_dispatch's
# _handle_announce/_handle_create/_echo_reply_in_own_room, and
# bridge.config.BridgeSection.set_msc4501_relates_to (the toggle for
# whether this gets set at all; on by default). Shape per the MSC's own
# "Cross-room post references" section: {"rel_type": ..., "event_id": ...,
# "room_id": ..., "sender": "<referenced post's author's mxid>",
# "displayname": ... (optional), "content": {<the related event's own
# full, untouched Matrix content dict>}} -- the real event content
# (msgtype, url, info, etc.), not just its extracted text, since a
# plain-text-only copy would be blank for an uncaptioned image/video
# repost. ``sender`` is MANDATORY per the MSC (a viewer may not share a
# room with the referenced post's author at all, so there's no other way
# to attribute it) -- every call site skips setting this field entirely
# if the referenced author's mxid can't be resolved (see
# resolve_actor_matrix_identity), same as it already does when there's no
# local Matrix mirror to reference at all. Purely additive content on an
# ordinary m.room.message event -- unlike SOCIAL_POST_EVENT_TYPE below, a
# client with no idea what this is just ignores it.
SOCIAL_RELATES_TO_FIELD = "org.matrix.msc4501.social.relates_to"

# SOCIAL_RELATES_TO_FIELD's "rel_type" for a boost (Announce) or quote-post:
# the related event is the thing being reposted.
SOCIAL_REL_TYPE_REPOST = "org.matrix.msc4501.social.repost"

# SOCIAL_RELATES_TO_FIELD's "rel_type" for a followed account's reply
# echoed into their OWN Remote User Room (see _echo_reply_in_own_room) --
# the related event is the post being replied to. Distinct from the real
# Matrix thread relation (``rel_type: "m.thread"``, see
# thread_reply_relates_to below) that the CANONICAL threaded copy of the
# same reply carries in the conversation's own room: this one lives on a
# second, non-primary copy in a different room entirely, which isn't
# itself part of that Matrix thread.
SOCIAL_REL_TYPE_REPLY = "org.matrix.msc4501.social.reply"


def social_relates_to(
    rel_type: str,
    *,
    event_id: str,
    room_id: str,
    sender: str,
    content: dict | None = None,
    content_inline: bool = False,
    displayname: str | None = None,
) -> dict:
    """Builds a ``SOCIAL_RELATES_TO_FIELD`` value -- shared by every mirrored
    event that relates to another one this way (a repost/boost, a quote-post,
    or a cross-posted reply's echo). ``sender`` (the referenced post's own
    author's mxid) is mandatory per the MSC; callers are responsible for not
    calling this at all when it can't be resolved (see
    ``resolve_actor_matrix_identity``) -- there's no meaningful placeholder
    for a required field. ``displayname`` is the MSC's RECOMMENDED (not
    mandatory) snapshot of the referenced author's display name, included
    when the caller already has one on hand.

    ``content_inline`` and ``content`` are mutually exclusive:
    ``content_inline=True`` takes precedence and omits ``content`` entirely,
    asserting the caller's own outer event ``content`` already IS a full
    copy of the referenced post rather than duplicating it a second time
    inside this block. Per the MSC's own text, this is ONLY valid for a
    genuine repost/boost with no reposting-user commentary of its own
    (``SOCIAL_REL_TYPE_REPOST``'s pure-boost case -- see
    ``bridge.inbox_dispatch._build_repost_message``'s own docstring, the
    only caller that passes it) -- never a quote-post (whose outer content
    is the quoter's own caption, not a copy of the quoted post) and never
    ``SOCIAL_REL_TYPE_REPLY`` (whose outer content is always the replier's
    own text). A compliant reader has no way to separate genuine
    commentary from duplicated original text once ``content_inline`` is
    set, so callers with any commentary of their own MUST use ``content``
    instead."""
    value = {"rel_type": rel_type, "event_id": event_id, "room_id": room_id, "sender": sender}
    if displayname:
        value["displayname"] = displayname
    if content_inline:
        value["content_inline"] = True
    elif content is not None:
        value["content"] = content
    return value

# Set (bool, always ``True`` when present) on a mirrored event whose body/
# formatted_body starts with a bridge-generated attribution line -- "🔁 X
# boosted Y's post:" (_build_repost_message) or "⤵️ Reply to X's post:"
# (_echo_reply_in_own_room) -- ahead of the actual mirrored content, rather
# than the mirrored content being the entire body verbatim. Haven's own
# field (not part of MSC4501 or any other spec), requested 2026-07-08, so
# it can strip that header off entirely when rendering instead of treating
# it as part of the post's own text. Deliberately NOT set on a quote-post's
# own tail (_quoted_post_render) or an outbound ;repost's own echo
# (bridge.commands._handle_repost) -- both put a REAL caption first, with
# any bridge-generated attribution coming after, not a header "at the top"
# in the sense this field means. Also deliberately NOT set on anything sent
# as the bridge BOT itself (e.g. send_boost's own reaction-triggered
# notice, posted into the booster's Profile Room as the bot rather than a
# ghost) -- Haven only ever needs to strip a header from a mirrored
# fediverse-authored post, never from the bridge's own first-party notices.
HAVEN_REMOVE_HEADER_FIELD = "software.haven.remove_header"

# The event TYPE (not a content field) MSC4501 proposes in place of
# m.room.message for a "real" social-media-style post -- see
# bridge.config.BridgeSection.use_msc4501_post_event_type's own comment
# for why this is off by default and not just recommended-off but
# actually risky to turn on before MSC4501's own Phase 2: a client that
# has never heard of this event type won't render the event at all.
SOCIAL_POST_EVENT_TYPE = "org.matrix.msc4501.social.post"


def mirrored_post_event_type(config) -> str:
    """The event type to use when mirroring a remote fediverse account's
    own posts, replies, and boosts into Matrix (never DMs/Chats, which
    aren't "posts") -- SOCIAL_POST_EVENT_TYPE if
    ``bridge.use_msc4501_post_event_type`` is on, otherwise the ordinary
    ``m.room.message`` every client already understands."""
    return SOCIAL_POST_EVENT_TYPE if config.bridge.use_msc4501_post_event_type else "m.room.message"


# No stable Matrix state event exists yet for a room's "banner"/header image
# (distinct from m.room.avatar) as of 2026-07 -- MSC4221 ("Room Banners":
# https://github.com/matrix-org/matrix-spec-proposals/pull/4221) proposes
# m.room.banner for exactly this, but per that MSC's own "Unstable prefix"
# section, implementations should use page.codeberg.everypizza.room.banner
# until it's actually merged, not m.room.banner itself (the same
# "don't use the future stable name early" reasoning as SOCIAL_PROFILE_ROOM_TYPE
# above). Content shape mirrors m.room.avatar's own: {"url": "mxc://..."}.
# Used for both a local user's own Profile Room (bridge.commands._handle_banner)
# and a ghost's Remote User Room, kept in sync with the remote actor's own
# AP ``image`` field (see _handle_update in bridge.inbox_dispatch).
PROFILE_BANNER_STATE_TYPE = "page.codeberg.everypizza.room.banner"

# Explicit room version for the ``;replace`` family -- this server's own
# configured default room version is v11, not v12, so leaving
# SynapseClient.create_room's ``room_version`` unset here would create a v11
# room while still omitting the creator from ``power_level_content_override``
# (see e.g. _replace_profile_room), which v11 rejects (only v12 gives a
# creator implicit power, letting the override omit them safely).
REPLACE_ROOM_VERSION = "12"

# A Remote User Room's and a local Profile Room's own join_rule are each
# separately configurable (bridge.config.BridgeSection.ghost_room_join_rule /
# local_profile_room_join_rule) -- this constant is now just the fixed
# default for a ghost's DM/Chat room specifically (ensure_ghost_dm_room/
# ensure_ghost_chat_room and their `;replace room` equivalents), which stays
# knock-only unconditionally: it's how the intended local user lets
# themselves back into a private 1:1 room they already know the ID of (e.g.
# after a `;replace room`) without needing an admin -- see
# bridge.membership.maybe_handle_knock, which auto-accepts a knock using the
# same rules as the `rejoin` command. Not exposed as its own setting since a
# DM/Chat's privacy concern (letting the ONE intended person back in) is
# different from a profile room's (who can discover/follow it at all).
KNOCK_JOIN_RULE = "knock"


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def source_post_url(note: dict) -> str | None:
    """The fediverse permalink for a mirrored post, so the mirrored Matrix
    event can carry a link back to it -- preferred over the Note's bare
    ``id`` (often an API endpoint rather than something a human can open).
    Handles the shapes seen in the wild: a bare URL string, a Link object,
    or a list of either (some implementations offer multiple
    representations -- we just use the first). Falls back to ``id`` if no
    usable ``url`` is present.

    Carried on the mirrored event as ``external_url`` -- not part of any
    current Matrix spec (there's no MSC for it), but an unprefixed field
    already used the same way by the wider mautrix bridge ecosystem
    (``mautrix.types.BaseMessageEventContent.external_url``), so reusing it
    keeps us consistent with bridges everyone already interops with instead
    of inventing a competing namespaced field.
    """
    url = note.get("url")
    if isinstance(url, list):
        url = url[0] if url else None
    if isinstance(url, dict):
        url = url.get("href") or url.get("url")
    if isinstance(url, str) and url:
        return url
    obj_id = note.get("id")
    return obj_id if isinstance(obj_id, str) else None


# ActivityPub predates nothing earlier than this by any real margin (the
# protocol itself dates to 2018, its OStatus-era predecessors to ~2010) --
# used purely as a sanity floor for a "published" timestamp so an obviously
# bogus value (a server's own clock defaulting to the Unix epoch, or some
# other clearly-wrong constant) doesn't get treated as real history. Not
# meant to be a precise cutoff, just implausibly early.
_EARLIEST_PLAUSIBLE_PUBLISHED = datetime(2000, 1, 1, tzinfo=timezone.utc)


def resolve_event_ts(note: dict, *, max_clock_skew: int, max_backdate_days: int = 730) -> int | None:
    """Parses a Note's own ``published`` field into Unix milliseconds, for
    ``SynapseClient.send_message_event``'s ``ts`` (timestamp massaging) --
    so a mirrored post's displayed time matches when it was actually
    posted on the fediverse, not when the bridge happened to process it
    (which is what plain "now" delivery, or a backfill running long after
    the fact, would otherwise show).

    Returns None (falls back to "now", exactly today's behavior prior to
    this) if ``published`` is missing, unparseable, or implausible --
    either more than ``max_clock_skew`` seconds in the future (the same
    tolerance already used for signature verification -- see
    ``bridge.config.FederationSection``), or before whichever is LATER of
    ``_EARLIEST_PLAUSIBLE_PUBLISHED`` or ``max_backdate_days`` ago.

    The ``max_backdate_days`` bound exists for a completely different
    reason than the other two (a malfunctioning/lying server) -- confirmed
    live against this project's own production Synapse that a custom
    ``ts`` older than its configured ``retention.default_policy.max_lifetime``
    is silently accepted (200 OK, a real event_id) but then never actually
    reachable again -- not via a later GET, not via ``/messages``, nothing.
    That's a silent post-drop, strictly worse than just showing "now" as
    this bridge always has, and there is no Client-Server API to discover
    a homeserver's own retention policy -- see
    ``bridge.config.FederationSection.max_backdate_days``, which the
    operator must set by hand to match (or stay safely below) their own
    server's policy, if it has one at all."""
    published = note.get("published")
    if not isinstance(published, str):
        return None
    try:
        parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    earliest_allowed = max(_EARLIEST_PLAUSIBLE_PUBLISHED, now - timedelta(days=max_backdate_days))
    if parsed < earliest_allowed:
        return None
    if (parsed - now).total_seconds() > max_clock_skew:
        return None
    return int(parsed.timestamp() * 1000)


def thread_reply_relates_to(*, event_id: str, thread_root_event_id: str | None) -> dict:
    """``m.relates_to`` for a real Matrix thread reply to ``event_id``.

    A real thread (not just a rich-reply quote block) needs the relation's
    OWN ``event_id`` to point at the root of the whole chain, not the
    immediate parent -- so this inherits ``thread_root_event_id`` if
    ``event_id`` is itself already part of one (Matrix threads don't
    nest), otherwise starts a fresh thread rooted at ``event_id`` itself
    (matching what a client's own "reply in thread" action would do for a
    not-yet-threaded message).

    Shared by every bot-composed reply that needs to land in the right
    thread: ``bridge.inbox_dispatch._mirror_note_as_reply`` (an inbound AP
    reply) and the ``;boost``/``;repost``/reaction-boost confirmation
    notices (``bridge.commands``, ``bridge.reaction_bridge``) alike.
    """
    return {
        "rel_type": "m.thread",
        "event_id": thread_root_event_id or event_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": event_id},
    }


REPOST_CAPTION_MARKER = "\n\n\U0001F501 reposted "


def build_repost_note_content(caption: str, quote_uri: str, mention_links: dict[str, str] | None = None) -> str:
    """The AP ``content`` HTML for a ``;repost``-command Note: the user's
    own caption, then "RT: <link>" -- other implementations use this exact
    shape as their own fallback for a receiver that doesn't understand
    ``quoteUri`` at all, and ``bridge.inbox_dispatch._strip_quote_tail``/
    ``_strip_quote_tail_html`` strip exactly this same shape back out on
    the way in, so it's also what a receiver-facing reader should expect
    on the way out.

    Shared between ``bridge.commands._handle_repost`` (building it fresh,
    for the actual outbound ``Create``) and ``bridge.activitypub.routes``
    (reconstructing it for the outbox/a dereferenced Note -- see
    ``split_repost_caption`` below) so the two can never drift apart and
    quietly disagree about what a repost's AP content actually looks like."""
    return plain_text_to_note_html(caption, mention_links) + (
        f'<p>RT: <a href="{html.escape(quote_uri)}">{html.escape(quote_uri)}</a></p>'
    )


def split_repost_caption(echo_body: str) -> str:
    """Recovers just the caption typed into ``;repost <caption>`` from its
    own Matrix profile-room echo body (see ``bridge.commands.
    _handle_repost`` -- the echo reads "{caption}\n\n\U0001F501 reposted
    X's post: ..." for a Matrix audience, which is NOT the same content
    that actually went out over ActivityPub for this same post -- see
    ``build_repost_note_content``). Used by ``bridge.activitypub.routes``
    to reconstruct the correct AP content for the outbox/a dereferenced
    Note, rather than serving the Matrix echo's own wording (which would
    otherwise show a stray matrix.to link and no ``quoteUri`` at all to a
    remote server re-fetching this post fresh, rather than trusting the
    originally-delivered ``Create``'s embedded copy). Returns the whole
    string unchanged if the marker isn't present -- shouldn't happen for
    anything actually built by ``_handle_repost``, but never worse than
    treating the whole thing as the caption."""
    return echo_body.split(REPOST_CAPTION_MARKER, 1)[0]


async def resolve_actor_matrix_identity(request: Request, actor_id: str) -> tuple[str, str, str | None]:
    """``(handle, display_name, mxid_or_None)`` for ``actor_id`` -- the
    shared lookup ``actor_html_with_avatar`` builds its pill from, factored
    out so a caller that needs the raw mxid/display_name themselves (rather
    than an HTML snippet) doesn't have to re-derive this same
    local-actor-vs-ghost-profile-vs-unknown-remote resolution by hand. Used
    by ``bridge.inbox_dispatch``/``bridge.commands``/``bridge.reaction_bridge``
    to populate ``m.social.relates_to``'s mandatory ``sender`` (and
    RECOMMENDED ``displayname``) fields per MSC4501 -- see
    ``SOCIAL_RELATES_TO_FIELD``'s own docstring."""
    config = request.app.state.config
    repository = request.app.state.repository
    base = config.bridge.public_base_url

    username = username_from_actor_url(base, actor_id)
    display_name: str
    handle: str
    mxid: str | None
    if username is not None:
        local_actor = await repository.get_local_actor(username)
        if local_actor is None:
            return actor_id, actor_id, None
        handle = f"@{username}@{config.bridge.domain}"
        display_name = local_actor.display_name or username
        mxid = local_actor.matrix_user_id
    else:
        profile = await repository.get_ghost_profile(actor_id)
        if profile is None:
            domain = urlsplit(actor_id).hostname or ""
            localpart = actor_id.rstrip("/").rsplit("/", 1)[-1]
            handle = f"@{localpart}@{domain}" if domain else localpart
            return handle, handle, None
        handle = profile.handle or actor_id
        display_name = profile.display_name or handle
        mxid = profile.mxid
    return handle, display_name, mxid


async def actor_html_with_avatar(request: Request, actor_id: str) -> tuple[str, str]:
    """``(plain_handle, html_snippet)`` for ``actor_id`` -- a Matrix pill if
    there's a real mxid for them (a ghost, or a local actor's own account),
    bold plain text otherwise. No explicit avatar image: a Matrix pill
    already renders one on its own in every client this bridge has been
    checked against, so embedding a second, separate ``<img>`` next to it
    would just be redundant.

    The pill anchor ALWAYS carries the display name as its inner text --
    the project-wide convention for every user pill this bridge emits, set
    by the user (2026-07-03). It used to be empty (an older Element X
    build had been observed rendering its own generated pill AND the
    anchor's inner content side by side), but an empty anchor contributes
    NOTHING to anything that extracts the message's text content, which
    is exactly what Element Web's desktop notification text does: a boost
    card notified as "boosted 's post" with both names missing (reported
    live via dunst, 2026-07-03). With the name inside, the user confirmed
    it renders correctly on BOTH Element Web and current Element X -- the
    old double-render no longer reproduces, so there is no tradeoff left:
    never emit an empty pill anchor.

    Shared by the boost/repost profile notices (``bridge.reaction_bridge``,
    ``bridge.commands``), inbound quote rendering (``bridge.inbox_dispatch``),
    and the mirrored-repost card (``bridge.inbox_dispatch._build_repost_message``)
    -- every rendering of "X boosted/reposted Y's post" looks the same
    regardless of whether X and Y are local, remote, or which side of the
    bridge made the repost.
    """
    handle, display_name, mxid = await resolve_actor_matrix_identity(request, actor_id)
    if mxid:
        pill_href = html.escape(f"https://matrix.to/#/{mxid}", quote=True)
        name_html = f'<a href="{pill_href}">{html.escape(display_name)}</a>'
    else:
        name_html = f"<strong>{html.escape(display_name)}</strong>"
    return handle, name_html


async def merge_attachment_into_content(
    request: Request, message_content: dict, attachment: dict, *, mxc_uri: str | None = None
) -> tuple[dict, str | None]:
    """Fold ``attachment`` into ``message_content`` -- switching its
    ``msgtype`` to the media type and adding ``url``/``info``/``filename`` --
    so a post's text and its primary attachment travel as a single Matrix
    event, the way a real client posts an image with a caption, instead of
    two separate ones. ``filename`` (not part of the stable spec, but a
    widely-supported convention -- MSC2529) preserves the original filename
    separately from ``body``, which keeps carrying the caption/text exactly
    as it would for a plain text message. Any keys already in
    ``message_content`` (like a thread ``m.relates_to``) are preserved.

    ``mxc_uri``, if given, is reused as-is rather than fetching/re-uploading
    the attachment's source URL again -- same reasoning as
    ``mirror_attachment``'s identical parameter.

    Returns ``(content, mxc_uri)`` -- ``message_content`` unchanged and
    ``mxc_uri`` None if the attachment couldn't actually be fetched/
    uploaded; otherwise the merged content and whichever mxc:// URI was
    used (freshly uploaded, or the given one), so a caller can pass it on
    to mirror the same attachment elsewhere without re-uploading it.
    """
    width = height = None
    if mxc_uri is None:
        # A video's dimensions aren't reused across the "mxc_uri already
        # given" path below since that path never fetched the file at all
        # -- only the primary attachment (the one actually fetched here)
        # ever gets them.
        if matrix_msgtype_for_mimetype(attachment["media_type"]) == "m.video":
            result = await fetch_and_upload_media_with_dimensions(
                request.app.state.http_client, request.app.state.synapse, attachment["url"]
            )
            if result is None:
                return message_content, None
            mxc_uri, width, height = result
        else:
            mxc_uri = await fetch_and_upload_media(request.app.state.http_client, request.app.state.synapse, attachment["url"])
    if not mxc_uri:
        return message_content, None
    merged = dict(message_content)
    merged["msgtype"] = matrix_msgtype_for_mimetype(attachment["media_type"])
    merged["url"] = mxc_uri
    info = {"mimetype": attachment["media_type"]}
    if width and height:
        info["w"] = width
        info["h"] = height
    merged["info"] = info
    raw_filename = attachment["name"] or attachment["url"].rsplit("/", 1)[-1] or "attachment"
    merged["filename"] = filename_with_extension(raw_filename, attachment["media_type"])
    return merged, mxc_uri


async def attach_media_to_content(
    request: Request, message_content: dict, attachments: list[dict], *, mxc_uri: str | None = None
) -> tuple[dict, str | None]:
    """Attach ``attachments`` to ``message_content`` -- embedding only the
    FIRST as real Matrix media (see ``merge_attachment_into_content``) so an
    ActivityPub post always maps to exactly one Matrix event, regardless of
    how many files it has. Any additional attachments are appended under an
    "Other Attachments:" header at the end of the body, each as a link
    labeled with just its filename (not the full URL -- cleaner-looking,
    and consistent between the plain body, which spells it out as a literal
    markdown link, and ``formatted_body``, which -- forced into existence
    here if the post didn't already have rich content, so the links always
    render as real clickable text instead of a client-dependent markdown
    guess -- gets a proper ``<a>`` anchor) rather than downloaded/
    re-uploaded and sent as their own separate events the way they used to
    be: Matrix only supports one piece of embedded media per event anyway,
    so a 2nd+ attachment was never going to be part of the SAME event
    either way, and giving it its own event split one ActivityPub post
    across several Matrix ones -- surprising for anyone trying to react to
    or otherwise refer to "the post" as a single thing.

    ``mxc_uri``, if given, is reused for the first attachment as-is rather
    than fetching/re-uploading it again -- same reasoning as
    ``merge_attachment_into_content``'s identical parameter.

    Returns ``(content, mxc_uri)`` matching ``merge_attachment_into_content``'s
    own shape: the second element is the mxc:// URI the FIRST attachment
    ended up uploaded as (None if there were no attachments, or embedding
    it failed), for a caller needing to reuse that same upload elsewhere
    (e.g. mirroring the same boosted post's first attachment into both the
    original author's room and a repost summary card). If embedding the
    first attachment fails outright, every attachment (including that
    first one) is linked instead, rather than silently dropping it."""
    if not attachments:
        return message_content, None
    merged, used_mxc_uri = await merge_attachment_into_content(
        request, message_content, attachments[0], mxc_uri=mxc_uri
    )
    extra = attachments[1:] if used_mxc_uri else attachments
    if not extra:
        return merged, used_mxc_uri

    merged = dict(merged)
    extra_names = [a["name"] or a["url"].rsplit("/", 1)[-1] or "attachment" for a in extra]
    extra_urls = [a["url"] for a in extra]

    plain_links = "\n".join(f"[{name}]({url})" for name, url in zip(extra_names, extra_urls))
    plain_section = f"Other Attachments:\n{plain_links}"
    body = merged.get("body") or ""
    merged["body"] = f"{body}\n\n{plain_section}" if body else plain_section

    html_links = "<br>".join(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>'
        for name, url in zip(extra_names, extra_urls)
    )
    html_section = f"Other Attachments:<br>{html_links}"
    formatted_body = merged.get("formatted_body")
    if formatted_body is None:
        # No rich content yet -- escape the existing plain body into HTML so
        # the links can still get real anchors: a bare markdown-style link
        # in plain-text body renders as literal, unclickable "[name](url)"
        # text in any client that only renders formatted_body when present
        # (most of them).
        merged["format"] = "org.matrix.custom.html"
        formatted_body = html.escape(body) if body else ""
    merged["formatted_body"] = f"{formatted_body}<br><br>{html_section}" if formatted_body else html_section
    return merged, used_mxc_uri


async def send_bridge_info(
    request: Request,
    *,
    room_id: str,
    actor_id: str,
    display_name: str | None,
    avatar_mxc: str | None,
    as_user_id: str,
) -> None:
    """Tag a freshly-created room (a Remote User Room mirroring someone
    else's fediverse account, or a local user's own via ``create profile``)
    with bridge-info state events identifying it as bridge-made and naming
    the specific ActivityPub actor it represents, so bridge-aware clients
    can show that provenance instead of it looking like an ordinary room.
    Best-effort -- the room is already fully usable without it.

    Sent under both the current ``m.bridge`` type (MSC2346) and the legacy
    ``uk.half-shot.bridge`` type it was renamed from: Element Web's room
    settings "Bridges" tab still only reads the legacy type as of 2026-07
    (the ``m.bridge`` lookup is present in its source but commented out),
    so relying on ``m.bridge`` alone leaves the tab empty in that client."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    channel: dict = {"id": actor_id}
    if display_name:
        channel["displayname"] = display_name
    if avatar_mxc:
        channel["avatar_url"] = avatar_mxc
    protocol: dict = {"id": "activitypub", "displayname": "ActivityPub"}
    if config.appservice.bot_avatar_mxc:
        # The bot's own Matrix avatar doubles as the ActivityPub protocol
        # icon for now -- there's no separate branding asset to point to.
        protocol["avatar_url"] = config.appservice.bot_avatar_mxc
    content = {
        "bridgebot": bot_mxid,
        "creator": bot_mxid,
        "protocol": protocol,
        "channel": channel,
    }
    state_key = f"activitypub://{actor_id}"
    for event_type in ("m.bridge", "uk.half-shot.bridge"):
        try:
            await request.app.state.synapse.send_state_event(
                room_id, event_type, state_key, content, as_user_id=as_user_id
            )
        except SynapseError:
            logger.info("Could not set %s state event in %s", event_type, room_id, exc_info=True)


async def set_ghost_profile_room_id(request: Request, *, mxid: str, room_id: str) -> None:
    """Point ``mxid`` (a ghost, never a local user -- see
    SOCIAL_PROFILE_ROOM_ID_FIELD's docstring) at ``room_id`` as its
    MSC4501 ``social.profile_room_id`` profile field. Best-effort, same as
    the rest of the bridge's room bookkeeping -- silently does nothing
    useful on a homeserver without MSC4133 support enabled, which is NOT
    the default even on this bridge's own target, Synapse (requires
    experimental_features.msc4133_enabled: true in homeserver.yaml,
    confirmed against Synapse's own source 2026-07-08) -- see
    ``bridge.config.BridgeSection.set_msc4501_profile_room_id``, which
    lets that be turned off entirely rather than eating a guaranteed-
    failing request every time on a deployment that hasn't opted in yet.
    Call this every time a ghost's Remote User Room is registered or
    changes (a first follow, a mention-triggered import, or a
    ``;replace room``), so the field never points at a stale,
    already-superseded room."""
    if not request.app.state.config.bridge.set_msc4501_profile_room_id:
        return
    synapse = request.app.state.synapse
    try:
        await synapse.set_profile_field(mxid, SOCIAL_PROFILE_ROOM_ID_FIELD, room_id)
    except SynapseError:
        logger.info("Could not set %s for %s", SOCIAL_PROFILE_ROOM_ID_FIELD, mxid, exc_info=True)


async def set_ghost_room_banner(
    request: Request, *, room_id: str, ghost_user_id: str, banner_mxc: str
) -> None:
    """Set PROFILE_BANNER_STATE_TYPE on a Remote User Room to mirror the
    remote actor's own AP ``image`` (their profile banner/header) -- same
    room-state-only mechanism as a local user's own ``;banner``, just
    driven by what the remote actor's Actor document says rather than a
    command. Best-effort, same as this room's own m.room.avatar handling
    right next to every call site of this -- a failure here is cosmetic,
    not worth blocking room creation or an Update sync over."""
    synapse = request.app.state.synapse
    try:
        await synapse.send_state_event(
            room_id, PROFILE_BANNER_STATE_TYPE, "", {"url": banner_mxc}, as_user_id=ghost_user_id
        )
    except SynapseError:
        logger.info("Could not set %s in %s", PROFILE_BANNER_STATE_TYPE, room_id, exc_info=True)


async def set_profile_user_id(
    request: Request, *, room_id: str, matrix_user_id: str, as_user_id: str
) -> None:
    """Set SOCIAL_PROFILE_USER_ID_STATE_TYPE on a Profile Room, asserting
    ``matrix_user_id`` as the room's true owner regardless of who actually
    created it (always the bridge's bot, for a bridge-created room -- see
    that constant's own comment). Best-effort, same as the rest of the
    bridge's room bookkeeping. Call this every time a Profile Room is
    created, linked, or replaced."""
    synapse = request.app.state.synapse
    try:
        await synapse.send_state_event(
            room_id, SOCIAL_PROFILE_USER_ID_STATE_TYPE, "", {"user_id": matrix_user_id}, as_user_id=as_user_id
        )
    except SynapseError:
        logger.info("Could not set %s in %s", SOCIAL_PROFILE_USER_ID_STATE_TYPE, room_id, exc_info=True)


async def protect_profile_user_id_power_level(request: Request, *, room_id: str) -> None:
    """Best-effort read-modify-write of ``room_id``'s CURRENT
    ``m.room.power_levels``, merging in an ``events`` override requiring
    SOCIAL_PROFILE_USER_ID_POWER_LEVEL to set SOCIAL_PROFILE_USER_ID_STATE_TYPE
    -- see that constant's own comment for why this matters (without it,
    any Moderator could silently reassign a profile's ownership). Only
    needed for ``;link profile`` (an already-existing room, whose power
    levels weren't set by ``create_room``'s own ``power_level_content_override``
    in the first place -- see ``_handle_create_profile``/``_replace_profile_room``,
    which bake this in at creation time instead). Sent as the bot; if the
    bot doesn't have high enough power in this room to touch
    ``m.room.power_levels`` at all (routinely true for a room the user
    made themselves, same caveat as this function's caller's own room
    name/avatar attempt), this just silently does nothing, same as that."""
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(request.app.state.config)
    try:
        current = await synapse.get_room_state(room_id, "m.room.power_levels", as_user_id=bot_mxid)
    except SynapseError:
        logger.info("Could not read power levels in %s to protect profile_user_id", room_id, exc_info=True)
        return
    events = dict(current.get("events") or {})
    if events.get(SOCIAL_PROFILE_USER_ID_STATE_TYPE) == SOCIAL_PROFILE_USER_ID_POWER_LEVEL:
        return  # already protected -- e.g. a second `;link profile` re-run
    events[SOCIAL_PROFILE_USER_ID_STATE_TYPE] = SOCIAL_PROFILE_USER_ID_POWER_LEVEL
    new_content = {**current, "events": events}
    try:
        await synapse.send_state_event(room_id, "m.room.power_levels", "", new_content, as_user_id=bot_mxid)
    except SynapseError:
        logger.info("Could not protect profile_user_id power level in %s", room_id, exc_info=True)


async def resolve_old_remote_actor_room(request: Request, room_id: str) -> RemoteActorRoom | None:
    """Recognize a Remote User Room even after ``;replace room`` has moved
    on from it -- ``ActorRepository.get_remote_actor_room_by_room_id`` only
    ever reflects the CURRENT room per actor (its upsert overwrites
    ``room_id`` in place). Tries the permanent ``remote_actor_room_history``
    table first (fast, indexed, and independent of any external state-event
    schema), falling back to reading the room's own
    ``m.bridge``/``uk.half-shot.bridge`` state (set once at creation by
    ``send_bridge_info``, never touched again) only for a room old enough
    to predate that table -- no ``ghost_profiles`` row for a local (not
    fediverse) actor_id naturally makes this fall through instead of
    misidentifying a Profile Room, which also carries ``m.bridge`` state."""
    repository = request.app.state.repository
    remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
    if remote_room is not None:
        return remote_room

    actor_id = await repository.get_remote_actor_room_history_actor_id(room_id)
    if actor_id is None:
        actor_id = await _actor_id_from_bridge_state(request, room_id)
    if actor_id is None:
        return None

    ghost_profile = await repository.get_ghost_profile(actor_id)
    if ghost_profile is None or not ghost_profile.mxid:
        return None
    return RemoteActorRoom(
        actor_id=actor_id, room_id=room_id, ghost_user_id=ghost_profile.mxid,
        inbox_url="", display_name=ghost_profile.display_name or "", icon_url=ghost_profile.icon_url,
    )


async def _actor_id_from_bridge_state(request: Request, room_id: str) -> str | None:
    """Read ``room_id``'s own permanent ``m.bridge``/``uk.half-shot.bridge``
    state back out to recover which actor it was created for -- the
    fallback of last resort for a room old enough to predate the relevant
    history table, used by both ``resolve_old_remote_actor_room`` and
    ``resolve_old_ghost_room_owner``."""
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(request.app.state.config)
    try:
        state_events = await synapse.get_full_room_state(room_id, as_user_id=bot_mxid)
    except SynapseError:
        return None
    for event in state_events:
        if event.get("type") in ("m.bridge", "uk.half-shot.bridge"):
            channel = (event.get("content") or {}).get("channel") or {}
            if channel.get("id"):
                return channel["id"]
    return None


async def resolve_old_ghost_room_owner(request: Request, room_id: str) -> tuple[str, str] | None:
    """Recover ``(actor_id, matrix_user_id)`` for an old, no-longer-tracked
    ghost DM or Chat room -- ``ghost_dm_rooms``/``ghost_chat_rooms`` only
    ever reflect the CURRENT room per ``(actor_id, matrix_user_id)`` pair
    (their upsert overwrites ``room_id`` in place on replace). Tries the
    permanent ``ghost_dm_room_history``/``ghost_chat_room_history`` tables
    first (fast, indexed, independent of any external state-event schema),
    falling back to reading the room's own state back out of Synapse only
    for a room old enough to predate those tables: ``actor_id`` from the
    room's permanent ``m.bridge`` state, ``matrix_user_id`` from the room's
    own historical membership -- the one non-bot, non-ghost member who was
    ever in it, a DM/Chat room being exactly a (ghost, one local user) pair
    by construction. Doesn't distinguish DM from Chat rooms either way --
    callers only care that a member was ever there at all, not which of
    the two room kinds this was."""
    repository = request.app.state.repository
    resolved = await repository.get_ghost_dm_room_history(room_id)
    if resolved is None:
        resolved = await repository.get_ghost_chat_room_history(room_id)
    if resolved is not None:
        return resolved

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    ghost_prefix = f"@{config.appservice.user_prefix}"
    synapse = request.app.state.synapse
    try:
        state_events = await synapse.get_full_room_state(room_id, as_user_id=bot_mxid)
    except SynapseError:
        return None

    actor_id: str | None = None
    matrix_user_id: str | None = None
    for event in state_events:
        event_type = event.get("type")
        if event_type in ("m.bridge", "uk.half-shot.bridge") and actor_id is None:
            channel = (event.get("content") or {}).get("channel") or {}
            if channel.get("id"):
                actor_id = channel["id"]
        elif event_type == "m.room.member":
            member = event.get("state_key", "")
            if member and member != bot_mxid and not member.startswith(ghost_prefix):
                matrix_user_id = member
    if actor_id is None or matrix_user_id is None:
        return None
    return actor_id, matrix_user_id


async def push_profile_update(request: Request, actor_record: ActorRecord) -> None:
    """Send a signed ``Update(Person)`` -- carrying ``actor_record``'s
    CURRENT name/bio/avatar/banner, built exactly the way
    ``GET /actor/{username}`` itself would -- to every one of their
    followers.

    A fresh fetch of the actor document already reflects a just-changed
    name/bio/avatar/banner on its own (all read live from
    ``ActorRepository`` by ``bridge.activitypub.routes.get_actor``); this is
    what makes an ALREADY-cached copy on a follower's server actually
    refresh too, instead of silently going stale until that server next
    decides on its own to re-fetch it -- which, in practice, for plenty of
    implementations, might be "never" for an account they already follow.
    Best-effort, like every other delivery in this bridge: a follower whose
    inbox can't be resolved, or whose server rejects the delivery, just
    keeps showing the old cached copy until some later Update gets through.

    Called by ``bridge.profile_posts`` (Profile Room topic/name/avatar
    changes) and ``bridge.commands`` (the ``banner`` command)."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository = request.app.state.repository

    actor = Actor(
        id=actor_url(base, actor_record.username),
        preferred_username=actor_record.username,
        name=actor_record.display_name or actor_record.username,
        summary=actor_record.summary,
        url=actor_url(base, actor_record.username),
        inbox=inbox_url(base, actor_record.username),
        outbox=outbox_url(base, actor_record.username),
        followers=followers_url(base, actor_record.username),
        following=following_url(base, actor_record.username),
        icon_url=actor_record.icon_url,
        image_url=actor_record.banner_url,
        shared_inbox=shared_inbox_url(base),
        public_key=PublicKey(
            id=main_key_id(base, actor_record.username),
            owner=actor_url(base, actor_record.username),
            public_key_pem=actor_record.public_key_pem,
        ),
    )
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_activity = Activity(
        id=f"{actor.id}/updates/{uuid.uuid4().hex}",
        type="Update",
        actor=actor.id,
        object=actor.to_dict(),
        published=published,
        to=[AS_PUBLIC],
        cc=[followers_url(base, actor_record.username)],
    )

    followers = await repository.list_followers(actor_record.username)
    for follower_actor_id in followers:
        inbox = await resolve_actor_inbox(request, follower_actor_id)
        if inbox is None:
            logger.warning("No inbox known for %s; skipping profile-update delivery", follower_actor_id)
            continue
        try:
            await deliver_activity(
                request.app.state.http_client,
                inbox_url=inbox,
                activity=update_activity.to_dict(),
                key_id=main_key_id(base, actor_record.username),
                private_key_pem=actor_record.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver profile update to %s", follower_actor_id, exc_info=True)


async def deliver_to_actor_or_followers(
    request: Request, *, target_actor_id: str, activity: dict, key_id: str, private_key_pem: str
) -> None:
    """Deliver ``activity`` to ``target_actor_id`` -- or, if it turns out to
    be one of our OWN local actors (a reply or reaction whose target is
    another user on this same bridge, not a genuinely remote account), to
    every one of THEIR followers instead.

    A local actor has no fediverse inbox worth delivering to: self-POSTing
    to our own ``/inbox/{username}`` would just loop the activity back into
    our own inbound handler, which (correctly) refuses to do anything with
    it, since its ``actor`` is also one of our own local actors -- see
    ``resolve_and_invite_ghost``'s docstring for why ghosting a local actor
    is always refused. The local target already sees the reply/reaction
    live in Matrix regardless of any of this; what they'd otherwise miss
    entirely is it ever reaching THEIR remote followers, since nothing else
    in this flow would deliver it there. Best-effort per recipient
    throughout, like every other delivery in this bridge -- never raises,
    and callers should record whatever bookkeeping they need unconditionally
    afterward rather than gating it on a single success/failure the way a
    single-recipient delivery would.
    """
    config = request.app.state.config
    repository = request.app.state.repository
    base = config.bridge.public_base_url

    local_username = username_from_actor_url(base, target_actor_id)
    recipients = await repository.list_followers(local_username) if local_username is not None else [target_actor_id]

    for recipient_actor_id in recipients:
        inbox = await resolve_actor_inbox(request, recipient_actor_id)
        if inbox is None:
            logger.warning("No inbox known for %s; skipping delivery", recipient_actor_id)
            continue
        try:
            await deliver_activity(
                request.app.state.http_client,
                inbox_url=inbox,
                activity=activity,
                key_id=key_id,
                private_key_pem=private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver to %s", recipient_actor_id, exc_info=True)


async def _inviter_for_room(request: Request, room_id: str) -> str:
    """Whichever of our own identities actually has presence in ``room_id``
    and so can invite into it -- the bot for a linked Profile Room (invited
    there by its owner when they linked their profile, a precondition for
    that to have worked at all), or that Remote User Room's own ghost
    (which created it, so has full permissions there) for one of those."""
    repository = request.app.state.repository
    remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
    if remote_room is not None:
        return remote_room.ghost_user_id
    return _bot_mxid(request.app.state.config)


async def resolve_and_invite_ghost(request: Request, actor_id: str, room_id: str) -> tuple[str, dict] | None:
    """Provision a ghost for a remote actor (fetching their profile for
    name/avatar) and invite it into ``room_id``. Returns ``(ghost_mxid,
    actor_doc)``, or None if the actor's domain couldn't even be determined
    or ``actor_id`` turned out to be one of our OWN local actors.

    That local-actor case is a deliberate refusal, not an oversight: a ghost
    exists to give a genuinely remote fediverse account a controllable
    Matrix stand-in, since the bridge has no other way to act as them. A
    local actor already has a real Matrix account and room the bridge
    doesn't (and shouldn't) impersonate -- minting `@fedi_username_ourdomain`
    or similar for one would be a fake, parallel identity for someone who
    already has a real one, and the bridge has no authority to send
    messages "as" them or force-join their real account into anything the
    way it can for a ghost it fully controls. Every caller of this function
    mirrors/announces/reacts-to content on a ghost's behalf, which simply
    isn't a sensible operation for a local actor to begin with -- their own
    content already lives natively in their own Profile Room.

    Deliberately doesn't wait for or force the join itself -- the ghost's own
    join happens via the same AppService-transaction-driven auto-accept path
    used everywhere else a ghost/the bot is invited (see
    ``bridge.membership``), since Synapse notifies us of this invite the same
    way it would any other membership event for one of our namespace's users.
    Callers that need the ghost to be an actual (not just invited) member
    *immediately*, because they're about to send a message as it (or pill it)
    right away, should follow up with their own explicit
    ``synapse.join_room`` instead of relying on that async path.
    """
    config = request.app.state.config
    synapse = request.app.state.synapse
    http_client = request.app.state.http_client
    repository = request.app.state.repository

    if username_from_actor_url(config.bridge.public_base_url, actor_id) is not None:
        logger.info("Refusing to ghost %s -- it looks like one of our own local actors", actor_id)
        return None

    try:
        actor_doc = await fetch_actor(http_client, actor_id)
    except RemoteActorFetchError:
        actor_doc = {}

    domain = urlsplit(actor_id).hostname or ""
    if not domain:
        logger.info("Could not determine domain for actor %s; skipping ghost", actor_id)
        return None
    remote_username = actor_doc.get("preferredUsername") or actor_id.rstrip("/").rsplit("/", 1)[-1]
    display_name = actor_doc.get("name") or remote_username
    icon_url = extract_icon_url(actor_doc) if actor_doc else None
    localpart = ghost_localpart(config.appservice.user_prefix, remote_username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, remote_username, domain, config.synapse.server_name)

    try:
        await sync_ghost_profile(
            synapse,
            http_client,
            repository,
            server_name=config.synapse.server_name,
            localpart=localpart,
            actor_id=actor_id,
            display_name=display_name,
            icon_url=icon_url,
            handle=f"@{remote_username}@{domain}",
            tag=actor_doc.get("tag") if actor_doc else None,
        )
        inviter = await _inviter_for_room(request, room_id)
        await synapse.invite_user(room_id, mxid, as_user_id=inviter)
    except SynapseError:
        logger.info("Could not invite ghost %s into %s", mxid, room_id, exc_info=True)
    return mxid, actor_doc


def _mention_targets(note: dict) -> list[tuple[str, str, str]]:
    """Extract ``(username, domain, href)`` triples from a Note's ``tag``
    array's ``Mention`` entries -- ``username``/``domain`` parsed from the
    tag's own ``name`` (e.g. ``@alice@instance.social``), ``href`` is the
    actor IRI to actually resolve/ghost. ``username``/``domain`` are what
    end up in the pill lookup key (see
    ``bridge.activitypub.sanitize.mention_pill_key``) -- not ``href``,
    since that routinely does NOT match the ``href`` on the mention's own
    anchor in ``content`` for the very same account (see that function's
    docstring).

    ``name`` routinely omits the ``@domain`` suffix entirely for a mention
    of an account on the SAME server as the poster (observed live from
    poa.st: ``"@JoeBravo77"``, not ``"@JoeBravo77@poa.st"``, right next to
    a cross-server mention in the same post that DID carry one) -- falls
    back to ``href``'s own hostname in that case rather than dropping the
    mention, since that's always a real, resolvable domain regardless of
    what ``name`` chose to spell out."""
    targets = []
    for tag in note.get("tag") or []:
        if not isinstance(tag, dict) or tag.get("type") != "Mention":
            continue
        name, href = tag.get("name"), tag.get("href")
        if not isinstance(name, str) or not isinstance(href, str) or not href:
            continue
        username, _, domain = name.lstrip("@").partition("@")
        if not domain:
            domain = urlsplit(href).hostname or ""
        if username and domain:
            targets.append((username, domain, href))
    return targets


def note_mentions_local_actor(request: Request, note: dict) -> bool:
    """Cheap, no-network, no-DB pre-check: does ``note`` mention anyone
    whose ``href`` merely LOOKS like one of our own actor URLs (i.e.
    ``username_from_actor_url`` parses it)? Doesn't confirm that username
    is an actual currently-linked local actor -- ``resolve_mention_pills``
    does the real (DB-backed) resolution later and is what actually acts on
    it. This exists so a caller that would otherwise drop a post outright
    (e.g. ``_handle_create``, when nobody follows its author) can decide to
    process it anyway just for the mention, without needing to do that
    real resolution (and everything it costs -- ghost/room bootstrapping)
    up front merely to find out whether it's warranted."""
    config = request.app.state.config
    return any(
        username_from_actor_url(config.bridge.public_base_url, href) is not None
        for _username, _domain, href in _mention_targets(note)
    )


def note_is_direct_message(note: dict, *, extra_to: list[str] | None = None, extra_cc: list[str] | None = None) -> bool:
    """Whether ``note`` was addressed as a private/direct message rather
    than a public, unlisted, or followers-only post.

    AS2 defines a direct message purely by exclusion: it isn't addressed to
    the special ``Public`` collection (a public OR unlisted post always is,
    just swapped between ``to``/``cc``), and it isn't addressed to the
    author's own followers collection either (a followers-only post is) --
    what's left once both of those are ruled out is one or more specific
    accounts and only those, which is exactly what a direct message is.

    Detecting the followers collection is a heuristic -- matching a
    "/followers" URL segment, the shape essentially every real
    implementation (Mastodon, Pleroma, Akkoma, ...) uses -- rather than an
    authoritative fetch of the author's actor document, to avoid a network
    round-trip just to classify every single inbound Note. The heuristic is
    deliberately loose in the direction that matters: misclassifying a
    genuine direct message as more widely addressed would be a privacy
    leak, while misclassifying an ordinary post as a direct message just
    means it lands in a private 1:1 room instead of the shared one --
    unwanted, but not unsafe.

    ``extra_to``/``extra_cc`` let a caller also fold in the wrapping
    ``Create`` activity's own addressing (some implementations only set it
    there, not on the Note itself) without this needing to know about
    ``Activity`` at all.
    """
    addressed = [
        *(note.get("to") or []), *(note.get("cc") or []),
        *(extra_to or []), *(extra_cc or []),
    ]
    if not addressed:
        return False  # no explicit audience at all -- don't guess; same as today's behavior
    if AS_PUBLIC in addressed:
        return False
    return not any(isinstance(a, str) and "/followers" in a for a in addressed)


@dataclass
class MentionPills:
    """Result of ``resolve_mention_pills``. ``pills`` is what
    ``bridge.activitypub.sanitize.strip_to_matrix_message`` needs to
    actually render pills; ``mentioned_locals`` is every local actor found
    among the mentions (a subset -- ghosted remote mentions aren't
    included), for a caller to run ``notify_mentioned_locals`` with once
    the post mentioning them has actually been sent somewhere."""

    pills: dict[str, str] = field(default_factory=dict)
    mentioned_locals: list[ActorRecord] = field(default_factory=list)


async def resolve_mention_pills(
    request: Request, *, room_id: str, note: dict, allow_remote: bool = True
) -> MentionPills:
    """For each ``Mention`` on ``note``, resolve who to pill. Best-effort
    per mention: one that can't be resolved is just omitted, left as
    sanitize.py's plain-text fallback -- never blocks the post itself.

    A mention whose ``href`` is one of OUR OWN local actors (i.e. someone
    here mentioning another bridge user by their fediverse identity,
    ``@user@our-domain``) pills that person's real Matrix ID directly --
    never a ghost. A ghost exists to represent a genuinely remote account
    locally; minting one for a local user would just be a redundant, fake
    stand-in for an identity that already has a real Matrix account, and
    unlike a ghost we don't control, isn't ours to invite/join into
    arbitrary rooms, so this path skips that step entirely too (see
    ``notify_mentioned_locals`` for what happens instead). Only a truly
    remote mention resolves/joins a ghost, same as before.

    ``allow_remote``, if False, skips that ghost resolve/invite/join
    entirely for a genuinely remote mention (a local-actor mention is
    unaffected either way -- pilling one never touches ghosts/room
    membership at all) -- used by ``mirror_direct_message`` for a private
    1:1 DM room, where inviting some OTHER remote account's ghost in
    response to a mention merely appearing in the message text (as opposed
    to being one of the DM's own explicit recipients) would leak that
    room's membership beyond the two people it's actually between.
    """
    targets = _mention_targets(note)
    if not targets:
        return MentionPills()
    config = request.app.state.config
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    result = MentionPills()
    for username, domain, href in targets:
        key = mention_pill_key(hostname=domain, username=username)
        if key is None:
            continue

        local_username = username_from_actor_url(config.bridge.public_base_url, href)
        if local_username is not None:
            local_actor = await repository.get_local_actor(local_username)
            if local_actor is not None:
                result.pills[key] = local_actor.matrix_user_id
                result.mentioned_locals.append(local_actor)
            continue  # never ghost a local actor, resolved or not

        if not allow_remote:
            continue

        resolved = await resolve_and_invite_ghost(request, href, room_id)
        if resolved is None:
            continue
        ghost_mxid_, _actor_doc = resolved
        try:
            await synapse.join_room(room_id, as_user_id=ghost_mxid_)
        except SynapseError:
            logger.info("Could not join mentioned ghost %s into %s", ghost_mxid_, room_id, exc_info=True)
            continue
        result.pills[key] = ghost_mxid_
    return result


async def is_silenced(repository: ActorRepository, local_username: str, remote_actor_id: str) -> bool:
    """Whether ``local_username`` has blocked OR muted ``remote_actor_id``
    (see ``bridge.commands``'s ``block``/``mute`` commands) -- checked
    everywhere an inbound interaction would otherwise (a) send a
    notification about them into the Fediverse Notifications DM, or (b)
    auto-invite the local user into a room because of them (a fresh DM/Chat
    room, or being pulled into someone else's room over a mention) --
    see ``notify_mentioned_locals``/``ensure_ghost_dm_room``/
    ``ensure_ghost_chat_room`` below, and ``bridge.inbox_dispatch``'s own
    ``_announce_new_follower``/``_notify_post_owner``. A block implies
    everything a mute does on top of the stronger things only ``;block``
    itself does (cutting the follow relationship, kicking existing rooms,
    rejecting future Follows) -- this single combined check is what makes
    that "on top of" true without duplicating the mute logic for blocked
    accounts separately."""
    return await repository.is_blocked(local_username, remote_actor_id) or await repository.is_muted(
        local_username, remote_actor_id
    )


async def _recipient_silenced(request: Request, matrix_user_id: str, remote_actor_id: str) -> bool:
    """``is_silenced``, starting from a recipient's Matrix ID rather than
    their already-resolved username -- for ``ensure_ghost_dm_room``/
    ``ensure_ghost_chat_room``, which only ever have the former on hand.
    False if ``matrix_user_id`` isn't even a linked local actor (shouldn't
    normally happen -- both are only ever called for an actual bridge
    user -- but there's nothing to silence against otherwise)."""
    repository = request.app.state.repository
    local_actor = await repository.get_local_actor_by_matrix_id(matrix_user_id)
    if local_actor is None:
        return False
    return await is_silenced(repository, local_actor.username, remote_actor_id)


async def unfollow_remote_actor(request: Request, actor_record: ActorRecord, remote_room: RemoteActorRoom) -> None:
    """Send a signed ``Undo(Follow)`` as ``actor_record``'s own identity and
    drop their following relationship -- never the shared bridge/service
    identity, so this only affects the specific user unfollowing. Shared by
    ``bridge.membership`` (a human leaving/being kicked from a Remote User
    Room) and ``bridge.commands``'s ``block`` command (cutting an existing
    follow relationship as part of blocking someone) -- lives here rather
    than in either of those (see this module's own docstring) since both
    need it and importing one from the other would cycle."""
    repository = request.app.state.repository
    await repository.remove_following(actor_record.username, remote_room.actor_id)

    base = request.app.state.config.bridge.public_base_url
    undo = Activity(
        id=f"{actor_url(base, actor_record.username)}/undos/{uuid.uuid4().hex}",
        type="Undo",
        actor=actor_url(base, actor_record.username),
        object=Activity(
            id=f"{actor_url(base, actor_record.username)}/follows/{uuid.uuid4().hex}",
            type="Follow",
            actor=actor_url(base, actor_record.username),
            object=remote_room.actor_id,
        ),
    )
    try:
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=remote_room.inbox_url,
            activity=undo.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver Undo(Follow) to %s", remote_room.actor_id, exc_info=True)


async def _is_current_member(request: Request, *, room_id: str, matrix_user_id: str, as_user_id: str) -> bool:
    try:
        content = await request.app.state.synapse.get_room_state(
            room_id, "m.room.member", matrix_user_id, as_user_id=as_user_id
        )
    except SynapseError:
        return False
    return content.get("membership") == "join"


async def notify_mentioned_locals(
    request: Request, *, mentioned: list[ActorRecord], room_id: str, event_id: str, author_actor_id: str,
    respect_silence: bool = True,
) -> None:
    """For each local actor mentioned in the post that just became
    ``room_id``/``event_id`` (see ``resolve_mention_pills``, which already
    filtered these down to genuinely linked local identities -- an
    unlinked mention target simply can't be resolved to an ActorRecord at
    all, satisfying "only if they've created their profile via the bridge"
    on its own): if they're not ALREADY a member of ``room_id``, invite them
    there (so they can actually see themselves being talked about, not just
    get told about it blind) and notify them via DM pointing at it -- not
    their Profile Room, which other Matrix users may have been invited into;
    see ``bridge.notifications``. Both gated on that same "not already
    there" check -- if they're already in the room they'll see the mention
    live and don't need either. Best-effort throughout; never blocks the
    post itself.

    ``respect_silence`` (default True) additionally skips a mentioned local
    actor entirely if THEY have blocked/muted ``author_actor_id`` (see
    ``is_silenced``) -- an unsolicited mention from someone you've silenced
    shouldn't be what pulls you into a room with them. Only ``;dm``/``;chat``
    (the local user's own deliberate action, not an inbound interaction they
    didn't choose) pass ``False`` here.

    ``author_actor_id`` names who actually wrote the mentioning post, so
    the notice can say who mentioned them rather than just that a mention
    happened -- looked up against the ghost profile synced for that actor
    (always present by this point, since mirroring the post itself already
    required one) to show their ``@user@instance.org`` handle; falls back
    to the bare actor IRI on the (should-be-impossible) chance no profile
    is on file for them."""
    if not mentioned:
        return
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    as_user_id = await _inviter_for_room(request, room_id)
    link = matrix_to_link(room_id, event_id)
    author_profile = await repository.get_ghost_profile(author_actor_id)
    author_handle = (author_profile.handle if author_profile else None) or author_actor_id
    # A real pill (see notification_actor_html, same convention every other
    # notification in this bridge uses -- e.g. bridge.inbox_dispatch's own
    # "X liked/boosted your post") rather than the bare "@user@instance"
    # text this used to be stuck with -- falls back to plain escaped text
    # only if we somehow have no synced ghost profile/mxid for them at all,
    # which shouldn't normally happen (mirroring the mentioning post itself
    # already required one).
    author_html = (
        notification_actor_html(mxid=author_profile.mxid, handle=author_handle)
        if author_profile is not None and author_profile.mxid
        else html.escape(author_handle)
    )

    for actor_record in mentioned:
        if respect_silence and await is_silenced(repository, actor_record.username, author_actor_id):
            continue

        if await _is_current_member(
            request, room_id=room_id, matrix_user_id=actor_record.matrix_user_id, as_user_id=as_user_id
        ):
            continue

        try:
            await synapse.invite_user(room_id, actor_record.matrix_user_id, as_user_id=as_user_id)
        except SynapseError as exc:
            logger.info("Could not invite mentioned user %s to %s: %s", actor_record.matrix_user_id, room_id, exc)

        # No m.mentions/tagged mxid here -- see bridge.inbox_dispatch's
        # _notify_post_owner for why every notification in this room is
        # left unintentional, so the room's own notification setting (not
        # a forced per-message mention) decides whether it actually pings
        # anyone.
        await notify_user(
            request,
            matrix_user_id=actor_record.matrix_user_id,
            content={
                "msgtype": "m.text",
                "body": f"\U0001F514 You were mentioned in {author_handle}'s post: {link}",
                "format": "org.matrix.custom.html",
                "formatted_body": (
                    f"\U0001F514 You were mentioned in {author_html}'s post: "
                    f'<a href="{html.escape(link)}">{html.escape(link)}</a>'
                ),
            },
        )


async def provision_ghost(request: Request, actor_id: str) -> tuple[str, dict, str, str | None] | None:
    """Fetch and sync a ghost for ``actor_id`` -- returns ``(mxid,
    actor_doc, display_name, avatar_mxc)``, or None if ``actor_id``'s
    domain couldn't be determined, or it turned out to be one of our OWN
    local actors (see ``resolve_and_invite_ghost``'s docstring for why
    that's a deliberate refusal). Doesn't invite the ghost anywhere --
    callers that need it in a specific room do that themselves (see
    ``resolve_and_invite_ghost`` for the "invite into an existing room"
    case, ``ensure_ghost_dm_room``/``ensure_ghost_chat_room`` below for
    the "get-or-create a private 1:1 room" case)."""
    config = request.app.state.config
    synapse = request.app.state.synapse
    http_client = request.app.state.http_client
    repository = request.app.state.repository

    if username_from_actor_url(config.bridge.public_base_url, actor_id) is not None:
        logger.info("Refusing to ghost %s -- it looks like one of our own local actors", actor_id)
        return None

    try:
        actor_doc = await fetch_actor(http_client, actor_id)
    except RemoteActorFetchError:
        actor_doc = {}

    domain = urlsplit(actor_id).hostname or ""
    if not domain:
        logger.info("Could not determine domain for actor %s; skipping ghost", actor_id)
        return None
    username = actor_doc.get("preferredUsername") or actor_id.rstrip("/").rsplit("/", 1)[-1]
    display_name = actor_doc.get("name") or username
    icon_url = extract_icon_url(actor_doc) if actor_doc else None
    localpart = ghost_localpart(config.appservice.user_prefix, username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, username, domain, config.synapse.server_name)
    avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url) if icon_url else None

    await sync_ghost_profile(
        synapse, http_client, repository,
        server_name=config.synapse.server_name, localpart=localpart, actor_id=actor_id,
        display_name=display_name, icon_url=icon_url, handle=f"@{username}@{domain}",
        tag=actor_doc.get("tag") if actor_doc else None,
    )
    return mxid, actor_doc, display_name, avatar_mxc


async def ensure_ghost_dm_room(
    request: Request, *, actor_id: str, matrix_user_id: str, display_name: str, avatar_mxc: str | None, mxid: str,
    respect_silence: bool = True,
) -> str | None:
    """Get-or-create the 1:1 Note-based DM room (``ActorRepository.get_ghost_dm_room``)
    between the ghost for ``actor_id`` and ``matrix_user_id``, re-inviting
    them if they've since left an existing one -- see
    ``mirror_direct_message``'s docstring for the full naming/lifecycle
    reasoning, shared here with ``bridge.commands``' ``dm`` command (the
    other way this room gets created/reused, alongside an inbound direct
    message actually arriving). Returns None only if room creation itself
    failed, OR (``respect_silence``, default True) ``matrix_user_id`` has
    blocked/muted ``actor_id`` and no room exists yet -- never auto-create
    one just so a silenced account can start pulling them into it.
    ``bridge.commands``'s ``;dm`` command passes ``respect_silence=False``:
    that's the local user's own deliberate choice to reach out, not an
    inbound interaction they didn't choose, so it's never silenced."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)

    dm_room_id = await repository.get_ghost_dm_room(actor_id, matrix_user_id)
    if dm_room_id is None:
        if respect_silence and await _recipient_silenced(request, matrix_user_id, actor_id):
            return None
        try:
            dm_room_id = await synapse.create_room(
                as_user_id=mxid,
                name=f"{display_name} (DM)",
                invite=[matrix_user_id, bot_mxid],
                is_direct=True,
                avatar_mxc=avatar_mxc,
                preset="trusted_private_chat",
                # Knockable (not just invite-only) so the intended local
                # user can let themselves back in with just the room ID --
                # e.g. after a `;replace room` they can still see the old
                # room ID via the new room's predecessor pointer -- without
                # needing to run `;dm` again. See maybe_handle_knock's
                # _resolve_ghost_room_inviter for the acceptance side.
                join_rule=KNOCK_JOIN_RULE,
                # bot_mxid kept at the same level as the ghost creator,
                # regardless of room version -- see SynapseClient.create_room's
                # own additional_creators docstring for how it handles pre-v12
                # vs v12+ differently under the hood.
                additional_creators=[bot_mxid],
                room_type=SOCIAL_PROFILE_ROOM_TYPE,
            )
        except SynapseError:
            logger.warning("Could not create DM room for %s with %s", actor_id, matrix_user_id, exc_info=True)
            return None
        await repository.register_ghost_dm_room(actor_id, matrix_user_id, dm_room_id)
        await send_bridge_info(
            request, room_id=dm_room_id, actor_id=actor_id,
            display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
        )
        await add_bridge_widget(request, room_id=dm_room_id)
    else:
        # The room already existed -- but the recipient may have left it
        # since (e.g. after an earlier DM conversation wrapped up, or --
        # see ``;block`` -- was kicked from it). Without this, a later
        # message mirrored into the same room they're no longer in would
        # arrive with nothing to tell them it happened at all: no invite to
        # notice, no membership in the room to see it live in. Re-invite
        # them if they're not currently a member and haven't silenced
        # ``actor_id`` (a no-op either way if they're still there or already
        # have a pending invite) -- this is also what stops a blocked
        # account from just getting re-invited back into a room ``;block``
        # already kicked them from.
        if not await _is_current_member(
            request, room_id=dm_room_id, matrix_user_id=matrix_user_id, as_user_id=mxid
        ):
            if respect_silence and await _recipient_silenced(request, matrix_user_id, actor_id):
                return dm_room_id
            try:
                await synapse.invite_user(dm_room_id, matrix_user_id, as_user_id=mxid)
            except SynapseError:
                logger.info("Could not re-invite %s into %s", matrix_user_id, dm_room_id, exc_info=True)
    return dm_room_id


async def mirror_direct_message(
    request: Request, *, note: dict, author_actor_id: str, recipient_matrix_user_id: str
) -> FederatedEvent | None:
    """Mirror an inbound Note addressed as a private/direct message (see
    ``note_is_direct_message``) into a dedicated 1:1 Matrix DM room between
    the author's ghost and ``recipient_matrix_user_id`` -- never into that
    author's shared Remote User Room, and never into the recipient's own
    Profile Room even if the DM happens to be structured as an AP reply to
    something they posted there (a common way to start a DM on Mastodon:
    hitting "reply" and narrowing the audience down to just the person
    being replied to). Either would put a private conversation in a room
    other Matrix users may have been invited into.

    Named "{display name} (DM)" (distinct from that same author's Remote
    User Room, just "{display name}"), with the author's AP avatar and the
    same ``m.bridge``/``uk.half-shot.bridge`` state (see
    ``send_bridge_info``) as every other bridge-made room. The bot is
    invited and made admin alongside the ghost (which -- as the room's
    creator -- is admin by default). One DM room per (author, recipient)
    pair, created lazily and reused after
    (``ActorRepository.get_ghost_dm_room``), same lifecycle as
    ``bridge.notifications``'s bot DM rooms.

    Threads as a Matrix thread reply if this Note's ``inReplyTo`` names
    something we already track THAT LIVES IN THIS SAME DM ROOM (an earlier
    turn of the same DM conversation); anything else -- including a reply
    to a public post, which is how a DM conversation often starts -- is
    sent as a fresh top-level message in the DM room instead, since the
    "parent" it's structurally replying to doesn't actually belong to this
    private conversation.

    Returns the recorded ``FederatedEvent``, or None if the author's domain
    couldn't be determined or actually sending into Matrix failed.
    """
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    ap_object_id = note.get("id")
    existing = await repository.get_federated_event_by_ap_object(ap_object_id) if ap_object_id else None
    if existing is not None:
        return existing  # already mirrored -- e.g. a redelivered transaction

    provisioned = await provision_ghost(request, author_actor_id)
    if provisioned is None:
        logger.info("Could not provision a ghost for %s; dropping direct message", author_actor_id)
        return None
    mxid, actor_doc, display_name, avatar_mxc = provisioned

    dm_room_id = await ensure_ghost_dm_room(
        request, actor_id=author_actor_id, matrix_user_id=recipient_matrix_user_id,
        display_name=display_name, avatar_mxc=avatar_mxc, mxid=mxid,
    )
    if dm_room_id is None:
        return None

    in_reply_to_ap = note.get("inReplyTo")
    parent = await repository.get_federated_event_by_ap_object(in_reply_to_ap) if in_reply_to_ap else None
    thread_root_event_id: str | None = None
    relates_to: dict | None = None
    if parent is not None and parent.room_id == dm_room_id:
        thread_root_event_id = parent.thread_root_event_id or parent.event_id
        relates_to = {
            "rel_type": "m.thread",
            "event_id": thread_root_event_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": parent.event_id},
        }

    # allow_remote=False: never invite some OTHER remote account's ghost
    # into this private 1:1 room just because the DM's text happens to
    # mention them too -- see resolve_mention_pills's docstring. A mention
    # of the recipient themselves (the actual bug this fixes) still pills
    # correctly either way, since that branch never touches ghosts at all.
    mentions = await resolve_mention_pills(request, room_id=dm_room_id, note=note, allow_remote=False)
    plain, safe_html = strip_to_matrix_message(note.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, synapse, repository, safe_html, note.get("tag") or [], subject_id=ap_object_id
        )
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html and safe_html != plain:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html
    if mentions.mentioned_locals:
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}
    if relates_to is not None:
        message_content["m.relates_to"] = relates_to
    src = source_post_url(note)
    if src:
        message_content["external_url"] = src

    # Only the first attachment (if any) is embedded as real Matrix media --
    # see attach_media_to_content's docstring for why the rest are appended
    # as plain links instead of split into their own separate events: an
    # ActivityPub post always maps to exactly one Matrix event now.
    attachments = extract_attachments(note)
    message_content, _ = await attach_media_to_content(request, message_content, attachments)

    config = request.app.state.config
    max_clock_skew = config.federation.max_clock_skew
    max_backdate_days = config.federation.max_backdate_days
    try:
        event_id = await synapse.send_message_event(
            dm_room_id, message_content, as_user_id=mxid,
            ts=resolve_event_ts(note, max_clock_skew=max_clock_skew, max_backdate_days=max_backdate_days),
        )
    except SynapseError:
        logger.warning("Failed to mirror direct message from %s", author_actor_id, exc_info=True)
        return None

    new_event: FederatedEvent | None = None
    if ap_object_id:
        new_event = FederatedEvent(
            event_id=event_id, room_id=dm_room_id, ap_object_id=ap_object_id, author_actor_id=author_actor_id,
            thread_root_event_id=thread_root_event_id,
        )
        await repository.record_federated_event(new_event)

    return new_event


async def ensure_ghost_chat_room(
    request: Request, *, actor_id: str, matrix_user_id: str, display_name: str, avatar_mxc: str | None, mxid: str,
    respect_silence: bool = True,
) -> str | None:
    """The ``ChatMessage`` counterpart of ``ensure_ghost_dm_room`` --
    identical get-or-create-and-re-invite (and ``respect_silence``-gated)
    logic, against the SEPARATE ``ActorRepository.get_ghost_chat_room``
    table instead (see that method's docstring for why a DM room and a Chat
    room are never the same room), named "{display name} (Chat)" instead of
    "(DM)" so the two are distinguishable in a room list if someone has both
    with the same person. Returns None only if room creation itself failed,
    or (see ``ensure_ghost_dm_room``'s identical case) it was skipped
    because the recipient has silenced ``actor_id`` and no room exists yet."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)

    chat_room_id = await repository.get_ghost_chat_room(actor_id, matrix_user_id)
    if chat_room_id is None:
        if respect_silence and await _recipient_silenced(request, matrix_user_id, actor_id):
            return None
        try:
            chat_room_id = await synapse.create_room(
                as_user_id=mxid,
                name=f"{display_name} (Chat)",
                invite=[matrix_user_id, bot_mxid],
                is_direct=True,
                avatar_mxc=avatar_mxc,
                preset="trusted_private_chat",
                # Knockable -- see ensure_ghost_dm_room's identical reasoning.
                join_rule=KNOCK_JOIN_RULE,
                # bot_mxid kept at the same level as the ghost creator -- see
                # the identical reasoning in ensure_ghost_dm_room above.
                additional_creators=[bot_mxid],
            )
        except SynapseError:
            logger.warning("Could not create chat room for %s with %s", actor_id, matrix_user_id, exc_info=True)
            return None
        await repository.register_ghost_chat_room(actor_id, matrix_user_id, chat_room_id)
        await send_bridge_info(
            request, room_id=chat_room_id, actor_id=actor_id,
            display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
        )
        await add_bridge_widget(request, room_id=chat_room_id)
    else:
        # Same reasoning as ensure_ghost_dm_room: re-invite if they've left,
        # unless they've silenced actor_id -- also what stops a blocked
        # account from getting re-invited back into a room ;block already
        # kicked them from.
        if not await _is_current_member(
            request, room_id=chat_room_id, matrix_user_id=matrix_user_id, as_user_id=mxid
        ):
            if respect_silence and await _recipient_silenced(request, matrix_user_id, actor_id):
                return chat_room_id
            try:
                await synapse.invite_user(chat_room_id, matrix_user_id, as_user_id=mxid)
            except SynapseError:
                logger.info("Could not re-invite %s into %s", matrix_user_id, chat_room_id, exc_info=True)
    return chat_room_id


async def mirror_chat_message(
    request: Request, *, chat_message: dict, author_actor_id: str, recipient_matrix_user_id: str
) -> FederatedEvent | None:
    """Mirror an inbound ``ChatMessage`` (see ``Actor.accepts_chat_messages``
    and ``bridge.chat_bridge``) into a dedicated 1:1 Matrix room between the
    author's ghost and ``recipient_matrix_user_id`` -- the ``ChatMessage``
    counterpart of ``mirror_direct_message``, but deliberately simpler:
    Pleroma's own Chats are a flat, linear conversation with exactly one
    other party for the room's whole lifetime, not something with a reply
    tree to thread against the way a Note-based DM (routinely started as a
    reply to some public post) is -- so every message here is just sent
    fresh, no ``inReplyTo``/thread-relation handling at all.

    Returns the recorded ``FederatedEvent``, or None if the author's ghost
    couldn't be provisioned, the room couldn't be created, or actually
    sending into Matrix failed.
    """
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    ap_object_id = chat_message.get("id")
    existing = await repository.get_federated_event_by_ap_object(ap_object_id) if ap_object_id else None
    if existing is not None:
        return existing  # already mirrored -- e.g. a redelivered transaction

    provisioned = await provision_ghost(request, author_actor_id)
    if provisioned is None:
        logger.info("Could not provision a ghost for %s; dropping chat message", author_actor_id)
        return None
    mxid, _actor_doc, display_name, avatar_mxc = provisioned

    chat_room_id = await ensure_ghost_chat_room(
        request, actor_id=author_actor_id, matrix_user_id=recipient_matrix_user_id,
        display_name=display_name, avatar_mxc=avatar_mxc, mxid=mxid,
    )
    if chat_room_id is None:
        return None

    plain, safe_html = strip_to_matrix_message(chat_message.get("content") or "")
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html and safe_html != plain:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html

    attachments = extract_attachments(chat_message)
    message_content, _ = await attach_media_to_content(request, message_content, attachments)

    config = request.app.state.config
    max_clock_skew = config.federation.max_clock_skew
    max_backdate_days = config.federation.max_backdate_days
    try:
        event_id = await synapse.send_message_event(
            chat_room_id, message_content, as_user_id=mxid,
            ts=resolve_event_ts(chat_message, max_clock_skew=max_clock_skew, max_backdate_days=max_backdate_days),
        )
    except SynapseError:
        logger.warning("Failed to mirror chat message from %s", author_actor_id, exc_info=True)
        return None

    new_event: FederatedEvent | None = None
    if ap_object_id:
        new_event = FederatedEvent(
            event_id=event_id, room_id=chat_room_id, ap_object_id=ap_object_id, author_actor_id=author_actor_id,
        )
        await repository.record_federated_event(new_event)

    return new_event


@dataclass
class ImportedNote:
    """Result of ``import_note``. ``first_attachment_mxc`` is the ``mxc://``
    URI the note's first (only embedded -- see ``attach_media_to_content``)
    attachment ended up uploaded as, if it had one, and ``author_avatar_mxc``
    is the author's ghost/room avatar (if a new Remote User Room had to be
    created for them) -- both exposed so a caller that separately builds its
    own message referencing the same attachment/avatar (e.g.
    ``_handle_announce``'s repost summary card) can reuse them instead of
    re-uploading the same files to the homeserver."""

    federated_event: FederatedEvent | None
    first_attachment_mxc: str | None = None
    author_avatar_mxc: str | None = None


# Per-``ap_object_id`` locks so two concurrent ``import_note`` calls for the
# SAME post (observed live 2026-07-08: a followed account's own post arriving
# as its own top-level Create at the same time someone else's quote-post of
# it triggers _maybe_import_quoted_note to import that exact same post as
# the quote target) don't both pass the "not tracked yet" check before
# either has recorded it, each upload their own copy of any attachment, and
# both send a duplicate Matrix event. Self-cleaning (refcounted, popped once
# nothing's waiting on a given id) rather than left to grow forever -- entries
# only live as long as an import for that specific post is actually in
# flight. Only closes the race within this one process; record_federated_event
# still has a real partial-unique DB index as a backstop (see import_note's
# own handling of a lost race there) for the case this doesn't cover, e.g. a
# future multi-process deployment sharing one Postgres database.
_import_locks: dict[str, asyncio.Lock] = {}
_import_lock_waiters: dict[str, int] = {}


@asynccontextmanager
async def _import_lock(ap_object_id: str):
    _import_lock_waiters[ap_object_id] = _import_lock_waiters.get(ap_object_id, 0) + 1
    lock = _import_locks.setdefault(ap_object_id, asyncio.Lock())
    try:
        async with lock:
            yield
    finally:
        _import_lock_waiters[ap_object_id] -= 1
        if _import_lock_waiters[ap_object_id] <= 0:
            _import_lock_waiters.pop(ap_object_id, None)
            _import_locks.pop(ap_object_id, None)


async def import_note(
    request: Request, *, note: dict, author_actor_id: str, author_doc: dict, inviter: str | None = None
) -> ImportedNote:
    """Mirror ``note`` (an already-fetched, already-type-checked Note, with
    its author already resolved/fetched too) into a Remote User Room for
    ``author_actor_id`` -- creating that room (and a ghost for the author)
    first if it doesn't exist yet. Reuses, rather than re-imports, whatever
    already mirrors this exact post by any path (a follow, an earlier
    import, an earlier boost, ...). ``inviter``, if given, is best-effort
    invited into the room either way. ``ImportedNote.federated_event`` is
    None if the post has no usable ``id``, or actually sending it into
    Matrix failed outright.

    Shared by ``bridge.commands``'s ``import`` command and
    ``bridge.inbox_dispatch``'s ``Announce``/quote-post handling
    (automatically importing whatever a followed account boosts/reposts, or
    whatever an inbound quote-post's own target turns out to be). Doesn't
    handle reply-threading the way ``import`` does for its own case -- a
    caller that cares should check for a trackable parent first and only
    fall back to this for the plain-top-level-post case, same as ``import``
    itself does.

    Serializes concurrent calls for the SAME ``note`` (see ``_import_lock``)
    -- two different inbound deliveries can legitimately both want to
    import the exact same post at the same time (e.g. it arrives as its
    own top-level ``Create`` right as someone else's quote-post of it is
    ALSO independently resolving it as their quote target), and without
    this, both would pass the "not tracked yet" check before either
    finishes, each upload their own copy of any attachment, and both send
    a duplicate Matrix event for the same post -- confirmed live 2026-07-08."""
    ap_object_id = note.get("id")
    if not ap_object_id:
        # Nothing to key a lock (or future dedup lookups) on regardless.
        return await _import_note_locked(
            request, note=note, author_actor_id=author_actor_id, author_doc=author_doc, inviter=inviter
        )
    async with _import_lock(ap_object_id):
        return await _import_note_locked(
            request, note=note, author_actor_id=author_actor_id, author_doc=author_doc, inviter=inviter
        )


async def _import_note_locked(
    request: Request, *, note: dict, author_actor_id: str, author_doc: dict, inviter: str | None = None
) -> ImportedNote:
    """``import_note``'s actual body, run while holding that post's own
    ``_import_lock`` -- see ``import_note``'s own docstring for why."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    ap_object_id = note.get("id")
    existing = await repository.get_federated_event_by_ap_object(ap_object_id) if ap_object_id else None
    if existing is not None:
        if inviter:
            try:
                await synapse.invite_user(existing.room_id, inviter, as_user_id=bot_mxid)
            except SynapseError as exc:
                if exc.errcode != "M_FORBIDDEN":
                    logger.warning("Could not invite %s to %s: %s", inviter, existing.room_id, exc)
        return ImportedNote(federated_event=existing)

    if username_from_actor_url(config.bridge.public_base_url, author_actor_id) is not None:
        # A local actor's own post always gets a FederatedEvent recorded the
        # moment it's actually posted (see bridge.profile_posts), so the
        # dedup check above should already have caught this -- reaching here
        # means we somehow have no record of a post that claims to be ours.
        # Ghosting a local actor is never right regardless (see
        # resolve_and_invite_ghost's docstring for why); refuse rather than
        # minting a fake identity for someone who already has a real one.
        logger.info("Refusing to import %s as a Remote User Room -- it looks like a local actor", author_actor_id)
        return ImportedNote(federated_event=None)

    username = author_doc.get("preferredUsername") or author_actor_id.rstrip("/").rsplit("/", 1)[-1]
    domain = urlsplit(author_actor_id).hostname or ""
    if not domain:
        logger.info("Could not determine a domain for %s; dropping import", author_actor_id)
        return ImportedNote(federated_event=None)
    localpart = ghost_localpart(config.appservice.user_prefix, username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, username, domain, config.synapse.server_name)

    remote_room = await repository.get_remote_actor_room(author_actor_id)
    author_avatar_mxc: str | None = None
    if remote_room is None:
        display_name = author_doc.get("name") or username
        icon_url = extract_icon_url(author_doc)
        avatar_mxc = await fetch_and_upload_media(request.app.state.http_client, synapse, icon_url) if icon_url else None
        author_avatar_mxc = avatar_mxc
        banner_url = extract_banner_url(author_doc)
        banner_mxc = await fetch_and_upload_media(request.app.state.http_client, synapse, banner_url) if banner_url else None

        await ensure_ghost_user(
            synapse,
            server_name=config.synapse.server_name,
            localpart=localpart,
            display_name=display_name,
            avatar_mxc=avatar_mxc,
        )
        await resolve_and_persist_emoji(
            request.app.state.http_client, synapse, repository, display_name, author_doc.get("tag") or [], author_actor_id
        )
        await repository.record_ghost_profile(
            GhostProfile(
                actor_id=author_actor_id, display_name=display_name, icon_url=icon_url,
                mxid=mxid, handle=f"@{username}@{domain}",
            )
        )
        new_room_id = await synapse.create_room(
            as_user_id=mxid,
            name=display_name or f"{username}@{domain}",
            topic=author_doc.get("summary") or f"Fediverse posts from {username}@{domain}",
            # See _handle_follow's identical reasoning: the bot is invited
            # into every Remote User Room, not just its own ghost -- and
            # made admin there too (not just the ghost that created it), so
            # it can always assist with re-inviting people later.
            invite=[inviter, bot_mxid] if inviter else [bot_mxid],
            avatar_mxc=avatar_mxc,
            room_type=SOCIAL_PROFILE_ROOM_TYPE,
            join_rule=config.bridge.ghost_room_join_rule,
            # bot_mxid kept at the same level as the ghost creator -- see
            # ensure_ghost_dm_room's identical reasoning. events'
            # SOCIAL_PROFILE_USER_ID_STATE_TYPE override matches every
            # other Remote User Room creation path's identical reasoning.
            additional_creators=[bot_mxid],
            power_level_content_override={
                "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            },
        )
        remote_room = RemoteActorRoom(
            actor_id=author_actor_id,
            room_id=new_room_id,
            ghost_user_id=mxid,
            inbox_url=author_doc.get("inbox") or "",
            display_name=display_name,
            icon_url=icon_url,
            banner_url=banner_url,
        )
        await repository.register_remote_actor_room(remote_room)
        await send_bridge_info(
            request, room_id=new_room_id, actor_id=author_actor_id,
            display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
        )
        await add_bridge_widget(request, room_id=new_room_id)
        await set_ghost_profile_room_id(request, mxid=mxid, room_id=new_room_id)
        await set_profile_user_id(request, room_id=new_room_id, matrix_user_id=mxid, as_user_id=mxid)
        if banner_mxc:
            await set_ghost_room_banner(request, room_id=new_room_id, ghost_user_id=mxid, banner_mxc=banner_mxc)
    elif inviter:
        try:
            await synapse.invite_user(remote_room.room_id, inviter, as_user_id=remote_room.ghost_user_id)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", inviter, remote_room.room_id, exc)

    mentions = await resolve_mention_pills(request, room_id=remote_room.room_id, note=note)
    plain, safe_html = strip_to_matrix_message(note.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, synapse, repository, safe_html, note.get("tag") or [], subject_id=ap_object_id
        )
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html and safe_html != plain:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html
    if mentions.mentioned_locals:
        # A pill alone (the <a href="matrix.to/..."> above) only makes the
        # mention a clickable link -- an intentional mention (MSC3952) is
        # what actually highlights/notifies the tagged user's client.
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}
    src = source_post_url(note)
    if src:
        message_content["external_url"] = src

    # Only the first attachment (if any) is embedded as real Matrix media --
    # see attach_media_to_content's docstring for why the rest are appended
    # as plain links instead of split into their own separate events: an
    # ActivityPub post always maps to exactly one Matrix event now.
    attachments = extract_attachments(note)
    message_content, first_attachment_mxc = await attach_media_to_content(request, message_content, attachments)

    config = request.app.state.config
    max_clock_skew = config.federation.max_clock_skew
    max_backdate_days = config.federation.max_backdate_days
    try:
        event_id = await synapse.send_message_event(
            remote_room.room_id, message_content, as_user_id=remote_room.ghost_user_id,
            event_type=mirrored_post_event_type(config),
            ts=resolve_event_ts(note, max_clock_skew=max_clock_skew, max_backdate_days=max_backdate_days),
        )
    except SynapseError:
        logger.warning("Failed to import post from %s", author_actor_id, exc_info=True)
        return ImportedNote(
            federated_event=None, first_attachment_mxc=first_attachment_mxc, author_avatar_mxc=author_avatar_mxc
        )

    new_event: FederatedEvent | None = None
    if ap_object_id:
        new_event = FederatedEvent(
            event_id=event_id, room_id=remote_room.room_id, ap_object_id=ap_object_id, author_actor_id=author_actor_id,
        )
        try:
            await repository.record_federated_event(new_event)
        except Exception:
            # Lost a race _import_lock (see import_note's own docstring)
            # didn't catch -- e.g. a future multi-process deployment
            # sharing one Postgres database, where the real partial-unique
            # index on ap_object_id is the actual backstop. event_id above
            # has already been sent as a genuine duplicate of whatever won
            # that race; redact it and hand back the winner's own record
            # instead of leaving two live copies of the same post around.
            existing = await repository.get_federated_event_by_ap_object(ap_object_id)
            if existing is None:
                # Not actually a duplicate -- some other failure recording
                # this (a transient DB hiccup, say). The Matrix event
                # itself is real and otherwise fine; leave it unrecorded
                # rather than redact a message that isn't a duplicate of
                # anything.
                logger.warning("Failed to record federated event for %s", ap_object_id, exc_info=True)
            else:
                try:
                    await synapse.redact_event(
                        remote_room.room_id, event_id, reason="Duplicate import",
                        as_user_id=remote_room.ghost_user_id,
                    )
                except SynapseError:
                    logger.warning(
                        "Lost an import race for %s but could not redact the duplicate %s",
                        ap_object_id, event_id, exc_info=True,
                    )
                new_event = existing

    await notify_mentioned_locals(
        request,
        mentioned=mentions.mentioned_locals,
        room_id=remote_room.room_id,
        event_id=event_id,
        author_actor_id=author_actor_id,
    )

    return ImportedNote(
        federated_event=new_event, first_attachment_mxc=first_attachment_mxc, author_avatar_mxc=author_avatar_mxc
    )
