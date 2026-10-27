# Bot commands

The bridge is controlled from inside Matrix by either tagging/mentioning the bot, or starting a message with `;<command>` (e.g. `;follow @user@instance.org`) -- `;` is the default prefix, configurable via `bridge.command_prefix` (see [CONFIG.md](CONFIG.md#bridge-command_prefix)), e.g. if it collides with another bot/bridge sharing the room. A few general rules apply to all of them:

- A command is only recognized on the **first line** of a message. Tagging the bot somewhere in a longer message won't misfire on a command word later in the text.
- Keyword matching is case-insensitive.
- Sharing a room with another bot or bridge (a Bluesky/Nostr bridge, another Matrix bot, ...)? List its own command prefix(es) in `bridge.other_bot_prefixes` (see [CONFIG.md](CONFIG.md#bridge-other_bot_prefixes)) so a message meant for IT never gets federated out by this bridge as an ordinary post -- this bridge still correctly never replies to it or tries to parse it as one of its own commands, either.
- Every command **except `;help` and `;delete profile`** only works for users on the bridge's own homeserver, unless a Matrix server admin has explicitly allowlisted them -- an exact MXID or a whole homeserver (see [`;allow`](#allow-mxidhomeserverroom-value-fullshadow)). A user on another Matrix server tagging the bot gets told commands aren't available to them unless allowlisted, and the whole feature can be switched off bridge-wide (`bridge.third_party_access`, on by default -- see [CONFIG.md](CONFIG.md#bridge-third_party_access)). This stops someone from squatting usernames on your domain or riding your bridge's reputation to follow arbitrary fediverse accounts from outside it, while still letting an admin selectively open the bridge up to trusted outsiders.
- An allowlisted third-party user gets one of two tiers, chosen per-grant (see `;allow` above): **shadow** (the default) -- `;follow` and interact only, no Profile Room, no `;dm`/`;chat`/`;banner`/`;replace room`/`;backfill`/`;repost`, their fediverse identity is minted automatically the first time they do anything that would federate and always mirrors their live Matrix name/avatar -- or **full** -- the ABILITY to self-provision a real Profile Room via `;create profile`/`;link profile`, after which they're identical to a local user. A first-party (this homeserver's own) user goes through the exact same shadow phase automatically, with no allowlisting involved -- it's just how every identity starts before `;create profile`.
- The bridge's own bot and ghost accounts never trigger commands, so a mirrored post that happens to start with `;` is never misread as one.
- Tagging the bot with no recognized keyword shows the `;help` section overview (if nothing else was said) or a short pointer to `;help` (if something else was said that just wasn't recognized).
- If a command was sent as a thread reply, the bot's response(s) stay in that same thread.
- Most of these are also available as buttons in the bridge's room widget (`;widget`). See [Widget vs. commands](#widget-vs-commands).

## Contents

- [Profile](#profile)
  - [`;create profile [<id>]`](#create-profile-id)
  - [`;id`](#id)
  - [`;import follows`](#import-follows-or-import-following)
  - [`;banner`](#banner-mxcservermediaid)
- [General](#general)
  - [`;help` / `;help <section>`](#help--help-section)
  - [`;follow`](#follow-userinstanceorg)
  - [`;unfollow`](#unfollow-userinstanceorg)
  - [`;following`](#following)
  - [`;dm`](#dm-userinstanceorg)
  - [`;chat`](#chat-userinstanceorg)
  - [`;import <url>`](#import-url)
  - [`;repost`](#repost-caption-reply-to-a-mirrored-fediverse-post)
- [Advanced/Maintenance](#advancedmaintenance)
  - [`;set`](#set)
  - [`;block`](#block-userinstanceorg)
  - [`;unblock`](#unblock-userinstanceorg)
  - [`;mute`](#mute-userinstanceorg)
  - [`;unmute`](#unmute-userinstanceorg)
  - [`;rejoin`](#rejoin-room_idprofilespace-othermatrixid)
  - [`;leave unfollowed`](#leave-unfollowed)
  - [`;backfill`](#backfill-n)
  - [`;refresh poll`](#refresh-poll-reply-to-a-poll-or-anything-in-its-thread)
  - [`;widget`](#widget)
- [Guild](#guild)
  - [`;joinguild`](#joinguild-codeguildexamplecom)
  - [`;leaveguild`](#leaveguild)
  - [`;refresh guild`](#refresh-guild)
  - [`;refresh guild invite`](#refresh-guild-invite-codedomain)
- [PeerTube](#peertube)
  - [`;create channel`](#create-channel-id-as-numberid-display-name)
  - [`;publish`](#publish-reply-to-a-video-message)
  - [`;unpublish`](#unpublish-reply-to-an-already-published-video)
  - [`;edit`](#edit-reply-to-an-already-published-video)
  - [`;replace video`](#replace-video-mxcservermediaid)
- [Lemmy communities](#lemmy-communities)
  - [`;create community`](#create-community-id-as-numberid-display-name)
  - [`;link community`](#link-community-id)
- [Groups](#groups)
  - [`;create group`](#create-group-id-as-numberid-display-name)
  - [`;link group`](#link-group-id)
- [Admin](#admin)
  - [`;allow`](#allow-mxidhomeserverroom-value-fullshadow)
  - [`;disallow`](#disallow-mxidhomeserverroom-value)
  - [`;allowed`](#allowed)
  - [`;refresh`](#refresh-userinstanceorg)
- [Widget vs. commands](#widget-vs-commands)
- [Danger Zone](#danger-zone)
  - [`;link profile`](#link-profile)
  - [`;unlink profile`](#unlink-profile--unlink-profile-numberid)
  - [`;replace room`](#replace-room)
  - [`;replace dm`/`;replace chat`](#replace-dmchat-userinstanceorg)
  - [`;delete profile`/`;delete channel`/`;delete community`/`;delete group`](#delete-profile--delete-channel--delete-community--delete-group)

---

## Profile

Setting up and maintaining your own linked fediverse identity.

### `;create profile [<id>]`

**Syntax:** `;create profile`, no other argument -- or, with multi-identity support enabled (`bridge.custom_identities_enabled`, see [CONFIG.md](CONFIG.md#bridge-custom_identities_enabled)), `;create profile <id>` to pick a custom id instead.

**What it does:** One-shot setup that would otherwise take several manual steps. Creates a brand-new Matrix room (bot as creator/admin), invites you and sets your power level to 99 (one below the bot), copies your current Matrix display name and avatar onto the room, tags it as a bridge-made Profile Room, adds the room widget, adds it to your personal Fediverse space, and mints (or, if you'd previously run `;unlink profile`, reattaches) your actual ActivityPub actor to it. With no `<id>`, your username is always derived from your Matrix localpart (`username@bridge-domain`), exactly as it always has been, regardless of whether custom identities are enabled -- `<id>` is purely an additional option, never required.

**Who can run it:** Any local Matrix user. If you already have a linked profile under the SAME username this resolves to, this doesn't create a second one -- it re-invites you to your existing profile room and Fediverse space instead (harmless no-op if you're already in either). Useful if a Matrix account deactivation force-left you from both; your linked identity itself isn't affected by that, only your Matrix-side room membership. See [`;rejoin profile`/`;rejoin space`](#rejoin-room_idprofilespace-othermatrixid) for the same recovery without recreating anything.

**Notes:** Reattaching an unlinked identity preserves its followers and following exactly as before. If room creation fails, you're told you can make your own room and use `;link profile` instead (see [Danger Zone](#danger-zone)). With multi-identity support on, creating an identity beyond your first is capped at `bridge.max_identities_per_user` (bridge admins are exempt) and never becomes your Active identity automatically -- switch to it with [`;id`](#id).

---

### `;id`

**Syntax:** `;id` or `;id list` to see your identities; `;id <number|id>` to switch which one is Active.

**What it does:** With multi-identity support enabled, a Matrix user can own more than one ActivityPub identity (see [`;create profile <id>`](#create-profile-id) above). Your **Active** identity is the one used for anything that doesn't already have an identity implicitly tied to it -- `;follow`, `;dm`, `;chat`, reacting/replying outside your own rooms, and so on. Posting in a specific Profile Room, or publishing to a PeerTube channel/Lemmy community already tied to one identity at creation, always uses THAT identity instead, regardless of Active. `;id`/`;id list` shows every identity you own, numbered, with the Active one marked. `;id <number|id>` switches Active to the one named, by its number in that list or by its username directly.

**Who can run it:** Any local Matrix user with at least one linked identity -- works (and still shows something useful) even with only one identity, or with multi-identity support off.

**Notes:** A newly created identity never becomes Active automatically unless it's your very first one -- you always keep the Active identity you already had until you explicitly switch. You always have an Active identity once you have at least one (deleting the Active one promotes another automatically -- see [`;delete profile`](#delete-profile--delete-channel--delete-community--delete-group)).

---

### `;import follows` (or `;import following`)

**Syntax:** Must be sent as a reply to a message with an uploaded file, in a room the bot is in. First export your follows list from the source account (Pleroma/Akkoma: Settings > Data export; Mastodon: Preferences > Import and export > Follows), upload that file, then reply to the upload with this command.

**What it does:** Parses either Pleroma/Akkoma's one-handle-per-line format or Mastodon's CSV export, then follows every handle in the background, exactly like running `;follow` for each one. Already-followed accounts (including your own account showing up in its own export) are skipped silently. Posts a single summary (followed/skipped/failed counts, with each failure's specific reason) once finished.

**Who can run it:** Requires a linked profile.

**Notes:** Long-running and asynchronous. The summary may arrive minutes later. The widget's equivalent accepts the file directly, without needing it to already be an upload you're replying to.

---

### `;banner mxc://server/mediaid`

**Syntax:** `;banner mxc://server/mediaid`. The argument must start with `mxc://` (upload the image to any room the bot can see to get this URI). Run inside your own linked Profile Room.

**What it does:** Sets your fediverse profile's banner/header image, distinct from your avatar. Matrix has no stable room-level banner state yet, so it's recorded via MSC4221's `m.room.banner` (under that MSC's own unstable prefix), and every run immediately pushes a signed `Update` to your followers.

**Who can run it:** Only your own profile, only from inside your linked Profile Room. Also reachable via the widget's file-upload control, which skips needing an `mxc://` URI in hand first.

---

## General

Following, messaging, and everyday day-to-day commands.

### `;help` / `;help <section>`

**Syntax:** `;help`, or tag the bot with nothing else recognizable, shows a table of every section (this document's own breakdown: Profile, General, Advanced/Maintenance, Guild, PeerTube, Admin, Danger Zone) with a one-line blurb each. Run `;help <section>` (e.g. `;help profile`, `;help guild`, `;help danger`) to see that section's own commands, in the same rich format `;help all` used to show everything in. `;help all` still works as a shortcut showing every section's commands in one combined table (except Admin -- see below). `;help admin` shows the commands actually gated to a Matrix server admin (`;allow`/`;disallow`/`;allowed`/`;refresh`) -- now just this same `;help <section>` form, "admin" being a section like any other.

**What it does:** Sends a table as a rich `m.text` message (not a notice, so it isn't visually suppressed by "hide notices" client settings). Each command's own invocation is bolded, set apart from its description below it. `;help all` deliberately never includes the Admin section's commands -- those list real capabilities a non-admin literally cannot use, not just "advanced"/inconvenient ones, so they're always their own separate, admin-gated view.

**Who can run it:** Anyone, including users on other Matrix homeservers. This is the one exception to the local-users-only rule. `;help admin` (or any section named `admin`) is refused outright, with nothing listed, for anyone who isn't actually a Matrix server admin.

**Notes:** Read-only, no side effects. An unrecognized section name falls back to the section-overview table.

---

### `;follow @user@instance.org`

**Syntax:** `;follow @user@instance.org`, a bare actor URL (e.g. `https://instance.example/users/alice` or a community link like `https://instance.example/c/name`), or no argument at all if run from inside that account's own room (or an already-mirrored community's own room). A tagged mention pill, of one of the bridge's ghost users or of another local user, works the same as typing the handle, and wins if both are somehow present.

**What it does:** Follows the target as your own linked actor.
- **Genuinely remote target:** creates or reuses a Remote User Room, invites you into it, and delivers a signed `Follow`.
- **A Lemmy community** (a `Group` actor with its own moderators collection, detected via NodeInfo or the actor document): creates or reuses a SHARED `org.matrix.msc4501.social.group` room instead -- see [Lemmy communities](#lemmy-communities) below.
- **A FEP-1b12 Group** (any other `Group`-typed actor -- e.g. Mitra, Friendica, Hubzilla): creates or reuses a shared PLAIN room (no MSC4501 room type) instead -- see [Groups](#groups) below.
- **Actually another local user on this bridge** (tagged directly, or resolved via their own `@user@yourdomain` handle): handled entirely in-process. Invites you straight into their existing Profile Room (never fabricating a ghost for someone with a real Matrix account), records the follow both ways, and DMs them a notification.

**Who can run it:** Requires a linked profile first.

**Notes:** No-op with a notice if you're already following them. If delivery fails, you're told why but still joined to the room; the Follow isn't retried automatically.

---

### `;unfollow @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;follow`.

**What it does:**
- **Remote target:** kicks you from their Remote User Room, which triggers a real `Undo(Follow)` the same way leaving the room yourself would.
- **Local target:** pure bookkeeping. Removes the follow record without kicking you from their Profile Room, since it's their real room and possibly has other unrelated members.

**Who can run it:** Any local user. Fails gracefully if there's no such follow.

---

### `;following`

**Syntax:** `;following`, no argument.

**What it does:** Lists every account your linked actor follows, alphabetically, each with a link to its room where one exists.

**Who can run it:** Any local user.

---

### `;dm @user@instance.org`

**Syntax:** Same argument/no-argument resolution pattern (no argument from inside the account's own room). Tagged ghost mention pills supported.

**What it does:** Starts, or reuses (re-inviting you if you'd left), a 1:1 `Note`-based direct-message room with the target.

**Who can run it:** Requires a linked profile. Refuses if the resolved handle is actually a local bridge user; just start an ordinary Matrix DM with them directly.

**Notes:** Distinct from `;chat` even for the same account. Different rooms, different ActivityPub message shapes. The bridge has no end-to-end encryption support at all (no OLM/Megolm), so it needs the room it creates to actually be unencrypted -- if your homeserver is configured to force encryption on new private rooms anyway (`encryption_enabled_by_default_for_room_type`), there's no way for this command to override that; you'll get a clear notice explaining that instead of a silently broken room. Ask a Matrix server admin to turn that setting off (or exclude invite-only rooms) if you hit this. See [`;replace dm`](#replace-dmchat-userinstanceorg) for fixing a room that already ended up encrypted this way.

---

### `;chat @user@instance.org`

**Syntax:** Same resolution as `;dm`.

**What it does:** Starts or reuses a 1:1 `ChatMessage`-based room (Pleroma/Akkoma's separate instant-messaging concept), the counterpart to `;dm`'s `Note`-based room. Warns, but still creates the room, if the target's actor doesn't advertise chat support.

**Who can run it:** Requires a linked profile; refuses for a local target the same way `;dm` does.

**Notes:** The other way to start one is inviting the ghost's own Matrix account directly into a fresh DM. Same forced-encryption caveat as `;dm` above -- see [`;replace chat`](#replace-dmchat-userinstanceorg) if an existing chat room ends up encrypted.

---

### `;import <url>`

**Syntax:** `;import <fediverse post URL>`, must start with `http://` or `https://`.

**What it does:** Fetches a single post by URL and mirrors it regardless of whether you follow its author, creating or reusing a Remote User Room for them (but not actually following them) and inviting you into it. Already-mirrored posts aren't duplicated. If the URL is actually a local bridge user's own post, you're just invited into their real Profile Room instead. Falls back to alternate URL forms for instances that don't serve their "pretty" post URLs as fetchable JSON. If the post is a reply to something already tracked in the same room, it's mirrored as a proper threaded reply. A PeerTube video URL works too, mirrored into a Remote User Room for its own author the same way.

**Who can run it:** Any local user. No linked profile required, since importing doesn't send anything out as you.

**Notes:** Also reachable via the widget's URL field.

---

### `;repost [<caption>]` (reply to a mirrored fediverse post)

**Syntax:** Sent as a reply to a tracked post, with or without a caption.

**What it does:** MSC4501 models a plain repost and one with your own added commentary as the same underlying relation, just with or without inline content, so this bridge merges them into one command the same way:

- **Bare** (`;repost`): sends a real, signed `Announce` -- exactly the same thing reacting to the post with 🔁 does, including the same "🔁 you reposted" card in your own Profile Room. A command-triggered repost and a reaction-triggered one are indistinguishable afterwards.
- **With a caption** (`;repost <your caption>`): creates a brand-new post of your own with the caption as its text, marked as quoting the original (for receivers that render real quote cards) with a plain link appended for those that don't.

Delivered like an ordinary post either way: to your followers, the original author, and (with a caption) anyone mentioned in it. Always rendered into your own Profile Room, never wherever the command was actually run.

**Who can run it:** Requires a linked profile.

---

## Advanced/Maintenance

One-off account/room-recovery operations and other things almost nobody needs day to day -- shown under `;help all` rather than plain `;help`.

### `;set`

**Syntax:** `;set` or `;set list` (show every setting and its current value), `;set <key>` (show just one), `;set <key> <value>`, `;set <key> default` (clear an override, reverting to the default).

**What it does:** One mechanism for every per-identity preference, replacing the old one-command-per-setting pattern. Current settings:

- `hide_followers` / `hide_following` (on/off, default off): whether your public followers/following collection exposes its member list to remote viewers. The reported count is always public regardless; only the list itself is withheld (same semantics as Mastodon's "hide network").
- `discoverable` (on/off, default on): whether other instances should include you in directories/search (the `toot:discoverable` AS2 extension).

**Who can run it:** Settings are scoped per ActivityPub identity, not per Matrix user. Run from inside your own linked Profile Room, or privately from inside your Fediverse Notifications room (useful if you'd rather not run it somewhere a Matrix-side follower in your Profile Room could see), to control your main identity's settings; or from inside a PeerTube channel room you own to control that channel's own, separate settings. `discoverable` only applies to a profile; a channel is always discoverable (the whole feature only ever federates public, findable content), so `;set discoverable ...` is rejected from a channel room.

**Notes:** Storage is sparse; only a setting you've actually changed from its default is stored at all.

---

### `;block @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;follow`.

**What it does (broader than `;mute`, which it subsumes):**
- Cuts any existing follow relationship immediately in both directions: a real `Undo(Follow)` if you were following them, or just a dropped record if they were following you.
- Kicks you from their Remote User Room (never a local target's own Profile Room), and from any open DM/Chat room between you.
- Declines any future `Follow` from them with a real `Reject`.
- Silences them exactly like `;mute`.

**Who can run it:** Requires a linked profile. You can't block yourself.

**Notes:** Does not stop their posts from mirroring. The shared Remote User Room may still be needed by other followers, or by a repost someone else made. `;unblock` only lifts the block flag itself; it does not restore the follow or re-invite you anywhere. Redo `;follow`/`;dm`/`;chat` yourself if you want that back.

---

### `;unblock @user@instance.org`

**Syntax:** Same resolution as `;block`.

**What it does:** Removes the block record only. Nothing else that changed as a side effect of blocking is restored automatically.

**Who can run it:** Requires a linked profile. No-op if not currently blocked.

---

### `;mute @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;block`.

**What it does:** Suppresses notifications about the target and auto-invites into a room because of them (a fresh DM/Chat they open, or being pulled into a mention). Doesn't touch any existing follow, room membership, or mirroring; their posts, replies, and reactions keep flowing normally. Explicitly running `;dm`/`;chat` toward a muted account still works, since that's your own deliberate action.

**Who can run it:** Requires a linked profile. Can't mute yourself. No-op if already muted.

---

### `;unmute @user@instance.org`

**Syntax:** Same resolution as `;mute`.

**What it does:** Undoes `;mute`.

**Who can run it:** Requires a linked profile. No-op if not currently muted.

---

### `;rejoin <room_id>|profile|space [@other:matrix.id]`

**Syntax:** `;rejoin <room_id>` to invite yourself into a specific room, or `;rejoin profile`/`;rejoin space` to invite yourself into your own linked Profile Room or Fediverse space without needing to know its room ID at all. Add `@other:matrix.id` to any form to invite someone else instead.

**What it does:** Force-attempts a fresh invite into a room the bridge manages, a manual recovery tool for a lockout -- a room's join rule got switched to knock-only with nobody left to approve one, or (the reason `profile`/`space` exist) a Matrix account deactivation force-left you from every room you were in. Your linked identity itself (keys, followers, following, room pointer) survives that untouched on the bridge's own side -- deactivation only ever affects Matrix-side room membership -- so once you're back (a homeserver admin can reverse a deactivation onto the same account), this is how you get back into the rooms themselves. `profile` invites you into EVERY Profile Room you currently own (if you have more than one identity), not just one. Inviting someone already a member is a harmless no-op. Never triggers an ActivityPub `Follow` as a side effect; following only ever happens via `;follow` itself.

**Who can run it:**
- Inviting only yourself: any Remote User Room (even an already-replaced one no longer live-tracked), your own linked Profile Room (`profile`, or its literal room ID, current or past), or your own Fediverse space (`space`).
- Inviting anyone else, or targeting anything else (including someone else's `profile`/`space`): admin only. An admin can also target any literal room ID this way, even ones the bridge doesn't otherwise recognize, as a true last resort.

---

### `;leave unfollowed`

**Syntax:** `;leave unfollowed`, no argument.

**What it does:** Finds every Remote User Room you're currently a member of for an account that NONE of your identities follow -- room membership and following are tracked independently, so this can happen via an old unfollow that never kicked you out, or various on-demand imports (a mention, a reply's ancestor chain, someone else's repost) landing you in a room you never actually ran `;follow` in. If you own more than one identity, a room is only offered up here if nobody of yours follows it -- one of your other identities still following is reason enough to keep it. Shows the count and a list first and asks you to reply "confirm" before removing anything -- nothing happens just from running the command. Removes you (kicks, via the bot) from each one; your own Profile Room, DMs, and Chats are never touched, only Remote User Rooms.

**Who can run it:** Requires a linked profile.

---

### `;backfill [N]`

**Syntax:** `;backfill` (uses the configured default count) or `;backfill N`. Run inside a Remote User Room: at the room root to backfill that account's outbox, or as a reply inside an existing thread to backfill that specific conversation's replies instead.

**What it does:** Pulls up to N posts (or that thread's replies) through the exact same mirroring path a live delivery uses, so a backfilled post is indistinguishable from a live one and already-mirrored posts are never duplicated. Runs in the background, with a summary posted once done.

**Who can run it:** Any local user, with the default count. Only a Matrix server admin can specify a custom N, since an arbitrarily large one means unbounded outbound fetches against a remote server.

**Notes:** A brand-new follow's first room-join already triggers one automatic backfill on its own. This is for topping that up or pulling in a specific thread.

---

### `;refresh poll` (reply to a poll, or anything in its thread)

**Syntax:** `;refresh poll`, or just bare `;refresh` with no argument, sent as a reply to a mirrored poll's own event, or to anything else inside its thread (e.g. its tallies message, or a human reply). Renamed 2026-07-11 from `;poll refresh`.

**What it does:** Actively re-fetches the poll's current live state from its own ActivityPub id and reflects it: refreshed vote tallies (posted/edited as a thread reply under the poll) and closed state, if it's ended. Exists because some remote implementations -- confirmed for Pleroma/Akkoma -- never push a live update over federation at all, live or at close, so a mirrored poll's tallies can otherwise sit stale forever. The same refresh already runs automatically right after you vote on a mirrored poll; this command is for anyone who wants to check again without voting again (which most polls don't allow anyway).

Bare `;refresh` checks for poll-thread context FIRST, before falling back to the admin-only ghost-profile refresh (see [Admin](#admin)) -- so replying to refresh a poll never also touches anyone's profile, and vice versa: a bare `;refresh` that isn't a reply to anything tracked (or isn't a reply at all) falls straight through to the ghost-profile behavior instead.

**Who can run it:** Any local user -- unlike the admin-only `;refresh [@user@instance.org]`, despite sharing the same keyword.

**Notes:** Best-effort -- if the poll is no longer reachable (deleted, network error), you'll get a notice saying so instead of a silent no-op.

---

### `;widget`

**Syntax:** `;widget`, run in whatever room should get it.

**What it does:** Adds the bridge's room widget, buttons for most of the commands above, scoped to whatever kind of room it's added to.

**Who can run it:** Any local user, in any room.

**Notes:** Mints a fresh widget every time. Running it again adds a second instance rather than being a no-op; remove a stale one yourself via your client's widgets panel.

---

## Guild

Shoot guild and channel commands -- FEP-bebd guild join/leave and channel management. See [FEATURES.md](FEATURES.md#guilds-and-channels-shoot) for how guild/channel bridging works overall.

### `;joinguild CODE@guild.example.com`

**Syntax:** `;joinguild CODE@guild.example.com`. The code is the bare invite code -- a leading `invite:` is stripped automatically if present.

**What it does:** Joins a Shoot guild (an `Organization` actor) using an invite code, per FEP-bebd. Unlike `;follow`, this can't resolve synchronously: the guild's `Accept`/`Reject` arrives later over its inbox, so this only sends and records the join request -- the notice deliberately doesn't say "Joined," since the code might be invalid or expired. Once accepted, the guild gets its own Matrix Space, with each of its text channels mirrored into a child Channel room.

**Who can run it:** Requires a linked profile.

**Notes:** Fails with a clear notice if the code can't be resolved or the guild's own actor document can't be fetched. If you're already a real member (a Follow the guild already accepted -- this survives leaving its Matrix Space entirely, since that's a purely local action), this re-invites you to the guild's Space instead of erroring out -- the way back in if you ever left it, no admin needed. If an admin has already stored an invite code on the guild's Space or Channel rooms (`;refresh guild invite`), you don't need this command at all -- just joining the Space or any Channel room auto-joins you to the guild over ActivityPub, with a DM if you don't have a linked profile yet or the attempt fails.

---

### `;leaveguild`

**Syntax:** `;leaveguild`, no argument, run inside one of that guild's own Channel rooms.

**What it does:** Sends a real `Undo(Follow)` and drops the bridge's own local membership tracking for that guild. The Undo is sent even though Shoot itself doesn't currently act on it, both because it's free and because it's correct behavior for any other implementation that does honor it.

**Who can run it:** Requires a linked profile, run inside one of that guild's Channel rooms.

---

### `;refresh guild`

**Syntax:** `;refresh guild`, run inside a joined guild's own Space or one of its Channel rooms.

**What it does:** Re-fetches the guild's live channel list right now and creates a Matrix room for any channel added since it was joined (or since the last refresh). Shoot doesn't federate channel-creation events at all, so a newly-created channel is otherwise only discovered the first time someone actually posts in it. Also the way to recover a Channel room the bridge finds tombstoned (its own mapping self-heals into a freshly created room) -- since most Matrix clients won't even let you type into a tombstoned room to ask, run it from the guild's Space instead, which is always still a live, postable room.

**Who can run it:** Matrix server admins only -- a Channel room (or the guild's Space) isn't "owned" by anyone on the Matrix side, and (unlike, say, a Profile Room) Channel rooms use restricted joins that any current guild Space member can use freely with no elevated power needed, so being present in one isn't a meaningful trust signal.

---

### `;refresh guild invite CODE@domain`

**Syntax:** `;refresh guild invite CODE@guild.example.com`, run inside a joined guild's own Space or one of its Channel rooms.

**What it does:** Stores `CODE@domain` on the guild's Space so that a Matrix user who joins the Space, or any of its Channel rooms, from then on is automatically joined to the guild over ActivityPub too -- no `;joinguild` needed from them. Also immediately resyncs: any current Matrix member of the Space who doesn't yet have their own accepted guild Follow gets one sent right away with this code, rather than waiting for them to leave and rejoin. Use this to set a guild's invite code for the first time, or to replace one that's gone stale or expired.

**Who can run it:** Matrix server admins only -- unlike bare `;refresh guild` above, this drives a real federated side effect (a Follow, for every future joiner) rather than just a re-fetch.

**Notes:** A user with no linked profile who joins the Space/a Channel room is DMed instead of silently skipped, telling them to `;link profile` and then `;joinguild` themselves. Anyone the immediate resync couldn't join (e.g. the code turned out to be bad) is reported in the summary count and needs to run `;joinguild` by hand too.

---

## PeerTube

Publishing and managing a PeerTube-compatible video channel. Opt-in (`bridge.peertube_channels_enabled`, off by default -- see [CONFIG.md](CONFIG.md#peertube-video-channels), and [FEATURES.md](FEATURES.md#peertube-video-channels) for how channel bridging works overall). Every command below responds with a plain notice if that setting is off.

### `;create channel <id> [as <number|id>] [display name]`

**Syntax:** `;create channel <id> [as <number|id>] [display name]`. `<id>` becomes the channel's fediverse username (`id@bridge-domain`); everything after it, if given, becomes its display name (defaults to `<id>` itself).

**What it does:** Mints a real, federating PeerTube-compatible video channel: a `Group`-typed ActivityPub actor with its own keypair, owned by one of your already-linked Profiles. Creates a brand-new Matrix room (bot as creator/admin), invites you and sets your power level to 99, adds the room widget, and adds it to your personal Fediverse space. Channels and profiles share one global actor-username namespace (both are ultimately served at the same `/actor/{username}` URL shape), so an `<id>` already taken by either kind is rejected.

**Who can run it:** Any local user with a linked profile.

**Notes:** One profile can own several channels. Send a video into the room, then reply to it with `;publish` to actually federate it. If you own more than one identity (see [`;id`](#id)), say which one owns this channel with `as <number|id>` right after `<id>` -- with exactly one identity, it's used automatically and `as ...` stays optional.

---

### `;publish` (reply to a video message)

**Syntax:** Reply to an uploaded video message inside a channel room with `;publish`, then on the following lines a `key: value` metadata block (`name` required; `category`/`license`/`language`/`tags`/`sensitive`/`commentsEnabled` all optional), then a blank line, then free-form description text -- the same git-commit/email-header-style shape `;edit` also uses:

```
;publish
name: My Cool Video
category: Comedy

Description text here.
```

**What it does:** Federates the replied-to video as a real PeerTube `Video`, delivered as `Create` to the channel owner's own followers and `Announce` to the channel's own followers, matching PeerTube's own publish choreography.

**Who can run it:** Only the channel's owner.

**Notes:** Refuses if that same video message has already been published -- use `;edit` to change its metadata, or `;replace video` to swap the file, instead.

---

### `;unpublish` (reply to an already-published video)

**Syntax:** Reply to an already-published video message with `;unpublish`, no other argument.

**What it does:** Retracts the video from the fediverse with a real, signed `Delete` -- delivered under both the channel owner's own identity and the channel's own identity, matching however a given follower originally subscribed. The underlying Matrix video message itself is completely untouched.

**Who can run it:** Only the channel's owner.

**Notes:** Clears the bridge's own record tying that Matrix message to its old ActivityPub video id, which is what makes a clean re-`;publish` of that same still-existing message work afterward, as a fresh publish.

---

### `;edit` (reply to an already-published video)

**Syntax:** Reply to an already-published video message with `;edit`, using the same `key: value` + blank line + description syntax as `;publish` -- every field here is optional; whatever isn't given is left exactly as it already was.

**What it does:** Changes the video's metadata only (name/category/license/language/tags/description/sensitive/commentsEnabled/thumbnail) -- the underlying file is immutable through this command. Sends a real, signed `Update` on the same existing video id.

**Who can run it:** Only the channel's owner.

**Notes:** To swap the actual video file instead, see `;replace video`.

---

### `;replace video [mxc://server/mediaid]`

**Syntax:** Two forms:
- Bare `;replace video`, sent as a direct reply to the newly-uploaded replacement file itself (which sits inside the published video's own thread).
- `;replace video mxc://server/mediaid`, sent as a direct reply to the ORIGINAL published video message, explicitly naming the new file's `mxc://` URI.

**What it does:** Swaps the underlying file of an already-published video while keeping the same video identity -- sends a signed `Update` on the same ActivityPub video id (not a fresh publish), re-runs the mimetype check against the new file, and re-derives its duration/dimensions from it.

**Who can run it:** Only the channel's owner.

---

## Lemmy communities

Following/mirroring a remote Lemmy community (`bridge.lemmy_communities_enabled`), and -- separately -- hosting your own, authoritative one (`bridge.lemmy_community_hosting_enabled`). See `;follow` above for mirroring a remote community, and `;backfill` for pulling its history.

### `;create community <id> [as <number|id>] [display name]`

**Syntax:** `;create community <id> [as <number|id>] [display name]`. `<id>` becomes the community's fediverse username (`id@bridge-domain`); everything after it, if given, becomes its display name (defaults to `<id>` itself).

**What it does:** Mints a real, federating Lemmy-compatible community: a `Group`-typed ActivityPub actor with its own keypair, hosted by this bridge itself (unlike mirroring, this bridge is the authoritative home instance). Creates a brand-new Matrix room (bot as creator/admin), invites you and makes one of your Profiles its first moderator (power level 50 -- a community has no single all-powerful owner, matching real Lemmy's own model), adds the room widget, and adds it to your personal Fediverse space. Shares the same global actor-username namespace as profiles/channels (all ultimately served at `/actor/{username}`), so an `<id>` already taken by any of them is rejected.

**Who can run it:** Any local user with a linked profile.

**Notes:** To post, just send an ordinary message: the first line becomes the post's title, a blank line, then the rest becomes the body. Reply to an existing post/comment to add a comment instead. Raising someone's Matrix power level to 50+ in the room makes them a real moderator (announced to followers); dropping it back below does the reverse. Banning a ghost sends a real `Block`; redacting someone else's post/comment sends a `Delete` signed as the moderator. If you own more than one identity, say which one moderates it with `as <number|id>` right after `<id>`, same as [`;create channel`](#create-channel-id-as-numberid-display-name).

---

### `;link community <id>`

**Syntax:** `;link community <id>`, run inside the room you want to convert. Sends a warning; reply to THAT message with `confirm` to go ahead -- nothing happens just from running the command itself.

**What it does:** Turns an already-existing room you control into a real, federating Lemmy-compatible community, instead of creating a fresh one. Adopts the room's current name/avatar/topic/banner as the community's starting values (best-effort). Same checks as `;create community` (linked profile required, `<id>` not already taken) plus needing to actually control the room (high enough power to set its own name/avatar).

**Who can run it:** Any local user with a linked profile who controls the target room.

**Notes:** A lasting change to what the room IS, same category as [`;link profile`](#link-profile) -- not reversible by an `;unlink` command (none exists for communities yet).

---

## Groups

Following/mirroring a remote FEP-1b12 Group, and -- separately -- hosting your own, authoritative one, both gated by one flag (`bridge.groups_enabled`). A Group is a flat chat room, unlike a Lemmy community's forum shape: every message is a plain message, no title convention, no comment tree. See `;follow` above for mirroring a remote Group (it's recognized automatically, just like a Lemmy community or a person), and `;backfill`/the automatic first-join backfill for pulling its history.

### `;create group <id> [as <number|id>] [display name]`

**Syntax:** `;create group <id> [as <number|id>] [display name]`. Same grammar as [`;create community`](#create-community-id-as-numberid-display-name).

**What it does:** Mints a real, federating FEP-1b12 Group: a `Group`-typed ActivityPub actor with its own keypair, hosted by this bridge itself. Creates a brand-new Matrix room (bot as creator/admin), invites you and makes one of your Profiles its first moderator (power level 50 -- any member can post, not just the owner, same "no single all-powerful owner" model as a Lemmy community), adds the room widget, and adds it to your personal Fediverse space. Shares the same global actor-username namespace as profiles/channels/communities. Unlike `;create community`, the room gets no MSC4501 `room_type` at all -- `social.group` specifically denotes a forum-of-posts shape, which doesn't describe a flat chat room.

**Who can run it:** Any local user with a linked profile.

**Notes:** To post, just send an ordinary message -- no title line, no blank-line convention. A reply to an existing message threads as a reply over ActivityPub too; an ordinary message is a flat, independent one. If you own more than one identity, say which one moderates it with `as <number|id>`, same as `;create community`.

---

### `;link group <id>`

**Syntax:** `;link group <id>`, run inside the room you want to convert. Sends a warning; reply to THAT message with `confirm` to go ahead.

**What it does:** Turns an already-existing room you control into a real, federating Group, instead of creating a fresh one. Adopts the room's current name/avatar/topic/banner as the Group's starting values (best-effort). Same checks as `;create group` (linked profile required, `<id>` not already taken) plus needing to actually control the room.

**Who can run it:** Any local user with a linked profile who controls the target room.

**Notes:** A lasting change to what the room IS, same category as [`;link profile`](#link-profile)/[`;link community`](#link-community-id) -- not reversible by an `;unlink` command (none exists for Groups yet).

---

## Admin

Bridge-wide access control and maintenance, gated to a Matrix server admin regardless of allowlisting.

### `;allow mxid|homeserver|room <value> [full|shadow]`

**Syntax:** `;allow mxid @user:example.org [full|shadow]`, `;allow homeserver example.org [full|shadow]` (tier defaults to `shadow` if you don't say), or `;allow room [!roomid:example.org|#alias:example.org]` (no tier -- always `shadow`; room is optional).

**What it does:** Grants third-party access to whoever/wherever `<value>` names:

- `mxid`/`homeserver` -- an exact Matrix user, or every user on a whole homeserver -- at `full` tier (self-service `;create profile`/`;link profile`, their own Profile Room, DM, Chat, every command) or `shadow` tier (an auto-minted identity that always mirrors their live Matrix display name/avatar, no self-service profile commands -- they still get `;follow` and ordinary posting/reacting, everywhere). `mxid` grants take effect immediately; `homeserver` (trusting an entire remote server's user base) asks for confirmation first, the same "reply confirm" flow as [`;delete profile`](#delete-profile--delete-channel--delete-community--delete-group).
- `room` -- any third party at all, from any homeserver, gets a shadow identity just by posting/reacting/replying in this one already-bridged room, with no individual or homeserver vetting needed. Always `shadow`, and scoped ONLY to that room and ONLY to passive content -- it does **not** satisfy the command-dispatch gate, so even `;follow` still needs its own `mxid`/`homeserver` grant; following would hand out a real signed `Follow` under this bridge's reputation and continue into a brand new room this grant was never meant to cover. Getting in via one room's grant gives no standing in any other room. Processed immediately even if the bot isn't currently a member of that room (warns, but still applies once it is). Takes a room ID, a `#alias:example.org` (resolved via the homeserver's directory), or no room at all -- run bare (just `;allow room`) from inside the room you want to allow, as long as it's already bridged as something (a Profile Room, Channel, Community, Group, DM/Chat room, ...); run from an unrelated, un-bridged room, it tells you there's nothing to default to and asks you to name one.

Precedence, most to least specific: an `mxid` grant (including a ban, see `;disallow` below) always wins outright; else a `homeserver` grant; else, only for the actions that consult it at all (not commands), a `room` grant for the room the action is actually happening in.

**Who can run it:** Matrix server admins only.

**Notes:** Granting `full` only gives the ABILITY to self-provision a profile -- it doesn't create one. Until they run `;create profile`/`;link profile` themselves, a full-granted user still acts through their shadow identity. See [CONFIG.md](CONFIG.md#bridge-third_party_access) for the master on/off switch this all sits under.

---

### `;disallow mxid|homeserver|room <value>`

**Syntax:** `;disallow mxid @user:example.org`, `;disallow homeserver example.org`, or `;disallow room [!roomid:example.org|#alias:example.org]` (room is optional, same default-to-current-room/alias support as `;allow room`).

**What it does:** For `homeserver`/`room`, removes that grant outright, immediately, no confirmation needed -- neither has a "banned" state, absence from the allowlist already means not allowed. For `mxid`, this is NOT a removal -- it stores an explicit ban for that one user, which overrides any homeserver- or room-level grant they'd otherwise inherit (so you can block one bad actor on an otherwise-allowed homeserver or room). To lift an mxid ban, just `;allow` them again at `full` or `shadow`.

Either way, never tears down any identity already provisioned under the grant -- everything (keys, followers, following, any Profile Room from a past full period) stays exactly as-is, just no longer reachable/authoritative. Use [`;delete profile`](#delete-profile--delete-channel--delete-community--delete-group) (still available to anyone who's ever linked one, allowlisted or not) to actually remove an identity.

**Who can run it:** Matrix server admins only.

---

### `;allowed`

**Syntax:** `;allowed`, no argument.

**What it does:** Lists every current third-party access grant, grouped by kind, with each one's tier (including a banned mxid), plus whether `bridge.third_party_access` is currently on or off.

**Who can run it:** Matrix server admins only.

---

### `;refresh [@user@instance.org]`

**Syntax:** `;refresh @user@instance.org`, or bare `;refresh` run inside that account's own Remote User Room (same "argument or implied by the room" convention as `;follow`). If bare `;refresh` is instead sent as a reply inside a poll's own thread, it refreshes that poll instead -- see [`;refresh poll`](#refresh-poll-reply-to-a-poll-or-anything-in-its-thread) -- never both at once.

**What it does:** Re-fetches the ghost's live ActivityPub actor document right now and brings everything this bridge keeps in sync with it up to date immediately, rather than waiting for whatever would normally trigger it (a reply/reaction, or an inbound `Update` some remote servers may never actually send):

- The ghost's own Matrix display name and avatar.
- The Remote User Room's name, avatar, and banner.
- The MSC4503 `m.external_handle` profile field -- brought into line with whatever `bridge.msc4503_external_handle` currently allows either way: set/refreshed if it's `profile` or `both`, actively removed if it's `off` or `events`, rather than leaving a stale value in place after a config change.
- The room's `history_visibility` -- brought into line with `bridge.world_readable_remote_rooms` either way, not just applied once at room creation.

**Who can run it:** Matrix server admins only.

**Notes:** Best-effort per piece -- a failure updating one part (say, the room avatar) doesn't stop the rest from refreshing.

---

## Widget vs. commands

The room widget is a UI wrapper around the exact same handlers the `;` commands use, with the same validation and the same feedback posted into the room. It covers `follow`, `unfollow`, `block`, `unblock`, `mute`, `unmute`, `dm`, `chat`, `import <url>`, `import follows`, `replace room`, `backfill` (including the admin-only custom count), `create profile`, `link profile`, `unlink profile`, `delete profile`, `banner` (with a convenience direct-upload variant that skips needing an `mxc://` URI first), the followers/following visibility toggle (via `set`), and a read-only following list.

It deliberately omits `;repost`, `;rejoin`, `;leave unfollowed`, `;create channel`/`;publish`/`;unpublish`/`;edit`/`;replace video`, `;joinguild`/`;leaveguild`/`;refresh guild`/`;refresh guild invite`, and `;allow`/`;disallow`/`;allowed`. None fit a simple button: repost needs a specific post to reply to, rejoin is a rare recovery tool, leave-unfollowed is a rare cleanup one, the PeerTube/Guild commands need specific reply targets or arguments the widget has no flow for, and the allowlist commands are admin-only bridge-wide configuration, not something to expose in an ordinary room widget -- several of these also need a confirmation step the widget doesn't have a flow for. It also simplifies `;delete profile`'s confirmation to a plain browser dialog instead of the chat reply flow, though both end up calling the same deletion logic underneath.

---

## Danger Zone

The commands below directly manipulate the binding between a Matrix room and a fediverse identity -- what room an identity lives in, or whether it exists at all. Used on the wrong room, or without understanding exactly what they do, they **can leave a bridged identity in a bugged/inconsistent state, or cause irreversible damage**. **Make sure you fully understand what a command does before using it.** Every command here is confirmation-gated for exactly this reason: running it alone only sends a warning explaining what's about to happen and asks you to reply "confirm" to a specific message -- nothing actually happens until you do.

Confirmed live 2026-08-27 (issue #6): a user who didn't realize `;link profile` permanently binds a room, ran it in a room that was already serving another purpose (a Shoot guild's Channel room), leaving the bridge's own bookkeeping registered as both at once -- then running `;replace room` on that already-corrupted room compounded the damage further, tombstoning it as if it had only ever been the one thing. Read the warning each of these sends. If you're not sure what it means, ask before confirming.

---

### `;link profile`

**Syntax:** `;link profile` to start, then reply "confirm" to the bot's own warning message to actually go through. Two-step and confirmation-gated. Run inside whichever room you want to bind your identity to.

**What it does:**
1. `;link profile` alone checks you're actually allowed to link this room -- you don't already have a linked profile, this room isn't already used by the bridge for something else, and you have enough power here to control it -- then sends a warning that this makes the room your permanent fediverse profile. Nothing is linked yet.
2. Replying "confirm" to that specific message binds your identity to the room, minting a new actor if you don't have one, or reattaching a previously-unlinked one. Sets the room's name/avatar to match your current Matrix profile (best-effort, needs the bot to have enough power in the room). If the room already has a topic and this is a brand-new identity, that topic becomes your bio.

**Who can run it:** Any local user without an already-linked profile, on a room they actually control that isn't already used by the bridge for something else -- both checked before the warning is even sent.

**Notes:** Unlike [`;create profile [<id>]`](#create-profile-id), this doesn't make a room for you. You need to already own or control one and have invited the bot with sufficient power. It's the option for people who'd rather use a room they already have. This isn't a one-off action -- it's a lasting change to what the room IS.

---

### `;unlink profile` / `;unlink profile <number|id>`

**Syntax:** `;unlink profile` to start, then reply "confirm" to the bot's own warning message to actually go through. Two-step and confirmation-gated. If you own more than one identity, `;unlink profile` alone lists them (numbered, same format as [`;id`](#id)) instead of warning about any one of them -- re-run as `;unlink profile <number|id>` to pick which.

**What it does:**
1. `;unlink profile` (or, with more than one identity, `;unlink profile <number|id>`) sends a warning that this room stops publishing for that identity immediately, and becomes linkable as a DIFFERENT identity by anyone with enough power here. Nothing is unlinked yet.
2. Replying "confirm" detaches your current room from that identity without telling the fediverse anything. No `Delete` is sent, and its followers, following, and keys are all preserved untouched. This is how you move a profile to a different room: unlink here, then `;link profile`/`;create profile` in the new room to reattach the exact same identity.

**Who can run it:** Any local user with a currently-linked profile.

**Notes:** The identity itself survives, so it's reversible in that sense -- but the room is immediately left open to becoming a completely different identity the moment anyone with enough power runs `;link profile` in it, so don't unlink until you're actually ready to relink somewhere. Contrast with `;delete profile`, which erases the identity itself and is not reversible at all. With more than one identity, the explicit selection step exists specifically so this can't land on the wrong one by mistake.

---

### `;replace room`

**Syntax:** `;replace room` to start, then reply "confirm" to the bot's own warning message to actually go through. Two-step and confirmation-gated. Run inside the room to be replaced.

**What it does:**
1. `;replace room` alone works out what kind of room this is and whether you're allowed to replace it, then sends a warning naming that kind (e.g. "your linked Profile Room"). Nothing is replaced yet.
2. Replying "confirm" creates a new room representing the exact same identity the current one does (a linked Profile Room, a Remote User Room, a ghost DM/Chat room, or your Notifications DM), bringing it up to date with anything the bridge has added since the old room was created (current room type/version, bridge tagging, the bot always being invited, and so on). Sets a proper `predecessor` link and tombstones the old room, renaming it with a "(Replaced ...)" suffix. This is entirely a local Matrix operation. Nothing goes out over ActivityPub, since the underlying identity isn't changing. For a Profile Room or Remote User Room, other local (non-ghost) members are automatically re-invited into the new room too, not just whoever ran the command.

**Who can run it:**
- Your own linked Profile Room: you, or a Matrix server admin.
- Someone else's Remote User Room: admin only.
- A DM/Chat/Notifications room: that room's owner, or an admin.

**Notes:** Anyone not automatically re-invited is left in the retired room, which stays around, just tombstoned. Nobody's forced out of it. Refuses outright, with no warning shown at all, if the room turns out to be ambiguously registered as more than one kind at once -- that means something's already wrong (a bug, not a normal state), and this command isn't the way to fix it; contact a Matrix server admin instead.

---

### `;replace dm/chat @user@instance.org`

**Syntax:** `;replace dm @user@instance.org` or `;replace chat @user@instance.org` (tagged ghost pills work too), then reply "confirm" to the bot's own warning message. Two-step and confirmation-gated -- but unlike `;replace room`, run from **anywhere**, not inside the room being replaced.

**What it does:** The DM/Chat-room equivalent of `;replace room`, for the one case that command can't itself reach: if a DM/Chat room ended up end-to-end encrypted (see `;dm`/`;chat`'s own notes -- this bridge has no encryption support at all), the bot can never read anything sent inside it, including a `;replace room` typed there. This command looks up your existing DM/Chat room with the target directly, so it works even against a room that's completely unreadable to the bot. Otherwise identical to `;replace room`'s own DM/Chat handling: new room, predecessor link, old room tombstoned. If the new room *also* comes out encrypted (the homeserver is still misconfigured), nothing is registered or tombstoned -- you're told plainly that a server admin needs to fix it first.

**Who can run it:** That room's owner, or a Matrix server admin. Disabled for third-party accounts at shadow tier, same as `;dm`/`;chat` themselves.

**Notes:** All warning/success notices go to wherever you ran this command, never into the old (possibly unreadable) room. Also works perfectly well on an ordinary, unencrypted DM/Chat room -- it's not encryption-specific, just the only option when it is.

---

### `;delete profile` / `;delete channel` / `;delete community` / `;delete group`

**Syntax:**
- `;delete profile` to start, then reply "confirm" to the bot's own warning message to actually go through. Two-step and confirmation-gated. If you own more than one identity, `;delete profile` alone lists them (numbered, same format as [`;id`](#id)) instead of warning about any one of them -- re-run as `;delete profile <number|id>` to pick which; with exactly one identity, it goes straight to the warning as always.
- `;delete channel <id>`, `;delete community <id>`, `;delete group <id>` -- same two-step, confirmation-gated shape, for a hosted PeerTube channel/Lemmy community/Group instead of your own profile. The `<id>` can be omitted (`;delete channel`/`;delete community`/`;delete group` bare) when run inside that entity's own room; with it, these work from anywhere. If you own more than one of that kind and don't run it inside one of their rooms, it lists them (by id) instead of warning about any one -- re-run naming one explicitly.

**What it does:**
1. Sends an itemized warning of exactly what will happen, and nothing else yet.
2. Replying "confirm" to that specific message (verified by looking up what it replied to) triggers the real deletion:
   - **Profile:** sends a signed `Delete` to every follower's inbox, kicks you from every other bridge-managed room that identity specifically has open (if you have other identities left, their own rooms and your shared Fediverse space are left alone), kicks you from your Fediverse space only if this was your last identity, unlinks the room, renames it to add "(Deleted)", and permanently erases that identity: keys, followers, following, everything. The room itself is left intact for you to leave whenever you like.
   - **Channel/community/group:** sends a signed `Delete` to every follower's inbox, renames the room to add "(Deleted)", and permanently erases that channel/community/group: keys, followers, everything. The room itself is left alone -- it can keep being used as an ordinary Matrix room, or its members can leave whenever they like.

**Who can run it:**
- `;delete profile`: any local user with a currently-linked profile. It always acts on whoever sends "confirm" and an identity that's actually theirs, never someone else's.
- `;delete channel`/`;delete community`/`;delete group`: that specific entity's owner, or a Matrix server admin -- re-verified fresh at confirmation time, not just when the warning was first sent.

**Notes:** Irreversible. Must be confirmed by replying to the bot's own warning specifically, not just any "confirm" message. Of everything in this Danger Zone, these are the only ones that can't be undone by any other command -- `;link`/`;unlink`/`;replace` all leave the underlying identity itself intact. Deleting your Active profile identity automatically promotes another of your remaining identities to Active; deleting your last one leaves you with none (a valid state, not an error). Deleting a channel/community/group has no such concept -- it's simply gone.
