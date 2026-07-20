# matrix-appservice-activitypub

![A Matrix room mirroring the Free Software Foundation's Mastodon account side-by-side with the actual Mastodon profile, showing matching posts](screenshots/screenshot1.png)

Turns your Matrix server into a fully functioning ActivityPub server. Matrix  users can post, follow, reply, react, and DM across the fediverse using ordinary Matrix rooms and clients. No separate account or server needed.

Runs natively as a single Python process, no containers required. It talks to your homeserver through the Client-Server API (plus the Application Service push API for inbound events), never by touching its database directly. It also stores no post content or media of its own; that all lives in Matrix rooms. The only local state is bookkeeping (linked identities, follow relationships, keys, and the Matrix-event/ActivityPub-object map), kept in a SQLite file or a Postgres database.

## Core concepts

Every ActivityPub identity or conversation the bridge manages is backed by an ordinary Matrix room, of one of five kinds:

- **Profile Room**: a local Matrix user's own linked ActivityPub identity (`username@bridge.domain`). Posting here publishes to the fediverse, and the room's membership doubles as a visible follower list.
- **Remote User Room**: one shared room per remote fediverse account, mirroring everything they post. Created the first time anyone follows or imports from that account, and reused by every local follower after that.
- **Ghost DM room**: a private 1:1 room between a local user and a remote account, carrying ActivityPub `Note`-based direct messages.
- **Ghost Chat room**: a private 1:1 room carrying ActivityPub `ChatMessage`s (Pleroma/Akkoma's separate instant-messaging concept). Deliberately never the same room as a DM, even between the same two parties.
- **Notification room**: a private 1:1 room between a local user and the bridge bot itself, named "Fediverse Notifications". Notification messages for new followers, mentions, reposts, and likes/reactions land here.

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
- Discovery and federation plumbing

See [FEATURES.md](FEATURES.md) for a breakdown of how each of these is actually bridged.

## Controlling the bridge

Everything above is controlled from inside Matrix, two ways: tagging the bridge bot or typing a `;`-prefixed command (`;follow @user@instance.org`, `;help`, and so on), or the room widget the bridge automatically adds to every room it creates, with buttons for most of those same commands. See [COMMANDS.md](COMMANDS.md) for the complete reference, including the widget's own entry.

## What homeservers are supported?

Synapse is the only homeserver this bridge has actually been tested against. It should work against any other spec-compliant homeserver in theory, with one exception: `bridge.use_synapse_admin_api` (on by default) depends on Synapse's own Admin API, which isn't part of the spec and other implementations aren't guaranteed to have. Turn it off if you're not running Synapse.

Running on other homeservers is untested, experimental territory as of this writing. When turning `bridge.use_synapse_admin_api` off: populate `bridge.admins` (see `config.example.yaml`), since admin status no longer falls back to a Synapse API check. As soon as the bridge is started up, spot-test every command by hand rather than assuming it behaves the same as the Synapse-backed path; and watch the bridge's logs closely to make sure you're not getting any unexpected errors.

## Setup

1. Install dependencies and generate a config:

   ```sh
   ./scripts/setup.sh
   ```

   This creates a virtualenv, installs `requirements.txt`, and generates `config.yaml` from `config.example.yaml` with fresh random AppService tokens.

2. Edit `config.yaml`. At minimum set `bridge.domain`, `bridge.public_base_url`, `synapse.base_url`, and `synapse.server_name` (these fields are named after Synapse, but just mean "your homeserver" -- see "What homeservers are supported?" above). Also set `synapse.admin_token` (an access token for a homeserver account with `admin: true`) unless you've explicitly turned off `bridge.use_synapse_admin_api`. See `config.example.yaml` for every option; each is documented inline (storage backend, logging level, federation timeouts, backfill defaults, and more).

3. Generate the AppService registration and wire it into your homeserver:

   ```sh
   .venv/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
   ```

   Add the resulting file's path to your homeserver's own application-service registration config (`app_service_config_files` in `homeserver.yaml`, if you're running Synapse), then restart it.

4. Run the bridge:

   ```sh
   .venv/bin/python main.py
   ```

   Or install `deploy/matrix-appservice-activitypub.service` to run it under systemd (see that file for the expected user/paths). `deploy/nginx.conf.example` shows a reverse-proxy config for exposing the ActivityPub surface on the same public domain as your homeserver. That's recommended, since it's what makes a user's Matrix ID and fediverse handle the exact same string (`@alice:example.org` == `@alice@example.org`).

5. Optionally, verify end-to-end:

   ```sh
   .venv/bin/python scripts/simulate_remote_follow.py --username <a-linked-profile>
   ```

## Storage

Bookkeeping (linked identities and their keys, follow relationships, ghost profiles, the Matrix-event/ActivityPub-object map, and room-history tables used for knock/backfill/outbox continuity after a `;replace room`) lives in either a local SQLite file (`storage.backend: sqlite`, the default) or Postgres (`storage.backend: postgresql`), selected in `config.yaml`. Same schema and content either way. No post content or media is stored outside Matrix itself.
