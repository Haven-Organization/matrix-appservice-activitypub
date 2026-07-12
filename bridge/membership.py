"""Auto-accepts invites extended to AppService-controlled users, and reacts
to a human leaving (or being kicked from) a Remote User Room by unfollowing.

Without the invite-accept half, the bot/ghosts are invited into rooms but
never join them -- and Synapse only pushes AppService transactions for rooms
where at least one of our namespace's users is actually a member, so tagging
the bot to link a profile, outbox history reads, and everything else would
silently never receive events for a room the user thinks they've connected.

Also warns when the bot specifically is invited into an already-encrypted
room: the bridge can't decrypt ``m.room.encrypted`` events (no Olm/Megolm
support), so tagging the bot there would otherwise just silently go
unanswered -- e.g. Element creates DMs encrypted by default, which is
exactly the room shape most users will instinctively use to talk to the bot.

A ghost invited into a room it has no existing tracked relationship with at
all (not its own Remote User Room, not a Profile Room it's being invited
into for reply-mirroring, not an existing DM/chat) is treated as a fresh
Matrix-native DM started directly with it -- one of the two ways to start
an ActivityPub "Chat" (see ``bridge.chat_bridge``), the other being
``bridge.commands``'s ``chat`` command. Only actually joins (and registers
the room as a chat) if the inviter has a linked profile; otherwise there'd
be no signing identity to ever chat back with, so it's left ignored rather
than joining a room that could never actually be used.

A Remote User Room can be shared by several local users following the same
account, but each follows under their *own* linked actor (not some shared
bridge identity) -- so leaving/being kicked sends a signed ``Undo(Follow)``
and drops the following relationship for that specific user only, leaving
anyone else still in the room untouched. The same path handles both a
voluntary leave and ``bridge.commands``'s ``unfollow`` command, which just
kicks the sender and lets this react to the resulting membership event,
rather than duplicating the Undo logic.

Also keeps each human's personal "Fediverse" space (``bridge.spaces``) in
sync with which bridge-managed rooms they're actually in: joining one (their
own linked Profile Room -- current or a past one they've since moved on
from, per ``get_profile_room_owner`` -- or a Remote User Room) adds it as a
space child; leaving a Remote User Room removes it again (a Profile Room,
current or past, never gets removed even if they leave it -- see
``maybe_handle_leave``).

Every fediverse-bridged room the bot creates uses the knock join rule (see
``bridge.note_mirroring.KNOCK_JOIN_RULE``) rather than invite-only,
specifically so someone locked out of one has a way back in without needing
an admin (``rejoin`` still exists for that too). ``maybe_handle_knock`` is
the other half of that: auto-accepting a knock using the exact same
self-service rules as ``rejoin`` -- their own Profile Room (current or
past) or any Remote User Room, since a knock is inherently the knocker
acting for themselves, never on someone else's behalf, so there's no
admin-override case to mirror here. Like ``rejoin``, accepting a knock
deliberately does NOT also follow the account on the fediverse -- getting
into a room (by any door: knocking, ``rejoin``, or just clicking a
matrix.to link into a Remote User Room someone else's repost mirrored into)
should never have the side effect of following someone; only the explicit
``follow`` command does that.

``maybe_handle_join`` also sends two kinds of one-off orientation notices,
on top of its space-membership bookkeeping: joining a Remote User Room
without (yet) following that account -- exactly the situation the previous
paragraph's "no side-effect following" design permits -- gets a reminder
that ``follow`` is still a separate, explicit step; joining your own
CURRENT linked Profile Room (not a past one) gets a short welcome
explaining what the room does and pointing at your Fediverse space.
"""

from __future__ import annotations

import asyncio
import html
import logging
from urllib.parse import urlsplit

from fastapi import Request

from bridge.commands import _COMMAND_PREFIX, _RUNNING_BACKFILLS, _run_auto_backfill
from bridge.matrix_links import matrix_to_room_link
from bridge.note_mirroring import resolve_old_ghost_room_owner, resolve_old_remote_actor_room, unfollow_remote_actor
from bridge.notifications import notification_actor_html
from bridge.repository import RemoteActorRoom
from bridge.spaces import add_room_to_space, remove_room_from_space
from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)


def _encrypted_room_notice(bot_mxid: str) -> str:
    return (
        "This room is end-to-end encrypted, and I can't read encrypted messages -- "
        f"tagging me won't work here. Please message me ({bot_mxid}) in a new, unencrypted room "
        "instead (in Element: turn off the encryption toggle before sending the invite -- once a "
        "room is encrypted it can't be switched back)."
    )


async def maybe_accept_invite(request: Request, event: dict) -> bool:
    """Returns True if this event was a membership invite to one of our users (handled)."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    if content.get("membership") != "invite":
        return False

    invited_user = event.get("state_key", "")
    room_id = event.get("room_id", "")
    if not invited_user or not room_id:
        return False

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    is_ghost = invited_user.startswith(f"@{config.appservice.user_prefix}")
    is_ours = invited_user == bot_mxid or is_ghost
    if not is_ours:
        return False

    if is_ghost:
        # A ghost invited into a room we don't already have SOME tracked
        # relationship with (its own Remote User Room, a local user's
        # Profile Room it's being invited into for reply-mirroring, an
        # existing DM, an existing chat) is a prospective NEW ActivityPub
        # "Chat" (see bridge.chat_bridge) -- a plain Matrix-native DM
        # invite sent directly to a ghost's own mxid is one of the two ways
        # to start one (the other being the `chat` bot command, which
        # registers the room itself before ever inviting the ghost, so
        # it's already "known" by the time this invite arrives and skips
        # this branch entirely). Gated on the inviter actually being a
        # genuine local bridge user with a linked profile -- otherwise
        # there'd be no signing identity to ever send a chat message back
        # with, so silently joining would just leave the ghost sitting in
        # a room it can never actually be used from.
        repository = request.app.state.repository
        already_known = (
            await repository.get_remote_actor_room_by_room_id(room_id) is not None
            or await repository.get_local_actor_by_room_id(room_id) is not None
            or await repository.is_ghost_dm_room(room_id)
            or await repository.is_ghost_chat_room(room_id)
        )
        if not already_known:
            inviter = event.get("sender", "")
            actor_record = await repository.get_local_actor_by_matrix_id(inviter)
            if actor_record is None:
                logger.info(
                    "Ignoring invite for ghost %s into %s from %s -- no linked profile",
                    invited_user, room_id, inviter,
                )
                return True  # handled -- deliberately not joining
            ghost_profile = await repository.get_ghost_profile_by_mxid(invited_user)
            if ghost_profile is not None:
                await repository.register_ghost_chat_room(ghost_profile.actor_id, inviter, room_id)

    try:
        await request.app.state.synapse.join_room(room_id, as_user_id=invited_user)
    except SynapseError:
        logger.warning("Failed to accept invite for %s into %s", invited_user, room_id, exc_info=True)
        return True

    if invited_user == bot_mxid:
        is_encrypted = await _warn_if_encrypted(request, room_id, bot_mxid)
        inviter = event.get("sender", "")
        if not is_encrypted and _is_fresh_dm_invite(config, content, invited_user=bot_mxid, inviter=inviter):
            await _welcome_dm_invite(request, room_id=room_id, inviter=inviter)
    return True


def _is_fresh_dm_invite(config, content: dict, *, invited_user: str, inviter: str) -> bool:
    """Whether this invite for ``invited_user`` looks like a real Matrix
    user on this homeserver starting a fresh 1:1 DM directly with them --
    as opposed to a room the bridge's OWN code created and invited them
    into itself (a Profile Room/Remote User Room, whose ``invite=[...,
    bot_mxid]`` also generates a genuine invite event for the bot, sent by
    a ghost or local actor, both of which are equally homeserver-local
    mxids and so wouldn't otherwise be told apart from a real human's).
    ``is_direct`` -- set on the invite's own membership content by every
    mainstream client (Element included) when a room is created as a DM --
    already rules out the bridge's own room-creation calls, which never set
    it; checking the inviter isn't one of our own ghosts/the bot itself
    (who could in principle send an invite with is_direct set, e.g. via a
    client acting AS a ghost, which never legitimately happens) is on top
    of that as a belt-and-suspenders check."""
    if content.get("is_direct") is not True:
        return False
    if ":" not in inviter:
        return False
    if inviter == invited_user or inviter.startswith(f"@{config.appservice.user_prefix}"):
        return False
    return inviter.split(":", 1)[1] == config.synapse.server_name


async def _welcome_dm_invite(request: Request, *, room_id: str, inviter: str) -> None:
    """First message the bot sends into a fresh Matrix-native DM someone on
    this homeserver starts with it directly -- introduces itself and what
    to do next, since otherwise joining leaves the room looking silent
    until they already know to tag it with a command."""
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(inviter)

    intro = (
        "Hi, I'm the fediverse bridge! I connect this homeserver to the fediverse (Mastodon, "
        "Pleroma, Akkoma, Misskey, and more), so you can post, follow, and interact with fediverse "
        "accounts right from Matrix."
    )
    if actor_record is None:
        next_steps = (
            f'You don\'t have a fediverse profile yet -- use "{_COMMAND_PREFIX}create profile" to set one up '
            "and get started."
        )
        next_steps_html = (
            f"<p>You don't have a fediverse profile yet -- use <code>{html.escape(_COMMAND_PREFIX)}create "
            "profile</code> to set one up and get started.</p>"
        )
    elif not actor_record.room_id:
        # A previously `unlink profile`d identity -- the actor record (and
        # so its fediverse identity/followers/following/keys) is still
        # very much alive, just with no Matrix room currently attached to
        # it (matrix_to_room_link on an empty room_id would otherwise
        # produce a broken matrix.to link here).
        next_steps = (
            "You already have a fediverse profile, but don't have a Matrix room linked to it -- use "
            f'"{_COMMAND_PREFIX}create profile" to make a new one (this reuses your existing fediverse '
            "identity, followers and all -- it won't create a second one)."
        )
        next_steps_html = (
            "<p>You already have a fediverse profile, but don't have a Matrix room linked to it -- use "
            f"<code>{html.escape(_COMMAND_PREFIX)}create profile</code> to make a new one (this reuses your "
            "existing fediverse identity, followers and all -- it won't create a second one).</p>"
        )
    else:
        profile_link = matrix_to_room_link(actor_record.room_id)
        next_steps = (
            "You already have a fediverse profile. Here's what you can do next:\n"
            f"- Post in your profile room ({profile_link}) to publish to the fediverse.\n"
            f'- Use "{_COMMAND_PREFIX}follow @user@instance.org" to follow people.\n'
            f'- Use "{_COMMAND_PREFIX}import <url>" to pull in a post to interact with without following.\n'
            f'- Use "{_COMMAND_PREFIX}help" for more.'
        )
        # "profile room" has to sit OUTSIDE the <a> -- a matrix.to room link
        # like this renders as a pill in most clients (Element included),
        # which overrides whatever text is inside the tag with the target
        # room's own name, same as bridge.membership._welcome_to_profile_room's
        # identical "Fediverse space" link -- unlike that one, there's no
        # single fixed name to put inside that would always match (a
        # profile room's name is whatever the user chose), so it's kept
        # parenthetical instead, matching the plain-text body above.
        profile_pill_html = f'<a href="{html.escape(profile_link, quote=True)}">link</a>'
        next_steps_html = (
            "<p>You already have a fediverse profile. Here's what you can do next:</p><ul>"
            f"<li>Post in your profile room ({profile_pill_html}) to publish to the fediverse.</li>"
            f"<li>Use <code>{html.escape(_COMMAND_PREFIX)}follow @user@instance.org</code> to follow people.</li>"
            f"<li>Use <code>{html.escape(_COMMAND_PREFIX)}import &lt;url&gt;</code> to pull in a post to "
            "interact with without following.</li>"
            f"<li>Use <code>{html.escape(_COMMAND_PREFIX)}help</code> for more.</li>"
            "</ul>"
        )

    body = f"{intro}\n\n{next_steps}"
    formatted_body = f"<p>{html.escape(intro)}</p>{next_steps_html}"
    try:
        await request.app.state.synapse.send_message_event(
            room_id,
            {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html", "formatted_body": formatted_body},
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.info("Could not send DM-invite welcome to %s", room_id, exc_info=True)


async def _warn_if_encrypted(request: Request, room_id: str, bot_mxid: str) -> bool:
    """Returns whether ``room_id`` turned out to be encrypted (and, if so,
    warns into it) -- so a caller can skip ALSO sending something else that
    would just be another plaintext event into the same encrypted room."""
    synapse = request.app.state.synapse
    try:
        await synapse.get_room_state(room_id, "m.room.encryption", as_user_id=bot_mxid)
    except SynapseError as exc:
        if exc.errcode != "M_NOT_FOUND":
            logger.debug("Could not check encryption state for %s: %s", room_id, exc)
        return False  # no m.room.encryption state event -> room is unencrypted

    # The state event exists, so the room is encrypted. Room *state* (unlike
    # message content) is never encrypted, so we could see this even though
    # we can't read any messages sent in the room -- but our own reply here
    # is a plaintext event going into a room marked encrypted, which most
    # clients (correctly) treat with suspicion; it may show with a warning
    # or not render at all depending on the client. Best effort.
    try:
        await synapse.send_message_event(
            room_id, {"msgtype": "m.notice", "body": _encrypted_room_notice(bot_mxid)}, as_user_id=bot_mxid
        )
    except SynapseError:
        logger.debug("Could not send encrypted-room notice to %s", room_id, exc_info=True)
    return True


async def _resolve_ghost_room_inviter(request: Request, room_id: str, *, knocker: str) -> str | None:
    """The DM/Chat-room counterpart of ``resolve_old_remote_actor_room`` --
    returns the ghost mxid that should do the inviting if ``room_id`` is (or
    once was) a ghost DM or Chat room whose intended local user is
    ``knocker``, else None. Tries the current DB reverse-lookup first (fast
    path for a still-current room), falling back to
    ``resolve_old_ghost_room_owner``'s state-based recovery for an old,
    already-replaced one."""
    repository = request.app.state.repository
    actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
    matrix_user_id = await repository.get_ghost_dm_room_matrix_user_id(room_id) if actor_id else None
    if actor_id is None:
        actor_id = await repository.get_ghost_chat_room_actor_id(room_id)
        matrix_user_id = await repository.get_ghost_chat_room_matrix_user_id(room_id) if actor_id else None
    if actor_id is None:
        resolved = await resolve_old_ghost_room_owner(request, room_id)
        if resolved is None:
            return None
        actor_id, matrix_user_id = resolved
    if matrix_user_id != knocker:
        return None  # this ghost room was intended for someone else
    ghost_profile = await repository.get_ghost_profile(actor_id)
    if ghost_profile is None or not ghost_profile.mxid:
        return None
    return ghost_profile.mxid


async def maybe_handle_knock(request: Request, event: dict) -> bool:
    """Returns True if this event was a human user knocking on a room the
    bridge manages (handled -- see module docstring). Accepted by inviting
    them, using the same self-service rules as the ``rejoin`` command: their
    own linked Profile Room (current or past), any Remote User Room
    (including a REPLACED one, current or past -- see
    ``resolve_old_remote_actor_room``), or their own ghost DM/Chat room with
    a given fediverse account (current or past -- see
    ``_resolve_ghost_room_inviter``/``resolve_old_ghost_room_owner``; a
    knock from anyone other than that room's intended local user is left
    untouched, since a DM/Chat room isn't a shared space like the other two
    kinds). Anything else is left untouched (no admin override here; a
    knock is always the knocker acting for themselves). Deliberately does
    NOT also establish an AP Follow -- see ``rejoin``'s identical reasoning,
    following only ever happens via the explicit ``follow`` command now."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    if content.get("membership") != "knock":
        return False

    room_id = event.get("room_id", "")
    knocker = event.get("state_key", "")
    if not room_id or not knocker:
        return False

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if knocker == bot_mxid or knocker.startswith(f"@{config.appservice.user_prefix}"):
        return False  # our own bot/ghosts never knock

    knocker_server = knocker.partition(":")[2]
    if knocker_server != config.synapse.server_name and not config.bridge.accept_federated_knocks:
        # A knocker from ANOTHER homeserver: mirrored posts are public
        # fediverse content, but who gets auto-admitted into this server's
        # rooms is this server's call -- so federated knocks are ignored
        # (not rejected; someone already inside can still invite them
        # manually) unless bridge.accept_federated_knocks opts in.
        return False

    repository = request.app.state.repository
    remote_room = await resolve_old_remote_actor_room(request, room_id)
    if remote_room is not None:
        as_user_id = remote_room.ghost_user_id
    elif await repository.get_profile_room_owner(room_id) == knocker:
        as_user_id = bot_mxid
    else:
        as_user_id = await _resolve_ghost_room_inviter(request, room_id, knocker=knocker)
        if as_user_id is None:
            return False  # not a room the bridge manages for this specific knocker

    try:
        await request.app.state.synapse.invite_user(room_id, knocker, as_user_id=as_user_id)
    except SynapseError:
        logger.warning("Could not invite knocking user %s into %s", knocker, room_id, exc_info=True)
    return True


async def maybe_handle_join(request: Request, event: dict) -> bool:
    """Returns True if this event was a human user joining a room the bridge
    manages for them -- their own linked Profile Room (current or a past one
    they've since moved on from), a Remote User Room, or a ghost DM room
    (``bridge.note_mirroring.mirror_direct_message``) -- handled by adding
    it to their personal Fediverse space (see ``bridge.spaces``), creating
    that space for them first if this is their first bridge-managed room,
    plus a one-off orientation notice for all three (see module docstring;
    a DM room's is specifically about how replying vs. not changes where a
    message goes -- see ``_welcome_to_dm_room``)."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    if content.get("membership") != "join":
        return False

    room_id = event.get("room_id", "")
    joined_user = event.get("state_key", "")
    if not room_id or not joined_user:
        return False

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if joined_user == bot_mxid or joined_user.startswith(f"@{config.appservice.user_prefix}"):
        return False  # our own bot/ghosts joining isn't meaningful here

    repository = request.app.state.repository
    remote_room = await resolve_old_remote_actor_room(request, room_id)
    is_profile_room = await repository.get_profile_room_owner(room_id) == joined_user
    dm_room_actor_id = await repository.get_ghost_dm_room_actor_id(room_id)
    if remote_room is None and not is_profile_room and dm_room_actor_id is None:
        return False  # not a room the bridge manages for this specific user

    await add_room_to_space(request, matrix_user_id=joined_user, child_room_id=room_id)

    if remote_room is not None:
        await _notify_if_not_following(request, room_id=room_id, joined_user=joined_user, remote_room=remote_room)
        if remote_room.pending_backfill:
            # Only ever True for the room's first-ever join, and only when
            # that room was created because nobody on the server had
            # followed this actor before (see
            # bridge.commands._establish_remote_follow) -- consumed here,
            # before starting the backfill itself, so a redelivered/
            # duplicate join event (or two people racing to join near-
            # simultaneously) can never trigger it twice.
            await repository.mark_backfill_pending_done(room_id)
            config = request.app.state.config
            task = asyncio.get_running_loop().create_task(
                _run_auto_backfill(
                    request, room_id=room_id, remote_room=remote_room,
                    count=config.bridge.backfill_default_count,
                )
            )
            _RUNNING_BACKFILLS.add(task)
            task.add_done_callback(_RUNNING_BACKFILLS.discard)
    elif is_profile_room:
        actor_record = await repository.get_local_actor_by_matrix_id(joined_user)
        if actor_record is not None and actor_record.room_id == room_id:
            await _welcome_to_profile_room(request, room_id=room_id, joined_user=joined_user)
    elif dm_room_actor_id is not None:
        await _welcome_to_dm_room(request, room_id=room_id, remote_actor_id=dm_room_actor_id)
    return True


async def _welcome_to_dm_room(request: Request, *, room_id: str, remote_actor_id: str) -> None:
    """First-join orientation for a ghost DM room
    (``bridge.note_mirroring.mirror_direct_message``) -- explains the one
    thing about it that isn't obvious from the room itself: a message sent
    at the room's own root (not a reply to anything already in it) starts
    a brand new, separate DM with the other party, rather than continuing
    whatever's already there (see ``bridge.reply_bridge``'s identical
    reasoning for why -- there's no thread list here the way a real client
    has, so a root-level message has to mean something on its own). Reply
    directly to a message already in the room instead to keep sending in
    that same conversation thread."""
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    repository = request.app.state.repository
    profile = await repository.get_ghost_profile(remote_actor_id)
    handle = (profile.handle if profile else None) or remote_actor_id
    actor_html = (
        notification_actor_html(mxid=profile.mxid, handle=handle, display_name=profile.display_name)
        if profile and profile.mxid else html.escape(handle)
    )

    body = (
        f"This is your direct-message room with {handle} over the fediverse.\n\n"
        "A couple of things to know:\n"
        "- A message you send here that isn't a reply starts a brand new DM thread with them.\n"
        "- To continue an existing conversation instead, reply directly to one of its messages."
    )
    formatted_body = (
        f"<p>This is your direct-message room with {actor_html} over the "
        "fediverse.</p><p>A couple of things to know:</p><ul>"
        "<li>A message you send here that isn't a reply starts a brand new DM thread "
        "with them.</li>"
        "<li>To continue an existing conversation instead, reply directly to one of its messages.</li>"
        "</ul>"
    )
    try:
        await request.app.state.synapse.send_message_event(
            room_id,
            {
                "msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
                "formatted_body": formatted_body,
            },
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.info("Could not send DM room welcome to %s", room_id, exc_info=True)


async def _notify_if_not_following(
    request: Request, *, room_id: str, joined_user: str, remote_room: RemoteActorRoom
) -> None:
    """Remind ``joined_user`` that being in this Remote User Room doesn't by
    itself mean they're following its account -- neither knocking,
    ``rejoin``, nor just landing here via a matrix.to link (e.g. from a
    reposted post someone else's room mirrored) establishes an AP Follow
    anymore (see ``bridge.commands._ensure_following``'s docstring), so
    without this they could easily assume they're following someone they
    never actually asked to. A no-op if they already are (whether from
    before, or because this join followed a `follow` command that already
    ran in the same breath). Best-effort, like every other notice here."""
    repository = request.app.state.repository
    actor_record = await repository.get_local_actor_by_matrix_id(joined_user)
    if actor_record is not None and await repository.is_following(actor_record.username, remote_room.actor_id):
        return

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    domain = urlsplit(remote_room.actor_id).hostname or ""
    username = remote_room.actor_id.rstrip("/").rsplit("/", 1)[-1]
    handle = remote_room.display_name or f"@{username}@{domain}"
    synapse = request.app.state.synapse

    # Tag the joiner by name at the front -- a Remote User Room is shared,
    # so without this, whichever of several local users happens to read
    # this notice has no way to tell it's actually addressed to them and
    # not whoever else is also in the room.
    try:
        joined_profile = await synapse.get_profile(joined_user)
    except SynapseError:
        joined_profile = {}
    joined_pill = notification_actor_html(
        mxid=joined_user, handle=joined_user, display_name=joined_profile.get("displayname"),
    )
    plain_body = (
        f"{joined_user}: You're not currently following {handle} -- use \"{_COMMAND_PREFIX}follow\" "
        "here to follow them. Until you do, their new posts "
        "may not show up in this room."
    )
    formatted_body = (
        f"{joined_pill}: You're not currently following <strong>{html.escape(handle)}</strong> -- use "
        f'"{html.escape(_COMMAND_PREFIX)}follow" here to follow them. Until you do, their new posts '
        "may not show up in this room."
    )
    try:
        await synapse.send_message_event(
            room_id,
            {
                "msgtype": "m.notice",
                "body": plain_body,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted_body,
            },
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.info("Could not send not-following notice to %s", room_id, exc_info=True)


async def _welcome_to_profile_room(request: Request, *, room_id: str, joined_user: str) -> None:
    """Welcome message for joining your own CURRENT linked Profile Room
    (never a past one -- rejoining an old, already-replaced room isn't a
    "welcome to the fediverse" moment).

    Sent as an ordinary ``m.text`` message, not ``m.notice`` -- someone's
    first message in their brand new fediverse room should stand out, not
    get muted into whatever quieter styling (or, per
    ``.m.rule.suppress_notices``, no notification at all) a client gives
    notices. A bold header plus a real HTML bulleted list, for the same
    "make it stand out and easy to scan" reason, with a plain-text
    ``-``-prefixed fallback for clients that ignore ``formatted_body``."""
    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    repository = request.app.state.repository
    space_room_id = await repository.get_user_space(joined_user)

    # Nested under its own top-level bullet -- these four are all facets of
    # the same one idea ("this room IS your profile"), not separate tips,
    # so grouping them under it reads clearer than four flat, unrelated-
    # looking bullets would.
    profile_points = [
        "Anything you post here (that isn't a bot command) goes out as a fediverse post.",
        "This room's avatar is your fediverse avatar.",
        "This room's topic is your fediverse bio.",
        'Use ";banner mxc://server/mediaid" to set your fediverse profile\'s banner image.',
    ]
    profile_points_html = [
        "Anything you post here (that isn't a bot command) goes out as a fediverse post.",
        "This room's avatar is your fediverse avatar.",
        "This room's topic is your fediverse bio.",
        "Use <code>;banner mxc://server/mediaid</code> to set your fediverse profile's banner image.",
    ]

    plain_bullets = [
        "This room IS your Fediverse profile:\n" + "\n".join(f"  - {point}" for point in profile_points),
        'Use ";follow @user@instance.org" to start following fediverse accounts.',
        "I've also invited you to a \"Fediverse Notifications\" DM -- accept it to get notified there "
        "of new followers, mentions, likes, and reposts (kept separate from this room so anyone else "
        "you've invited in here can't see them).",
    ]
    html_bullets = [
        "This room IS your Fediverse profile:<ul>"
        + "".join(f"<li>{point}</li>" for point in profile_points_html)
        + "</ul>",
        "Use <code>;follow @user@instance.org</code> to start following fediverse accounts.",
        "I've also invited you to a \"Fediverse Notifications\" DM -- accept it to get notified there "
        "of new followers, mentions, likes, and reposts (kept separate from this room so anyone else "
        "you've invited in here can't see them).",
    ]
    if space_room_id:
        link = matrix_to_room_link(space_room_id)
        plain_bullets.append(
            f"Your Fediverse space ({link}) keeps this room and every account you follow together "
            "automatically."
        )
        html_bullets.append(
            # A matrix.to room link like this renders as a "pill" in most
            # clients (Element included), which overrides whatever text is
            # inside the <a> with the target room's OWN name ("Fediverse",
            # per bridge.spaces.SPACE_NAME) -- so "space" has to sit outside
            # the tag, or it gets silently swallowed by that override.
            f'Your <a href="{html.escape(link)}">Fediverse</a> space keeps this room and every account '
            "you follow together automatically."
        )

    plain_bullets.append(f'Use "{_COMMAND_PREFIX}help" to see everything else I can do.')
    html_bullets.append(f"Use <code>{html.escape(_COMMAND_PREFIX)}help</code> to see everything else I can do.")

    body = "Welcome to the fediverse!\n" + "\n".join(f"- {bullet}" for bullet in plain_bullets)
    formatted_body = (
        "<p><strong>Welcome to the fediverse!</strong></p><ul>"
        + "".join(f"<li>{bullet}</li>" for bullet in html_bullets)
        + "</ul>"
    )
    try:
        await request.app.state.synapse.send_message_event(
            room_id,
            {
                "msgtype": "m.text",
                "body": body,
                "format": "org.matrix.custom.html",
                "formatted_body": formatted_body,
            },
            as_user_id=bot_mxid,
        )
    except SynapseError:
        logger.info("Could not send welcome notice to %s", room_id, exc_info=True)


async def maybe_handle_leave(request: Request, event: dict) -> bool:
    """Returns True if this event was a human user leaving/being kicked from a
    Remote User Room (handled -- see module docstring for the unfollow logic
    this triggers)."""
    if event.get("type") != "m.room.member":
        return False
    content = event.get("content") or {}
    if content.get("membership") != "leave":
        return False

    room_id = event.get("room_id", "")
    left_user = event.get("state_key", "")
    if not room_id or not left_user:
        return False

    config = request.app.state.config
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"
    if left_user == bot_mxid or left_user.startswith(f"@{config.appservice.user_prefix}"):
        return False  # our own bot/ghosts leaving isn't meaningful here

    repository = request.app.state.repository
    remote_room = await repository.get_remote_actor_room_by_room_id(room_id)
    if remote_room is None:
        return False  # not a Remote User Room at all

    # Space removal happens for ANY human leaving a Remote User Room,
    # independent of whether they even had a linked profile/were
    # independently following -- it's about room membership, not follow
    # status. remove_room_from_space has its own (belt-and-suspenders) guard
    # against ever removing a Profile Room, but this path can't reach one
    # anyway since we've already returned False above for anything that
    # isn't a Remote User Room.
    await remove_room_from_space(request, matrix_user_id=left_user, child_room_id=room_id)

    actor_record = await repository.get_local_actor_by_matrix_id(left_user)
    if actor_record is None:
        return True  # no linked profile of their own -- never an independent follower

    if await repository.is_following(actor_record.username, remote_room.actor_id):
        await unfollow_remote_actor(request, actor_record, remote_room)
    return True
