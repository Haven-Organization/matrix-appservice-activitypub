"""Dispatch logic for verified incoming ActivityPub activities.

Called from ``POST /inbox/{username}`` (or the shared inbox) once the HTTP
signature has been verified. ``username`` is whichever local actor the
activity was addressed to -- an individual user's own linked Profile Room
actor (which receives Follows, Likes, Announces, Undos and Deletes targeting
that user's own posts). Following a fediverse account is done under the
follower's own actor, not some shared bridge identity, so a ``Create`` from
an account someone here follows is mirrored as long as *any* local actor
still follows it (``ActorRepository.is_anyone_following``) -- not tied to
which specific inbox a given delivery happened to arrive at, since more than
one person can independently follow the same account and its server may
send a separate delivery per follower; only the first is actually mirrored
(deduped via the existing federated-event record), the rest are no-ops. A
new Follow (once accepted) posts a notice in the Profile Room and invites a
ghost for the follower into it, so the room's member list doubles as a
human-readable follower list -- the ghost's own join happens via the same
invite-auto-accept path used everywhere else (``bridge.membership``), not
here, so it isn't duplicated. Retried/duplicate Follows are deduped against
the existing follower list first, so this doesn't repeat on every resend.
``Update`` activities (a followed actor changing their name/avatar)
keep the Remote User Room's name and avatar, and the ghost's own Matrix
profile, in sync. A mirrored ``Create``'s attachments are downloaded and
re-uploaded to Synapse, then sent as their own proper Matrix media events
(``m.image``/``m.video``/``m.audio``/``m.file``), not just left as a link
in the text. A ``Create`` that's a reply to anything we already track (a
local user's own post or a followed account's post) is mirrored as a real
Matrix thread reply instead, from a ghost for the replier -- who we may not
follow or have any room for at all. If its ``inReplyTo`` isn't anything we
track directly, ``_resolve_ancestor_chain`` fetches ancestors one at a time
until it either reaches something we do track or gives up: this is what
lets a reply from an account nobody here follows still get bridged (a
followed account's later post replying to it is what triggers the fetch)
and what keeps a followed account's own multi-post threads correctly
threaded to each other even when a third party replied in between. Same as
``Like``/``EmojiReact`` reactions, mirrored as ``m.reaction``. Both directions of that are
undoable: an inbound ``Undo`` looks the reaction up by its own activity id
(via ``ActorRepository``'s reaction map) and redacts exactly that reaction,
never the post itself. ``Announce`` (a repost/boost) is its own thing, not
a reaction: it's mirrored into the booster's own Remote User Room as a
freshly-formatted repost message (fetching the boosted post and its
original author if we don't already have either), with the original
poster's avatar/name/handle in the HTML body, since that's what a boost
conventionally shows -- unlike a Like/EmojiReact, the boosted post may not
be anything we otherwise track at all, e.g. from an account nobody here
follows or has ever seen mentioned before. The boosted post is ALSO
actually imported (``bridge.note_mirroring.import_note`` -- the same shared
logic ``bridge.commands``'s ``import`` command uses), into a Remote User
Room for its own original author rather than the booster, so it's
independently navigable/reply-able and not just a one-off summary card; the
repost message links to it with a matrix.to URL.
"""

from __future__ import annotations

import html
import logging
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import Activity
from bridge.activitypub.remote_actor import (
    RemoteActorFetchError,
    extract_attachments,
    extract_banner_url,
    extract_icon_url,
    fetch_actor,
    resolve_actor_inbox,
)
from bridge.activitypub.sanitize import strip_to_matrix_message
from bridge.activitypub.urls import actor_url, main_key_id, username_from_actor_url
from bridge.matrix_links import matrix_to_link
from bridge.custom_emoji import emoji_img_html, inline_custom_emoji, resolve_custom_emoji_image
from bridge.media import fetch_and_upload_media, filename_with_extension
from bridge.ghosts import ghost_mxid
from bridge.notifications import notification_actor_html, notify_user
from bridge.note_mirroring import (
    SOCIAL_REPOST_OF_FIELD,
    actor_html_with_avatar,
    import_note,
    is_silenced,
    mirror_chat_message,
    mirror_direct_message,
    mirrored_post_event_type,
    note_is_direct_message,
    note_mentions_local_actor,
    notify_mentioned_locals,
    resolve_event_ts,
    resolve_mention_pills,
    thread_reply_relates_to,
)
from bridge.note_mirroring import attach_media_to_content as _attach_media_to_content
from bridge.note_mirroring import resolve_and_invite_ghost as _resolve_and_invite_ghost
from bridge.note_mirroring import set_ghost_room_banner as _set_ghost_room_banner
from bridge.note_mirroring import source_post_url as _source_post_url
from bridge.repository import ActorRecord, FederatedEvent, GhostProfile, ReactionRecord, RemoteActorRoom
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# Actor types we treat an `Update` activity's object as a profile change for,
# as opposed to e.g. an edited Note (Mastodon also sends `Update{object: Note}`
# for post edits, which this bridge doesn't otherwise support mirroring).
_ACTOR_TYPES = {"Person", "Service", "Application", "Group", "Organization"}


async def handle_activity(request: Request, username: str, activity: Activity) -> None:
    handler = _HANDLERS.get(activity.type)
    if handler is None:
        logger.info("No handler for activity type %s (from %s)", activity.type, activity.actor)
        return
    await handler(request, username, activity)


async def _handle_follow(request: Request, username: str, activity: Activity) -> None:
    """A remote actor wants to follow one of our local (Profile Room) actors."""
    repository = request.app.state.repository
    record = await repository.get_local_actor(username)
    if record is None:
        return

    if await repository.is_blocked(username, activity.actor):
        # ";block"'s own "decline any follow requests from that person" --
        # a real Reject, not silence, so their own client shows the follow
        # as rejected rather than stuck pending forever. Never recorded as
        # a follower, never invited/announced.
        await _reject_blocked_follow(request, username=username, record=record, activity=activity)
        return

    already_following = activity.actor in await repository.list_followers(username)
    await repository.add_follower(username, activity.actor)

    config = request.app.state.config
    base = config.bridge.public_base_url
    accept = Activity(
        id=f"{actor_url(base, username)}/accepts/{uuid.uuid4().hex}",
        type="Accept",
        actor=actor_url(base, username),
        object=activity.to_dict(),
    )
    try:
        inbox = await _resolve_inbox(request, activity.actor)
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=inbox,
            activity=accept.to_dict(),
            key_id=main_key_id(base, username),
            private_key_pem=record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver Accept to %s", activity.actor, exc_info=True)

    # Only announce genuinely new followers -- a retried/duplicate Follow
    # (some implementations resend if they never saw our Accept) shouldn't
    # spam the room or re-invite a ghost that's already sitting in it.
    if not already_following and record.room_id:
        await _announce_new_follower(request, record, activity.actor)


async def _reject_blocked_follow(
    request: Request, *, username: str, record: ActorRecord, activity: Activity
) -> None:
    """Sends a signed ``Reject`` in place of ``_handle_follow``'s normal
    ``Accept`` -- see ``bridge.commands``'s ``block`` command for the full
    policy this is one piece of ("decline any follow requests from that
    person"). A real Reject rather than silence so their own client shows
    the follow as rejected instead of stuck pending forever; never records
    them as a follower, never invites a ghost, never announces anything."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    reject = Activity(
        id=f"{actor_url(base, username)}/rejects/{uuid.uuid4().hex}",
        type="Reject",
        actor=actor_url(base, username),
        object=activity.to_dict(),
    )
    try:
        inbox = await _resolve_inbox(request, activity.actor)
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=inbox,
            activity=reject.to_dict(),
            key_id=main_key_id(base, username),
            private_key_pem=record.private_key_pem,
        )
    except DeliveryError:
        logger.warning("Failed to deliver Reject to %s", activity.actor, exc_info=True)


async def _announce_new_follower(request: Request, record: ActorRecord, follower_actor_id: str) -> None:
    """Invite a ghost for the new follower into the Profile Room -- its
    member list then doubles as a human-readable follower list -- and notify
    the owner via DM (not the Profile Room itself, which other Matrix users
    may have been invited into; see ``bridge.notifications``). Best-effort
    throughout: a failure here shouldn't affect the Follow/Accept handshake,
    which has already happened by the time this runs.

    The ghost is invited regardless of ``;mute`` -- muting only ever
    suppresses notifications/auto-invites TOWARD the muting user (see
    ``is_silenced``), never who gets invited into a room they already own --
    but the "so-and-so is now following you" DM below is skipped if the
    owner has muted (or blocked, though a blocked follower's Follow was
    already rejected above ``_handle_follow`` and never reaches here at
    all) this follower."""
    resolved = await _resolve_and_invite_ghost(request, follower_actor_id, record.room_id)
    if resolved is None:
        return
    mxid, actor_doc = resolved

    if await is_silenced(request.app.state.repository, record.username, follower_actor_id):
        return

    domain = urlsplit(follower_actor_id).hostname or ""
    remote_username = actor_doc.get("preferredUsername") or follower_actor_id.rstrip("/").rsplit("/", 1)[-1]
    handle = f"@{remote_username}@{domain}"
    actor_html = notification_actor_html(
        mxid=mxid, handle=handle, display_name=actor_doc.get("name") or remote_username
    )

    await notify_user(
        request,
        matrix_user_id=record.matrix_user_id,
        # m.text, not m.notice -- every notification sent into the
        # Fediverse Notifications room is a regular message (see
        # bridge.notifications' module docstring), both for a consistent
        # look and because m.notice is invisible to a room's own "All
        # messages" notification setting (Matrix's default push rules
        # unconditionally suppress it before that setting is even
        # consulted). "on the fediverse" is deliberately left off here --
        # redundant once it's already sitting in a room named "Fediverse
        # Notifications".
        content={
            "msgtype": "m.text",
            "body": f"\U0001F464 {handle} is now following you.",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>\U0001F464 {actor_html} is now following you.</p>",
        },
    )


_PREVIEW_TEXT_LIMIT = 200
# Both are just size hints for whatever renders the notification --
# Synapse still holds the original full-size media; this doesn't request or
# generate an actual separate thumbnail asset, just tells the client not to
# blow it up to full size and swamp the room. A video needs more room to
# have usable-looking controls than a photo just needs to be glanceable at,
# hence the larger of the two.
_PREVIEW_IMAGE_SIZE = 96
_PREVIEW_VIDEO_MAX_DIMENSION = 240


def _scaled_dimensions(width: int | None, height: int | None, max_dimension: int) -> tuple[int, int]:
    """Scale ``(width, height)`` down to fit within ``max_dimension`` on
    its longer side, preserving aspect ratio. Falls back to a
    ``max_dimension`` square if the original dimensions are missing or
    non-positive (some implementations omit ``info.w``/``info.h``
    entirely)."""
    if not width or not height or width <= 0 or height <= 0:
        return max_dimension, max_dimension
    if width >= height:
        return max_dimension, round(height * max_dimension / width)
    return round(width * max_dimension / height), max_dimension


async def _fetch_post_preview(
    request: Request, target: FederatedEvent
) -> tuple[str, dict[str, object], dict[str, object] | None, dict[str, object] | None]:
    """Best-effort fetch of the Matrix event ``target`` mirrors, reduced to
    ``(truncated_text, full_content, image_or_None, video_or_None)`` for a
    compact notification preview (``truncated_text``) plus, separately,
    the event's complete, untouched ``content`` dict (``full_content``)
    for a caller that needs the real thing instead of a text-only
    preview -- e.g. ``m.social.repost_of.content`` (see
    ``_quoted_post_render``), which MSC4501 wants as a genuine full copy
    of whatever the reposted post's own event content actually was
    (``msgtype``, ``url``, ``info``, and all -- not just its ``body``,
    which is blank for an uncaptioned image/video and would otherwise
    make ``repost_of`` look broken for exactly the posts a snippet can't
    represent anyway). Needed because the notification itself is sent
    into a different room entirely (the bot's DM with the post's owner)
    than the one the actual post event lives in, so it can't just be
    quoted the way an ordinary same-room Matrix reply would be -- this
    fetches and condenses it by hand instead. Returns ``("", {}, None,
    None)`` if the event can't be fetched at all (e.g. it's since been
    redacted).

    ``image``/``video``, when present, are each ``{"mxc", "mimetype",
    "width", "height", "filename"}`` -- already scaled down to a small
    preview size (see ``_scaled_dimensions``) rather than the post's own
    real dimensions, so a follow-up ``m.image``/``m.video`` event built
    from either (see ``_notify_post_owner``) renders compact instead of
    full-size. Both are real attachments rather than an inline ``<img>``
    in the caption's HTML -- Element X doesn't render one at all, only
    ever showing the filename text next to nothing (observed live), and
    ``video`` was never an option in the first place, since ``<video>``
    isn't among the tags Matrix's own HTML sanitization rules allow
    inside a ``formatted_body`` on ANY client. Audio and file posts get no
    inline preview at all -- only their (already fetched, above)
    filename-as-body -- since there's nothing to shrink down and show at
    a glance the way an image or video frame is."""
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    try:
        event = await request.app.state.synapse.get_event(target.room_id, target.event_id, as_user_id=bot_mxid)
    except SynapseError:
        logger.info(
            "Could not fetch %s in %s for a notification preview", target.event_id, target.room_id, exc_info=True
        )
        return "", {}, None, None

    content = event.get("content") or {}
    full_content = content
    raw_body = (content.get("body") or "").strip()
    event_filename = (content.get("filename") or "").strip()
    msgtype = content.get("msgtype")
    info = content.get("info") or {}

    # An image/video's `body` is a genuine caption only when a separate
    # `filename` exists and differs from it (MSC2530); otherwise body IS
    # the filename -- not post text, so previewing it as such would
    # blockquote a filename. Confirmed live the other way around too
    # (2026-07-03): a CAPTIONED image's caption was being used as the
    # preview attachment's filename (no image extension -> Element renders
    # a file link instead of embedding) while the caption itself went
    # missing from the notification entirely. m.audio/m.file keep their
    # filename-as-body text: they get no media attachment preview below,
    # so that text is the only hint of what the post was.
    body = raw_body
    if msgtype in ("m.image", "m.video") and (not event_filename or raw_body == event_filename):
        body = ""
    if len(body) > _PREVIEW_TEXT_LIMIT:
        body = body[:_PREVIEW_TEXT_LIMIT].rstrip() + "…"

    image: dict[str, object] | None = None
    if msgtype == "m.image" and content.get("url"):
        width, height = _scaled_dimensions(info.get("w"), info.get("h"), _PREVIEW_IMAGE_SIZE)
        image_mimetype = info.get("mimetype") or "image/jpeg"
        image = {
            "mxc": content["url"],
            "mimetype": image_mimetype,
            "width": width,
            "height": height,
            # The mirrored event's own real filename, when it has one. The
            # body-derived fallback needs filename_with_extension (see its
            # docstring): alt-text bodies have no extension, and Element
            # and other clients need one to render inline rather than as
            # a bare file/filename link.
            "filename": event_filename or filename_with_extension(raw_body or "image", image_mimetype),
        }

    video: dict[str, object] | None = None
    if msgtype == "m.video" and content.get("url"):
        width, height = _scaled_dimensions(info.get("w"), info.get("h"), _PREVIEW_VIDEO_MAX_DIMENSION)
        video_mimetype = info.get("mimetype") or "video/mp4"
        video = {
            "mxc": content["url"],
            "mimetype": video_mimetype,
            "width": width,
            "height": height,
            "filename": event_filename or filename_with_extension(raw_body or "video", video_mimetype),
        }

    return body, full_content, image, video


def build_preview_media_content(
    *, plain_body: str, formatted_caption: str,
    preview_image: dict[str, object] | None, preview_video: dict[str, object] | None,
) -> dict:
    """Builds the actual event content for a notice that includes a small
    preview of some other post (a boost/repost echo, or a "your post was
    liked" notification) -- shared by ``_notify_post_owner``,
    ``bridge.commands._handle_repost``'s and ``bridge.reaction_bridge.
    send_boost``'s own profile-room echoes, all three of which used to
    embed the preview image inline in the caption's HTML instead.

    Sends a real ``m.image``/``m.video`` attachment (reusing the previewed
    post's own ``mxc://``, scaled down -- see ``_fetch_post_preview``) with
    ``plain_body``/``formatted_caption`` as an MSC2530 caption when there's
    a preview image or video, rather than folding it into the caption's own
    HTML: Element X doesn't render an inline ``<img>`` in a message body at
    all (observed live -- see ``_fetch_post_preview``'s docstring), so
    that's the only rendering that reliably works everywhere. Falls back to
    a plain ``m.text`` when there's no media preview (just text, or nothing
    at all) -- nothing to attach, so there's no reason to be anything but
    the plainest event type."""
    preview_media = preview_video or preview_image
    if preview_media is None:
        return {
            "msgtype": "m.text",
            "body": plain_body,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_caption,
        }
    return {
        "msgtype": "m.video" if preview_video is not None else "m.image",
        "body": plain_body,
        "filename": preview_media["filename"],
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_caption,
        "url": preview_media["mxc"],
        "info": {
            "mimetype": preview_media["mimetype"],
            "w": preview_media["width"],
            "h": preview_media["height"],
        },
    }


def merge_quote_preview_attachment(
    content: dict, *, preview_image: dict[str, object] | None, preview_video: dict[str, object] | None,
) -> dict:
    """If ``content`` (a quote-post's own mirrored Matrix event, already
    built by ``_handle_create`` -- caption text/HTML, ``m.mentions``,
    ``external_url``, and whatever ``_attach_media_to_content`` did with
    the quoting post's OWN attachments, if it had any) is still a plain
    ``m.text``, upgrades it to a real ``m.image``/``m.video`` attachment
    for the QUOTED post's preview media instead -- same reasoning as
    ``build_preview_media_content`` (Element X doesn't render an inline
    ``<img>`` in a caption at all), just merged into an already-built
    content dict rather than one built fresh, since this one has other
    fields on it worth keeping.

    A quoting post that already has its own attachment is left exactly as
    ``_attach_media_to_content`` built it: one Matrix event can only ever
    carry one real attachment, and the quoter's own is what they actually
    posted, so it always wins over the thing they merely quoted -- the
    quoted post's preview stays text-only (see ``_quoted_post_render``'s
    ``quote_block_html``) in that case, same as it's always been."""
    preview_media = preview_video or preview_image
    if content.get("msgtype") != "m.text" or preview_media is None:
        return content
    return {
        **content,
        "msgtype": "m.video" if preview_video is not None else "m.image",
        "filename": preview_media["filename"],
        "url": preview_media["mxc"],
        "info": {
            "mimetype": preview_media["mimetype"],
            "w": preview_media["width"],
            "h": preview_media["height"],
        },
    }


async def _notify_post_owner(
    request: Request, *, target: FederatedEvent, actor_id: str, actor_doc: dict, verb_phrase: str,
    reaction_emoji: str | None = None, reaction_emoji_mxc: str | None = None,
) -> None:
    """If ``target`` is a post authored by a local user's own linked
    identity (i.e. it's genuinely *their* post), notify them via DM (not
    their Profile Room -- see ``bridge.notifications``) when someone on the
    fediverse interacts with it, with "your post" itself an event pill
    (a matrix.to link straight back to it -- see ``matrix_to_link``) rather
    than a separate link line. A no-op for a post mirrored into a Remote
    User Room instead (someone else's post, which nobody here owns).
    Best-effort -- same reasoning as ``_announce_new_follower``.

    ``reaction_emoji``/``reaction_emoji_mxc`` (only set by
    ``_handle_like_or_emoji_react``, for an actual reaction -- i.e.
    ``activity.content`` was present, not a plain heart-less ``Like``) show
    up TWICE: once as the notification's leading type emoji (in place of a
    generic one -- the literal unicode character, or the resolved
    custom-emoji image when ``_resolve_custom_emoji_image`` found one), and
    again inline within "with X" at the end of the sentence, same as
    before -- just without the shortcode repeated as separate text next to
    that second image, since Element X already surfaces it via the
    ``alt``/``title`` text. See the inline comments where they're used for
    why.

    Deliberately resolved via ``target.author_actor_id`` (stable across a
    ``replace room``) rather than ``target.room_id``: a replace re-points
    the actor's ``room_id`` at a freshly-created room, but the ORIGINAL
    post's Matrix event can't move with it -- it's still sitting in the old
    room, which is where ``target.room_id``/``event_id`` (and so the "your
    post" pill) still correctly point. Resolving the owner via
    ``target.room_id`` instead would silently find nobody the moment a post
    made before a replace gets liked/boosted after one, since no actor's
    ``room_id`` matches the old room anymore.

    Never ``m.notice`` (see ``_announce_new_follower``'s identical
    reasoning), and deliberately WITHOUT an ``m.mentions`` intentional
    mention or the owner's own mxid tagged into the body: that would force
    a notification regardless of how they've configured this room's own
    notification setting (e.g. Element's "All messages" / "Mentions &
    Keywords" / "Off"), overriding a choice that setting exists
    specifically to let them make. Letting the room setting alone decide is
    exactly the point of every notification in here being an ordinary
    message rather than something that fights past it.

    Names the reactor/booster via a user pill (see
    ``notification_actor_html`` -- Element renders its own avatar for one,
    so nothing separate is fetched/embedded for it here), and a compact
    preview of the post itself (see ``_fetch_post_preview``) -- truncated
    text, or a small image/video if the post itself was one, deliberately
    kept small (see ``_PREVIEW_IMAGE_SIZE``/``_PREVIEW_VIDEO_MAX_DIMENSION``)
    so a notification doesn't dominate the room the way embedding the post
    at full size would.

    An image or video preview is still ONE event, same as everything else
    here, but built differently to get there: the event ITSELF is
    ``m.image``/``m.video`` (reusing the exact same ``mxc://`` the original
    post's own media already lives at, no re-upload needed, just scaled
    down via a small ``info.w``/``info.h`` rather than the media's real
    dimensions) with a ``filename`` distinct from its
    ``body``/``formatted_body`` -- the caption convention (MSC2530, now
    part of the spec) essentially every mainstream client already renders
    as a normal rich caption ABOVE/BELOW the media, avatar and all, exactly
    like the ``m.text`` case's own formatting. This is the same convention
    ``bridge.note_mirroring.merge_attachment_into_content`` already relies
    on for an ordinary mirrored post with both text and an attachment --
    and, for an image specifically, NOT optional the way it might look:
    Element X doesn't render an inline ``<img>`` inside a message body's
    HTML at all (observed live -- it shows only the filename text next to
    nothing), so an image preview embedded that way is invisible on it
    regardless of blockquote styling; a real ``m.image`` attachment is the
    only rendering that works everywhere. ``video`` was never an option to
    inline in the first place, since ``<video>`` isn't among the tags
    Matrix's own HTML sanitization rules allow inside a ``formatted_body``
    on ANY client."""
    config = request.app.state.config
    repository = request.app.state.repository

    username = username_from_actor_url(config.bridge.public_base_url, target.author_actor_id or "")
    if username is None:
        return  # not a local actor's own post -- nothing to notify
    owner = await repository.get_local_actor(username)
    if owner is None or not owner.room_id:
        return  # no linked Profile Room -- e.g. the bridge's own service actor, never a real Matrix user to DM
    if await is_silenced(repository, owner.username, actor_id):
        return  # ;mute/;block -- no notification about this specific actor's interactions

    domain = urlsplit(actor_id).hostname or ""
    reactor_username = actor_doc.get("preferredUsername") or actor_id.rstrip("/").rsplit("/", 1)[-1]
    handle = f"@{reactor_username}@{domain}"
    mxid = ghost_mxid(config.appservice.user_prefix, reactor_username, domain, config.synapse.server_name)
    link = matrix_to_link(target.room_id, target.event_id)

    preview_text, _full_content, preview_image, preview_video = await _fetch_post_preview(request, target)
    preview_media = preview_video or preview_image

    # preview_text alongside preview_media means the post had BOTH media
    # and a real caption (_fetch_post_preview only returns caption-worthy
    # text for media posts) -- show both: the attachment previews the
    # media, the blockquote quotes the caption. For an uncaptioned
    # image/video, preview_text is already empty and the attachment alone
    # is the whole preview.
    # Every notification leads with an emoji identifying its type
    # (user-requested, 2026-07-04, to make types scannable at a glance):
    # \U0001F44D like, \U0001F501 boost -- and for an actual reaction (not a
    # plain heart-less Like), the reaction itself (its literal unicode
    # character, or its resolved custom-emoji image -- see type_emoji_html
    # below) rather than a generic reaction emoji, so the notification shows
    # what someone reacted with instead of making the reader open the
    # linked post to find out. Follows (\U0001F464) and mentions
    # (\U0001F514) get theirs at their own send sites.
    type_emoji = (
        "\U0001F501" if verb_phrase.startswith("boosted")
        else reaction_emoji if verb_phrase.startswith("reacted") and reaction_emoji
        else "\U0001F44D"
    )
    body = f"{type_emoji} {handle} {verb_phrase}"
    if preview_text:
        body += f"\n> {preview_text}"
    body += f"\n{link}"

    actor_html = notification_actor_html(
        mxid=mxid, handle=handle, display_name=actor_doc.get("name") or reactor_username
    )
    quote_html = ""
    if preview_text:
        quote_html = f"<blockquote>{html.escape(preview_text)}</blockquote>"

    # "your post" is a literal substring of every verb_phrase this is ever
    # called with ("liked your post", "reacted to your post with X",
    # "boosted your post") -- turned into an event pill (a matrix.to link
    # straight to the post, Matrix's own convention for pilling a specific
    # event -- see matrix_to_link) in place of the separate link line this
    # used to end with, so following it back to the post is one click on
    # the sentence itself rather than a whole extra line below.
    post_pill_html = f'<a href="{html.escape(link, quote=True)}">your post</a>'
    verb_phrase_html = html.escape(verb_phrase).replace("your post", post_pill_html)

    # A custom-emoji reaction gets its image inlined right next to its own
    # shortcode text within "with X" -- but, unlike emoji_img_html's
    # default, WITHOUT repeating the shortcode as separate text there:
    # Element X already surfaces it via the <img>'s own alt/title text
    # (observed live), so spelling it out again would just be noise. mxc://
    # is used directly (not the public /media/ proxy -- that's only for the
    # anonymous-HTTP AP surface) since Matrix clients resolve it via their
    # own authenticated media API.
    if reaction_emoji_mxc and reaction_emoji:
        verb_phrase_html = verb_phrase_html.replace(
            html.escape(reaction_emoji), emoji_img_html(reaction_emoji, reaction_emoji_mxc, with_text=False)
        )

    # Every notification also LEADS with an emoji identifying its type (see
    # type_emoji above) -- for an actual reaction this is the same image/
    # character as the "with X" above, just repeated at the front so the
    # type is scannable without reading the sentence.
    type_emoji_html = (
        emoji_img_html(reaction_emoji, reaction_emoji_mxc, with_text=False)
        if reaction_emoji_mxc and reaction_emoji
        else html.escape(type_emoji)
    )

    formatted_caption = f"<p>{type_emoji_html} {actor_html} {verb_phrase_html}</p>{quote_html}"

    content = build_preview_media_content(
        plain_body=body, formatted_caption=formatted_caption,
        preview_image=preview_image, preview_video=preview_video,
    )
    await notify_user(request, matrix_user_id=owner.matrix_user_id, content=content)


async def _handle_accept(request: Request, username: str, activity: Activity) -> None:
    """A remote actor accepted a Follow that one of our local actors sent them."""
    inner = activity.object
    target_actor_id = inner.get("object") if isinstance(inner, dict) and inner.get("type") == "Follow" else None
    if isinstance(target_actor_id, dict):
        target_actor_id = target_actor_id.get("id")
    if not target_actor_id:
        target_actor_id = activity.actor  # fall back: trust the sender as the accepted actor
    await request.app.state.repository.add_following(username, target_actor_id)


async def _handle_undo(request: Request, username: str, activity: Activity) -> None:
    repository = request.app.state.repository

    # Try the undone activity's own id against the reaction map first --
    # covers Like/EmojiReact regardless of whether the sender embedded the
    # full activity or just referenced it by id (in which case we have no
    # `type` to switch on at all, but don't need one: reactions are looked
    # up by id alone).
    inner_id = activity.object_id()
    if inner_id:
        reaction = await repository.get_reaction_by_activity_id(inner_id)
        if reaction is not None:
            await _redact_reaction(request, reaction)
            return
        # An Undo(Announce) references the Announce activity's own id -- if
        # we mirrored it as a repost message (_handle_announce), reuse the
        # same lookup+redact helper as Delete, since "remove our mirror of
        # this AP object" is exactly the same operation either way.
        if await repository.get_federated_event_by_ap_object(inner_id) is not None:
            await _redact_for_ap_object(request, inner_id, reason="Undo Announce", actor_id=activity.actor)
            return

    inner = activity.object
    inner_type = inner.get("type") if isinstance(inner, dict) else None
    if inner_type == "Follow":
        await repository.remove_follower(username, activity.actor)


async def _resolve_dm_recipient(request: Request, username: str, obj: dict, activity: Activity) -> ActorRecord | None:
    """The local actor a direct message is actually addressed to.

    ``username`` (whichever local actor's own per-actor inbox this arrived
    at, from ``POST /inbox/{username}``) is authoritative when present and
    resolves. But a ``Create`` delivered to the SHARED inbox instead never
    gets a ``username`` at all (``_resolve_shared_inbox_target`` only
    resolves one for Follow/Accept/Reject/Undo -- everything else normally
    routes purely off ``activity.actor``, which works for an ordinary
    public post but says nothing about who a DM is actually FOR) -- so as a
    fallback, scan the Note's own (and the wrapping Create's) ``to``/``cc``
    for a URL that resolves to one of our own local actors."""
    repository = request.app.state.repository
    if username:
        record = await repository.get_local_actor(username)
        if record is not None:
            return record

    base = request.app.state.config.bridge.public_base_url
    addressed = [*(obj.get("to") or []), *(obj.get("cc") or []), *(activity.to or []), *(activity.cc or [])]
    for target in addressed:
        if not isinstance(target, str):
            continue
        local_username = username_from_actor_url(base, target)
        if local_username is None:
            continue
        record = await repository.get_local_actor(local_username)
        if record is not None:
            return record
    return None


_QUOTE_FIELDS = ("quoteUri", "quoteUrl", "_misskey_quote")


def _extract_quote_uri(note: dict) -> str | None:
    """The URI of the post ``note`` quotes/reposts, if any -- checked under
    every field name a real implementation might send it as (see
    ``bridge.activitypub.models.Note.quote_uri``'s docstring for why
    there's more than one)."""
    for field_name in _QUOTE_FIELDS:
        value = note.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _strip_quote_tail(body: str, quote_uri: str) -> str:
    """Drop a trailing line that's just the quoted post's own link -- e.g.
    Akkoma appends a plain-text "RT: <link>" fallback to a quote-post's OWN
    content for receivers that don't understand ``quoteUri``, which reads
    as redundant clutter once ``_quoted_post_render`` is rendering our own
    proper quote line/preview from ``quote_uri`` anyway. A no-op if the
    body doesn't actually end with it (some implementations don't add a
    fallback at all)."""
    lines = body.rstrip().split("\n")
    if lines and lines[-1].strip().endswith(quote_uri):
        lines.pop()
    return "\n".join(lines).rstrip()


def _strip_quote_tail_html(safe_html: str, quote_uri: str) -> str:
    """HTML counterpart of ``_strip_quote_tail`` -- the same fallback is
    typically wrapped in markup here (``<br>``s, a ``<span>``, ...) rather
    than plain newlines, so a trailing-line check can't find it. Strips a
    trailing anchor whose ``href`` is exactly ``quote_uri``, plus any
    immediately-surrounding whitespace/``<br>``/``<span>`` wrapper -- a
    no-op if the markup doesn't actually end in one (this is inherently a
    best-effort heuristic against one observed real-world shape, not a full
    HTML parse, so a differently-structured fallback just won't match)."""
    escaped_uri = re.escape(html.escape(quote_uri, quote=True))
    pattern = re.compile(
        rf"(?:<span>)?\s*(?:<br\s*/?>\s*)*(?:RT|QT)?:?\s*"
        rf'<a[^>]*?href="{escaped_uri}"[^>]*>.*?</a>\s*(?:</span>)?\s*$',
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub("", safe_html).rstrip()


@dataclass
class QuotedPostRender:
    """Result of ``_quoted_post_render``. ``quoted_ref`` is
    ``(room_id, event_id)`` for the quoted post's own Matrix mirror, if it
    has one -- used for ``m.social.repost_of``'s required ``room_id``/
    ``event_id`` fields, ``None`` when the quoted post was only ever
    fetched fresh over ActivityPub (so there's no Matrix event to point
    at, and ``m.social.repost_of`` can't be attached at all -- in that
    case ``full_content`` is also ``None``, for the same reason).
    ``full_content`` is the quoted post's own complete, untouched Matrix
    event ``content`` dict -- unlike the other fields here, which are all
    deliberately truncated/condensed for the quote-card rendering,
    MSC4501 wants a genuine full copy in ``m.social.repost_of.content``,
    not a preview snippet, and specifically the real event content
    (``msgtype``, ``url``, ``info``, etc.) rather than just extracted
    text, since a plain-text-only copy would be blank for an uncaptioned
    image/video post."""

    plain_tail: str
    html_tail: str
    preview_image: dict[str, object] | None
    preview_video: dict[str, object] | None
    quoted_ref: tuple[str, str] | None
    full_content: dict[str, object] | None


async def _quoted_post_render(
    request: Request, quoter_actor_id: str, quote_uri: str, *, quoter_has_own_attachment: bool
) -> QuotedPostRender:
    """Renders a quote-post's "X reposted Y's post" line plus a preview of
    the quoted post -- for appending after the quoter's own (already
    stripped of any "RT: <link>" fallback, see ``_strip_quote_tail``)
    caption text. Mirrors ``bridge.commands._handle_repost``'s identical
    outbound rendering, so a quote reads the same regardless of which side
    of the bridge made it.

    ``preview_image``/``preview_video`` are ALSO returned separately for
    ``merge_quote_preview_attachment`` to turn into the event's own real
    Matrix attachment -- but only possible if the quoting post itself has
    none of its own (``quoter_has_own_attachment``, decided by the caller
    from the SAME ``obj`` this was reached from -- one Matrix event can
    only ever carry one real attachment, and the quoter's own wins). When
    it doesn't win -- ``quoter_has_own_attachment`` is True -- the quoted
    post's own image/video preview would otherwise vanish with nothing
    showing what was actually quoted, so THIS renders it as an inline
    ``<img>`` in ``html_tail``'s blockquote instead (with the filename
    printed right after it, for a client like Element X that doesn't
    render an inline image in a message body at all -- see
    ``_fetch_post_preview``'s docstring) rather than promoting it to a
    real attachment, since that slot's already taken.

    Prefers an already-tracked local Matrix copy of the quoted post (real
    preview text/image, a matrix.to link to it) over a fresh fetch; never
    provisions a ghost/room for its author just because someone else quoted
    them, unlike a genuine reply/mention -- this is read-only, best-effort
    (a network hiccup just means a plainer rendering, never a dropped
    post)."""
    repository = request.app.state.repository
    existing = await repository.get_federated_event_by_ap_object(quote_uri)
    preview_text = ""
    full_content: dict[str, object] | None = None
    preview_image: dict[str, object] | None = None
    preview_video: dict[str, object] | None = None
    quoted_author_id: str | None = None
    quoted_ref: tuple[str, str] | None = None
    post_link = quote_uri
    if existing is not None:
        preview_text, full_content, preview_image, preview_video = await _fetch_post_preview(request, existing)
        quoted_author_id = existing.author_actor_id
        post_link = matrix_to_link(existing.room_id, existing.event_id)
        quoted_ref = (existing.room_id, existing.event_id)
    else:
        try:
            quoted_obj = await fetch_actor(request.app.state.http_client, quote_uri)
        except RemoteActorFetchError:
            quoted_obj = None
        if quoted_obj is not None and quoted_obj.get("type") == "Note":
            # No local Matrix mirror exists for this quoted post at all
            # (quoted_ref stays None below), so there's no real event
            # ``content`` to hand back either -- m.social.repost_of won't
            # be attached regardless, per its own required room_id/event_id.
            preview_text, _ = strip_to_matrix_message(quoted_obj.get("content") or "")
            if len(preview_text) > _PREVIEW_TEXT_LIMIT:
                preview_text = preview_text[:_PREVIEW_TEXT_LIMIT].rstrip() + "…"
            quoted_author_id = _note_author(quoted_obj)

    quoter_handle, quoter_html = await actor_html_with_avatar(request, quoter_actor_id)
    if quoted_author_id:
        quoted_handle, quoted_author_html = await actor_html_with_avatar(request, quoted_author_id)
    else:
        quoted_handle, quoted_author_html = quote_uri, html.escape(quote_uri)

    preview_media = preview_video or preview_image
    # _fetch_post_preview already returns "" (never the filename) for an
    # uncaptioned media post's text, so bool(preview_text) alone covers the
    # tracked-event branch; the filename comparison stays as a second guard
    # for the OTHER branch above (a fresh remote fetch via
    # strip_to_matrix_message, which never went through that filtering).
    media_filename = str(preview_media.get("filename") or "attachment") if preview_media is not None else None
    has_real_caption = bool(preview_text) and (media_filename is None or preview_text != media_filename)
    # The media becomes the event's own attachment (via
    # merge_quote_preview_attachment) unless the quoter's own already took
    # that slot -- only then does it need an inline fallback rendering here
    # instead, so it isn't dropped with nothing to show for it.
    show_media_inline = preview_media is not None and quoter_has_own_attachment

    quote_block_parts = []
    if has_real_caption:
        quote_block_parts.append(html.escape(preview_text))
    if show_media_inline:
        img_html = f'<img src="{html.escape(str(preview_media["mxc"]), quote=True)}" width="{_PREVIEW_IMAGE_SIZE}">'
        quote_block_parts.append(f"{img_html}<br>{html.escape(media_filename)}")
    quote_block_html = f"<blockquote>{'<br>'.join(quote_block_parts)}</blockquote>" if quote_block_parts else ""

    plain_tail = f"\U0001F501 {quoter_handle} reposted {quoted_handle}'s post:"
    if has_real_caption:
        plain_tail += f"\n> {preview_text}"
    if show_media_inline:
        plain_tail += f"\n> {media_filename}"
    plain_tail += f"\n{post_link}"

    post_pill_html = f'<a href="{html.escape(post_link, quote=True)}">post</a>'
    html_tail = (
        f"<p>\U0001F501 {quoter_html} reposted {quoted_author_html}'s {post_pill_html}</p>{quote_block_html}"
    )
    return QuotedPostRender(
        plain_tail=plain_tail, html_tail=html_tail, preview_image=preview_image, preview_video=preview_video,
        quoted_ref=quoted_ref, full_content=full_content,
    )


async def _handle_create(request: Request, username: str, activity: Activity, *, force: bool = False) -> None:
    """Mirror an inbound ``Create(Note)`` into the author's Remote User
    Room -- but only if someone here actually follows them, UNLESS the
    post mentions a local user (``note_mentions_local_actor``), in which
    case it's imported anyway (just this one post, not their ongoing
    stream) purely so that mention can be surfaced -- see
    ``bridge.note_mirroring.import_note``/``notify_mentioned_locals``.
    Without that exception, a total stranger mentioning a local user would
    otherwise be silently dropped before mention-handling ever ran.

    ``force`` (only ever passed by ``bridge.commands``' ``;backfill``, for
    a post it fetched directly from a Remote User Room's own actor's
    outbox/replies, never from a live inbox delivery) skips the
    "does anyone actually follow them" relevance gate entirely -- same
    reasoning ``import`` already carves out for a single post: running the
    command against an already-existing Remote User Room is itself the
    relevance signal, the same way linking to a post is. Never bypasses
    dedup (``federated_events``) or the requirement that a Remote User Room
    already exist for this actor -- it still won't provision one.

    A Note addressed as a private/direct message (``note_is_direct_message``)
    is diverted before any of that: straight into a dedicated 1:1 DM room
    with ``username`` (see ``mirror_direct_message``), never the shared
    Remote User Room and never ``username``'s own Profile Room -- even
    though a DM is routinely structured as an AP reply to a public post (the
    common way to start one on Mastodon), which would otherwise land it in
    whichever room mirrors that parent, visible to anyone else who's been
    invited into it. This has to run before the reply-chain check below,
    since that's exactly the path that would otherwise catch it."""
    repository = request.app.state.repository
    obj = activity.object if isinstance(activity.object, dict) else None
    if obj is None:
        return

    if obj.get("type") == "ChatMessage":
        # Pleroma/Akkoma's "Chats" -- a distinct instant-messaging concept
        # from a Note-based direct message (see bridge.chat_bridge and
        # Actor.accepts_chat_messages) -- diverted before the Note-only
        # check below, into its own dedicated room type
        # (mirror_chat_message/ActorRepository.get_ghost_chat_room), never
        # the same room a Note-based DM with the same person would use.
        actor_record = await _resolve_dm_recipient(request, username, obj, activity)
        if actor_record is None:
            logger.info("Could not resolve a local recipient for a chat message from %s", activity.actor)
            return
        await mirror_chat_message(
            request, chat_message=obj, author_actor_id=activity.actor,
            recipient_matrix_user_id=actor_record.matrix_user_id,
        )
        return

    if obj.get("type") != "Note":
        return

    if note_is_direct_message(obj, extra_to=activity.to, extra_cc=activity.cc):
        actor_record = await _resolve_dm_recipient(request, username, obj, activity)
        if actor_record is None:
            logger.info("Could not resolve a local recipient for a direct message from %s", activity.actor)
            return
        await mirror_direct_message(
            request, note=obj, author_actor_id=activity.actor, recipient_matrix_user_id=actor_record.matrix_user_id
        )
        return

    in_reply_to_ap = obj.get("inReplyTo")
    if in_reply_to_ap:
        chain = await _resolve_ancestor_chain(request, in_reply_to_ap)
        if chain is not None:
            # A reply to something we already track (possibly several
            # never-seen ancestors up the chain, backfilled here first, e.g.
            # a third-party reply nobody here follows that a followed
            # account later replied to in turn) -- goes into that post's own
            # room as a threaded reply, regardless of whether we (or anyone)
            # follow the replier: replies routinely come from accounts
            # nobody here follows at all.
            parent, missing_ancestors = chain
            imported_root = False
            if parent is None and not force and not await repository.is_anyone_following(activity.actor):
                # Nobody follows the replier AND the conversation is
                # untracked: nothing about this Create is relevant here,
                # and the inbox has no sender auth -- taking the import
                # path below anyway would let any server provision
                # ghosts/Remote User Rooms on this bridge at will just by
                # POSTing reply-shaped Creates. Skip it and fall through
                # to the ordinary relevance gate below (which drops the
                # post, or imports just this one post if it mentions a
                # local user). A TRACKED-parent chain deliberately does
                # not require the replier be followed -- a third-party
                # reply into a conversation we already mirror is relevant
                # because the conversation is.
                pass
            elif parent is None:
                # The chain reached the conversation's true root without
                # touching anything we track -- historically this whole
                # branch was skipped and the reply landed as a STANDALONE
                # post in the replier's own room, silently severed from
                # its conversation. Instead (user-requested, 2026-07-04):
                # import the root, creating a ghost + Remote User Room for
                # its author on demand exactly like the `import` command
                # does, so the reply has a real thread to land in. If the
                # import fails, fall through to the old standalone
                # behavior rather than dropping the reply.
                root_note = missing_ancestors[0]
                root_author = _note_author(root_note)
                if root_author is not None:
                    try:
                        root_author_doc = await fetch_actor(request.app.state.http_client, root_author)
                    except RemoteActorFetchError:
                        root_author_doc = {}
                    imported = await import_note(
                        request, note=root_note, author_actor_id=root_author, author_doc=root_author_doc
                    )
                    parent = imported.federated_event
                    if parent is not None:
                        imported_root = True
                        missing_ancestors = missing_ancestors[1:]
            if parent is not None:
                for ancestor_note in missing_ancestors:
                    ancestor_author = _note_author(ancestor_note)
                    if ancestor_author is None:
                        break  # can't attribute it to anyone -- stop backfilling, but still try the real reply below
                    mirrored = await _mirror_note_as_reply(request, ancestor_note, parent, ancestor_author)
                    if mirrored is None:
                        break  # couldn't mirror this ancestor -- attach directly to whichever parent we did reach
                    parent = mirrored
                reply_event = await _mirror_note_as_reply(request, obj, parent, activity.actor)
                if imported_root and reply_event is not None:
                    # The reply's author previously had this post show up in
                    # their OWN room (as the severed standalone) -- keep that
                    # room's timeline complete with an annotated copy
                    # pointing back at the thread it actually lives in.
                    await _echo_reply_in_own_room(
                        request, note=obj, reply_event=reply_event, parent=parent, author_actor_id=activity.actor
                    )
                return

    if not force and not await repository.is_anyone_following(activity.actor):
        if not note_mentions_local_actor(request, obj):
            logger.info("Dropping Create from an actor nobody here follows: %s", activity.actor)
            return
        # Nobody follows this account, but this post mentions a local
        # user -- import just this one post (same as the `import` command;
        # creates/reuses a Remote User Room for the author on demand) so
        # the mention can still be surfaced, without starting to mirror
        # their entire ongoing post stream the way actually following them
        # would. import_note does its own dedup (redelivery-safe) and runs
        # notify_mentioned_locals itself.
        try:
            author_doc = await fetch_actor(request.app.state.http_client, activity.actor)
        except RemoteActorFetchError:
            author_doc = {}
        await import_note(request, note=obj, author_actor_id=activity.actor, author_doc=author_doc)
        return

    remote_room = await repository.get_remote_actor_room(activity.actor)
    if remote_room is None:
        logger.info("No Remote User Room mapped for %s; dropping Create", activity.actor)
        return

    ap_object_id = obj.get("id")
    if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
        # Already mirrored -- following is tracked per local actor now, so if
        # more than one person here independently follows the same account,
        # its server may deliver the same post once per follower (once per
        # inbox/shared-inbox recipient); only the first delivery should
        # actually create anything.
        return

    mentions = await resolve_mention_pills(request, room_id=remote_room.room_id, note=obj)
    plain, safe_html = strip_to_matrix_message(obj.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, request.app.state.synapse, repository,
            safe_html, obj.get("tag") or [], subject_id=ap_object_id,
        )

    # Computed up front (rather than where it's actually attached, below)
    # since _quoted_post_render needs to know whether the quoting post has
    # an attachment of its own BEFORE it decides how to render the quoted
    # post's -- see its docstring.
    attachments = extract_attachments(obj)

    quote_uri = _extract_quote_uri(obj)
    quote_plain_tail = quote_html_tail = ""
    quote_preview_image: dict[str, object] | None = None
    quote_preview_video: dict[str, object] | None = None
    quote_render: QuotedPostRender | None = None
    if quote_uri:
        # A genuine quote-post (Akkoma/Pleroma/Fedibird/Misskey-family --
        # see Note.quote_uri's own docstring) -- some of those append a
        # plain-text "RT: <link>"-shaped fallback to their OWN content for
        # receivers that don't understand the quote fields, which is
        # exactly what we'd otherwise just mirror verbatim. Strip that tail
        # and render our own explicit "X reposted Y's post" line plus a
        # preview instead, same convention as our own outbound ;repost (see
        # bridge.commands._handle_repost).
        plain = _strip_quote_tail(plain, quote_uri)
        if safe_html:
            safe_html = _strip_quote_tail_html(safe_html, quote_uri)
        quote_render = await _quoted_post_render(
            request, activity.actor, quote_uri, quoter_has_own_attachment=bool(attachments)
        )
        quote_plain_tail = quote_render.plain_tail
        quote_html_tail = quote_render.html_tail
        quote_preview_image = quote_render.preview_image
        quote_preview_video = quote_render.preview_video

    message_content: dict = {"msgtype": "m.text", "body": plain}
    if quote_plain_tail:
        message_content["body"] += f"\n\n{quote_plain_tail}"
    if (safe_html and safe_html != plain) or quote_html_tail:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = (safe_html or html.escape(plain)) + quote_html_tail
    if (
        request.app.state.config.bridge.set_msc4501_repost_of
        and quote_render is not None
        and quote_render.quoted_ref is not None
    ):
        quoted_room_id, quoted_event_id = quote_render.quoted_ref
        message_content[SOCIAL_REPOST_OF_FIELD] = {
            "event_id": quoted_event_id,
            "room_id": quoted_room_id,
            "content": quote_render.full_content,
        }
    if mentions.mentioned_locals:
        # A pill alone (the <a href="matrix.to/..."> in formatted_body,
        # above) only makes the mention a clickable link -- an intentional
        # mention (MSC3952) is what actually highlights/notifies the
        # tagged user's client, and nothing sets that unless we do.
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}
    source_url = _source_post_url(obj)
    if source_url:
        message_content["external_url"] = source_url

    # Only the first attachment (if any) is embedded as real Matrix media --
    # see attach_media_to_content's docstring for why the rest are appended
    # as plain links instead of split into their own separate events: an
    # ActivityPub post always maps to exactly one Matrix event.
    message_content, _ = await _attach_media_to_content(request, message_content, attachments)
    # Only takes effect if the quoting post above had no attachment of its
    # own to keep -- see merge_quote_preview_attachment's docstring.
    message_content = merge_quote_preview_attachment(
        message_content, preview_image=quote_preview_image, preview_video=quote_preview_video
    )

    federation_config = request.app.state.config.federation
    try:
        event_id = await request.app.state.synapse.send_message_event(
            remote_room.room_id, message_content, as_user_id=remote_room.ghost_user_id,
            event_type=mirrored_post_event_type(request.app.state.config),
            ts=resolve_event_ts(
                obj, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror post from %s", activity.actor, exc_info=True)
        return

    if ap_object_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id,
                room_id=remote_room.room_id,
                ap_object_id=ap_object_id,
                author_actor_id=activity.actor,
            )
        )

    await notify_mentioned_locals(
        request,
        mentioned=mentions.mentioned_locals,
        room_id=remote_room.room_id,
        event_id=event_id,
        author_actor_id=activity.actor,
    )


async def _resolve_ancestor_chain(
    request: Request, in_reply_to_id: str, *, max_depth: int = 10
) -> tuple[FederatedEvent | None, list[dict]] | None:
    """Walk *up* an AP reply chain starting from ``in_reply_to_id``, fetching
    any ancestor Note we don't already track, until reaching one we do --
    or the conversation's true root.

    This is what lets a third-party reply from an account nobody here
    follows still get bridged: if a followed account's later post replies
    to (or itself gets replied to by, once *that* reply's own later replies
    arrive) something we've never seen, fetching it on demand discovers who
    actually wrote it and what it was replying to, all the way back to a
    post we already mirror.

    Three outcomes:

    ``(nearest_tracked_ancestor, missing_notes)`` -- the chain reached a
    post we already mirror; ``missing_notes`` is the previously-unknown
    ancestors in oldest-first order (so a caller can mirror them in
    sequence, each becoming the parent of the next).

    ``(None, chain_notes)`` -- the chain reached the conversation's TRUE
    ROOT (a fetched Note with no ``inReplyTo`` of its own) without touching
    anything we track. ``chain_notes[0]`` is that root; the caller can
    import it to create an anchor (see ``_handle_create``'s reply branch)
    rather than dropping the whole chain on the floor the way this
    function itself used to.

    ``None`` -- the chain is unusable: an inaccessible/deleted/non-Note
    ancestor, or deeper than ``max_depth``. Nothing to thread against and
    nothing importable either.
    """
    repository = request.app.state.repository
    missing: list[dict] = []
    current_id = in_reply_to_id
    for _ in range(max_depth):
        parent = await repository.get_federated_event_by_ap_object(current_id)
        if parent is not None:
            return parent, list(reversed(missing))

        note = await _resolve_object(request, current_id)
        if note is None or note.get("type") != "Note":
            return None  # inaccessible, deleted, or not actually a Note

        missing.append(note)
        next_id = note.get("inReplyTo")
        if not next_id:
            return None, list(reversed(missing))  # untracked root -- importable anchor candidate
        current_id = next_id
    return None  # gave up -- chain deeper than we're willing to fetch


async def _mirror_note_as_reply(
    request: Request, note: dict, parent: FederatedEvent, author_actor_id: str
) -> FederatedEvent | None:
    """Mirror ``note`` into ``parent``'s room as a threaded reply from a
    ghost representing ``author_actor_id`` -- who we may not otherwise
    follow or have any existing room for at all. Returns the FederatedEvent
    recorded for it (so a caller backfilling a whole missing ancestor chain
    can use it as the next parent), or None if mirroring failed outright.

    Dedups against an existing record for this exact note the same way
    ``import_note`` does (checked first, before doing any of the
    ghost/room/send work below) -- none of this function's three call sites
    (a fresh reply, an ancestor-chain backfill, or ``bridge.commands``'
    ``import``) otherwise guarantee ``note`` hasn't already been mirrored by
    an earlier pass, e.g. a redelivered transaction, or reaching the same
    note via two different paths (an ancestor backfilled here, then later
    also arriving as its own top-level Create). Without this, a second
    ``record_federated_event`` for the same ``ap_object_id`` would collide
    with the first's now-primary row."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    ap_object_id = note.get("id")
    if ap_object_id:
        existing = await repository.get_federated_event_by_ap_object(ap_object_id)
        if existing is not None:
            return existing

    resolved = await _resolve_and_invite_ghost(request, author_actor_id, parent.room_id)
    if resolved is None:
        logger.info("Could not resolve a ghost for reply from %s; dropping", author_actor_id)
        return None
    ghost_mxid_, _actor_doc = resolved

    # Unlike a follower announcement, we're about to post as this ghost right
    # now -- can't wait on the usual invite-then-async-auto-accept path (see
    # _resolve_and_invite_ghost), so force the join synchronously here.
    try:
        await synapse.join_room(parent.room_id, as_user_id=ghost_mxid_)
    except SynapseError:
        logger.info("Could not join reply ghost %s into %s", ghost_mxid_, parent.room_id, exc_info=True)
        return None

    mentions = await resolve_mention_pills(request, room_id=parent.room_id, note=note)
    plain, safe_html = strip_to_matrix_message(note.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, request.app.state.synapse, repository,
            safe_html, note.get("tag") or [], subject_id=ap_object_id,
        )
    message_content: dict = {
        "msgtype": "m.text",
        "body": plain,
        "m.relates_to": thread_reply_relates_to(
            event_id=parent.event_id, thread_root_event_id=parent.thread_root_event_id
        ),
    }
    if safe_html and safe_html != plain:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html
    if mentions.mentioned_locals:
        # See _handle_create's identical reasoning: the pill alone doesn't
        # notify/highlight the tagged user, an intentional mention does.
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}
    source_url = _source_post_url(note)
    if source_url:
        message_content["external_url"] = source_url

    # See _handle_create's identical reasoning: only the first attachment is
    # embedded, the rest are appended as plain links -- an ActivityPub post
    # always maps to exactly one Matrix event.
    attachments = extract_attachments(note)
    message_content, _ = await _attach_media_to_content(request, message_content, attachments)

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            parent.room_id, message_content, as_user_id=ghost_mxid_,
            event_type=mirrored_post_event_type(request.app.state.config),
            ts=resolve_event_ts(
                note, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror reply from %s into %s", author_actor_id, parent.room_id, exc_info=True)
        return None

    new_federated_event = None
    if ap_object_id:
        new_federated_event = FederatedEvent(
            event_id=event_id,
            room_id=parent.room_id,
            ap_object_id=ap_object_id,
            author_actor_id=author_actor_id,
            thread_root_event_id=parent.thread_root_event_id or parent.event_id,
        )
        await repository.record_federated_event(new_federated_event)

    await notify_mentioned_locals(
        request,
        mentioned=mentions.mentioned_locals,
        room_id=parent.room_id,
        event_id=event_id,
        author_actor_id=author_actor_id,
    )

    return new_federated_event


async def _echo_reply_in_own_room(
    request: Request, *, note: dict, reply_event: FederatedEvent, parent: FederatedEvent, author_actor_id: str
) -> None:
    """Post an annotated copy of a just-threaded reply into the REPLIER's
    own Remote User Room: a "⤵️ Reply to <author>'s post" header
    (the word "post" being a matrix.to event pill to the thread's root)
    above the reply's own content.

    Used only by ``_handle_create``'s import-the-untracked-root path
    (user-requested, 2026-07-04): before that path existed, such a reply
    landed in the replier's own room as a standalone post, so anyone
    watching that room as the account's timeline SAW it there -- threading
    it into the (freshly imported) conversation room alone would silently
    remove it from the timeline it used to appear in. This keeps the
    replier's room complete while pointing at where the thread actually
    lives. Ordinary replies into ALREADY-tracked conversations don't get
    an echo -- they never appeared in the replier's room before either,
    and echoing every reply a chatty account makes would swamp its room.

    Best-effort: any failure just means no echo; the canonical threaded
    copy already exists. Recorded as a NON-primary ``FederatedEvent`` for
    the same AP object (the partial-unique index only constrains primary
    rows -- verified live), so a Matrix reaction or reply to the echo
    resolves to the same underlying post as one on the threaded copy.
    """
    repository = request.app.state.repository
    remote_room = await repository.get_remote_actor_room(author_actor_id)
    if remote_room is None:
        return  # replier isn't followed here -- no room of theirs to keep complete
    profile = await repository.get_ghost_profile(author_actor_id)
    if profile is None or not profile.mxid:
        return

    root_event_id = reply_event.thread_root_event_id or parent.event_id
    thread_link = matrix_to_link(reply_event.room_id, root_event_id)
    parent_handle, parent_author_html = await actor_html_with_avatar(request, parent.author_actor_id)

    mentions = await resolve_mention_pills(request, room_id=remote_room.room_id, note=note)
    plain, safe_html = strip_to_matrix_message(note.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, request.app.state.synapse, repository,
            safe_html, note.get("tag") or [], subject_id=note.get("id"),
        )

    plain_body = f"⤵️ Reply to {parent_handle}'s post:\n\n{plain}".strip()
    plain_body += f"\n\n{thread_link}"
    header_html = (
        f"<p>⤵️ Reply to {parent_author_html}'s "
        f'<a href="{html.escape(thread_link, quote=True)}">post</a>:</p>'
    )
    content_html = safe_html or (f"<p>{html.escape(plain)}</p>" if plain else "")
    message_content: dict = {
        "msgtype": "m.text",
        "body": plain_body,
        "format": "org.matrix.custom.html",
        "formatted_body": header_html + content_html,
    }
    source_url = _source_post_url(note)
    if source_url:
        message_content["external_url"] = source_url
    attachments = extract_attachments(note)
    message_content, _ = await _attach_media_to_content(request, message_content, attachments)

    federation_config = request.app.state.config.federation
    try:
        event_id = await request.app.state.synapse.send_message_event(
            remote_room.room_id, message_content, as_user_id=profile.mxid,
            event_type=mirrored_post_event_type(request.app.state.config),
            ts=resolve_event_ts(
                note, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning(
            "Failed to echo reply from %s into their own room %s", author_actor_id, remote_room.room_id, exc_info=True
        )
        return

    ap_object_id = note.get("id")
    if ap_object_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id,
                room_id=remote_room.room_id,
                ap_object_id=ap_object_id,
                author_actor_id=author_actor_id,
            ),
            is_primary=False,
        )


_DEFAULT_LIKE_EMOJI = "👍"


async def _handle_like_or_emoji_react(request: Request, username: str, activity: Activity) -> None:
    """Mirror an inbound Like/EmojiReact as an ``m.reaction`` on whatever
    Matrix event the target post mirrors -- regardless of who reacted or
    whether we (or anyone) follow them, since reactions routinely come from
    accounts nobody here follows, same as replies.

    Announce/boosts are handled separately (see ``_handle_announce``): a
    boost isn't really a reaction to one specific message the way a Like is,
    it's its own repost, and the boosted post may not be anything we
    otherwise track at all.
    """
    target_id = activity.object_id()
    if not target_id:
        return
    repository = request.app.state.repository
    target = await repository.get_federated_event_by_ap_object(target_id)
    if target is None:
        return  # We never mirrored the target object; nothing to react to.

    resolved = await _resolve_and_invite_ghost(request, activity.actor, target.room_id)
    if resolved is None:
        return
    ghost_mxid_, actor_doc = resolved

    synapse = request.app.state.synapse
    try:
        await synapse.join_room(target.room_id, as_user_id=ghost_mxid_)
    except SynapseError:
        logger.info("Could not join reactor ghost %s into %s", ghost_mxid_, target.room_id, exc_info=True)
        return

    # A standard Mastodon `Like` never has `content` (just a fixed heart);
    # Pleroma/Misskey/Akkoma's richer reactions put the actual emoji -- a
    # literal unicode character, or a custom-emoji shortcode like
    # ":blobcat:" -- there, and using it as-is for the reaction key satisfies
    # both cases without needing to tell them apart.
    key = activity.content or _DEFAULT_LIKE_EMOJI
    custom_emoji_mxc = await resolve_custom_emoji_image(
        request.app.state.http_client, request.app.state.synapse, repository, activity.tag, key
    )

    try:
        event_id = await synapse.send_message_event(
            target.room_id,
            {"m.relates_to": {"rel_type": "m.annotation", "event_id": target.event_id, "key": key}},
            event_type="m.reaction",
            as_user_id=ghost_mxid_,
        )
    except SynapseError:
        logger.warning("Failed to mirror %s as a reaction", activity.type, exc_info=True)
        return

    if activity.id:
        await repository.record_reaction(
            ReactionRecord(
                activity_id=activity.id,
                room_id=target.room_id,
                event_id=event_id,
                target_ap_object_id=target_id,
                reactor_ghost_mxid=ghost_mxid_,
                custom_emoji_mxc=custom_emoji_mxc,
            )
        )

    verb_phrase = "liked your post" if not activity.content else f"reacted to your post with {key}"
    await _notify_post_owner(
        request, target=target, actor_id=activity.actor, actor_doc=actor_doc, verb_phrase=verb_phrase,
        reaction_emoji=key if activity.content else None, reaction_emoji_mxc=custom_emoji_mxc,
    )


async def _handle_announce(request: Request, username: str, activity: Activity) -> None:
    """A followed account reposted/boosted someone else's post -- mirror it
    into their own Remote User Room as a nicely-formatted repost (not just a
    bare reaction), fetching the boosted post and/or its original author if
    we don't already have either, since the booster is who we follow but the
    original poster usually isn't.

    Separately -- and regardless of whether the booster is themselves
    followed/mirrored at all -- if the boosted post is one we track that
    lives in a local user's own Profile Room, notify them there. Anyone on
    the fediverse can boost a public post, not just people the poster
    follows back, so this check can't be gated on ``remote_room`` existing.
    """
    repository = request.app.state.repository

    announce_id = activity.id
    if announce_id and await repository.get_federated_event_by_ap_object(announce_id) is not None:
        return  # already handled (e.g. a redelivered transaction) -- covers the notification below too

    boosted_id = activity.object_id()
    boosted_target = await repository.get_federated_event_by_ap_object(boosted_id) if boosted_id else None
    if boosted_target is not None:
        try:
            booster_actor_doc = await fetch_actor(request.app.state.http_client, activity.actor)
        except RemoteActorFetchError:
            booster_actor_doc = {}
        await _notify_post_owner(
            request, target=boosted_target, actor_id=activity.actor, actor_doc=booster_actor_doc,
            verb_phrase="boosted your post",
        )

    remote_room = await repository.get_remote_actor_room(activity.actor)
    if remote_room is None:
        return  # we don't mirror this booster at all

    obj = await _resolve_object(request, activity.object)
    if obj is None or obj.get("type") != "Note":
        logger.info("Could not resolve announced object from %s; dropping repost", activity.actor)
        return

    original_author_id = _note_author(obj)
    original_actor_doc: dict = {}
    if isinstance(original_author_id, str):
        try:
            original_actor_doc = await fetch_actor(request.app.state.http_client, original_author_id)
        except RemoteActorFetchError:
            original_actor_doc = {}

    # Actually import the boosted post itself (same as the `import` command
    # would, into a Remote User Room for its ORIGINAL author, not the
    # booster) -- not just the summary card below -- so it's independently
    # navigable/reply-able/reactable, and dedupes against it being imported
    # again later some other way. `inviter` is whichever local actor's
    # inbox this Announce actually arrived at (i.e. the person following
    # the booster), so they land in the original author's room too, not
    # just the booster's. Its first attachment is reused below for the
    # repost summary card rather than re-uploaded, since it's the exact
    # same file.
    imported_link: str | None = None
    imported_ref: tuple[str, str] | None = None
    imported_attachment_mxc: str | None = None
    if isinstance(original_author_id, str):
        local_actor = await repository.get_local_actor(username)
        inviter = local_actor.matrix_user_id if local_actor is not None else None
        imported = await import_note(
            request, note=obj, author_actor_id=original_author_id, author_doc=original_actor_doc, inviter=inviter,
        )
        imported_attachment_mxc = imported.first_attachment_mxc
        if imported.federated_event is not None:
            imported_link = matrix_to_link(imported.federated_event.room_id, imported.federated_event.event_id)
            imported_ref = (imported.federated_event.room_id, imported.federated_event.event_id)

    message_content = await _build_repost_message(
        request, remote_room, obj, original_author_id, original_actor_doc, imported_link, imported_ref,
    )

    # See _handle_create's identical reasoning: only the boosted post's
    # first attachment is embedded, the rest are appended as plain links --
    # an ActivityPub post always maps to exactly one Matrix event, boosted
    # or not.
    attachments = extract_attachments(obj)
    message_content, _ = await _attach_media_to_content(
        request, message_content, attachments, mxc_uri=imported_attachment_mxc
    )

    synapse = request.app.state.synapse
    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            remote_room.room_id, message_content, as_user_id=remote_room.ghost_user_id,
            event_type=mirrored_post_event_type(request.app.state.config),
            # The Announce's OWN published time (when the boost happened),
            # not the boosted post's -- this message represents "X boosted
            # this", so that's the moment it belongs at in the timeline,
            # not whenever the original (possibly much older) post was
            # first made.
            ts=resolve_event_ts(
                {"published": activity.published}, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to mirror repost from %s", activity.actor, exc_info=True)
        return

    if announce_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id,
                room_id=remote_room.room_id,
                ap_object_id=announce_id,
                author_actor_id=activity.actor,
                boosted_object_id=obj.get("id"),
                boosted_author_actor_id=original_author_id,
            )
        )


async def _resolve_object(request: Request, obj_field) -> dict | None:
    """Resolve an Activity's ``object`` field to a dict, fetching it if it's
    only a bare IRI reference -- some implementations embed the boosted Note
    directly, others just reference it by id."""
    if isinstance(obj_field, dict):
        return obj_field
    if isinstance(obj_field, str):
        try:
            # fetch_actor is a generic AP-Accept-header GET despite the name
            # -- reused here to fetch a Note, not an actor.
            return await fetch_actor(request.app.state.http_client, obj_field)
        except RemoteActorFetchError:
            return None
    return None


def _note_author(note: dict) -> str | None:
    """Extract a Note's ``attributedTo`` as a bare actor IRI, handling the
    shapes seen in the wild: a plain string, an embedded actor object, or a
    list of either (some implementations allow multiple attributions --
    we just use the first)."""
    author = note.get("attributedTo")
    if isinstance(author, list):
        author = author[0] if author else None
    if isinstance(author, dict):
        author = author.get("id")
    return author if isinstance(author, str) else None


async def _build_repost_message(
    request: Request,
    remote_room: RemoteActorRoom,
    obj: dict,
    original_author_id: str | None,
    original_actor_doc: dict,
    imported_link: str | None = None,
    imported_ref: tuple[str, str] | None = None,
) -> dict:
    """Build the Matrix message content for a mirrored repost: an HTML body
    showing "\U0001F501 {booster} boosted {original author}'s post:" above
    the (sanitized) post content, plus a plaintext fallback for clients
    that ignore HTML -- the same attribution-line convention (pills where
    possible, see ``actor_html_with_avatar``) every other boost/repost
    rendering in this bridge uses, so one looks the same as another
    regardless of whether it's local, remote, an Announce, or a
    ``;repost``. ``imported_link``, if given, is a matrix.to link to
    wherever the boosted post itself was mirrored to (see
    ``_handle_announce``'s use of ``bridge.note_mirroring.import_note``) --
    same as ``_quoted_post_render``/``_notify_post_owner``, that link is
    the word "post" itself (an event pill), not a separate "Full post:"
    line -- one looks the same as another everywhere in this bridge.

    ``imported_ref`` is that same boosted post's own ``(room_id,
    event_id)``, used (when ``bridge.set_msc4501_repost_of`` is on) to
    fetch that event's own real ``content`` dict and set
    ``m.social.repost_of.content`` to it verbatim -- the actual event
    content (``msgtype``, ``url``, ``info``, etc.), not just extracted
    text, since a plain-text copy would be blank for an uncaptioned
    image/video boost. Also, per the MSC's own definition of a boost (as
    opposed to a quote-post with commentary), replaces the PLAIN ``body``
    with nothing but ``imported_link`` itself, so a MSC4501-aware client
    can recognize this as a boost by ``body`` being just a permalink
    pointing at the same event ``m.social.repost_of`` names.
    ``formatted_body`` is untouched either way -- that plain ``body``
    swap only matters to a client parsing MSC4501 fields; an
    ordinary client still renders the same rich HTML card as always."""
    _, booster_html = await actor_html_with_avatar(request, remote_room.actor_id)
    if original_author_id:
        original_handle, original_author_html = await actor_html_with_avatar(request, original_author_id)
    else:
        # No real actor id to resolve a pill for at all -- fall back to
        # whatever the fetched actor document itself offered.
        original_handle = original_actor_doc.get("name") or original_actor_doc.get("preferredUsername") or "someone"
        original_author_html = html.escape(original_handle)

    # Not using notify_mentioned_locals's mentioned_locals here -- this
    # content is the repost SUMMARY card, a secondary rendering of the same
    # Note that import_note (called by _handle_announce for this exact
    # note) already mirrors as its own canonical copy and already runs
    # notify_mentioned_locals against; doing it again here would just be a
    # second, redundant notification for the same mention.
    mentions = await resolve_mention_pills(request, room_id=remote_room.room_id, note=obj)
    plain, safe_html = strip_to_matrix_message(obj.get("content") or "", mention_pills=mentions.pills)
    if safe_html:
        safe_html = await inline_custom_emoji(
            request.app.state.http_client, request.app.state.synapse, request.app.state.repository,
            safe_html, obj.get("tag") or [], subject_id=obj.get("id"),
        )

    plain_body = f"\U0001F501 boosted {original_handle}'s post:\n\n{plain}".strip()
    if imported_link:
        plain_body += f"\n\n{imported_link}"

    post_pill_html = f'<a href="{html.escape(imported_link, quote=True)}">post</a>' if imported_link else "post"
    header_html = f"<p>\U0001F501 {booster_html} boosted {original_author_html}'s {post_pill_html}:</p>"
    content_html = safe_html or (f"<p>{html.escape(plain)}</p>" if plain else "")

    boosted_content: dict[str, object] | None = None
    if request.app.state.config.bridge.set_msc4501_repost_of and imported_ref is not None:
        config = request.app.state.config
        bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
        try:
            boosted_event = await request.app.state.synapse.get_event(*imported_ref, as_user_id=bot_mxid)
            boosted_content = boosted_event.get("content") or {}
        except SynapseError:
            logger.info("Could not fetch boosted event %s in %s for repost_of", *imported_ref, exc_info=True)

    use_repost_of = boosted_content is not None and imported_link
    message_content = {
        "msgtype": "m.text",
        # Per MSC4501, a boost's plain body MUST be nothing but a matrix.to
        # permalink to the boosted event -- see this function's own
        # docstring. formatted_body (below) keeps the full rich card
        # regardless; only an MSC4501-aware client parses body at all.
        "body": imported_link if use_repost_of else plain_body,
        "format": "org.matrix.custom.html",
        "formatted_body": header_html + content_html,
    }
    if use_repost_of:
        quoted_room_id, quoted_event_id = imported_ref
        message_content[SOCIAL_REPOST_OF_FIELD] = {
            "event_id": quoted_event_id,
            "room_id": quoted_room_id,
            "content": boosted_content,
        }
    source_url = _source_post_url(obj)
    if source_url:
        message_content["external_url"] = source_url
    return message_content


async def _handle_update(request: Request, username: str, activity: Activity) -> None:
    """A followed remote actor changed their profile -- keep our mirror (the
    ghost user and the Remote User Room's name/avatar/banner) in sync."""
    obj = activity.object
    if isinstance(obj, str):
        try:
            obj = await fetch_actor(request.app.state.http_client, obj)
        except RemoteActorFetchError:
            return
    if not isinstance(obj, dict) or obj.get("type") not in _ACTOR_TYPES:
        return  # not a profile update we care about (e.g. an edited Note)

    repository = request.app.state.repository
    remote_room = await repository.get_remote_actor_room(activity.actor)
    if remote_room is None:
        return  # we don't mirror this actor

    new_name = obj.get("name") or obj.get("preferredUsername") or remote_room.display_name
    new_icon_url = extract_icon_url(obj)
    new_banner_url = extract_banner_url(obj)
    name_changed = new_name != remote_room.display_name
    icon_changed = new_icon_url != remote_room.icon_url
    banner_changed = new_banner_url != remote_room.banner_url
    if not name_changed and not icon_changed and not banner_changed:
        return

    avatar_mxc = None
    if icon_changed and new_icon_url:
        avatar_mxc = await fetch_and_upload_media(
            request.app.state.http_client, request.app.state.synapse, new_icon_url
        )
    banner_mxc = None
    if banner_changed and new_banner_url:
        banner_mxc = await fetch_and_upload_media(
            request.app.state.http_client, request.app.state.synapse, new_banner_url
        )

    synapse = request.app.state.synapse
    try:
        if name_changed:
            await synapse.send_state_event(
                remote_room.room_id, "m.room.name", "", {"name": new_name}, as_user_id=remote_room.ghost_user_id
            )
            await synapse.set_display_name(remote_room.ghost_user_id, new_name)
        if icon_changed and avatar_mxc:
            await synapse.send_state_event(
                remote_room.room_id, "m.room.avatar", "", {"url": avatar_mxc}, as_user_id=remote_room.ghost_user_id
            )
            await synapse.set_avatar_url(remote_room.ghost_user_id, avatar_mxc)
    except SynapseError:
        logger.warning("Failed to sync profile changes for %s", activity.actor, exc_info=True)

    if banner_changed and banner_mxc:
        await _set_ghost_room_banner(
            request, room_id=remote_room.room_id, ghost_user_id=remote_room.ghost_user_id, banner_mxc=banner_mxc
        )

    await repository.register_remote_actor_room(
        RemoteActorRoom(
            actor_id=remote_room.actor_id,
            room_id=remote_room.room_id,
            ghost_user_id=remote_room.ghost_user_id,
            inbox_url=remote_room.inbox_url,
            display_name=new_name,
            icon_url=new_icon_url,
            banner_url=new_banner_url,
        )
    )
    # Keep _resolve_and_invite_ghost's separate sync-ghost-profile cache
    # (used for replies/reactions from this same actor, which don't always
    # go through a Remote User Room) in step with what was just applied here
    # -- otherwise its next call would see a stale icon_url and think this
    # change still needs (re-)applying, redundantly re-uploading/re-setting
    # something Update just already took care of.
    await repository.record_ghost_profile(
        GhostProfile(actor_id=remote_room.actor_id, display_name=new_name, icon_url=new_icon_url)
    )


async def _handle_block(request: Request, username: str, activity: Activity) -> None:
    """A remote actor blocked one of our local users -- surface it as a
    \U0001F6AB notification (user-requested, 2026-07-04).

    Mastodon/Pleroma DELIVER the ``Block`` activity to the blocked user's
    server (unless the instance is configured for stealth blocks) -- most
    software just receives it silently, but some (Pleroma-family frontends
    notably) show a "blocked you" notification, and that's the behavior
    wanted here. FEDERATED blocks only: a local user blocking another
    local user never produces an inbound activity, and per the user's
    explicit scoping shouldn't notify even if one someday did.

    A block SEVERS the blocker's follow of the target (fediverse
    semantics: the blocking server drops its user's follow locally and
    does NOT send a separate Undo(Follow) for it) -- so the follower
    record is removed here exactly like an inbound ``Undo(Follow)`` would
    (record only, no ghost kick, matching ``_handle_undo``). Without
    this, an unblock-and-refollow found the STALE follower record and
    ``_handle_follow``'s redelivery dedup swallowed the "is now following
    you" notification -- confirmed live (2026-07-04, first block test).
    Otherwise no side effects: anything already mirrored here stays, and
    the blocker's name renders as a pill only if a ghost already exists
    for them; a block is exactly the wrong reason to go provision one.
    """
    repository = request.app.state.repository
    config = request.app.state.config
    base = config.bridge.public_base_url

    # Shared-inbox deliveries resolve the target from the activity itself
    # (`object` names the blocked actor); the per-user inbox already knows.
    target_username = username_from_actor_url(base, activity.object_id() or "") or username
    if not target_username:
        return
    record = await repository.get_local_actor(target_username)
    if record is None:
        return

    # Sever their follow BEFORE the silenced check: the record must go
    # regardless of whether a notification is wanted.
    await repository.remove_follower(record.username, activity.actor)

    if await is_silenced(repository, record.username, activity.actor):
        return  # they muted/blocked this account themselves -- no note needed

    profile = await repository.get_ghost_profile(activity.actor)
    if profile is not None and profile.handle:
        handle = profile.handle
        blocker_html = (
            notification_actor_html(mxid=profile.mxid, handle=handle, display_name=profile.display_name)
            if profile.mxid
            else f"<strong>{html.escape(handle)}</strong>"
        )
    else:
        domain = urlsplit(activity.actor).hostname or ""
        localpart = activity.actor.rstrip("/").rsplit("/", 1)[-1]
        handle = f"@{localpart}@{domain}" if domain else activity.actor
        blocker_html = f"<strong>{html.escape(handle)}</strong>"

    await notify_user(
        request,
        matrix_user_id=record.matrix_user_id,
        content={
            "msgtype": "m.text",
            "body": f"\U0001F6AB {handle} blocked you.",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>\U0001F6AB {blocker_html} blocked you.</p>",
        },
    )


async def _handle_delete(request: Request, username: str, activity: Activity) -> None:
    await _redact_for_ap_object(request, activity.object_id(), reason="Delete", actor_id=activity.actor)


async def _redact_for_ap_object(request: Request, ap_object_id: str | None, *, reason: str, actor_id: str) -> None:
    if not ap_object_id:
        return
    repository = request.app.state.repository
    target = await repository.get_federated_event_by_ap_object(ap_object_id)
    if target is None:
        return
    if target.author_actor_id != actor_id:
        # Whoever sent this isn't who actually authored the thing they're
        # asking us to redact -- a forged/mistargeted Delete (or Undo of
        # someone else's Announce). Refusing here is also what keeps a
        # resent/replayed Delete from an unrelated actor from re-redacting
        # (and re-spamming a fresh redaction event for) the same post over
        # and over, since it never gets past this check to begin with.
        logger.warning(
            "Refusing to redact %s for %s: sender %s is not the author %s",
            target.event_id, reason, actor_id, target.author_actor_id,
        )
        return
    remote_room = await repository.get_remote_actor_room_by_room_id(target.room_id)
    as_user = remote_room.ghost_user_id if remote_room else None
    try:
        await request.app.state.synapse.redact_event(
            target.room_id, target.event_id, reason=reason, as_user_id=as_user
        )
    except SynapseError:
        logger.warning("Failed to redact %s for %s", target.event_id, reason, exc_info=True)


async def _redact_reaction(request: Request, reaction: ReactionRecord) -> None:
    """Redact just the reaction (never the post it's on) that an inbound Undo
    is retracting. Redacts as ``reactor_ghost_mxid`` specifically -- the same
    ghost that sent it -- rather than the room's "owning" ghost, since
    redacting your own event is always permitted regardless of power level,
    while redacting someone else's usually isn't."""
    repository = request.app.state.repository
    try:
        await request.app.state.synapse.redact_event(
            reaction.room_id, reaction.event_id, reason="Undo", as_user_id=reaction.reactor_ghost_mxid
        )
    except SynapseError:
        logger.warning("Failed to redact reaction %s in %s", reaction.event_id, reaction.room_id, exc_info=True)
    await repository.remove_reaction(reaction.activity_id)


async def _resolve_inbox(request: Request, actor_id: str) -> str:
    inbox = await resolve_actor_inbox(request, actor_id)
    if not inbox:
        raise DeliveryError(f"No inbox known for actor {actor_id}")
    return inbox


_HANDLERS = {
    "Follow": _handle_follow,
    "Accept": _handle_accept,
    "Undo": _handle_undo,
    "Create": _handle_create,
    "Update": _handle_update,
    "Like": _handle_like_or_emoji_react,
    "EmojiReact": _handle_like_or_emoji_react,
    "Announce": _handle_announce,
    "Block": _handle_block,
    "Delete": _handle_delete,
}
