"""Static (no-JS) HTML rendering of a local actor's profile and posts, for a
plain browser landing on an ``/actor/{username}`` or ``/actor/{username}/notes/{id}``
link (e.g. one shown by a remote fediverse client as "View profile"/the post's
own permalink) instead of a fediverse client speaking ActivityPub. Those same
routes keep serving JSON to an AP-speaking caller -- see
``bridge.activitypub.routes._prefers_html`` for the content negotiation this
feeds into.

Deliberately dependency-free (no Jinja2 -- this bridge's own requirements.txt
stays minimal) and deliberately static: no inline ``<script>`` anywhere, so
the page is cheap to serve and can't be used as an XSS vector via anything
relayed from a remote instance. Post ``content_html`` is safe to embed
as-is -- it already passed through ``bridge.activitypub.sanitize``'s
allow-listed tag/attribute sanitizer on the way into Matrix, the same
guarantee every mirrored post already relies on.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone

_VARIATION_SELECTOR_16 = "️"
_THUMBS_UP = "\U0001F44D"


def _is_like_key(key: str) -> bool:
    """Whether a mirrored reaction's key is a plain thumbs up -- the only
    key a bare ``Like`` activity ever carries (see
    ``bridge.inbox_dispatch._DEFAULT_LIKE_EMOJI``), and the one this bridge's
    own outbound side (``bridge.reaction_bridge._is_favorite_emoji``) treats
    as a "like" rather than a distinct emoji reaction -- kept consistent
    with that same narrow definition here so a skin-toned thumbs up still
    counts as its own emoji reaction, not a like."""
    return key.replace(_VARIATION_SELECTOR_16, "") == _THUMBS_UP


def _is_custom_emoji_shortcode(key: str) -> bool:
    """Whether ``key`` looks like a custom ActivityPub emoji shortcode
    (e.g. ``":blobcat:"``) rather than a literal unicode emoji."""
    return len(key) > 2 and key.startswith(":") and key.endswith(":")


@dataclass
class PersonRef:
    """A resolved (avatar/name/profile-link) identity for anyone shown in a
    popup list -- a follower/following entry, or a reactor/liker under a
    post. Always pre-resolved by ``bridge.activitypub.routes`` (a local
    actor's own record, or a remote account's synced ``GhostProfile``)
    before it ever reaches this dependency-free rendering module -- same
    split as ``PostView.author_display_name_html``/``author_profile_url``.
    ``profile_url`` is this bridge's own ``/actor/{username}`` page for a
    LOCAL account, or the remote account's own real actor IRI (content-
    negotiated into their own instance's profile page by a browser) for a
    remote one -- ``None`` only for a plain Matrix user who reacted/followed
    without ever having a fediverse identity of their own, in which case
    the name is shown unlinked."""

    display_name: str
    display_name_html: str
    avatar_url: str | None
    profile_url: str | None
    # The account's ``@user@domain`` fediverse handle (or ``@user:server``
    # MXID for a plain Matrix user with no fediverse identity) -- shown in
    # small dim text under the display name, unless it's None or identical
    # to the display name (several resolution fallbacks use the handle AS
    # the display name; repeating it as its own line would just be noise).
    handle: str | None = None


@dataclass
class ReactionEvent:
    """One raw ``m.reaction``, reduced to just what ``summarize_reactions``
    needs to group it -- ``key`` is the literal unicode emoji or a
    ``:shortcode:``, ``event_id`` is looked up against
    ``custom_emoji_by_event`` to resolve a custom emoji's image, and
    ``person`` is the reactor's already-resolved identity (see
    ``bridge.activitypub.routes._resolve_reactor``)."""

    key: str
    event_id: str
    person: PersonRef


@dataclass
class EmojiReaction:
    # The literal unicode emoji, or a custom emoji's :shortcode: (used as
    # this pill's alt/title text either way).
    key: str
    count: int = 0
    # Set only for a custom (ActivityPub-extension) emoji -- see
    # summarize_reactions' identical distinction to the pre-existing
    # emoji_counts/custom_emoji_counts split.
    image_url: str | None = None
    people: list[PersonRef] = field(default_factory=list)


@dataclass
class ReactionSummary:
    like_count: int = 0
    likers: list[PersonRef] = field(default_factory=list)
    # Every distinct emoji/custom-emoji reaction, each gets its own pill now
    # (see _reactions_html) -- combined unicode+custom in first-seen order,
    # unlike the old separate emoji_counts/custom_emoji_counts lists, so a
    # page interleaving both types still shows them in the order people
    # actually reacted.
    emojis: list[EmojiReaction] = field(default_factory=list)


def summarize_reactions(
    reaction_events: list[ReactionEvent], custom_emoji_by_event: dict[str, str] | None = None
) -> ReactionSummary:
    """Groups already-resolved ``ReactionEvent``s into a like count (with
    each liker's identity) and one ``EmojiReaction`` per distinct emoji/
    custom-emoji (each with every person who used it) -- ``custom_emoji_by_event``
    (from ``ActorRepository.get_custom_emoji_by_reaction_event_ids``, keyed
    by each reaction event's own ``event_id``) resolves a custom emoji
    shortcode to its actual image URL, grouped by THAT (not shortcode text
    alone, since the same shortcode means different images on different
    remote instances -- see ``bridge.inbox_dispatch._resolve_custom_emoji_image``).
    A redacted reaction event has its content stripped to ``{}`` by Matrix's
    own redaction rules, so it naturally never reaches here as a
    ``ReactionEvent`` in the first place (see
    ``bridge.activitypub.routes._build_post_view``, which skips it before
    constructing one)."""
    custom_emoji_by_event = custom_emoji_by_event or {}
    like_count = 0
    likers: list[PersonRef] = []
    order: list[str] = []  # group keys, first-seen order (unicode and custom interleaved)
    groups: dict[str, EmojiReaction] = {}
    for reaction in reaction_events:
        key = reaction.key
        if _is_like_key(key):
            like_count += 1
            likers.append(reaction.person)
            continue
        if _is_custom_emoji_shortcode(key):
            image_url = custom_emoji_by_event.get(reaction.event_id)
            if not image_url:
                continue  # image never resolved (fetch failed, or a redelivered/stale event) -- nothing to show
            group_key = f"custom:{image_url}"
            if group_key not in groups:
                groups[group_key] = EmojiReaction(key=key, image_url=image_url)
                order.append(group_key)
        else:
            group_key = f"emoji:{key}"
            if group_key not in groups:
                groups[group_key] = EmojiReaction(key=key)
                order.append(group_key)
        group = groups[group_key]
        group.count += 1
        group.people.append(reaction.person)
    return ReactionSummary(like_count=like_count, likers=likers, emojis=[groups[k] for k in order])


@dataclass
class PostView:
    """Everything a post card needs to render, already resolved from
    whatever mix of ``ActorRepository``/Matrix state it took to get there --
    see ``bridge.activitypub.routes._build_post_view``."""

    ap_object_id: str
    source_url: str
    author_display_name: str
    # Pre-escaped, with any custom-emoji :shortcode: in the name already
    # inlined as an <img> (bridge.activitypub.routes._resolve_post_author) --
    # use this directly in HTML, never re-escape author_display_name itself
    # for that purpose (it stays plain text, for the page <title> and the
    # no-avatar fallback initial).
    author_display_name_html: str
    author_handle: str
    author_avatar_url: str | None
    # This bridge's own /actor/{username} page for a LOCAL author, or the
    # remote account's own real actor_id (their ActivityPub actor IRI, not
    # a guessed profile-page URL -- see
    # bridge.activitypub.routes._resolve_post_author's docstring for why)
    # for a remote one -- either way, wherever the byline should link to.
    # None only if neither could be determined at all.
    author_profile_url: str | None
    origin_server_ts: int
    content_html: str
    attachment: dict | None
    reactions: ReactionSummary


def _fmt_timestamp(origin_server_ts: int) -> str:
    if not origin_server_ts:
        return ""
    dt = datetime.fromtimestamp(origin_server_ts / 1000, tz=timezone.utc)
    return dt.strftime("%b %-d, %Y, %H:%M UTC")


def _avatar_html(display_name: str, avatar_url: str | None, *, css_class: str = "avatar") -> str:
    if avatar_url:
        url = html.escape(avatar_url, quote=True)
        # Links straight to the image file itself (its only resolution --
        # this bridge doesn't generate separate avatar thumbnails) rather
        # than any kind of JS lightbox/overlay, so this stays a plain,
        # static anchor like every other link on the page.
        return (
            f'<a class="avatar-link" href="{url}" target="_blank" rel="noopener">'
            f'<img class="{css_class}" src="{url}" alt="" loading="lazy"></a>'
        )
    initial = html.escape((display_name or "?").strip()[:1].upper() or "?")
    return f'<div class="{css_class} avatar-fallback">{initial}</div>'

def _attachment_html(attachment: dict | None) -> str:
    if not attachment:
        return ""
    kind = attachment.get("type")
    url = html.escape(str(attachment.get("url") or ""), quote=True)
    if not url:
        return ""
    if kind == "Image":
        return f'<div class="post-media"><img src="{url}" loading="lazy" alt=""></div>'
    if kind == "Video":
        thumbnail_url = attachment.get("thumbnail_url")
        # preload="none" means the browser won't fetch a frame to show on
        # its own -- without a poster, that leaves the player blank until
        # playback actually starts. See bridge.activitypub.routes._build_post_view
        # for where this comes from (Matrix's own auto-generated video
        # thumbnail, when the upload had one).
        poster_attr = f' poster="{html.escape(str(thumbnail_url), quote=True)}"' if thumbnail_url else ""
        return f'<div class="post-media"><video src="{url}"{poster_attr} controls preload="none"></video></div>'
    if kind == "Audio":
        return f'<div class="post-media"><audio src="{url}" controls preload="none"></audio></div>'
    return f'<div class="post-attachment-link"><a href="{url}">Attachment</a></div>'


# Once there are this many distinct pills (likes + one per distinct emoji)
# to show, the 6th slot is replaced by a "+X more" pill instead -- X is
# however many distinct reactions that leaves out, not the total number of
# individual reactions (someone reacting twice with the same emoji is still
# one pill/one slot).
_MAX_REACTION_PILLS = 6


def _emoji_icon_html(reaction: "EmojiReaction", *, css_class: str) -> str:
    if reaction.image_url:
        url = html.escape(reaction.image_url, quote=True)
        title = html.escape(reaction.key)
        return f'<img class="{css_class}" src="{url}" alt="{title}" title="{title}" loading="lazy">'
    return f'<span class="{css_class}">{html.escape(reaction.key)}</span>'


def _person_list_item_html(person: "PersonRef", *, trailing_html: str = "") -> str:
    avatar = _avatar_html(person.display_name, person.avatar_url, css_class="avatar-sm")
    if person.profile_url:
        url = html.escape(person.profile_url, quote=True)
        name_html = f'<a href="{url}">{person.display_name_html}</a>'
    else:
        name_html = person.display_name_html
    handle_html = ""
    if person.handle and person.handle != person.display_name:
        handle_html = f'<span class="person-handle">{html.escape(person.handle)}</span>'
    return (
        f'<li class="person-row">{avatar}'
        f'<span class="person-ident"><span class="person-name">{name_html}</span>{handle_html}</span>'
        f"{trailing_html}</li>"
    )


def _person_list_html(people: list["PersonRef"], *, empty_message: str, trailing_html_for=None) -> str:
    if not people:
        return f'<div class="empty-state">{html.escape(empty_message)}</div>'
    items = "".join(
        _person_list_item_html(p, trailing_html=trailing_html_for(p) if trailing_html_for else "")
        for p in people
    )
    return f'<ul class="person-list">{items}</ul>'


# Closing links point here, not "#" -- an EMPTY fragment is spec'd
# (HTML "scroll to the fragment" algorithm) to scroll to the very top of the
# document, which is exactly the "closing a popup jumps me back to the top
# of the page" bug this was. A fragment that doesn't match any element's id
# has no such effect (there's nothing to scroll to, so nothing happens) while
# still un-targeting whichever ".modal-overlay:target" was showing, since the
# URL's fragment no longer matches its id either way -- so this closes the
# popup without moving the scroll position at all. Must never collide with a
# real dialog id (reactions-N-likes/-reactions, followers-modal, following-modal).
_MODAL_CLOSE_HREF = "#_close"


def _modal_dialog_html(dialog_id: str, *, title_html: str, body_html: str) -> str:
    # .modal-backdrop is a SIBLING of .modal-dialog, not its parent -- it
    # covers the whole overlay and closes on click, but .modal-dialog is
    # stacked above it (see the CSS: position:relative + later in paint
    # order) and fills its own rectangle, so a click actually inside the
    # dialog (including its own nested links -- a follower's profile link,
    # the tab labels, ...) hits that instead and never reaches the backdrop
    # underneath. Keeping it a sibling rather than wrapping the dialog in one
    # single clickable element avoids ever nesting an <a> inside an <a>,
    # which is invalid HTML and would make click targeting inside the
    # dialog unreliable.
    return f"""
<div id="{dialog_id}" class="modal-overlay">
  <a class="modal-backdrop" href="{_MODAL_CLOSE_HREF}" aria-label="Close"></a>
  <div class="modal-dialog">
    <a class="modal-close" href="{_MODAL_CLOSE_HREF}" aria-label="Close">&larr; Back</a>
    {title_html}
    {body_html}
  </div>
</div>
""".strip()


def _person_list_dialog_html(dialog_id: str, *, title: str, people: list["PersonRef"], empty_message: str) -> str:
    title_html = f'<h2 class="modal-title">{html.escape(title)}</h2>'
    body_html = _person_list_html(people, empty_message=empty_message)
    return _modal_dialog_html(dialog_id, title_html=title_html, body_html=body_html)


def _reactions_dialog_html(dialog_id: str, reactions: "ReactionSummary", *, default_tab: str) -> str:
    """One popup, reachable from two different anchor ids (``{dialog_id}-likes``
    and ``{dialog_id}-reactions``, see ``_reactions_html``) so it opens on
    whichever tab matches the pill actually clicked -- pure CSS can't
    otherwise vary a single element's default state by which of two links
    led to it, so this renders two near-identical copies (same tab markup,
    differing only in which ``<input>`` starts ``checked``) rather than
    reaching for JS. Both tabs are still freely switchable by hand once
    open, same as any radio-button CSS-tabs pattern."""
    likes_checked = " checked" if default_tab == "likes" else ""
    reactions_checked = "" if default_tab == "likes" else " checked"

    likes_body = _person_list_html(reactions.likers, empty_message="No likes yet.")
    reaction_items = []
    for reaction in reactions.emojis:
        icon_html = _emoji_icon_html(reaction, css_class="person-row-emoji")
        for person in reaction.people:
            reaction_items.append(_person_list_item_html(person, trailing_html=icon_html))
    reactions_body = (
        f'<ul class="person-list">{"".join(reaction_items)}</ul>' if reaction_items
        else '<div class="empty-state">No reactions yet.</div>'
    )

    tabs_html = f"""
<input type="radio" class="tab-radio tab-radio-likes" name="{dialog_id}-tab" id="{dialog_id}-tab-likes"{likes_checked}>
<label class="tab-label" for="{dialog_id}-tab-likes">Likes 👍</label>
<input type="radio" class="tab-radio tab-radio-reactions" name="{dialog_id}-tab" id="{dialog_id}-tab-reactions"{reactions_checked}>
<label class="tab-label" for="{dialog_id}-tab-reactions">Reactions 😂</label>
<div class="tab-panel likes-panel">{likes_body}</div>
<div class="tab-panel reactions-panel">{reactions_body}</div>
""".strip()
    return _modal_dialog_html(dialog_id, title_html="", body_html=tabs_html)


def _reactions_html(post_dialog_id: str, reactions: ReactionSummary) -> str:
    if not reactions.like_count and not reactions.emojis:
        return ""

    likes_href = f"#{post_dialog_id}-likes"
    reactions_href = f"#{post_dialog_id}-reactions"

    pills: list[str] = []
    if reactions.like_count:
        pills.append(
            f'<a class="reaction-pill reaction-likes" href="{likes_href}">'
            f'<span class="reaction-icon">👍</span> {reactions.like_count}</a>'
        )
    for reaction in reactions.emojis:
        icon_html = _emoji_icon_html(reaction, css_class="reaction-icon")
        pills.append(f'<a class="reaction-pill" href="{reactions_href}">{icon_html} {reaction.count}</a>')

    if len(pills) >= _MAX_REACTION_PILLS:
        shown = pills[: _MAX_REACTION_PILLS - 1]
        remaining = len(pills) - (_MAX_REACTION_PILLS - 1)
        # Whichever tab the overflowed pills would have opened -- they're
        # never likes (there's only ever one likes pill), so this always
        # points at the reactions tab.
        shown.append(f'<a class="reaction-pill reaction-more" href="{reactions_href}">+{remaining} more</a>')
        pills = shown

    dialogs = ""
    if reactions.like_count:
        dialogs += _reactions_dialog_html(f"{post_dialog_id}-likes", reactions, default_tab="likes")
    if reactions.emojis:
        dialogs += _reactions_dialog_html(f"{post_dialog_id}-reactions", reactions, default_tab="reactions")

    return f'<div class="post-reactions">{"".join(pills)}</div>{dialogs}'


def _byline_html(post: "PostView") -> str:
    display_name = post.author_display_name_html
    handle = html.escape(post.author_handle)
    if not post.author_profile_url:
        return f'<div class="display-name">{display_name}</div><div class="handle">{handle}</div>'
    profile_url = html.escape(post.author_profile_url, quote=True)
    return (
        f'<div class="display-name"><a href="{profile_url}">{display_name}</a></div>'
        f'<div class="handle"><a href="{profile_url}">{handle}</a></div>'
    )


def _post_card_html(post: PostView, index: int, *, highlighted: bool = False) -> str:
    classes = "post" + (" post-focused" if highlighted else "")
    source_url = html.escape(post.source_url, quote=True)
    return f"""
<article class="{classes}">
  <a class="post-permalink" href="{source_url}" title="View on source instance" target="_blank" rel="nofollow noopener noreferrer">&#8599;</a>
  <div class="post-header">
    {_avatar_html(post.author_display_name, post.author_avatar_url)}
    <div class="post-author">
      {_byline_html(post)}
    </div>
    <a class="post-time" href="{source_url}">{html.escape(_fmt_timestamp(post.origin_server_ts))}</a>
  </div>
  <div class="post-content">{post.content_html}</div>
  {_attachment_html(post.attachment)}
  {_reactions_html(f"reactions-{index}", post.reactions)}
</article>
""".strip()


_BASE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #15181c;
  --bg-elevated: #1c2027;
  --border: #2e3338;
  --text: #e7e9ea;
  --text-dim: #8b98a5;
  --accent: #6d94ff;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f8fa;
    --bg-elevated: #ffffff;
    --border: #e1e8ed;
    --text: #0f1419;
    --text-dim: #536471;
    --accent: #2563eb;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.4;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.page { max-width: 640px; margin: 0 auto; min-height: 100vh; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }
.banner { width: 100%; height: 200px; background-size: cover; background-position: center; background-color: var(--border); }
.profile-header { padding: 0 16px 16px; border-bottom: 1px solid var(--border); }
.avatar-lg { width: 96px; height: 96px; border-radius: 50%; border: 4px solid var(--bg-elevated); margin-top: -48px; object-fit: cover; display: block; background: var(--bg-elevated); }
.avatar-lg.avatar-fallback { display: flex; align-items: center; justify-content: center; font-size: 36px; font-weight: 700; color: var(--text-dim); }
.profile-header h1 { margin: 12px 0 0; font-size: 20px; }
.profile-header .handle { color: var(--text-dim); margin-bottom: 12px; }
.profile-header .summary { white-space: pre-wrap; }
.profile-header .summary p { margin: 0 0 8px; }
.profile-stats { display: flex; gap: 16px; margin-bottom: 12px; font-size: 14px; }
.profile-stat { color: var(--text); }
.profile-stat:hover { text-decoration: underline; }
.profile-stat-hidden { color: var(--text); cursor: default; }
.profile-stat strong, .profile-stat-hidden strong { font-weight: 700; }
.timeline { display: flex; flex-direction: column; }
.post { position: relative; padding: 12px 16px; border-bottom: 1px solid var(--border); }
.post-focused { background: var(--bg-elevated); }
.post-permalink { position: absolute; top: 12px; right: 16px; color: var(--text-dim); font-size: 15px; }
.post-header { display: flex; align-items: flex-start; gap: 10px; padding-right: 24px; }
.avatar-link { display: block; flex-shrink: 0; line-height: 0; }
.avatar { width: 44px; height: 44px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--bg-elevated); }
.avatar.avatar-fallback { display: flex; align-items: center; justify-content: center; font-weight: 700; color: var(--text-dim); border: 1px solid var(--border); }
.post-author { min-width: 0; flex: 1; }
.display-name { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.handle { color: var(--text-dim); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.display-name a, .handle a { color: inherit; text-decoration: none; }
.display-name a:hover, .handle a:hover { text-decoration: underline; }
.post-time { display: block; color: var(--text-dim); font-size: 13px; margin-top: 2px; flex-shrink: 0; }
.post-content { margin-top: 8px; word-wrap: break-word; }
.post-content p { margin: 0 0 8px; }
.post-content p:last-child { margin-bottom: 0; }
.post-content blockquote { margin: 8px 0; padding-left: 10px; border-left: 3px solid var(--border); color: var(--text-dim); }
.post-content a { word-break: break-word; }
.post-media { margin-top: 10px; }
.post-media img, .post-media video { max-width: 100%; border-radius: 12px; display: block; }
.post-media audio { width: 100%; }
.post-attachment-link { margin-top: 10px; }
.post-reactions { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
.reaction-pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 999px; background: var(--bg-elevated); border: 1px solid var(--border); font-size: 13px; color: var(--text-dim); }
.reaction-pill:hover { text-decoration: none; border-color: var(--accent); color: var(--text); }
.reaction-more { color: var(--text-dim); }
.reaction-icon { font-size: 14px; line-height: 1; display: inline-flex; }
img.reaction-icon { width: 16px; height: 16px; object-fit: contain; }
.empty-state { padding: 32px 16px; text-align: center; color: var(--text-dim); }
.page-footer { padding: 16px; text-align: center; color: var(--text-dim); font-size: 13px; }
.page-footer a { color: inherit; text-decoration: none; }
.page-footer a:hover { text-decoration: underline; }
.pagination { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 16px; }
.pagination-link { padding: 8px 16px; border-radius: 999px; border: 1px solid var(--border); background: var(--bg-elevated); font-size: 14px; font-weight: 600; }
.pagination-link:hover { text-decoration: none; background: var(--border); }
.pagination-spacer { flex: 1; }
.pagination-status { color: var(--text-dim); font-size: 13px; }

/* Popups (followers/following, post reactions) -- pure CSS, no JS: each
   dialog is a normally-hidden full-page overlay shown via the :target
   pseudo-class when its own id matches the URL fragment (an ordinary <a
   href="#id">), and closed by linking to _MODAL_CLOSE_HREF instead (a
   fragment matching no element -- unlike a bare "#", which the HTML spec
   scrolls to the top of the page for, this un-targets the overlay without
   moving the scroll position at all). See _modal_dialog_html/
   _reactions_dialog_html. .modal-backdrop fills the overlay behind the
   dialog so clicking the dimmed space around it also closes it --
   .modal-dialog is stacked above (higher z-index) and covers its own box,
   so clicks actually inside the dialog (including its own links) never
   fall through to it. */
.modal-overlay { display: none; position: fixed; inset: 0; z-index: 1000; padding: 16px; align-items: center; justify-content: center; background: rgba(0,0,0,0.6); }
.modal-overlay:target { display: flex; }
.modal-backdrop { position: absolute; inset: 0; z-index: 1; }
.modal-dialog { position: relative; z-index: 2; width: 100%; max-width: 420px; max-height: 80vh; overflow-y: auto; padding: 48px 18px 18px; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 16px; }
.modal-close { position: absolute; top: 14px; left: 16px; font-size: 14px; font-weight: 700; color: var(--text); }
.modal-title { margin: 0 0 12px; font-size: 17px; }
.person-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.person-row { display: flex; align-items: center; gap: 10px; }
.person-ident { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.person-name { min-width: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.person-name a { color: inherit; }
.person-handle { font-size: 12px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.avatar-sm { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--bg); }
.avatar-sm.avatar-fallback { display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; color: var(--text-dim); border: 1px solid var(--border); }
.person-row-emoji { font-size: 18px; flex-shrink: 0; }
img.person-row-emoji { width: 22px; height: 22px; object-fit: contain; }

/* Radio-button CSS tabs (Likes/Reactions) -- see _reactions_dialog_html. */
.tab-radio { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
.tab-label { display: inline-block; padding: 5px 14px; margin: 0 6px 14px 0; border-radius: 999px; font-size: 14px; font-weight: 600; color: var(--text-dim); cursor: pointer; }
.tab-radio:checked + .tab-label { background: var(--bg); color: var(--text); }
.tab-panel { display: none; }
.tab-radio-likes:checked ~ .likes-panel { display: block; }
.tab-radio-reactions:checked ~ .reactions-panel { display: block; }
"""


def _page_shell(*, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="page">
{body}
</div>
</body>
</html>
"""


def _follow_stat_html(*, label: str, count: int, dialog_id: str | None) -> str:
    """One "N Followers"/"N Following" stat -- a link opening that list's
    popup when ``dialog_id`` is given (the owner hasn't hidden it, see
    ``ActorRecord.hide_followers``/``hide_following``), otherwise plain,
    unlinked text: the count itself is still always shown (same as a
    profile's public ActivityPub collection -- see
    ``bridge.activitypub.routes.get_followers``/``get_following``, which
    reports the real ``totalItems`` regardless of whether the member list
    itself is withheld), only the popup listing individual accounts is
    gated on it."""
    inner = f'<strong>{count}</strong> {html.escape(label)}'
    if dialog_id is None:
        return f'<span class="profile-stat profile-stat-hidden">{inner}</span>'
    return f'<a class="profile-stat" href="#{dialog_id}">{inner}</a>'


def render_profile_page(
    *, display_name: str, handle: str, summary_html: str, avatar_url: str | None, banner_url: str | None,
    posts: list[PostView], older_posts_url: str | None = None,
    followers_count: int = 0, followers_hidden: bool = True, followers: list[PersonRef] | None = None,
    following_count: int = 0, following_hidden: bool = True, following: list[PersonRef] | None = None,
) -> str:
    banner_style = f' style="background-image:url(\'{html.escape(banner_url, quote=True)}\')"' if banner_url else ""
    followers_stat = _follow_stat_html(
        label="Followers", count=followers_count, dialog_id=None if followers_hidden else "followers-modal"
    )
    following_stat = _follow_stat_html(
        label="Following", count=following_count, dialog_id=None if following_hidden else "following-modal"
    )
    header = f"""
<div class="banner"{banner_style}></div>
<div class="profile-header">
  {_avatar_html(display_name, avatar_url, css_class="avatar-lg")}
  <h1>{html.escape(display_name)}</h1>
  <div class="handle">{html.escape(handle)}</div>
  <div class="profile-stats">{followers_stat}{following_stat}</div>
  <div class="summary">{summary_html}</div>
</div>
""".strip()

    dialogs = ""
    if not followers_hidden:
        dialogs += _person_list_dialog_html(
            "followers-modal", title="Followers", people=followers or [], empty_message="No followers yet."
        )
    if not following_hidden:
        dialogs += _person_list_dialog_html(
            "following-modal", title="Following", people=following or [], empty_message="Not following anyone yet."
        )

    if posts:
        timeline = '<div class="timeline">' + "".join(
            _post_card_html(p, i) for i, p in enumerate(posts)
        ) + "</div>"
    else:
        timeline = '<div class="empty-state">No posts yet.</div>'

    pagination = ""
    if older_posts_url:
        url = html.escape(older_posts_url, quote=True)
        pagination = f'<div class="pagination"><span class="pagination-spacer"></span><a class="pagination-link" href="{url}">Older posts &rarr;</a></div>'

    body = (
        f"{header}\n{dialogs}\n{timeline}\n{pagination}"
        + '<div class="page-footer"><a href="https://github.com/Haven-Organization/matrix-appservice-activitypub">matrix-appservice-activitypub</a></div>'
    )
    return _page_shell(title=f"{display_name} ({handle})", body=body)


def render_thread_page(
    *,
    posts: list[PostView],
    focused_ap_object_id: str,
    page: int = 1,
    total_pages: int = 1,
    prev_url: str | None = None,
    next_url: str | None = None,
) -> str:
    if not posts:
        body = '<div class="empty-state">Post not found.</div>'
        return _page_shell(title="Post", body=body)

    cards = "".join(
        _post_card_html(p, i, highlighted=(p.ap_object_id == focused_ap_object_id)) for i, p in enumerate(posts)
    )
    focused = next((p for p in posts if p.ap_object_id == focused_ap_object_id), posts[0])
    title = f"{focused.author_display_name} ({focused.author_handle})"

    pagination = ""
    if total_pages > 1:
        prev_link = (
            f'<a class="pagination-link" href="{html.escape(prev_url, quote=True)}">&larr; Newer replies</a>'
            if prev_url else ""
        )
        next_link = (
            f'<a class="pagination-link" href="{html.escape(next_url, quote=True)}">Older replies &rarr;</a>'
            if next_url else ""
        )
        pagination = (
            '<div class="pagination">'
            f"{prev_link}"
            f'<span class="pagination-status">Page {page} of {total_pages}</span>'
            f"{next_link}"
            "</div>"
        )

    body = f'<div class="timeline">{cards}</div>\n{pagination}\n<div class="page-footer"><a href="https://github.com/Haven-Organization/matrix-appservice-activitypub">matrix-appservice-activitypub</a></div>'
    return _page_shell(title=title, body=body)
