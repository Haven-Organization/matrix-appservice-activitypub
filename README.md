# matrix-appservice-activitypub

![A Matrix room mirroring the Free Software Foundation's Mastodon account side-by-side with the actual Mastodon profile, showing matching posts](screenshots/screenshot1.png)

Turns your Matrix server into a fully functioning ActivityPub server. Matrix  users can post, follow, reply, react, and DM across the fediverse using ordinary Matrix rooms and clients. No separate account or server needed.

Runs natively as a single Python process, no containers required. It talks to your homeserver through the Client-Server API (plus the Application Service push API for inbound events), never by touching its database directly. It also stores no post content or media of its own; that all lives in Matrix rooms. The only local state is bookkeeping (linked identities, follow relationships, keys, and the Matrix-event/ActivityPub-object map), kept in a SQLite file or a Postgres database.

## Core concepts

Every ActivityPub identity or conversation the bridge manages is backed by an ordinary Matrix room, of one of these kinds:

- **Profile Room**: a local Matrix user's own linked ActivityPub identity (`username@bridge.domain`). Posting here publishes to the fediverse, and the room's membership doubles as a visible follower list.
- **Remote User Room**: one shared room per remote fediverse account, mirroring everything they post. Created the first time anyone follows or imports from that account, and reused by every local follower after that.
- **Ghost DM room**: a private 1:1 room between a local user and a remote account, carrying ActivityPub `Note`-based direct messages.
- **Ghost Chat room**: a private 1:1 room carrying ActivityPub `ChatMessage`s (Pleroma/Akkoma's separate instant-messaging concept). Deliberately never the same room as a DM, even between the same two parties.
- **Notification room**: a private 1:1 room between a local user and the bridge bot itself, named "Fediverse Notifications". Notification messages for new followers, mentions, reposts, and likes/reactions land here.
- **PeerTube Channel room** (opt-in, `bridge.peertube_channels_enabled`): a Matrix room turned into a real, federating PeerTube-compatible video channel with its own ActivityPub identity, owned by a linked Profile. Posting a video here and running `;publish` federates it to the fediverse.
- **Lemmy Community room**: either mirroring a remote Lemmy community you follow (opt-in, `bridge.lemmy_communities_enabled`), or one this bridge hosts itself as the community's authoritative home instance (opt-in separately, `bridge.lemmy_community_hosting_enabled`) -- a real, federating `Group`-typed ActivityPub actor either way. Posting here is an ordinary message (first line as the post title); replying adds a comment.
- **Guild Space**: a Matrix Space representing a joined Shoot guild (an `Organization` actor), created once the guild's `Accept` for a `;joinguild` (or auto-join) comes back.
- **Guild Channel room**: one of that guild's text channels, mirrored into a child room under its Guild Space -- created for a channel already known when the guild was joined, or the first time anyone posts in a newer one otherwise.

Every remote account you interact with gets a deterministic "ghost" Matrix user (`@ap_user_instance:yourdomain`) that posts, reacts, and DMs on their behalf inside Matrix. Its display name, avatar, and (for a Remote User Room) banner stay in sync with their real ActivityPub profile.

## What's bridged

- Posts, replies, and threads
- Reactions and reposts
- Polls
- Direct messages and chats
- Mentions
- Media
- Follows and moderation
- Profile and identity
- PeerTube video channels, publishing and following
- Guilds and channels (Shoot), joined via invite code or automatically by joining a Matrix room
- Discovery and federation plumbing

See [FEATURES.md](FEATURES.md) for a breakdown of how each of these is actually bridged.

## Controlling the bridge

Most of what's above just happens through ordinary Matrix actions: sending a message, reacting, replying, editing. For everything else, like following an account, starting a DM, or managing your profile, tag the bridge bot or type a `;`-prefixed command (`;follow @user@instance.org`, `;help`, and so on), or use the room widget the bridge automatically adds to every room it creates, with buttons for most of those same commands. See [COMMANDS.md](COMMANDS.md) for the complete reference, including the widget's own entry.

## What homeservers are supported?

Synapse is the only homeserver this bridge has actually been tested against. It should work against any other spec-compliant homeserver in theory, with one exception: `bridge.use_synapse_admin_api` (on by default) depends on Synapse's own Admin API, which isn't part of the spec and other implementations aren't guaranteed to have. Turn it off if you're not running Synapse.

Running on other homeservers is untested, experimental territory as of this writing. When turning `bridge.use_synapse_admin_api` off: populate `bridge.admins` (see [CONFIG.md](CONFIG.md)), since admin status no longer falls back to a Synapse API check. As soon as the bridge is started up, spot-test every command by hand rather than assuming it behaves the same as the Synapse-backed path; and watch the bridge's logs closely to make sure you're not getting any unexpected errors.

## Setup

See [INSTALL.md](INSTALL.md) for the full walkthrough, natively or in Docker. In short: `./scripts/setup.sh` (or the `Dockerfile`), edit `config.yaml` (from `config.example.yaml`), register the AppService with your homeserver, then run the bridge.

`config.example.yaml` only has the settings you need to start. Every other setting is documented in [CONFIG.md](CONFIG.md).

## Storage

Bookkeeping (linked identities and their keys, follow relationships, ghost profiles, the Matrix-event/ActivityPub-object map, and room-history tables used for knock/backfill/outbox continuity after a `;replace room`) lives in either a local SQLite file (`storage.backend: sqlite`, the default) or Postgres (`storage.backend: postgresql`, see [CONFIG.md](CONFIG.md#storage) for its connection settings), selected in `config.yaml`. Same schema and content either way. No post content or media is stored outside Matrix itself.

## MSC4501 Compliance

[MSC4501](https://github.com/matrix-org/matrix-spec-proposals/pull/4501) ("Social Media on Matrix") is a Matrix spec proposal for representing fediverse-style social media, such as posts, reposts, replies, and profiles, as native Matrix concepts, so a social timeline can be built directly on top of ordinary Matrix rooms and events. This bridge makes use of MSC4501's types and concepts wherever it can. Mirrored posts, reposts, quote-posts, and cross-posted reply echoes carry `org.matrix.msc4501.social.relates_to`, and Profile Rooms and Remote User Rooms are tagged and cross-linked via `m.social.profile_room`/`m.social.profile_user_id`, among other things. See [FEATURES.md](FEATURES.md) for the full breakdown and [CONFIG.md](CONFIG.md#msc4501-and-related-proposals) for the settings that control it.

<p align="center">
  <img src="screenshots/haven-desktop-feed.png" alt="The Social feed in Haven, showing posts from followed Fediverse and Matrix accounts" width="280">
  <img src="screenshots/haven-desktop-profile.png" alt="A Social profile page in Haven, showing a Fediverse account bridged into Matrix" width="280">
</p>

Any ordinary Matrix client can be used with this bridge; nothing about it requires MSC4501 support. It's best experienced, though, with an MSC4501-compatible client, which renders that extra structure as an actual social-media-style timeline and profile instead of a plain chat room. [Haven Desktop](https://github.com/Haven-Organization/haven-desktop) is one such client.
