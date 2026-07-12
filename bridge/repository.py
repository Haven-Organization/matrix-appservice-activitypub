"""Repository abstraction over actor/follower/federation bookkeeping.

Per the project's data-sovereignty constraint, the bridge does not keep a
separate database of posts -- those live in Matrix rooms. It does, however,
need to answer cheap, frequent questions ("does this username exist as a
linked profile?", "who follows them?", "what Matrix event mirrors this
fediverse post?") without round-tripping to the Matrix Client-Server API on
every ActivityPub request.

``ActorRepository`` is the seam: two durable implementations share this same
protocol, selected via ``storage.backend`` (see ``bridge.config.StorageSection``
and ``bridge.server._create_repository``) --
``bridge.sqlite_repository.SqliteActorRepository`` (the default) persists
everything to a sqlite file under ``storage.data_dir``, and
``bridge.postgres_repository.PostgresActorRepository`` to a Postgres database
instead, for a deployment that would rather not manage a separate sqlite
file. Either way linked profiles, follows, ghost/room mappings, and the
post/event map all survive a restart. ``InMemoryActorRepository`` here is a
third, process-local implementation of the same protocol kept for tests and
quick local runs; it is **not durable**.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class ActorRecord:
    """Everything the ActivityPub layer needs to know about a local actor.

    A local actor is either a Matrix user's linked "Profile Room" (``room_id``
    is that room, ``matrix_user_id`` its owner) or the bridge's own
    persistent service actor used for inbound following (``room_id`` is
    empty -- it isn't tied to any single room). ``private_key_pem`` is
    present because the bridge controls this actor and must sign outgoing
    activities on its behalf.
    """

    username: str
    matrix_user_id: str
    room_id: str
    public_key_pem: str
    private_key_pem: str
    display_name: str = ""
    summary: str = ""
    icon_url: str | None = None
    banner_url: str | None = None
    # Visible by default (matching e.g. Mastodon's own default) -- see
    # ``bridge.commands``'s ``hide``/``show`` commands and
    # ``bridge.activitypub.routes.get_followers``/``get_following``, which
    # only withhold the member list (``first.orderedItems``) when set, never
    # ``totalItems`` -- a profile's follower/following COUNT stays public
    # either way, same as Mastodon's own "hide network" setting.
    hide_followers: bool = False
    hide_following: bool = False
    # True for an actor provisioned for a Matrix user on a DIFFERENT
    # homeserver, admin-allowlisted via the ";allow"/";disallow"/";allowed"
    # commands (see bridge.commands's _effective_third_party_mode). Never
    # set for a local user or the bridge's own service actor. This is the
    # ONLY per-identity state this feature stores -- deliberately NOT a
    # "current mode" (full/follow_only), which is always computed live from
    # this flag + the CURRENT config.bridge.third_party_access_mode +
    # whether room_id is set, never cached, so flipping that config back and
    # forth never requires migrating or mutating any existing record.
    is_third_party: bool = False


@dataclass(frozen=True)
class ThirdPartyAllowRecord:
    """One admin-granted allowlist entry letting a user/room/homeserver on a
    DIFFERENT Matrix homeserver use the bridge (see the ";allow"/";disallow"/
    ";allowed" commands and ``ActorRecord.is_third_party``).

    ``rule_type`` is ``"mxid"`` (an exact ``@user:server``), ``"room"`` (any
    sender whose command arrives from this room -- membership is never
    separately queried, "the bot received this event from an allowlisted
    room" IS the check), or ``"homeserver"`` (any user on this domain).
    Checked in that precedence order (most to least specific) by
    ``ActorRepository.is_third_party_allowed``. ``granted_by``/``granted_at``
    are audit-trail only, never read for any permission decision.
    """

    rule_type: str
    value: str
    granted_by: str
    granted_at: str


@dataclass(frozen=True)
class RemoteActorRoom:
    """Maps a remote (fediverse) actor to the Matrix "Remote User Room" mirroring them.

    ``display_name``/``icon_url``/``banner_url`` mirror the actor's AP-side
    name, avatar, and banner (``image``) as of the last sync, so an inbound
    ``Update`` activity can tell whether any of them actually changed
    before re-uploading/re-setting anything.
    """

    actor_id: str
    room_id: str
    ghost_user_id: str
    inbox_url: str
    display_name: str = ""
    icon_url: str | None = None
    # Mirrors the actor's AP-side ``image`` (their profile banner/header),
    # same "as of last sync" purpose as icon_url -- see set_ghost_room_banner.
    banner_url: str | None = None
    # True only for the brief window between this room being freshly
    # created (the first-ever follow of this actor on this server -- see
    # bridge.commands._establish_remote_follow) and the follower actually
    # joining it, at which point bridge.membership.maybe_handle_join
    # consumes it (mark_backfill_pending_done) to trigger a one-time
    # ";backfill"-equivalent auto-backfill. False for every other room
    # (already-followed accounts reusing an existing room, room replace,
    # mention-triggered imports, ...) -- those never got a "first follow"
    # moment to hang this off of.
    pending_backfill: bool = False


@dataclass(frozen=True)
class FederatedEvent:
    """Bidirectional link between a Matrix event and the AP object it represents.

    Recorded both when an inbound ``Create`` is mirrored into a Remote User
    Room, and when a local reply is sent out as a new AP ``Note`` -- so reply
    chains resolve in either direction without a second data model.

    ``thread_root_event_id`` is the Matrix event at the root of this event's
    reply chain (None if this event *is* the root) -- AP's ``inReplyTo`` only
    ever names the immediate parent, but a Matrix thread relation needs the
    root of the whole chain, so it's threaded through here rather than
    re-walked on every reply.

    For a mirrored repost (an inbound ``Announce``), ``ap_object_id`` and
    ``author_actor_id`` deliberately name the *Announce activity itself* and
    its reposter -- not the reposted post or its original author -- since
    that's the key an inbound ``Undo(Announce)`` and Announce redelivery
    dedup both need to look this record up by. ``reposted_object_id``/
    ``reposted_author_actor_id`` separately record the reposted post's own AP
    id and its real author, so a *reaction* to the repost message (a Like or
    EmojiReact someone sends on the Matrix side) can be addressed to the
    actual post and delivered to its actual author, instead of nonsensically
    "liking" a reposter's Announce activity and notifying the wrong account.
    Both None for anything that isn't a mirrored repost.
    """

    event_id: str
    room_id: str
    ap_object_id: str
    author_actor_id: str
    thread_root_event_id: str | None = None
    reposted_object_id: str | None = None
    reposted_author_actor_id: str | None = None


@dataclass(frozen=True)
class GhostProfile:
    """The remote (http) display name/avatar URL last actually applied to a
    ghost's Matrix profile, keyed by the AP actor IRI -- independent of
    whether that actor has a Remote User Room at all (a third party just
    replying to something, never followed by anyone, still gets a ghost).

    Lets a ghost's profile sync be a no-op when nothing's actually changed:
    without this, every single interaction from a given remote actor (a
    reply, a reaction, ...) would re-fetch and re-upload their avatar image
    fresh and call ``set_avatar_url``/``set_display_name`` unconditionally,
    which Synapse renders as a "changed their profile picture/name" event in
    every room the ghost is in -- flooding rooms for accounts that interact
    often even though nothing about them actually changed between calls.

    ``mxid``/``handle`` additionally make this the reverse index from a
    ghost's Matrix user ID back to the fediverse actor it represents (and
    its ``@user@instance.org`` handle) -- used to turn a Matrix mention of a
    ghost into a proper ActivityPub mention when federating a post, since a
    ghost's localpart isn't reliably reversible by parsing it back apart.
    """

    actor_id: str
    display_name: str | None = None
    icon_url: str | None = None
    mxid: str | None = None
    handle: str | None = None


@dataclass(frozen=True)
class ReactionRecord:
    """Bidirectional link between an AP ``Like``/``EmojiReact``/``Announce``
    activity and the Matrix ``m.reaction`` event mirroring it.

    Lets an inbound ``Undo`` find and redact the specific reaction (not the
    post itself -- a past bug here), and lets a local Matrix redaction of an
    outbound reaction find the right ``Undo`` to send. Exactly one of
    ``reactor_ghost_mxid`` (inbound: redact as the same ghost that sent it,
    always permitted since you can redact your own event) or
    ``reactor_matrix_user_id`` (outbound: sign the eventual Undo as the same
    local user who reacted) is set, depending on direction.

    ``secondary_event_id`` is a SECOND Matrix event that also undoes this
    same activity if redacted -- used for an outbound repost specifically
    (see ``bridge.reaction_bridge.send_repost``): the reposter's own reaction/
    ``;repost`` command message is ``event_id``, and the "you reposted so-and-
    so's post" record posted into their Profile Room is
    ``secondary_event_id``, so redacting EITHER one works, matching how a
    repost's own Profile Room record (an ordinary ``FederatedEvent``, not a
    reaction at all) is already independently redactable through the
    regular post-delete path.
    """

    activity_id: str
    room_id: str
    event_id: str
    target_ap_object_id: str
    reactor_ghost_mxid: str | None = None
    reactor_matrix_user_id: str | None = None
    secondary_event_id: str | None = None
    # The custom emoji's own mxc:// (see ActorRepository.get_custom_emoji_mxc),
    # if this reaction's key was a custom-emoji shortcode with a resolvable
    # image -- None for a plain unicode/like reaction, or a custom emoji
    # whose image couldn't be fetched. Recorded per-reaction (not just per
    # shortcode) since the same shortcode text means different images on
    # different remote instances.
    custom_emoji_mxc: str | None = None


@dataclass(frozen=True)
class PollVoteRecord:
    """Bidirectional idempotency record for one poll vote -- either an
    inbound remote vote mirrored as a ghost's ``org.matrix.msc3381.poll.response``
    (``voter_actor_id``/``matrix_event_id`` set, ``matrix_user_id`` None), or
    an outbound local vote federated as a private ``Create{Note}`` to a
    mirrored poll's real author (``matrix_user_id`` set, ``voter_actor_id``/
    ``matrix_event_id`` None).

    Deliberately never stores WHICH option was chosen -- matching this
    module's own data-sovereignty rule (see its docstring below): the choice
    always lives in Matrix's own poll.response/poll.start event content,
    re-read live wherever it's needed (see
    ``bridge.inbox_dispatch._maybe_handle_poll_vote`` /
    ``bridge.poll_bridge.maybe_federate_poll_vote``), never duplicated here.

    ``vote_activity_id`` is the vote's own AP id (the inbound Create's own
    id for a mirrored remote vote, or a freshly minted one for an outbound
    vote) -- same "key off the source-side id" idempotency convention as
    ``federated_events``/``processed_transactions``/``reactions``.
    """

    vote_activity_id: str
    question_ap_object_id: str
    room_id: str
    voter_actor_id: str | None = None
    matrix_user_id: str | None = None
    matrix_event_id: str | None = None


class ActorRepository(Protocol):
    """Read/write access to local actors, remote actor rooms, and the post/event map."""

    async def get_local_actor(self, username: str) -> ActorRecord | None: ...

    async def get_local_actor_by_matrix_id(self, matrix_user_id: str) -> ActorRecord | None: ...

    async def get_local_actor_by_room_id(self, room_id: str) -> ActorRecord | None:
        """Look up the actor whose linked Profile Room is ``room_id`` (never matches
        the service actor, which has no room)."""
        ...

    async def get_profile_room_owner(self, room_id: str) -> str | None:
        """The ``matrix_user_id`` of whichever local user ``room_id`` is or
        EVER WAS linked to as their Profile Room -- unlike
        ``get_local_actor_by_room_id``, this survives a ``replace room``
        (which moves ``local_actors.room_id`` on to the new room, but the old
        room genuinely was theirs too). Returns None if ``room_id`` was never
        anyone's Profile Room, current or past."""
        ...

    async def get_profile_room_history(self, username: str) -> list[str]:
        """Every room_id ``username``'s Profile Room has ever been, current
        one included -- the reverse of ``get_profile_room_owner``. Used by
        the outbox so posts made before a ``replace room`` stay discoverable
        instead of vanishing once ``local_actors.room_id`` moves on."""
        ...

    async def register_local_actor(self, record: ActorRecord) -> None:
        """Create or replace a local actor (service actor at startup, or a freshly
        linked Profile Room)."""
        ...

    async def unregister_local_actor(self, username: str) -> None:
        """Remove a local actor entirely, along with its follower/following
        relationships -- used when a user unlinks their profile. No-op if the
        username doesn't exist."""
        ...

    async def list_followers(self, username: str) -> list[str]:
        """Return the AP actor IRIs following ``username``."""
        ...

    async def list_following(self, username: str) -> list[str]:
        """Return the AP actor IRIs ``username`` follows."""
        ...

    async def is_following(self, username: str, remote_actor_id: str) -> bool:
        """Whether ``username`` (locally) follows ``remote_actor_id``."""
        ...

    async def is_anyone_following(self, remote_actor_id: str) -> bool:
        """Whether *any* local actor follows ``remote_actor_id`` -- following is
        tracked per local actor (each user follows under their own identity),
        but a Remote User Room mirrors posts for whoever's still following it
        at all, regardless of which specific local inbox a given delivery
        happened to arrive at."""
        ...

    async def add_follower(self, username: str, remote_actor_id: str) -> None: ...

    async def remove_follower(self, username: str, remote_actor_id: str) -> None: ...

    async def add_following(self, username: str, remote_actor_id: str) -> None: ...

    async def remove_following(self, username: str, remote_actor_id: str) -> None: ...

    async def set_followers_hidden(self, username: str, hidden: bool) -> None:
        """Toggle whether ``username``'s public followers collection exposes
        its member list to remote viewers (see ``ActorRecord.hide_followers``).
        No-op if ``username`` doesn't exist."""
        ...

    async def set_following_hidden(self, username: str, hidden: bool) -> None:
        """Same as ``set_followers_hidden``, for the following collection."""
        ...

    async def list_third_party_records(self) -> list[ActorRecord]:
        """Every ``ActorRecord`` with ``is_third_party=True`` and an empty
        ``room_id`` -- i.e. every identity currently eligible for the
        periodic Follow-Only profile-mirroring sync (see
        ``bridge.third_party_sync``). A record that's graduated to a real
        Profile Room (``room_id`` set) is deliberately excluded -- that room
        is authoritative for their profile now, current config permitting
        (see ``ActorRecord.is_third_party``'s docstring)."""
        ...

    async def add_third_party_allow(self, rule_type: str, value: str, *, granted_by: str) -> None:
        """Grant third-party access to ``value`` (an MXID, room ID, or
        homeserver domain depending on ``rule_type``). Idempotent -- granting
        the same ``(rule_type, value)`` again just refreshes ``granted_by``/
        ``granted_at``."""
        ...

    async def remove_third_party_allow(self, rule_type: str, value: str) -> None:
        """Revoke a grant. No-op if it doesn't exist. Never tears down any
        ``ActorRecord`` already provisioned under this grant -- see this
        feature's design note in ``bridge.commands`` for why revocation is
        deliberately non-destructive."""
        ...

    async def list_third_party_allows(self, rule_type: str | None = None) -> list[ThirdPartyAllowRecord]:
        """Every current grant, optionally filtered to one ``rule_type`` --
        used by the ";allowed" command."""
        ...

    async def is_third_party_allowed(self, *, mxid: str, homeserver: str, room_id: str) -> bool:
        """Whether ``mxid`` currently has third-party access, checking all
        three grant kinds in precedence order (exact ``mxid``, then
        ``room_id`` membership, then ``homeserver``) -- ``True`` as soon as
        any one matches. This is the single source of truth both the
        command-dispatch gate AND every outbound-federation re-validation
        point (reactions, replies, DM, Chat, post distribution -- see
        ``bridge.commands.is_third_party_still_allowed``) call, so a
        revoked grant takes effect everywhere at once, live, with nothing
        cached."""
        ...

    async def is_blocked(self, username: str, remote_actor_id: str) -> bool:
        """Whether ``username`` has blocked ``remote_actor_id`` -- see
        ``bridge.commands``'s ``block``/``unblock`` commands, and
        ``bridge.inbox_dispatch.handle_activity``'s own Follow-specific
        check (a Follow from a blocked actor gets a ``Reject``, never
        recorded as a follower)."""
        ...

    async def add_blocked(self, username: str, remote_actor_id: str) -> None: ...

    async def remove_blocked(self, username: str, remote_actor_id: str) -> None: ...

    async def is_muted(self, username: str, remote_actor_id: str) -> bool:
        """Whether ``username`` has muted ``remote_actor_id`` -- see
        ``bridge.commands``'s ``mute``/``unmute`` commands, and
        ``bridge.note_mirroring.is_silenced`` (checked alongside
        ``is_blocked`` everywhere a notification/auto-invite would
        otherwise fire)."""
        ...

    async def add_muted(self, username: str, remote_actor_id: str) -> None: ...

    async def remove_muted(self, username: str, remote_actor_id: str) -> None: ...

    async def get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None: ...

    async def get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None: ...

    async def register_remote_actor_room(self, record: RemoteActorRoom) -> None: ...

    async def get_remote_actor_room_history_actor_id(self, room_id: str) -> str | None:
        """The fediverse actor ``room_id`` is OR EVER WAS the Remote User
        Room for -- unlike ``get_remote_actor_room_by_room_id``, this
        survives a ``replace room`` (whose upsert moves
        ``remote_actor_rooms.room_id`` on to the new room, overwriting the
        old pointer with no history kept there). Returns None if
        ``room_id`` was never a Remote User Room, current or past. Backed
        by a permanent, append-only history table -- see
        ``bridge.note_mirroring.resolve_old_remote_actor_room``, which
        falls back to reading the room's own state back out of Synapse
        only for a room old enough to predate this table."""
        ...

    async def mark_backfill_pending_done(self, room_id: str) -> None:
        """Clear ``RemoteActorRoom.pending_backfill`` for ``room_id`` --
        called once, by ``bridge.membership.maybe_handle_join``, the moment
        it consumes the flag to trigger the one-time auto-backfill. A
        no-op if the room has no pending backfill (already consumed, or
        never set in the first place)."""
        ...

    async def record_federated_event(self, record: FederatedEvent, *, is_primary: bool = True) -> None:
        """Record ``record``. ``ap_object_id`` is only required to be unique
        among ``is_primary=True`` rows -- not, as it might look, because
        anything currently records more than one ``FederatedEvent`` for the
        same AP object. It briefly did: a multi-attachment post used to send
        each extra attachment as its own separate Matrix event, each getting
        its own non-primary row sharing the post's ``ap_object_id`` so
        reacting to any of them resolved to the same underlying object. That
        was reverted -- an ActivityPub post now always maps to exactly one
        Matrix event (see ``bridge.note_mirroring.attach_media_to_content``)
        -- but the historical non-primary rows it already wrote are still
        sitting in the repository, so the schema keeps accommodating them
        rather than un-migrating past data for no benefit. Nothing should
        pass ``is_primary=False`` going forward; the parameter stays mainly
        so old rows keep meaning what they always meant.
        ``get_federated_event_by_ap_object`` only ever returns the primary
        row for a given ``ap_object_id``; ``get_federated_event_by_matrix_event``
        finds either kind, keyed by its own always-unique ``event_id``."""
        ...

    async def get_federated_event_by_matrix_event(self, event_id: str) -> FederatedEvent | None: ...

    async def get_federated_event_by_ap_object(self, ap_object_id: str) -> FederatedEvent | None:
        """The PRIMARY ``FederatedEvent`` for ``ap_object_id`` -- see
        ``record_federated_event``'s docstring for what "primary" means."""
        ...

    async def record_reaction(self, record: ReactionRecord) -> None: ...

    async def get_reaction_by_activity_id(self, activity_id: str) -> ReactionRecord | None: ...

    async def get_reaction_by_matrix_event(self, event_id: str) -> ReactionRecord | None:
        """Matches EITHER ``event_id`` or ``secondary_event_id`` -- see
        ``ReactionRecord.secondary_event_id``'s own docstring for why
        there's two."""
        ...

    async def remove_reaction(self, activity_id: str) -> None: ...

    async def record_poll_vote(self, record: PollVoteRecord) -> None: ...

    async def get_poll_vote_by_activity_id(self, vote_activity_id: str) -> PollVoteRecord | None:
        """Inbound redelivery dedup -- have we already mirrored this exact vote?"""
        ...

    async def get_poll_vote_by_matrix_user(
        self, question_ap_object_id: str, matrix_user_id: str
    ) -> PollVoteRecord | None:
        """Outbound no-revote check -- has this local user already sent a
        vote out for this externally-owned poll? (Mastodon-family software
        doesn't support changing a vote once cast.)"""
        ...

    async def get_custom_emoji_mxc(self, source_url: str) -> str | None:
        """The ``mxc://`` a custom emoji's remote image ``source_url`` was
        already uploaded to Synapse's media repo as, if any -- checked
        before ever fetching+uploading one, so the exact same emoji image is
        never downloaded/stored twice no matter how many reactions use it.
        None on a cache miss."""
        ...

    async def record_custom_emoji_mxc(self, source_url: str, mxc_url: str) -> None:
        """Record that ``source_url`` has been uploaded as ``mxc_url`` --
        idempotent (a second call for the same ``source_url`` is a no-op),
        matching every other dedup-by-insert pattern in this module."""
        ...

    async def get_custom_emoji_by_reaction_event_ids(self, event_ids: list[str]) -> dict[str, str]:
        """Batch lookup (one query, not N+1) of which of ``event_ids`` (a
        post's ``m.reaction`` events) carried a custom emoji, mapping each
        such ``event_id`` to that reaction's ``custom_emoji_mxc`` -- used by
        the public profile/post HTML pages (bridge.web_views.summarize_reactions)
        to render custom-emoji images alongside unicode reaction chips."""
        ...

    async def record_resolved_emoji(self, subject_id: str, shortcode: str, mxc_url: str) -> None:
        """Records that ``shortcode`` (e.g. ``":blobcat:"``), as used
        specifically by ``subject_id``, resolved to ``mxc_url`` -- so a
        later re-render (the public HTML page rebuilding a post's content
        from its stored plain-text body, or a ghost's byline from its
        cached display name -- neither of which keeps the original AP
        ``tag`` data around) can still show the right image without
        re-fetching it. ``subject_id`` is deliberately opaque -- either a
        post's ``ap_object_id`` (content emoji) or a remote actor's own
        ``actor_id`` (display-name emoji): scoping by subject, not just
        shortcode, is what keeps two different servers' same-named-but-
        different emoji from colliding (see ``bridge.custom_emoji``).
        Idempotent, same as every other dedup-by-insert pattern here."""
        ...

    async def get_resolved_emoji(self, subject_id: str) -> dict[str, str]:
        """Every shortcode -> ``mxc_url`` previously recorded for
        ``subject_id`` via ``record_resolved_emoji``."""
        ...

    async def has_processed_transaction(self, txn_id: str) -> bool:
        """Whether this AppService transaction ID has already been handled.

        Synapse retries a transaction it didn't get a 200 for; this check
        (together with ``mark_transaction_processed``) makes that safe even
        across a bridge restart between the original attempt and the retry.
        """
        ...

    async def mark_transaction_processed(self, txn_id: str) -> None: ...

    async def is_media_published(self, mxc_uri: str) -> bool:
        """Whether ``mxc_uri`` is one the bridge has actually published as public
        ActivityPub-facing content (a local actor's avatar, or a Profile Room
        post's attachment).

        This is the allowlist the public media proxy (``GET /media/{server}/{id}``)
        checks before serving anything -- without it, the proxy would be an open,
        unauthenticated gateway to *any* media on the homeserver, defeating
        Synapse's own authenticated-media protections for every other room.
        """
        ...

    async def mark_media_published(self, mxc_uri: str) -> None: ...

    async def get_ghost_profile(self, actor_id: str) -> GhostProfile | None:
        """The display name/avatar URL last synced to this actor's ghost, or
        None if we've never synced one (a brand new ghost)."""
        ...

    async def get_ghost_profile_by_mxid(self, mxid: str) -> GhostProfile | None:
        """The reverse lookup: which fediverse actor (and handle) a ghost's
        Matrix user ID represents, or None if it isn't a ghost we've ever
        synced a profile for."""
        ...

    async def record_ghost_profile(self, profile: GhostProfile) -> None:
        """Record what was just synced to an actor's ghost, replacing any
        previous record for the same ``actor_id``."""
        ...

    async def get_user_space(self, matrix_user_id: str) -> str | None:
        """The room ID of ``matrix_user_id``'s personal "Fediverse" space
        (see ``bridge.spaces``), or None if they don't have one yet."""
        ...

    async def register_user_space(self, matrix_user_id: str, space_room_id: str) -> None:
        """Record ``space_room_id`` as ``matrix_user_id``'s Fediverse space."""
        ...

    async def get_bot_dm_room(self, matrix_user_id: str) -> str | None:
        """The room ID of the bot's 1:1 direct-message room with
        ``matrix_user_id`` (see ``bridge.notifications``), or None if one
        hasn't been created yet."""
        ...

    async def register_bot_dm_room(self, matrix_user_id: str, room_id: str) -> None:
        """Record ``room_id`` as the bot's DM room with ``matrix_user_id``."""
        ...

    async def get_bot_dm_room_owner(self, room_id: str) -> str | None:
        """The reverse of ``get_bot_dm_room``: which ``matrix_user_id`` this
        is the bot's notifications DM room with, or None if it isn't one --
        used by ``;replace room`` to find out whose Notifications room this
        is and check they're the one running the command (or an admin)."""
        ...

    async def get_ghost_dm_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        """The room ID of the 1:1 direct-message room between the ghost for
        ``actor_id`` and ``matrix_user_id`` (see
        ``bridge.note_mirroring.mirror_direct_message``), or None if one
        hasn't been created yet."""
        ...

    async def register_ghost_dm_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        """Record ``room_id`` as the DM room between the ghost for
        ``actor_id`` and ``matrix_user_id``."""
        ...

    async def is_ghost_dm_room(self, room_id: str) -> bool:
        """Whether ``room_id`` is a ghost-to-local-user direct-message room
        (as opposed to a Remote User Room, a Profile Room, or anything
        else) -- used to decide whether a Matrix reply sent in it should be
        federated out as a private reply instead of a public one (see
        ``bridge.reply_bridge``)."""
        ...

    async def get_ghost_dm_room_actor_id(self, room_id: str) -> str | None:
        """The reverse of ``get_ghost_dm_room``: the fediverse actor ID
        ``room_id`` is a direct-message room with, or None if it isn't a
        ghost DM room at all. Used to address a fresh (non-reply) message
        sent in one -- there's no reply target to derive the recipient
        from in that case, only the room itself (see
        ``bridge.reply_bridge.maybe_federate_reply``)."""
        ...

    async def get_ghost_dm_room_matrix_user_id(self, room_id: str) -> str | None:
        """The other half of ``get_ghost_dm_room_actor_id``: which local
        user ``room_id`` is a direct-message room with, or None if it isn't
        a ghost DM room at all -- used by ``;replace room`` to check the
        command runner is that same local user (or an admin)."""
        ...

    async def get_ghost_dm_room_history(self, room_id: str) -> tuple[str, str] | None:
        """The ``(actor_id, matrix_user_id)`` pair ``room_id`` is OR EVER
        WAS a ghost DM room for -- survives a ``replace room`` the same way
        ``get_remote_actor_room_history_actor_id`` does for Remote User
        Rooms (``ghost_dm_rooms``' upsert moves ``room_id`` on to the new
        room with no history kept there). None if ``room_id`` was never a
        ghost DM room, current or past. Backed by a permanent, append-only
        history table -- see ``bridge.note_mirroring.resolve_old_ghost_room_owner``,
        the state-reading fallback for a room old enough to predate it."""
        ...

    async def get_ghost_dm_room_ids_for_actor(self, actor_id: str) -> list[str]:
        """Every DM room ID the ghost for ``actor_id`` has, across every
        local user that's ever started one with them -- used to keep a
        room's avatar in sync when that actor changes their profile
        picture (see ``bridge.ghosts.sync_ghost_profile``), since a single
        remote actor can have more than one DM room open at once (one per
        local user who's DMed them)."""
        ...

    async def get_ghost_chat_room_ids_for_actor(self, actor_id: str) -> list[str]:
        """The ``ChatMessage`` counterpart of ``get_ghost_dm_room_ids_for_actor``."""
        ...

    async def get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        """The room ID of the 1:1 ``ChatMessage`` room between the ghost
        for ``actor_id`` and ``matrix_user_id`` (see
        ``bridge.note_mirroring.mirror_chat_message``), or None if one
        hasn't been created yet. A DELIBERATELY SEPARATE table/lifecycle
        from ``get_ghost_dm_room`` -- ActivityPub ``ChatMessage`` (Pleroma's
        "Chats", a distinct instant-messaging concept from a Mastodon-style
        Note-based direct message, shown in its own UI section on software
        that supports it) and a Note-based DM are different wire formats a
        room's outgoing messages commit to for its whole lifetime, so the
        same (ghost, local user) pair can have up to one of EACH kind of
        room, never one room serving both."""
        ...

    async def register_ghost_chat_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        """Record ``room_id`` as the ``ChatMessage`` room between the ghost
        for ``actor_id`` and ``matrix_user_id``."""
        ...

    async def is_ghost_chat_room(self, room_id: str) -> bool:
        """Whether ``room_id`` is a ghost-to-local-user ``ChatMessage`` room
        -- used to decide whether a Matrix message sent in it should be
        federated out as a chat message instead of a reply/DM/public post
        (see ``bridge.chat_bridge``)."""
        ...

    async def get_ghost_chat_room_actor_id(self, room_id: str) -> str | None:
        """The reverse of ``get_ghost_chat_room``: the fediverse actor ID
        ``room_id`` is a chat room with, or None if it isn't a ghost chat
        room at all."""
        ...

    async def get_ghost_chat_room_matrix_user_id(self, room_id: str) -> str | None:
        """The other half of ``get_ghost_chat_room_actor_id``: which local
        user ``room_id`` is a chat room with -- used by ``;replace room`` to
        check the command runner is that same local user (or an admin)."""
        ...

    async def get_ghost_chat_room_history(self, room_id: str) -> tuple[str, str] | None:
        """The ``ChatMessage`` counterpart of ``get_ghost_dm_room_history``."""
        ...


@dataclass
class _LocalActorState:
    record: ActorRecord
    followers: set[str] = field(default_factory=set)
    following: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    muted: set[str] = field(default_factory=set)


_MAX_TRACKED_TRANSACTIONS = 10_000


class InMemoryActorRepository:
    """Process-local ``ActorRepository`` implementation.

    Not durable: all state (linked profiles, follows, the post/event map) is
    lost on restart. Useful for tests and quick local runs; production
    deployments get ``SqliteActorRepository`` by default (see
    ``bridge.server.create_app``).
    """

    def __init__(self) -> None:
        self._actors: dict[str, _LocalActorState] = {}
        self._actors_by_matrix_id: dict[str, str] = {}
        self._actors_by_room_id: dict[str, str] = {}
        self._room_history: dict[str, str] = {}  # room_id -> matrix_user_id, never popped
        self._remote_rooms: dict[str, RemoteActorRoom] = {}
        self._remote_rooms_by_room_id: dict[str, RemoteActorRoom] = {}
        self._remote_room_history: dict[str, str] = {}  # room_id -> actor_id, never popped
        self._federated_by_matrix_event: dict[str, FederatedEvent] = {}
        self._federated_by_ap_object: dict[str, FederatedEvent] = {}
        self._processed_txn_ids: set[str] = set()
        self._processed_txn_order: deque[str] = deque()
        self._published_media: set[str] = set()
        self._reactions_by_activity: dict[str, ReactionRecord] = {}
        self._reactions_by_matrix_event: dict[str, ReactionRecord] = {}
        self._poll_votes_by_activity: dict[str, PollVoteRecord] = {}
        self._custom_emoji: dict[str, str] = {}  # source_url -> mxc_url
        self._resolved_emoji: dict[str, dict[str, str]] = {}  # subject_id -> {shortcode: mxc_url}
        self._ghost_profiles: dict[str, GhostProfile] = {}
        self._user_spaces: dict[str, str] = {}
        self._bot_dm_rooms: dict[str, str] = {}
        self._ghost_dm_rooms: dict[tuple[str, str], str] = {}
        self._ghost_dm_room_ids: set[str] = set()
        self._ghost_dm_room_history: dict[str, tuple[str, str]] = {}  # room_id -> (actor_id, matrix_user_id)
        self._ghost_chat_rooms: dict[tuple[str, str], str] = {}
        self._ghost_chat_room_ids: set[str] = set()
        self._ghost_chat_room_history: dict[str, tuple[str, str]] = {}  # room_id -> (actor_id, matrix_user_id)
        self._third_party_allows: dict[tuple[str, str], ThirdPartyAllowRecord] = {}

    async def get_local_actor(self, username: str) -> ActorRecord | None:
        state = self._actors.get(username)
        return state.record if state else None

    async def get_local_actor_by_matrix_id(self, matrix_user_id: str) -> ActorRecord | None:
        username = self._actors_by_matrix_id.get(matrix_user_id)
        return await self.get_local_actor(username) if username else None

    async def get_local_actor_by_room_id(self, room_id: str) -> ActorRecord | None:
        username = self._actors_by_room_id.get(room_id) if room_id else None
        return await self.get_local_actor(username) if username else None

    async def get_profile_room_owner(self, room_id: str) -> str | None:
        return self._room_history.get(room_id) if room_id else None

    async def register_local_actor(self, record: ActorRecord) -> None:
        existing = self._actors.get(record.username)
        followers = existing.followers if existing else set()
        following = existing.following if existing else set()
        # An unlink (room_id cleared) or a room replacement/relink moves
        # which room this actor is bound to -- drop the old reverse mapping
        # so a stale room_id doesn't keep resolving to this actor.
        if existing is not None and existing.record.room_id and existing.record.room_id != record.room_id:
            self._actors_by_room_id.pop(existing.record.room_id, None)
        self._actors[record.username] = _LocalActorState(
            record=record, followers=followers, following=following
        )
        self._actors_by_matrix_id[record.matrix_user_id] = record.username
        if record.room_id:
            self._actors_by_room_id[record.room_id] = record.username
            # Unlike the reverse index above, this is never popped -- a room
            # that was genuinely someone's Profile Room stays "theirs" for
            # ownership checks even after a `replace room` moves them on.
            self._room_history.setdefault(record.room_id, record.matrix_user_id)

    async def unregister_local_actor(self, username: str) -> None:
        state = self._actors.pop(username, None)
        if state is None:
            return
        if self._actors_by_matrix_id.get(state.record.matrix_user_id) == username:
            del self._actors_by_matrix_id[state.record.matrix_user_id]
        if state.record.room_id and self._actors_by_room_id.get(state.record.room_id) == username:
            del self._actors_by_room_id[state.record.room_id]

    async def list_followers(self, username: str) -> list[str]:
        state = self._actors.get(username)
        return sorted(state.followers) if state else []

    async def list_following(self, username: str) -> list[str]:
        state = self._actors.get(username)
        return sorted(state.following) if state else []

    async def is_following(self, username: str, remote_actor_id: str) -> bool:
        state = self._actors.get(username)
        return remote_actor_id in state.following if state else False

    async def is_anyone_following(self, remote_actor_id: str) -> bool:
        return any(remote_actor_id in state.following for state in self._actors.values())

    async def add_follower(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.followers.add(remote_actor_id)

    async def remove_follower(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.followers.discard(remote_actor_id)

    async def add_following(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.following.add(remote_actor_id)

    async def remove_following(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.following.discard(remote_actor_id)

    async def set_followers_hidden(self, username: str, hidden: bool) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.record = replace(state.record, hide_followers=hidden)

    async def set_following_hidden(self, username: str, hidden: bool) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.record = replace(state.record, hide_following=hidden)

    async def list_third_party_records(self) -> list[ActorRecord]:
        return [
            state.record
            for state in self._actors.values()
            if state.record.is_third_party and not state.record.room_id
        ]

    async def add_third_party_allow(self, rule_type: str, value: str, *, granted_by: str) -> None:
        self._third_party_allows[(rule_type, value)] = ThirdPartyAllowRecord(
            rule_type=rule_type,
            value=value,
            granted_by=granted_by,
            granted_at=datetime.now(timezone.utc).isoformat(),
        )

    async def remove_third_party_allow(self, rule_type: str, value: str) -> None:
        self._third_party_allows.pop((rule_type, value), None)

    async def list_third_party_allows(self, rule_type: str | None = None) -> list[ThirdPartyAllowRecord]:
        records = self._third_party_allows.values()
        if rule_type is not None:
            records = [r for r in records if r.rule_type == rule_type]
        return sorted(records, key=lambda r: (r.rule_type, r.value))

    async def is_third_party_allowed(self, *, mxid: str, homeserver: str, room_id: str) -> bool:
        if ("mxid", mxid) in self._third_party_allows:
            return True
        if room_id and ("room", room_id) in self._third_party_allows:
            return True
        if homeserver and ("homeserver", homeserver) in self._third_party_allows:
            return True
        return False

    async def is_blocked(self, username: str, remote_actor_id: str) -> bool:
        state = self._actors.get(username)
        return remote_actor_id in state.blocked if state else False

    async def add_blocked(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.blocked.add(remote_actor_id)

    async def remove_blocked(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.blocked.discard(remote_actor_id)

    async def is_muted(self, username: str, remote_actor_id: str) -> bool:
        state = self._actors.get(username)
        return remote_actor_id in state.muted if state else False

    async def add_muted(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.muted.add(remote_actor_id)

    async def remove_muted(self, username: str, remote_actor_id: str) -> None:
        state = self._actors.get(username)
        if state is not None:
            state.muted.discard(remote_actor_id)

    async def get_remote_actor_room(self, actor_id: str) -> RemoteActorRoom | None:
        return self._remote_rooms.get(actor_id)

    async def get_remote_actor_room_by_room_id(self, room_id: str) -> RemoteActorRoom | None:
        return self._remote_rooms_by_room_id.get(room_id)

    async def register_remote_actor_room(self, record: RemoteActorRoom) -> None:
        # Same reasoning as register_local_actor: a room replacement moves
        # this actor to a new room_id -- drop the old reverse mapping.
        previous = self._remote_rooms.get(record.actor_id)
        if previous is not None and previous.room_id != record.room_id:
            self._remote_rooms_by_room_id.pop(previous.room_id, None)
        if previous is not None:
            # An ordinary re-registration (profile sync, room replace) must
            # never clobber pending_backfill -- only mark_backfill_pending_done
            # is allowed to change it after the row's first creation. See
            # PostgresActorRepository.register_remote_actor_room's identical
            # reasoning.
            record = replace(record, pending_backfill=previous.pending_backfill)
        self._remote_rooms[record.actor_id] = record
        self._remote_rooms_by_room_id[record.room_id] = record
        # Permanent, unlike the two dicts above -- see
        # get_remote_actor_room_history_actor_id's docstring.
        self._remote_room_history[record.room_id] = record.actor_id

    async def get_remote_actor_room_history_actor_id(self, room_id: str) -> str | None:
        return self._remote_room_history.get(room_id)

    async def mark_backfill_pending_done(self, room_id: str) -> None:
        record = self._remote_rooms_by_room_id.get(room_id)
        if record is None:
            return
        updated = replace(record, pending_backfill=False)
        self._remote_rooms[record.actor_id] = updated
        self._remote_rooms_by_room_id[room_id] = updated

    async def record_federated_event(self, record: FederatedEvent, *, is_primary: bool = True) -> None:
        self._federated_by_matrix_event[record.event_id] = record
        if is_primary:
            self._federated_by_ap_object[record.ap_object_id] = record

    async def get_federated_event_by_matrix_event(self, event_id: str) -> FederatedEvent | None:
        return self._federated_by_matrix_event.get(event_id)

    async def get_federated_event_by_ap_object(self, ap_object_id: str) -> FederatedEvent | None:
        return self._federated_by_ap_object.get(ap_object_id)

    async def has_processed_transaction(self, txn_id: str) -> bool:
        return txn_id in self._processed_txn_ids

    async def mark_transaction_processed(self, txn_id: str) -> None:
        self._processed_txn_ids.add(txn_id)
        self._processed_txn_order.append(txn_id)
        while len(self._processed_txn_order) > _MAX_TRACKED_TRANSACTIONS:
            self._processed_txn_ids.discard(self._processed_txn_order.popleft())

    async def is_media_published(self, mxc_uri: str) -> bool:
        return mxc_uri in self._published_media

    async def mark_media_published(self, mxc_uri: str) -> None:
        self._published_media.add(mxc_uri)

    async def record_reaction(self, record: ReactionRecord) -> None:
        self._reactions_by_activity[record.activity_id] = record
        self._reactions_by_matrix_event[record.event_id] = record
        if record.secondary_event_id:
            self._reactions_by_matrix_event[record.secondary_event_id] = record

    async def get_reaction_by_activity_id(self, activity_id: str) -> ReactionRecord | None:
        return self._reactions_by_activity.get(activity_id)

    async def get_reaction_by_matrix_event(self, event_id: str) -> ReactionRecord | None:
        return self._reactions_by_matrix_event.get(event_id)

    async def remove_reaction(self, activity_id: str) -> None:
        record = self._reactions_by_activity.pop(activity_id, None)
        if record is not None:
            self._reactions_by_matrix_event.pop(record.event_id, None)
            if record.secondary_event_id:
                self._reactions_by_matrix_event.pop(record.secondary_event_id, None)

    async def record_poll_vote(self, record: PollVoteRecord) -> None:
        self._poll_votes_by_activity[record.vote_activity_id] = record

    async def get_poll_vote_by_activity_id(self, vote_activity_id: str) -> PollVoteRecord | None:
        return self._poll_votes_by_activity.get(vote_activity_id)

    async def get_poll_vote_by_matrix_user(
        self, question_ap_object_id: str, matrix_user_id: str
    ) -> PollVoteRecord | None:
        for record in self._poll_votes_by_activity.values():
            if record.question_ap_object_id == question_ap_object_id and record.matrix_user_id == matrix_user_id:
                return record
        return None

    async def get_custom_emoji_mxc(self, source_url: str) -> str | None:
        return self._custom_emoji.get(source_url)

    async def record_custom_emoji_mxc(self, source_url: str, mxc_url: str) -> None:
        self._custom_emoji.setdefault(source_url, mxc_url)

    async def get_custom_emoji_by_reaction_event_ids(self, event_ids: list[str]) -> dict[str, str]:
        result = {}
        for event_id in event_ids:
            record = self._reactions_by_matrix_event.get(event_id)
            if record is not None and record.custom_emoji_mxc:
                result[event_id] = record.custom_emoji_mxc
        return result

    async def record_resolved_emoji(self, subject_id: str, shortcode: str, mxc_url: str) -> None:
        self._resolved_emoji.setdefault(subject_id, {}).setdefault(shortcode, mxc_url)

    async def get_resolved_emoji(self, subject_id: str) -> dict[str, str]:
        return dict(self._resolved_emoji.get(subject_id, {}))

    async def get_ghost_profile(self, actor_id: str) -> GhostProfile | None:
        return self._ghost_profiles.get(actor_id)

    async def get_ghost_profile_by_mxid(self, mxid: str) -> GhostProfile | None:
        for profile in self._ghost_profiles.values():
            if profile.mxid == mxid:
                return profile
        return None

    async def record_ghost_profile(self, profile: GhostProfile) -> None:
        self._ghost_profiles[profile.actor_id] = profile

    async def get_user_space(self, matrix_user_id: str) -> str | None:
        return self._user_spaces.get(matrix_user_id)

    async def register_user_space(self, matrix_user_id: str, space_room_id: str) -> None:
        self._user_spaces[matrix_user_id] = space_room_id

    async def get_bot_dm_room(self, matrix_user_id: str) -> str | None:
        return self._bot_dm_rooms.get(matrix_user_id)

    async def register_bot_dm_room(self, matrix_user_id: str, room_id: str) -> None:
        self._bot_dm_rooms[matrix_user_id] = room_id

    async def get_bot_dm_room_owner(self, room_id: str) -> str | None:
        for matrix_user_id, dm_room_id in self._bot_dm_rooms.items():
            if dm_room_id == room_id:
                return matrix_user_id
        return None

    async def get_ghost_dm_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        return self._ghost_dm_rooms.get((actor_id, matrix_user_id))

    async def register_ghost_dm_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        self._ghost_dm_rooms[(actor_id, matrix_user_id)] = room_id
        self._ghost_dm_room_ids.add(room_id)
        self._ghost_dm_room_history[room_id] = (actor_id, matrix_user_id)

    async def get_ghost_dm_room_history(self, room_id: str) -> tuple[str, str] | None:
        return self._ghost_dm_room_history.get(room_id)

    async def is_ghost_dm_room(self, room_id: str) -> bool:
        return room_id in self._ghost_dm_room_ids

    async def get_ghost_dm_room_actor_id(self, room_id: str) -> str | None:
        for (actor_id, _matrix_user_id), dm_room_id in self._ghost_dm_rooms.items():
            if dm_room_id == room_id:
                return actor_id
        return None

    async def get_ghost_dm_room_matrix_user_id(self, room_id: str) -> str | None:
        for (_actor_id, matrix_user_id), dm_room_id in self._ghost_dm_rooms.items():
            if dm_room_id == room_id:
                return matrix_user_id
        return None

    async def get_ghost_dm_room_ids_for_actor(self, actor_id: str) -> list[str]:
        return [
            dm_room_id
            for (room_actor_id, _matrix_user_id), dm_room_id in self._ghost_dm_rooms.items()
            if room_actor_id == actor_id
        ]

    async def get_ghost_chat_room(self, actor_id: str, matrix_user_id: str) -> str | None:
        return self._ghost_chat_rooms.get((actor_id, matrix_user_id))

    async def register_ghost_chat_room(self, actor_id: str, matrix_user_id: str, room_id: str) -> None:
        self._ghost_chat_rooms[(actor_id, matrix_user_id)] = room_id
        self._ghost_chat_room_ids.add(room_id)
        self._ghost_chat_room_history[room_id] = (actor_id, matrix_user_id)

    async def get_ghost_chat_room_history(self, room_id: str) -> tuple[str, str] | None:
        return self._ghost_chat_room_history.get(room_id)

    async def is_ghost_chat_room(self, room_id: str) -> bool:
        return room_id in self._ghost_chat_room_ids

    async def get_ghost_chat_room_actor_id(self, room_id: str) -> str | None:
        for (actor_id, _matrix_user_id), chat_room_id in self._ghost_chat_rooms.items():
            if chat_room_id == room_id:
                return actor_id
        return None

    async def get_ghost_chat_room_matrix_user_id(self, room_id: str) -> str | None:
        for (_actor_id, matrix_user_id), chat_room_id in self._ghost_chat_rooms.items():
            if chat_room_id == room_id:
                return matrix_user_id
        return None

    async def get_ghost_chat_room_ids_for_actor(self, actor_id: str) -> list[str]:
        return [
            chat_room_id
            for (room_actor_id, _matrix_user_id), chat_room_id in self._ghost_chat_rooms.items()
            if room_actor_id == actor_id
        ]
