"""Resolves Matrix mentions of fediverse-known accounts into proper
ActivityStreams ``Mention`` tags when federating an outbound post/reply.

Three distinct cases, handled by the two functions below:

``resolve_pill_mentions`` -- a Matrix client's mention of an account this
bridge already has a fediverse identity on file for shows up two ways in
the event: ``content["m.mentions"]["user_ids"]`` (a reliable list of
exactly who was mentioned) and, typically, that user's current display
name inserted as plain text in the message body (what Element and most
other clients do when you pick someone from the mention autocomplete) --
there's no structurally unambiguous marker for *where* in the plain body a
mention sits the way a formatted_body's pill anchors would give us, so
replacing the known display name is a best-effort substitution, not a
guaranteed one. Covers both of the accounts we can resolve this way: a
ghost (a REMOTE fediverse account already mirrored here), and a real LOCAL
Matrix user who's linked their own fediverse profile -- mentioning a
fellow bridge user is otherwise indistinguishable, on the fediverse side,
from a typo: without resolving it to their own actor IRI, all that goes
out is their bare Matrix display name as inert plain text.

``resolve_plaintext_mentions`` -- a ``@user@instance.org`` handle typed out
by hand rather than picked from autocomplete never touches ``m.mentions`` at
all (Matrix has no concept of it), so it isn't a pill mention in the above
sense and ``resolve_pill_mentions`` never sees it. This scans the body text
directly for that shape and resolves each via WebFinger instead -- which
also transparently covers a hand-typed ``@user@ourdomain`` naming a local
bridge user, since webfinger for our own users resolves exactly like any
other account's.

Without either, a mention would federate out as either the mentioned
account's Matrix display name, its raw MXID, or (for a hand-typed handle)
inert text with no ``Mention`` tag at all -- meaningless in all three
cases, and not a real mention to anyone reading it from the fediverse
side, since the account being talked about wouldn't be tagged there,
linked, or notified.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from fastapi import Request

from bridge.activitypub.remote_actor import RemoteActorFetchError, fetch_actor
from bridge.activitypub.urls import actor_url
from bridge.activitypub.webfinger import WebfingerError, WebfingerUnreachableError, resolve_remote_actor_id

logger = logging.getLogger(__name__)


def _extract_attributed_to(parent: dict) -> str | None:
    """Normalize a fetched object's ``attributedTo`` to a single actor IRI.

    Usually a bare string, but legally also an object with an ``id`` or a
    list of either (AS2 allows multi-attribution; the first entry is the
    conventional primary author).
    """
    attributed = parent.get("attributedTo")
    if isinstance(attributed, list):
        attributed = attributed[0] if attributed else None
    if isinstance(attributed, dict):
        attributed = attributed.get("id")
    return attributed if isinstance(attributed, str) and attributed else None


async def _author_handle(request: Request, author_id: str) -> str | None:
    """Best-effort ``@user@domain`` handle for an actor IRI -- the ghost
    profile we almost certainly already synced for them (that's whose
    mirrored post is being replied to), falling back to a live actor fetch.
    Returns None if neither works; the caller then skips the author's
    ``Mention`` (their delivery/threading via ``cc``/``inReplyTo`` never
    depended on it)."""
    profile = await request.app.state.repository.get_ghost_profile(author_id)
    if profile is not None and profile.handle:
        return profile.handle if profile.handle.startswith("@") else f"@{profile.handle}"
    try:
        actor_doc = await fetch_actor(request.app.state.http_client, author_id)
    except RemoteActorFetchError:
        return None
    username = actor_doc.get("preferredUsername") or author_id.rstrip("/").rsplit("/", 1)[-1]
    domain = urlparse(author_id).netloc
    if not username or not domain:
        return None
    return f"@{username}@{domain}"


async def collect_reply_participants(
    request: Request,
    parent_object_id: str,
    *,
    exclude_actor_ids: set[str],
    already_tagged: set[str],
) -> tuple[list[dict], list[str]]:
    """Build the ``Mention`` tags a reply owes the post it's replying to:
    the parent's author first, then everyone the parent itself mentions.

    Fediverse convention (Mastodon/Pleroma/Misskey alike) is that replying
    to a post keeps the whole conversation on the thread: the reply's own
    ``tag``/``cc`` carries a ``Mention`` for the parent's author AND for
    everyone in the parent's own ``Mention``\\ s. Receiving software derives
    its "reply to @a @b @c" participant line -- and who to notify -- from
    those tags, NOT from ``inReplyTo``/``cc`` alone: a reply whose tags
    name only the carried-over participants but not the author being
    directly replied to renders as a reply to everyone EXCEPT that person
    (confirmed live on Pleroma, 2026-07-03 -- once tags exist at all, the
    tag list wins over any inReplyTo-derived fallback rendering).

    Fetches the parent object live rather than persisting tags at mirror
    time: the parent may predate any such tracking, may be one of our own
    locally-published notes, or may have been edited since -- its current
    ``tag`` list is authoritative and one GET is cheap next to the delivery
    fan-out a reply already does. Returns ``(mention_tags, cc_actor_ids)``
    shaped exactly like ``resolve_pill_mentions``' return, meant to be
    merged the same way. Best-effort: any fetch/parse failure returns empty
    and the reply just goes out addressed as it always was.

    ``exclude_actor_ids`` -- actor IRIs to skip (the replying sender
    themselves, plus anyone their own message text already tags: re-tagging
    the sender would make them "reply to" themselves, and re-adding someone
    the sender deliberately tagged would duplicate the mention).
    ``already_tagged`` -- same skip-list keyed by lowercased
    ``user@domain`` handle instead, covering a hand-typed handle whose
    WebFinger resolution failed (present in the sender's tags with a
    GUESSED href -- see ``resolve_plaintext_mentions`` -- so href-based
    exclusion alone would miss it and tag them twice).
    """
    http_client = request.app.state.http_client
    try:
        response = await http_client.get(parent_object_id, headers={"Accept": "application/activity+json"})
        if response.status_code >= 400:
            logger.info("Fetching reply parent %s failed: HTTP %s", parent_object_id, response.status_code)
            return [], []
        parent = response.json()
    except (httpx.HTTPError, ValueError):
        logger.info("Could not fetch reply parent %s; sending reply without its participants", parent_object_id)
        return [], []

    if not isinstance(parent, dict):
        return [], []
    # Some software serves the wrapping Create activity at (or redirected
    # from) the object's own id -- the Note itself is then one level down.
    if parent.get("type") == "Create" and isinstance(parent.get("object"), dict):
        parent = parent["object"]

    tags: list[dict] = []
    cc: list[str] = []
    seen_hrefs: set[str] = set()

    # The author leads the mention list -- they're who this is a reply TO;
    # every mainstream implementation puts them first.
    author_id = _extract_attributed_to(parent)
    if author_id and author_id not in exclude_actor_ids:
        handle = await _author_handle(request, author_id)
        if handle is not None and handle.lstrip("@").lower() not in already_tagged:
            seen_hrefs.add(author_id)
            tags.append({"type": "Mention", "href": author_id, "name": handle})
            cc.append(author_id)

    raw_tags = parent.get("tag") or []
    if isinstance(raw_tags, dict):
        raw_tags = [raw_tags]
    for item in raw_tags:
        if not isinstance(item, dict) or item.get("type") != "Mention":
            continue  # Hashtag/Emoji tags aren't participants
        href = item.get("href")
        if not isinstance(href, str) or not href or href in exclude_actor_ids or href in seen_hrefs:
            continue
        name = item.get("name")
        if isinstance(name, str) and name.lstrip("@").lower() in already_tagged:
            continue
        seen_hrefs.add(href)
        tag: dict = {"type": "Mention", "href": href}
        if isinstance(name, str) and name:
            tag["name"] = name
        tags.append(tag)
        cc.append(href)
    return tags, cc


async def resolve_pill_mentions(request: Request, body: str, content: dict) -> tuple[str, list[dict], list[str]]:
    """Given a Matrix ``m.room.message`` event's already-extracted ``body``
    text (whatever the caller has already decided is the post's actual
    text) and its full ``content`` dict, replace any mentioned ghost's OR
    local bridge user's name/mxid in that text with their fediverse
    ``@user@instance.org`` handle.

    Returns ``(rewritten_body, ap_mention_tags, extra_cc_actor_ids)`` --
    ``ap_mention_tags`` are ActivityStreams ``Mention`` tag objects and
    ``extra_cc_actor_ids`` are the mentioned actors' IRIs, both meant to be
    merged into the outbound Note's own ``tag``/``cc`` so the mentioned
    account actually gets notified, not just referenced in the text.
    """
    mentioned_user_ids = (content.get("m.mentions") or {}).get("user_ids") or []
    if not mentioned_user_ids:
        return body, [], []

    config = request.app.state.config
    repository = request.app.state.repository
    ghost_prefix = f"@{config.appservice.user_prefix}"
    bot_mxid = f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"

    tags: list[dict] = []
    extra_cc: list[str] = []
    for mxid in mentioned_user_ids:
        if mxid == bot_mxid:
            continue  # tagging the bot is a command, not a mention to federate

        if mxid.startswith(ghost_prefix):
            profile = await repository.get_ghost_profile_by_mxid(mxid)
            if profile is None or not profile.handle:
                continue  # not a ghost we've ever actually synced a profile for
            display_name, actor_id, handle = profile.display_name, profile.actor_id, profile.handle
        else:
            local_actor = await repository.get_local_actor_by_matrix_id(mxid)
            if local_actor is None:
                continue  # a real Matrix user with no fediverse profile on this bridge -- nothing to tag
            display_name = local_actor.display_name or local_actor.username
            actor_id = actor_url(config.bridge.public_base_url, local_actor.username)
            handle = f"@{local_actor.username}@{config.bridge.domain}"

        if display_name and display_name in body:
            body = body.replace(display_name, handle)
        elif mxid in body:
            body = body.replace(mxid, handle)

        tags.append({"type": "Mention", "href": actor_id, "name": handle})
        extra_cc.append(actor_id)

    return body, tags, extra_cc


# A username segment can't start/end with one of its own separator
# characters, and the domain must have at least one "."-separated label
# after the first (i.e. an actual TLD) -- both narrow down what's matched
# to things that are plausibly real handles, e.g. excluding a sentence
# that merely trails off into "...@home." from being treated as one.
# Doesn't need to be a fully precise hostname/username grammar: anything
# that slips through anyway just fails the WebFinger lookup below and is
# left alone as plain text, same as a handle for an account that doesn't
# exist.
_PLAINTEXT_HANDLE_RE = re.compile(
    r"(?<![\w@])@(?P<user>[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)"
    r"@(?P<domain>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
)


async def resolve_plaintext_mentions(
    request: Request, body: str, *, already_tagged: set[str] | None = None
) -> tuple[list[dict], list[str]]:
    """Find ``@user@instance.org`` handles typed out as plain text -- rather
    than picked from a Matrix client's mention autocomplete, the only case
    ``resolve_pill_mentions`` (above) handles -- and resolve each via
    WebFinger into a proper ActivityStreams ``Mention`` tag, so the account
    named is actually linked/notified on the fediverse side instead of the
    handle just sitting there looking like a mention without being one.

    ``already_tagged`` -- the ``user@domain`` keys (lowercased) already
    covered by ``resolve_pill_mentions`` -- is skipped, to avoid a
    redundant WebFinger round-trip (and a duplicate ``Mention`` tag) for a
    handle already resolved that way.

    Best-effort per handle, same as ``resolve_pill_mentions`` -- but split
    into two different kinds of "couldn't resolve," handled two different
    ways (see ``bridge.activitypub.webfinger``'s ``WebfingerNotFoundError``
    vs ``WebfingerUnreachableError`` for the exact split):

    A domain that answers and confirms there's no such account (a 4xx
    response, or a JRD with no matching ``activity+json`` link) is a
    CONFIRMED negative -- guessing a link for it would point at something
    we've been explicitly told doesn't exist, so this leaves it as plain,
    unlinked text, same as ever.

    A domain that couldn't give a real answer at all (connection refused,
    timed out, DNS failure, a 5xx of its own -- never got anything to
    actually judge the account by) is different: we genuinely don't know
    whether the account exists, only that we can't check right now. Rather
    than leave a plausible-looking handle as dead text, this falls back to
    the conventional ``https://{domain}/@{username}`` profile-page URL --
    reliable across Mastodon/Pleroma/Akkoma/GoToSocial (and one most of
    them also content-negotiate on, redirecting a signed AP fetch to the
    real actor object, so it isn't purely cosmetic even though delivery
    can't be attempted against it directly). This is a genuine guess, not
    a confirmed resolution: it can be wrong for software that doesn't
    follow that convention (Misskey-family instances, most notably), and
    unlike a real resolution it's never added to delivery -- we have no
    confirmed inbox to send to, and a stale/wrong link is a much smaller
    problem than delivering to one.
    """
    already_tagged = already_tagged or set()
    http_client = request.app.state.http_client
    tags: list[dict] = []
    extra_cc: list[str] = []
    seen: set[str] = set()
    for match in _PLAINTEXT_HANDLE_RE.finditer(body):
        username, domain = match.group("user"), match.group("domain")
        key = f"{username.lower()}@{domain.lower()}"
        if key in already_tagged or key in seen:
            continue
        seen.add(key)
        try:
            actor_id = await resolve_remote_actor_id(http_client, key)
        except WebfingerUnreachableError:
            logger.info("Could not reach %r to resolve via WebFinger; linking a guessed profile URL", key)
            tags.append({"type": "Mention", "href": f"https://{domain}/@{username}", "name": f"@{key}"})
            continue
        except WebfingerError:
            logger.info("Could not resolve plaintext mention %r via WebFinger; leaving as text", key)
            continue
        tags.append({"type": "Mention", "href": actor_id, "name": f"@{key}"})
        extra_cc.append(actor_id)

    return tags, extra_cc
