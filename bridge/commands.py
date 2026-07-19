"""Bridge bot commands, dispatched from incoming AppService transactions.

Triggered either by tagging the bot -- a proper Matrix mention
(``m.mentions.user_ids`` includes the bot) or its bare MXID typed at the
start of the message -- or by the ``;command`` shorthand prefix (e.g.
``;help``), for anyone who'd rather not type/select the bot's full MXID
every time. The recognized command keyword is then looked for on the
message's first line -- restricted to one line so tagging the bot inside an
unrelated multi-line message can't accidentally misfire on a command word
appearing later in the text, and so we don't need to know the exact
display-name text a client rendered for the mention pill in order to find
where the command starts. The ``;`` form is stricter than the tag form: it
only counts as addressing the bot at all if a real command keyword
immediately follows the prefix (see ``_PREFIXED_COMMAND_RE``), since unlike
deliberately tagging the bot's mxid, a single leading symbol is plausible in
someone's ordinary message text for unrelated reasons. Either form is
rejected outright if the message's ``sender`` is one of our own
ghosts/the bot itself (see ``maybe_handle_command``), so a mirrored
fediverse post that happens to start with ``;`` can never be mistaken for a
command -- it always arrives from that account's ghost, not a real local
Matrix user.

Supported commands (sent as a plain Matrix message, tagging the bot):

    <tag> create profile          Do the whole setup: creates a fresh room
                                  (the bot as its admin), invites the sender
                                  and promotes them to admin there too, sets
                                  its name/avatar to the sender's current
                                  Matrix profile, tags it as bridge-made
                                  (MSC2346) and as an ActivityPub profile room
                                  (MSC4501), and links it -- one command
                                  instead of manually making a room, inviting
                                  the bot with enough power, and running
                                  ``link profile`` yourself.
    <tag> link profile           Bind the sending Matrix user's identity to
                                  the room the command was sent in, minting a
                                  local AP actor (``username@bridge.domain``).
                                  The room's name/avatar are set to match the
                                  user's current Matrix profile. Still here
                                  for anyone who'd rather use their own
                                  already-existing room instead of
                                  ``create profile`` making one for them.
    <tag> unlink profile         Detach this room from the sender's linked
                                  identity WITHOUT telling the fediverse side
                                  anything -- followers, following, and keys
                                  are all preserved untouched, so relinking
                                  (``link profile``/``create profile``, even
                                  in a different room) reattaches the exact
                                  same identity. Use this to move your
                                  profile to a new room invisibly.
    <tag> delete profile          The old ``unlink``'s full behavior: sends a
                                  signed ``Delete`` to every follower and then
                                  permanently erases the identity -- keys,
                                  followers, following, all of it.
                                  Irreversible, unlike ``unlink profile``.
    <tag> follow @user@instance.org (or no argument, from inside that
                                  account's own Remote User Room)
                                  Start following a fediverse account, as the
                                  sender's own linked actor (not the bridge
                                  itself) -- creates/reuses its Remote User
                                  Room and invites the sender into it.
                                  Requires the sender to already have a
                                  linked profile, both so the Follow has a
                                  real identity to come from and so a reply
                                  they send never comes from an identity the
                                  remote server has never seen. If the
                                  target is actually another user ON THIS
                                  BRIDGE (tagged directly, or named by their
                                  own @user@ourdomain handle), this is
                                  handled entirely in-process instead --
                                  invites the sender straight into that
                                  user's own existing Profile Room, never a
                                  ghost/Remote User Room (see
                                  ``resolve_and_invite_ghost``'s docstring
                                  for why a local user never gets ghosted).
    <tag> unfollow @user@instance.org (or no argument, from inside that
                                  account's own room)
                                  Kicks the sender from the Remote User Room
                                  representing that account -- the actual
                                  unfollow happens in ``bridge.membership``
                                  when it sees the resulting leave, the same
                                  as if the sender had just left the room
                                  themselves (an equivalent alternative to
                                  this command, not just a side effect of it).
    <tag> following                List every fediverse account the sender's
                                  own linked actor is following, with a
                                  matrix.to link to each one's Remote User
                                  Room where there is one.
    <tag> hide followers          Hide the sender's own public followers (or
        (or following)               ``hide following``: following) collection's
                                  member list from remote viewers fetching
                                  ``/followers/{username}``/``/following/{username}``
                                  -- ``totalItems`` still reports the real
                                  count either way (same as Mastodon's own
                                  "hide network" setting), only the list
                                  itself is withheld. Run from inside the
                                  sender's own linked Profile Room. Visible
                                  by default.
    <tag> show followers          Undo ``hide`` -- makes the list visible
        (or following)               again.
    <tag> block @user@instance.org (or no argument, from inside the room
                                  representing that account -- their Remote
                                  User Room, or another local bridge user's
                                  Profile Room)
                                  Blocks the target: cuts any existing follow
                                  relationship in either direction (a real
                                  ``Undo(Follow)`` if you were following
                                  them), kicks you from their Remote User
                                  Room and any DM/Chat room open between you
                                  (never a local target's own Profile Room --
                                  see ``unfollow``'s identical restriction),
                                  declines (``Reject``) any future ``Follow``
                                  from them, and silences them exactly like
                                  ``mute`` below. Does NOT stop their posts
                                  from mirroring altogether -- that's shared
                                  infrastructure other followers may still
                                  need. ``unblock`` undoes just the block
                                  itself (not the follow/room memberships --
                                  redo those yourself once unblocked).
    <tag> mute @user@instance.org (or no argument, same as ``block``)
                                  Mutes the target: no notifications about
                                  them into your Fediverse Notifications DM,
                                  and no auto-invite into a room because of
                                  them (a fresh DM/Chat room, or being pulled
                                  into someone else's room over a mention) --
                                  explicitly running ``dm``/``chat`` toward
                                  them yourself is unaffected. Doesn't touch
                                  any existing follow relationship, room
                                  membership, or mirroring. ``unmute`` undoes
                                  it.
    <tag> import <url>            Fetch a single post by its source URL and
                                  force-mirror it, regardless of whether
                                  anyone follows its author -- creates/reuses
                                  a Remote User Room for that author (same as
                                  ``follow``, but without actually following)
                                  and invites the sender into it. A post
                                  that's already been mirrored (by this or
                                  any other path) isn't duplicated; the
                                  existing event is reported instead. Replies
                                  a matrix.to link to the resulting Matrix
                                  event so the sender can jump straight to it.
    <tag> replace room            Replace the room this command was run in
                                  with a freshly-created one representing the
                                  exact same identity (a local profile, or a
                                  Remote User Room) -- for bringing an old
                                  room up to date with features added to the
                                  bridge after it was originally created
                                  (MSC4501 room type, m.bridge info, the bot
                                  always being a member, ...). Purely a local
                                  Matrix-side operation -- nothing goes out
                                  over ActivityPub. A regular user can only
                                  do this for their own linked Profile Room;
                                  replacing a Remote User Room (representing
                                  someone else's fediverse account) requires
                                  being a Matrix server admin.
    <tag> rejoin <room_id> [@other:matrix.id]
                                  Force-attempt an invite into a room the
                                  bridge manages -- for recovering from a
                                  lockout (e.g. a room's join rule got set to
                                  knocking with nobody left to approve it).
                                  Self-service into any Remote User Room, or
                                  a room that is or ever was your own linked
                                  Profile Room; inviting anyone but yourself,
                                  or targeting any other room, requires being
                                  a Matrix server admin. Does NOT also follow
                                  the account on the fediverse -- following
                                  only ever happens via ``follow`` itself.
    <tag> banner mxc://server/mediaid
                                  Set your fediverse profile's banner/header
                                  image (distinct from your avatar) to
                                  already-uploaded Matrix media, run from
                                  inside your own linked Profile Room.
                                  Matrix has no stable room-level state for a
                                  banner separate from the room's own avatar
                                  yet -- MSC4221 proposes ``m.room.banner``,
                                  so this is recorded under that MSC's own
                                  unstable prefix (``PROFILE_BANNER_STATE_TYPE``)
                                  until it's merged. Pushed to followers
                                  immediately, every time this is run.
    <tag> help                   List these commands.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import inspect
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request

from bridge.activitypub.delivery import DeliveryError, deliver_activity
from bridge.activitypub.models import AS_PUBLIC, Activity, Note
from bridge.activitypub.remote_actor import (
    RemoteActorFetchError,
    extract_actor_url,
    extract_attachments,
    extract_banner_url,
    extract_icon_url,
    fetch_actor,
    resolve_actor_inbox,
)
from bridge.activitypub.sanitize import strip_reply_fallback, strip_to_matrix_message
from bridge.activitypub.urls import actor_url, followers_url, main_key_id, media_url, username_from_actor_url
from bridge.activitypub.webfinger import (
    WebfingerError,
    WebfingerNotFoundError,
    WebfingerUnreachableError,
    resolve_invite_code,
    resolve_remote_actor_id,
)
from bridge.crypto import generate_keypair
from bridge.ghosts import (
    ensure_ghost_user,
    ghost_localpart,
    ghost_mxid,
    sanitize_localpart_component,
    third_party_username_from_mxid,
)
from bridge.inbox_dispatch import (
    _backfill_ancestor_chain,
    _fetch_post_preview,
    _handle_create,
    _mirror_note_as_reply,
    _note_author,
    _resolve_ancestor_chain,
    build_preview_media_content,
)
from bridge.matrix_links import matrix_to_link, matrix_to_room_link, room_pill_html
from bridge.media import fetch_and_upload_media
from bridge.mentions import resolve_pill_mentions, resolve_plaintext_mentions
from bridge.notifications import NOTIFICATIONS_ROOM_NAME as _NOTIFICATIONS_ROOM_NAME
from bridge.notifications import ensure_bot_dm_invite, notification_actor_html, notify_user, welcome_new_user
from bridge.note_mirroring import KNOCK_JOIN_RULE as _KNOCK_JOIN_RULE
from bridge.note_mirroring import PROFILE_BANNER_STATE_TYPE
from bridge.note_mirroring import REPLACE_ROOM_VERSION as _REPLACE_ROOM_VERSION
from bridge.note_mirroring import SOCIAL_PROFILE_ROOM_TYPE as _SOCIAL_PROFILE_ROOM_TYPE
from bridge.note_mirroring import attach_media_to_content as _attach_media_to_content
from bridge.note_mirroring import resolve_old_remote_actor_room as _resolve_old_remote_actor_room
from bridge.note_mirroring import (
    EXTERNAL_HANDLE_FIELD,
    SOCIAL_BODY_FIELD,
    SOCIAL_FORMATTED_BODY_FIELD,
    SOCIAL_REL_TYPE_REPOST,
    SOCIAL_RELATES_TO_FIELD,
    actor_html_with_avatar,
    build_repost_note_content,
    clear_ghost_external_handle,
    deliver_to_actor_or_followers,
    ensure_ghost_chat_room,
    ensure_ghost_dm_room,
    event_external_handle_content,
    import_question,
    notify_mentioned_locals,
    provision_ghost,
    push_profile_update,
    refresh_poll_tallies,
    resolve_actor_matrix_identity,
    resolve_event_ts,
    resolve_mention_pills,
    set_ghost_external_handle,
    social_relates_to,
    thread_reply_relates_to,
    unfollow_remote_actor,
)
from bridge.note_mirroring import send_bridge_info as _send_bridge_info
from bridge.note_mirroring import set_ghost_profile_room_id as _set_ghost_profile_room_id
from bridge.note_mirroring import set_ghost_room_banner as _set_ghost_room_banner
from bridge.note_mirroring import set_profile_user_id as _set_profile_user_id
from bridge.note_mirroring import protect_profile_user_id_power_level as _protect_profile_user_id_power_level
from bridge.note_mirroring import SOCIAL_PROFILE_USER_ID_STATE_TYPE, SOCIAL_PROFILE_USER_ID_POWER_LEVEL
from bridge.note_mirroring import source_post_url as _source_post_url
from bridge.repository import ActorRecord, FederatedEvent, GhostProfile, PendingGuildFollow, RemoteActorRoom
from bridge.room_widget import add_bridge_widget
from bridge.spaces import add_room_to_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# ";" rather than something like "!" or "." -- both extremely common
# defaults for other Matrix bots/bridges (maubot plugins, IRC/Discord
# bridges, ...) that might share a room with this one -- and never "/",
# which Element and other clients already reserve client-side for their own
# slash commands (/me, /ban, ...) before an event is even sent, so a "/"
# prefix could never actually reach us as ordinary message text in the first
# place. ";" is short (one character, matching the brief to keep it as
# terse as tagging allows) and, in practice, rare as a bot prefix.
_COMMAND_PREFIX = ";"

_COMMAND_KEYWORDS = (
    r"unlink|unfollow|following|import|follow|link|delete|create|replace|rejoin|banner|"
    r"dm|chat|boost|repost|show|hide|unblock|block|unmute|mute|backfill|widget|leave|help|"
    r"disallow|allowed|allow|refresh|joinguild|leaveguild"
)


_COMMAND_RE = re.compile(rf"\b({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE | re.DOTALL)

# Anchored to the very start of the line and requires a recognized keyword
# to immediately follow (unlike _COMMAND_RE, which just looks for one
# anywhere in the line) -- a bare ";" prefix with no valid command after it
# is deliberately NOT treated as addressing the bot at all, unlike tagging
# it directly. Tagging the bot's literal mxid is an unambiguous, deliberate
# action; a one-character symbol at the start of a line is comparatively
# likely to appear in someone's ordinary message text for unrelated reasons
# (a sentence that just starts with ";)", a Lisp snippet, ...) -- requiring
# an actual known keyword right after it keeps those from being mistaken
# for a command and swallowed/never federated (see message_addresses_bot).
_PREFIXED_COMMAND_RE = re.compile(
    rf"^{re.escape(_COMMAND_PREFIX)}\s*({_COMMAND_KEYWORDS})\b\s*(.*)", re.IGNORECASE | re.DOTALL
)

# _SOCIAL_PROFILE_ROOM_TYPE and _KNOCK_JOIN_RULE now live in
# bridge.note_mirroring (imported above, aliased to these same names) --
# bridge.note_mirroring.import_note needs them too, for the exact same
# room-creation shape, and commands.py already depended on inbox_dispatch.py
# in a way that made note_mirroring.py the only cycle-free place for
# anything shared between the two.


def _extract_command(body: str) -> tuple[str, str] | None:
    """Find a recognized command keyword on the first line of ``body``.

    Returns ``(keyword, rest_of_line)``, or None if no keyword is found.
    """
    first_line = body.split("\n", 1)[0]
    match = _COMMAND_RE.search(first_line)
    if not match:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _extract_prefixed_command(body: str) -> tuple[str, str] | None:
    """Find a ``;``-prefixed command keyword anchored to the very start of
    ``body``'s first line (see ``_PREFIXED_COMMAND_RE`` for why this is
    stricter than ``_extract_command``'s anywhere-in-the-line search).

    Returns ``(keyword, rest_of_line)``, or None if the first line isn't a
    recognized ``;command``.
    """
    first_line = body.split("\n", 1)[0]
    match = _PREFIXED_COMMAND_RE.match(first_line)
    if not match:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _bot_mxid(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def message_addresses_bot(content: dict, config) -> bool:
    """Whether a Matrix message's content tags/addresses the bridge bot --
    via a proper Matrix intentional mention (``m.mentions.user_ids``), the
    bot's bare MXID appearing anywhere in the plain body (some clients never
    send intentional mentions at all, or someone might type the MXID by hand
    without it becoming a real pill), or the ``;command`` shorthand prefix
    (see ``_PREFIXED_COMMAND_RE``) at the very start of the message.

    Used both to recognize a bot command (see ``maybe_handle_command``,
    below) and, independently, as a safety check in the outbound
    federation paths (``bridge.profile_posts``, ``bridge.reply_bridge``) so
    a message that's clearly directed at the bot is never mistaken for an
    ordinary post and sent out over ActivityPub -- even in a hypothetical
    case where it isn't recognized as a valid command (a typo, an unknown
    subcommand, ...), since it still was never meant to be public. Messages
    about internal bridge bookkeeping should stay in Matrix, full stop.

    Safe against a ghosted fediverse account's own mirrored post incidentally
    starting with ";": ``maybe_handle_command`` (the only place this feeds
    into an actual command execution) separately checks the event's
    ``sender`` against our own ghost namespace, same as it always has for
    the tag-based forms -- this function only decides whether a message
    LOOKS bot-directed, not who gets to act on that."""
    bot_mxid = _bot_mxid(config)
    mentioned = (content.get("m.mentions") or {}).get("user_ids") or []
    body = content.get("body") or ""
    if bot_mxid in mentioned or bot_mxid in body:
        return True
    return _extract_prefixed_command(body) is not None


# Single-word commands blocked outright for a third-party sender currently
# effective-Follow-Only, regardless of argument (see _effective_third_party_mode).
_FOLLOW_ONLY_BLOCKED_ANY_ARG = {"banner", "dm", "chat", "backfill", "repost", "boost"}

# (subcommand, lowercased argument) pairs blocked the same way -- these
# subcommands only actually dispatch to anything when paired with this
# exact argument (see maybe_handle_command's own dispatch chain), so this
# mirrors that shape rather than blocking the subcommand word outright.
_FOLLOW_ONLY_BLOCKED_EXACT = {
    ("create", "profile"),
    ("link", "profile"),
    ("unlink", "profile"),
    ("replace", "room"),
}


async def _effective_third_party_mode(request: Request, record: ActorRecord) -> str:
    """"full" or "follow_only" -- computed FRESH on every call from three
    live inputs (``record.is_third_party``, the CURRENT
    ``config.bridge.third_party_access_mode``, and whether ``record.room_id``
    is currently set), never cached anywhere. A local user's record always
    resolves to "full" here regardless of config.

    This is deliberately re-derived at every call site rather than stored,
    so an admin flipping ``third_party_access_mode`` back and forth is
    always safe and takes effect everywhere at once -- see
    ``ActorRecord.is_third_party``'s docstring for the full reasoning."""
    if not record.is_third_party:
        return "full"
    mode = request.app.state.config.bridge.third_party_access_mode
    if mode == "full" and record.room_id:
        return "full"
    return "follow_only"


async def is_third_party_still_allowed(request: Request, record: ActorRecord, *, room_id: str) -> bool:
    """Whether ``record`` is still allowed to federate AT ALL right now --
    not just run commands. A local user's ``ActorRecord`` existing is
    always sufficient (it could only have been created via an
    already-gated ``;create profile``/``;link profile``), but a
    third-party identity's continued right to federate ANYTHING --
    reactions, replies, DMs, Chat, post distribution, not just new
    commands -- depends on still being allowlisted, since revoking a grant
    deliberately never tears down an already-provisioned identity (see
    this feature's design notes). Call this at every point one of those
    paths resolves the acting ``ActorRecord``, using the room the
    triggering Matrix event itself arrived in; treat ``False`` the same as
    "no linked profile" (drop the action silently)."""
    if not record.is_third_party:
        return True
    repository = request.app.state.repository
    homeserver = record.matrix_user_id.split(":", 1)[1] if ":" in record.matrix_user_id else ""
    return await repository.is_third_party_allowed(mxid=record.matrix_user_id, homeserver=homeserver, room_id=room_id)


async def maybe_handle_command(request: Request, event: dict) -> bool:
    """Handle a bot-tagged command event.

    Returns True if the message tagged the bot (handled, regardless of
    whether a recognized command keyword was found in it).
    """
    if event.get("type") != "m.room.message":
        return False

    content = event.get("content") or {}
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)

    if not message_addresses_bot(content, config):
        return False
    body = content.get("body") or ""

    sender = event.get("sender", "")
    room_id = event.get("room_id", "")
    if not sender or not room_id:
        return False

    # Never act on our own ghosts/bot talking -- avoids command loops, and
    # (together with message_addresses_bot's own docstring note) is what
    # keeps a remote fediverse post that happens to start with ";" from
    # ever being mistaken for a ;command: a mirrored post always arrives
    # here as a message from that account's ghost, so it's caught right
    # here regardless of which form (tag or ;prefix) made
    # message_addresses_bot return True for it.
    if sender == bot_mxid or sender.startswith(f"@{config.appservice.user_prefix}"):
        return True

    # The ;prefix form is checked first and, unlike _extract_command, only
    # ever matches when a real keyword immediately follows the prefix (see
    # _PREFIXED_COMMAND_RE) -- so it's never the reason the fallback below
    # gets hit; that fallback stays reachable only via the tag-based forms,
    # exactly as before this shorthand existed.
    parsed = _extract_prefixed_command(body) or _extract_command(body)
    if parsed is None:
        # Tagging the bot with nothing else recognizable as a command --
        # either truly nothing beyond the tag/prefix itself, or some other
        # text that isn't a known command keyword -- once the tag's own
        # mxid/display name and a bare prefix are stripped out. Show the
        # full help message directly for the "nothing else" case (most
        # likely someone just poking the bot to see what it does), and a
        # brief pointer to it otherwise (already tried to say SOMETHING,
        # so a whole command table would bury that they got it wrong).
        residue = body.replace(bot_mxid, "")
        if config.appservice.bot_display_name:
            residue = residue.replace(config.appservice.bot_display_name, "")
        residue = residue.strip(f" \t\n\r{_COMMAND_PREFIX}")
        if not residue:
            await _handle_help(request, room_id=room_id, sender=sender)
        else:
            await _notice(
                request, room_id, f'Not sure what you mean -- try "{_COMMAND_PREFIX}help" to see what I can do.'
            )
        return True
    subcommand, argument = parsed

    # Set for the rest of this dispatch (reset in the finally below) so
    # _notice -- and the handful of raw send_message_event calls for
    # responses _notice's plain-notice shape can't express -- automatically
    # keep every response in the same thread the command itself was run in,
    # with no explicit relates_to threaded through each individual call.
    # See _command_relates_to_var's own docstring for why a ContextVar
    # rather than a plain parameter.
    token = _command_relates_to_var.set(_preserve_command_thread(content, event.get("event_id")))
    try:
        if subcommand == "help":
            help_arg = argument.strip().lower()
            await _handle_help(
                request, room_id=room_id, sender=sender, show_all=help_arg == "all", show_admin=help_arg == "admin",
            )
            return True

        # Every other command mints identities or triggers signed federation
        # traffic under our own domain, so they're only honored for users on our
        # own homeserver -- anyone can invite the bot into a room they control
        # from any Matrix server, and without this check they could otherwise
        # squat usernames on our domain or use our bridge's reputation to follow
        # arbitrary fediverse accounts. An admin can lift this for specific
        # outside users/rooms/homeservers via ";allow" -- see
        # is_third_party_allowed and _effective_third_party_mode.
        repository = request.app.state.repository
        sender_server = sender.split(":", 1)[1] if ":" in sender else ""
        is_third_party_sender = sender_server != config.synapse.server_name
        is_delete_profile_command = subcommand == "delete" and argument.lower() == "profile"
        # ";delete profile" is exempt from the allowlist check itself (not
        # just the Follow-Only block below) -- it's their own identity to
        # walk away from, and revoking a grant deliberately never tears one
        # down on its own (see this feature's design notes), so someone
        # already removed from the allowlist must still be able to trigger
        # their own signed Delete/cleanup rather than being stuck as an
        # orphaned identity with no way to remove themselves.
        if is_third_party_sender and not is_delete_profile_command:
            allowed = await repository.is_third_party_allowed(
                mxid=sender, homeserver=sender_server, room_id=room_id
            )
            if not allowed:
                logger.info("Ignoring tagged command from non-local, non-allowlisted user %s", sender)
                await _notice(
                    request, room_id, f"Commands are only available to users on {config.synapse.server_name}."
                )
                return True

        # Any allowed third-party sender is effective-Follow-Only whenever
        # the CURRENT global mode isn't "full" -- this is deliberately
        # decided from the live config alone, not from whether an
        # ActorRecord exists yet or what its room_id is: per this feature's
        # truth table, Follow Only always wins for a third party regardless
        # of a leftover room_id from a past Full period, and a sender with
        # NO record yet must be stopped from self-service-provisioning a
        # full Profile Room via ";create profile"/";link profile" just as
        # much as one who already has an identity -- only ";follow" is
        # allowed to auto-mint one for them in that case (see
        # _handle_follow). ";delete profile" is deliberately exempt --
        # it's the one thing a third-party user should always be able to do
        # to their own identity regardless of current allowlist/mode state.
        if is_third_party_sender and config.bridge.third_party_access_mode != "full":
            if not is_delete_profile_command:
                normalized_argument = argument.strip().lower()
                if subcommand in _FOLLOW_ONLY_BLOCKED_ANY_ARG:
                    if subcommand == "chat":
                        await _notice(request, room_id, "Chat is disabled for third-party accounts in Follow Only mode.")
                    else:
                        await _notice(
                            request, room_id,
                            f'"{_COMMAND_PREFIX}{subcommand}" is disabled for third-party accounts in Follow Only mode.',
                        )
                    return True
                if (subcommand, normalized_argument) in _FOLLOW_ONLY_BLOCKED_EXACT:
                    await _notice(
                        request, room_id,
                        f'"{_COMMAND_PREFIX}{subcommand} {normalized_argument}" is disabled for '
                        "third-party accounts in Follow Only mode.",
                    )
                    return True

        if subcommand == "follow":
            await _handle_follow(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "unfollow":
            await _handle_unfollow(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "import" and argument.strip().lower() in ("follows", "following"):
            await _handle_import_follows(request, sender=sender, room_id=room_id, content=content)
        elif subcommand == "import":
            await _handle_import(request, sender=sender, room_id=room_id, url=argument)
        elif subcommand == "link" and argument.lower() == "profile":
            await _handle_link_profile(request, sender=sender, room_id=room_id)
        elif subcommand == "unlink" and argument.lower() == "profile":
            await _handle_unlink_profile(request, sender=sender, room_id=room_id)
        elif subcommand == "delete" and argument.lower() == "profile":
            await _handle_delete_profile(request, sender=sender, room_id=room_id)
        elif subcommand == "create" and argument.lower() == "profile":
            await _handle_create_profile(request, sender=sender, room_id=room_id)
        elif subcommand == "replace" and argument.lower() == "room":
            await _handle_replace_room(request, sender=sender, room_id=room_id)
        elif subcommand == "leave" and argument.lower() == "unfollowed":
            await _handle_leave_unfollowed(request, sender=sender, room_id=room_id)
        elif subcommand == "rejoin":
            await _handle_rejoin(request, sender=sender, room_id=room_id, argument=argument)
        elif subcommand == "following":
            await _handle_list_following(request, sender=sender, room_id=room_id)
        elif subcommand in ("show", "hide"):
            await _handle_set_collection_visibility(
                request, sender=sender, room_id=room_id, hidden=(subcommand == "hide"), argument=argument,
            )
        elif subcommand == "block":
            await _handle_block(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "unblock":
            await _handle_unblock(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "mute":
            await _handle_mute(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "unmute":
            await _handle_unmute(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "banner":
            await _handle_banner(request, sender=sender, room_id=room_id, argument=argument)
        elif subcommand == "dm":
            await _handle_dm(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "chat":
            await _handle_chat(request, sender=sender, room_id=room_id, handle=argument, content=content)
        elif subcommand == "boost":
            # Undocumented alias for a caption-less ";repost" -- kept for
            # muscle memory/anyone still typing the old keyword, but never
            # shown in _handle_help/COMMANDS.md (see _handle_repost's own
            # docstring for the full reasoning). Always treated as
            # caption-less regardless of any trailing text, matching what
            # ";boost" always meant historically -- it never took a caption.
            await _handle_repost(
                request, sender=sender, room_id=room_id, content=content, caption="",
                event_id=event.get("event_id"),
            )
        elif subcommand == "repost":
            await _handle_repost(
                request, sender=sender, room_id=room_id, content=content, caption=argument,
                event_id=event.get("event_id"),
            )
        elif subcommand == "backfill":
            await _handle_backfill(request, sender=sender, room_id=room_id, argument=argument, content=content)
        elif subcommand == "widget":
            await _handle_widget(request, room_id=room_id)
        elif subcommand == "allow":
            await _handle_allow(request, sender=sender, room_id=room_id, argument=argument)
        elif subcommand == "disallow":
            await _handle_disallow(request, sender=sender, room_id=room_id, argument=argument)
        elif subcommand == "allowed":
            await _handle_allowed(request, sender=sender, room_id=room_id)
        elif subcommand == "refresh" and argument.strip().lower() == "poll":
            await _handle_poll_refresh(request, room_id=room_id, content=content)
        elif subcommand == "refresh":
            await _handle_refresh(request, sender=sender, room_id=room_id, argument=argument, content=content)
        elif subcommand == "joinguild":
            await _handle_joinguild(request, sender=sender, room_id=room_id, argument=argument)
        elif subcommand == "leaveguild":
            await _handle_leaveguild(request, sender=sender, room_id=room_id)
        else:
            await _notice(
                request, room_id, f'Unknown command -- try "{_COMMAND_PREFIX}help" to see what I can do.'
            )
        return True
    finally:
        _command_relates_to_var.reset(token)


def _check_command_keywords_cover_dispatch() -> None:
    """Fails loudly, at import time, if ``_COMMAND_KEYWORDS`` is missing a
    subcommand this function's own ``elif subcommand == "..."``/``subcommand
    in (...)`` dispatch chain actually recognizes -- confirmed live
    2026-07-11, TWICE (``;poll refresh`` and, moments earlier, ``;refresh``
    itself): a subcommand added to the dispatch chain without also adding
    it here is never recognized as addressing the bot at all (see
    ``message_addresses_bot``'s own ``;``-prefix check, which requires a
    keyword match specifically so an ordinary message that merely starts
    with ``;`` for unrelated reasons isn't swallowed) -- so the message
    falls through this function entirely and gets federated to the
    fediverse as if it were an ordinary post/reply, silently leaking a
    bot command onto the network instead of running it. Parses the
    keywords straight out of THIS function's own source (the actual
    dispatch, the only real source of truth) rather than a second
    hand-maintained list, so the two can never silently drift apart again
    -- a missing keyword now crashes the bridge on startup instead of
    leaking a future command over ActivityPub in production."""
    source = inspect.getsource(maybe_handle_command)
    dispatched = set(re.findall(r'subcommand == "([a-z_]+)"', source))
    for group in re.findall(r'subcommand in \(([^)]+)\)', source):
        dispatched.update(re.findall(r'"([a-z_]+)"', group))
    recognized = set(_COMMAND_KEYWORDS.split("|"))
    missing = dispatched - recognized
    if missing:
        raise AssertionError(
            f"_COMMAND_KEYWORDS is missing {sorted(missing)} -- present in maybe_handle_command's own dispatch "
            "chain but not recognized by the ;prefix command detector, so a message using it would silently "
            "fall through to federation instead of running the command. Add it to _COMMAND_KEYWORDS."
        )


_check_command_keywords_cover_dispatch()


# The bot response(s) to whichever command is currently being dispatched,
# so a command run as a thread reply gets every one of its responses kept
# in that same thread instead of landing at the room root -- set once, in
# maybe_handle_command, around the dispatch to whichever _handle_* was
# matched, then read automatically by _notice (and a handful of raw
# send_message_event calls for responses _notice's plain-notice shape
# can't express, e.g. _handle_help's rich HTML). A ContextVar rather than
# a parameter threaded through every handler/_notice call (which would
# mean touching ~130 call sites across ~25 functions) because a few
# handlers (_run_backfill, _run_follows_import) hand off to a detached
# asyncio.create_task that can still be running long after
# maybe_handle_command has returned -- a Task copies the current context
# at creation time, so it keeps seeing the right value regardless of
# whatever the outer request handling does afterward, without needing
# this threaded through its own signature either.
_command_relates_to_var: ContextVar[dict | None] = ContextVar("_command_relates_to_var", default=None)


async def _notice(
    request: Request, room_id: str, message: str, *, relates_to: dict | None = None, html_message: str | None = None,
) -> None:
    """Sends a plain ``m.notice`` -- ``message`` -- or, when ``html_message``
    is given too, an HTML-formatted one instead (``message`` stays the
    plain-text fallback for clients/notification text that don't render
    ``formatted_body``). Pass ``html_message`` whenever the notice names a
    room -- e.g. via ``bridge.matrix_links.room_pill_html`` -- so it renders
    as a real clickable pill rather than a bare, unclickable room id."""
    config = request.app.state.config
    if relates_to is None:
        relates_to = _command_relates_to_var.get()
    content: dict = {"msgtype": "m.notice", "body": message}
    if html_message is not None:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html_message
    if relates_to:
        content["m.relates_to"] = relates_to
    try:
        await request.app.state.synapse.send_message_event(room_id, content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send notice to %s", room_id, exc_info=True)


def _command_reply_relates_to(content: dict, command_event_id: str | None) -> dict | None:
    """``m.relates_to`` for the bot's own reply to a ``;boost``/``;repost``
    command message -- so its "Reposted." confirmation lands as
    a reply to the command itself (and, if the command was sent as a
    thread reply, in that same thread -- see ``thread_reply_relates_to``)
    instead of a bare unrelated message in the room. None if there's no
    command event id to reply to at all (shouldn't normally happen, but
    every ``_notice`` call already tolerates it being omitted).

    Unlike ``_preserve_command_thread`` below, this ALWAYS ties the reply
    to the command -- starting a fresh thread rooted at the command
    itself even if it wasn't already in one -- which is deliberately
    right for ;boost/;repost's own confirmation but too aggressive as the
    general behavior every other command's plainer notices should get."""
    if not command_event_id:
        return None
    command_relates_to = content.get("m.relates_to") or {}
    thread_root_event_id = (
        command_relates_to.get("event_id") if command_relates_to.get("rel_type") == "m.thread" else None
    )
    return thread_reply_relates_to(event_id=command_event_id, thread_root_event_id=thread_root_event_id)


def _preserve_command_thread(content: dict, command_event_id: str | None) -> dict | None:
    """``m.relates_to`` for a general command response, so running a
    command as a thread reply keeps its response(s) in that same thread
    instead of at the room root. None (today's plain, unrelated message)
    if the command wasn't already sent inside an existing thread --
    deliberately NOT starting a brand new thread for an ordinary,
    not-yet-threaded command the way ``_command_reply_relates_to`` does
    for ;boost/;repost, since turning every simple command reply into a
    visible "in reply to" quote block would be a bigger, unrequested
    change to how the bulk of commands read."""
    if not command_event_id:
        return None
    command_relates_to = content.get("m.relates_to") or {}
    if command_relates_to.get("rel_type") != "m.thread":
        return None
    thread_root_event_id = command_relates_to.get("event_id")
    if not thread_root_event_id:
        return None
    return thread_reply_relates_to(event_id=command_event_id, thread_root_event_id=thread_root_event_id)


async def _last_event_id(request: Request, room_id: str, *, as_user_id: str) -> str | None:
    """Best-effort: the most recent event ID in ``room_id``, for use as a
    room-replacement's ``predecessor.event_id`` -- fetched *before* the old
    room is tombstoned (see ``_replace_profile_room``/``_replace_remote_actor_room``),
    since a room's ``creation_content`` (which is where ``predecessor`` lives)
    is immutable and must be set in the very same call that creates the new
    room, before the old room's tombstone event (and its own event ID) exist
    yet. None if the room has no events yet or the lookup fails -- callers
    just omit ``event_id`` from ``predecessor`` in that case, per spec ("It is
    possible for this event ID to be undefined")."""
    try:
        result = await request.app.state.synapse.get_room_messages(
            room_id, limit=1, direction="b", as_user_id=as_user_id,
        )
    except SynapseError:
        return None
    chunk = result.get("chunk") or []
    return chunk[0]["event_id"] if chunk else None


async def _send_tombstone(
    request: Request, *, old_room_id: str, new_room_id: str, as_user_id: str, body: str
) -> None:
    """Mark ``old_room_id`` as superseded by ``new_room_id`` with a standard
    room-upgrade ``m.room.tombstone`` state event, so bridge-aware clients
    can point people at the replacement the same way they would for a
    regular room upgrade. ``as_user_id`` must hold sufficient power level in
    the OLD room (the room's creator, in practice -- either the bot or the
    ghost that made it, depending on which kind of room this is).
    Best-effort -- the old room already carries a plain-text notice pointing
    at the new one regardless."""
    try:
        await request.app.state.synapse.send_state_event(
            old_room_id, "m.room.tombstone", "", {"body": body, "replacement_room": new_room_id},
            as_user_id=as_user_id,
        )
    except SynapseError:
        logger.info("Could not set m.room.tombstone state event in %s", old_room_id, exc_info=True)


_REPLACED_SUFFIX_RE = re.compile(r" \(Replaced \d{2}/\d{2}/\d{4}\)$")


def _replaced_suffix() -> str:
    return f" (Replaced {datetime.now(timezone.utc).strftime('%m/%d/%Y')})"


async def _mark_room_replaced(request: Request, *, old_room_id: str, as_user_id: str) -> None:
    """Rename the old room to flag it as superseded (appending a
    ``(Replaced MM/DD/YYYY)`` suffix to whatever its current name already
    is), so someone who ends up there anyway (an old bookmark, a stale room
    list entry, a matrix.to link some other room's post still points at,
    ...) can tell at a glance it's not the current one -- the tombstone and
    plain-text notice already cover that, but not every client surfaces a
    tombstone prominently, and a renamed room list entry is unmissable.
    ``as_user_id`` needs the same power level in the OLD room
    ``_send_tombstone`` does. Best-effort, and a no-op if the room has no
    name to begin with (nothing to append the suffix to), or if it's
    already been marked (a second replace of an already-replaced room)."""
    synapse = request.app.state.synapse
    try:
        content = await synapse.get_room_state(old_room_id, "m.room.name", as_user_id=as_user_id)
    except SynapseError:
        return
    current_name = content.get("name")
    if not current_name or _REPLACED_SUFFIX_RE.search(current_name):
        return
    try:
        await synapse.send_state_event(
            old_room_id, "m.room.name", "", {"name": f"{current_name}{_replaced_suffix()}"}, as_user_id=as_user_id
        )
    except SynapseError:
        logger.info("Could not rename replaced room %s", old_room_id, exc_info=True)


async def _handle_help(
    request: Request, *, room_id: str, sender: str, show_all: bool = False, show_admin: bool = False,
) -> None:
    """Sent as an ordinary ``m.text`` message, not ``m.notice`` -- someone
    asking what the bot can do should get an answer that stands out, not
    one muted into whatever quieter styling (or, per
    ``.m.rule.suppress_notices``, no notification at all) a client gives
    notices. Each command's own invocation is bolded in the HTML rendering,
    set apart from its description below it -- a plain, unstyled wall of
    "command\\n  description" pairs is hard to scan for the one command
    you're actually looking for.

    Three tiers, each hidden from the ones "below" it: ``show_all``
    (``;help all``) additionally lists the room-maintenance/advanced
    commands -- hidden from the ordinary ``;help`` since they're one-off
    account/room-recovery operations (or things like ``;refresh poll``)
    almost nobody needs day to day, not things a new user scanning "how do
    I follow someone" should have to scroll past. ``show_admin``
    (``;help admin``) instead lists ONLY the commands that are actually
    gated to a Matrix server admin (``;allow``/``;disallow``/``;allowed``/
    ``;refresh``)
    -- kept out of ``;help all`` entirely, since they're not "advanced
    user" features, they're admin-only and irrelevant to (can't even be
    run by) everyone else. Deliberately no combined "all + admin" view --
    each tier answers one specific "what can I do" question.

    ``show_admin`` is refused outright (with its own notice, nothing listed)
    for anyone ``_is_matrix_admin`` doesn't recognize -- the admin tier lists
    real capabilities a non-admin literally cannot use, not just
    "advanced"/inconvenient ones, so exposing it is a real (if minor)
    information leak, not just clutter. The pointer to ``;help admin`` in
    the other two tiers' own outro is hidden the same way, for the same
    reason -- a non-admin has no use for it and no way to act on it, so
    advertising it at all is pointless at best."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    is_admin = await _is_matrix_admin(request, sender)
    if show_admin and not is_admin:
        await _notice(request, room_id, "Only a Matrix server admin can use this.")
        return
    intro = (
        f'Hi, I\'m the fediverse bridge! Start a message with "{_COMMAND_PREFIX}" to give me a command '
        f'(e.g. "{_COMMAND_PREFIX}help"). You can also tag me in place of "{_COMMAND_PREFIX}":'
    )
    intro_html = (
        f"Hi, I'm the fediverse bridge! Start a message with <code>{html.escape(_COMMAND_PREFIX)}</code> to give "
        f"me a command (e.g. <code>{html.escape(_COMMAND_PREFIX)}help</code>). You can also tag me in place of "
        f"<code>{html.escape(_COMMAND_PREFIX)}</code>:"
    )
    commands = [
        (f"{_COMMAND_PREFIX}help", "Show this message."),
        (
            f"{_COMMAND_PREFIX}create profile",
            f"Set up a new fediverse identity ({config.bridge.domain}) and Matrix room for you in one step.",
        ),
        (
            f"{_COMMAND_PREFIX}follow @user@instance.org",
            "Follow a fediverse account, creating or reusing a room for their posts "
            f'(requires a linked profile first). Undo it with "{_COMMAND_PREFIX}unfollow".',
        ),
        (
            f"{_COMMAND_PREFIX}following",
            "List every fediverse account you're following.",
        ),
        (
            f"{_COMMAND_PREFIX}dm @user@instance.org",
            "Start or reuse a private direct-message room with a fediverse account.",
        ),
        (
            f"{_COMMAND_PREFIX}chat @user@instance.org",
            "Start or reuse an ActivityPub Chat with a fediverse account.",
        ),
        (
            f"{_COMMAND_PREFIX}import <url>",
            "Fetch and mirror a single fediverse post by its URL.",
        ),
        (
            f"{_COMMAND_PREFIX}repost [<caption>]",
            "Reply to a fediverse post with this to repost it -- bare, a plain repost (same as reacting with "
            "🔁); with a caption, a new post of your own quoting the original with your added commentary.",
        ),
        (
            f"{_COMMAND_PREFIX}banner mxc://server/mediaid",
            "Set your fediverse profile's banner image from already-uploaded Matrix media.",
        ),
    ]
    # Advanced/maintenance commands -- one-off account/room recovery, not
    # day-to-day use -- only shown for "help all", appended after the
    # regular ones so they still read as a coherent single table.
    advanced_commands = [
        (
            f"{_COMMAND_PREFIX}link profile",
            f"Bind this room to your fediverse identity ({config.bridge.domain}) instead of creating a new one.",
        ),
        (
            f"{_COMMAND_PREFIX}unlink profile",
            "Detach this room from your identity without notifying the fediverse. "
            "Relinking (even in a different room) reattaches the same identity.",
        ),
        (
            f"{_COMMAND_PREFIX}delete profile",
            "Permanently delete your identity and notify your followers. Irreversible.",
        ),
        (
            f"{_COMMAND_PREFIX}replace room",
            "Replace this room with a freshly-created one for the same identity, fixing missing features.",
        ),
        (
            f"{_COMMAND_PREFIX}rejoin <room_id> [@other:matrix.id]",
            "Force an invite into a room the bridge manages, e.g. to recover from a lockout.",
        ),
        (
            f"{_COMMAND_PREFIX}leave unfollowed",
            "Leave every Remote User Room you're a member of but no longer (or never actually) follow. "
            "Shows a count and asks for confirmation first.",
        ),
        (
            f"{_COMMAND_PREFIX}refresh poll",
            "Reply to a poll (or anything in its thread) to actively re-fetch its current tallies/closed "
            "state right now, rather than waiting for the remote server to push an update (some, like "
            "Pleroma/Akkoma, never do). Any local user -- unlike the admin-only ;refresh below.",
        ),
        (
            f"{_COMMAND_PREFIX}hide followers",
            'Hide your followers list from remote viewers (also works with "following"; your counts stay '
            f'public either way). Undo it with "{_COMMAND_PREFIX}show" (visible by default).',
        ),
        (
            f"{_COMMAND_PREFIX}block @user@instance.org",
            "Block an account (or run with no argument inside the room representing them): cuts any existing "
            f'follow, kicks you from their room/DM/chat, declines future follows, and silences them like '
            f'"{_COMMAND_PREFIX}mute" below. "{_COMMAND_PREFIX}unblock" undoes it.',
        ),
        (
            f"{_COMMAND_PREFIX}import follows",
            "Import a follows list exported from another fediverse account: upload the CSV "
            "(Pleroma/Akkoma data export, or Mastodon's follows CSV) to a room I'm in, then reply to "
            "that upload with this command. Already-followed accounts are skipped; a summary with any "
            "failures follows when it finishes.",
        ),
        (
            f"{_COMMAND_PREFIX}mute @user@instance.org",
            "Mute an account (or run with no argument inside their room): no notifications about them, and "
            f'no auto-invites into a DM/chat/mention room because of them. "{_COMMAND_PREFIX}unmute" undoes it.',
        ),
        (
            f"{_COMMAND_PREFIX}backfill [N]",
            f"Pull that account's latest {config.bridge.backfill_default_count} posts into this room "
            "(already-mirrored ones are skipped). Run as a reply inside a Matrix thread mirroring a "
            "fediverse conversation to backfill that thread's replies instead. Only a Matrix server admin "
            "can pass a custom N.",
        ),
        (
            f"{_COMMAND_PREFIX}widget",
            "Add a room widget with buttons for most of these commands, for clients that support "
            "Matrix widgets.",
        ),
    ]
    # Commands actually GATED to a Matrix server admin -- nobody else can
    # run these at all, as opposed to advanced_commands above (one-off
    # account/room-recovery operations any user can run for their own
    # stuff). Its own tier ("help admin"), never folded into "help all".
    admin_commands = [
        (
            f"{_COMMAND_PREFIX}allow mxid|room|homeserver <value>",
            "Let a user on a DIFFERENT homeserver (an exact MXID, anyone in a room, or a whole homeserver's "
            "users) use this bridge -- in whatever mode bridge.third_party_access_mode currently configures. "
            f'Whitelisting a homeserver asks for confirmation first. "{_COMMAND_PREFIX}disallow" undoes any '
            "of these.",
        ),
        (
            f"{_COMMAND_PREFIX}allowed",
            "List every current third-party access grant.",
        ),
        (
            f"{_COMMAND_PREFIX}refresh [@user@instance.org]",
            "Re-fetch a ghost's live profile right now and bring their display name/avatar, their room's "
            "name/avatar/banner, and their MSC4503 external-handle profile field (set or removed, matching "
            "whatever bridge.msc4503_external_handle currently allows) all up to date immediately. The handle "
            "can be omitted when run inside that account's own room.",
        ),
    ]
    if show_all:
        commands = commands + advanced_commands
    elif show_admin:
        commands = commands + admin_commands

    body = intro + "\n\n" + "\n\n".join(f"{cmd}\n  - {desc}" for cmd, desc in commands)
    formatted_body = (
        f"<p>{intro_html}</p>"
        "<table><thead><tr><th>Command</th><th>Description</th></tr></thead>"
        f"<tbody>{''.join(f'<tr><td>{html.escape(cmd)}</td><td>{html.escape(desc)}</td></tr>' for cmd, desc in commands)}</tbody></table>"
    )
    if not show_all:
        outro = f'Advanced/maintenance commands are hidden here -- use "{_COMMAND_PREFIX}help all" to see them too.'
        outro_html = (
            f"Advanced/maintenance commands are hidden here -- use "
            f"<code>{html.escape(_COMMAND_PREFIX)}help all</code> to see them too."
        )
        body += f"\n\n{outro}"
        formatted_body += f"<p>{outro_html}</p>"
    if not show_admin and is_admin:
        outro = f'Matrix server admin commands are hidden here -- use "{_COMMAND_PREFIX}help admin" to see them.'
        outro_html = (
            f"Matrix server admin commands are hidden here -- use "
            f"<code>{html.escape(_COMMAND_PREFIX)}help admin</code> to see them."
        )
        body += f"\n\n{outro}"
        formatted_body += f"<p>{outro_html}</p>"
    help_content: dict = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
    }
    relates_to = _command_relates_to_var.get()
    if relates_to:
        help_content["m.relates_to"] = relates_to
    try:
        await request.app.state.synapse.send_message_event(room_id, help_content, as_user_id=bot_mxid)
    except SynapseError:
        logger.warning("Failed to send help message to %s", room_id, exc_info=True)


_COMMAND_PILL_MXID_RE = re.compile(r'href="https://matrix\.to/#/(@[^"/?]+)"')


def _mentioned_mxids(content: dict) -> list[str]:
    """All mxids ``content`` names as a mention target -- both the
    structured, reliable ``m.mentions.user_ids`` (MSC3952 intentional
    mentions, set when a mention is picked from a client's autocomplete) AND
    any matrix.to pill anchor literally present in ``formatted_body``. The
    latter covers a pill that ended up in the message body WITHOUT also
    setting ``m.mentions`` -- e.g. pasted in rather than picked from
    autocomplete -- which is otherwise indistinguishable from a hand-typed
    handle to everything downstream of here. Order doesn't matter to any
    caller, only membership; de-duplicated so a pill that's ALSO in
    ``m.mentions`` (the common case) isn't checked twice."""
    mxids = list((content.get("m.mentions") or {}).get("user_ids") or [])
    formatted_body = content.get("formatted_body")
    if isinstance(formatted_body, str):
        for mxid in _COMMAND_PILL_MXID_RE.findall(formatted_body):
            if mxid not in mxids:
                mxids.append(mxid)
    return mxids


async def _resolve_tagged_ghost(request: Request, content: dict) -> GhostProfile | None:
    """If ``content`` names one of our own ghost users (a fediverse account
    already mirrored here) via ``m.mentions`` or a matrix.to pill in
    ``formatted_body`` -- see ``_mentioned_mxids`` -- return that ghost's
    profile. Lets ``follow``/``unfollow`` accept a tagged ghost pill the
    same way they accept a typed-out ``@user@instance.org`` handle -- without
    a redundant webfinger round-trip, since we already know the actor id
    from having ghosted them before. The reverse-direction counterpart of
    ``bridge.mentions.resolve_pill_mentions``, which does this same
    mxid-to-ghost lookup for an outgoing post's mentions. A bare, hand-typed
    Matrix ID with no pill/mention structure at all is handled separately --
    see ``_resolve_mxid_handle``."""
    config = request.app.state.config
    repository = request.app.state.repository
    ghost_prefix = f"@{config.appservice.user_prefix}"
    for mxid in _mentioned_mxids(content):
        if not mxid.startswith(ghost_prefix):
            continue  # a mention of a real Matrix user, or the bot -- not a fediverse ghost
        profile = await repository.get_ghost_profile_by_mxid(mxid)
        if profile is not None and profile.handle:
            return profile
    return None


async def _resolve_tagged_local_actor(request: Request, content: dict) -> ActorRecord | None:
    """If ``content`` names a real local Matrix user (never a ghost, never
    the bot) via ``m.mentions`` or a matrix.to pill in ``formatted_body`` --
    see ``_mentioned_mxids`` -- who has a linked fediverse profile, return
    their ``ActorRecord``. The local-user counterpart of
    ``_resolve_tagged_ghost`` -- lets ``follow``/``unfollow`` accept tagging
    a fellow bridge user directly, the same way tagging a ghost already
    works."""
    config = request.app.state.config
    repository = request.app.state.repository
    bot_mxid = _bot_mxid(config)
    ghost_prefix = f"@{config.appservice.user_prefix}"
    for mxid in _mentioned_mxids(content):
        if mxid == bot_mxid or mxid.startswith(ghost_prefix):
            continue
        record = await repository.get_local_actor_by_matrix_id(mxid)
        if record is not None:
            return record
    return None


async def _resolve_mxid_handle(request: Request, handle: str) -> tuple[str, str, ActorRecord | None] | None:
    """If ``handle`` is itself a bare Matrix ID (``@localpart:server``) --
    typed by hand with no client-recorded mention structure at all, unlike
    a pill/autocomplete mention (see ``_resolve_tagged_ghost``/
    ``_resolve_tagged_local_actor``/``_mentioned_mxids``, which cover those)
    -- resolves it the same way a tagged mention would: a ghost's own
    cached handle, or a local bridge user's own fediverse handle. Returns
    ``(remote_actor_id, display_handle, local_record)``, shaped exactly
    like ``_resolve_target_actor``'s return value -- ``local_record`` is set
    only for a local bridge user. Returns None if ``handle`` doesn't even
    look like a Matrix ID (most calls -- this is checked well before any
    webfinger attempt), or looks like one but isn't a ghost or local user we
    actually know: callers should fall through to their usual webfinger
    resolution either way, exactly as if this function didn't exist."""
    if not _MXID_RE.match(handle):
        return None
    config = request.app.state.config
    repository = request.app.state.repository
    base = config.bridge.public_base_url

    local_record = await repository.get_local_actor_by_matrix_id(handle)
    if local_record is not None:
        return (
            actor_url(base, local_record.username),
            f"@{local_record.username}@{config.bridge.domain}",
            local_record,
        )

    profile = await repository.get_ghost_profile_by_mxid(handle)
    if profile is not None and profile.handle:
        return profile.actor_id, profile.handle, None

    return None


async def _resolve_target_actor(
    request: Request, *, room_id: str, handle: str, content: dict, verb: str,
) -> tuple[str, str, ActorRecord | None] | None:
    """Resolves a ``block``/``unblock``/``mute``/``unmute`` command's target
    into ``(actor_id, display_handle, local_record)`` -- ``local_record`` is
    the target's own ``ActorRecord`` if they're another LOCAL bridge user,
    else ``None`` for a genuinely remote account. Same resolution order as
    ``follow``/``unfollow`` (see ``_handle_follow``'s docstring): a tagged
    ghost/local-actor mention pill, a bare Matrix ID (see
    ``_resolve_mxid_handle``), an explicit ``@user@instance.org`` handle,
    or -- with no argument at all -- whichever account's own room
    this command was run inside (a Remote User Room, or another local
    user's Profile Room). Unlike ``follow``, never requires an existing
    Remote User Room/fetches the target's actor document at all -- you can
    block/mute someone you've never otherwise interacted with, purely by
    handle. Sends its own usage notice and returns None on failure --
    callers just check for that.
    """
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    tagged_local = None if tagged_ghost is not None else await _resolve_tagged_local_actor(request, content)
    if tagged_local is not None:
        return actor_url(base, tagged_local.username), f"@{tagged_local.username}@{config.bridge.domain}", tagged_local

    if tagged_ghost is not None:
        return tagged_ghost.actor_id, tagged_ghost.handle or tagged_ghost.actor_id, None

    if handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            return mxid_match
        try:
            remote_actor_id = await resolve_remote_actor_id(request.app.state.http_client, handle)
        except WebfingerNotFoundError:
            await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
            return None
        except WebfingerUnreachableError:
            await _notice(
                request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit."
            )
            return None
        except WebfingerError as exc:
            await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
            return None
        local_username = username_from_actor_url(base, remote_actor_id)
        if local_username is not None:
            target = await repository.get_local_actor(local_username)
            if target is None:
                await _notice(request, room_id, f"{handle} doesn't look like an active profile on this bridge.")
                return None
            return remote_actor_id, f"@{target.username}@{config.bridge.domain}", target
        return remote_actor_id, handle, None

    remote_room_here = await repository.get_remote_actor_room_by_room_id(room_id)
    if remote_room_here is not None:
        return remote_room_here.actor_id, remote_room_here.display_name or remote_room_here.actor_id, None

    local_here = await repository.get_local_actor_by_room_id(room_id)
    if local_here is not None:
        return actor_url(base, local_here.username), f"@{local_here.username}@{config.bridge.domain}", local_here

    await _notice(
        request, room_id,
        f'Usage: "{_COMMAND_PREFIX}{verb} @user@instance.org", or run "{_COMMAND_PREFIX}{verb}" '
        "from inside the room representing that account.",
    )
    return None


async def _auto_provision_third_party_actor(request: Request, *, matrix_user_id: str) -> ActorRecord:
    """Mint a fresh Follow-Only identity for ``matrix_user_id`` (a user on a
    DIFFERENT homeserver, already confirmed allowlisted by the caller) the
    first time they ever ``;follow`` anyone -- see this feature's design
    notes and ``ActorRecord.is_third_party``'s docstring. Mirrors
    ``bridge.service_actor.load_or_create_service_actor``'s own
    keypair-minting shape, and the profile-mapping ``_handle_create_profile``
    uses -- but with ``is_third_party=True`` and an empty ``room_id``: this
    actor's AP profile always live-mirrors their current Matrix profile from
    here on (see ``_effective_third_party_mode``/the profile-sync timer)
    rather than being self-controlled via a Profile Room.

    Usernames are derived via ``third_party_username_from_mxid`` -- since
    ``register_local_actor`` is a pure upsert keyed on ``username`` with NO
    uniqueness check, a collision with an already-registered DIFFERENT
    ``matrix_user_id`` (astronomically unlikely given that encoding, but not
    impossible) falls back to a numeric-suffixed username rather than
    silently hijacking the existing identity."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse

    base_username = third_party_username_from_mxid(matrix_user_id)
    username = base_username
    suffix = 2
    while True:
        existing_username_owner = await repository.get_local_actor(username)
        if existing_username_owner is None or existing_username_owner.matrix_user_id == matrix_user_id:
            break
        username = f"{base_username}_{suffix}"
        suffix += 1

    try:
        profile = await synapse.get_profile(matrix_user_id)
    except SynapseError:
        profile = {}
    matrix_display_name = profile.get("displayname")
    matrix_avatar_mxc = profile.get("avatar_url")
    display_name = matrix_display_name or matrix_user_id
    icon_url = None
    if matrix_avatar_mxc:
        try:
            icon_url = media_url(config.bridge.public_base_url, matrix_avatar_mxc)
        except ValueError:
            icon_url = None
        else:
            await repository.mark_media_published(matrix_avatar_mxc)

    private_key_pem, public_key_pem = generate_keypair()
    record = ActorRecord(
        username=username,
        matrix_user_id=matrix_user_id,
        room_id="",
        public_key_pem=public_key_pem,
        private_key_pem=private_key_pem,
        display_name=display_name,
        summary=f"{matrix_user_id} is a Matrix user.",
        icon_url=icon_url,
        is_third_party=True,
    )
    await repository.register_local_actor(record)
    return record


async def _handle_follow(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Follow ``handle`` (``@user@instance.org``) as the sender's own linked
    actor. ``handle`` may be omitted entirely if this is run from inside
    that account's own Remote User Room -- the room itself already names
    exactly who'd otherwise have to be spelled out. A tagged ghost pill
    (``content``'s ``m.mentions``, or a matrix.to pill pasted into
    ``formatted_body`` -- see ``_mentioned_mxids``) works the same as typing
    out the handle -- see ``_resolve_tagged_ghost`` -- and takes priority
    over ``handle`` if somehow both are present, since a resolved mention is
    unambiguous where parsed text could in principle be wrong. ``handle``
    itself may ALSO be a bare Matrix ID (typed by hand, with none of that
    mention structure at all) instead of a ``@user@instance.org`` fediverse
    handle -- see ``_resolve_mxid_handle``, checked first and, on no match,
    falling through to the ordinary webfinger resolution below exactly as
    before.

    If the target turns out to be another LOCAL bridge user -- tagged
    directly (see ``_resolve_tagged_local_actor``), named by their own
    Matrix ID (see ``_resolve_mxid_handle``), or named by their own
    ``@user@ourdomain`` handle, which webfinger resolves exactly like any
    other account's -- this hands off to ``_follow_local_actor`` instead of
    the remote flow below: a ghost/Remote User Room is never appropriate
    for someone who already has a real Matrix account and Profile Room of
    their own (see ``resolve_and_invite_ghost``'s docstring for the full
    reasoning)."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    http_client = request.app.state.http_client
    synapse = request.app.state.synapse
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    tagged_local = None if tagged_ghost is not None else await _resolve_tagged_local_actor(request, content)
    if tagged_local is not None:
        await _follow_local_actor(request, sender=sender, room_id=room_id, target=tagged_local)
        return

    if tagged_ghost is not None:
        remote_actor_id = tagged_ghost.actor_id
        try:
            actor_doc = await fetch_actor(request, remote_actor_id)
        except RemoteActorFetchError as exc:
            await _notice(request, room_id, f"Could not fetch {remote_actor_id}: {exc}")
            return
        handle = tagged_ghost.handle
    elif handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            remote_actor_id, handle, local_record = mxid_match
            if local_record is not None:
                await _follow_local_actor(request, sender=sender, room_id=room_id, target=local_record)
                return
            try:
                actor_doc = await fetch_actor(request, remote_actor_id)
            except RemoteActorFetchError as exc:
                await _notice(request, room_id, f"Could not fetch {remote_actor_id}: {exc}")
                return
        else:
            try:
                remote_actor_id = await resolve_remote_actor_id(http_client, handle)
            except WebfingerNotFoundError:
                await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
                return
            except WebfingerUnreachableError:
                await _notice(
                    request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit."
                )
                return
            except WebfingerError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
            local_username = username_from_actor_url(config.bridge.public_base_url, remote_actor_id)
            if local_username is not None:
                target = await repository.get_local_actor(local_username)
                if target is None:
                    await _notice(request, room_id, f"{handle} doesn't look like an active profile on this bridge.")
                    return
                await _follow_local_actor(request, sender=sender, room_id=room_id, target=target)
                return
            try:
                actor_doc = await fetch_actor(request, remote_actor_id)
            except RemoteActorFetchError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
    else:
        remote_room_here = await repository.get_remote_actor_room_by_room_id(room_id)
        if remote_room_here is None:
            await _notice(
                request, room_id,
                f"Usage: {_COMMAND_PREFIX}follow @user@instance.org (or run this with no argument inside "
                "that account's own room)",
            )
            return
        remote_actor_id = remote_room_here.actor_id
        try:
            actor_doc = await fetch_actor(request, remote_actor_id)
        except RemoteActorFetchError as exc:
            await _notice(request, room_id, f"Could not fetch {remote_actor_id}: {exc}")
            return
        handle = remote_room_here.display_name or remote_actor_id

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        sender_server = sender.split(":", 1)[1] if ":" in sender else ""
        is_third_party_sender = sender_server != config.synapse.server_name
        # Reaching here at all already means an allowed third party (the
        # dispatch gate in maybe_handle_command rejected anyone else before
        # ever calling this handler) -- Follow Only mints their identity
        # right here, on first follow, instead of requiring the self-service
        # ";create profile"/";link profile" flow local users (and Full-mode
        # third parties) go through. Full mode with no room_id yet falls
        # through to the same "link a profile first" rejection a local user
        # with no profile gets, unchanged.
        if is_third_party_sender and config.bridge.third_party_access_mode != "full":
            actor_record = await _auto_provision_third_party_actor(request, matrix_user_id=sender)
        else:
            await _notice(
                request, room_id,
                f'You need a linked profile before following anyone -- run "{_COMMAND_PREFIX}link profile" '
                "in your own room first. (Without one, a reply you send to a followed account "
                "would come from an identity the remote server has never seen.)",
            )
            return

    if await repository.is_following(actor_record.username, remote_actor_id):
        await _notice(request, room_id, f"You're already following {handle}.")
        return

    followed_room_id, follow_error = await _establish_remote_follow(
        request, sender=sender, actor_record=actor_record, remote_actor_id=remote_actor_id,
        actor_doc=actor_doc, handle=handle,
    )
    if follow_error is not None:
        if followed_room_id is not None:
            await _notice(
                request, room_id,
                f"Joined the room, but delivering the Follow to {handle} failed "
                "(it is not retried automatically yet).",
            )
        else:
            await _notice(request, room_id, f"Could not follow {handle}: {follow_error}.")
        return
    await _notice(
        request, room_id, f"Following {handle}. Posts will appear in {followed_room_id}.",
        html_message=f"Following {html.escape(handle)}. Posts will appear in {room_pill_html(followed_room_id)}.",
    )




async def _establish_remote_follow(
    request: Request, *, sender: str, actor_record: ActorRecord, remote_actor_id: str, actor_doc: dict, handle: str
) -> tuple[str | None, str | None]:
    """The shared core of following a REMOTE account as ``actor_record``:
    ghost + Remote User Room provisioning (or reusing + re-inviting into an
    existing one), then the signed ``Follow`` delivery -- exactly the flow
    ``;follow`` has always run, extracted verbatim so the follows-list
    import (``_handle_import_follows``) can run it once per account without
    ``;follow``'s own conversational notices.

    Returns ``(room_id, None)`` on success, ``(room_id, reason)`` when the
    room exists but the Follow delivery failed, or ``(None, reason)`` when
    nothing could be established at all. Assumes the caller has already
    checked ``is_following`` (both callers do).
    """
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    http_client = request.app.state.http_client
    synapse = request.app.state.synapse
    repository = request.app.state.repository

    inbox = actor_doc.get("inbox")
    if not inbox:
        return None, "their account document has no inbox"

    username = actor_doc.get("preferredUsername") or remote_actor_id.rstrip("/").rsplit("/", 1)[-1]
    domain = urlsplit(remote_actor_id).hostname or handle.lstrip("@").split("@", 1)[-1]
    localpart = ghost_localpart(config.appservice.user_prefix, username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, username, domain, config.synapse.server_name)

    remote_room = await repository.get_remote_actor_room(remote_actor_id)
    if remote_room is None:
        display_name = actor_doc.get("name") or username
        icon_url = extract_icon_url(actor_doc)
        # Uploaded once and reused for both the ghost's own Matrix avatar and
        # the room's avatar below, rather than uploading the same image
        # twice -- this only runs once, at room creation, so there's no
        # earlier sync to compare against; still records it into the same
        # cache _resolve_and_invite_ghost checks, so a later reply/reaction
        # from this same actor doesn't think this still needs (re-)applying.
        avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url) if icon_url else None
        banner_url = extract_banner_url(actor_doc)
        banner_mxc = await fetch_and_upload_media(http_client, synapse, banner_url) if banner_url else None

        await ensure_ghost_user(
            synapse,
            server_name=config.synapse.server_name,
            localpart=localpart,
            display_name=display_name,
            avatar_mxc=avatar_mxc,
        )
        await repository.record_ghost_profile(
            GhostProfile(
                actor_id=remote_actor_id, display_name=display_name, icon_url=icon_url,
                mxid=mxid, handle=f"@{username}@{domain}",
            )
        )
        new_room_id = await synapse.create_room(
            as_user_id=mxid,
            name=display_name or f"{username}@{domain}",
            topic=actor_doc.get("summary") or f"Fediverse posts from {username}@{domain}",
            # The bot is invited into every Remote User Room too (not just
            # its own ghost), so bridge commands work from inside it without
            # needing the bot to already be a member for some other reason
            # -- and made admin there too (not just the ghost), so it can
            # always assist with re-inviting people later.
            invite=[sender, bot_mxid],
            avatar_mxc=avatar_mxc,
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            join_rule=_KNOCK_JOIN_RULE,
            # bot_mxid kept at the same level as the ghost creator,
            # regardless of room version -- see SynapseClient.create_room's
            # own additional_creators docstring for how it handles pre-v12
            # vs v12+ differently under the hood. events'
            # SOCIAL_PROFILE_USER_ID_STATE_TYPE override matches
            # _handle_create_profile's identical reasoning -- redundant
            # with mxid already being this room's creator, but set anyway
            # for a client checking the state event itself rather than
            # falling back to m.room.create.
            additional_creators=[bot_mxid],
            power_level_content_override={
                "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            },
        )
        remote_room = RemoteActorRoom(
            actor_id=remote_actor_id,
            room_id=new_room_id,
            ghost_user_id=mxid,
            inbox_url=inbox,
            display_name=display_name,
            icon_url=icon_url,
            banner_url=banner_url,
            # This branch only ever runs when nobody on the server has
            # followed this actor before (see get_remote_actor_room's None
            # check above) -- flags the room for a one-time auto-backfill
            # the moment the follower actually joins it (bridge.membership's
            # maybe_handle_join), same mechanism as ";backfill". Never set
            # True anywhere else a RemoteActorRoom gets (re-)registered
            # (room replace, mention-triggered import, ...), since those
            # aren't "the first ever follow" -- see mark_backfill_pending_done.
            pending_backfill=True,
        )
        await repository.register_remote_actor_room(remote_room)
        await _send_bridge_info(
            request, room_id=new_room_id, actor_id=remote_actor_id,
            display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
        )
        await add_bridge_widget(request, room_id=new_room_id)
        await _set_ghost_profile_room_id(request, mxid=mxid, room_id=new_room_id)
        await set_ghost_external_handle(request, mxid=mxid, handle=handle, profile_url=extract_actor_url(actor_doc))
        await _set_profile_user_id(request, room_id=new_room_id, matrix_user_id=mxid, as_user_id=mxid)
        if banner_mxc:
            await _set_ghost_room_banner(request, room_id=new_room_id, ghost_user_id=mxid, banner_mxc=banner_mxc)
    else:
        try:
            await synapse.invite_user(remote_room.room_id, sender, as_user_id=remote_room.ghost_user_id)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", sender, remote_room.room_id, exc)

    followed = await _ensure_following(
        request, actor_record=actor_record, remote_actor_id=remote_actor_id, remote_room=remote_room
    )
    if not followed:
        return remote_room.room_id, "delivering the Follow to their server failed"
    return remote_room.room_id, None


async def _follow_local_actor(
    request: Request, *, sender: str, room_id: str, target: ActorRecord, quiet: bool = False
) -> str | None:
    """Follow another LOCAL bridge user -- entirely in-process, never over
    HTTP: both identities are already fully known here, so there's no
    reason to round-trip a signed Follow/Accept out to our own server and
    back. Invites the sender straight into ``target``'s own, already-
    existing Profile Room -- never a ghost, never a fabricated Remote User
    Room for someone who already has a real Matrix account and room (see
    ``resolve_and_invite_ghost``'s docstring). Records the relationship
    both ways (following/follower) and notifies ``target`` via DM, the same
    as a genuinely remote follow would via ``bridge.inbox_dispatch``'s
    ``_announce_new_follower``."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    repository = request.app.state.repository
    base = config.bridge.public_base_url

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        if not quiet:
            await _notice(
                request, room_id,
                f'You need a linked profile before following anyone -- run "{_COMMAND_PREFIX}link profile" '
                "in your own room first.",
            )
        return "you have no linked profile"

    handle = f"@{target.username}@{config.bridge.domain}"
    if actor_record.username == target.username:
        if not quiet:
            await _notice(request, room_id, "You can't follow yourself.")
        return "you can't follow yourself"

    target_actor_id = actor_url(base, target.username)
    if await repository.is_following(actor_record.username, target_actor_id):
        if not quiet:
            await _notice(request, room_id, f"You're already following {handle}.")
        return None  # already following -- success as far as any caller cares

    if not target.room_id:
        if not quiet:
            await _notice(request, room_id, f"{handle} doesn't currently have a linked room to invite you into.")
        return "they don't currently have a linked room"

    try:
        await request.app.state.synapse.invite_user(target.room_id, sender, as_user_id=bot_mxid)
    except SynapseError as exc:
        if exc.errcode != "M_FORBIDDEN":
            logger.warning("Could not invite %s to %s: %s", sender, target.room_id, exc)

    await repository.add_following(actor_record.username, target_actor_id)
    await repository.add_follower(target.username, actor_url(base, actor_record.username))

    # No m.mentions/tagged mxid here -- see bridge.inbox_dispatch's
    # _notify_post_owner for why every notification in this room is left
    # unintentional, so the room's own notification setting (not a forced
    # per-message mention) decides whether it actually pings anyone.
    follower_handle = f"@{actor_record.username}@{config.bridge.domain}"
    actor_html = notification_actor_html(mxid=sender, handle=follower_handle)
    await notify_user(
        request,
        matrix_user_id=target.matrix_user_id,
        content={
            "msgtype": "m.text",
            "body": f"\U0001F464 {follower_handle} is now following you.",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>\U0001F464 {actor_html} is now following you.</p>",
        },
    )
    if not quiet:
        await _notice(
            request, room_id, f"Following {handle}. You've been invited to {target.room_id}.",
            html_message=f"Following {html.escape(handle)}. You've been invited to {room_pill_html(target.room_id)}.",
        )
    return None


async def _ensure_following(
    request: Request, *, actor_record: ActorRecord, remote_actor_id: str, remote_room: RemoteActorRoom
) -> bool:
    """Deliver a signed ``Follow`` from ``actor_record`` to ``remote_actor_id``
    if not already following, recording it on success. Returns whether
    ``actor_record`` now (or already did) follow ``remote_actor_id``.

    The ``follow`` command's own helper -- deliberately NOT reused by
    ``rejoin`` or knock-acceptance (``bridge.membership.maybe_handle_knock``)
    even though both also invite someone into a Remote User Room: getting
    into a room should never have the side effect of following its account,
    only actually running ``follow`` should."""
    repository = request.app.state.repository
    if await repository.is_following(actor_record.username, remote_actor_id):
        return True

    config = request.app.state.config
    base = config.bridge.public_base_url
    follow_activity = Activity(
        id=f"{actor_url(base, actor_record.username)}/follows/{uuid.uuid4().hex}",
        type="Follow",
        actor=actor_url(base, actor_record.username),
        object=remote_actor_id,
    )
    try:
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=remote_room.inbox_url,
            activity=follow_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError as exc:
        logger.warning("Follow delivery failed for %s -> %s: %s", actor_record.username, remote_actor_id, exc)
        return False

    # Recorded as soon as delivery succeeds, not on receiving a formal Accept
    # back: plenty of real-world software (locked accounts pending manual
    # approval aside) never sends one for an open account and just starts
    # delivering posts directly, which we'd otherwise silently drop forever
    # waiting for an Accept that isn't coming. `_handle_accept` still exists
    # and calling this again there is a harmless no-op if one does arrive.
    await repository.add_following(actor_record.username, remote_actor_id)
    return True


async def _handle_joinguild(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Join a Shoot guild (an ``Organization`` actor) using an invite code,
    per FEP-bebd. ``argument`` is ``CODE@guild.example.com`` -- the bare
    form, not the ``invite:CODE@domain`` WebFinger resource string itself,
    which this prepends automatically (see ``resolve_invite_code``).

    Unlike ``;follow``, this can't resolve synchronously: the guild's
    ``Accept``/``Reject`` arrives later over the inbox (see
    ``bridge.inbox_dispatch``'s ``_handle_accept``/``_handle_reject``), so
    this only ever sends the join request and records it as pending --
    the notice deliberately doesn't say "Joined", since the invite code
    might turn out to be invalid/expired.
    """
    config = request.app.state.config
    repository = request.app.state.repository
    base = config.bridge.public_base_url

    argument = argument.strip()
    if "@" not in argument:
        await _notice(
            request, room_id, f"Usage: {_COMMAND_PREFIX}joinguild CODE@guild.example.com",
        )
        return
    code, domain = argument.split("@", 1)
    code = code.removeprefix("invite:")

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before joining a guild -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    try:
        invite_code_doc, qualified_mention = await resolve_invite_code(request, code, domain)
    except WebfingerNotFoundError:
        await _notice(request, room_id, f"That invite code wasn't found on {domain} -- check it and try again.")
        return
    except WebfingerUnreachableError:
        await _notice(request, room_id, f"Couldn't reach {domain} right now -- try again in a bit.")
        return
    except WebfingerError as exc:
        await _notice(request, room_id, f"Could not resolve that invite code: {exc}")
        return

    guild_actor_id = invite_code_doc.get("attributedTo")
    # Confirmed live 2026-07-14: Shoot's own InviteCode embeds the full
    # guild actor object here rather than a bare IRI -- same
    # embedded-object-or-bare-IRI ambiguity Activity.from_dict already
    # normalizes for `actor`, just missed here on the first pass.
    if isinstance(guild_actor_id, dict):
        guild_actor_id = guild_actor_id.get("id")
    if not guild_actor_id:
        await _notice(request, room_id, "That invite code's own object is missing required fields.")
        return

    if await repository.is_guild_member(guild_actor_id):
        await _notice(request, room_id, "This bridge has already joined that guild.")
        return

    try:
        guild_doc = await fetch_actor(request, guild_actor_id)
    except RemoteActorFetchError as exc:
        await _notice(request, room_id, f"Could not fetch the guild's own actor document: {exc}")
        return
    guild_inbox = guild_doc.get("inbox")
    if not guild_inbox:
        await _notice(request, room_id, "The guild's own actor document has no inbox.")
        return

    follow_id = f"{actor_url(base, actor_record.username)}/follows/{uuid.uuid4().hex}"
    follow_activity = Activity(
        id=follow_id,
        type="Follow",
        actor=actor_url(base, actor_record.username),
        object=guild_actor_id,
        instrument=qualified_mention,
    )

    # Recorded BEFORE delivery, not after -- confirmed live 2026-07-14:
    # Shoot's own FollowActivityHandler processes the join and sends its
    # Accept back to us SYNCHRONOUSLY, inside the same request our
    # deliver_activity POST is still waiting on -- their Accept can reach
    # our inbox and finish processing before our own delivery call even
    # returns. Recording the pending row only after delivery "succeeded"
    # lost that race every time: _handle_accept found no row yet, fell
    # through to the generic (no-op-for-guilds) path, and by the time this
    # function got around to recording it, the Accept had already come and
    # gone. Rolled back below if delivery turns out to have failed after all.
    await repository.record_pending_guild_follow(
        PendingGuildFollow(
            follow_id=follow_id, guild_actor_id=guild_actor_id, username=actor_record.username,
            matrix_user_id=sender, invite_code=code, created_at=time.time(),
        )
    )
    try:
        await deliver_activity(
            request.app.state.http_client,
            inbox_url=guild_inbox,
            activity=follow_activity.to_dict(),
            key_id=main_key_id(base, actor_record.username),
            private_key_pem=actor_record.private_key_pem,
        )
    except DeliveryError as exc:
        await repository.remove_pending_guild_follow(follow_id)
        await _notice(request, room_id, f"Could not send the join request: {exc}")
        return

    guild_name = guild_doc.get("name") or guild_doc.get("preferredUsername") or domain
    await _notice(
        request, room_id, f"Join request sent to {guild_name} -- you'll be notified once it's confirmed.",
    )


async def _handle_leaveguild(request: Request, *, sender: str, room_id: str) -> None:
    """``;leaveguild``, run inside one of that guild's own Channel rooms --
    sends a real ``Undo(Follow)`` (the spec-correct way to leave, so a
    server that DOES handle it properly stops treating us as a member) and
    drops this bridge's own local membership tracking.

    Only the local side actually takes effect right now: confirmed live
    2026-07-14 by reading Shoot's own ``Undo`` handler
    (``src/util/activitypub/inbox/handlers/undo.ts``) -- it validates the
    undone object is Follow-shaped and then does nothing else at all
    (a literal ``// TODO: undo the follow`` with no implementation below
    it), so Shoot itself never actually removes the membership, its cached
    (possibly avatar-less -- see ``bridge.channel_bridge``'s own docstring
    on why a stale cache can get permanently stuck) ``User``/``Member``
    row, or anything else. This command still sends the Undo (it's free,
    and correct behavior for any OTHER implementation that DOES honor it)
    and still drops OUR OWN tracking regardless -- ``;joinguild`` on a
    NEW guild is unaffected by any of this either way, since it's keyed
    entirely by that guild's own distinct ``guild_actor_id``."""
    repository = request.app.state.repository
    channel_room = await repository.get_channel_room_by_room_id(room_id)
    if channel_room is None:
        await _notice(
            request, room_id, f'"{_COMMAND_PREFIX}leaveguild" only works inside one of that guild\'s own Channel rooms.',
        )
        return

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before leaving a guild -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    config = request.app.state.config
    base = config.bridge.public_base_url
    guild_actor_id = channel_room.guild_actor_id
    own_actor_id = actor_url(base, actor_record.username)

    try:
        guild_doc = await fetch_actor(request, guild_actor_id)
    except RemoteActorFetchError as exc:
        await _notice(request, room_id, f"Could not fetch the guild's own actor document: {exc}")
        return
    guild_inbox = guild_doc.get("inbox")
    if guild_inbox:
        undo_activity = Activity(
            id=f"{own_actor_id}/undos/{uuid.uuid4().hex}",
            type="Undo",
            actor=own_actor_id,
            object={"type": "Follow", "actor": own_actor_id, "object": guild_actor_id},
        )
        try:
            await deliver_activity(
                request.app.state.http_client,
                inbox_url=guild_inbox,
                activity=undo_activity.to_dict(),
                key_id=main_key_id(base, actor_record.username),
                private_key_pem=actor_record.private_key_pem,
            )
        except DeliveryError as exc:
            logger.warning("Failed to deliver guild-leave Undo(Follow) to %s: %s", guild_inbox, exc)

    await repository.remove_guild_membership(actor_record.username, guild_actor_id)
    guild_name = guild_doc.get("name") or guild_doc.get("preferredUsername") or guild_actor_id
    await _notice(
        request, room_id,
        f"Left {guild_name} on this bridge's own side. Its Space and Channel rooms are untouched -- "
        f'leave them yourself in Matrix if you don\'t want them anymore. Note: Shoot itself doesn\'t '
        f"actually process guild-leave yet, so it will still list you as a member there regardless.",
    )


async def _handle_unfollow(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Kicks the sender from the Remote User Room representing ``handle`` (or
    the current room, if run from inside it without an argument). The actual
    unfollow -- a signed ``Undo(Follow)``, and dropping the following
    relationship -- happens in ``bridge.membership`` when it sees the
    resulting membership-leave event, the same as if the sender had simply
    left the room themselves; this command is just a kick that triggers it,
    to avoid duplicating that logic here. A tagged ghost pill (``content``'s
    ``m.mentions``) works the same as typing out the handle -- see
    ``_resolve_tagged_ghost``.

    If the target is another LOCAL bridge user instead -- tagged directly,
    or named by their own ``@user@ourdomain`` handle -- hands off to
    ``_unfollow_local_actor``: there's no Remote User Room to kick anyone
    from (a local user's Profile Room is a real room they own, with
    ordinary Matrix membership the bridge has no business forcibly
    changing), just the following/follower bookkeeping to undo."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    tagged_local = None if tagged_ghost is not None else await _resolve_tagged_local_actor(request, content)
    if tagged_local is not None:
        await _unfollow_local_actor(request, sender=sender, room_id=room_id, target=tagged_local)
        return

    if tagged_ghost is not None:
        remote_room = await repository.get_remote_actor_room(tagged_ghost.actor_id)
    elif handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            remote_actor_id, handle, local_record = mxid_match
            if local_record is not None:
                await _unfollow_local_actor(request, sender=sender, room_id=room_id, target=local_record)
                return
            remote_room = await repository.get_remote_actor_room(remote_actor_id)
        else:
            try:
                remote_actor_id = await resolve_remote_actor_id(request.app.state.http_client, handle)
            except WebfingerNotFoundError:
                await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
                return
            except WebfingerUnreachableError:
                await _notice(
                    request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit."
                )
                return
            except WebfingerError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
            local_username = username_from_actor_url(config.bridge.public_base_url, remote_actor_id)
            if local_username is not None:
                target = await repository.get_local_actor(local_username)
                if target is None:
                    await _notice(request, room_id, f"{handle} doesn't look like an active profile on this bridge.")
                    return
                await _unfollow_local_actor(request, sender=sender, room_id=room_id, target=target)
                return
            remote_room = await repository.get_remote_actor_room(remote_actor_id)
    else:
        remote_room = await repository.get_remote_actor_room_by_room_id(room_id)

    if remote_room is None:
        await _notice(
            request, room_id,
            f'Usage: "{_COMMAND_PREFIX}unfollow @user@instance.org", or run "{_COMMAND_PREFIX}unfollow" '
            "from inside the room representing that account.",
        )
        return

    try:
        await request.app.state.synapse.kick_user(
            remote_room.room_id, sender, as_user_id=bot_mxid, reason="Unfollowed via bridge command"
        )
    except SynapseError as exc:
        logger.warning("Could not kick %s from %s: %s", sender, remote_room.room_id, exc)
        await _notice(request, room_id, "Could not remove you from that room -- are you actually a member of it?")
        return

    if room_id != remote_room.room_id:
        await _notice(
            request, room_id, f"Unfollowed and removed you from {remote_room.room_id}.",
            html_message=f"Unfollowed and removed you from {room_pill_html(remote_room.room_id)}.",
        )

    # The kick above is what actually drives the unfollow (see this
    # function's own docstring -- bridge.membership sees the resulting
    # leave and does the real bookkeeping/Undo(Follow) from there), so this
    # is safe to send right away rather than waiting on that to happen.
    remote_domain = urlsplit(remote_room.actor_id).hostname or ""
    remote_username = remote_room.actor_id.rstrip("/").rsplit("/", 1)[-1]
    remote_handle = remote_room.display_name or f"@{remote_username}@{remote_domain}"
    unfollowed_pill = notification_actor_html(
        mxid=remote_room.ghost_user_id, handle=remote_handle, display_name=remote_room.display_name,
    )
    await notify_user(
        request,
        matrix_user_id=sender,
        content={
            "msgtype": "m.text",
            "body": f"Successfully unfollowed {remote_handle}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>Successfully unfollowed {unfollowed_pill}</p>",
        },
    )


async def _unfollow_local_actor(request: Request, *, sender: str, room_id: str, target: ActorRecord) -> None:
    """Unfollow another LOCAL bridge user -- pure following/follower
    bookkeeping, entirely in-process (the follow itself never touched HTTP
    either, see ``_follow_local_actor``, so there's no ``Undo(Follow)`` to
    send). Deliberately does NOT kick the sender from ``target``'s Profile
    Room: unlike a Remote User Room (which exists solely to mirror one
    account and which leaving IS the unfollow signal), a local user's
    Profile Room is a real room they own that may have other members for
    reasons having nothing to do with this bridge -- removing someone from
    it as a side effect of an unrelated command would be well outside what
    this bridge has any business doing."""
    repository = request.app.state.repository
    config = request.app.state.config
    base = config.bridge.public_base_url

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    handle = f"@{target.username}@{config.bridge.domain}"
    if actor_record is None:
        await _notice(request, room_id, f"You're not following {handle}.")
        return

    target_actor_id = actor_url(base, target.username)
    if not await repository.is_following(actor_record.username, target_actor_id):
        await _notice(request, room_id, f"You're not following {handle}.")
        return

    await repository.remove_following(actor_record.username, target_actor_id)
    await repository.remove_follower(target.username, actor_url(base, actor_record.username))
    await _notice(
        request, room_id,
        f"Unfollowed {handle}. You're still in {target.room_id} if you were invited there -- "
        "leave it yourself if you'd like to.",
        html_message=(
            f"Unfollowed {html.escape(handle)}. You're still in {room_pill_html(target.room_id)} "
            "if you were invited there -- leave it yourself if you'd like to."
        ),
    )

    unfollowed_pill = notification_actor_html(mxid=target.matrix_user_id, handle=handle)
    await notify_user(
        request,
        matrix_user_id=sender,
        content={
            "msgtype": "m.text",
            "body": f"Successfully unfollowed {handle}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<p>Successfully unfollowed {unfollowed_pill}</p>",
        },
    )


async def _handle_list_following(request: Request, *, sender: str, room_id: str) -> None:
    """List every fediverse account the sender's own linked actor follows,
    each with a matrix.to link to the Remote User Room mirroring it (if any
    -- following and having a room for someone are tracked separately, so
    it's a best-effort lookup, not guaranteed)."""
    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(request, room_id, "You don't have a linked profile, so you're not following anyone.")
        return

    following = await repository.list_following(actor_record.username)
    if not following:
        await _notice(request, room_id, "You're not following anyone yet.")
        return

    lines = []
    html_lines = []
    for actor_id in sorted(following):
        remote_room = await repository.get_remote_actor_room(actor_id)
        domain = urlsplit(actor_id).hostname or ""
        username = actor_id.rstrip("/").rsplit("/", 1)[-1]
        handle = f"@{username}@{domain}"
        label = f"{remote_room.display_name} ({handle})" if remote_room and remote_room.display_name else handle
        html_label = html.escape(label)
        if remote_room is not None:
            label += f" -- {matrix_to_room_link(remote_room.room_id)}"
            html_label += f" -- {room_pill_html(remote_room.room_id)}"
        lines.append(f"- {label}")
        html_lines.append(f"- {html_label}")

    await _notice(
        request, room_id, f"Following {len(following)} account(s):\n" + "\n".join(lines),
        html_message=(
            f"<p>Following {len(following)} account(s):</p><p>" + "<br>".join(html_lines) + "</p>"
        ),
    )


async def _handle_set_collection_visibility(
    request: Request, *, sender: str, room_id: str, hidden: bool, argument: str
) -> None:
    """``hide followers``/``show followers`` (or ``following``) -- toggles
    whether the sender's own public ActivityPub followers/following
    collection exposes its member list to remote viewers fetching
    ``/followers/{username}``/``/following/{username}`` (see
    ``bridge.activitypub.routes``). Only the member list is ever withheld --
    ``totalItems`` always reports the real count, same as Mastodon's own
    "hide network" setting, so a profile's follower/following COUNT stays
    public either way. Visible by default; only the sender's own linked
    actor can be toggled, and only from inside their own Profile Room (same
    ownership check as ``banner``)."""
    verb = "hide" if hidden else "show"
    collection = argument.strip().lower()
    if collection not in ("followers", "following"):
        await _notice(
            request, room_id, f'Usage: {_COMMAND_PREFIX}{verb} followers -- or "{_COMMAND_PREFIX}{verb} following".'
        )
        return

    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None or actor_record.room_id != room_id:
        await _notice(
            request, room_id,
            f'Run "{_COMMAND_PREFIX}{verb} {collection}" from inside your own linked Profile Room.',
        )
        return

    if collection == "followers":
        await repository.set_followers_hidden(actor_record.username, hidden)
    else:
        await repository.set_following_hidden(actor_record.username, hidden)

    visibility = "hidden from" if hidden else "visible to"
    await _notice(
        request, room_id,
        f"Your {collection} list is now {visibility} remote viewers "
        f"(your {collection} count is still public either way).",
    )


async def _handle_block(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Blocks the target account, as the sender's own linked actor.
    ``handle`` may be omitted if run from inside the room representing that
    account (its own Remote User Room, or -- for another LOCAL bridge user
    -- their Profile Room); see ``_resolve_target_actor``.

    Full policy (deliberately broader than ``;mute``, which this also
    subsumes -- see ``bridge.note_mirroring.is_silenced``, checked
    everywhere ``;mute`` is):

    - Cuts any existing follow relationship in either direction immediately
      -- a real signed ``Undo(Follow)`` (``unfollow_remote_actor``) if the
      sender was following them, since their side has no other way to know;
      just dropped locally if they were following the sender, since a
      follower relationship was never announced to begin with.
    - Kicks the sender from that account's Remote User Room (never a local
      target's own Profile Room -- a real room they own that may have other
      members for reasons having nothing to do with this bridge, same
      restriction ``_unfollow_local_actor`` already applies), and from any
      DM/Chat room open between them.
    - Declines (``Reject``, not silence) any future ``Follow`` from them --
      see ``bridge.inbox_dispatch._handle_follow``.
    - Suppresses notifications and DM/Chat/mention auto-invites the same
      way ``;mute`` does.

    Deliberately does NOT stop their posts from being mirrored at all --
    that mirroring is shared infrastructure (the same Remote User Room, if
    anyone else on this homeserver still follows them, or a repost by
    someone the sender follows), not something scoped to one blocker."""
    repository = request.app.state.repository
    config = request.app.state.config
    base = config.bridge.public_base_url
    bot_mxid = _bot_mxid(config)
    synapse = request.app.state.synapse

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before blocking anyone -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    resolved = await _resolve_target_actor(request, room_id=room_id, handle=handle, content=content, verb="block")
    if resolved is None:
        return
    target_actor_id, target_handle, target_local = resolved

    own_actor_id = actor_url(base, actor_record.username)
    if target_actor_id == own_actor_id:
        await _notice(request, room_id, "You can't block yourself.")
        return

    if await repository.is_blocked(actor_record.username, target_actor_id):
        await _notice(request, room_id, f"{target_handle} is already blocked.")
        return

    await repository.add_blocked(actor_record.username, target_actor_id)
    await repository.remove_follower(actor_record.username, target_actor_id)

    if await repository.is_following(actor_record.username, target_actor_id):
        if target_local is not None:
            await repository.remove_following(actor_record.username, target_actor_id)
        else:
            remote_room = await repository.get_remote_actor_room(target_actor_id)
            if remote_room is not None:
                await unfollow_remote_actor(request, actor_record, remote_room)
            else:
                await repository.remove_following(actor_record.username, target_actor_id)

    # Room kicks -- best-effort throughout, same reasoning as everywhere
    # else this bridge kicks someone from a room it manages: a failure here
    # shouldn't stop the block itself, which has already taken effect above.
    if target_local is None:
        remote_room = await repository.get_remote_actor_room(target_actor_id)
        if remote_room is not None:
            try:
                await synapse.kick_user(
                    remote_room.room_id, sender, as_user_id=bot_mxid, reason="Blocked via bridge command"
                )
            except SynapseError as exc:
                logger.info("Could not kick %s from %s: %s", sender, remote_room.room_id, exc)

        dm_room_id = await repository.get_ghost_dm_room(target_actor_id, sender)
        if dm_room_id is not None:
            try:
                await synapse.kick_user(dm_room_id, sender, as_user_id=bot_mxid, reason="Blocked via bridge command")
            except SynapseError as exc:
                logger.info("Could not kick %s from %s: %s", sender, dm_room_id, exc)

        chat_room_id = await repository.get_ghost_chat_room(target_actor_id, sender)
        if chat_room_id is not None:
            try:
                await synapse.kick_user(
                    chat_room_id, sender, as_user_id=bot_mxid, reason="Blocked via bridge command"
                )
            except SynapseError as exc:
                logger.info("Could not kick %s from %s: %s", sender, chat_room_id, exc)

    await _notice(request, room_id, f"Blocked {target_handle}.")


async def _handle_unblock(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Undoes ``;block`` -- only the block itself: doesn't restore the
    follow relationship or re-invite the sender into any room they were
    kicked from, both of which are things the sender can just redo
    themselves (``;follow``, ``;dm``, ``;chat``) now that they're able to."""
    repository = request.app.state.repository

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(request, room_id, f'You need a linked profile first -- run "{_COMMAND_PREFIX}link profile".')
        return

    resolved = await _resolve_target_actor(request, room_id=room_id, handle=handle, content=content, verb="unblock")
    if resolved is None:
        return
    target_actor_id, target_handle, _target_local = resolved

    if not await repository.is_blocked(actor_record.username, target_actor_id):
        await _notice(request, room_id, f"{target_handle} isn't blocked.")
        return

    await repository.remove_blocked(actor_record.username, target_actor_id)
    await _notice(request, room_id, f"Unblocked {target_handle}.")


async def _handle_mute(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Mutes the target account, as the sender's own linked actor --
    ``handle`` may be omitted the same way ``;block`` allows (see
    ``_resolve_target_actor``). Unlike ``;block``, doesn't touch any
    existing follow relationship, room membership, or their ability to
    follow the sender -- their posts/replies/reactions keep mirroring and
    they can still follow normally. Only suppresses (see
    ``bridge.note_mirroring.is_silenced``, checked everywhere this
    matters): notifications about them into the sender's Fediverse
    Notifications DM, and auto-inviting the sender into a room because of
    them (a fresh DM/Chat room, or being pulled into someone else's room
    over a mention) -- explicitly running ``;dm``/``;chat`` toward them is
    unaffected, since that's the sender's own deliberate choice, not an
    inbound interaction they didn't choose."""
    repository = request.app.state.repository

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before muting anyone -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    resolved = await _resolve_target_actor(request, room_id=room_id, handle=handle, content=content, verb="mute")
    if resolved is None:
        return
    target_actor_id, target_handle, _target_local = resolved

    base = request.app.state.config.bridge.public_base_url
    if target_actor_id == actor_url(base, actor_record.username):
        await _notice(request, room_id, "You can't mute yourself.")
        return

    if await repository.is_muted(actor_record.username, target_actor_id):
        await _notice(request, room_id, f"{target_handle} is already muted.")
        return

    await repository.add_muted(actor_record.username, target_actor_id)
    await _notice(request, room_id, f"Muted {target_handle}.")


async def _handle_unmute(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Undoes ``;mute``."""
    repository = request.app.state.repository

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(request, room_id, f'You need a linked profile first -- run "{_COMMAND_PREFIX}link profile".')
        return

    resolved = await _resolve_target_actor(request, room_id=room_id, handle=handle, content=content, verb="unmute")
    if resolved is None:
        return
    target_actor_id, target_handle, _target_local = resolved

    if not await repository.is_muted(actor_record.username, target_actor_id):
        await _notice(request, room_id, f"{target_handle} isn't muted.")
        return

    await repository.remove_muted(actor_record.username, target_actor_id)
    await _notice(request, room_id, f"Unmuted {target_handle}.")


async def _handle_banner(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Set the sender's fediverse profile banner/header image (AS2's
    ``image``, distinct from ``icon`` -- the avatar) to the ``mxc://...``
    media given as the command's argument, run from inside their own linked
    Profile Room.

    Matrix has no STABLE ``m.room``-level state for a "banner" separate
    from the room's own avatar (``m.room.avatar``) yet, so there's nothing
    to reuse the way ``create profile``/``link profile`` reuse the Matrix
    account's own avatar for ``icon``. Recorded instead as
    ``PROFILE_BANNER_STATE_TYPE`` (MSC4221's own unstable-prefixed
    ``m.room.banner``) on the Profile Room -- see that constant's own
    comment. Every later run of this command overwrites both that state
    event and ``ActorRecord.banner_url``, and re-pushes a signed ``Update``
    (see ``push_profile_update``) so the change actually reaches followers
    immediately rather than waiting for some unrelated later change to a
    name/bio/avatar to carry it along.
    """
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    repository = request.app.state.repository

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None or actor_record.room_id != room_id:
        await _notice(
            request, room_id,
            f'Run "{_COMMAND_PREFIX}banner mxc://server/mediaid" from inside your own linked Profile Room.',
        )
        return

    mxc = argument.strip()
    if not mxc.startswith("mxc://"):
        await _notice(
            request, room_id,
            f'Usage: {_COMMAND_PREFIX}banner mxc://server/mediaid -- upload the image in this room first '
            "(or any room I can see) to get its mxc:// URL.",
        )
        return

    try:
        icon_url = media_url(config.bridge.public_base_url, mxc)
    except ValueError:
        await _notice(request, room_id, f"{mxc!r} doesn't look like a valid mxc:// URL.")
        return

    synapse = request.app.state.synapse
    try:
        await synapse.send_state_event(
            room_id, PROFILE_BANNER_STATE_TYPE, "", {"url": mxc}, as_user_id=bot_mxid
        )
    except SynapseError:
        logger.info("Could not set banner state event in %s", room_id, exc_info=True)

    await repository.mark_media_published(mxc)
    actor_record = dataclasses.replace(actor_record, banner_url=icon_url)
    await repository.register_local_actor(actor_record)
    await push_profile_update(request, actor_record)

    await _notice(request, room_id, "Banner updated and pushed to your followers.")


async def _is_matrix_admin(request: Request, mxid: str) -> bool:
    """Whether ``mxid`` counts as a bridge admin -- gates commands that
    manage a room representing someone *else's* identity (a Remote User
    Room mirroring a fediverse account nobody here necessarily owns), as
    opposed to a user's own linked Profile Room, which they can always
    manage themselves regardless of admin status.

    ``bridge.admins`` (an explicit MXID list) is checked first, no live
    query at all -- ADDITIVE only: being left off it never takes admin
    rights away from an actual Synapse server admin. Only if ``mxid``
    isn't listed there AND ``bridge.use_synapse_admin_api`` is on does
    this fall back to asking Synapse itself (the only way to learn that
    for an MXID the operator didn't already name) -- with that setting
    off, an unlisted MXID is simply not an admin, full stop; no API call
    is even attempted (see that setting's own docstring for why)."""
    config = request.app.state.config
    if mxid in config.bridge.admins:
        return True
    if not config.bridge.use_synapse_admin_api:
        return False
    try:
        return await request.app.state.synapse.admin_is_server_admin(mxid)
    except SynapseError:
        logger.warning("Could not check admin status for %s", mxid, exc_info=True)
        return False


async def _handle_dm(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Start (or reuse) a 1:1 Note-based direct-message room with ``handle``
    -- lets a local user proactively open a DM instead of only ever getting
    one by receiving a message or replying to something narrowed down to
    just one recipient. A tagged ghost pill (``content``'s ``m.mentions``)
    works the same as typing out the handle -- see ``_resolve_tagged_ghost``
    -- and ``handle`` may be omitted entirely if this is run from inside
    that account's own Remote User Room instead, same as ``;follow``.

    If a room for this exact (sender, handle) pair already exists, this
    reuses it -- re-inviting the sender if they've since left (see
    ``ensure_ghost_dm_room``) -- and just points them back at it, rather
    than ever creating a second one for the same conversation.

    This is the Note-based DM counterpart of ``_handle_chat`` below -- see
    ``ActorRepository.get_ghost_chat_room``'s docstring for why they're
    deliberately different rooms even for the exact same fediverse account,
    not two names for the same feature."""
    config = request.app.state.config
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    if tagged_ghost is not None:
        remote_actor_id = tagged_ghost.actor_id
    elif handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            remote_actor_id, handle, local_record = mxid_match
            if local_record is not None:
                await _notice(
                    request, room_id,
                    f"{handle} looks like a local user on this bridge -- just start an "
                    "ordinary Matrix DM with them directly, no bridge command needed.",
                )
                return
        else:
            try:
                remote_actor_id = await resolve_remote_actor_id(request.app.state.http_client, handle)
            except WebfingerNotFoundError:
                await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
                return
            except WebfingerUnreachableError:
                await _notice(
                    request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit."
                )
                return
            except WebfingerError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
    else:
        remote_room_here = await repository.get_remote_actor_room_by_room_id(room_id)
        if remote_room_here is None:
            await _notice(
                request, room_id,
                f"Usage: {_COMMAND_PREFIX}dm @user@instance.org (or run this with no argument inside "
                "that account's own room)",
            )
            return
        remote_actor_id = remote_room_here.actor_id

    if username_from_actor_url(config.bridge.public_base_url, remote_actor_id) is not None:
        await _notice(
            request, room_id,
            f"{handle or remote_actor_id} looks like a local user on this bridge -- just start an "
            "ordinary Matrix DM with them directly, no bridge command needed.",
        )
        return

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before starting a DM -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    # Belt-and-suspenders alongside maybe_handle_command's own gate (which
    # already keeps a Follow-Only third party from ever reaching this
    # handler at all) -- see is_third_party_still_allowed's docstring.
    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return

    provisioned = await provision_ghost(request, remote_actor_id)
    if provisioned is None:
        await _notice(request, room_id, f"Could not resolve {handle or remote_actor_id}.")
        return
    mxid, _actor_doc, display_name, avatar_mxc = provisioned

    already_existed = await repository.get_ghost_dm_room(remote_actor_id, sender) is not None
    dm_room_id = await ensure_ghost_dm_room(
        request, actor_id=remote_actor_id, matrix_user_id=sender,
        display_name=display_name, avatar_mxc=avatar_mxc, mxid=mxid, respect_silence=False,
    )
    if dm_room_id is None:
        await _notice(request, room_id, f"Could not create a DM room with {display_name}.")
        return

    link = matrix_to_room_link(dm_room_id)
    pill = room_pill_html(dm_room_id, display_name)
    if already_existed:
        await _notice(
            request, room_id, f"You already have a DM room with {display_name}: {link} -- send your message there.",
            html_message=f"You already have a DM room with {html.escape(display_name)}: {pill} -- send your message there.",
        )
    else:
        await _notice(
            request, room_id,
            f"Started a DM with {display_name}: {link} -- you've been invited, send your message there.",
            html_message=f"Started a DM with {html.escape(display_name)}: {pill} -- you've been invited, send your message there.",
        )


async def _handle_chat(request: Request, *, sender: str, room_id: str, handle: str, content: dict) -> None:
    """Start (or reuse) a 1:1 ActivityPub ``ChatMessage`` room with
    ``handle`` -- the ``ChatMessage`` counterpart of ``_handle_dm`` above,
    identical except for using the separate chat-room tracking/creation
    (``ensure_ghost_chat_room``/``ActorRepository.get_ghost_chat_room``)
    instead. The other way to start one is a plain Matrix-native DM invite
    sent directly to the ghost's own mxid -- see
    ``bridge.membership.maybe_accept_invite``. ``handle`` may be omitted
    entirely if this is run from inside that account's own Remote User Room
    instead, same as ``;follow``/``;dm``."""
    config = request.app.state.config
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    if tagged_ghost is not None:
        remote_actor_id = tagged_ghost.actor_id
    elif handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            remote_actor_id, handle, local_record = mxid_match
            if local_record is not None:
                await _notice(
                    request, room_id,
                    f"{handle} looks like a local user on this bridge -- just start an "
                    "ordinary Matrix DM with them directly, no bridge command needed.",
                )
                return
        else:
            try:
                remote_actor_id = await resolve_remote_actor_id(request.app.state.http_client, handle)
            except WebfingerNotFoundError:
                await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
                return
            except WebfingerUnreachableError:
                await _notice(
                    request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit."
                )
                return
            except WebfingerError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
    else:
        remote_room_here = await repository.get_remote_actor_room_by_room_id(room_id)
        if remote_room_here is None:
            await _notice(
                request, room_id,
                f"Usage: {_COMMAND_PREFIX}chat @user@instance.org (or run this with no argument inside "
                "that account's own room)",
            )
            return
        remote_actor_id = remote_room_here.actor_id

    if username_from_actor_url(config.bridge.public_base_url, remote_actor_id) is not None:
        await _notice(
            request, room_id,
            f"{handle or remote_actor_id} looks like a local user on this bridge -- just start an "
            "ordinary Matrix DM with them directly, no bridge command needed.",
        )
        return

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before starting a chat -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    # Belt-and-suspenders alongside maybe_handle_command's own gate (which
    # already keeps a Follow-Only third party from ever reaching this
    # handler at all) -- see is_third_party_still_allowed's docstring.
    if not await is_third_party_still_allowed(request, actor_record, room_id=room_id):
        return

    provisioned = await provision_ghost(request, remote_actor_id)
    if provisioned is None:
        await _notice(request, room_id, f"Could not resolve {handle or remote_actor_id}.")
        return
    mxid, actor_doc, display_name, avatar_mxc = provisioned

    if not actor_doc.get("capabilities", {}).get("acceptsChatMessages"):
        # Best-effort courtesy notice, not a hard block -- their profile not
        # advertising Chat support usually means their software doesn't
        # implement it (in which case the ChatMessage we'd send would
        # likely just be silently dropped or misread as something else),
        # but there's no harm in still creating the room in case it works
        # anyway (implementations differ in how faithfully they advertise
        # this).
        await _notice(
            request, room_id,
            f"Note: {display_name} doesn't appear to advertise Chat support -- this might not work, "
            "but creating the room anyway.",
        )

    already_existed = await repository.get_ghost_chat_room(remote_actor_id, sender) is not None
    chat_room_id = await ensure_ghost_chat_room(
        request, actor_id=remote_actor_id, matrix_user_id=sender,
        display_name=display_name, avatar_mxc=avatar_mxc, mxid=mxid, respect_silence=False,
    )
    if chat_room_id is None:
        await _notice(request, room_id, f"Could not create a chat room with {display_name}.")
        return

    link = matrix_to_room_link(chat_room_id)
    pill = room_pill_html(chat_room_id, display_name)
    if already_existed:
        await _notice(
            request, room_id, f"You already have a chat with {display_name}: {link} -- send your message there.",
            html_message=f"You already have a chat with {html.escape(display_name)}: {pill} -- send your message there.",
        )
    else:
        await _notice(
            request, room_id,
            f"Started a chat with {display_name}: {link} -- you've been invited, send your message there.",
            html_message=f"Started a chat with {html.escape(display_name)}: {pill} -- you've been invited, send your message there.",
        )


_PRETTY_POST_URL_RE = re.compile(r"^/@[^/]+/(?:posts/)?([A-Za-z0-9_-]+)/?$")


def _pretty_post_url_fallback(url: str) -> str | None:
    """If ``url`` looks like a human-facing "pretty" post URL -- Mastodon's
    ``/@user/ID``, or Pleroma/Akkoma/Soapbox's ``/@user/posts/ID`` -- return
    that same instance's Pleroma-style ``/notice/ID`` URL as a fallback, or
    None if ``url`` doesn't look like a post URL at all.

    Some instances' web frontend serves the human-facing HTML page for
    these "pretty" routes regardless of the ``Accept: application/activity+json``
    header sent (seen in the wild on poa.st), so a normal ``fetch_actor``
    call against them fails with "not valid JSON" even though the exact
    same post has a working AP representation one URL shape away.
    ``/notice/ID`` is a long-standing, stable convention across the whole
    Pleroma family (Pleroma/Akkoma/Rebased) -- it 302s to the post's real
    ``/objects/{uuid}`` AP document, which ``fetch_actor``'s ``http_client``
    (``follow_redirects=True``) follows transparently -- using the exact
    same trailing ID segment either URL shape already carries.
    """
    parsed = urlsplit(url)
    match = _PRETTY_POST_URL_RE.match(parsed.path)
    if not match:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, f"/notice/{match.group(1)}", "", ""))


def _parse_follows_export(text: str) -> list[str]:
    """``user@domain`` handles from a follows export, in file order,
    de-duplicated case-insensitively.

    Handles both shapes seen in the wild: Pleroma/Akkoma's (one bare handle
    per line, no header -- see the user-provided friends.csv this was built
    against) and Mastodon's (a CSV with an "Account address" header column,
    plus extra columns like "Show boosts"). A leading "@" and surrounding
    quotes are tolerated per entry; any line that doesn't look like a
    handle at all is skipped, and the CALLER treats an overall-empty result
    as "not a follows export" (better one clear warning than 141 per-line
    ones for a file that was never a follows list to begin with)."""
    handles: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(text.splitlines()):
        first_column = line.split(",")[0].strip().strip('"').strip().lstrip("@")
        if not first_column:
            continue
        if index == 0 and first_column.lower() == "account address":
            continue  # Mastodon's header row
        if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", first_column):
            continue
        key = first_column.lower()
        if key in seen:
            continue
        seen.add(key)
        handles.append(first_column)
    return handles


_IMPORT_FOLLOWS_USAGE = (
    "To import a follows list: export it from your other fediverse account "
    "(Pleroma/Akkoma: Settings > Data export; Mastodon: Preferences > Import and export > Follows), "
    "upload the CSV file to a room I'm in, then REPLY to that upload (a plain or thread reply both "
    f'work) with "{_COMMAND_PREFIX}import follows".'
)

# Strong references to in-flight follows imports -- a bare create_task
# result a caller drops can be garbage-collected mid-run.
_RUNNING_FOLLOWS_IMPORTS: set = set()


async def _handle_import_follows(request: Request, *, sender: str, room_id: str, content: dict) -> None:
    """Bulk-follow every account in an uploaded follows export (see
    ``_parse_follows_export``). The command must be a REPLY to the upload
    event -- that's how the file is named, per the user's design.

    Validation (linked profile, reply target, file, parseability) happens
    inline so the sender gets immediate feedback; the actual following runs
    as a BACKGROUND task. That split isn't cosmetic: each new account means
    WebFinger + an actor fetch + possibly ghost/room provisioning, so a
    real export (141 rows in the one this was built against) takes minutes
    -- far past Synapse's AppService transaction timeout. Blocking the
    transaction handler that long would make Synapse re-deliver the whole
    transaction and re-run every event in it mid-import."""
    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id,
            f'You need a linked profile before following anyone -- run "{_COMMAND_PREFIX}link profile" '
            "in your own room first.",
        )
        return

    target_event_id = _reply_target_event_id(content)
    if target_event_id is None:
        await _notice(
            request, room_id, f"This command must be sent as a reply to your uploaded export. {_IMPORT_FOLLOWS_USAGE}"
        )
        return

    bot_mxid = _bot_mxid(request.app.state.config)
    try:
        target_event = await request.app.state.synapse.get_event(room_id, target_event_id, as_user_id=bot_mxid)
    except SynapseError:
        await _notice(request, room_id, f"Couldn't fetch the message you replied to. {_IMPORT_FOLLOWS_USAGE}")
        return

    target_content = target_event.get("content") or {}
    mxc = target_content.get("url")
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        await _notice(
            request, room_id, f"The message you replied to doesn't have a file attached. {_IMPORT_FOLLOWS_USAGE}"
        )
        return

    server_name, _, media_id = mxc.removeprefix("mxc://").partition("/")
    try:
        download = await request.app.state.synapse.download_media(server_name, media_id)
    except SynapseError:
        await _notice(request, room_id, "Couldn't download that file from Matrix -- try re-uploading it.")
        return

    handles = _parse_follows_export(download.content.decode("utf-8", errors="replace"))
    if not handles:
        await _notice(
            request, room_id,
            "That file doesn't look like a follows export -- expected one @user@instance.org handle per "
            'line (Pleroma/Akkoma), or a CSV with an "Account address" column (Mastodon). '
            + _IMPORT_FOLLOWS_USAGE,
        )
        return

    await _notice(
        request, room_id,
        f"Importing {len(handles)} follows -- this can take a while (each new account gets its own "
        "room). I'll post a summary here when it's done.",
    )
    task = asyncio.get_running_loop().create_task(
        _run_follows_import(request, sender=sender, room_id=room_id, actor_record=actor_record, handles=handles)
    )
    _RUNNING_FOLLOWS_IMPORTS.add(task)
    task.add_done_callback(_RUNNING_FOLLOWS_IMPORTS.discard)


async def _run_follows_import(
    request: Request, *, sender: str, room_id: str, actor_record: ActorRecord, handles: list[str]
) -> None:
    """The background half of ``_handle_import_follows``: follow each
    handle, then post ONE summary -- successes and already-followed
    accounts (including the sender's own exported account, which every
    export of your old account naturally contains) are silently counted,
    only failures get itemized, each with its reason, per the user's spec."""
    config = request.app.state.config
    base = config.bridge.public_base_url
    repository = request.app.state.repository
    http_client = request.app.state.http_client

    followed = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    try:
        for handle in handles:
            try:
                remote_actor_id = await resolve_remote_actor_id(http_client, handle)
            except WebfingerNotFoundError:
                failures.append((handle, "no such account (WebFinger lookup found nothing)"))
                continue
            except WebfingerUnreachableError:
                failures.append((handle, "their server couldn't be reached"))
                continue
            except WebfingerError as exc:
                failures.append((handle, f"WebFinger lookup failed: {exc}"))
                continue

            local_username = username_from_actor_url(base, remote_actor_id)
            if local_username is not None:
                target = await repository.get_local_actor(local_username)
                if target is None:
                    failures.append((handle, "not an active profile on this bridge"))
                    continue
                if target.username == actor_record.username:
                    skipped += 1  # the export naturally contains the account it came from -- silently ignore
                    continue
                if await repository.is_following(actor_record.username, actor_url(base, target.username)):
                    skipped += 1
                    continue
                reason = await _follow_local_actor(request, sender=sender, room_id=room_id, target=target, quiet=True)
                if reason is None:
                    followed += 1
                else:
                    failures.append((handle, reason))
                continue

            if await repository.is_following(actor_record.username, remote_actor_id):
                skipped += 1
                continue
            try:
                actor_doc = await fetch_actor(request, remote_actor_id)
            except RemoteActorFetchError as exc:
                failures.append((handle, f"couldn't fetch their account: {exc}"))
                continue
            _followed_room, error = await _establish_remote_follow(
                request, sender=sender, actor_record=actor_record,
                remote_actor_id=remote_actor_id, actor_doc=actor_doc, handle=f"@{handle}",
            )
            if error is None:
                followed += 1
            else:
                failures.append((handle, error))
    except Exception:
        logger.exception("Follows import crashed for %s in %s", sender, room_id)
        await _notice(
            request, room_id,
            "The follows import hit an unexpected error and stopped early -- the summary below covers "
            "what completed.",
        )

    summary = (
        f"Follows import finished: {followed} followed, {skipped} already followed (skipped), "
        f"{len(failures)} failed."
    )
    if not failures:
        await _notice(request, room_id, summary)
        return
    plain = summary + "\n" + "\n".join(f"  - {h}: {r}" for h, r in failures)
    items = "".join(f"<li><code>{html.escape(h)}</code> -- {html.escape(r)}</li>" for h, r in failures)
    summary_content: dict = {
        "msgtype": "m.notice",
        "body": plain,
        "format": "org.matrix.custom.html",
        "formatted_body": f"<p>{html.escape(summary)}</p><ul>{items}</ul>",
    }
    relates_to = _command_relates_to_var.get()
    if relates_to:
        summary_content["m.relates_to"] = relates_to
    try:
        await request.app.state.synapse.send_message_event(room_id, summary_content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send follows-import summary to %s", room_id, exc_info=True)


_RUNNING_BACKFILLS: set = set()


async def _collect_recent_items(request: Request, collection: dict | str, *, limit: int) -> list[dict]:
    """Page through an ActivityPub collection (a remote actor's ``outbox``,
    or a Note's own ``replies``) and return up to ``limit`` raw items,
    newest first -- fewer if the collection doesn't have that many.
    Handles a collection with its items embedded directly (no paging), one
    whose ``first`` page is embedded inline, and the ordinary case of
    ``first``/``next`` as separate URLs to fetch. Tolerates both
    ``orderedItems`` (OrderedCollection, the outbox's own shape) and
    ``items`` (a plain Collection, the shape a Note's ``replies`` commonly
    uses instead). Bare IRI entries (rather than embedded objects) are
    individually fetched, since some implementations list a page of
    ``replies`` as plain Note IRIs instead of embedding the full objects."""
    http_client = request.app.state.http_client
    items: list[dict] = []

    async def _extend(raw: object) -> None:
        raw_items = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
        for entry in raw_items:
            if len(items) >= limit:
                return
            if isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, str):
                try:
                    items.append(await fetch_actor(request, entry))
                except RemoteActorFetchError:
                    continue

    if isinstance(collection, dict):
        root = collection
    else:
        try:
            root = await fetch_actor(request, collection)
        except RemoteActorFetchError:
            return items

    await _extend(root.get("orderedItems") if root.get("orderedItems") is not None else root.get("items"))

    page_ref = root.get("first")
    seen_urls: set[str] = set()
    while len(items) < limit and page_ref:
        if isinstance(page_ref, dict):
            page = page_ref
        else:
            if page_ref in seen_urls:
                break  # a misbehaving server looping "next" back on itself
            seen_urls.add(page_ref)
            try:
                page = await fetch_actor(request, page_ref)
            except RemoteActorFetchError:
                break
        await _extend(page.get("orderedItems") if page.get("orderedItems") is not None else page.get("items"))
        page_ref = page.get("next")

    return items[:limit]


def _note_from_item(item: dict, *, fallback_author: str | None) -> dict | None:
    """Normalize a raw outbox/replies collection item into a ``Create``
    activity JSON dict ready for ``Activity.from_dict``. Items show up as
    either a full ``Create`` activity (typical of an outbox) or a bare
    ``Note`` (typical of a ``replies`` collection) -- wraps the latter.
    Returns None for anything else (Announces, Questions, ...) -- backfill
    only ever mirrors the account's own authored posts, same scope as a
    live ``Create`` delivery."""
    if item.get("type") == "Create":
        obj = item.get("object")
        return item if isinstance(obj, dict) and obj.get("type") == "Note" else None
    if item.get("type") == "Note":
        actor = item.get("attributedTo") or fallback_author
        if not isinstance(actor, str):
            return None
        return {
            "id": f"{item.get('id', '')}#backfill",
            "type": "Create",
            "actor": actor,
            "object": item,
            "published": item.get("published"),
            "to": item.get("to") or [],
            "cc": item.get("cc") or [],
        }
    return None


async def _handle_backfill(request: Request, *, sender: str, room_id: str, argument: str, content: dict) -> None:
    """Pull a remote actor's latest posts into the Remote User Room this
    command was run in -- or, run as a reply inside a Matrix thread that
    mirrors an ActivityPub conversation, that specific thread's replies
    instead (resolved via the thread root's own tracked ``ap_object_id``
    and its live ``replies`` collection).

    Everyone gets ``config.bridge.backfill_default_count`` posts; only a
    Matrix server admin may override that with an explicit ``N``, since an
    arbitrarily large backfill means an unbounded number of outbound
    fetches (and, for media-bearing posts, media downloads) any local user
    could otherwise trigger against a remote server at will.

    Reuses ``bridge.inbox_dispatch._handle_create`` for the actual
    mirroring -- the exact same code path a live inbound ``Create`` runs
    through (dedup via ``federated_events``, reply-thread placement,
    mentions, quote-posts, attachments) -- so a backfilled post is
    indistinguishable from one that arrived live, and already-mirrored
    posts are skipped rather than duplicated.
    """
    config = request.app.state.config
    repository = request.app.state.repository

    remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
    if remote_room is None:
        await _notice(
            request, room_id,
            f"Run {_COMMAND_PREFIX}backfill inside the Remote User Room for the fediverse account "
            "you want to backfill.",
        )
        return

    argument = argument.strip()
    if argument:
        if not await _is_matrix_admin(request, sender):
            await _notice(request, room_id, "Only a Matrix server admin can choose a custom backfill count.")
            return
        try:
            count = int(argument)
        except ValueError:
            await _notice(request, room_id, f'"{argument}" isn\'t a number.')
            return
        if count <= 0:
            await _notice(request, room_id, "Give a positive number of posts to backfill.")
            return
    else:
        count = config.bridge.backfill_default_count

    thread_root_event_id = None
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") == "m.thread":
        thread_root_event_id = relates_to.get("event_id")

    await _notice(
        request, room_id,
        f"Backfilling up to {count} posts -- this can take a while. I'll post a summary here when it's done.",
    )
    task = asyncio.get_running_loop().create_task(
        _run_backfill(
            request, room_id=room_id, remote_room=remote_room, count=count,
            thread_root_event_id=thread_root_event_id,
        )
    )
    _RUNNING_BACKFILLS.add(task)
    task.add_done_callback(_RUNNING_BACKFILLS.discard)


class _BackfillSourceError(Exception):
    """Raised by ``_resolve_backfill_source`` for any reason backfilling
    can't even start -- carries the user-facing notice text."""

    def __init__(self, notice: str) -> None:
        super().__init__(notice)
        self.notice = notice


async def _resolve_backfill_source(
    request: Request, *, remote_room: RemoteActorRoom, count: int, thread_root_event_id: str | None,
) -> tuple[list[dict], str | None]:
    """Fetches and pages whichever collection this backfill draws from --
    the thread root's own ``replies`` (thread mode) or the actor's
    ``outbox`` (the ordinary case) -- and returns ``(raw_items,
    fallback_author)`` ready for ``_mirror_backfilled_notes``. Raises
    ``_BackfillSourceError`` with a ready-to-send notice for anything that
    stops backfilling before it can even start."""
    repository = request.app.state.repository
    http_client = request.app.state.http_client

    if thread_root_event_id is not None:
        root_event = await repository.get_federated_event_by_matrix_event(thread_root_event_id)
        if root_event is None:
            raise _BackfillSourceError(
                "That thread isn't one I'm tracking -- can't tell which fediverse post it maps to."
            )
        try:
            root_note = await fetch_actor(request, root_event.ap_object_id)
        except RemoteActorFetchError as exc:
            raise _BackfillSourceError(f"Couldn't fetch that thread's post: {exc}") from exc
        replies = root_note.get("replies")
        if not replies:
            raise _BackfillSourceError("That post doesn't expose a replies collection to backfill from.")
        raw_items = await _collect_recent_items(request, replies, limit=count)
        return raw_items, None

    try:
        actor_doc = await fetch_actor(request, remote_room.actor_id)
    except RemoteActorFetchError as exc:
        raise _BackfillSourceError(f"Couldn't fetch their account: {exc}") from exc
    outbox = actor_doc.get("outbox")
    if not outbox:
        raise _BackfillSourceError("Their account doesn't expose an outbox to backfill from.")
    raw_items = await _collect_recent_items(request, outbox, limit=count)
    return raw_items, remote_room.actor_id


async def _mirror_backfilled_notes(
    request: Request, *, raw_items: list[dict], fallback_author: str | None,
) -> tuple[int, int, int]:
    """Normalizes raw outbox/replies items into ``Create`` activities and
    mirrors each via ``_handle_create`` (``force=True`` -- see its
    docstring), oldest of the batch first. Returns ``(imported, already,
    failed)`` counts. Shared by ``;backfill`` (``_run_backfill``) and the
    one-time auto-backfill a brand-new follow's first join triggers
    (``_run_auto_backfill``)."""
    repository = request.app.state.repository
    notes = [n for n in (_note_from_item(item, fallback_author=fallback_author) for item in raw_items) if n]
    notes.reverse()  # oldest of the collected batch first, closer to true chronological order

    imported = 0
    already = 0
    failed = 0
    for activity_json in notes:
        note = activity_json.get("object")
        ap_object_id = note.get("id") if isinstance(note, dict) else None
        if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
            already += 1
            continue
        try:
            activity = Activity.from_dict(activity_json)
        except ValueError:
            failed += 1
            continue
        try:
            await _handle_create(request, "", activity, force=True)
        except Exception:
            logger.warning("Backfill failed to mirror %s", ap_object_id, exc_info=True)
            failed += 1
            continue
        if ap_object_id and await repository.get_federated_event_by_ap_object(ap_object_id) is not None:
            imported += 1
    return imported, already, failed


async def _run_backfill(
    request: Request, *, room_id: str, remote_room: RemoteActorRoom, count: int, thread_root_event_id: str | None,
) -> None:
    """The background half of ``_handle_backfill`` -- same
    inline-validate/background-run split as ``_run_follows_import``, for
    the same reason: even the default 15 posts means at least that many
    outbound HTTP round trips, easily past Synapse's AppService
    transaction timeout."""
    try:
        raw_items, fallback_author = await _resolve_backfill_source(
            request, remote_room=remote_room, count=count, thread_root_event_id=thread_root_event_id,
        )
        imported, already, failed = await _mirror_backfilled_notes(
            request, raw_items=raw_items, fallback_author=fallback_author,
        )
    except _BackfillSourceError as exc:
        await _notice(request, room_id, exc.notice)
        return
    except Exception:
        logger.exception("Backfill crashed in %s", room_id)
        await _notice(request, room_id, "Backfill hit an unexpected error and stopped early.")
        return

    summary = f"Backfill finished: {imported} post(s) mirrored, {already} already here"
    if failed:
        summary += f", {failed} couldn't be mirrored"
    summary += "."
    await _notice(request, room_id, summary)


async def _run_auto_backfill(request: Request, *, room_id: str, remote_room: RemoteActorRoom, count: int) -> None:
    """The one-time auto-backfill a brand-new follow's first join triggers
    (see ``bridge.membership.maybe_handle_join``, which consumes
    ``RemoteActorRoom.pending_backfill`` before calling this) -- same
    mechanism as ``;backfill`` with no explicit count, always the outbox
    (never a thread -- there's no Matrix thread to be inside yet for a
    room that was just created and hasn't even been joined).

    Failures are logged, not surfaced as a room notice -- unlike
    ``;backfill``, nobody asked for this, so an error notice about
    something they never requested would just be confusing. Success still
    gets an explanatory notice, since a pile of new messages appearing
    unprompted right after joining needs the context."""
    await _notice(
        request, room_id,
        f"New follow -- backfilling their latest {count} posts now. I'll post a summary here when it's done.",
    )
    try:
        raw_items, fallback_author = await _resolve_backfill_source(
            request, remote_room=remote_room, count=count, thread_root_event_id=None,
        )
        imported, already, failed = await _mirror_backfilled_notes(
            request, raw_items=raw_items, fallback_author=fallback_author,
        )
    except _BackfillSourceError as exc:
        logger.info("Auto-backfill for %s couldn't start: %s", remote_room.actor_id, exc.notice)
        return
    except Exception:
        logger.exception("Auto-backfill crashed in %s", room_id)
        return

    summary = f"Backfill finished: {imported} post(s) mirrored, {already} already here"
    if failed:
        summary += f", {failed} couldn't be mirrored"
    summary += "."
    await _notice(request, room_id, summary)


async def _handle_widget(request: Request, *, room_id: str) -> None:
    """Adds this bridge's room widget (``bridge.widget``) to the current
    room, on explicit ``;widget`` request -- see
    ``bridge.room_widget.add_bridge_widget`` for the actual mechanics
    (shared with every room the bridge creates automatically).

    A fresh widget id is minted every time this runs (rather than reusing
    a fixed one), so running it again in the same room adds a second
    instance instead of silently no-op'ing against an existing one -- if
    an old one is still there, remove it via the room's own widgets/apps
    settings; this command has no way to know from here whether removing
    it was actually wanted."""
    if await add_bridge_widget(request, room_id=room_id):
        await _notice(
            request, room_id,
            "Added the Fediverse Bridge widget to this room -- open it from your client's widgets/apps panel.",
        )
    else:
        await _notice(request, room_id, "Could not add the widget -- check the bridge's logs.")


async def _handle_import(request: Request, *, sender: str, room_id: str, url: str) -> None:
    """Fetch a single post by its source URL and force-mirror it, regardless
    of whether anyone follows its author -- for pulling in one specific post
    someone links you to, without committing to following the account.
    Never duplicates a post that's already been mirrored by this or any
    other path (a follow, another import, ...). Replies with a matrix.to
    link so the sender can jump straight to the resulting event.

    If the post is a reply, its AP ancestor chain is walked UP to wherever
    we already track one -- importing/mirroring any untracked ancestor
    along the way, all the way to the conversation's true root if nothing
    in between is tracked either (see ``bridge.inbox_dispatch``'s
    ``_backfill_ancestor_chain``, the same machinery a live inbound reply
    into an untracked conversation already uses) -- and threads the
    requested post onto whatever that resolves to, in THAT parent's own
    room. Deliberately does not walk back DOWN to pull in the rest of that
    thread -- ``;backfill``, run inside the resulting room, covers that.

    Only when there's no ``inReplyTo`` at all, or the chain can't be
    resolved (a deleted/inaccessible ancestor, or one deeper than
    ``_resolve_ancestor_chain`` is willing to fetch), does this fall back
    to the original behavior: creating/reusing a Remote User Room for the
    post's own author exactly like ``follow`` does (minus actually
    following), and inviting the sender into it."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    if not url:
        await _notice(request, room_id, f"Usage: {_COMMAND_PREFIX}import <fediverse post URL>")
        return
    if not url.startswith(("http://", "https://")):
        await _notice(request, room_id, "That doesn't look like a URL.")
        return

    http_client = request.app.state.http_client
    synapse = request.app.state.synapse
    repository = request.app.state.repository

    try:
        obj = await fetch_actor(request, url)
    except RemoteActorFetchError as exc:
        fallback_url = _pretty_post_url_fallback(url)
        if fallback_url is None:
            await _notice(request, room_id, f"Could not fetch {url}: {exc}")
            return
        try:
            obj = await fetch_actor(request, fallback_url)
        except RemoteActorFetchError:
            await _notice(request, room_id, f"Could not fetch {url}: {exc}")
            return

    if obj.get("type") not in ("Note", "Question"):
        await _notice(request, room_id, f"{url} doesn't look like a fediverse post (got {obj.get('type')!r}).")
        return

    author_actor_id = _note_author(obj)
    if not author_actor_id:
        await _notice(request, room_id, f"Could not determine who posted {url}.")
        return

    local_author_username = username_from_actor_url(config.bridge.public_base_url, author_actor_id)
    if local_author_username is not None:
        # A local actor's own post is already natively in Matrix, in their
        # own Profile Room -- there's nothing to "import" (and never a
        # ghost to invent for someone who already has a real Matrix
        # account; see resolve_and_invite_ghost's docstring). Just invite
        # into their real room instead, if they're linked at all.
        local_author = await repository.get_local_actor(local_author_username)
        if local_author is None or not local_author.room_id:
            await _notice(request, room_id, f"{url} looks like a local post, but that profile no longer exists.")
            return
        try:
            await synapse.invite_user(local_author.room_id, sender, as_user_id=bot_mxid)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", sender, local_author.room_id, exc)
        await _notice(
            request, room_id,
            f"{url} is a local post -- you've been invited to {local_author.room_id} instead of importing it.",
            html_message=(
                f"{html.escape(url)} is a local post -- you've been invited to "
                f"{room_pill_html(local_author.room_id)} instead of importing it."
            ),
        )
        return

    ap_object_id = obj.get("id")
    existing = await repository.get_federated_event_by_ap_object(ap_object_id) if ap_object_id else None
    if existing is not None:
        try:
            await synapse.invite_user(existing.room_id, sender, as_user_id=bot_mxid)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", sender, existing.room_id, exc)
        await _notice(
            request, room_id,
            f"Already imported: {matrix_to_link(existing.room_id, existing.event_id)}",
        )
        return

    if obj.get("type") == "Question":
        # Polls have no reply-chain-walk path (Mastodon's poll UI doesn't
        # offer replying to the poll object itself) -- straight to
        # import_question, which does its own ghost/room provisioning
        # (shared with live inbound poll mirroring; see its own docstring).
        try:
            author_doc = await fetch_actor(request, author_actor_id)
        except RemoteActorFetchError as exc:
            await _notice(request, room_id, f"Could not fetch the poll's author ({author_actor_id}): {exc}")
            return
        imported = await import_question(
            request, question=obj, author_actor_id=author_actor_id, author_doc=author_doc, inviter=sender
        )
        if imported.federated_event is None:
            await _notice(request, room_id, f"Fetched {url}, but failed to post it into Matrix.")
            return
        await _notice(
            request, room_id,
            f"Imported: {matrix_to_link(imported.federated_event.room_id, imported.federated_event.event_id)}",
        )
        return

    # If this is a reply, walk its AP ancestor chain up to wherever we
    # already track one, importing/mirroring any untracked ancestor along
    # the way -- all the way up to the conversation's true root if nothing
    # in between is tracked either (same machinery _handle_create already
    # uses for a live inbound reply into an untracked conversation; see
    # _backfill_ancestor_chain's own docstring). Deliberately does NOT walk
    # back down again to pull in the rest of that thread -- ";backfill"
    # covers that, given a room in that thread to run it from. If this
    # resolves to a real parent, the requested post is threaded onto it (in
    # THAT parent's room, not necessarily a room of its own -- a reply
    # belongs in the conversation it's part of, same convention as every
    # other reply this bridge mirrors) instead of getting its own Remote
    # User Room at all.
    in_reply_to = obj.get("inReplyTo")
    parent: FederatedEvent | None = None
    if in_reply_to:
        chain = await _resolve_ancestor_chain(request, in_reply_to)
        if chain is not None:
            chain_parent, missing_ancestors = chain
            parent, _imported_root = await _backfill_ancestor_chain(request, chain_parent, missing_ancestors)

    if parent is not None:
        new_federated_event = await _mirror_note_as_reply(request, obj, parent, author_actor_id)
        if new_federated_event is None:
            await _notice(request, room_id, f"Fetched {url}, but failed to post it into Matrix.")
            return
        try:
            await synapse.invite_user(new_federated_event.room_id, sender, as_user_id=bot_mxid)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", sender, new_federated_event.room_id, exc)
        await _notice(
            request, room_id,
            f"Imported: {matrix_to_link(new_federated_event.room_id, new_federated_event.event_id)}",
        )
        return

    # Not a reply, or its ancestor chain couldn't be resolved/imported at
    # all (deleted/inaccessible ancestor, or too deep) -- the original
    # standalone behavior: mirror into a Remote User Room dedicated to THIS
    # post's own author, creating one on demand.
    try:
        author_doc = await fetch_actor(request, author_actor_id)
    except RemoteActorFetchError as exc:
        await _notice(request, room_id, f"Could not fetch the post's author ({author_actor_id}): {exc}")
        return

    username = author_doc.get("preferredUsername") or author_actor_id.rstrip("/").rsplit("/", 1)[-1]
    domain = urlsplit(author_actor_id).hostname or ""
    if not domain:
        await _notice(request, room_id, f"Could not determine a domain for {author_actor_id}.")
        return
    localpart = ghost_localpart(config.appservice.user_prefix, username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, username, domain, config.synapse.server_name)

    remote_room = await repository.get_remote_actor_room(author_actor_id)
    if remote_room is None:
        display_name = author_doc.get("name") or username
        icon_url = extract_icon_url(author_doc)
        avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url) if icon_url else None
        banner_url = extract_banner_url(author_doc)
        banner_mxc = await fetch_and_upload_media(http_client, synapse, banner_url) if banner_url else None

        await ensure_ghost_user(
            synapse,
            server_name=config.synapse.server_name,
            localpart=localpart,
            display_name=display_name,
            avatar_mxc=avatar_mxc,
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
            # made admin there too.
            invite=[sender, bot_mxid],
            avatar_mxc=avatar_mxc,
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            join_rule=_KNOCK_JOIN_RULE,
            # bot_mxid kept at the same level as the ghost creator -- see
            # _establish_remote_follow's identical reasoning. events'
            # SOCIAL_PROFILE_USER_ID_STATE_TYPE override matches that same
            # function's identical reasoning too.
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
        await _send_bridge_info(
            request, room_id=new_room_id, actor_id=author_actor_id,
            display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
        )
        await add_bridge_widget(request, room_id=new_room_id)
        await _set_ghost_profile_room_id(request, mxid=mxid, room_id=new_room_id)
        await set_ghost_external_handle(
            request, mxid=mxid, handle=f"@{username}@{domain}", profile_url=extract_actor_url(author_doc),
        )
        await _set_profile_user_id(request, room_id=new_room_id, matrix_user_id=mxid, as_user_id=mxid)
        if banner_mxc:
            await _set_ghost_room_banner(request, room_id=new_room_id, ghost_user_id=mxid, banner_mxc=banner_mxc)
    else:
        try:
            await synapse.invite_user(remote_room.room_id, sender, as_user_id=remote_room.ghost_user_id)
        except SynapseError as exc:
            if exc.errcode != "M_FORBIDDEN":
                logger.warning("Could not invite %s to %s: %s", sender, remote_room.room_id, exc)

    # Plain top-level post (no inReplyTo at all, or the reply-chain handling
    # above couldn't resolve/import a parent to thread onto).
    mentions = await resolve_mention_pills(request, room_id=remote_room.room_id, note=obj)
    plain, safe_html = strip_to_matrix_message(obj.get("content") or "", mention_pills=mentions.pills)
    message_content: dict = {"msgtype": "m.text", "body": plain}
    if safe_html and safe_html != plain:
        message_content["format"] = "org.matrix.custom.html"
        message_content["formatted_body"] = safe_html
    if mentions.mentioned_locals:
        # A pill alone (the <a href="matrix.to/..."> above) only makes
        # the mention a clickable link -- an intentional mention
        # (MSC3952) is what actually highlights/notifies the tagged
        # user's client.
        message_content["m.mentions"] = {"user_ids": [a.matrix_user_id for a in mentions.mentioned_locals]}
    source_url = _source_post_url(obj)
    if source_url:
        message_content["external_url"] = source_url
    handle_content = await event_external_handle_content(request, author_actor_id)
    if handle_content:
        message_content[EXTERNAL_HANDLE_FIELD] = handle_content

    # Only the first attachment (if any) is embedded as real Matrix
    # media -- see attach_media_to_content's docstring for why the rest
    # are appended as plain links: an ActivityPub post always maps to
    # exactly one Matrix event.
    attachments = extract_attachments(obj)
    message_content, _ = await _attach_media_to_content(request, message_content, attachments)

    federation_config = request.app.state.config.federation
    try:
        event_id = await synapse.send_message_event(
            remote_room.room_id, message_content, as_user_id=remote_room.ghost_user_id,
            ts=resolve_event_ts(
                obj, max_clock_skew=federation_config.max_clock_skew,
                max_backdate_days=federation_config.max_backdate_days,
            ),
        )
    except SynapseError:
        logger.warning("Failed to import post from %s", author_actor_id, exc_info=True)
        await _notice(request, room_id, f"Fetched {url}, but failed to post it into Matrix.")
        return

    if ap_object_id:
        await repository.record_federated_event(
            FederatedEvent(
                event_id=event_id,
                room_id=remote_room.room_id,
                ap_object_id=ap_object_id,
                author_actor_id=author_actor_id,
            )
        )

    await notify_mentioned_locals(
        request,
        mentioned=mentions.mentioned_locals,
        room_id=remote_room.room_id,
        event_id=event_id,
        author_actor_id=author_actor_id,
    )

    await _notice(request, room_id, f"Imported: {matrix_to_link(remote_room.room_id, event_id)}")


def _reply_target_event_id(content: dict) -> str | None:
    """Find the event a ``;boost``/``;repost`` command was sent as a reply
    to -- covers both Matrix relation shapes (rich reply, thread reply),
    same as ``bridge.reply_bridge``'s own extraction. Duplicated rather than
    imported from there: ``reply_bridge`` imports ``message_addresses_bot``
    from this module, so the reverse import would be circular."""
    relates_to = content.get("m.relates_to") or {}
    if relates_to.get("rel_type") == "m.thread":
        in_reply_to = relates_to.get("m.in_reply_to") or {}
        return in_reply_to.get("event_id") or relates_to.get("event_id")
    in_reply_to = relates_to.get("m.in_reply_to") or {}
    return in_reply_to.get("event_id")


async def _resolve_poll_refresh_target(request: Request, content: dict) -> FederatedEvent | None:
    """Resolves whatever poll ``content`` was sent as a reply to -- either
    directly to the poll's own event, or to anything else inside its
    thread (most naturally the tallies reply itself, or a human reply),
    trying the specific reply target first and falling back to the
    thread's own root event id, since only the poll's own root event has a
    tracked ``FederatedEvent`` to resolve at all. ``None`` if ``content``
    isn't a reply/thread message at all, or resolves to nothing tracked.
    Shared by ``_handle_poll_refresh`` (";refresh poll") and bare
    ";refresh" (``_handle_refresh``, when run as a reply inside a poll's
    own thread instead of naming a ghost)."""
    repository = request.app.state.repository
    relates_to = content.get("m.relates_to") or {}
    candidate_ids: list[str] = []
    direct = (relates_to.get("m.in_reply_to") or {}).get("event_id")
    if direct:
        candidate_ids.append(direct)
    if relates_to.get("rel_type") == "m.thread":
        root = relates_to.get("event_id")
        if root and root not in candidate_ids:
            candidate_ids.append(root)

    for candidate_id in candidate_ids:
        target = await repository.get_federated_event_by_matrix_event(candidate_id)
        if target is not None:
            return target
    return None


async def _handle_poll_refresh(request: Request, *, room_id: str, content: dict) -> None:
    """";refresh poll" -- actively re-fetches a mirrored poll's own live AP
    object and refreshes its tallies reply / closed state right now,
    rather than only ever waiting for a push ``Update`` some remote
    implementations (confirmed for Pleroma/Akkoma) never send at all --
    see ``bridge.note_mirroring.refresh_poll_tallies``'s own docstring for
    the shared mechanism (the same one that already runs automatically
    right after a local vote)."""
    target = await _resolve_poll_refresh_target(request, content)
    if target is None:
        await _notice(
            request, room_id,
            f'Reply to a poll (or anything in its thread) with "{_COMMAND_PREFIX}refresh poll" '
            "to refresh its tallies.",
        )
        return

    if await refresh_poll_tallies(request, target=target):
        await _notice(request, room_id, "Refreshed.")
    else:
        await _notice(request, room_id, "Couldn't refresh that poll -- it may no longer be reachable.")


async def _handle_refresh(request: Request, *, sender: str, room_id: str, argument: str, content: dict) -> None:
    """``;refresh @user@instance.org`` (admin-only, or bare ``;refresh`` run
    inside that account's own Remote User Room, same "argument or implied
    by the room" convention as ``;follow`` -- see its own docstring; a
    tagged ghost mention pill or a bare Matrix ID both work here too, same
    as ``;follow`` -- see ``_resolve_tagged_ghost``/``_resolve_mxid_handle``)
    re-fetches a ghost's live ActivityPub actor document right now and
    brings everything this bridge keeps in sync with it up to date
    immediately, rather than waiting for whatever next triggers it
    naturally (a reply/reaction re-running ``sync_ghost_profile``, or an
    inbound ``Update`` the remote server may not even send -- Pleroma/Akkoma
    confirmed to never send one for a poll's own Question, and there's no
    reason to assume every implementation is better about a profile
    Update either):

    - The ghost's own Matrix display name/avatar -- only actually touched
      when the live document's value differs from what's already on
      record, same change-detection convention as sync_ghost_profile, so
      a repeat ;refresh with nothing genuinely changed doesn't re-upload
      the same avatar and show a spurious "changed their avatar" event.
    - The Remote User Room's name/avatar/banner (same change detection).
    - The MSC4503 ``m.external_handle`` profile field, brought into line
      with the CURRENT ``bridge.msc4503_external_handle`` setting either
      way -- set/refreshed if it now allows profile data ("profile"/
      "both"), actively removed if it doesn't (e.g. the operator just
      turned this off, or switched to "events" only), rather than leaving
      a stale value in place forever either way.

    Bare ``;refresh`` (no argument) run as a reply inside a poll's own
    thread refreshes THAT poll instead -- same resolution and permission
    level (any local user, no admin check) as ``;refresh poll`` itself,
    and mutually exclusive with the ghost-profile refresh above: replying
    to refresh a poll never also touches anyone's profile (explicit user
    request 2026-07-11). Only falls through to the admin-gated
    ghost-profile behavior below when this ISN'T a reply that resolves to
    a tracked poll at all.

    ``;refresh guild``, run inside one of that guild's own Channel rooms,
    re-syncs its live channel list instead -- see ``_handle_refresh_guild``.
    Also not admin-gated: being present in a Channel room at all already
    means the bot invited you as a recorded guild member (see
    ``bridge.channel_bridge.ensure_channel_room``), same trust level this
    bridge already extends anyone sufficiently-powered in a room elsewhere
    (e.g. a Profile Room's topic/name/avatar).
    """
    if argument.strip() == "guild":
        await _handle_refresh_guild(request, room_id=room_id)
        return

    if not argument.strip():
        poll_target = await _resolve_poll_refresh_target(request, content)
        if poll_target is not None:
            if await refresh_poll_tallies(request, target=poll_target):
                await _notice(request, room_id, "Refreshed.")
            else:
                await _notice(request, room_id, "Couldn't refresh that poll -- it may no longer be reachable.")
            return

    if not await _is_matrix_admin(request, sender):
        await _notice(request, room_id, "Only a Matrix server admin can refresh a ghost's profile.")
        return

    config = request.app.state.config
    http_client = request.app.state.http_client
    synapse = request.app.state.synapse
    repository = request.app.state.repository

    tagged_ghost = await _resolve_tagged_ghost(request, content)
    handle = argument.strip()
    if tagged_ghost is not None:
        remote_room = await repository.get_remote_actor_room(tagged_ghost.actor_id)
        if remote_room is None:
            await _notice(request, room_id, f"{tagged_ghost.handle} isn't a ghost on this bridge yet.")
            return
    elif handle:
        mxid_match = await _resolve_mxid_handle(request, handle)
        if mxid_match is not None:
            remote_actor_id, handle, local_record = mxid_match
            if local_record is not None:
                await _notice(request, room_id, f"{handle} is a local bridge user, not a ghost -- nothing to refresh.")
                return
        else:
            try:
                remote_actor_id = await resolve_remote_actor_id(http_client, handle)
            except WebfingerNotFoundError:
                await _notice(request, room_id, f"Couldn't find {handle} -- check the handle and try again.")
                return
            except WebfingerUnreachableError:
                await _notice(request, room_id, f"Couldn't reach {handle}'s server right now -- try again in a bit.")
                return
            except WebfingerError as exc:
                await _notice(request, room_id, f"Could not resolve {handle}: {exc}")
                return
        remote_room = await repository.get_remote_actor_room(remote_actor_id)
        if remote_room is None:
            await _notice(request, room_id, f"{handle} isn't a ghost on this bridge yet.")
            return
    else:
        remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
        if remote_room is None:
            await _notice(
                request, room_id,
                f"Usage: {_COMMAND_PREFIX}refresh @user@instance.org (or run this with no argument inside "
                "that account's own room)",
            )
            return
        remote_actor_id = remote_room.actor_id

    try:
        actor_doc = await fetch_actor(request, remote_actor_id)
    except RemoteActorFetchError as exc:
        await _notice(request, room_id, f"Could not fetch {remote_actor_id}: {exc}")
        return

    mxid = remote_room.ghost_user_id
    username = actor_doc.get("preferredUsername") or remote_actor_id.rstrip("/").rsplit("/", 1)[-1]
    domain = urlsplit(remote_actor_id).hostname or ""
    display_name = actor_doc.get("name") or username
    icon_url = extract_icon_url(actor_doc)
    banner_url = extract_banner_url(actor_doc)

    # Compared against what's already on record (same convention as
    # sync_ghost_profile) rather than unconditionally re-fetching/
    # re-uploading/re-setting every single time -- Synapse mints a brand
    # new mxc:// for every upload even of byte-identical content, so
    # skipping this check would re-upload the SAME avatar on every
    # ;refresh and show it as a spurious "changed their avatar" event in
    # the room and the ghost's own membership, even when nothing on the
    # ActivityPub side actually changed (confirmed live 2026-07-10).
    name_changed = display_name != remote_room.display_name
    icon_changed = icon_url != remote_room.icon_url
    banner_changed = banner_url != remote_room.banner_url

    avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url) if icon_changed and icon_url else None
    banner_mxc = (
        await fetch_and_upload_media(http_client, synapse, banner_url) if banner_changed and banner_url else None
    )

    # Ghost's own Matrix profile.
    if name_changed:
        try:
            await synapse.set_display_name(mxid, display_name)
        except SynapseError:
            logger.info("Could not refresh display name for %s", mxid, exc_info=True)
    if avatar_mxc:
        try:
            await synapse.set_avatar_url(mxid, avatar_mxc)
        except SynapseError:
            logger.info("Could not refresh avatar for %s", mxid, exc_info=True)

    # The Remote User Room's own name/avatar/banner.
    if name_changed:
        try:
            await synapse.send_state_event(
                remote_room.room_id, "m.room.name", "", {"name": display_name}, as_user_id=mxid
            )
        except SynapseError:
            logger.info("Could not refresh room name for %s", remote_room.room_id, exc_info=True)
    if avatar_mxc:
        try:
            await synapse.send_state_event(
                remote_room.room_id, "m.room.avatar", "", {"url": avatar_mxc}, as_user_id=mxid
            )
        except SynapseError:
            logger.info("Could not refresh room avatar for %s", remote_room.room_id, exc_info=True)
    if banner_mxc:
        await _set_ghost_room_banner(request, room_id=remote_room.room_id, ghost_user_id=mxid, banner_mxc=banner_mxc)

    # MSC4503 -- brought into line with the CURRENT setting either way.
    handle_str = f"@{username}@{domain}"
    if config.bridge.msc4503_external_handle in ("profile", "both"):
        await set_ghost_external_handle(
            request, mxid=mxid, handle=handle_str, profile_url=extract_actor_url(actor_doc)
        )
    else:
        await clear_ghost_external_handle(request, mxid=mxid)

    # Keep this bridge's own cached bookkeeping (used by sync_ghost_profile
    # and every other reply/reaction-driven sync) from immediately treating
    # this as "changed again" the next time one of those runs.
    await repository.record_ghost_profile(
        GhostProfile(actor_id=remote_actor_id, display_name=display_name, icon_url=icon_url, mxid=mxid, handle=handle_str)
    )
    await repository.register_remote_actor_room(
        dataclasses.replace(remote_room, display_name=display_name, icon_url=icon_url, banner_url=banner_url)
    )

    await _notice(request, room_id, f"Refreshed {handle_str}.")


async def _handle_refresh_guild(request: Request, *, room_id: str) -> None:
    """``;refresh guild``, run inside one of a joined guild's own Channel
    rooms -- re-fetches that guild's live ``channels`` collection right now
    and creates a Matrix room for any channel added since it was joined (or
    since the last refresh), rather than waiting for a new channel's first
    message to trigger lazy discovery (see
    ``bridge.channel_bridge.maybe_handle_channel_message``'s own docstring
    on why that's the only OTHER way this bridge ever finds out -- Shoot
    itself never federates channel creation at all, confirmed by reading
    its own source). Useful specifically for an empty new channel nobody's
    posted in yet, which lazy discovery alone would never surface."""
    repository = request.app.state.repository
    channel_room = await repository.get_channel_room_by_room_id(room_id)
    if channel_room is None:
        await _notice(
            request, room_id,
            f'"{_COMMAND_PREFIX}refresh guild" only works inside one of that guild\'s own Channel rooms.',
        )
        return

    # Deferred: bridge.channel_bridge already imports from bridge.commands
    # (message_addresses_bot, _collect_recent_items -- both deferred
    # themselves for the identical reason), so importing from there at
    # module level here would be circular.
    from bridge.channel_bridge import sync_guild_channels

    channels = await sync_guild_channels(request, channel_room.guild_actor_id)
    if channels:
        await _notice(request, room_id, f"Refreshed -- {len(channels)} channel(s) known now.")
    else:
        await _notice(request, room_id, "Couldn't refresh that guild's channel list -- it may not be reachable.")


async def _handle_repost(
    request: Request, *, sender: str, room_id: str, content: dict, caption: str, event_id: str | None,
) -> None:
    """Repost the fediverse post ``;repost`` (or its undocumented ``;boost``
    alias) was sent as a reply to -- MSC4501 models a caption-less repost
    and one WITH added commentary (a "quote-post" in Mastodon/Fediverse
    terms) as the same underlying ``social.repost`` relation, just with or
    without inline content, so this bridge merges them into one command the
    same way (renamed/unified 2026-07-11; ``;boost`` used to be this
    caption-less half's own separate, since-retired command):

    - No caption (bare ``;repost``, or ``;boost``): a real ``Announce`` of
      the original -- see ``bridge.reaction_bridge.send_repost``, the exact
      same function reacting with 🔁 already calls, so a command-triggered
      repost and a reaction-triggered one are indistinguishable afterwards
      (same "you reposted" card, same ``ReactionRecord``, same Undo path).
    - A caption (``;repost <your caption>``): a brand new ``Create`` of the
      sender's own, with ``caption`` as its text, so it carries commentary
      an Announce has no room for. Marked as quoting the original via
      ``Note.quote_uri`` (see its docstring) for AP receivers that render
      an actual quote card, with a plain link still appended to the content
      for ones that don't.

    Delivered and recorded like an ordinary top-level post (see
    ``bridge.profile_posts``) either way, but the bot's own rendering of it
    always posts into the sender's OWN Profile Room specifically -- never
    wherever the command happened to be run from (most commonly a reply to
    the original post inside ITS OWN author's Remote User Room), which
    would otherwise read as a notification landing in a room that isn't the
    reposter's timeline at all, and that their own followers -- the actual
    intended audience -- aren't even in. A plain "Reposted." notice still
    goes wherever the command was run, but only when that's a different
    room, so the sender gets some acknowledgement there too. With a
    caption, that rendering is the caption, then an "X reposted Y's post"
    line (pills -- see ``actor_html_with_avatar``) and a preview of the
    original underneath: a real image/video attachment if the original
    post had one (see ``build_preview_media_content`` -- Element X doesn't
    render one embedded in a caption's own HTML at all), otherwise a
    ``<blockquote>`` of its text, not a bare appended link.

    Every notice this sends (including the usage/error ones, and the final
    "Reposted." ack) replies to the command message itself, in the same
    thread if it was sent as a thread reply -- see
    ``_command_reply_relates_to``."""
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    repository = request.app.state.repository
    relates_to = _command_reply_relates_to(content, event_id)

    target_event_id = _reply_target_event_id(content)
    caption = caption.strip()
    if not target_event_id:
        await _notice(
            request, room_id,
            f'Reply to a fediverse post with "{_COMMAND_PREFIX}repost" (or "{_COMMAND_PREFIX}repost '
            '<your caption>" to add your own commentary) to repost it.',
            relates_to=relates_to,
        )
        return

    parent = await repository.get_federated_event_by_matrix_event(target_event_id)
    if parent is None:
        await _notice(
            request, room_id, "That message isn't a fediverse post I'm tracking, so there's nothing to repost.",
            relates_to=relates_to,
        )
        return

    actor_record = await repository.get_local_actor_by_matrix_id(sender)
    if actor_record is None:
        await _notice(
            request, room_id, f'Link a profile first: run "{_COMMAND_PREFIX}link profile".', relates_to=relates_to,
        )
        return

    if not caption:
        # Bare repost -- exactly what reacting with 🔁 already does; see
        # send_repost's own docstring for why this is shared code, not a
        # separate implementation. Imported locally (not at module level)
        # to avoid a circular import: bridge.reaction_bridge itself
        # imports from this module (is_third_party_still_allowed), same
        # reasoning bridge.reply_bridge's own docstring gives for why IT
        # duplicates rather than imports a helper from here.
        from bridge.reaction_bridge import send_repost

        await send_repost(
            request, actor_record=actor_record, parent=parent, matrix_event_id=event_id,
            room_id=room_id, reactor_matrix_user_id=sender,
        )
        if room_id != actor_record.room_id:
            await _notice(request, room_id, "Reposted.", relates_to=relates_to)
        return

    # Same reasoning as send_repost: repost the actual original post/author,
    # not a mirrored repost message's own Announce-activity bookkeeping.
    target_object_id = parent.reposted_object_id or parent.ap_object_id
    target_author_actor_id = parent.reposted_author_actor_id or parent.author_actor_id

    base = config.bridge.public_base_url
    own_actor_id = actor_url(base, actor_record.username)

    body, mention_tags, mention_cc = await resolve_pill_mentions(request, caption, content)
    already_tagged = {tag["name"].lstrip("@").lower() for tag in mention_tags if tag.get("name")}
    plaintext_tags, plaintext_cc = await resolve_plaintext_mentions(request, body, already_tagged=already_tagged)
    mention_tags += plaintext_tags
    mention_cc += plaintext_cc
    mention_links = {tag["name"]: tag["href"] for tag in mention_tags if tag.get("name") and tag.get("href")}

    note_id = f"{own_actor_id}/notes/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Shared with bridge.activitypub.routes's outbox/get_note
    # reconstruction (see build_repost_note_content's docstring) so a
    # re-fetch of this same post can never drift from what actually went
    # out here.
    note_html = build_repost_note_content(body, target_object_id, mention_links)
    note = Note(
        id=note_id,
        attributed_to=own_actor_id,
        content=note_html,
        published=published,
        to=[AS_PUBLIC],
        cc=[followers_url(base, actor_record.username), target_author_actor_id, *mention_cc],
        tag=mention_tags,
        quote_uri=target_object_id,
    )
    create_activity = Activity(
        id=f"{note_id}/activity", type="Create", actor=own_actor_id, object=note,
        published=published, to=note.to, cc=note.cc,
    )
    activity_dict = create_activity.to_dict()

    await deliver_to_actor_or_followers(
        request, target_actor_id=own_actor_id, activity=activity_dict,
        key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
    )
    if target_author_actor_id and target_author_actor_id != own_actor_id:
        await deliver_to_actor_or_followers(
            request, target_actor_id=target_author_actor_id, activity=activity_dict,
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )
    for mention_actor_id in dict.fromkeys(mention_cc):
        if mention_actor_id in (own_actor_id, target_author_actor_id):
            continue
        await deliver_to_actor_or_followers(
            request, target_actor_id=mention_actor_id, activity=activity_dict,
            key_id=main_key_id(base, actor_record.username), private_key_pem=actor_record.private_key_pem,
        )

    # The real (possibly mirrored-copy) Matrix event for the ORIGINAL post,
    # not necessarily `parent` itself -- `parent` is whatever message the
    # ";repost" command was sent as a reply to, which (same reasoning as
    # send_repost) might itself be a mirrored repost's own card rather than
    # the original post. Falls back to `parent` in the (shouldn't-happen)
    # case that lookup somehow misses.
    preview_target = await repository.get_federated_event_by_ap_object(target_object_id) or parent
    preview_text, preview_full_content, preview_image, preview_video = await _fetch_post_preview(
        request, preview_target
    )
    post_link = matrix_to_link(preview_target.room_id, preview_target.event_id)

    # preview_text alongside preview media means the post had BOTH media
    # and a real caption (_fetch_post_preview only returns caption-worthy
    # text for media posts) -- quote the caption AND attach the media
    # preview, same as bridge.reaction_bridge.send_repost's identical card.
    quote_block_html = f"<blockquote>{html.escape(preview_text)}</blockquote>" if preview_text else ""

    _, reposter_html = await actor_html_with_avatar(request, own_actor_id)
    original_handle, original_author_html = await actor_html_with_avatar(request, target_author_actor_id)
    _, original_displayname, original_sender = await resolve_actor_matrix_identity(
        request, target_author_actor_id
    )

    plain_body = f"\U0001F501 reposted {original_handle}'s post:"
    if preview_text:
        plain_body += f"\n> {preview_text}"
    plain_body += f"\n{post_link}"
    plain_body = f"{caption}\n\n{plain_body}"

    post_pill_html = f'<a href="{html.escape(post_link, quote=True)}">post</a>'
    caption_html = f"<p>{html.escape(caption)}</p>"
    formatted_caption = (
        caption_html
        + f"<p>\U0001F501 {reposter_html} reposted {original_author_html}'s {post_pill_html}</p>{quote_block_html}"
    )
    echo_content = build_preview_media_content(
        plain_body=plain_body, formatted_caption=formatted_caption,
        preview_image=preview_image, preview_video=preview_video,
    )
    if config.bridge.set_msc4501_relates_to and preview_full_content and original_sender is not None:
        echo_content[SOCIAL_RELATES_TO_FIELD] = social_relates_to(
            SOCIAL_REL_TYPE_REPOST,
            event_id=preview_target.event_id, room_id=preview_target.room_id,
            sender=original_sender, displayname=original_displayname, content=preview_full_content,
        )
        # A compliant client's replacement for body/formatted_body: just the
        # reposter's OWN caption, without the "🔁 reposted X's post: ..."
        # tail appended above -- see SOCIAL_BODY_FIELD's own docstring. This
        # branch only runs with a real caption (the bare-repost case returns
        # via send_repost long before here), so it's never empty.
        echo_content[SOCIAL_BODY_FIELD] = caption
        echo_content[SOCIAL_FORMATTED_BODY_FIELD] = caption_html
    # Always lands in the reposter's OWN Profile Room -- never wherever the
    # command happened to be run from (e.g. a reply inside the ORIGINAL
    # author's Remote User Room, replying to their own mirrored post there),
    # which would otherwise post what reads as a self-notification into a
    # room that isn't the reposter's own timeline at all, and that their own
    # followers (the actual intended audience) aren't even in.
    destination_room_id = actor_record.room_id
    try:
        echo_event_id = await request.app.state.synapse.send_message_event(
            destination_room_id, echo_content, as_user_id=bot_mxid
        )
    except SynapseError:
        logger.warning("Failed to post repost echo in %s", destination_room_id, exc_info=True)
        return

    await repository.record_federated_event(
        FederatedEvent(
            event_id=echo_event_id, room_id=destination_room_id, ap_object_id=note_id, author_actor_id=own_actor_id,
            # So a re-dereference of this Note (see
            # bridge.activitypub.routes.get_note) still knows it's a quote
            # and can put quoteUri back on the reconstructed object --
            # without this, a receiver that re-fetches the Note fresh
            # (rather than trusting the originally-delivered Create's
            # embedded copy) would see a plain post with no quote at all.
            reposted_object_id=target_object_id, reposted_author_actor_id=target_author_actor_id,
        )
    )
    if room_id != destination_room_id:
        await _notice(request, room_id, "Reposted.", relates_to=relates_to)


async def _handle_create_profile(request: Request, *, sender: str, room_id: str) -> None:
    """Do everything ``link profile`` otherwise requires the user to have
    already set up manually themselves (their own room, with the bot
    invited into it and given enough power to set its name/avatar) -- create
    the room itself (the bot as creator/admin), invite the sender and
    promote them to admin there too, mirror their current Matrix display
    name/avatar onto it, tag it as bridge-made (MSC2346) and as an
    ActivityPub profile room (MSC4501), and mint the actual linked local
    actor -- all as a single atomic command. ``link profile`` still exists
    unchanged for anyone who'd rather use their own already-existing room.
    """
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    existing = await repository.get_local_actor_by_matrix_id(sender)
    if existing is not None and existing.room_id:
        await _notice(
            request, room_id,
            f"{sender} is already linked as {existing.username}@{config.bridge.domain} "
            f"(room {existing.room_id}).",
            html_message=(
                f"{html.escape(sender)} is already linked as {existing.username}@{config.bridge.domain} "
                f"(room {room_pill_html(existing.room_id)})."
            ),
        )
        return

    # An `existing` record with no room_id is a previously `unlink profile`d
    # identity -- see _handle_link_profile's identical reasoning: reattach
    # the SAME actor to the freshly-created room instead of minting a new one.
    username = existing.username if existing is not None else sanitize_localpart_component(sender[1:].split(":", 1)[0])

    try:
        profile = await synapse.get_profile(sender)
    except SynapseError:
        profile = {}
    matrix_display_name = profile.get("displayname")
    matrix_avatar_mxc = profile.get("avatar_url")
    display_name = matrix_display_name or username
    icon_url = None
    if matrix_avatar_mxc:
        try:
            icon_url = media_url(config.bridge.public_base_url, matrix_avatar_mxc)
        except ValueError:
            icon_url = None
        else:
            await repository.mark_media_published(matrix_avatar_mxc)

    try:
        new_room_id = await synapse.create_room(
            as_user_id=bot_mxid,
            name=display_name,
            # No placeholder topic -- the room's topic IS the bio now (see
            # maybe_handle_topic_change), so it starts unset for a brand
            # new identity, or carries an existing bio over verbatim when
            # reattaching a previously `unlink profile`d one.
            topic=existing.summary if existing is not None and existing.summary else None,
            invite=[sender],
            avatar_mxc=matrix_avatar_mxc,
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            join_rule=_KNOCK_JOIN_RULE,
            # sender is 99, not 100 -- one level below the bot (the
            # creator, as_user_id above -- deliberately NOT listed here;
            # room v12 rejects m.room.power_levels outright if the creator
            # appears in its own `users`, and every earlier room version
            # already defaults a room's creator to 100 on its own), so
            # sender is never locked out of moderating its own creation
            # (kicking the owner back out, e.g. as part of `delete
            # profile`) by them being at the exact same power level.
            # events' SOCIAL_PROFILE_USER_ID_STATE_TYPE override is
            # MSC4501's own explicit guidance -- without it, any Moderator
            # (not just this room's actual owner) could reassign the
            # profile out from under sender.
            power_level_content_override={
                "users": {sender: 99},
                "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            },
        )
    except SynapseError as exc:
        logger.warning("Could not create profile room for %s: %s", sender, exc)
        await _notice(
            request, room_id,
            f'Could not create a room for you -- you can still make one yourself and run '
            f'"{_COMMAND_PREFIX}link profile" in it instead.',
        )
        return

    if existing is not None:
        record = dataclasses.replace(existing, room_id=new_room_id, display_name=display_name, icon_url=icon_url)
    else:
        private_key_pem, public_key_pem = generate_keypair()
        record = ActorRecord(
            username=username,
            matrix_user_id=sender,
            room_id=new_room_id,
            public_key_pem=public_key_pem,
            private_key_pem=private_key_pem,
            display_name=display_name,
            icon_url=icon_url,
        )
    await repository.register_local_actor(record)
    if existing is None:
        await welcome_new_user(request, matrix_user_id=sender)
    else:
        # welcome_new_user (above) already covers a brand-new identity --
        # this is the reattach-after-`unlink profile` case instead, whose
        # own welcome message below unconditionally claims they've been
        # invited to their notifications room. ensure_bot_dm_invite makes
        # that true even if they'd left it (or it was somehow never
        # actually joined) since the room record itself was created.
        await ensure_bot_dm_invite(request, matrix_user_id=sender)

    base = config.bridge.public_base_url
    await _send_bridge_info(
        request, room_id=new_room_id, actor_id=actor_url(base, username),
        display_name=display_name, avatar_mxc=matrix_avatar_mxc, as_user_id=bot_mxid,
    )
    await add_bridge_widget(request, room_id=new_room_id)
    await _set_profile_user_id(request, room_id=new_room_id, matrix_user_id=sender, as_user_id=bot_mxid)
    await add_room_to_space(request, matrix_user_id=sender, child_room_id=new_room_id)

    verb = "Re-created" if existing is not None else "Created"
    message = f"{verb} {new_room_id} and linked it as {username}@{config.bridge.domain} -- you've been invited and made admin there."
    html_message = (
        f"{verb} {room_pill_html(new_room_id)} and linked it as {username}@{config.bridge.domain} "
        "-- you've been invited and made admin there."
    )
    if existing is not None:
        message += " Your followers/following from before are preserved."
        html_message += " Your followers/following from before are preserved."
    await _notice(request, room_id, message, html_message=html_message)


async def _handle_link_profile(request: Request, *, sender: str, room_id: str) -> None:
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse

    existing = await repository.get_local_actor_by_matrix_id(sender)
    if existing is not None and existing.room_id:
        await _notice(
            request, room_id,
            f"{sender} is already linked as {existing.username}@{config.bridge.domain} "
            f"(room {existing.room_id}).",
            html_message=(
                f"{html.escape(sender)} is already linked as {existing.username}@{config.bridge.domain} "
                f"(room {room_pill_html(existing.room_id)})."
            ),
        )
        return

    # An `existing` record with no room_id is a previously `unlink profile`d
    # identity -- reattach the SAME actor (same username, keys, followers,
    # following) to this room instead of minting a brand new one, which is
    # what actually lets someone move their profile to a different room
    # without their fediverse followers ever noticing anything changed.
    username = existing.username if existing is not None else sanitize_localpart_component(sender[1:].split(":", 1)[0])

    try:
        profile = await synapse.get_profile(sender)
    except SynapseError:
        profile = {}
    matrix_display_name = profile.get("displayname")
    matrix_avatar_mxc = profile.get("avatar_url")
    display_name = matrix_display_name or username
    icon_url = None
    if matrix_avatar_mxc:
        try:
            icon_url = media_url(config.bridge.public_base_url, matrix_avatar_mxc)
        except ValueError:
            icon_url = None
        else:
            await repository.mark_media_published(matrix_avatar_mxc)

    if existing is not None:
        # Preserve their existing bio as-is -- don't adopt whatever topic
        # this (possibly entirely different) room happens to already have.
        record = dataclasses.replace(existing, room_id=room_id, display_name=display_name, icon_url=icon_url)
    else:
        # A brand new identity has no bio yet -- if this pre-existing room
        # already has a topic, treat it as the starting one rather than
        # discarding it, since the room's topic IS the bio now (see
        # maybe_handle_topic_change).
        initial_summary = ""
        try:
            topic_content = await synapse.get_room_state(room_id, "m.room.topic", as_user_id=_bot_mxid(config))
            initial_summary = topic_content.get("topic") or ""
        except SynapseError:
            pass
        private_key_pem, public_key_pem = generate_keypair()
        record = ActorRecord(
            username=username,
            matrix_user_id=sender,
            room_id=room_id,
            public_key_pem=public_key_pem,
            private_key_pem=private_key_pem,
            display_name=display_name,
            icon_url=icon_url,
            summary=initial_summary,
        )
    await repository.register_local_actor(record)
    if existing is None:
        await welcome_new_user(request, matrix_user_id=sender)
    else:
        # Same reasoning as _handle_create_profile's identical branch: this
        # is a reattach-after-`unlink profile`, whose own welcome message
        # below unconditionally claims they've been invited to their
        # notifications room -- make sure that's actually still true.
        await ensure_bot_dm_invite(request, matrix_user_id=sender)
    await add_room_to_space(request, matrix_user_id=sender, child_room_id=room_id)

    # Best-effort: this requires the bot to have a high enough power level in
    # the user's room, which it won't by default unless granted one.
    room_styling_failed = False
    try:
        await synapse.send_state_event(room_id, "m.room.name", "", {"name": display_name})
        if matrix_avatar_mxc:
            await synapse.send_state_event(room_id, "m.room.avatar", "", {"url": matrix_avatar_mxc})
    except SynapseError:
        logger.info("Could not set room name/avatar for %s's profile room", sender, exc_info=True)
        room_styling_failed = True

    # Same best-effort caveat as room name/avatar above -- an already-
    # existing room's power levels weren't set up by us at creation time
    # the way _handle_create_profile's own power_level_content_override
    # is, so protecting this is only possible if the bot happens to have
    # high enough power here already.
    await _set_profile_user_id(request, room_id=room_id, matrix_user_id=sender, as_user_id=_bot_mxid(config))
    await _protect_profile_user_id_power_level(request, room_id=room_id)

    verb = "Relinked" if existing is not None else "Linked"
    message = f"{verb}! This room now publishes as {username}@{config.bridge.domain}."
    if existing is not None:
        message += " Your followers/following from before are preserved."
    if room_styling_failed:
        message += (
            " (Couldn't automatically set this room's name/avatar -- give the bot a higher "
            "power level here if you'd like that to happen automatically.)"
        )
    await _notice(request, room_id, message)


async def _handle_unlink_profile(request: Request, *, sender: str, room_id: str) -> None:
    """Detach this room from the sender's linked fediverse identity WITHOUT
    telling the fediverse side anything at all -- no Delete is sent, and
    followers/following/keys are all left exactly as they are. This is what
    lets someone move their identity to a different room (unlink here, then
    `link profile`/`create profile` in the new one to reattach the same
    actor) with nobody on the other end ever noticing the room changed.
    For a permanent, fediverse-visible removal instead, use `delete profile`.
    """
    repository = request.app.state.repository
    config = request.app.state.config

    record = await repository.get_local_actor_by_matrix_id(sender)
    if record is None:
        await _notice(request, room_id, "You don't have a linked profile to unlink.")
        return

    await repository.register_local_actor(dataclasses.replace(record, room_id=""))

    await _notice(
        request, room_id,
        f"Unlinked -- {record.username}@{config.bridge.domain} still exists on the fediverse exactly as "
        "it was (followers, following, and keys are all preserved); nobody there was told anything "
        f'changed. Run "{_COMMAND_PREFIX}link profile" or "{_COMMAND_PREFIX}create profile" in a room to '
        f'reattach it, or "{_COMMAND_PREFIX}delete profile" instead if you actually want to permanently '
        "remove it from the fediverse.",
    )


_DELETE_PROFILE_WARNING_MARKER = "This will permanently delete your fediverse identity"


async def _handle_delete_profile(request: Request, *, sender: str, room_id: str) -> None:
    """First half of a two-step, confirmation-gated deletion: sends an
    itemized warning of everything this is about to do (irreversible) and
    tells the sender to reply "confirm" to it to actually go through --
    nothing is deleted yet just from running this command. See
    ``maybe_handle_delete_confirmation`` for how that reply is recognized
    (deliberately not tied to any DB state -- see its own docstring), and
    ``_execute_delete_profile`` for what actually runs once confirmed."""
    repository = request.app.state.repository
    config = request.app.state.config

    record = await repository.get_local_actor_by_matrix_id(sender)
    if record is None:
        await _notice(request, room_id, "You don't have a linked profile to delete.")
        return

    steps = [
        "Notify your followers with a signed Delete, so their side actually cleans up "
        "(no more endless retries or a stale follow of an identity that's about to 404).",
        "Kick you from every other fediverse room the bridge put you in (accounts you follow, "
        "any fediverse DMs/Chats) -- except this room.",
        "Kick you from your Fediverse space.",
        "Unlink this room from your fediverse identity.",
        'Rename this room to end in " (Deleted)".',
        "Permanently delete your fediverse identity -- keys, followers, following, all of it.",
        "Post a notice here once it's done -- this room is otherwise left alone; you can leave "
        "it whenever you like, then or now.",
    ]
    warning = (
        f"⚠️ {_DELETE_PROFILE_WARNING_MARKER} ({record.username}@{config.bridge.domain}). "
        "This cannot be undone. Specifically, I will:\n"
        + "\n".join(f"- {step}" for step in steps)
        + '\n\nReply to THIS message with "confirm" to go ahead.'
    )
    formatted_warning = (
        f"<p>⚠️ {_DELETE_PROFILE_WARNING_MARKER} "
        f"(<strong>{html.escape(record.username)}@{html.escape(config.bridge.domain)}</strong>). "
        "This cannot be undone. Specifically, I will:</p><ul>"
        + "".join(f"<li>{html.escape(step)}</li>" for step in steps)
        + '</ul><p>Reply to THIS message with "confirm" to go ahead.</p>'
    )
    warning_content: dict = {
        "msgtype": "m.text",
        "body": warning,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_warning,
    }
    relates_to = _command_relates_to_var.get()
    if relates_to:
        warning_content["m.relates_to"] = relates_to
    try:
        await request.app.state.synapse.send_message_event(room_id, warning_content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send delete-profile warning to %s", room_id, exc_info=True)


async def maybe_handle_delete_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this event was a "confirm" reply to one of our own
    ``delete profile`` warnings (handled, whether or not it actually
    matched one) -- checked independent of ``message_addresses_bot``/the
    ``;`` prefix, since the reply is just the bare word "confirm", not a
    tagged command. Must run before ``maybe_federate_reply``/
    ``maybe_distribute_profile_post`` in the dispatch chain (see
    ``bridge.appservice_routes``), or a genuine "confirm" reply sent inside
    a Profile Room would otherwise get mirrored out as an ordinary post.

    Deliberately stateless (no DB row recording "a deletion is pending"):
    the replied-to event is fetched and checked for
    ``_DELETE_PROFILE_WARNING_MARKER`` (only ever present in a message this
    bridge itself sent, from ``_handle_delete_profile``) instead. This also
    means the actual deletion always acts on whoever SENT "confirm", not
    whoever originally ran ``delete profile`` -- fine, since that's just
    ``_execute_delete_profile`` looking up its own sender's linked profile
    same as any other command; someone else replying "confirm" in the same
    room can only ever delete THEIR OWN profile, never someone else's."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    # A rich reply's body carries a "> quoted text" fallback prefix ahead of
    # what was actually typed (for clients that don't render rich replies)
    # -- has to come off before comparing, or a genuine "confirm" reply
    # would never match.
    if strip_reply_fallback(content.get("body") or "").strip().lower() != "confirm":
        return False
    target_event_id = _reply_target_event_id(content)
    if not target_event_id:
        return False

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not room_id or not sender:
        return False

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    try:
        target_event = await request.app.state.synapse.get_event(room_id, target_event_id, as_user_id=bot_mxid)
    except SynapseError:
        return False
    if target_event.get("sender") != bot_mxid:
        return False
    if _DELETE_PROFILE_WARNING_MARKER not in ((target_event.get("content") or {}).get("body") or ""):
        return False

    # A "confirm" reply is its own separate dispatch entry point (this
    # function), not routed through maybe_handle_command -- so it needs
    # its own _command_relates_to_var scoping, from this reply's own
    # content, for _execute_delete_profile's own notices to land back in
    # whatever thread the "confirm" was itself sent in (independent of
    # whether the original ";delete profile" was also sent in-thread).
    token = _command_relates_to_var.set(_preserve_command_thread(content, event.get("event_id")))
    try:
        await _execute_delete_profile(request, sender=sender, room_id=room_id)
    finally:
        _command_relates_to_var.reset(token)
    return True


async def _list_bridge_managed_rooms(request: Request, sender: str) -> list[str]:
    """Every bridge-managed room (Remote User Room, ghost DM/Chat room)
    ``sender`` -- a real local Matrix user, never a ghost/the bot -- is
    CURRENTLY a member of. Shared by ``_execute_delete_profile``'s room
    sweep and ``_list_unfollowed_ghost_rooms``.

    Ghost DM/Chat rooms are found directly and exactly, straight from this
    bridge's own repository (each row already records its one owning
    local user -- ``list_ghost_dm_rooms_for_user``/
    ``list_ghost_chat_rooms_for_user``), then confirmed via a live
    ``get_joined_members`` check (the bot is already a member of every
    one of these -- see ``ensure_ghost_dm_room``/its Chat counterpart)
    before being included. That confirmation step is required, not
    redundant: confirmed live 2026-07-14 that this bridge's own
    bookkeeping can list a DM/Chat room as ``sender``'s CURRENT one for a
    ghost even after they've actually left it in Matrix (nothing clears
    that record on a plain room-leave) -- without this check, a stale row
    would make ``_execute_delete_profile`` attempt (harmlessly, but
    noisily) to kick someone from a room they're not even in anymore.
    Cheap regardless -- bounded by this ONE user's own DM/Chat room count,
    not bridge-wide.

    A Remote User Room has no such single owner (anyone following, or
    who's ever had a post imported from, that same remote actor can be a
    member), so it's resolved differently depending on
    ``bridge.use_synapse_admin_api``:
    - On (default): one ``admin_list_joined_rooms`` call gets EVERY room
      ``sender`` is in, homeserver-wide; kept only the ones that turn out
      to already be a tracked Remote User Room. O(1) Admin API call, and
      already exclusively "join" state by construction -- no separate
      confirmation step needed here the way the DM/Chat case above does.
    - Off (see that setting's own docstring -- only tested against
      Synapse itself, untested on every other homeserver): no admin call
      exists to make, so this instead walks EVERY Remote User Room this
      bridge tracks bridge-wide (``list_all_remote_actor_room_ids``) and
      checks live room membership per room as the bot (already a member
      of each -- see ``ensure_remote_actor_room``). Correct, but
      O(bridge-wide room count) instead of O(this user's rooms) --
      meaningfully slower at real scale, which is exactly why the fast
      path above is still used whenever the Admin API is actually
      available."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    dm_chat_room_ids = list(await repository.list_ghost_dm_rooms_for_user(sender))
    dm_chat_room_ids += await repository.list_ghost_chat_rooms_for_user(sender)
    rooms: list[str] = []
    for candidate_room_id in dm_chat_room_ids:
        try:
            members = await synapse.get_joined_members(candidate_room_id, as_user_id=bot_mxid)
        except SynapseError:
            continue
        if sender in members:
            rooms.append(candidate_room_id)

    if config.bridge.use_synapse_admin_api:
        try:
            joined_rooms = await synapse.admin_list_joined_rooms(sender)
        except SynapseError:
            logger.warning("Could not list rooms for %s via Admin API", sender, exc_info=True)
            joined_rooms = []
        for candidate_room_id in joined_rooms:
            if await repository.get_remote_actor_room_by_room_id(candidate_room_id) is not None:
                rooms.append(candidate_room_id)
        return rooms

    for candidate_room_id in await repository.list_all_remote_actor_room_ids():
        try:
            members = await synapse.get_joined_members(candidate_room_id, as_user_id=bot_mxid)
        except SynapseError:
            continue
        if sender in members:
            rooms.append(candidate_room_id)
    return rooms


async def _execute_delete_profile(request: Request, *, sender: str, room_id: str) -> None:
    """The actual, irreversible deletion -- only ever reached via a
    confirmed ``maybe_handle_delete_confirmation``. See
    ``_handle_delete_profile``'s warning message for the itemized list of
    what this does; kept in that same order here."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    record = await repository.get_local_actor_by_matrix_id(sender)
    if record is None:
        await _notice(request, room_id, "You don't have a linked profile to delete.")
        return

    base = config.bridge.public_base_url
    actor_id = actor_url(base, record.username)
    followers = await repository.list_followers(record.username)
    profile_room_id = record.room_id

    # 1. Tell the fediverse side first, while the actor's key is still
    # valid -- a signed Delete lets remote followers clean up their side
    # instead of endlessly retrying delivery to (or just silently keeping a
    # stale follow of) an identity that's about to 404.
    delete_activity = Activity(
        id=f"{actor_id}/deletes/{uuid.uuid4().hex}",
        type="Delete",
        actor=actor_id,
        object=actor_id,
    )
    http_client = request.app.state.http_client
    for follower_actor_id in followers:
        inbox = await resolve_actor_inbox(request, follower_actor_id)
        if inbox is None:
            continue
        try:
            await deliver_activity(
                http_client,
                inbox_url=inbox,
                activity=delete_activity.to_dict(),
                key_id=main_key_id(base, record.username),
                private_key_pem=record.private_key_pem,
            )
        except DeliveryError:
            logger.warning("Failed to deliver Delete to follower %s", follower_actor_id, exc_info=True)

    # 2. Kick from every OTHER bridge-managed room they're in (Remote User
    # Rooms for accounts they follow, ghost DM/chat rooms) -- never the
    # profile room itself (stays around, renamed below, for the notice at
    # the end to land in) or their Fediverse space (kicked separately,
    # next, since it isn't one of these room *kinds* at all).
    space_room_id = await repository.get_user_space(sender)
    for other_room_id in await _list_bridge_managed_rooms(request, sender):
        if other_room_id in (profile_room_id, space_room_id):
            continue
        try:
            await synapse.kick_user(other_room_id, sender, as_user_id=bot_mxid, reason="Fediverse profile deleted")
        except SynapseError:
            logger.warning("Could not kick %s from %s while deleting profile", sender, other_room_id, exc_info=True)

    # 3. Kick from their Fediverse space.
    if space_room_id:
        try:
            await synapse.kick_user(space_room_id, sender, as_user_id=bot_mxid, reason="Fediverse profile deleted")
        except SynapseError:
            logger.warning("Could not kick %s from their space %s", sender, space_room_id, exc_info=True)

    # 4. Unlink the profile room from the identity before actually erasing
    # the identity itself (next), so nothing else in the bridge treats it
    # as a live profile room even mid-way through this.
    await repository.register_local_actor(dataclasses.replace(record, room_id=""))

    # 5. Rename the room so it's visually obvious at a glance it's defunct
    # -- same convention as _mark_room_replaced for a replaced room.
    if profile_room_id:
        try:
            room_state = await synapse.get_room_state(profile_room_id, "m.room.name", as_user_id=bot_mxid)
            current_name = room_state.get("name")
        except SynapseError:
            current_name = None
        if current_name and not current_name.endswith(" (Deleted)"):
            try:
                await synapse.send_state_event(
                    profile_room_id, "m.room.name", "", {"name": f"{current_name} (Deleted)"}, as_user_id=bot_mxid,
                )
            except SynapseError:
                logger.info("Could not rename deleted profile room %s", profile_room_id, exc_info=True)

    # 6. Actually erase the identity: keys, followers, following, all of it.
    await repository.unregister_local_actor(record.username)

    # 7. Let them know. A roomless identity (a third-party Follow-Only
    # actor, or -- in principle -- our own service actor, though that one
    # is never reachable via this command) has no profile_room_id to post
    # the notice into -- fall back to wherever "confirm" was actually sent
    # (this function's own room_id parameter), which is guaranteed to be
    # a room both the bot and sender are already in. Otherwise, same as
    # before: post in the profile room itself, still theirs to leave
    # whenever they like, just no longer tied to anything on the fediverse.
    if profile_room_id:
        deletion_notice_content: dict = {
            "msgtype": "m.text",
            "body": f"Your fediverse identity ({record.username}@{config.bridge.domain}) has been "
            "deleted, and this room is no longer associated with ActivityPub. You can safely leave "
            "it whenever you like.",
        }
        notice_room_id = profile_room_id
    else:
        deletion_notice_content = {
            "msgtype": "m.text",
            "body": f"Your fediverse identity ({record.username}@{config.bridge.domain}) has been deleted.",
        }
        notice_room_id = room_id
    relates_to = _command_relates_to_var.get()
    if relates_to:
        deletion_notice_content["m.relates_to"] = relates_to
    if notice_room_id:
        try:
            await synapse.send_message_event(notice_room_id, deletion_notice_content, as_user_id=bot_mxid)
        except SynapseError:
            logger.info("Could not send deletion notice to %s", notice_room_id, exc_info=True)


async def _list_unfollowed_ghost_rooms(
    request: Request, *, sender: str, username: str
) -> list[tuple[str, str, str]]:
    """Every Remote User Room ``sender`` currently belongs to that they do
    NOT (or no longer) follow -- ``(room_id, actor_id, handle)`` tuples.
    Membership (``_list_bridge_managed_rooms``, shared with
    ``_execute_delete_profile``) and following (``ActorRepository.is_following``)
    are two genuinely independent things in this bridge: a room invite can
    outlive an unfollow (there's no code path that kicks someone out just
    because they unfollowed), and various on-demand imports (a mention, a
    reply's ancestor chain, someone else's repost) can land a user in a
    Remote User Room's membership without them ever having run ``;follow``
    there at all. Best-effort against listing itself failing; a room this
    can't even enumerate just doesn't show up rather than erroring the
    whole command out."""
    repository = request.app.state.repository
    unfollowed: list[tuple[str, str, str]] = []
    for candidate_room_id in await _list_bridge_managed_rooms(request, sender):
        remote_room = await repository.get_remote_actor_room_by_room_id(candidate_room_id)
        if remote_room is None:
            continue  # not a Remote User Room at all (a DM/Chat room)
        if await repository.is_following(username, remote_room.actor_id):
            continue
        handle = remote_room.display_name or remote_room.actor_id
        unfollowed.append((candidate_room_id, remote_room.actor_id, handle))
    return unfollowed


_LEAVE_UNFOLLOWED_WARNING_MARKER = "This will remove you from every Remote User Room you don't follow"
_LEAVE_UNFOLLOWED_LIST_LIMIT = 25


async def _handle_leave_unfollowed(request: Request, *, sender: str, room_id: str) -> None:
    """First half of a two-step, confirmation-gated cleanup: shows how many
    Remote User Rooms ``sender`` would be removed from (see
    ``_list_unfollowed_ghost_rooms``) and, only if there's at least one,
    asks them to reply "confirm" to actually go through -- nothing is left
    yet just from running this command. See
    ``maybe_handle_leave_unfollowed_confirmation`` for how that reply is
    recognized, and ``_execute_leave_unfollowed`` for what actually runs
    once confirmed."""
    repository = request.app.state.repository
    relates_to = _command_relates_to_var.get()

    record = await repository.get_local_actor_by_matrix_id(sender)
    if record is None:
        await _notice(
            request, room_id, f'Link a profile first: run "{_COMMAND_PREFIX}link profile".', relates_to=relates_to,
        )
        return

    unfollowed = await _list_unfollowed_ghost_rooms(request, sender=sender, username=record.username)
    if not unfollowed:
        await _notice(
            request, room_id,
            "You're not in any Remote User Rooms for accounts you don't follow -- nothing to leave.",
            relates_to=relates_to,
        )
        return

    shown = unfollowed[:_LEAVE_UNFOLLOWED_LIST_LIMIT]
    handle_list = "\n".join(f"- {handle}" for _room_id, _actor_id, handle in shown)
    remaining = len(unfollowed) - len(shown)
    if remaining > 0:
        handle_list += f"\n...and {remaining} more"

    warning = (
        f"⚠️ {_LEAVE_UNFOLLOWED_WARNING_MARKER} -- {len(unfollowed)} room"
        f"{'s' if len(unfollowed) != 1 else ''}:\n{handle_list}"
        '\n\nReply to THIS message with "confirm" to go ahead.'
    )
    formatted_list = "".join(f"<li>{html.escape(handle)}</li>" for _room_id, _actor_id, handle in shown)
    formatted_warning = (
        f"<p>⚠️ {html.escape(_LEAVE_UNFOLLOWED_WARNING_MARKER)} -- "
        f"<strong>{len(unfollowed)}</strong> room{'s' if len(unfollowed) != 1 else ''}:</p><ul>{formatted_list}</ul>"
        + (f"<p>...and {remaining} more</p>" if remaining > 0 else "")
        + '<p>Reply to THIS message with "confirm" to go ahead.</p>'
    )
    warning_content: dict = {
        "msgtype": "m.text",
        "body": warning,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_warning,
    }
    if relates_to:
        warning_content["m.relates_to"] = relates_to
    config = request.app.state.config
    try:
        await request.app.state.synapse.send_message_event(room_id, warning_content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send leave-unfollowed warning to %s", room_id, exc_info=True)


async def maybe_handle_leave_unfollowed_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this event was a "confirm" reply to one of our own
    ``;leave unfollowed`` warnings (handled, whether or not it actually
    matched one) -- same pattern as ``maybe_handle_delete_confirmation``
    (checked independent of ``message_addresses_bot``/the ``;`` prefix,
    since the reply is just the bare word "confirm"; deliberately stateless
    -- the replied-to event is checked for ``_LEAVE_UNFOLLOWED_WARNING_MARKER``
    instead of any DB-tracked "pending confirmation" row). Must run before
    ``maybe_federate_reply``/``maybe_distribute_profile_post`` in the
    dispatch chain, same reasoning as that function."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    if strip_reply_fallback(content.get("body") or "").strip().lower() != "confirm":
        return False
    target_event_id = _reply_target_event_id(content)
    if not target_event_id:
        return False

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not room_id or not sender:
        return False

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    try:
        target_event = await request.app.state.synapse.get_event(room_id, target_event_id, as_user_id=bot_mxid)
    except SynapseError:
        return False
    if target_event.get("sender") != bot_mxid:
        return False
    if _LEAVE_UNFOLLOWED_WARNING_MARKER not in ((target_event.get("content") or {}).get("body") or ""):
        return False

    token = _command_relates_to_var.set(_preserve_command_thread(content, event.get("event_id")))
    try:
        await _execute_leave_unfollowed(request, sender=sender, room_id=room_id)
    finally:
        _command_relates_to_var.reset(token)
    return True


async def _execute_leave_unfollowed(request: Request, *, sender: str, room_id: str) -> None:
    """The actual room-kicking, only ever reached via a confirmed
    ``maybe_handle_leave_unfollowed_confirmation``. Recomputes the
    unfollowed-room list fresh rather than trusting whatever
    ``_handle_leave_unfollowed`` found earlier -- room membership/follow
    state could have changed in between (e.g. they re-followed one before
    confirming), same reasoning as ``_execute_delete_profile`` recomputing
    from scratch rather than trusting anything from its own warning step.

    Kicks (never just a Matrix-side leave) as the bot -- the bridge has no
    puppeting rights over a real local user's own account to make them
    literally "leave" it themselves (see
    ``bridge.note_mirroring.resolve_and_invite_ghost``'s docstring for why
    that's true everywhere else in this bridge too); the bot has power in
    every Remote User Room it created (or that has invited it), so kicking
    from there is always possible without needing the room owner/creator
    to intervene."""
    repository = request.app.state.repository
    synapse = request.app.state.synapse
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)

    record = await repository.get_local_actor_by_matrix_id(sender)
    if record is None:
        await _notice(request, room_id, f'Link a profile first: run "{_COMMAND_PREFIX}link profile".')
        return

    unfollowed = await _list_unfollowed_ghost_rooms(request, sender=sender, username=record.username)
    if not unfollowed:
        await _notice(
            request, room_id, "Nothing to leave anymore -- looks like this already changed since you asked.",
        )
        return

    left = 0
    for other_room_id, _actor_id, _handle in unfollowed:
        try:
            await synapse.kick_user(other_room_id, sender, as_user_id=bot_mxid, reason="Not followed")
        except SynapseError:
            logger.warning("Could not remove %s from %s for ;leave unfollowed", sender, other_room_id, exc_info=True)
            continue
        left += 1

    await _notice(
        request, room_id,
        f"Done -- left {left} of {len(unfollowed)} room{'s' if len(unfollowed) != 1 else ''}"
        + ("." if left == len(unfollowed) else f" ({len(unfollowed) - left} failed, see the logs)."),
    )


async def _handle_replace_room(request: Request, *, sender: str, room_id: str) -> None:
    """Replace whichever bridged room this command was run in with a
    freshly-created one representing the exact same identity, bringing it
    up to date with anything the bridge has added since it was originally
    created (MSC4501 room type, m.bridge info, the bot always being
    invited, ...). Purely local -- nothing goes out over ActivityPub, since
    the identity itself isn't changing, just which Matrix room represents
    it. Only the command runner (and, for a Remote User Room, nobody else)
    is invited into the replacement; anyone else who was in the old room
    isn't automatically brought along.

    Permissioned per the room's kind: a regular user may only replace their
    own linked Profile Room; a Remote User Room (someone else's mirrored
    fediverse account) requires a Matrix server admin.
    """
    repository = request.app.state.repository

    actor_record = await repository.get_local_actor_by_room_id(room_id)
    remote_room = None if actor_record is not None else await repository.get_remote_actor_room_by_room_id(room_id)

    if actor_record is not None:
        if actor_record.matrix_user_id != sender and not await _is_matrix_admin(request, sender):
            await _notice(
                request, room_id, "Only this profile's owner (or a Matrix server admin) can replace this room."
            )
            return
        await _replace_profile_room(request, sender=sender, old_room_id=room_id, actor_record=actor_record)
        return

    if remote_room is not None:
        if not await _is_matrix_admin(request, sender):
            await _notice(
                request, room_id,
                "Replacing a room representing someone else's fediverse account requires being a "
                "Matrix server admin.",
            )
            return
        await _replace_remote_actor_room(request, sender=sender, old_room_id=room_id, remote_room=remote_room)
        return

    dm_actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
    if dm_actor_id is not None:
        owner = await repository.get_ghost_dm_room_matrix_user_id(room_id)
        if owner != sender and not await _is_matrix_admin(request, sender):
            await _notice(
                request, room_id, "Only this DM's owner (or a Matrix server admin) can replace this room."
            )
            return
        await _replace_dm_room(request, old_room_id=room_id, actor_id=dm_actor_id, matrix_user_id=owner)
        return

    chat_actor_id = await repository.get_ghost_chat_room_actor_id(room_id)
    if chat_actor_id is not None:
        owner = await repository.get_ghost_chat_room_matrix_user_id(room_id)
        if owner != sender and not await _is_matrix_admin(request, sender):
            await _notice(
                request, room_id, "Only this chat's owner (or a Matrix server admin) can replace this room."
            )
            return
        await _replace_chat_room(request, old_room_id=room_id, actor_id=chat_actor_id, matrix_user_id=owner)
        return

    notification_owner = await repository.get_bot_dm_room_owner(room_id)
    if notification_owner is not None:
        if notification_owner != sender and not await _is_matrix_admin(request, sender):
            await _notice(
                request, room_id,
                "Only this room's owner (or a Matrix server admin) can replace this Notifications room.",
            )
            return
        await _replace_notification_room(request, old_room_id=room_id, matrix_user_id=notification_owner)
        return

    await _notice(request, room_id, "This isn't a room the bridge manages -- nothing to replace.")


async def _members_to_reinvite(
    request: Request, *, old_room_id: str, as_user_id: str, already_invited: set[str], exclude_ghosts: bool
) -> list[str]:
    """Everyone from ``old_room_id`` who should follow along into a
    replacement room, beyond whoever a given ``_replace_*`` path already
    invites explicitly. A Profile Room or Remote User Room can accumulate
    ghost members (remote repliers, etc.) that have nothing to do with the
    room's own identity and get re-added automatically as activity resumes,
    so ``exclude_ghosts=True`` drops those and keeps only genuine local
    followers. A DM/Chat room's membership is just the two participants
    (plus the bot), so ``exclude_ghosts=False`` re-adds everyone unconditionally.
    """
    config = request.app.state.config
    try:
        members = await request.app.state.synapse.get_joined_members(old_room_id, as_user_id=as_user_id)
    except SynapseError:
        return []
    ghost_prefix = f"@{config.appservice.user_prefix}"
    return [
        member
        for member in members
        if member not in already_invited and not (exclude_ghosts and member.startswith(ghost_prefix))
    ]


async def _replace_profile_room(
    request: Request, *, sender: str, old_room_id: str, actor_record: ActorRecord
) -> None:
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)
    owner = actor_record.matrix_user_id

    try:
        profile = await synapse.get_profile(owner)
    except SynapseError:
        profile = {}
    display_name = profile.get("displayname") or actor_record.display_name or actor_record.username
    matrix_avatar_mxc = profile.get("avatar_url")
    icon_url = None
    if matrix_avatar_mxc:
        try:
            icon_url = media_url(config.bridge.public_base_url, matrix_avatar_mxc)
        except ValueError:
            icon_url = None
        else:
            await repository.mark_media_published(matrix_avatar_mxc)

    predecessor: dict[str, str] = {"room_id": old_room_id}
    last_event_id = await _last_event_id(request, old_room_id, as_user_id=bot_mxid)
    if last_event_id:
        predecessor["event_id"] = last_event_id

    # Bring along whichever of the old room's members are actual local
    # followers, not just the owner -- see _members_to_reinvite's docstring
    # for why ghost members are excluded here.
    extra_invitees = await _members_to_reinvite(
        request, old_room_id=old_room_id, as_user_id=bot_mxid,
        already_invited={owner, bot_mxid}, exclude_ghosts=True,
    )

    try:
        new_room_id = await synapse.create_room(
            as_user_id=bot_mxid,
            name=display_name,
            # No placeholder topic -- carry the existing bio over verbatim
            # instead (see maybe_handle_topic_change), same reasoning as
            # _handle_create_profile.
            topic=actor_record.summary or None,
            invite=[owner, *extra_invitees],
            avatar_mxc=matrix_avatar_mxc,
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            room_version=_REPLACE_ROOM_VERSION,
            predecessor=predecessor,
            join_rule=_KNOCK_JOIN_RULE,
            # owner is 99, not 100, and bot_mxid (the creator) is omitted
            # entirely -- see _handle_create_profile's identical reasoning,
            # including for the events override below.
            power_level_content_override={
                "users": {owner: 99},
                "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            },
        )
    except SynapseError as exc:
        logger.warning("Could not create replacement profile room for %s: %s", actor_record.username, exc)
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return

    await repository.register_local_actor(
        dataclasses.replace(actor_record, room_id=new_room_id, display_name=display_name, icon_url=icon_url)
    )

    base = config.bridge.public_base_url
    await _send_bridge_info(
        request, room_id=new_room_id, actor_id=actor_url(base, actor_record.username),
        display_name=display_name, avatar_mxc=matrix_avatar_mxc, as_user_id=bot_mxid,
    )
    await add_bridge_widget(request, room_id=new_room_id)
    await _set_profile_user_id(request, room_id=new_room_id, matrix_user_id=owner, as_user_id=bot_mxid)
    await add_room_to_space(request, matrix_user_id=owner, child_room_id=new_room_id)

    tombstone_body = (
        f"This room has been replaced -- {actor_record.username}@{config.bridge.domain} now "
        f"publishes from {new_room_id} instead."
    )
    await _send_tombstone(
        request, old_room_id=old_room_id, new_room_id=new_room_id, as_user_id=bot_mxid, body=tombstone_body
    )
    await _mark_room_replaced(request, old_room_id=old_room_id, as_user_id=bot_mxid)

    await _notice(
        request, old_room_id,
        f"Replaced. {actor_record.username}@{config.bridge.domain} now publishes from {new_room_id} "
        "instead -- you've been invited there. This room is no longer linked.",
        html_message=(
            f"Replaced. {actor_record.username}@{config.bridge.domain} now publishes from "
            f"{room_pill_html(new_room_id)} instead -- you've been invited there. This room is no longer linked."
        ),
    )


async def _replace_remote_actor_room(
    request: Request, *, sender: str, old_room_id: str, remote_room: RemoteActorRoom
) -> None:
    repository = request.app.state.repository
    config = request.app.state.config
    http_client = request.app.state.http_client
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    try:
        actor_doc = await fetch_actor(request, remote_room.actor_id)
    except RemoteActorFetchError:
        actor_doc = {}

    domain = urlsplit(remote_room.actor_id).hostname or ""
    username = actor_doc.get("preferredUsername") or remote_room.actor_id.rstrip("/").rsplit("/", 1)[-1]
    display_name = actor_doc.get("name") or username
    # Same fallback-to-what-we-already-had convention as inbox_url below --
    # a failed re-fetch (actor_doc == {}) must NOT be read as "this actor has
    # no icon/banner now", or a transient fetch failure during ;replace room
    # silently wipes a previously-known-good avatar from both the new room
    # and the cache, even though the ghost's own live Matrix avatar is left
    # untouched (ensure_ghost_user only ever sets, never clears, an avatar)
    # -- confirmed live 2026-07-10 as the cause of a room/ghost avatar
    # mismatch after a replace whose actor fetch happened to fail.
    icon_url = extract_icon_url(actor_doc) if actor_doc else remote_room.icon_url
    localpart = ghost_localpart(config.appservice.user_prefix, username, domain)
    mxid = ghost_mxid(config.appservice.user_prefix, username, domain, config.synapse.server_name)
    avatar_mxc = await fetch_and_upload_media(http_client, synapse, icon_url) if icon_url else None
    banner_url = extract_banner_url(actor_doc) if actor_doc else remote_room.banner_url
    banner_mxc = await fetch_and_upload_media(http_client, synapse, banner_url) if banner_url else None

    await ensure_ghost_user(
        synapse,
        server_name=config.synapse.server_name,
        localpart=localpart,
        display_name=display_name,
        avatar_mxc=avatar_mxc,
    )
    await repository.record_ghost_profile(
        GhostProfile(
            actor_id=remote_room.actor_id, display_name=display_name, icon_url=icon_url,
            mxid=mxid, handle=f"@{username}@{domain}",
        )
    )

    predecessor: dict[str, str] = {"room_id": old_room_id}
    last_event_id = await _last_event_id(request, old_room_id, as_user_id=bot_mxid)
    if last_event_id:
        predecessor["event_id"] = last_event_id

    # Bring along whichever of the old room's members are other local
    # followers of this same actor, not just whoever ran the replace -- see
    # _members_to_reinvite's docstring for why ghost members are excluded.
    extra_invitees = await _members_to_reinvite(
        request, old_room_id=old_room_id, as_user_id=bot_mxid,
        already_invited={sender, bot_mxid, mxid}, exclude_ghosts=True,
    )

    try:
        new_room_id = await synapse.create_room(
            as_user_id=mxid,
            name=display_name or f"{username}@{domain}",
            topic=actor_doc.get("summary") or f"Fediverse posts from {username}@{domain}",
            invite=[sender, bot_mxid, *extra_invitees],
            avatar_mxc=avatar_mxc,
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            room_version=_REPLACE_ROOM_VERSION,
            predecessor=predecessor,
            join_rule=_KNOCK_JOIN_RULE,
            # bot_mxid is kept at the SAME level as the ghost creator, same
            # as every non-replace Remote User Room path (see
            # _establish_remote_follow) -- under v11 that was a plain
            # `users: {bot_mxid: 100}` override matching the creator's own
            # 100 default, but v12 reserves numeric 100 for creators only,
            # so parity now requires making the bot a genuine
            # ``additional_creators`` peer instead (see
            # SynapseClient.create_room's docstring). sender/extra_invitees
            # are deliberately NOT elevated -- ordinary local users, not
            # bridge infrastructure.
            additional_creators=[bot_mxid],
            # events' SOCIAL_PROFILE_USER_ID_STATE_TYPE override matches
            # every other Remote User Room creation path's identical
            # reasoning -- independent of additional_creators above, which
            # only handles bot/ghost power parity, not this.
            power_level_content_override={
                "events": {SOCIAL_PROFILE_USER_ID_STATE_TYPE: SOCIAL_PROFILE_USER_ID_POWER_LEVEL},
            },
        )
    except SynapseError as exc:
        logger.warning("Could not create replacement room for %s: %s", remote_room.actor_id, exc)
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return

    new_remote_room = RemoteActorRoom(
        actor_id=remote_room.actor_id,
        room_id=new_room_id,
        ghost_user_id=mxid,
        inbox_url=remote_room.inbox_url or actor_doc.get("inbox") or "",
        display_name=display_name,
        icon_url=icon_url,
        banner_url=banner_url,
    )
    await repository.register_remote_actor_room(new_remote_room)
    await _send_bridge_info(
        request, room_id=new_room_id, actor_id=remote_room.actor_id,
        display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
    )
    await add_bridge_widget(request, room_id=new_room_id)
    await _set_ghost_profile_room_id(request, mxid=mxid, room_id=new_room_id)
    await set_ghost_external_handle(
        request, mxid=mxid, handle=f"@{username}@{domain}", profile_url=extract_actor_url(actor_doc)
    )
    await _set_profile_user_id(request, room_id=new_room_id, matrix_user_id=mxid, as_user_id=mxid)
    if banner_mxc:
        await _set_ghost_room_banner(request, room_id=new_room_id, ghost_user_id=mxid, banner_mxc=banner_mxc)

    tombstone_body = f"This room has been replaced -- {username}@{domain}'s posts now mirror into {new_room_id} instead."
    await _send_tombstone(
        request, old_room_id=old_room_id, new_room_id=new_room_id,
        as_user_id=remote_room.ghost_user_id, body=tombstone_body,
    )
    await _mark_room_replaced(request, old_room_id=old_room_id, as_user_id=remote_room.ghost_user_id)

    await _notice(
        request, old_room_id,
        f"Replaced. {username}@{domain}'s posts now mirror into {new_room_id} instead -- "
        "you've been invited there. This room is no longer linked.",
        html_message=(
            f"Replaced. {username}@{domain}'s posts now mirror into {room_pill_html(new_room_id)} instead -- "
            "you've been invited there. This room is no longer linked."
        ),
    )


async def _replace_dm_room(request: Request, *, old_room_id: str, actor_id: str, matrix_user_id: str) -> None:
    """The DM-room counterpart of ``_replace_remote_actor_room`` -- same
    identity (the ghost for ``actor_id`` and ``matrix_user_id``), fresh
    room. Reuses the ghost's already-synced profile (``get_ghost_profile``)
    rather than re-fetching the actor doc, since a DM room's identity is
    the (ghost, local user) pair, not a public profile that needs to be
    re-resolved."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    ghost_profile = await repository.get_ghost_profile(actor_id)
    if ghost_profile is None or not ghost_profile.mxid:
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return
    mxid = ghost_profile.mxid
    display_name = ghost_profile.display_name or ghost_profile.handle or actor_id

    try:
        profile = await synapse.get_profile(mxid)
    except SynapseError:
        profile = {}
    avatar_mxc = profile.get("avatar_url")

    predecessor: dict[str, str] = {"room_id": old_room_id}
    last_event_id = await _last_event_id(request, old_room_id, as_user_id=mxid)
    if last_event_id:
        predecessor["event_id"] = last_event_id

    # Unlike a Profile/Remote User Room, a DM room's whole membership is
    # meant to be re-added -- there's no ghost-noise to filter out, just the
    # (at most handful of) people already in a 1:1-ish conversation.
    extra_invitees = await _members_to_reinvite(
        request, old_room_id=old_room_id, as_user_id=mxid,
        already_invited={matrix_user_id, bot_mxid, mxid}, exclude_ghosts=False,
    )

    try:
        new_room_id = await synapse.create_room(
            as_user_id=mxid,
            name=f"{display_name} (DM)",
            invite=[matrix_user_id, bot_mxid, *extra_invitees],
            is_direct=True,
            avatar_mxc=avatar_mxc,
            # Deliberately "private_chat", NOT "trusted_private_chat" --
            # that preset's defining behavior is giving every invitee the
            # SAME power as the creator (which is also what broke v12
            # above: it promotes them all to implicit "additional
            # creators"). We want the opposite here: matrix_user_id and any
            # extra_invitees are ordinary local users, not bridge
            # infrastructure, and should stay at the preset's normal
            # default (0) -- only the bot gets elevated, via
            # additional_creators below. Ghost (mxid, the creator) already
            # has full control by construction, on any room version.
            preset="private_chat",
            # Knockable -- see ensure_ghost_dm_room's identical reasoning
            # (lets the intended local user let themselves back in with
            # just the room ID, e.g. via this room's own predecessor
            # pointer, without needing to run `;dm` again).
            join_rule=_KNOCK_JOIN_RULE,
            # bot_mxid is kept at the SAME level as the ghost creator, same
            # as every non-replace ghost-DM path (see ensure_ghost_dm_room)
            # -- see _replace_remote_actor_room's identical
            # additional_creators reasoning.
            additional_creators=[bot_mxid],
            room_type=_SOCIAL_PROFILE_ROOM_TYPE,
            room_version=_REPLACE_ROOM_VERSION,
            predecessor=predecessor,
        )
    except SynapseError as exc:
        logger.warning("Could not create replacement DM room for %s with %s: %s", actor_id, matrix_user_id, exc)
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return

    await repository.register_ghost_dm_room(actor_id, matrix_user_id, new_room_id)
    await _send_bridge_info(
        request, room_id=new_room_id, actor_id=actor_id,
        display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
    )
    await add_bridge_widget(request, room_id=new_room_id)

    tombstone_body = f"This room has been replaced -- your DMs with {display_name} now continue in {new_room_id} instead."
    await _send_tombstone(request, old_room_id=old_room_id, new_room_id=new_room_id, as_user_id=mxid, body=tombstone_body)
    await _mark_room_replaced(request, old_room_id=old_room_id, as_user_id=mxid)

    await _notice(
        request, old_room_id,
        f"Replaced. Your DMs with {display_name} now continue in {new_room_id} instead -- "
        "you've been invited there. This room is no longer linked.",
        html_message=(
            f"Replaced. Your DMs with {html.escape(display_name)} now continue in {room_pill_html(new_room_id)} "
            "instead -- you've been invited there. This room is no longer linked."
        ),
    )


async def _replace_chat_room(request: Request, *, old_room_id: str, actor_id: str, matrix_user_id: str) -> None:
    """The ``ChatMessage`` counterpart of ``_replace_dm_room`` -- identical
    except for the separate ``ghost_chat_rooms`` table and "(Chat)" naming
    (see ``ensure_ghost_chat_room``'s docstring for why a Chat room and a DM
    room are never the same room)."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    ghost_profile = await repository.get_ghost_profile(actor_id)
    if ghost_profile is None or not ghost_profile.mxid:
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return
    mxid = ghost_profile.mxid
    display_name = ghost_profile.display_name or ghost_profile.handle or actor_id

    try:
        profile = await synapse.get_profile(mxid)
    except SynapseError:
        profile = {}
    avatar_mxc = profile.get("avatar_url")

    predecessor: dict[str, str] = {"room_id": old_room_id}
    last_event_id = await _last_event_id(request, old_room_id, as_user_id=mxid)
    if last_event_id:
        predecessor["event_id"] = last_event_id

    try:
        new_room_id = await synapse.create_room(
            as_user_id=mxid,
            name=f"{display_name} (Chat)",
            invite=[matrix_user_id, bot_mxid],
            is_direct=True,
            avatar_mxc=avatar_mxc,
            preset="trusted_private_chat",
            # Knockable -- see ensure_ghost_dm_room's identical reasoning.
            join_rule=_KNOCK_JOIN_RULE,
            # bot_mxid kept at the same level as the ghost creator -- see
            # _establish_remote_follow's identical reasoning. Also forces
            # room_version, matching _replace_dm_room's identical
            # reasoning -- this path had simply been missed when the other
            # replace paths were brought up to explicit v12 (2026-07-07).
            additional_creators=[bot_mxid],
            room_version=_REPLACE_ROOM_VERSION,
            predecessor=predecessor,
        )
    except SynapseError as exc:
        logger.warning("Could not create replacement chat room for %s with %s: %s", actor_id, matrix_user_id, exc)
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return

    await repository.register_ghost_chat_room(actor_id, matrix_user_id, new_room_id)
    await _send_bridge_info(
        request, room_id=new_room_id, actor_id=actor_id,
        display_name=display_name, avatar_mxc=avatar_mxc, as_user_id=mxid,
    )
    await add_bridge_widget(request, room_id=new_room_id)

    tombstone_body = (
        f"This room has been replaced -- your chat with {display_name} now continues in {new_room_id} instead."
    )
    await _send_tombstone(request, old_room_id=old_room_id, new_room_id=new_room_id, as_user_id=mxid, body=tombstone_body)
    await _mark_room_replaced(request, old_room_id=old_room_id, as_user_id=mxid)

    await _notice(
        request, old_room_id,
        f"Replaced. Your chat with {display_name} now continues in {new_room_id} instead -- "
        "you've been invited there. This room is no longer linked.",
        html_message=(
            f"Replaced. Your chat with {html.escape(display_name)} now continues in {room_pill_html(new_room_id)} "
            "instead -- you've been invited there. This room is no longer linked."
        ),
    )


async def _replace_notification_room(request: Request, *, old_room_id: str, matrix_user_id: str) -> None:
    """The Notifications-room counterpart of ``_replace_dm_room`` --
    bot-created rather than ghost-created, and untagged with any bridge-info
    (``ensure_bot_dm_room`` never sets any either -- it doesn't represent a
    fediverse actor, just the bot itself)."""
    repository = request.app.state.repository
    config = request.app.state.config
    synapse = request.app.state.synapse
    bot_mxid = _bot_mxid(config)

    predecessor: dict[str, str] = {"room_id": old_room_id}
    last_event_id = await _last_event_id(request, old_room_id, as_user_id=bot_mxid)
    if last_event_id:
        predecessor["event_id"] = last_event_id

    try:
        new_room_id = await synapse.create_room(
            as_user_id=bot_mxid,
            name=_NOTIFICATIONS_ROOM_NAME,
            invite=[matrix_user_id],
            is_direct=True,
            preset="trusted_private_chat",
            avatar_mxc=config.appservice.bot_avatar_mxc,
            predecessor=predecessor,
        )
    except SynapseError as exc:
        logger.warning("Could not create replacement notifications room for %s: %s", matrix_user_id, exc)
        await _notice(request, old_room_id, "Could not create a replacement room -- please try again.")
        return

    await repository.register_bot_dm_room(matrix_user_id, new_room_id)
    await add_bridge_widget(request, room_id=new_room_id)

    tombstone_body = f"This room has been replaced -- your Fediverse Notifications now continue in {new_room_id} instead."
    await _send_tombstone(
        request, old_room_id=old_room_id, new_room_id=new_room_id, as_user_id=bot_mxid, body=tombstone_body,
    )
    await _mark_room_replaced(request, old_room_id=old_room_id, as_user_id=bot_mxid)

    await _notice(
        request, old_room_id,
        f"Replaced. Your Fediverse Notifications now continue in {new_room_id} instead -- "
        "you've been invited there. This room is no longer linked.",
        html_message=(
            f"Replaced. Your Fediverse Notifications now continue in {room_pill_html(new_room_id)} instead -- "
            "you've been invited there. This room is no longer linked."
        ),
    )


# No `:\S+` domain suffix required (unlike _MXID_RE below, whose shape is
# unaffected) -- room v12 room IDs are a hash of the create event with no
# server-name component at all, so requiring one here would reject every
# legitimate v12 room ID passed to `;rejoin`.
_ROOM_ID_RE = re.compile(r"^!\S+$")
_MXID_RE = re.compile(r"^@\S+:\S+$")


async def _handle_rejoin(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Force-attempt an invite into a room the bridge manages, for
    recovering from lockouts -- e.g. someone locks themselves out of their
    own Profile Room by setting its join rule to knocking with nobody left
    who can approve the knock.

    Self-service (inviting only yourself) is allowed into any Remote User
    Room (mirroring a fediverse account) -- those aren't "owned" by any one
    person, anyone here can join one, INCLUDING an already-replaced one no
    longer tracked by ``remote_actor_rooms`` (see
    ``resolve_old_remote_actor_room``) -- or a room that either currently is,
    or (per ``ActorRepository.get_profile_room_owner``, which survives
    ``replace room``) EVER WAS, your own linked Profile Room. Inviting
    anyone other than yourself requires being a Matrix server admin, same
    bar as replacing someone else's Remote User Room; an admin can also
    target any room this way, not just ones the bridge recognizes, since
    the whole point is a manual escape hatch of last resort.

    Deliberately does NOT also establish an AP Follow the way it briefly did
    -- ActivityPub following only ever happens via the explicit `follow`
    command now, precisely so getting into a room (this way, or by knocking,
    or by clicking a matrix.to link to a reposted post someone else's room
    mirrors) never has the side effect of following someone you never
    actually asked to follow.
    """
    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    repository = request.app.state.repository
    synapse = request.app.state.synapse

    parts = argument.split()
    target_room_id = parts[0] if parts else ""
    target_mxid = parts[1] if len(parts) > 1 else sender

    if not _ROOM_ID_RE.match(target_room_id) or (len(parts) > 1 and not _MXID_RE.match(target_mxid)):
        await _notice(request, room_id, f"Usage: {_COMMAND_PREFIX}rejoin <room_id> [@other:matrix.id]")
        return

    is_admin = await _is_matrix_admin(request, sender)
    if target_mxid != sender and not is_admin:
        await _notice(request, room_id, "Only a Matrix server admin can invite someone other than themselves.")
        return

    remote_room = await _resolve_old_remote_actor_room(request, target_room_id)
    profile_owner_mxid = None if remote_room is not None else await repository.get_profile_room_owner(target_room_id)

    if not is_admin and remote_room is None and profile_owner_mxid != sender:
        await _notice(
            request, room_id,
            "You can only rejoin a fediverse account's room, or your own linked Profile Room "
            "(current or past) -- a Matrix server admin can help with anything else.",
        )
        return

    as_user_id = remote_room.ghost_user_id if remote_room is not None else bot_mxid
    try:
        await synapse.invite_user(target_room_id, target_mxid, as_user_id=as_user_id)
    except SynapseError as exc:
        await _notice(request, room_id, f"Could not invite {target_mxid} to {target_room_id}: {exc}")
        return
    await _notice(
        request, room_id, f"Invited {target_mxid} to {target_room_id}.",
        html_message=f"Invited {html.escape(target_mxid)} to {room_pill_html(target_room_id)}.",
    )


# Matrix server_name shape: a domain (optionally with a port) -- deliberately
# permissive rather than a strict RFC952/1123 hostname grammar, same spirit
# as _ROOM_ID_RE/_MXID_RE above not fully validating Matrix ID grammar
# either. Just needs to reject obvious garbage (spaces, a stray "@"/"!"/
# extra ":") before it's stored as an allowlist value.
_HOMESERVER_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-]+(?::\d+)?$")

_ALLOW_RULE_TYPES = ("mxid", "room", "homeserver")

_ALLOW_USAGE = (
    f"Usage: {_COMMAND_PREFIX}allow mxid @user:example.org | "
    f"{_COMMAND_PREFIX}allow room !roomid:example.org | "
    f"{_COMMAND_PREFIX}allow homeserver example.org"
)
_DISALLOW_USAGE = (
    f"Usage: {_COMMAND_PREFIX}disallow mxid @user:example.org | "
    f"{_COMMAND_PREFIX}disallow room !roomid:example.org | "
    f"{_COMMAND_PREFIX}disallow homeserver example.org"
)

# Whitelisting an entire homeserver means trusting every account on a server
# this bridge's admin doesn't control -- confirmation-gated the same
# stateless, marker-based way as ;delete profile/;leave unfollowed (see
# maybe_handle_delete_confirmation's own docstring for why stateless: no DB
# row for "a grant is pending", just this substring checked on the replied-to
# event). mxid/room grants are immediate -- narrow enough in scope not to
# warrant the extra step.
_ALLOW_HOMESERVER_WARNING_MARKER = "This will let EVERY user on"
_ALLOW_HOMESERVER_DOMAIN_FROM_MARKER_RE = re.compile(
    re.escape(_ALLOW_HOMESERVER_WARNING_MARKER) + r" (\S+) use this bridge"
)


async def _handle_allow(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Admin-only: grant third-party access to an exact MXID, a room's
    membership, or a whole homeserver -- see this feature's design notes
    (``ActorRecord.is_third_party``, ``_effective_third_party_mode``) for
    what that access actually means, which is controlled once, globally,
    by ``bridge.third_party_access_mode`` -- not per-grant."""
    if not await _is_matrix_admin(request, sender):
        await _notice(request, room_id, "Only a Matrix server admin can manage third-party access.")
        return

    parts = argument.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() not in _ALLOW_RULE_TYPES:
        await _notice(request, room_id, _ALLOW_USAGE)
        return
    rule_type, value = parts[0].lower(), parts[1].strip()
    repository = request.app.state.repository

    if rule_type == "mxid":
        if not _MXID_RE.match(value):
            await _notice(request, room_id, f"{value} doesn't look like a valid MXID (e.g. @user:example.org).")
            return
        await repository.add_third_party_allow("mxid", value, granted_by=sender)
        await _notice(request, room_id, f"Allowed {value} to use this bridge.")
        return

    if rule_type == "room":
        if not _ROOM_ID_RE.match(value):
            await _notice(request, room_id, f"{value} doesn't look like a valid room ID (e.g. !roomid:example.org).")
            return
        # A room grant only does anything once the bot is actually present
        # to receive events from it (the allowlist check at the command
        # dispatch gate, and every outbound-federation re-validation point,
        # both work purely off "did this event arrive from an allowlisted
        # room" -- there's no separate live membership query). Doesn't block
        # the grant either way, just informs -- the bot might be invited
        # into it moments later.
        # The bot's OWN rooms -- unlike an arbitrary real human's, this
        # never needed the Admin API at all: the bot is within this
        # bridge's own registered AS namespace, so a plain C-S
        # joined_rooms call (impersonating it) already has full access.
        bot_mxid = _bot_mxid(request.app.state.config)
        try:
            bot_rooms = await request.app.state.synapse.get_joined_rooms(bot_mxid)
        except SynapseError:
            bot_rooms = []
        await repository.add_third_party_allow("room", value, granted_by=sender)
        note = (
            "" if value in bot_rooms
            else " Note: the bot isn't currently in this room, so this won't take effect until it is."
        )
        await _notice(request, room_id, f"Allowed members of {value} to use this bridge.{note}")
        return

    # homeserver -- confirmation-gated, see _ALLOW_HOMESERVER_WARNING_MARKER.
    if not _HOMESERVER_DOMAIN_RE.match(value):
        await _notice(request, room_id, f"{value} doesn't look like a valid homeserver domain.")
        return
    mode = request.app.state.config.bridge.third_party_access_mode
    warning = (
        f"⚠️ {_ALLOW_HOMESERVER_WARNING_MARKER} {value} use this bridge, in whatever mode is "
        f'currently configured (currently "{mode}") -- that\'s every account on a server you '
        "don't control, not just people you already trust. This can be undone later with "
        f'"{_COMMAND_PREFIX}disallow homeserver {value}" (though any identity already '
        f'provisioned under it stays around until deleted -- see "{_COMMAND_PREFIX}delete profile").\n\n'
        'Reply to THIS message with "confirm" to go ahead.'
    )
    formatted_warning = (
        f"<p>⚠️ {html.escape(_ALLOW_HOMESERVER_WARNING_MARKER)} <strong>{html.escape(value)}</strong> "
        f'use this bridge, in whatever mode is currently configured (currently "{html.escape(mode)}") -- '
        "that's every account on a server you don't control, not just people you already trust. "
        f"This can be undone later with \"{_COMMAND_PREFIX}disallow homeserver {html.escape(value)}\" "
        "(though any identity already provisioned under it stays around until deleted).</p>"
        '<p>Reply to THIS message with "confirm" to go ahead.</p>'
    )
    warning_content: dict = {
        "msgtype": "m.text",
        "body": warning,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_warning,
    }
    relates_to = _command_relates_to_var.get()
    if relates_to:
        warning_content["m.relates_to"] = relates_to
    config = request.app.state.config
    try:
        await request.app.state.synapse.send_message_event(room_id, warning_content, as_user_id=_bot_mxid(config))
    except SynapseError:
        logger.warning("Failed to send allow-homeserver warning to %s", room_id, exc_info=True)


async def maybe_handle_allow_homeserver_confirmation(request: Request, event: dict) -> bool:
    """Returns True if this event was a "confirm" reply to one of our own
    ``;allow homeserver`` warnings (handled, whether or not it actually
    matched one) -- same stateless pattern as ``maybe_handle_delete_confirmation``,
    with one addition: unlike a delete/leave-unfollowed confirmation (which
    only ever act on the CONFIRMER's own identity, so it's safe regardless of
    who sends "confirm"), granting a whole homeserver access is a
    bridge-wide admin action -- so the confirmer's OWN admin status is
    re-checked here too, not just whoever originally ran ``;allow
    homeserver``. Otherwise any non-admin in the same room could confirm
    someone else's pending admin action just by replying "confirm" to it."""
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content") or {}
    if strip_reply_fallback(content.get("body") or "").strip().lower() != "confirm":
        return False
    target_event_id = _reply_target_event_id(content)
    if not target_event_id:
        return False

    room_id = event.get("room_id", "")
    sender = event.get("sender", "")
    if not room_id or not sender:
        return False

    config = request.app.state.config
    bot_mxid = _bot_mxid(config)
    try:
        target_event = await request.app.state.synapse.get_event(room_id, target_event_id, as_user_id=bot_mxid)
    except SynapseError:
        return False
    if target_event.get("sender") != bot_mxid:
        return False
    warning_body = (target_event.get("content") or {}).get("body") or ""
    if _ALLOW_HOMESERVER_WARNING_MARKER not in warning_body:
        return False
    domain_match = _ALLOW_HOMESERVER_DOMAIN_FROM_MARKER_RE.search(warning_body)
    if domain_match is None:
        return False
    homeserver = domain_match.group(1)

    token = _command_relates_to_var.set(_preserve_command_thread(content, event.get("event_id")))
    try:
        if not await _is_matrix_admin(request, sender):
            await _notice(request, room_id, "Only a Matrix server admin can confirm this.")
            return True
        repository = request.app.state.repository
        await repository.add_third_party_allow("homeserver", homeserver, granted_by=sender)
        await _notice(request, room_id, f"Allowed every user on {homeserver} to use this bridge.")
    finally:
        _command_relates_to_var.reset(token)
    return True


async def _handle_disallow(request: Request, *, sender: str, room_id: str, argument: str) -> None:
    """Admin-only: revoke a third-party access grant. Immediate for all
    three rule types, no confirmation needed -- unlike granting, revoking
    can only narrow access. Never tears down any identity already
    provisioned under the grant (see this feature's design notes) -- it
    just stops being reachable/authoritative going forward."""
    if not await _is_matrix_admin(request, sender):
        await _notice(request, room_id, "Only a Matrix server admin can manage third-party access.")
        return

    parts = argument.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() not in _ALLOW_RULE_TYPES:
        await _notice(request, room_id, _DISALLOW_USAGE)
        return
    rule_type, value = parts[0].lower(), parts[1].strip()
    repository = request.app.state.repository
    await repository.remove_third_party_allow(rule_type, value)
    await _notice(
        request, room_id,
        f"Removed the {rule_type} grant for {value}. Any identity already provisioned under it stays "
        f'around until deleted (see "{_COMMAND_PREFIX}allowed" to see current grants).',
    )


async def _handle_allowed(request: Request, *, sender: str, room_id: str) -> None:
    """Admin-only: list every current third-party access grant, grouped by
    rule type, plus the current global mode they'd all get."""
    if not await _is_matrix_admin(request, sender):
        await _notice(request, room_id, "Only a Matrix server admin can view third-party access grants.")
        return

    repository = request.app.state.repository
    grants = await repository.list_third_party_allows()
    mode = request.app.state.config.bridge.third_party_access_mode
    if not grants:
        await _notice(request, room_id, f'No third-party access grants (mode: "{mode}") -- everyone stays local-only.')
        return

    lines = [f'Third-party access mode: "{mode}".', ""]
    for rule_type in _ALLOW_RULE_TYPES:
        matching = [grant for grant in grants if grant.rule_type == rule_type]
        if not matching:
            continue
        lines.append(f"{rule_type}:")
        lines.extend(f"- {grant.value} (by {grant.granted_by})" for grant in matching)
    await _notice(request, room_id, "\n".join(lines))
