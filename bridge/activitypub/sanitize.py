"""HTML sanitization between ActivityPub Note ``content`` and Matrix message bodies.

Both directions need to defend against malicious markup: inbound Note HTML
comes from arbitrary, untrusted fediverse instances and is relayed into
Matrix rooms (a stored-XSS vector if forwarded unsanitized to a vulnerable
client); outbound Matrix message bodies are plain text that must be escaped
before being embedded as AP Note HTML.

Inbound content also routinely contains @mention and #hashtag references,
which Mastodon/Pleroma/etc. render as ordinary ``<a href="...">`` links to
the mentioned account's or tag's page. Left as real hyperlinks, most Matrix
clients (Element included) generate a URL preview card for every single one
-- noisy, and it makes an otherwise-short post take up a lot of timeline
space for something that's really just an @-reference, not a link the
poster actually chose to share. There's no universal per-link "don't
preview this" signal in Matrix, so instead: any ``<a>`` whose visible text
starts with ``@`` or ``#`` is treated as a mention/hashtag and rendered as
plain (non-linked) text here, keeping only the visible reference. This is a
text heuristic rather than keying off Mastodon's `class="mention"`
convention because not every implementation includes it (Pleroma, observed
live, often doesn't) -- but every implementation's mention/hashtag text
itself starts with the `@`/`#` sigil, since that's the whole point of it
being readable as a mention at all.

A @mention specifically (never a #hashtag, which has no Matrix equivalent
to become) gets upgraded one step further, from this plain text into a
real Matrix user pill for the mentioned account, using the Note's own
``tag`` array (a structured, reliable list of exactly who's mentioned) via
``bridge.note_mirroring.resolve_mention_pills`` -- so it still avoids the
link-preview problem this module exists to solve, while being an actual,
clickable reference to the right ghost instead of inert text.

Matching a ``tag`` entry to its anchor in ``content`` is its own small
problem: it can't be done by comparing the anchor's ``href`` to the tag's
``href`` (see ``_mention_pill_key``'s docstring for why, observed live on
real posts -- they're routinely two different, both individually valid
URLs for the very same account, and neither reliably resolves to the
other, e.g. if the instance is down). Instead both sides are reduced to a
``username@hostname`` key: the tag's own ``name`` for one side, and the
anchor's own ``href`` hostname plus its visible ``@username`` text for the
other -- two different pieces of information that are nonetheless
guaranteed to describe the same account consistently, which is what
actually makes the match reliable.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

_ALLOWED_TAGS = {
    "p", "br", "a", "strong", "b", "em", "i", "ul", "ol", "li", "blockquote", "span", "del", "code", "pre",
}
_ALLOWED_ATTRS = {"a": {"href"}}
_ALLOWED_URL_SCHEMES = ("http://", "https://", "mailto:")


def mention_pill_key(*, hostname: str | None, username: str | None) -> str | None:
    """The lookup key both sides of a mention match are reduced to:
    ``"username@hostname"``, lowercased. None if either half is missing/blank.

    Deliberately not the anchor's ``href`` compared to the tag's ``href``:
    on real posts (observed live) they're routinely two different, both
    individually valid URLs for the very same account -- e.g. Mastodon's
    human-facing ``https://instance/@user`` permalink (used in the mention
    anchor's ``href``) versus its actual actor id ``https://instance/users/user``
    (used in the Note's own ``tag`` array) -- and neither reliably resolves
    to the other (a redirect isn't guaranteed, and the instance might just
    be down when we'd need to check). The hostname plus the visible
    username, however, has to agree between the two on any real, honest
    mention -- nothing about which URL FORM an implementation chooses to
    use in either place changes either of those.
    """
    if not hostname or not username:
        return None
    return f"{username.strip().lower()}@{hostname.strip().lower()}"


class _SanitizingParser(HTMLParser):
    """Parses untrusted HTML, emitting only allow-listed tags/attributes plus plain text.

    ``<a>`` tags are buffered until their matching ``</a>`` (HTML5 doesn't
    allow nested anchors, so a single-level buffer is enough) so the
    decision to keep or drop the link can look at the anchor's own fully
    rendered text -- see the module docstring for why.

    ``mention_pills`` maps a ``mention_pill_key(...)`` result to a Matrix
    mxid -- when a mention anchor's own hostname+username reduce to a key
    in it, the link is kept (rewritten to a matrix.to pill for that mxid)
    instead of being dropped to plain text.
    """

    def __init__(self, mention_pills: dict[str, str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.html_parts: list[str] = []
        self.text_parts: list[str] = []
        self._mention_pills = mention_pills or {}
        self._anchor_href: str | None = None
        self._anchor_html_buffer: list[str] = []
        self._anchor_text_buffer: list[str] = []

    def _emit_html(self, s: str) -> None:
        (self._anchor_html_buffer if self._anchor_href is not None else self.html_parts).append(s)

    def _emit_text(self, s: str) -> None:
        (self._anchor_text_buffer if self._anchor_href is not None else self.text_parts).append(s)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            if self._anchor_href is not None:
                return  # a nested <a> isn't valid HTML; ignore rather than corrupt the buffer
            allowed_attrs = _ALLOWED_ATTRS.get(tag, set())
            kept = {
                name: value
                for name, value in attrs
                if name in allowed_attrs and value and value.startswith(_ALLOWED_URL_SCHEMES)
            }
            self._anchor_href = kept.get("href") or ""
            self._anchor_html_buffer = []
            self._anchor_text_buffer = []
            return
        allowed_attrs = _ALLOWED_ATTRS.get(tag, set())
        kept = [
            (name, value)
            for name, value in attrs
            if name in allowed_attrs and value and value.startswith(_ALLOWED_URL_SCHEMES)
        ]
        attr_str = "".join(f' {name}="{html.escape(value or "", quote=True)}"' for name, value in kept)
        self._emit_html(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            if self._anchor_href is None:
                return  # a stray </a> with no matching open
            inner_html = "".join(self._anchor_html_buffer)
            inner_text = "".join(self._anchor_text_buffer)
            href = self._anchor_href
            self._anchor_href = None
            stripped_text = inner_text.strip()

            pill_mxid = None
            if href and stripped_text.startswith("@"):
                # The visible text is routinely just "@user" (no "@domain"),
                # so only the part up to any second "@" is the username --
                # see mention_pill_key for why hostname comes from href instead.
                username = stripped_text[1:].split("@", 1)[0]
                key = mention_pill_key(hostname=urlsplit(href).hostname, username=username)
                if key:
                    pill_mxid = self._mention_pills.get(key)

            if pill_mxid:
                pill_href = html.escape(f"https://matrix.to/#/{pill_mxid}", quote=True)
                self.html_parts.append(f'<a href="{pill_href}">{inner_html}</a>')
            elif href and not stripped_text.startswith(("@", "#")):
                self.html_parts.append(f'<a href="{html.escape(href, quote=True)}">{inner_html}</a>')
            else:
                self.html_parts.append(inner_html)
            self.text_parts.append(inner_text)
            return
        self._emit_html(f"</{tag}>")
        if tag in ("p", "li", "blockquote"):
            self._emit_text("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._emit_html("<br>")
            self._emit_text("\n")

    def handle_data(self, data: str) -> None:
        self._emit_html(html.escape(data))
        self._emit_text(data)

    def flush_unclosed_anchor(self) -> None:
        """If the input ended with an ``<a>`` never actually closed
        (malformed HTML), its buffered content would otherwise just be
        silently dropped -- HTMLParser doesn't synthesize a matching
        ``handle_endtag`` for unclosed elements at EOF the way a lenient
        browser parser would. Call after ``.close()`` to recover it."""
        if self._anchor_href is None:
            return
        self.html_parts.append("".join(self._anchor_html_buffer))
        self.text_parts.append("".join(self._anchor_text_buffer))
        self._anchor_href = None


def strip_to_matrix_message(content_html: str, *, mention_pills: dict[str, str] | None = None) -> tuple[str, str]:
    """Sanitize an incoming AP Note's ``content`` HTML for use as a Matrix message.

    Returns ``(plain_text, safe_html)``. ``safe_html`` only ever contains tags
    from a small allow-list with ``href`` restricted to http(s)/mailto schemes --
    no scripts, styles, event handlers, or ``javascript:`` links survive.

    ``mention_pills``, if given, maps an actor IRI to a Matrix mxid -- see
    ``_SanitizingParser`` for why matching happens on ``href`` rather than
    the mention's own visible text.
    """
    parser = _SanitizingParser(mention_pills)
    parser.feed(content_html)
    parser.close()
    parser.flush_unclosed_anchor()
    plain = "".join(parser.text_parts).strip()
    safe_html = "".join(parser.html_parts).strip()
    return plain, safe_html


_BARE_URL_RE = re.compile(r'https?://[^\s<>"]+')
# Trimmed off the end of a matched URL if there's no earlier, unmatched
# opening character to justify it -- otherwise a URL immediately followed by
# ordinary prose punctuation (a period ending the sentence, a comma, a
# closing quote someone typed around it, ...) would swallow that
# punctuation into the link itself.
_URL_TRAILING_PUNCTUATION = ".,;:!?\"'"


def _linkify_bare_urls(escaped: str) -> str:
    """Wraps every bare ``http(s)://`` URL in ``escaped`` (already
    ``html.escape``d plain text -- see ``plain_text_to_note_html``) in a real
    ``<a href>``.

    Real fediverse clients write this anchor into a post's HTML themselves
    at compose time; a plain Matrix message body has no such markup at all
    (Matrix's own auto-linkification, when a client applies it, only ever
    shows up client-side and isn't part of ``body``), so without this a
    shared link posted from Matrix federates as inert text -- confirmed live
    (2026-07-03): the exact same URL posted natively from poa.st rendered as
    a clickable link there, but arrived unclickable when posted through this
    bridge instead.

    Matched against the ALREADY-escaped text rather than the raw body -- a
    URL's own syntax never includes literal ``<``/``>``/``"``/whitespace (a
    real one already has those percent-encoded), so this is unambiguous even
    though e.g. a literal ``&`` in a query string has by this point become
    ``&amp;``; that's the correct, HTML-safe spelling to put inside the
    ``href`` attribute too, not something that needs undoing here.
    """

    def _replace(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in _URL_TRAILING_PUNCTUATION:
            trailing = url[-1] + trailing
            url = url[:-1]
        # A trailing ")" is different -- kept as part of the URL if it
        # balances an earlier "(" (e.g. a Wikipedia article URL), otherwise
        # treated the same as ordinary trailing punctuation and stripped.
        while url.endswith(")") and url.count("(") < url.count(")"):
            trailing = ")" + trailing
            url = url[:-1]
        if not url:
            return match.group(0)
        return f'<a href="{url}" rel="nofollow noopener noreferrer" target="_blank">{url}</a>{trailing}'

    return _BARE_URL_RE.sub(_replace, escaped)


def plain_text_to_note_html(body: str, mention_links: dict[str, str] | None = None) -> str:
    """Escape a plaintext Matrix message body for use as an AP Note's ``content``.

    ``mention_links``, if given, maps a mention's own visible handle (e.g.
    ``"@user@instance.org"``, matching a resolved Mention tag's ``name`` --
    see ``bridge.mentions``) to the actor IRI it should link to. Without
    this, a mention still resolves correctly server-side (the Note's own
    ``tag`` array names/notifies the right account regardless), but reads as
    inert text everywhere a mainstream fediverse client (Mastodon included)
    actually renders a post -- from its ``content`` HTML as-is, not by
    cross-referencing ``tag`` after the fact. Real fediverse clients write
    the anchor into ``content`` themselves at compose time; since ours
    builds it from a plain Matrix message body instead, it has to add that
    anchor itself, here, or the mention never becomes a visible link on the
    other end even though it did register as one.
    """
    escaped = html.escape(body)
    escaped = _linkify_bare_urls(escaped)
    for handle, href in (mention_links or {}).items():
        escaped_handle = html.escape(handle)
        if escaped_handle not in escaped:
            continue
        anchor = (
            f'<a href="{html.escape(href, quote=True)}" class="mention" '
            f'rel="nofollow noopener noreferrer" target="_blank">{escaped_handle}</a>'
        )
        escaped = escaped.replace(escaped_handle, anchor)
    paragraphs = [f"<p>{line}</p>" for line in escaped.splitlines() if line.strip()]
    return "".join(paragraphs) or f"<p>{escaped}</p>"


_REPLY_FALLBACK_RE = re.compile(r"^(?:>.*\n)+\n?")


def strip_reply_fallback(body: str) -> str:
    """Remove the leading rich-reply quote block Matrix clients prepend to reply bodies."""
    return _REPLY_FALLBACK_RE.sub("", body, count=1).strip()
