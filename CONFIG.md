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
  - [Rooms and knocking](#rooms-and-knocking)
  - [Posts and polls](#posts-and-polls)
  - [Third-party access](#third-party-access)
  - [MSC4501 and related proposals](#msc4501-and-related-proposals)
  - [PeerTube video channels](#peertube-video-channels)
- [`synapse`](#synapse)
- [`storage`](#storage)
- [`federation`](#federation)
- [`logging`](#logging)

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

#### `bridge.internal_base_url`

**Default:** `http://<listen_host>:<listen_port>`

The address Synapse pushes AppService transactions to. Only set this if Synapse and the bridge are on different hosts (or separate Docker networks) and can't reach each other at the default directly -- that traffic never needs to leave the host/trusted network otherwise.

### Rooms and knocking

#### `bridge.ghost_room_join_rule`

**Default:** `knock`. One of `knock`, `invite`, `public`.

Join rule for a new Remote User Room (a ghost's mirror of a remote fediverse account). `knock` lets anyone locked out (e.g. after a `;replace room`) ask their way back in without an admin.

#### `bridge.local_profile_room_join_rule`

**Default:** `knock`. Same values as above, but for a local user's own Profile Room.

#### `bridge.world_readable_remote_rooms`

**Default:** `false`

Whether a Remote User Room (including a followed PeerTube channel's) gets `history_visibility: world_readable`, letting anyone on this homeserver preview it without joining or knocking -- e.g. to check a repost's `social.relates_to` against its real source. Independent of `ghost_room_join_rule` above, which still governs joining itself. Run `;refresh` in an existing room to re-sync it to a changed value.

#### `bridge.accept_federated_knocks`

**Default:** `false`

Whether a knock from a user on a DIFFERENT homeserver is auto-accepted the same as a local user's. Off by default: mirrored posts are public fediverse content, but who gets auto-admitted into this server's rooms is this server's call. A federated user can always be invited manually either way.

#### `bridge.backfill_default_count`

**Default:** `15`

How many posts `;backfill` pulls with no explicit count given. A Matrix server admin can override this per-use with an explicit argument.

### Posts and polls

#### `bridge.poll_default_duration_days`

**Default:** `7`

Matrix polls have no expiry; Mastodon-family servers refuse a poll with none. This many days after creation is sent as the poll's `endTime` instead.

### Third-party access

#### `bridge.admins`

**Default:** `[]`

Matrix user IDs treated as bridge admins regardless of real Synapse server-admin status. Always additive -- a real Synapse admin not listed here still gets admin rights too, as long as `use_synapse_admin_api` is on.

```yaml
bridge:
  admins:
    - "@you:example.org"
```

#### `bridge.use_synapse_admin_api`

**Default:** `true`

Whether the bridge is allowed to call Synapse's own Admin API, for checking whether another user is a server admin and for a couple of admin-only room sweeps. Turn this off to run against a homeserver that doesn't have (or won't grant) Synapse's Admin API -- EXPERIMENTAL, only ever tested against Synapse itself.

Turning it off means admin status relies entirely on `bridge.admins` above (give it real entries, or nobody gets those commands), and `synapse.admin_token` is no longer required. Spot-test every admin-gated command and watch the bridge's logs after turning this off, rather than assuming it behaves the same as the Synapse-backed path.

#### `bridge.third_party_access_mode`

**Default:** `follow_only`. One of `follow_only`, `full`.

What a user on a DIFFERENT homeserver gets once an admin allows them (the `;allow`/`;disallow`/`;allowed` commands):

- `follow_only`: `;follow` and interact only. No Profile Room; their AP identity is minted automatically, always mirroring their live Matrix profile.
- `full`: identical to a local user -- self-service `;create profile`/`;link profile`, DM, Chat.

A single deployment-wide setting, applied live to everyone currently allowed -- not fixed at the time they were first allowed. Flipping it is always safe and reversible: nothing about an already-provisioned identity is ever deleted or reassigned by the change alone.

### MSC4501 and related proposals

Behaviour each of these controls is described in [FEATURES.md](FEATURES.md#msc4501).

#### `bridge.set_msc4501_relates_to`

**Default:** `true`

Set `org.matrix.msc4501.social.relates_to` on every mirrored repost, quote-post, and cross-posted reply echo. Purely additive on an ordinary message; a client that's never heard of MSC4501 just ignores the extra field.

#### `bridge.use_msc4501_content_inline`

**Default:** `true`

For a mirrored repost specifically: assert `content_inline` (the event's own content already *is* the reposted post's content) instead of duplicating that content a second time into `relates_to.content`. Turn off for a client that doesn't handle `content_inline` yet.

#### `bridge.quote_import_policy`

**Default:** `known`. One of `known`, `always`, `never`.

Whether an untracked quote-post's TARGET gets imported into its own Remote User Room (needed for `relates_to` to populate on it at all):

- `known`: only if the quoted author already has a Remote User Room for some other reason.
- `always`: import unconditionally.
- `never`: read-only preview only, `relates_to` stays unset.

A quote is often adversarial ("quote-dunking"), so `always` means provisioning a room for a total stranger purely because someone followed here quoted them.

#### `bridge.use_msc4501_post_event_type`

**Default:** `false`

Mirror a remote account's posts using `org.matrix.msc4501.social.post` as the event TYPE, instead of `m.room.message`. **Not recommended** until Phase 2 of MSC4501's own rollout plan -- unlike the additive fields above, a client that's never heard of MSC4501 won't render the event at all.

#### `bridge.msc4144_per_message_profiles`

**Default:** `true`

Adds an MSC4144 per-message profile (id/displayname/avatar_url from the user's Profile Room) to a bridge-sent message acting on their behalf, like the repost card, so it visually reads as their own action. Ignored by clients that don't know MSC4144.

#### `bridge.set_msc4501_profile_room`

**Default:** `false`

Set `org.matrix.msc4501.social.profile_room` (MSC4133 Extensible Profiles) on every ghost, pointing at its room. Needs `experimental_features.msc4133_enabled: true` in Synapse's `homeserver.yaml` first (off even there by default) -- leave this off until that's done, or every ghost registration eats a guaranteed-failing request. An admin can force an existing ghost's field into line via `;refresh`.

#### `bridge.msc4503_external_handle`

**Default:** `off`. One of `off`, `profile`, `events`, `both`.

Where each ghost's real ActivityPub handle (e.g. `@alice@mastodon.social`) is recorded (MSC4503): as a field on its Matrix profile (`profile`, same MSC4133 requirement as above), as a snapshot on every event it sends (`events`), or `both`.

#### `bridge.msc4027_custom_reactions`

**Default:** `false`

Mirror an inbound custom-emoji reaction as a real MSC4027 custom-image reaction instead of plain `:shortcode:` text. Off by default: unlike the additive fields above, this replaces the one field every client renders, so a client that's never heard of MSC4027 shows the raw `mxc://...` string instead of the shortcode text it showed before. Only turn this on once you know your users' clients support it. Reacting with a custom image FROM Matrix always federates out as a custom-emoji reaction regardless of this setting.

### PeerTube video channels

#### `bridge.peertube_channels_enabled`

**Default:** `false`

Whether a Matrix room can become a real, federating PeerTube-compatible video channel (`;create channel`, `;publish`, and friends -- see [FEATURES.md](FEATURES.md#peertube-video-channels-publishing-and-following)). Off by default: a real behavior change, not a safely-ignorable additive field. Every remote view/scrub of a hosted video streams through this bridge's own media proxy with no caching layer, a much heavier bandwidth pattern per view than the images that proxy already serves -- weigh that cost for your own deployment before turning this on.

#### `bridge.follow_video_media_mode`

**Default:** `link`. One of `link`, `embed`.

How a followed PeerTube channel/account's videos show up as posts. `link` always shows a text/thumbnail card linking back to the original instance, never downloading the video. `embed` tries to download and re-host the video natively, falling back to a `link`-style card when the file is bigger than `follow_video_max_embed_mb` below. Defaults to `link` for the same bandwidth/storage reasoning as `peertube_channels_enabled` above.

#### `bridge.follow_video_max_embed_mb`

**Default:** `100`

Only consulted in `embed` mode -- checked against the video's own declared file size before ever starting a download.

---

## `synapse`

#### `synapse.admin_token`

**Default:** unset

Access token for an account with `admin: true`, used for Synapse's Admin API. Required unless [`bridge.use_synapse_admin_api`](#bridgeuse_synapse_admin_api) is turned off.

---

## `storage`

Bookkeeping (linked identities and their keys, follow relationships, ghost profiles, the Matrix-event/ActivityPub-object map, and room-history tables used for knock/backfill/outbox continuity after a `;replace room`) is kept in one of two places. Same schema and content either way; no post content or media is stored in either.

#### `storage.backend`

**Default:** `sqlite`. One of `sqlite`, `postgresql`.

- `sqlite`: a single file, `bridge.db`, under `storage.data_dir`. Nothing to set up.
- `postgresql`: the Postgres server described by `storage.postgres` below. Useful if you already run Postgres for something else, such as your homeserver.

Switching backends on an existing deployment starts that backend's storage empty -- there is no automatic migration between them.

#### `storage.postgres`

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

#### `federation.request_timeout`

**Default:** `15` (seconds)

Timeout for outgoing ActivityPub HTTP requests: inbox delivery, actor/webfinger lookups.

#### `federation.actor_key_cache_ttl`

**Default:** `3600` (seconds)

How long a fetched remote actor's public key is cached before being re-fetched.

#### `federation.max_clock_skew`

**Default:** `3600` (seconds)

Maximum allowed clock skew between the `Date` header on an incoming signed request and local time, before the signature is rejected.

#### `federation.max_backdate_days`

**Default:** `730`

A mirrored post's Matrix event shows the real ActivityPub `published` time, trusted up to this many days in the past before falling back to "now" instead. This exists mainly as a safety margin against Synapse's own `retention.default_policy.max_lifetime`, if configured: a custom `origin_server_ts` older than that policy's `max_lifetime` is silently accepted (a real event ID, 200 OK) but never actually reachable afterwards -- a silent post-drop, worse than just showing "now". There's no API to discover a homeserver's own retention policy, so set this by hand, comfortably below it, if one is configured. Leave it generous (or raise it) if the homeserver has no retention policy limiting history at all.

---

## `logging`

#### `logging.level`

**Default:** `ERROR`

Standard Python logging level name (`ERROR`, `WARNING`, `INFO`, `DEBUG`, ...). `ERROR` keeps the service journal quiet in normal operation; switch to `INFO` temporarily while actively investigating an issue, then switch back.
