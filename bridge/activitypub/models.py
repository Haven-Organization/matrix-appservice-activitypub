"""ActivityPub/JSON-LD data models.

Minimal dataclasses for the subset of ActivityStreams 2.0 this bridge speaks:
``Actor`` (Person), ``Note`` (an ordinary federated post), ``ChatMessage``
(Pleroma/Akkoma's "Chats" -- a distinct instant-messaging object type from a
Note-based direct message), ``Question`` (a poll), the generic ``Activity``
envelope (Create/Follow/Accept/Like/Announce/Undo/Delete), and
``OrderedCollection`` for outbox/followers/following.

These are intentionally not a full ActivityStreams implementation -- only the
fields Mastodon/Pleroma actually require to interoperate are modeled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"

# The trailing inline object defines the extension terms this bridge uses
# beyond bare ActivityStreams -- ``capabilities`` (on an Actor, see
# ``Actor.accepts_chat_messages``), ``ChatMessage`` (the object type), and
# ``EmojiReact`` (the activity type, already used by
# ``bridge.reaction_bridge`` for a custom-emoji reaction) are Pleroma/Akkoma
# ("litepub") terms; ``quoteUri``/``quoteUrl``/``_misskey_quote`` (see
# ``Note.quote_uri``) are the handful of informally-standardized field names
# various implementations (Akkoma/Pleroma, Fedibird-patched Mastodon,
# Misskey/Iceshrimp) each independently check for a "quote post" -- sent all
# three, all pointing at the same object, since there's no one ratified
# standard yet and this is cheap insurance against picking the one a given
# receiver doesn't happen to look for. Without a recognized definition for a
# term, JSON-LD's own expansion rules treat it as unmapped -- confirmed
# against a real Pleroma-family instance's own actor documents (which all
# carry this exact same litepub mapping, just to their own copy of the
# schema) that a strict receiver genuinely does drop an undefined
# property/type rather than just ignoring the ambiguity, which is why
# "capabilities" alone wasn't enough to make a Chat button show up on our
# own actor's profile there without this too -- the quote terms get the
# same treatment for the same reason. Defined inline here (rather than
# referencing any one instance's own hosted copy of a schema, e.g.
# https://<instance>/schemas/litepub-0.1.jsonld) so this has no runtime
# dependency on a third party's server -- the mapped IRIs are identical
# either way, since that's the actual vocabulary being referenced, not the
# document defining it.
JSON_LD_CONTEXT: list[str | dict[str, str]] = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {
        "litepub": "http://litepub.social/ns#",
        "capabilities": "litepub:capabilities",
        "ChatMessage": "litepub:ChatMessage",
        "EmojiReact": "litepub:EmojiReact",
        "misskey": "https://misskey-hub.net/ns#",
        "quoteUri": "as:quoteUrl",
        "quoteUrl": "as:quoteUrl",
        "_misskey_quote": "misskey:_misskey_quote",
    },
]

ACTIVITY_JSON_CONTENT_TYPE = 'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'


def _without_none(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None or an empty list, keeping JSON-LD output tidy."""
    return {k: v for k, v in d.items() if v is not None and v != []}


@dataclass(frozen=True)
class PublicKey:
    """The ``publicKey`` block embedded in an Actor object (security-v1 vocabulary)."""

    id: str
    owner: str
    public_key_pem: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "publicKeyPem": self.public_key_pem,
        }


@dataclass(frozen=True)
class Actor:
    """An ActivityPub Actor (Person for human-linked profiles, Service for the bridge bot)."""

    id: str
    preferred_username: str
    inbox: str
    outbox: str
    followers: str
    following: str
    public_key: PublicKey
    type: str = "Person"
    name: str | None = None
    summary: str | None = None
    url: str | None = None
    icon_url: str | None = None
    image_url: str | None = None
    shared_inbox: str | None = None
    # Pleroma/Akkoma extension advertising that this actor accepts
    # ChatMessage (see ChatMessage below) -- their own UI (and any other
    # implementation's that checks for this) uses it to decide whether to
    # offer a "Chat" option on the profile, distinct from the ordinary
    # "Direct Message" one every actor already implicitly supports via a
    # restricted-audience Note. Not part of any W3C-standardized vocabulary
    # (there is no spec for Chats at all, only Pleroma's own convention);
    # implemented here as best understood from that convention, and worth
    # double-checking against a real Pleroma/Akkoma instance if the "Chat"
    # option doesn't show up as expected.
    accepts_chat_messages: bool = False

    def to_dict(self) -> dict[str, Any]:
        icon = (
            {"type": "Image", "url": self.icon_url}
            if self.icon_url
            else None
        )
        # "image" is the AS2/Mastodon convention for a profile's header/banner
        # picture -- distinct from "icon" (the avatar). Absent entirely
        # (rather than present-but-null) when there isn't one, same as icon.
        image = (
            {"type": "Image", "url": self.image_url}
            if self.image_url
            else None
        )
        endpoints = {"sharedInbox": self.shared_inbox} if self.shared_inbox else None
        capabilities = {"acceptsChatMessages": True} if self.accepts_chat_messages else None
        return _without_none(
            {
                "@context": JSON_LD_CONTEXT,
                "id": self.id,
                "type": self.type,
                "preferredUsername": self.preferred_username,
                "name": self.name or self.preferred_username,
                "summary": self.summary or "",
                "url": self.url or self.id,
                "inbox": self.inbox,
                "outbox": self.outbox,
                "followers": self.followers,
                "following": self.following,
                "publicKey": self.public_key.to_dict(),
                "icon": icon,
                "image": image,
                "endpoints": endpoints,
                "capabilities": capabilities,
            }
        )


@dataclass(frozen=True)
class Note:
    """An ActivityStreams ``Note`` -- the object type used for federated posts."""

    id: str
    attributed_to: str
    content: str
    published: str
    to: list[str] = field(default_factory=lambda: [AS_PUBLIC])
    cc: list[str] = field(default_factory=list)
    in_reply_to: str | None = None
    attachment: list[dict[str, Any]] = field(default_factory=list)
    tag: list[dict[str, Any]] = field(default_factory=list)
    type: str = "Note"
    # Set when this Note is the object of an ``Update`` (an edit -- see
    # ``bridge.edit_bridge``): the edit's own timestamp, distinct from the
    # original ``published``. Mastodon/Pleroma use its presence/recency to
    # treat the Update as a real revision rather than a no-op re-delivery.
    updated: str | None = None
    # Set for ``;repost``'s own Note (see ``bridge.commands._handle_repost``)
    # -- the post being quoted, not merely linked/replied to. Sent under all
    # three field names real implementations check (see JSON_LD_CONTEXT's
    # own comment on why), rather than one specific one.
    quote_uri: str | None = None
    # Set only for a poll vote (see ``bridge.poll_bridge.maybe_federate_poll_vote``):
    # a poll vote's *entire* payload is ``name`` (the chosen option's exact
    # text) + ``inReplyTo`` (the Question's id), privately addressed to just
    # the poll's author -- no ``content``. Sent with ``type="Answer"``, NOT
    # the default "Note" -- Pleroma/Akkoma only count a vote at all if the
    # object's own type is literally "Answer" (confirmed in their
    # side_effects.ex); Mastodon doesn't gate on type here, so "Answer" is
    # correct for both. Absent for every ordinary post.
    name: str | None = None
    # FEP-7888 containment: set on an outbound Shoot Channel message (see
    # bridge.channel_bridge.maybe_distribute_channel_message) to the
    # Channel actor it belongs to -- matches the shape Shoot's own channel
    # messages use (confirmed live 2026-07-14: attributedTo names the
    # AUTHOR, context/to name the CHANNEL). Absent for every ordinary
    # top-level post, which has no such containment to express.
    context: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none(
            {
                "id": self.id,
                "type": self.type,
                "attributedTo": self.attributed_to,
                "content": self.content,
                "published": self.published,
                "updated": self.updated,
                "to": self.to,
                "cc": self.cc,
                "quoteUri": self.quote_uri,
                "quoteUrl": self.quote_uri,
                "_misskey_quote": self.quote_uri,
                "inReplyTo": self.in_reply_to,
                "tag": self.tag,
                "attachment": self.attachment,
                "name": self.name,
                "context": self.context,
            }
        )


@dataclass(frozen=True)
class ChatMessage:
    """A Pleroma/Akkoma ``ChatMessage`` -- the object type used for
    ActivityPub "Chats", a separate instant-messaging concept from a
    Mastodon-style Note-based direct message (see ``Actor.accepts_chat_messages``).

    Deliberately simpler than ``Note``: always exactly one recipient (never
    ``AS_PUBLIC``, never multiple ``cc`` targets -- a Chat is inherently a
    flat 1:1 conversation, not something with an audience to build a reply
    tree against), and no ``inReplyTo``/``tag`` -- Pleroma's own Chats don't
    thread or support mentions the way Notes do, every message is just its
    own fresh object addressed to the same other party. Both ``actor`` and
    ``attributedTo`` are set to the same value in ``to_dict`` since
    different implementations' Chat support has been observed checking
    either field; this is intentionally redundant rather than a considered
    choice of one over the other.
    """

    id: str
    attributed_to: str
    to: str
    content: str
    published: str
    attachment: list[dict[str, Any]] = field(default_factory=list)
    type: str = "ChatMessage"

    def to_dict(self) -> dict[str, Any]:
        return _without_none(
            {
                "id": self.id,
                "type": self.type,
                "actor": self.attributed_to,
                "attributedTo": self.attributed_to,
                "content": self.content,
                "published": self.published,
                "to": [self.to],
                "attachment": self.attachment,
            }
        )


@dataclass(frozen=True)
class Question:
    """An ActivityStreams ``Question`` -- the object type Mastodon/Pleroma
    use for a poll. ``one_of``/``any_of`` mirror AS2's own single-choice vs.
    multi-choice idiom (mutually exclusive; whichever the poll actually is
    gets populated, the other stays empty). Each option is
    ``{"type": "Note", "name": "<option text>"}``.

    Deliberately never carries a live ``replies.totalItems`` tally on an
    OUTBOUND option: per-option counts here would only ever reflect this
    bridge's own partial view (Matrix voters plus any votes personally
    received as the poll's author -- a structural ceiling of how
    Mastodon-style private voting works, not a bug -- see
    ``bridge.poll_bridge``'s module docstring), and publishing a
    provably-incomplete count is worse than omitting it entirely.
    """

    id: str
    attributed_to: str
    content: str
    published: str
    to: list[str] = field(default_factory=lambda: [AS_PUBLIC])
    cc: list[str] = field(default_factory=list)
    one_of: list[dict[str, Any]] = field(default_factory=list)
    any_of: list[dict[str, Any]] = field(default_factory=list)
    # Some Mastodon-family receivers refuse a Question with no expiry at
    # all -- Matrix's own poll model has no such concept, so this bridge
    # synthesizes one (see bridge.config.BridgeSection.poll_default_duration_days)
    # rather than omitting it outright.
    end_time: str | None = None
    # Set only on the ``Update`` sent when the poll is closed (see
    # bridge.poll_bridge.maybe_federate_poll_close) -- always absent on the
    # original Create.
    closed: str | None = None
    type: str = "Question"

    def to_dict(self) -> dict[str, Any]:
        return _without_none(
            {
                "id": self.id,
                "type": self.type,
                "attributedTo": self.attributed_to,
                "content": self.content,
                "published": self.published,
                "to": self.to,
                "cc": self.cc,
                "oneOf": self.one_of,
                "anyOf": self.any_of,
                "endTime": self.end_time,
                "closed": self.closed,
            }
        )


@dataclass(frozen=True)
class Activity:
    """A generic ActivityStreams Activity envelope.

    ``object`` may be a bare IRI (str), an embedded object (dict/Note), or
    another Activity (e.g. the inner activity of an ``Undo``). ``content`` is
    the reaction "key" for ``Like``/``EmojiReact`` activities (a
    Pleroma/Misskey/Akkoma extension) -- a literal unicode emoji or a
    custom-emoji shortcode; absent for a bare Mastodon-style ``Like`` (heart,
    no choice of emoji) and for every other activity type.
    """

    id: str
    type: str
    actor: str
    object: Union[str, dict[str, Any], "Activity", Note, ChatMessage, Question, None] = None
    published: str | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    content: str | None = None
    # Only populated by a Like/EmojiReact carrying a custom-emoji reaction --
    # a Pleroma/Misskey/Akkoma extension shaped like Note.tag's own Emoji
    # entries ({"type": "Emoji", "name": ":blobcat:", "icon": {"url": ...}}),
    # matched against `content` by bridge.inbox_dispatch to resolve the
    # reaction's actual image. Absent (empty) for every other activity type.
    tag: list[dict[str, Any]] = field(default_factory=list)
    # FEP-bebd's invite-gated Follow: the InviteCode object's own id, carried
    # on a Follow<Organization> to join a Shoot guild. Absent for every other
    # activity type/ordinary Follow.
    instrument: str | None = None

    def to_dict(self) -> dict[str, Any]:
        obj: Any
        if isinstance(self.object, (Activity, Note, ChatMessage, Question)):
            obj = self.object.to_dict()
        else:
            obj = self.object
        return _without_none(
            {
                "@context": JSON_LD_CONTEXT,
                "id": self.id,
                "type": self.type,
                "actor": self.actor,
                "object": obj,
                "published": self.published,
                "to": self.to,
                "cc": self.cc,
                "content": self.content,
                "instrument": self.instrument,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Activity":
        """Parse an incoming (untrusted) activity JSON payload.

        Only structural validation is done here -- semantic checks (is the
        actor known/followed, does the object exist, ...) happen in the
        inbox handler.
        """
        if "type" not in data:
            raise ValueError("Activity JSON is missing required field 'type'")
        if "actor" not in data:
            raise ValueError("Activity JSON is missing required field 'actor'")

        actor = data["actor"]
        if isinstance(actor, dict):
            actor = actor.get("id", "")

        return cls(
            id=data.get("id", ""),
            type=data["type"],
            actor=actor,
            object=data.get("object"),
            published=data.get("published"),
            to=list(data.get("to", []) or []),
            cc=list(data.get("cc", []) or []),
            content=data.get("content"),
            tag=list(data.get("tag", []) or []),
            instrument=data.get("instrument"),
        )

    def object_id(self) -> str | None:
        """Best-effort extraction of the IRI referenced by ``object``."""
        if isinstance(self.object, str):
            return self.object
        if isinstance(self.object, dict):
            return self.object.get("id")
        if isinstance(self.object, (Activity, Note, ChatMessage, Question)):
            return self.object.id
        return None


@dataclass(frozen=True)
class OrderedCollection:
    """An ``OrderedCollection`` small enough to always fit on a single page
    (followers/following/outbox) -- but still shaped with a real ``first``
    ``OrderedCollectionPage``, not a flat top-level ``orderedItems``.

    Confirmed live (2026-07-03) against a real Pleroma/Akkoma instance
    (poa.st) that this is required, not just spec pedantry: a remote
    viewer reads ``totalItems`` straight off the top-level object (so a
    profile's follower/following COUNT displayed fine), but looks for the
    actual member IRIs inside ``first.orderedItems`` specifically --
    finding nothing there (since this class used to put ``orderedItems``
    directly on the top-level collection, no ``first`` at all) and
    rendering an empty list despite the correct nonzero count. Every real
    collection this class produces is small enough that "page 1" already
    contains everything -- see ``bridge.activitypub.routes.get_followers``/
    ``get_following``, which also serve that identical page object directly
    at ``?page=1`` (poa.st's own ``first.id`` is a real, separately
    fetchable URL, not just an embedded object -- some remote
    implementations may re-fetch it rather than trust what's embedded)."""

    id: str
    items: list[Any] = field(default_factory=list)
    type: str = "OrderedCollection"
    # Overrides ``totalItems`` independent of ``len(items)`` -- lets
    # ``get_followers``/``get_following`` report the real follower/following
    # count while still passing an empty ``items`` when the owner has hidden
    # the member list (``ActorRecord.hide_followers``/``hide_following``),
    # matching Mastodon's own "hide network" behavior: the count stays
    # public, only the list itself is withheld. ``None`` (the default) just
    # falls back to ``len(items)``, as before.
    total_items: int | None = None

    def first_page_dict(self) -> dict[str, Any]:
        """The ``OrderedCollectionPage`` embedded as ``first`` below, and
        also served standalone at ``?page=1`` -- see this class's own
        docstring for why both need to exist."""
        return {
            "id": f"{self.id}?page=1",
            "type": "OrderedCollectionPage",
            "partOf": self.id,
            "orderedItems": self.items,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "@context": JSON_LD_CONTEXT,
            "id": self.id,
            "type": self.type,
            "totalItems": len(self.items) if self.total_items is None else self.total_items,
            "first": self.first_page_dict(),
        }
