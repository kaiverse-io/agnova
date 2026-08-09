"""Live test against a real external Nostr relay — not the local
docker-compose stack every other contract test in this repo uses. Proves the
front door's WS/HTTP proxying and NIP-42/NIP-98 re-signing work against real
infrastructure, not just a byte-for-byte-friendly local mock.

Skipped unless BUZZ_RELAY_URL points somewhere other than localhost — this
tier is inherently credential-gated (see agents/example.env's own 4-step
provisioning note: keypair -> buzz-admin add-member -> room access ->
owner-signed attestation, none of which this repo or a fresh key can grant
itself) and was verified by hand against wss://kaiverse.communities.buzz.xyz
on 2026-08-09 with a freshly generated, unregistered keypair:

  - WS upgrade + NIP-42 AUTH challenge: PASSED (real relay, real challenge)
  - HTTP bridge (re-signed NIP-98 query): 403 relay_membership_required — a
    clean, real rejection from the relay's own membership policy, not a
    connection failure or timeout. That's the honest ceiling for a key this
    repo can mint by itself: it proves the transport reaches a real relay
    and behaves coherently, not that it has access.

Run with real, provisioned credentials to get past the membership check too:

    BUZZ_RELAY_URL=wss://your-relay BUZZ_PRIVATE_KEY=<registered key> \\
      uv run pytest --no-cov tests/contract/test_live_relay.py -v
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import coincurve
import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from agnova import nostr  # noqa: E402
from agnova.selftest import query, upgrade  # noqa: E402

_RELAY_URL = os.environ.get("BUZZ_RELAY_URL", "")


def _points_at_a_real_relay(url: str) -> bool:
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return host not in ("localhost", "127.0.0.1", "::1", "")


pytestmark = pytest.mark.skipif(
    not _points_at_a_real_relay(_RELAY_URL),
    reason="set BUZZ_RELAY_URL to a real external relay to run this tier",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def frontdoor() -> Iterator[tuple[int, coincurve.PrivateKey]]:
    """Start the real front door standalone against BUZZ_RELAY_URL — no
    docker, no aither, no buzz-acp (which isn't even built in this repo).
    A fresh keypair is generated unless the caller already supplied one via
    BUZZ_PRIVATE_KEY (the "run with real credentials" path above)."""
    secret_raw = os.environ.get("BUZZ_PRIVATE_KEY") or coincurve.PrivateKey().to_hex()
    port = _free_port()
    env = {**os.environ, "BUZZ_RELAY_URL": _RELAY_URL, "BUZZ_PRIVATE_KEY": secret_raw}
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_SRC / "agnova" / "frontdoor.py"), "--port", str(port), "--quiet"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("front door never started listening")
        yield port, nostr.load_secret_key(secret_raw)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_websocket_upgrade_and_nip42_challenge_reach_the_real_relay(
    frontdoor: tuple[int, coincurve.PrivateKey],
) -> None:
    """This must pass regardless of membership — it proves the front door's
    TLS/WS proxying reaches a real relay and gets a real NIP-42 challenge
    back, which needs no registration at all."""
    port, _secret = frontdoor
    passed, detail = upgrade(port)
    assert passed, f"WS upgrade / NIP-42 challenge failed against a real relay: {detail}"


def test_http_bridge_gets_a_coherent_response_from_the_real_relay(
    frontdoor: tuple[int, coincurve.PrivateKey],
) -> None:
    """A real relay without this key registered as a member is expected to
    reject the query — the point of this test is that it rejects *cleanly*
    (a structured relay-side 401/403), not that it times out or the
    connection fails, which would mean the re-signing/proxying is broken.
    Passes outright if real membership was provided instead."""
    port, secret = frontdoor
    passed, detail = query(port, secret)
    if passed:
        return  # real, provisioned credentials — full round trip succeeded
    assert detail[:3].isdigit(), f"expected a real HTTP status from the relay, got: {detail!r}"
    status = int(detail[:3])
    assert status in (401, 403), f"expected an auth/membership rejection, got {status}: {detail}"
