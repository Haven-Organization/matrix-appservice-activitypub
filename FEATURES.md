# What's bridged

## Posts, replies, and threads

- **New posts**: a message in your linked Profile Room becomes an outgoing `Create{Note}`, addressed publicly and delivered to your followers' inboxes (plus anyone mentioned).
- **Incoming posts**: a followed account's `Create{Note}` is mirrored as a plain Matrix message into their Remote User Room. It's HTML-sanitized to a safe tag subset and deduplicated by the post's ActivityPub object ID.
- **Replies, both directions**: a Matrix reply or thread-reply to a mirrored post federates out with the correct `inReplyTo`, tagging the parent author and every other participant already in the thread (fetched live from the parent, so the "reply to @a @b" line reads correctly on the far end). Incoming replies are mirrored as real Matrix thread replies, walking up untracked ancestors and auto-importing the true root if needed so the reply always has somewhere real to land.
- **Guest posting**: a different local user posting inside someone else's Profile Room federates as their own post, under their own actor and outbox, with the room owner auto-mentioned.
- **Edits**: a Matrix edit (`m.replace`) federates as `Update` on the same Note ID, never delete-and-recreate, so replies, likes, and reposts referencing it stay intact.
- **Deletes**: redacting your own distributed post sends a signed `Delete` to every follower. An incoming `Delete` for a tracked post redacts the mirrored Matrix event, but only if the sender actually matches the recorded author.
- **Backfill**: `;backfill` (or an inbound reply chain with gaps) pulls an account's outbox or a specific thread's replies through the exact same mirroring path a live delivery uses, so history is indistinguishable from anything that arrived live.
- **Quote-posts**: Akkoma/Pleroma/Fedibird/Misskey-style quote fields are detected on the way in (any auto-appended "RT: link" fallback text is stripped) and rendered as a quote card. `;repost <caption>` sends a real quote-post of your own the other way.

## Reactions and reposts

- **Reactions**: a plain 👍 Matrix reaction sends `Like`. Any other emoji sends `EmojiReact` (a Pleroma/Misskey/Akkoma extension) carrying the literal emoji. Incoming `Like`/`EmojiReact` become `m.reaction` events from a ghost, and redacting a reaction in either direction sends or receives the matching `Undo`.
- **Reposts**: reacting with 🔁, or running bare `;repost` as a reply, sends a real `Announce` (not a Like) to both your followers and the original author, plus a "🔁 you reposted" card in your own Profile Room. An incoming `Announce` renders the same card and independently imports the reposted post into its original author's own room, so it's reply/react-able there too. Un-reposting (redacting the reaction, the command, or the card, all three are linked) sends `Undo(Announce)`.
- **Custom emoji**: Pleroma/Misskey/Akkoma image-emoji shortcodes (in post text, reactions, or display names) are resolved against the object's own metadata to an uploaded `mxc://` image and inlined next to the shortcode text, in both directions.
- **MSC4027 custom-image reactions**: reacting with a Matrix image pack (a custom-image reaction, animated GIFs included) always federates out as a custom-emoji `EmojiReact` with a real Emoji tag. The reverse -- mirroring an incoming custom-emoji reaction as an actual MSC4027 image reaction instead of `:shortcode:` text -- is opt-in (`bridge.msc4027_custom_reactions`, off by default, since it changes what non-MSC4027 clients render).

## Polls

- Polls are bridged bidirectionally, as real Matrix poll widgets (MSC3381), not text. A Profile Room poll becomes a `Create{Question}`; an incoming poll from a followed account is mirrored the same way a post is, but as an actual interactive poll.
- Voting either direction works: a local vote on a mirrored (remote-owned) poll federates out as a private vote to the poll's author; a remote vote on your own poll makes that voter's ghost cast a real Matrix poll response, so Matrix's own widget tallies it alongside everyone else's.
- A mirrored poll's results are shown as a "Fediverse Tallies" thread reply, seeded from whatever the poll already shows at import time and actively refreshed after a vote or via `;refresh poll` -- some remote servers (confirmed for Pleroma/Akkoma) never push a live tally update over federation at all, so this doesn't rely solely on one arriving.
- An incoming poll closing is mirrored as a real poll-end event in the same thread.

## Direct messages and chats

- **DMs** (`;dm`): a private, non-public `Create{Note}` addressed only to the recipient, in a dedicated ghost DM room. Threaded if replying within that same room.
- **Chats** (`;chat`): Pleroma/Akkoma's separate `ChatMessage` concept, in its own dedicated room type, flat with no threading. Also started by simply inviting a ghost into a fresh Matrix DM directly.
- Both directions mirror faithfully. An inbound private `Note`/`ChatMessage` lands in the matching room type, and a Matrix message sent there federates back out the same way.

## Mentions

- A Matrix client's structured mention (`m.mentions`) of a ghost or fellow local user is rewritten to `@user@domain` text and added as a real AS2 `Mention` tag on the way out. A hand-typed `@user@instance.org` is resolved the same way via WebFinger.
- Incoming `Mention` tags are matched by handle, not raw URL since they routinely differ, against known ghosts/local users and rendered as genuine Matrix mention pills.

## Media

- Incoming attachments (image, video, audio, document) are downloaded and re-uploaded as real Matrix media. Extra attachments beyond the first are appended as links, since one ActivityPub post maps to exactly one Matrix event.
- Outgoing Matrix media becomes an ActivityPub `attachment` pointing at the bridge's own public media proxy (`/media/{server}/{id}`), never your homeserver directly, since remote servers have no Matrix access token. Only media explicitly published this way is ever served.

## Follows and moderation

- **Follows**: `;follow` sends a signed `Follow`. An incoming `Follow` is auto-accepted (or auto-rejected if the follower is blocked), recorded, and reflected as a ghost invite into your Profile Room, so room membership doubles as your visible follower list.
- **Unfollow**: leaving or being kicked from a Remote User Room sends a real `Undo(Follow)` under your own identity. An incoming `Undo(Follow)` just drops the follower record.
- **Block**: cuts any existing follow (a real `Undo(Follow)` if you were following them), kicks you from their rooms, declines future follows with a real `Reject`, and silences notifications, all in one command. An incoming `Block` surfaces as a notification and silently drops the follower record, since AP has no separate unfollow signal for a block.
- **Mute**: suppresses notifications and auto-invites from an account without touching the follow relationship or their content.
- **Followers/following visibility**: `;hide`/`;show` withholds just the member list of your public collections. Counts stay public either way, matching Mastodon's "hide network" convention.

## Profile and identity

- Linking or creating a profile mints an ActivityPub `Actor` document with its own RSA keypair. Your room's name, topic, and avatar map to the actor's name, bio, and icon, pushed live as `Update` on every change.
- `;banner` sets the actor's header image via `m.room.banner` (MSC4221, currently under its own unstable prefix since Matrix has no stable room banner concept yet).
- MSC4501 discoverability: a linked Profile Room gets `m.social.profile_user_id` (asserting who it actually belongs to, since the bridge's bot is always its technical creator) with a power level requiring the room's owner to change it, and a ghost's Remote User Room gets `m.social.profile_room` (a `{room_id, via}` block) set on their own Matrix profile pointing back at it. Both stay in sync across a `;replace room`.
- An incoming profile change (`Update{Person}`) syncs the Remote User Room's name/avatar/banner and the ghost's own Matrix profile.
- `;replace room` recreates the Matrix room behind any identity (Profile Room, Remote User Room, DM, or Chat room) to pick up newer bridge features. It's entirely Matrix-side; nothing is sent over ActivityPub, since the identity itself doesn't change.
- `;delete profile` is a confirmation-gated, irreversible account deletion. It sends a signed `Delete` to every follower, then erases the local identity.

## PeerTube video channels

Opt-in (`bridge.peertube_channels_enabled`, off by default -- every remote view/scrub of a hosted video streams through the bridge's own media proxy with no caching layer, a real bandwidth cost to weigh first).

- **Publishing**: `;create channel <id>` turns a Matrix room into a real, federating PeerTube-compatible video channel -- a `Group`-typed ActivityPub actor with its own keypair, owned by an already-linked Profile. Reply to an uploaded video with `;publish` (`key: value` metadata lines -- name/category/license/language/tags/sensitive/commentsEnabled -- then a blank line and description) to federate it as a real PeerTube `Video`, delivered as `Create` to the owner's own followers and `Announce` to the channel's, matching PeerTube's own publish choreography.
- `;edit` changes a published video's metadata without touching the file. `;replace video` swaps the underlying file while keeping the same video identity. `;unpublish` retracts it from the fediverse; the Matrix message itself is untouched, so it can be re-published later.
- A video's reactions support a real `Dislike` (👎) alongside the usual `Like`/`EmojiReact`, and inbound `View` activities from remote instances are tallied into a durable view count. Comments (Matrix thread replies) federate as `Note`s the same way an ordinary post's replies do, and incoming comment content renders Markdown, PeerTube's own convention there.
- Playback goes through the bridge's media proxy with extension-aware URLs and CORS preflight handling, so a remote video player can actually infer a playable file type and stream byte-range requests.
- **Following**: `;follow` a remote PeerTube channel or account exactly like any other fediverse actor -- its videos show up as posts in your Remote User Room the same way an ordinary account's Notes do, and `;backfill` pulls its history in the same way too. Whether a followed video is downloaded and re-hosted natively or just linked back to its original instance as a card is controlled by `bridge.follow_video_media_mode` (`"link"`/`"embed"`, default `"link"`, falling back to a link card when the file is bigger than `bridge.follow_video_max_embed_mb`).

## Guilds and channels (Shoot)

- `;joinguild CODE@guild.example.com` joins a Shoot guild (an `Organization` actor) using an invite code, per FEP-bebd. The guild's `Accept` arrives later over the inbox, so this only sends and records the join request rather than confirming membership immediately.
- A joined guild gets its own Matrix Space, with each of its text channels mirrored into a child Matrix room -- immediately for a channel already known when the guild was joined, or the first time anyone posts in a newer one otherwise.
- Channel messages mirror bidirectionally: an incoming channel message is attributed to whichever guild member actually wrote it (never the channel actor itself), and an outgoing Matrix message sent in a channel room federates back out the same way.
- `;leaveguild`, run inside one of that guild's own channel rooms, sends a real `Undo(Follow)` and drops the bridge's own local membership tracking.
- `;refresh guild` re-syncs a guild's channel list by hand -- Shoot doesn't federate channel-creation events at all, so a channel created after the guild was joined is otherwise only discovered once someone actually posts in it.
- `;refresh guild invite CODE@domain` (admin-only) stores an invite code on the guild's own Space. From then on, a local Matrix user who joins that Space or any of its Channel rooms is automatically joined to the guild over ActivityPub too, with no `;joinguild` needed -- an unlinked user is DMed to link a profile first instead of silently failing, and a failed auto-join (bad/expired code, unreachable guild) is DMed rather than dropped. Running it also immediately resyncs any current Space member who's missing a real accepted Follow, covering both a stale code being replaced and anyone who joined before a code was ever set.

## Discovery and federation plumbing

- **HTTP Signatures** on every outbound activity and every inbound delivery, including cross-checking the signing key's actor against the activity's own claimed actor to reject spoofing.
- **WebFinger**, both serving local identities for remote discovery and resolving remote handles for outgoing follows, mentions, and DMs.
- **Outbox/followers/following/actor pages** are all served live, reconstructed from actual Matrix room state and history on every request, never cached separately, in keeping with the project's philosophy that Matrix is the only place content lives.
- A **shared inbox** endpoint, plus per-actor inboxes, both signature-verified.
- **Knock-based self-service room access**: every bridge room uses Matrix's `knock` join rule, and a knock from that room's rightful local owner (current or a past, since-replaced room) is auto-accepted without side effects.

For the exact ActivityPub activity types, Matrix event shapes, and implementation entry points behind each of these, see the source: `bridge/inbox_dispatch.py` for incoming, `bridge/note_mirroring.py`/`bridge/reply_bridge.py`/`bridge/reaction_bridge.py`/`bridge/edit_bridge.py`/`bridge/chat_bridge.py` for outgoing, `bridge/activitypub/routes.py` for the ActivityPub HTTP surface the bridge itself serves, `bridge/peertube.py`/`bridge/commands.py` for video channels, and `bridge/channel_bridge.py`/`bridge/spaces.py` for Shoot guilds and channels.
