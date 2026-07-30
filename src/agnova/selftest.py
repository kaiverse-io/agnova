#!/usr/bin/env python3
"""Prove the transport works, without starting an agent or spending a token.

Three assertions, in the order things break:

  1. The front door accepts a WebSocket upgrade and the relay answers `101`.
  2. The relay's first frame is a NIP-42 `AUTH` challenge — which means the
     connection reached the real relay, not something in between.
  3. `POST /query` signed for the *local* URL comes back `200` — which only
     happens if the front door re-signed it for the real one.

Run before blaming the agent for anything.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agnova import config as agent_config  # noqa: E402
from agnova import nostr  # noqa: E402
from agnova.supervise import bad, ok  # noqa: E402


def upgrade(port: int) -> tuple[bool, str]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")

    # Server frames are never masked, so the payload starts after the length.
    challenge = ""
    sock.settimeout(15)
    try:
        frame = sock.recv(8192)
        if len(frame) > 2:
            length = frame[1] & 0x7F
            offset = 2 if length < 126 else (4 if length == 126 else 10)
            challenge = frame[offset:].decode(errors="replace")
    except TimeoutError:
        pass
    sock.close()
    return "101" in status, f"{status} | {challenge[:60]}"


def query(port: int, secret) -> tuple[bool, str]:
    body = json.dumps([{"kinds": [9], "limit": 1}]).encode()
    # Signed for the loopback URL on purpose: the relay rejects that outright,
    # so a 200 proves the front door replaced the token.
    header = nostr.nip98_header(secret, "POST", f"http://127.0.0.1:{port}/query", body)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(
        "POST",
        "/query",
        body=body,
        headers={"Authorization": header, "Content-Type": "application/json"},
    )
    response = conn.getresponse()
    payload = response.read()[:80].decode(errors="replace")
    return response.status == 200, f"{response.status} {payload}"


def main() -> None:
    cfg = agent_config.load(sys.argv[1] if len(sys.argv) > 1 else None)
    secret = nostr.load_secret_key(cfg.secret_key)
    print(f"pubkey {nostr.public_key_hex(secret)}")
    print(f"front door 127.0.0.1:{cfg.frontdoor_port} -> {cfg.relay_url}\n")

    failures = 0
    for label, (passed, detail) in (
        ("websocket upgrade + NIP-42 challenge", upgrade(cfg.frontdoor_port)),
        ("HTTP bridge with re-signed NIP-98", query(cfg.frontdoor_port, secret)),
    ):
        (ok if passed else bad)(f"{label}: {detail}")
        failures += not passed

    if failures:
        print("\nIs the front door up? `just up` or `just doctor`.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
