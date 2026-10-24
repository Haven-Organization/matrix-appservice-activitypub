# Configuration reference

`config.example.yaml` holds only the settings you need to get going. Everything else has a default and is listed here. To change one, add it to your `config.yaml` under the section named in its heading, for example:

```yaml
bridge:
  backfill_default_count: 30

federation:
  max_backdate_days: 365
```

The bridge reads `config.yaml` once, at startup, so restart it after a change. The path is the first argument to `main.py`, else the `BRIDGE_CONFIG` environment variable, else `./config.yaml`.

## Contents

- [In the sample config](#in-the-sample-config)
- [`bridge`](#bridge)
  - [Access and networking](#access-and-networking)
    - [Internal base URL (`bridge.internal_base_url`)](#bridge-internal_base_url)
  - [Discovery](#discovery)
    - [Instance admin username (`bridge.instance_admin_username`)](#bridge-instance_admin_username)
  - [Rooms and knocking](#rooms-and-knocking)
    - [Remote User Room join rule (`bridge.ghost_room_join_rule`)](#bridge-ghost_room_join_rule)
    - [Profile Room join rule (`bridge.local_profile_room_join_rule`)](#bridge-local_profile_room_join_rule)
    - [World-readable remote rooms (`bridge.world_readable_remote_rooms`)](#bridge-world_readable_remote_rooms)
    - [Accept federated knocks (`bridge.accept_federated_knocks`)](#bridge-accept_federated_knocks)
    - [Default backfill count (`bridge.backfill_default_count`)](#bridge-backfill_default_count)
  - [Replies](#replies)
    - [Reply cross-posting (`bridge.reply_cross_post_mode`)](#bridge-reply_cross_post_mode)
  - [Posts and polls](#posts-and-polls)
    - [Poll duration (`bridge.poll_default_duration_days`)](#bridge-poll_default_duration_days)
  - [Third-party access](#third-party-access)
    - [Admins (`bridge.admins`)](#bridge-admins)
    - [Use Synapse Admin API (`bridge.use_synapse_admin_api`)](#bridge-use_synapse_admin_api)
    - [Third-party access mode (`bridge.third_party_access_mode`)](#bridge-third_party_access_mode)
  - [MSC4501 and related proposals](#msc4501-and-related-proposals)
    - [Set MSC4501 relates_to (`bridge.set_msc4501_relates_to`)](#bridge-set_msc4501_relates_to)
    - [MSC4501 content_inline for reposts (`bridge.use_msc4501_content_inline`)](#bridge-use_msc4501_content_inline)
    - [Quote-post import policy (`bridge.quote_import_policy`)](#bridge-quote_import_policy)
    - [MSC4501 post event type (`bridge.use_msc4501_post_event_type`)](#bridge-use_msc4501_post_event_type)
    - [MSC4144 per-message profiles (`bridge.msc4144_per_message_profiles`)](#bridge-msc4144_per_message_profiles)
    - [MSC4501 profile room (`bridge.set_msc4501_profile_room`)](#bridge-set_msc4501_profile_room)
    - [MSC4503 external handle (`bridge.msc4503_external_handle`)](#bridge-msc4503_external_handle)
    - [MSC4027 custom reactions (`bridge.msc4027_custom_reactions`)](#bridge-msc4027_custom_reactions)
  - [PeerTube video channels](#peertube-video-channels)
    - [Enable PeerTube channels (`bridge.peertube_channels_enabled`)](#bridge-peertube_channels_enabled)
    - [Followed video media mode (`bridge.follow_video_media_mode`)](#bridge-follow_video_media_mode)
    - [Max embedded video size (`bridge.follow_video_max_embed_mb`)](#bridge-follow_video_max_embed_mb)
  - [Multi-identity support](#multi-identity-support)
    - [Enable custom identities (`bridge.custom_identities_enabled`)](#bridge-custom_identities_enabled)
    - [Max identities per user (`bridge.max_identities_per_user`)](#bridge-max_identities_per_user)
    - [Default timeline domains (`bridge.default_timeline_domains`)](#bridge-default_timeline_domains)
  - [Multi-bridge rooms](#multi-bridge-rooms)
    - [Command prefix (`bridge.command_prefix`)](#bridge-command_prefix)
    - [Other bots' prefixes (`bridge.other_bot_prefixes`)](#bridge-other_bot_prefixes)
    - [Other bridges' users (`bridge.other_bridge_users`)](#bridge-other_bridge_users)
- [`synapse`](#synapse)
  - [Admin token (`synapse.admin_token`)](#synapse-admin_token)
- [`storage`](#storage)
  - [Storage backend (`storage.backend`)](#storage-backend)
  - [Postgres connection (`storage.postgres`)](#storage-postgres)
- [`federation`](#federation)
  - [Request timeout (`federation.request_timeout`)](#federation-request_timeout)
  - [Actor key cache TTL (`federation.actor_key_cache_ttl`)](#federation-actor_key_cache_ttl)
  - [Max clock skew (`federation.max_clock_skew`)](#federation-max_clock_skew)
  - [Max backdate days (`federation.max_backdate_days`)](#federation-max_backdate_days)
- [`logging`](#logging)
  - [Log level (`logging.level`)](#logging-level)

---

## In the sample config

These are documented inline in `config.example.yaml` and only summarised here.

| Setting | Default | What it is |
|---|---|---|
| `bridge.domain` | required | Public domain used to mint ActivityPub actor IDs (`https://<domain>/actor/<user>`). |
| `bridge.public_base_url` | required | Base URL this bridge is publicly reachable at. |
| `bridge.listen_host` / `listen_port` | `127.0.0.1` / `8090` | Where the bridge's own HTTP server binds. |
| `synapse.base_url` | required | Homeserver Client-Server/Admin API, as reachable from the bridge. |
| `synapse.server_name` | required | The homeserver's `server_name`. |
| `synapse.admin_token` | required unless `use_synapse_admin_api` is off | Access token for an `admin: true` account. |
| `appservice.registration_path`, `id`, `hs_token`, `as_token` | required | AppService identity and its two secrets. |
| `appservice.user_prefix` | `ap_` | Username prefix of ghost users. Changing it later orphans existing ghosts and needs the registration file regenerated. |
| `appservice.bot_localpart` | `bridgebot` | The bot's username. |
| `appservice.bot_display_name` / `bot_avatar_mxc` | unset (not managed) | The bot's own profile, applied at startup. |
| `storage.backend` | `sqlite` | `sqlite` or `postgresql`. See [`storage`](#storage). |
| `storage.data_dir` | `./data` | Where `bridge.db` (and its keys) is kept, for the `sqlite` backend. |
| `logging.level` | `ERROR` | Log level. |

---

## `bridge`

### Access and networking

<a id="bridge-internal_base_url"></a>

#### Internal base URL (`bridge.internal_base_url`)

**Default:** `http://<listen_host>:<listen_port>`

The address Synapse pushes AppService transactions to. Only set this if Synapse and the bridge are on different hosts (or separate Docker networks) and can't reach each other at the default directly; that traffic never needs to leave the host/trusted network otherwise.

---

### Discovery

<a id="bridge-instance_admin_username"></a>

#### Instance admin username (`bridge.instance_admin_username`)

**Default:** unset

Local username (not a full MXID) of an already-registered local actor to advertise as this instance's admin contact in the Mastodon-compatible `/api/v1/instance` document, which directory/crawler sites (fedidb.com, fedilist.com) read alongside NodeInfo. Leave unset to omit `contact_account` entirely.

---

### Rooms and knocking

<a id="bridge-ghost_room_join_rule"></a>

#### Remote User Room join rule (`bridge.ghost_room_join_rule`)

**Default:** `knock`. One of `knock`, `invite`, `public`.

Join rule for a new Remote User Room (a ghost's mirror of a remote fediverse account). `knock` lets anyone locked out (e.g. after a `;replace room`) ask their way back in without an admin.

---

<a id="bridge-local_profile_room_join_rule"></a>

#### Profile Room join rule (`bridge.local_profile_room_join_rule`)

**Default:** `knock`. Same values as above, but for a local user's own Profile Room.

---

<a id="bridge-world_readable_remote_rooms"></a>

#### World-readable remote rooms (`bridge.world_readable_remote_rooms`)

**Default:** `false`

Whether a Remote User Room (including a followed PeerTube channel's) gets `history_visibility: world_readable`, letting anyone on this homeserver preview it without joining or knocking (e.g. to check a repost's `social.relates_to` against its real source). Independent of `ghost_room_join_rule` above, which still governs joining itself. Run `;refresh` in an existing room to re-sync it to a changed value.

---

<a id="bridge-accept_federated_knocks"></a>

#### Accept federated knocks (`bridge.accept_federated_knocks`)

**Default:** `false`

Whether a knock from a user on a DIFFERENT homeserver is auto-accepted the same as a local user's. Off by default: mirrored posts are public fediverse content, but who gets auto-admitted into this server's rooms is this server's call. A federated user can always be invited manually either way.

---

<a id="bridge-backfill_default_count"></a>

#### Default backfill count (`bridge.backfill_default_count`)

**Default:** `15`

How many posts `;backfill` pulls with no explicit count given. A Matrix server admin can override this per-use with an explicit argument.

---

### Replies

<a id="bridge-reply_cross_post_mode"></a>

#### Reply cross-posting (`bridge.reply_cross_post_mode`)

**Default:** `followed`. One of `off`, `followed`, `always`.

Whether a remote actor's reply into a conversation that's ALREADY tracked here also gets an annotated "⤵️ Reply to X's post" copy posted into THEIR OWN Remote User Room, not just threaded into the parent post's own room:

- `off`: never.
- `followed` (default): only if the replier already has a Remote User Room here (i.e. is already followed/known); never provisions a new room/ghost just for this echo.
- `always`: echo regardless, provisioning a Remote User Room/ghost for the replier on demand if they don't have one yet. Can mint a room for every chatty remote account that ever replies to anything tracked here, followed or not, so weigh that before turning this on.

A reply whose conversation root had to be freshly imported here for the first time always gets this echo, regardless of this setting. That's a separate, long-standing fallback (predating this setting) that keeps a reply visible in the replier's own room the way it used to appear before threading existed for that case.

---

### Posts and polls

<a id="bridge-poll_default_duration_days"></a>

#### Poll duration (`bridge.poll_default_duration_days`)

**Default:** `7`

Matrix polls have no expiry; Mastodon-family servers refuse a poll with none. This many days after creation is sent as the poll's `endTime` instead.

---

### Third-party access

<a id="bridge-admins"></a>

#### Admins (`bridge.admins`)

**Default:** `[]`

Matrix user IDs treated as bridge admins regardless of real Synapse server-admin status. Always additive: a real Synapse admin not listed here still gets admin rights too, as long as `use_synapse_admin_api` is on.

```yaml
bridge:
  admins:
    - "@you:example.org"
```

---

<a id="bridge-use_synapse_admin_api"></a>

#### Use Synapse Admin API (`bridge.use_synapse_admin_api`)

**Default:** `true`

Whether the bridge is allowed to call Synapse's own Admin API, for checking whether another user is a server admin and for a couple of admin-only room sweeps. Turn this off to run against a homeserver that doesn't have (or won't grant) Synapse's Admin API. EXPERIMENTAL, only ever tested against Synapse itself.

Turning it off means admin status relies entirely on [`bridge.admins`](#bridge-admins) above (give it real entries, or nobody gets those commands), and `synapse.admin_token` is no longer required. Spot-test every admin-gated command and watch the bridge's logs after turning this off, rather than assuming it behaves the same as the Synapse-backed path.

---

<a id="bridge-third_party_access_mode"></a>

#### Third-party access mode (`bridge.third_party_access_mode`)

**Default:** `follow_only`. One of `follow_only`, `full`.

What a user on a DIFFERENT homeserver gets once an admin allows them (the `;allow`/`;disallow`/`;allowed` commands):

- `follow_only`: `;follow` and interact only. No Profile Room; their AP identity is minted automatically, always mirroring their live Matrix profile.
- `full`: identical to a local user, with self-service `;create profile`/`;link profile`, DM, Chat.

A single deployment-wide setting, applied live to everyone currently allowed, not fixed at the time they were first allowed. Flipping it is always safe and reversible: nothing about an already-provisioned identity is ever deleted or reassigned by the change alone.

---

### MSC4501 and related proposals

Behaviour each of these controls is described throughout [FEATURES.md](FEATURES.md); there's no single dedicated section, since MSC4501 touches most of what's bridged.

<a id="bridge-set_msc4501_relates_to"></a>

#### Set MSC4501 relates_to (`bridge.set_msc4501_relates_to`)

**Default:** `true`

Set `org.matrix.msc4501.social.relates_to` on every mirrored repost, quote-post, and cross-posted reply echo. Purely additive on an ordinary message; a client that's never heard of MSC4501 just ignores the extra field.

---

<a id="bridge-use_msc4501_content_inline"></a>

#### MSC4501 content_inline for reposts (`bridge.use_msc4501_content_inline`)

**Default:** `true`

For a mirrored repost specifically: assert `content_inline` (the event's own content already *is* the reposted post's content) instead of duplicating that content a second time into `relates_to.content`. Turn off for a client that doesn't handle `content_inline` yet.

---

<a id="bridge-quote_import_policy"></a>

#### Quote-post import policy (`bridge.quote_import_policy`)

**Default:** `known`. One of `known`, `always`, `never`.

Whether an untracked quote-post's TARGET gets imported into its own Remote User Room (needed for `relates_to` to populate on it at all):

- `known`: only if the quoted author already has a Remote User Room for some other reason.
- `always`: import unconditionally.
- `never`: read-only preview only, `relates_to` stays unset.

A quote is often adversarial ("quote-dunking"), so `always` means provisioning a room for a total stranger purely because someone followed here quoted them.

---

<a id="bridge-use_msc4501_post_event_type"></a>

#### MSC4501 post event type (`bridge.use_msc4501_post_event_type`)

**Default:** `false`

Mirror a remote account's posts using `org.matrix.msc4501.social.post` as the event TYPE, instead of `m.room.message`. **Not recommended** until Phase 2 of MSC4501's own rollout plan: unlike the additive fields above, a client that's never heard of MSC4501 won't render the event at all.

---

<a id="bridge-msc4144_per_message_profiles"></a>

#### MSC4144 per-message profiles (`bridge.msc4144_per_message_profiles`)

**Default:** `true`

Adds an MSC4144 per-message profile (id/displayname/avatar_url from the user's Profile Room) to a bridge-sent message acting on their behalf, like the repost card, so it visually reads as their own action. Ignored by clients that don't know MSC4144.

---

<a id="bridge-set_msc4501_profile_room"></a>

#### MSC4501 profile room (`bridge.set_msc4501_profile_room`)

**Default:** `false`

Set `org.matrix.msc4501.social.profile_room` (MSC4133 Extensible Profiles) on every ghost, pointing at its room. Needs `experimental_features.msc4133_enabled: true` in Synapse's `homeserver.yaml` first (off even there by default). Leave this off until that's done, or every ghost registration eats a guaranteed-failing request. An admin can force an existing ghost's field into line via `;refresh`.

---

<a id="bridge-msc4503_external_handle"></a>

#### MSC4503 external handle (`bridge.msc4503_external_handle`)

**Default:** `off`. One of `off`, `profile`, `events`, `both`.

Where each ghost's real ActivityPub handle (e.g. `@alice@mastodon.social`) is recorded (MSC4503): as a field on its Matrix profile (`profile`, same MSC4133 requirement as above), as a snapshot on every event it sends (`events`), or `both`.

---

<a id="bridge-msc4027_custom_reactions"></a>

#### MSC4027 custom reactions (`bridge.msc4027_custom_reactions`)

**Default:** `false`

Mirror an inbound custom-emoji reaction as a real MSC4027 custom-image reaction instead of plain `:shortcode:` text. Off by default: unlike the additive fields above, this replaces the one field every client renders, so a client that's never heard of MSC4027 shows the raw `mxc://...` string instead of the shortcode text it showed before. Only turn this on once you know your users' clients support it. Reacting with a custom image FROM Matrix always federates out as a custom-emoji reaction regardless of this setting.

---

### PeerTube video channels

<a id="bridge-peertube_channels_enabled"></a>

#### Enable PeerTube channels (`bridge.peertube_channels_enabled`)

**Default:** `false`

Whether a Matrix room can become a real, federating PeerTube-compatible video channel (`;create channel`, `;publish`, and friends; see [FEATURES.md](FEATURES.md#peertube-video-channels-publishing-and-following)). Off by default: a real behavior change, not a safely-ignorable additive field. Every remote view/scrub of a hosted video streams through this bridge's own media proxy with no caching layer, a much heavier bandwidth pattern per view than the images that proxy already serves, so weigh that cost for your own deployment before turning this on.

---

<a id="bridge-follow_video_media_mode"></a>

#### Followed video media mode (`bridge.follow_video_media_mode`)

**Default:** `link`. One of `link`, `embed`.

How a followed PeerTube channel/account's videos show up as posts. `link` always shows a text/thumbnail card linking back to the original instance, never downloading the video. `embed` tries to download and re-host the video natively, falling back to a `link`-style card when the file is bigger than `follow_video_max_embed_mb` below. Defaults to `link` for the same bandwidth/storage reasoning as `peertube_channels_enabled` above.

---

<a id="bridge-follow_video_max_embed_mb"></a>

#### Max embedded video size (`bridge.follow_video_max_embed_mb`)

**Default:** `100`

Only consulted in `embed` mode, checked against the video's own declared file size before ever starting a download.

---

### Multi-identity support

<a id="bridge-custom_identities_enabled"></a>

#### Enable custom identities (`bridge.custom_identities_enabled`)

**Default:** `false`

Whether a Matrix user may own more than one ActivityPub identity, and pick a custom id for one instead of always being stuck with their Matrix localpart (`;create profile <id>`, `;id`, and friends; see [FEATURES.md](FEATURES.md)). Off by default: with this off, the bridge behaves exactly as it always has -- one identity per Matrix user, username always equal to the localpart, no selection/switching commands exposed at all.

---

<a id="bridge-max_identities_per_user"></a>

#### Max identities per user (`bridge.max_identities_per_user`)

**Default:** `5`

Per-user cap on how many ActivityPub identities they may create once [`bridge.custom_identities_enabled`](#bridge-custom_identities_enabled) is on. A bridge admin (see [`bridge.admins`](#bridge-admins) / [`bridge.use_synapse_admin_api`](#bridge-use_synapse_admin_api)) is never subject to this cap.

---

<a id="bridge-default_timeline_domains"></a>

#### Default timeline domains (`bridge.default_timeline_domains`)

**Default:** `[]` (falls back to just this bridge's own domain)

Instance domain(s) whose "whole known network" (federated) timeline the widget's Home tab shows by default -- a single popular instance, or several merged together by recency. Only instances running Mastodon-API-compatible software (Mastodon, Pleroma/Akkoma, GoToSocial, and Sharkey -- the one Misskey-family fork that also ships a real Mastodon-API-compatible layer) can supply one; a bare ActivityPub server (including another install of this bridge) or any other Misskey-family instance (Misskey itself, Calckey, Firefish, Iceshrimp) has no site-wide timeline to browse at all, and is silently skipped.

---

### Multi-bridge rooms

Settings for sharing a Matrix room with other bots/bridges -- e.g. a room that's simultaneously an ActivityPub, Bluesky (atproto), and Nostr identity, each bridged separately into the same room. See [FEATURES.md](FEATURES.md) for the full picture of what's safe to share and what isn't.

<a id="bridge-command_prefix"></a>

#### Command prefix (`bridge.command_prefix`)

**Default:** `;`

The character(s) that start a bot command (`;follow ...`, `;help`, and so on). Change this if `;` collides with another bot/bridge sharing the same room -- e.g. set it to something that bot doesn't also use. Must not be empty.

---

<a id="bridge-other_bot_prefixes"></a>

#### Other bots' prefixes (`bridge.other_bot_prefixes`)

**Default:** `[]` (empty)

Other bots'/bridges' own command prefixes (e.g. `["!", "nostr:"]`), for a room shared with other bridges or Matrix bots -- entries come in two shapes, matched differently:

- A short/punctuation-style prefix (`!`, `]`, `nostr:`, ...) matches only at the very start of a message's first line, same as this bridge's own `;` prefix -- not matched anywhere in the message, since a common character like `!` would otherwise show up in ordinary prose by coincidence and silently swallow real posts.
- A full Matrix ID (e.g. `@bluesky:yourdomain`, `@fediverse:yourdomain` -- that bridge's own bot) is treated as a real mention of that bot: a proper intentional mention (most clients' own plain-text fallback for a mention pill is the mentioned user's display name, not their raw mxid, so this is the only reliable signal for an actual pill) OR the literal mxid typed anywhere in the message. Matched ANYWHERE, not just the start, since a full mxid is specific enough that it won't appear in ordinary prose by coincidence.

Either way, a matching message is never federated out by this bridge as an ordinary post, even though (correctly) it's also never treated as addressed to THIS bridge's own bot -- no reply is sent, and no attempt is made to parse it as one of this bridge's own commands. Without this, a command meant for a different bridge would fall straight through this bridge's own "not one of my commands" check and get posted to the fediverse as if it were ordinary content, since this bridge has no way to otherwise recognize another bot's own keyword vocabulary or mention conventions. Leave empty if this room isn't shared with anything else.

---

<a id="bridge-other_bridge_users"></a>

#### Other bridges' users (`bridge.other_bridge_users`)

**Default:** `[]` (empty)

Regex patterns matching OTHER bridges' own ghost/bot mxid namespaces sharing a room with this one -- e.g. `@bsky_.*:yourdomain` for a Bluesky bridge's ghosts, `@nostr:yourdomain` for its bot. Each pattern is matched against the WHOLE mxid (a full match, not a search), so write a pattern that describes the entire string, not just part of it.

This is a different problem from [`bridge.other_bot_prefixes`](#bridge-other_bot_prefixes) above: that one recognizes another bridge's *commands* so they aren't federated as posts; this one recognizes another bridge's *ghosts* (puppet accounts for remote users, not real humans) so a message/reaction/vote/membership-event from one of them is never mistaken for "a genuine local Matrix user who just hasn't linked a profile yet." Without this, every post, reaction, or vote a sibling bridge mirrors into a shared room would make this bridge try to resolve a linked profile for that ghost, fail, and send a "link a profile first" notice into the room -- which that ghost obviously can never act on, repeated on every single message. Leave empty if this room isn't shared with anything else. Matches the same convention already used for this by the sibling Bluesky/Nostr bridges sharing the same homeserver, for exactly the same reason.

---

## `synapse`

<a id="synapse-admin_token"></a>

#### Admin token (`synapse.admin_token`)

**Default:** unset

Access token for an account with `admin: true`, used for Synapse's Admin API. Required unless [`bridge.use_synapse_admin_api`](#bridge-use_synapse_admin_api) is turned off.

---

## `storage`

Bookkeeping (linked identities and their keys, follow relationships, ghost profiles, the Matrix-event/ActivityPub-object map, and room-history tables used for knock/backfill/outbox continuity after a `;replace room`) is kept in one of two places. Same schema and content either way; no post content or media is stored in either.

<a id="storage-backend"></a>

#### Storage backend (`storage.backend`)

**Default:** `sqlite`. One of `sqlite`, `postgresql`.

- `sqlite`: a single file, `bridge.db`, under `storage.data_dir`. Nothing to set up.
- `postgresql`: the Postgres server described by `storage.postgres` below. Useful if you already run Postgres for something else, such as your homeserver.

Switching backends on an existing deployment starts that backend's storage empty; there is no automatic migration between them.

---

<a id="storage-postgres"></a>

#### Postgres connection (`storage.postgres`)

Only read when the backend is `postgresql`. Either give a full connection string, or the separate fields; `dsn` wins if both are present.

```yaml
storage:
  backend: postgresql
  postgres:
    # dsn: "postgresql://matrix_appservice_activitypub:replace_me@localhost:5432/matrix_appservice_activitypub"
    host: localhost
    port: 5432
    database: matrix_appservice_activitypub
    user: matrix_appservice_activitypub
    password: "replace_me"
    min_pool_size: 1
    max_pool_size: 10
```

| Setting | Default |
|---|---|
| `storage.postgres.dsn` | unset |
| `storage.postgres.host` | `localhost` |
| `storage.postgres.port` | `5432` |
| `storage.postgres.database` | `matrix_appservice_activitypub` |
| `storage.postgres.user` | `matrix_appservice_activitypub` |
| `storage.postgres.password` | unset |
| `storage.postgres.min_pool_size` | `1` |
| `storage.postgres.max_pool_size` | `10` |

Create the role and an empty database first; the bridge creates its own tables on startup:

```sh
sudo -u postgres createuser --pwprompt matrix_appservice_activitypub
sudo -u postgres createdb --owner matrix_appservice_activitypub matrix_appservice_activitypub
```

---

## `federation`

<a id="federation-request_timeout"></a>

#### Request timeout (`federation.request_timeout`)

**Default:** `15` (seconds)

Timeout for outgoing ActivityPub HTTP requests: inbox delivery, actor/webfinger lookups.

---

<a id="federation-actor_key_cache_ttl"></a>

#### Actor key cache TTL (`federation.actor_key_cache_ttl`)

**Default:** `3600` (seconds)

How long a fetched remote actor's public key is cached before being re-fetched.

---

<a id="federation-max_clock_skew"></a>

#### Max clock skew (`federation.max_clock_skew`)

**Default:** `3600` (seconds)

Maximum allowed clock skew between the `Date` header on an incoming signed request and local time, before the signature is rejected.

---

<a id="federation-max_backdate_days"></a>

#### Max backdate days (`federation.max_backdate_days`)

**Default:** `730`

A mirrored post's Matrix event shows the real ActivityPub `published` time, trusted up to this many days in the past before falling back to "now" instead. This exists mainly as a safety margin against Synapse's own `retention.default_policy.max_lifetime`, if configured: a custom `origin_server_ts` older than that policy's `max_lifetime` is silently accepted (a real event ID, 200 OK) but never actually reachable afterwards, a silent post-drop that's worse than just showing "now". There's no API to discover a homeserver's own retention policy, so set this by hand, comfortably below it, if one is configured. Leave it generous (or raise it) if the homeserver has no retention policy limiting history at all.

---

## `logging`

<a id="logging-level"></a>

#### Log level (`logging.level`)

**Default:** `ERROR`

Standard Python logging level name (`ERROR`, `WARNING`, `INFO`, `DEBUG`, ...). `ERROR` keeps the service journal quiet in normal operation; switch to `INFO` temporarily while actively investigating an issue, then switch back.
