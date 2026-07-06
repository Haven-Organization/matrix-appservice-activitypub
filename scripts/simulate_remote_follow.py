#!/usr/bin/env python3
"""Simulates a remote Mastodon-style instance following one of this bridge's
local actors, to manually verify the inbound-Follow / signature-verification
path end-to-end against a *running* bridge.

It stands up a tiny HTTP server playing the role of the remote instance
(serving an Actor document with a freshly generated keypair), sends a
properly signed ``Follow`` activity to the bridge's ``/inbox/{username}``,
and waits to receive a signed ``Accept`` back -- verifying that signature
against the bridge's own published public key. It then checks that
``GET /followers/{username}`` lists the simulated actor.

Prerequisites: the bridge must already be running, and ``--username`` must
already be a linked local actor (run ``!bridge link profile`` in a room
first, or pass the bridge's own service-actor localpart from
``appservice.bot_localpart`` in config.yaml).

Usage:
    python scripts/simulate_remote_follow.py --bridge-url http://127.0.0.1:8090 --username alice
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.activitypub.signatures import SignatureError, sign_request, verify_signature_string  # noqa: E402
from bridge.crypto import generate_keypair  # noqa: E402

received_accept: dict | None = None
received_event = threading.Event()
verification_error: str | None = None


def make_handler(inbox_path: str, actor_doc: dict, bridge_public_key_pem: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            pass  # keep stdout quiet; we print our own status lines

        def do_GET(self) -> None:
            if self.path == _path_of(actor_doc["id"]):
                body = json.dumps(actor_doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/activity+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            global received_accept, verification_error
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)

            if self.path != inbox_path:
                self.send_response(404)
                self.end_headers()
                return

            try:
                headers = {k.lower(): v for k, v in self.headers.items()}
                signature_header = headers.get("signature")
                if not signature_header:
                    raise SignatureError("missing Signature header on the Accept")
                verify_signature_string(
                    method="POST",
                    path=self.path,
                    headers=headers,
                    signature_header=signature_header,
                    public_key_pem=bridge_public_key_pem,
                )
                received_accept = json.loads(body)
            except Exception as exc:
                verification_error = str(exc)

            self.send_response(202)
            self.end_headers()
            received_event.set()

    return Handler


def _path_of(url: str) -> str:
    # Minimal path extraction without pulling in urllib.parse for one field.
    return "/" + url.split("://", 1)[1].split("/", 1)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8090", help="Base URL of the running bridge")
    parser.add_argument("--username", required=True, help="Local bridge actor to follow, e.g. alice")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=9009)
    parser.add_argument("--timeout", type=float, default=10.0, help="Seconds to wait for the Accept")
    args = parser.parse_args()

    remote_base = f"http://{args.listen_host}:{args.listen_port}"
    actor_id = f"{remote_base}/users/testbot"
    inbox_url = f"{actor_id}/inbox"
    inbox_path = _path_of(inbox_url)
    key_id = f"{actor_id}#main-key"

    private_key_pem, public_key_pem = generate_keypair()
    actor_doc = {
        "@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
        "id": actor_id,
        "type": "Person",
        "preferredUsername": "testbot",
        "inbox": inbox_url,
        "outbox": f"{actor_id}/outbox",
        "followers": f"{actor_id}/followers",
        "following": f"{actor_id}/following",
        "publicKey": {"id": key_id, "owner": actor_id, "publicKeyPem": public_key_pem},
    }

    print(f"==> Fetching bridge actor: {args.bridge_url}/actor/{args.username}")
    try:
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.get(
                f"{args.bridge_url}/actor/{args.username}", headers={"Accept": "application/activity+json"}
            )
            resp.raise_for_status()
            target_actor = resp.json()
    except httpx.HTTPError as exc:
        print(f"FAIL: could not fetch the bridge's actor -- is it running and is {args.username!r} linked? ({exc})")
        return 1

    target_inbox = target_actor["inbox"]
    bridge_public_key_pem = target_actor["publicKey"]["publicKeyPem"]
    print(f"    -> {args.username}'s inbox is {target_inbox}")

    server = ThreadingHTTPServer(
        (args.listen_host, args.listen_port), make_handler(inbox_path, actor_doc, bridge_public_key_pem)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"==> Simulated remote actor serving at {remote_base} (id: {actor_id})")

    try:
        follow_activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{actor_id}#follows/{int(time.time())}",
            "type": "Follow",
            "actor": actor_id,
            "object": target_actor["id"],
        }
        body = json.dumps(follow_activity).encode("utf-8")
        headers = sign_request(
            method="POST", url=target_inbox, body=body, key_id=key_id, private_key_pem=private_key_pem
        )
        headers["Content-Type"] = "application/activity+json"

        print(f"==> Sending signed Follow to {target_inbox}")
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.post(target_inbox, content=body, headers=headers)
        print(f"    -> bridge responded {resp.status_code}")
        if resp.status_code >= 300:
            print(f"FAIL: bridge rejected the Follow: {resp.text[:300]}")
            return 1

        print(f"==> Waiting up to {args.timeout:.0f}s for the bridge to send a signed Accept back...")
        if not received_event.wait(timeout=args.timeout):
            print("FAIL: no Accept received -- check the bridge's logs")
            return 1

        if verification_error:
            print(f"FAIL: Accept received but failed verification: {verification_error}")
            return 1
        if received_accept is None or received_accept.get("type") != "Accept":
            print(f"FAIL: expected a verified Accept, got: {received_accept}")
            return 1
        print("PASS: bridge accepted the Follow and sent back a correctly-signed Accept")

        print(f"==> Checking GET {args.bridge_url}/followers/{args.username} includes us")
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.get(f"{args.bridge_url}/followers/{args.username}")
            resp.raise_for_status()
            followers = resp.json().get("orderedItems", [])
        if actor_id not in followers:
            print(f"FAIL: {actor_id} not present in followers collection: {followers}")
            return 1
        print("PASS: followers collection includes the simulated remote actor")

        print("\nAll checks passed.")
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
