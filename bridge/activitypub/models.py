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
        # Mastodon's own extension namespace, also declared by real PeerTube
        # actor documents (confirmed live 2026-07-25 against a real channel
        # actor) -- only actually used on a PeerTube channel's own Actor
        # (see Actor.discoverable below), harmless/unused everywhere else.
        "toot": "http://joinmastodon.org/ns#",
        "discoverable": "toot:discoverable",
        "indexable": "toot:indexable",
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
    # PeerTube's own remote-actor-image importer (getImagesInfoFromObject,
    # confirmed by reading their real source 2026-07-25) needs a mediaType
    # to know what to save an avatar/banner as, falling back to sniffing a
    # file extension off the URL only when it's absent -- and this bridge's
    # own media URLs are opaque Matrix media ids with no extension at all,
    # so omitting this silently drops the image entirely on their end, the
    # same failure mode as Video.uuid's own docstring describes for a video
    # missing its uuid. Every other AP implementation this bridge has been
    # tested against (Mastodon/Pleroma/Akkoma) tolerates an extensionless,
    # mediaType-less icon URL fine, which is why this went unnoticed until
    # specifically checked against PeerTube's own channel/profile pages.
    icon_media_type: str | None = None
    image_url: str | None = None
    image_media_type: str | None = None
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
    # PeerTube's own extension: a Group-typed channel actor's playlists
    # collection (see bridge.activitypub.routes.get_playlists, a
    # permanently-empty stub for v1 per the agreed design). Never set for
    # an ordinary Person profile, which has no such concept.
    playlists_url: str | None = None
    # A channel's owning account actor id(s) -- confirmed live 2026-07-25
    # against a real PeerTube channel actor, which always carries this
    # (pointing back at the Person that owns it). Never set for an
    # ordinary Person profile, which has no owner of its own to name.
    attributed_to_urls: list[str] | None = None
    # Mastodon's toot:discoverable/toot:indexable extension (see
    # JSON_LD_CONTEXT's own comment) -- also confirmed live on that same
    # real channel actor, both true. Plausibly load-bearing for whether a
    # remote PeerTube instance's own search/resolve treats an unfamiliar
    # Group actor as a real, discoverable channel at all, rather than
    # something to silently ignore; set for channels, left unset (and so
    # omitted entirely) for an ordinary Person profile.
    discoverable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        icon = (
            _without_none({"type": "Image", "url": self.icon_url, "mediaType": self.icon_media_type})
            if self.icon_url
            else None
        )
        # "image" is the AS2/Mastodon convention for a profile's header/banner
        # picture -- distinct from "icon" (the avatar). Absent entirely
        # (rather than present-but-null) when there isn't one, same as icon.
        image = (
            _without_none({"type": "Image", "url": self.image_url, "mediaType": self.image_media_type})
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
                "playlists": self.playlists_url,
                "attributedTo": self.attributed_to_urls,
                "discoverable": self.discoverable,
                "indexable": self.discoverable,
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
    # reaction's actual image (inbound), or built the same way by
    # bridge.reaction_bridge when mirroring an MSC4027 custom-image Matrix
    # reaction back out (outbound). Absent (empty) for every other activity
    # type.
    tag: list[dict[str, Any]] = field(default_factory=list)
    # FEP-bebd's invite-gated Follow: the InviteCode object's own id, carried
    # on a Follow<Organization> to join a Shoot guild. Absent for every other
    # activity type/ordinary Follow.
    instrument: str | None = None

    def to_dict(self) -> dict[str, Any]:
        obj: Any
        if isinstance(self.object, (Activity, Note, ChatMessage, Question, Video)):
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
                "tag": self.tag,
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
    # Whether ``first`` below is the full ``OrderedCollectionPage`` embedded
    # inline (the default, confirmed live against Pleroma/Akkoma, see this
    # class's own docstring) or a bare URL string a caller is expected to
    # fetch separately (at the same ``?page=1``, still served by
    # ``bridge.activitypub.routes._collection_or_page`` either way). Real
    # PeerTube needs the bare-URL form instead. Confirmed live 2026-07-25
    # against a real channel outbox (framatube.org): its own ``first`` is a
    # plain string, never an embedded object, and our channel outbox
    # embedding one instead meant a real PeerTube instance importing the
    # channel saw a ``first`` shape it didn't recognize as a fetchable page
    # at all, and so imported zero videos despite the channel itself
    # resolving correctly. Set False for the channel's own outbox/followers/
    # following (``bridge.commands``'s ``;create channel`` and friends);
    # left True (unchanged) for every ordinary Profile Room collection.
    embed_first: bool = True

    def first_page_dict(self) -> dict[str, Any]:
        """The ``OrderedCollectionPage`` embedded as ``first`` when
        ``embed_first`` is True, and also served standalone at ``?page=1``
        regardless. See this class's own docstring for why both need to
        exist."""
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
            "first": self.first_page_dict() if self.embed_first else f"{self.id}?page=1",
        }


# PeerTube's own extension namespace, verified live 2026-07-25 against a
# real video object (framatube.org). Deliberately trimmed to only the terms
# this bridge actually emits below; the real thing also declares live-
# broadcast, subtitle, storyboard, and multi-resolution/torrent terms this
# bridge has no use for, since it never transcodes and only ever serves one
# progressive-MP4 Link (see Video's own docstring).
VIDEO_JSON_LD_CONTEXT: list[str | dict[str, Any]] = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {
        "pt": "https://joinpeertube.org/ns#",
        "sc": "http://schema.org/",
        "Hashtag": "as:Hashtag",
        "category": "sc:category",
        "licence": "sc:license",
        "sensitive": "as:sensitive",
        "language": "sc:inLanguage",
        "identifier": "sc:identifier",
        "views": {"@type": "sc:Number", "@id": "pt:views"},
        "state": {"@type": "sc:Number", "@id": "pt:state"},
        "size": {"@type": "sc:Number", "@id": "pt:size"},
        "commentsPolicy": {"@type": "sc:Number", "@id": "pt:commentsPolicy"},
        "downloadEnabled": {"@type": "sc:Boolean", "@id": "pt:downloadEnabled"},
        "waitTranscoding": {"@type": "sc:Boolean", "@id": "pt:waitTranscoding"},
    },
]


@dataclass(frozen=True)
class VideoIdentifier:
    """PeerTube's ``ActivityIdentifierObject``: the shared shape ``Video``
    uses for its ``category``/``licence``/``language`` fields below (an
    ``identifier`` plus an optional human-readable ``name``)."""

    identifier: str
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _without_none({"identifier": self.identifier, "name": self.name})


@dataclass(frozen=True)
class Video:
    """A PeerTube-compatible ActivityStreams ``Video`` object (see
    ``bridge.peertube`` for the category/licence constants and resolver
    helpers that fill in ``category``/``licence``/``language`` below, and
    the ``project_peertube_channels_scoping`` memory for the full agreed
    design). This bridge never transcodes, so this deliberately omits
    everything about PeerTube's own multi-resolution HLS/BitTorrent/
    storyboard/subtitle machinery: a single progressive-MP4 ``Link`` in
    ``url`` was confirmed (via PeerTube's own docs during scoping) to be
    sufficient for remote playback on its own.

    ``state``/``waitTranscoding`` are hardcoded rather than exposed as
    fields: this bridge's videos are always immediately, fully available
    the moment they're published (nothing queued, nothing to wait for), so
    they're always "published" (``state=1``) and never
    ``waitTranscoding=True``, unlike a real PeerTube upload, which
    federates a placeholder Video immediately and flips both fields once
    its own transcoding pipeline finishes."""

    id: str
    name: str
    attributed_to: list[dict[str, str]]
    published: str
    url_html: str
    # PeerTube-specific extension, but a REQUIRED one on their receiving
    # end, not just informational: PeerTube's own AP video importer
    # (APVideoCreator/getVideoAttributesFromObject, confirmed by reading
    # their real source 2026-07-25) maps this straight into its Video
    # table's own NOT-NULL ``uuid`` column, and silently discards the
    # entire video (caught, logged, no video created, no error surfaced
    # anywhere) if it's missing -- exactly the failure mode confirmed live
    # across three separate real PeerTube instances: Follow/Accept and
    # delivery all succeeded, but a published video never once appeared
    # in any of their catalogs. Callers must pass a real (dashed, RFC
    # 4122-shaped) UUID string, not this bridge's own internal hex id.
    uuid: str
    media_url: str
    media_type: str
    duration_seconds: int | None = None
    media_size: int | None = None
    media_width: int | None = None
    media_height: int | None = None
    icon_url: str | None = None
    icon_width: int | None = None
    icon_height: int | None = None
    # Sent as raw markdown (mediaType text/markdown), matching PeerTube's own
    # video-description convention exactly (confirmed live 2026-07-25).
    # Unlike an ordinary mirrored post's Note.content, which this bridge
    # always sends as HTML, a Video is PeerTube-shaped vocabulary that only
    # PeerTube-compatible software consumes at all, so matching its own
    # literal expected format here (rather than the wider fediverse's HTML
    # convention) is what keeps a description from rendering as raw
    # asterisks on the receiving end.
    content: str | None = None
    category: VideoIdentifier | None = None
    licence: VideoIdentifier | None = None
    language: VideoIdentifier | None = None
    tags: list[str] = field(default_factory=list)
    sensitive: bool = False
    comments_enabled: bool = True
    views: int = 0
    # Confirmed live 2026-07-25 against a real video object (framatube.org):
    # unlike an ordinary Note (AS_PUBLIC alone in `to`, the author's own
    # followers in `cc`), a real PeerTube Video additionally names its OWNING
    # CHANNEL directly in `to` (not just via attributedTo) and in `audience`,
    # while `cc` carries only the posting ACCOUNT's followers, not the
    # channel's own. Construction sites should pass both `to` and
    # `audience` explicitly to match, not rely on the bare-AS_PUBLIC default
    # below (kept only as a fallback for a caller that genuinely has no
    # channel actor id on hand).
    to: list[str] = field(default_factory=lambda: [AS_PUBLIC])
    cc: list[str] = field(default_factory=list)
    audience: str | None = None
    updated: str | None = None

    def to_dict(self) -> dict[str, Any]:
        icon = (
            _without_none(
                {
                    "type": "Image",
                    "url": self.icon_url,
                    "mediaType": "image/jpeg",
                    "width": self.icon_width,
                    "height": self.icon_height,
                }
            )
            if self.icon_url
            else None
        )
        media_link = _without_none(
            {
                "type": "Link",
                "mediaType": self.media_type,
                "href": self.media_url,
                "height": self.media_height,
                "width": self.media_width,
                "size": self.media_size,
            }
        )
        return _without_none(
            {
                "id": self.id,
                "type": "Video",
                "uuid": self.uuid,
                "name": self.name,
                "duration": f"PT{self.duration_seconds}S" if self.duration_seconds is not None else None,
                "views": self.views,
                "sensitive": self.sensitive,
                "waitTranscoding": False,
                "state": 1,
                "commentsPolicy": 1 if self.comments_enabled else 2,
                "downloadEnabled": True,
                "published": self.published,
                "updated": self.updated,
                "mediaType": "text/markdown" if self.content else None,
                "content": self.content,
                "category": self.category.to_dict() if self.category else None,
                "licence": self.licence.to_dict() if self.licence else None,
                "language": self.language.to_dict() if self.language else None,
                "tag": [{"type": "Hashtag", "name": t} for t in self.tags],
                "icon": [icon] if icon else None,
                "url": [{"type": "Link", "mediaType": "text/html", "href": self.url_html}, media_link],
                "to": self.to,
                "cc": self.cc,
                "audience": self.audience,
                "attributedTo": self.attributed_to,
            }
        )
