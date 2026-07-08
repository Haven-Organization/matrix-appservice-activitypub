# Bot commands

The bridge is controlled from inside Matrix by either tagging/mentioning the bot, or starting a message with `;<command>` (e.g. `;follow @user@instance.org`). A few general rules apply to all of them:

- A command is only recognized on the **first line** of a message. Tagging the bot somewhere in a longer message won't misfire on a command word later in the text.
- Keyword matching is case-insensitive.
- Every command **except `;help`** only works for users on the bridge's own homeserver. A user on another Matrix server tagging the bot gets told commands aren't available to them. This stops someone from squatting usernames on your domain or riding your bridge's reputation to follow arbitrary fediverse accounts from outside it.
- The bridge's own bot and ghost accounts never trigger commands, so a mirrored post that happens to start with `;` is never misread as one.
- Tagging the bot with no recognized keyword shows the full help message (if nothing else was said) or a short pointer to `;help` (if something else was said that just wasn't recognized).
- If a command was sent as a thread reply, the bot's response(s) stay in that same thread.
- Most of these are also available as buttons in the bridge's room widget (`;widget`). See [Widget vs. commands](#widget-vs-commands) at the bottom.

## Contents

- [`;help` / `;help all`](#help--help-all)
- [`;create profile`](#create-profile)
- [`;link profile`](#link-profile)
- [`;unlink profile`](#unlink-profile)
- [`;delete profile`](#delete-profile)
- [`;follow`](#follow-userinstanceorg)
- [`;unfollow`](#unfollow-userinstanceorg)
- [`;following`](#following)
- [`;hide` / `;show`](#hide-followers--hide-following--show-followers--show-following)
- [`;block`](#block-userinstanceorg)
- [`;unblock`](#unblock-userinstanceorg)
- [`;mute`](#mute-userinstanceorg)
- [`;unmute`](#unmute-userinstanceorg)
- [`;banner`](#banner-mxcservermediaid)
- [`;dm`](#dm-userinstanceorg)
- [`;chat`](#chat-userinstanceorg)
- [`;import <url>`](#import-url)
- [`;import follows`](#import-follows-or-import-following)
- [`;replace room`](#replace-room)
- [`;rejoin`](#rejoin-room_id-othermatrixid)
- [`;repost`](#repost-caption-reply-to-a-mirrored-fediverse-post)
- [`;backfill`](#backfill-n)
- [`;widget`](#widget)
- [Widget vs. commands](#widget-vs-commands)

---

## `;help` / `;help all`

**Syntax:** `;help`, or tag the bot with nothing else recognizable. `;help all` shows an expanded list.

**What it does:** Sends a table of commands as a rich `m.text` message (not a notice, so it isn't visually suppressed by "hide notices" client settings). Plain `;help` shows only the everyday commands: `help`, `create profile`, `follow`, `following`, `dm`, `chat`, `import <url>`, `repost`, `banner`. `;help all` appends the advanced/maintenance set: `link profile`, `unlink profile`, `delete profile`, `replace room`, `rejoin`, `hide`/`show`, `block`/`unblock`, `import follows`, `mute`/`unmute`, `backfill`, `widget`.

**Who can run it:** Anyone, including users on other Matrix homeservers. This is the one exception to the local-users-only rule.

**Notes:** Read-only, no side effects.

---

## `;create profile`

**Syntax:** `;create profile`, no other argument.

**What it does:** One-shot setup that would otherwise take several manual steps. Creates a brand-new Matrix room (bot as creator/admin), invites you and sets your power level to 99 (one below the bot), copies your current Matrix display name and avatar onto the room, tags it as a bridge-made Profile Room, adds the room widget, adds it to your personal Fediverse space, and mints (or, if you'd previously run `;unlink profile`, reattaches) your actual ActivityPub actor (`username@bridge-domain`) to it. Your username is derived from your Matrix localpart.

**Who can run it:** Any local Matrix user without an already-linked profile. If you already have one, this just reports its room and does nothing further.

**Notes:** Reattaching an unlinked identity preserves its followers and following exactly as before. If room creation fails, you're told you can make your own room and use `;link profile` instead.

---

## `;link profile`

**Syntax:** `;link profile`, run inside whichever room you want to bind your identity to.

**What it does:** Binds your identity to the room the command was run in, minting a new actor if you don't have one, or reattaching a previously-unlinked one. Sets the room's name/avatar to match your current Matrix profile (best-effort, needs the bot to have enough power in the room). If the room already has a topic and this is a brand-new identity, that topic becomes your bio.

**Who can run it:** Any local user without an already-linked profile.

**Notes:** Unlike `;create profile`, this doesn't make a room for you. You need to already own or control one and have invited the bot with sufficient power. It's the option for people who'd rather use a room they already have.

---

## `;unlink profile`

**Syntax:** `;unlink profile`.

**What it does:** Detaches your current room from your identity without telling the fediverse anything. No `Delete` is sent, and your followers, following, and keys are all preserved untouched. This is how you move your profile to a different room: unlink here, then `;link profile`/`;create profile` in the new room to reattach the exact same identity.

**Who can run it:** Any local user with a currently-linked profile.

**Notes:** Fully reversible, since the identity itself survives. Contrast with `;delete profile`, which is not.

---

## `;delete profile`

**Syntax:** `;delete profile` to start, then reply "confirm" to the bot's own warning message to actually go through. Two-step and confirmation-gated.

**What it does:**
1. `;delete profile` alone sends an itemized warning of exactly what will happen, and nothing else yet.
2. Replying "confirm" to that specific message (verified by looking up what it replied to) triggers the real deletion: sends a signed `Delete` to every follower's inbox, kicks you from every other bridge-managed room you're in (except the Profile Room itself), kicks you from your Fediverse space, unlinks the room, renames it to add "(Deleted)", and permanently erases the identity: keys, followers, following, everything. The room itself is left intact for you to leave whenever you like.

**Who can run it:** Any local user with a currently-linked profile. It always acts on whoever sends "confirm" and their own profile, never someone else's.

**Notes:** Irreversible. Must be confirmed by replying to the bot's own warning specifically, not just any "confirm" message.

---

## `;follow @user@instance.org`

**Syntax:** `;follow @user@instance.org`, or no argument at all if run from inside that account's own room. A tagged mention pill, of one of the bridge's ghost users or of another local user, works the same as typing the handle, and wins if both are somehow present.

**What it does:** Follows the target as your own linked actor.
- **Genuinely remote target:** creates or reuses a Remote User Room, invites you into it, and delivers a signed `Follow`.
- **Actually another local user on this bridge** (tagged directly, or resolved via their own `@user@yourdomain` handle): handled entirely in-process. Invites you straight into their existing Profile Room (never fabricating a ghost for someone with a real Matrix account), records the follow both ways, and DMs them a notification.

**Who can run it:** Requires a linked profile first.

**Notes:** No-op with a notice if you're already following them. If delivery fails, you're told why but still joined to the room; the Follow isn't retried automatically.

---

## `;unfollow @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;follow`.

**What it does:**
- **Remote target:** kicks you from their Remote User Room, which triggers a real `Undo(Follow)` the same way leaving the room yourself would.
- **Local target:** pure bookkeeping. Removes the follow record without kicking you from their Profile Room, since it's their real room and possibly has other unrelated members.

**Who can run it:** Any local user. Fails gracefully if there's no such follow.

---

## `;following`

**Syntax:** `;following`, no argument.

**What it does:** Lists every account your linked actor follows, alphabetically, each with a link to its room where one exists.

**Who can run it:** Any local user.

---

## `;hide followers` / `;hide following` / `;show followers` / `;show following`

**Syntax:** `;hide followers`, `;hide following`, `;show followers`, `;show following`. The collection name is required.

**What it does:** Toggles whether your public followers/following collection exposes its member list to remote viewers. The reported count is always public regardless; only the list itself is withheld or shown (same semantics as Mastodon's "hide network"). Visible by default.

**Who can run it:** Only your own linked actor, and only from inside your own linked Profile Room.

**Notes:** Purely a privacy/cosmetic toggle. Doesn't affect who can follow you or see your posts.

---

## `;block @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;follow`.

**What it does (broader than `;mute`, which it subsumes):**
- Cuts any existing follow relationship immediately in both directions: a real `Undo(Follow)` if you were following them, or just a dropped record if they were following you.
- Kicks you from their Remote User Room (never a local target's own Profile Room), and from any open DM/Chat room between you.
- Declines any future `Follow` from them with a real `Reject`.
- Silences them exactly like `;mute`.

**Who can run it:** Requires a linked profile. You can't block yourself.

**Notes:** Does not stop their posts from mirroring. The shared Remote User Room may still be needed by other followers, or by a repost someone else made. `;unblock` only lifts the block flag itself; it does not restore the follow or re-invite you anywhere. Redo `;follow`/`;dm`/`;chat` yourself if you want that back.

---

## `;unblock @user@instance.org`

**Syntax:** Same resolution as `;block`.

**What it does:** Removes the block record only. Nothing else that changed as a side effect of blocking is restored automatically.

**Who can run it:** Requires a linked profile. No-op if not currently blocked.

---

## `;mute @user@instance.org`

**Syntax:** Same argument/no-argument resolution as `;block`.

**What it does:** Suppresses notifications about the target and auto-invites into a room because of them (a fresh DM/Chat they open, or being pulled into a mention). Doesn't touch any existing follow, room membership, or mirroring; their posts, replies, and reactions keep flowing normally. Explicitly running `;dm`/`;chat` toward a muted account still works, since that's your own deliberate action.

**Who can run it:** Requires a linked profile. Can't mute yourself. No-op if already muted.

---

## `;unmute @user@instance.org`

**Syntax:** Same resolution as `;mute`.

**What it does:** Undoes `;mute`.

**Who can run it:** Requires a linked profile. No-op if not currently muted.

---

## `;banner mxc://server/mediaid`

**Syntax:** `;banner mxc://server/mediaid`. The argument must start with `mxc://` (upload the image to any room the bot can see to get this URI). Run inside your own linked Profile Room.

**What it does:** Sets your fediverse profile's banner/header image, distinct from your avatar. Matrix has no stable room-level banner state yet, so it's recorded via MSC4221's `m.room.banner` (under that MSC's own unstable prefix), and every run immediately pushes a signed `Update` to your followers.

**Who can run it:** Only your own profile, only from inside your linked Profile Room. Also reachable via the widget's file-upload control, which skips needing an `mxc://` URI in hand first.

---

## `;dm @user@instance.org`

**Syntax:** Same argument/no-argument resolution pattern (no argument from inside the account's own room). Tagged ghost mention pills supported.

**What it does:** Starts, or reuses (re-inviting you if you'd left), a 1:1 `Note`-based direct-message room with the target.

**Who can run it:** Requires a linked profile. Refuses if the resolved handle is actually a local bridge user; just start an ordinary Matrix DM with them directly.

**Notes:** Distinct from `;chat` even for the same account. Different rooms, different ActivityPub message shapes.

---

## `;chat @user@instance.org`

**Syntax:** Same resolution as `;dm`.

**What it does:** Starts or reuses a 1:1 `ChatMessage`-based room (Pleroma/Akkoma's separate instant-messaging concept), the counterpart to `;dm`'s `Note`-based room. Warns, but still creates the room, if the target's actor doesn't advertise chat support.

**Who can run it:** Requires a linked profile; refuses for a local target the same way `;dm` does.

**Notes:** The other way to start one is inviting the ghost's own Matrix account directly into a fresh DM.

---

## `;import <url>`

**Syntax:** `;import <fediverse post URL>`, must start with `http://` or `https://`.

**What it does:** Fetches a single post by URL and mirrors it regardless of whether you follow its author, creating or reusing a Remote User Room for them (but not actually following them) and inviting you into it. Already-mirrored posts aren't duplicated. If the URL is actually a local bridge user's own post, you're just invited into their real Profile Room instead. Falls back to alternate URL forms for instances that don't serve their "pretty" post URLs as fetchable JSON. If the post is a reply to something already tracked in the same room, it's mirrored as a proper threaded reply.

**Who can run it:** Any local user. No linked profile required, since importing doesn't send anything out as you.

**Notes:** Also reachable via the widget's URL field.

---

## `;import follows` (or `;import following`)

**Syntax:** Must be sent as a reply to a message with an uploaded file, in a room the bot is in. First export your follows list from the source account (Pleroma/Akkoma: Settings > Data export; Mastodon: Preferences > Import and export > Follows), upload that file, then reply to the upload with this command.

**What it does:** Parses either Pleroma/Akkoma's one-handle-per-line format or Mastodon's CSV export, then follows every handle in the background, exactly like running `;follow` for each one. Already-followed accounts (including your own account showing up in its own export) are skipped silently. Posts a single summary (followed/skipped/failed counts, with each failure's specific reason) once finished.

**Who can run it:** Requires a linked profile.

**Notes:** Long-running and asynchronous. The summary may arrive minutes later. The widget's equivalent accepts the file directly, without needing it to already be an upload you're replying to.

---

## `;replace room`

**Syntax:** `;replace room`, run inside the room to be replaced.

**What it does:** Creates a new room representing the exact same identity the current one does (a linked Profile Room, a Remote User Room, a ghost DM/Chat room, or your Notifications DM), bringing it up to date with anything the bridge has added since the old room was created (current room type/version, bridge tagging, the bot always being invited, and so on). Sets a proper `predecessor` link and tombstones the old room, renaming it with a "(Replaced ...)" suffix. This is entirely a local Matrix operation. Nothing goes out over ActivityPub, since the underlying identity isn't changing. For a Profile Room or Remote User Room, other local (non-ghost) members are automatically re-invited into the new room too, not just whoever ran the command.

**Who can run it:**
- Your own linked Profile Room: you, or a Matrix server admin.
- Someone else's Remote User Room: admin only.
- A DM/Chat/Notifications room: that room's owner, or an admin.

**Notes:** Anyone not automatically re-invited is left in the retired room, which stays around, just tombstoned. Nobody's forced out of it.

---

## `;rejoin <room_id> [@other:matrix.id]`

**Syntax:** `;rejoin <room_id>` to invite yourself, or add `@other:matrix.id` to invite someone else.

**What it does:** Force-attempts a fresh invite into a room the bridge manages, a manual recovery tool for a lockout (e.g. a room's join rule got switched to knock-only with nobody left to approve one). Never triggers an ActivityPub `Follow` as a side effect; following only ever happens via `;follow` itself.

**Who can run it:**
- Inviting only yourself: any Remote User Room (even an already-replaced one no longer live-tracked), or any room that currently is, or ever was, your own linked Profile Room.
- Inviting anyone else, or targeting anything else: admin only. An admin can target any room this way, even ones the bridge doesn't otherwise recognize, as a true last resort.

---

## `;repost <caption>` (reply to a mirrored fediverse post)

**Syntax:** Sent as a reply to a tracked post, with a required, non-empty caption.

**What it does:** Unlike reacting to a post with 🔁 (which sends a signed `Announce` -- a bare boost/repost with no commentary of your own), this creates a brand-new post of your own with the caption as its text, marked as quoting the original (for receivers that render real quote cards) with a plain link appended for those that don't. Delivered like an ordinary post: to your followers, the original author, and anyone mentioned in the caption. Always rendered into your own Profile Room, never wherever the command was actually run.

**Who can run it:** Requires a linked profile.

---

## `;backfill [N]`

**Syntax:** `;backfill` (uses the configured default count) or `;backfill N`. Run inside a Remote User Room: at the room root to backfill that account's outbox, or as a reply inside an existing thread to backfill that specific conversation's replies instead.

**What it does:** Pulls up to N posts (or that thread's replies) through the exact same mirroring path a live delivery uses, so a backfilled post is indistinguishable from a live one and already-mirrored posts are never duplicated. Runs in the background, with a summary posted once done.

**Who can run it:** Any local user, with the default count. Only a Matrix server admin can specify a custom N, since an arbitrarily large one means unbounded outbound fetches against a remote server.

**Notes:** A brand-new follow's first room-join already triggers one automatic backfill on its own. This is for topping that up or pulling in a specific thread.

---

## `;widget`

**Syntax:** `;widget`, run in whatever room should get it.

**What it does:** Adds the bridge's room widget, buttons for most of the commands above, scoped to whatever kind of room it's added to.

**Who can run it:** Any local user, in any room.

**Notes:** Mints a fresh widget every time. Running it again adds a second instance rather than being a no-op; remove a stale one yourself via your client's widgets panel.

---

## Widget vs. commands

The room widget is a UI wrapper around the exact same handlers the `;` commands use, with the same validation and the same feedback posted into the room. It covers `follow`, `unfollow`, `block`, `unblock`, `mute`, `unmute`, `dm`, `chat`, `import <url>`, `import follows`, `replace room`, `backfill` (including the admin-only custom count), `create profile`, `link profile`, `unlink profile`, `delete profile`, `banner` (with a convenience direct-upload variant that skips needing an `mxc://` URI first), the `hide`/`show` toggle, and a read-only following list.

It deliberately omits `;repost` and `;rejoin`. Neither fits a simple button: repost needs a specific post to reply to, and rejoin is a rare recovery tool. It also simplifies `;delete profile`'s confirmation to a plain browser dialog instead of the chat reply flow, though both end up calling the same deletion logic underneath.
