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
import select
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import coincurve

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


def query(port: int, secret: coincurve.PrivateKey) -> tuple[bool, str]:
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
    args = [a for a in sys.argv[1:] if a != "--memory"]
    agent = args[0] if args else None
    if "--memory" in sys.argv[1:]:
        run_memory(agent)
    else:
        run(agent)


def run(agent: str | None) -> None:
    cfg = agent_config.load(agent)
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


# ── memory: prove agnova-memory works, without buzz-acp or a token ─────────
#
# Same instinct as the transport check above, applied to the plane the
# memory-plane review found had no proof at all: `just ci-test` is green
# with qortia_backend.py at 100% coverage while an agent launched by
# `agnova up` had no memory tools at all, because nothing exercised the
# actual subprocess+stdio+JSON-RPC path buzz-acp drives. This does —
# spawning the real `agnova-memory` console script, speaking the real
# protocol, in the order things break: does the process start, does it
# answer initialize, does tools/list include what it should, does a
# memory written really come back out.


class _RpcError(RuntimeError):
    pass


def _rpc_send(proc: subprocess.Popen[str], method: str, params: dict[str, Any]) -> int:
    msg_id = uuid.uuid4().int & 0xFFFF
    assert proc.stdin is not None
    proc.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n"
    )
    proc.stdin.flush()
    return msg_id


def _rpc_recv(proc: subprocess.Popen[str], expect_id: int, timeout: float = 10.0) -> dict[str, Any]:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise _RpcError(f"no response within {timeout}s (id={expect_id})")
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise _RpcError(f"agnova-memory exited — stderr: {stderr[:400]}")
    reply: dict[str, Any] = json.loads(line)
    if reply.get("id") != expect_id:
        raise _RpcError(f"reply id {reply.get('id')} != request id {expect_id}")
    if "error" in reply:
        raise _RpcError(str(reply["error"]))
    return reply


def _rpc_call(proc: subprocess.Popen[str], method: str, params: dict[str, Any]) -> dict[str, Any]:
    msg_id = _rpc_send(proc, method, params)
    return _rpc_recv(proc, msg_id)


def run_memory(agent: str | None) -> None:
    cfg = agent_config.load(agent)
    env = cfg.harness_env()
    backend_name = env.get("AGENT_MEMORY_BACKEND", "git")
    print(f"agent {cfg.name}   backend {backend_name}\n")

    binary = shutil.which("agnova-memory")
    if not binary:
        bad("agnova-memory not on PATH — is agnova installed? (pip install -e . / uv sync)")
        sys.exit(1)

    # S603: argv is the resolved agnova-memory console script, never network input.
    proc = subprocess.Popen(  # noqa: S603
        [binary],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(cfg.home),
    )

    steps: list[tuple[str, Any]] = []
    marker = f"agnova-selftest-{uuid.uuid4().hex[:10]}"
    memory_id = ""
    try:
        init = _rpc_call(proc, "initialize", {})
        steps.append(("initialize", init["result"]["serverInfo"]["name"] == "agnova-memory"))

        listed = _rpc_call(proc, "tools/list", {})
        names = {t["name"] for t in listed["result"]["tools"]}
        expected = {"context", "recall", "remember", "forget", "reflect"}
        steps.append(("tools/list has context/recall/remember/forget/reflect", expected <= names))

        remembered = _rpc_call(
            proc,
            "tools/call",
            {
                "name": "remember",
                "arguments": {"items": [{"content": f"selftest memory containing {marker}"}]},
            },
        )
        remember_payload = json.loads(remembered["result"]["content"][0]["text"])
        remember_ok = not remembered["result"].get("isError") and bool(remember_payload)
        steps.append(("remember stores a memory", remember_ok))
        if remember_ok:
            memory_id = remember_payload[0]["id"]

        recalled = _rpc_call(proc, "tools/call", {"name": "recall", "arguments": {"query": marker}})
        recall_payload = json.loads(recalled["result"]["content"][0]["text"])
        found = any(marker in item.get("content", "") for item in recall_payload)
        steps.append(("recall finds what remember stored", found))

        if memory_id:
            forgotten = _rpc_call(
                proc, "tools/call", {"name": "forget", "arguments": {"id": memory_id}}
            )
            forget_payload = json.loads(forgotten["result"]["content"][0]["text"])
            steps.append(("forget removes it", forget_payload.get("forgotten") is True))
        else:
            steps.append(("forget removes it", False))

    except (_RpcError, KeyError, IndexError, json.JSONDecodeError) as exc:
        steps.append((f"protocol error: {exc}", False))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    failures = 0
    for label, passed in steps:
        (ok if passed else bad)(label)
        failures += not passed
        if not passed:
            break  # in the order things break — later steps depend on earlier ones

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
