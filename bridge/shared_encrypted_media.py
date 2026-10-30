"""Encrypted attachments shared with the sibling bridges on this server.

``bridge.media``'s encrypted-attachment confirmation asks the sender before
decrypting a forwarded file and uploading a plaintext copy. matrix-appservice-
nostr and matrix-appservice-atproto do the same; on one server, configured
with the same ``encrypted_media.bridges`` list, they share it so the sender
is asked once and the file is decrypted once:

- **Turns.** The bridges ask in list order. This one usually comes first;
  when it doesn't, it waits ``wait_seconds`` per place in the list, then reads
  the post's relations: a question (``PROMPT_FIELD`` on a thread reply) or an
  announced copy from a bridge before it, and it leaves it to that.
- **Confirming.** A "confirm" reply (``bridge.appservice_routes.
  maybe_handle_encrypted_attachment_confirmation``) or a 👍 in any skin tone
  on the question (``maybe_handle_shared_encrypted_media``), from the post's
  own sender. Only the bridge that asked acts on it.
- **The copy.** The bridge that asked decrypts and uploads once, then
  announces it: an ``ANNOUNCE_TYPE`` event related to the post (``rel_type``
  the same), with ``url``, ``encrypted_url`` and ``info``. Clients don't show
  it. A bridge that already had the copy (the same file forwarded again)
  announces it for the new post, unless someone already has.
- **Trust.** A bridge using another's copy re-encrypts it with the post's
  own key and IV (AES-CTR gives back the original ciphertext) and checks it
  against the post's ``hashes.sha256``, so no bridge takes another's word.

The encrypted -> plaintext mapping is the same cache ``bridge.media`` already
keeps (``record_custom_emoji_mxc``), so a copy announced by another bridge
is used here exactly like one this bridge made.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Request

from bridge.synapse_client import SynapseError

logger = logging.getLogger(__name__)

# On a bridge's question: {"prompt_for": "<the post's event id>"}.
PROMPT_FIELD = "software.haven.encrypted_media"
# A decrypted copy, announced by the bridge that made it.
ANNOUNCE_TYPE = "software.haven.decrypted_media"
# A thumbs up in any skin tone confirms, compared without the emoji
# variation selector (U+FE0F).
CONFIRM_REACTIONS = frozenset({"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"})

_RUNNING: set[asyncio.Task] = set()


def is_confirm_reaction(key: str) -> bool:
    return key.replace("️", "").strip() in CONFIRM_REACTIONS


def _bot(config) -> str:
    return f"@{config.appservice.bot_localpart}:{config.synapse.server_name}"


def _bridges(config) -> tuple[str, ...]:
    return config.encrypted_media.bridges


def _rank(config) -> int:
    bridges, bot = _bridges(config), _bot(config)
    return bridges.index(bot) if bot in bridges else 0


def _b64(value: str, *, urlsafe: bool) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded) if urlsafe else base64.b64decode(padded)


def _encrypt(encrypted_file: dict, plaintext: bytes) -> bytes:
    worker = Cipher(
        algorithms.AES(_b64(encrypted_file["key"]["k"], urlsafe=True)),
        modes.CTR(_b64(encrypted_file["iv"], urlsafe=False)),
    ).encryptor()
    return worker.update(plaintext) + worker.finalize()


def _in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def _related(request: Request, room_id: str, event_id: str, *, ours: bool = False) -> list[dict[str, Any]]:
    """The post's thread replies and announcements from the other listed
    bridges (and this one's own, with ``ours``)."""
    config = request.app.state.config
    senders = set(_bridges(config)) - (set() if ours else {_bot(config)})
    found: list[dict[str, Any]] = []
    for rel_type in (ANNOUNCE_TYPE, "m.thread"):
        try:
            found += await request.app.state.synapse.get_relations(
                room_id, event_id, rel_type=rel_type, as_user_id=_bot(config)
            )
        except SynapseError:
            continue
    return [e for e in found if e.get("sender") in senders]


# -- asking, in turn -------------------------------------------------------------------------


async def ask_in_turn(request: Request, *, room_id: str, sender: str, trigger_event_id: str) -> None:
    """In place of asking straight away: this bridge's turn, if it gets
    one. First in the list (or no list), it asks now."""
    from bridge.media import _send_encrypted_attachment_confirmation_request

    rank = _rank(request.app.state.config)
    if rank == 0:
        await _send_encrypted_attachment_confirmation_request(
            request, room_id=room_id, sender=sender, trigger_event_id=trigger_event_id,
        )
        return
    _in_background(_wait_then_ask(request, rank, room_id=room_id, sender=sender, trigger_event_id=trigger_event_id))


async def _wait_then_ask(request: Request, rank: int, *, room_id: str, sender: str, trigger_event_id: str) -> None:
    from bridge.media import ENCRYPTED_ATTACHMENT_WARNING_MARKER, _send_encrypted_attachment_confirmation_request

    try:
        await asyncio.sleep(rank * request.app.state.config.encrypted_media.wait_seconds)
        for related in await _related(request, room_id, trigger_event_id):
            if related.get("type") == ANNOUNCE_TYPE:
                await handle_announcement(request, related)
                return
            content = related.get("content") or {}
            if (content.get(PROMPT_FIELD) or {}).get("prompt_for") == trigger_event_id or (
                ENCRYPTED_ATTACHMENT_WARNING_MARKER in str(content.get("body") or "")
            ):
                return  # a bridge before this one asked
        await _send_encrypted_attachment_confirmation_request(
            request, room_id=room_id, sender=sender, trigger_event_id=trigger_event_id,
        )
    except Exception:
        logger.exception("Waiting to ask about the encrypted attachment in %s failed", trigger_event_id)


# -- announcing a copy -----------------------------------------------------------------------


async def announce(request: Request, *, room_id: str, post_event_id: str, content: dict, plaintext_mxc: str) -> None:
    """Tell the other listed bridges about the decrypted copy of the post's
    file. Nothing without a list."""
    config = request.app.state.config
    encrypted_file = content.get("file") if isinstance(content.get("file"), dict) else None
    if not _bridges(config) or encrypted_file is None or not isinstance(encrypted_file.get("url"), str):
        return
    info = {
        k: v for k, v in (content.get("info") or {}).items()
        if not k.startswith("thumbnail_") and k != "xyz.amorgan.blurhash"
    }
    try:
        await request.app.state.synapse.send_message_event(
            room_id,
            {
                "m.relates_to": {"rel_type": ANNOUNCE_TYPE, "event_id": post_event_id},
                "url": plaintext_mxc, "encrypted_url": encrypted_file["url"], "info": info,
            },
            event_type=ANNOUNCE_TYPE,
            as_user_id=_bot(config),
        )
    except SynapseError:
        logger.warning("Could not announce the decrypted copy for %s", post_event_id, exc_info=True)


def announce_if_needed(request: Request, *, room_id: str, post_event_id: str, content: dict, plaintext_mxc: str) -> None:
    """A copy this bridge already had, used for a new post: announced unless
    a bridge already did, so the others needn't ask. In the background."""
    if not _bridges(request.app.state.config) or not post_event_id:
        return

    async def run() -> None:
        try:
            if any(e.get("type") == ANNOUNCE_TYPE for e in await _related(request, room_id, post_event_id, ours=True)):
                return
            await announce(request, room_id=room_id, post_event_id=post_event_id, content=content,
                           plaintext_mxc=plaintext_mxc)
        except Exception:
            logger.exception("Announcing the decrypted copy for %s failed", post_event_id)

    _in_background(run())


async def handle_announcement(request: Request, event: dict) -> None:
    """Another listed bridge's decrypted copy: checked against the post's own
    file, cached, and the post sent on as if it had been confirmed here."""
    from bridge.appservice_routes import _dispatch_outbound_send
    from bridge.media import unresolvable_encrypted_attachment_mxc

    config = request.app.state.config
    sender = event.get("sender", "")
    if sender not in _bridges(config) or sender == _bot(config):
        return
    content = event.get("content") or {}
    relation = content.get("m.relates_to") or {}
    room_id, post_event_id, plaintext_mxc = event.get("room_id", ""), relation.get("event_id"), content.get("url")
    if relation.get("rel_type") != ANNOUNCE_TYPE or not isinstance(post_event_id, str):
        return
    if not isinstance(plaintext_mxc, str) or not plaintext_mxc.startswith("mxc://"):
        return
    synapse = request.app.state.synapse
    try:
        post = await synapse.get_event(room_id, post_event_id, as_user_id=_bot(config))
    except SynapseError:
        return
    post_content = post.get("content") or {}
    new_content = post_content.get("m.new_content")
    effective = new_content if isinstance(new_content, dict) else post_content
    encrypted_mxc = unresolvable_encrypted_attachment_mxc(effective)
    if encrypted_mxc is None or encrypted_mxc != content.get("encrypted_url"):
        return

    repository = request.app.state.repository
    if await repository.get_custom_emoji_mxc(encrypted_mxc) is None:
        server_name, media_id = plaintext_mxc.removeprefix("mxc://").split("/", 1)
        try:
            download = await synapse.download_media(server_name, media_id)
            ciphertext = await asyncio.to_thread(_encrypt, effective["file"], download.content)
            matches = hashlib.sha256(ciphertext).digest() == _b64(effective["file"]["hashes"]["sha256"], urlsafe=False)
        except (SynapseError, KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            logger.warning("%s announced a copy of %s that isn't the same file; ignored", sender, encrypted_mxc)
            return
        await repository.record_custom_emoji_mxc(encrypted_mxc, plaintext_mxc)
    # Its usual path finds the copy in the cache (resolve_attachment_or_request_confirmation),
    # and every usual check applies; a post already sent isn't sent again.
    await _dispatch_outbound_send(request, post)


# -- the chain hook --------------------------------------------------------------------------


async def maybe_handle_shared_encrypted_media(request: Request, event: dict) -> bool:
    """Early in the dispatch chain (before reactions are federated): another
    bridge's announced copy, or a 👍 confirming one of this bridge's own
    encrypted-attachment questions. Returns True if the event was one of
    those."""
    from bridge.commands import _reply_target_event_id
    from bridge.media import ENCRYPTED_ATTACHMENT_WARNING_MARKER
    from bridge.activitypub.sanitize import strip_reply_fallback

    if event.get("type") == ANNOUNCE_TYPE:
        await handle_announcement(request, event)
        return True
    content = event.get("content") or {}
    reaction = event.get("type") == "m.reaction"
    if reaction:
        relation = content.get("m.relates_to") or {}
        if relation.get("rel_type") != "m.annotation" or not is_confirm_reaction(str(relation.get("key") or "")):
            return False
        target = relation.get("event_id")
    elif event.get("type") == "m.room.message" and strip_reply_fallback(content.get("body") or "").strip().lower() == "confirm":
        target = _reply_target_event_id(content)
    else:
        return False
    room_id, sender = event.get("room_id", ""), event.get("sender", "")
    if not isinstance(target, str) or not room_id or not sender:
        return False
    config = request.app.state.config
    try:
        warning = await request.app.state.synapse.get_event(room_id, target, as_user_id=_bot(config))
    except SynapseError:
        return False
    warning_content = warning.get("content") or {}
    asked = PROMPT_FIELD in warning_content or ENCRYPTED_ATTACHMENT_WARNING_MARKER in str(warning_content.get("body") or "")
    if warning.get("sender") in set(_bridges(config)) - {_bot(config)} and asked:
        return True  # answering a sibling bridge's question: it acts on it; never federated from here
    if not reaction:
        return False  # a "confirm" for this bridge: maybe_handle_encrypted_attachment_confirmation
    if warning.get("sender") != _bot(config) or PROMPT_FIELD not in warning_content:
        return False  # a 👍 on anything else is just a reaction
    from bridge.appservice_routes import resume_encrypted_attachment

    return await resume_encrypted_attachment(
        request, room_id=room_id, sender=sender, warning_event_id=target, confirm_event_id=event.get("event_id") or "",
    )
