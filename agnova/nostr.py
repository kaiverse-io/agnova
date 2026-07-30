"""Nostr primitives — keys, event signing, NIP-98 HTTP auth.

Deliberately small and dependency-light: `coincurve` for schnorr, nothing else.
The Buzz CLI does all of this already, but the front door has to sign *inside*
a proxied request, where shelling out per request is not an option.

Nothing here knows about any particular agent. Identity arrives as a key.
"""

from __future__ import annotations

import hashlib
import json
import time
from base64 import b64encode

import coincurve

# NIP-98 HTTP auth. One kind, one purpose.
KIND_HTTP_AUTH = 27235

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _bech32_polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i, generator in enumerate(_BECH32_GENERATOR):
            if (top >> i) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_decode(bech: str) -> tuple[str, bytes]:
    """Minimal bech32 decode — enough for `nsec1…` / `npub1…`.

    Vendored rather than pulled in as a dependency: this is a short piece of
    frozen spec, and a key-handling path is the last place to want an unpinned
    transitive dependency tree.

    **The checksum is verified, not discarded.** An earlier version dropped the
    six checksum symbols without checking them, which meant a mistyped or
    truncated key decoded into thirty-two different-but-plausible bytes instead
    of failing. That is the worst possible outcome for key material: the agent
    starts cleanly under an identity nobody owns, posts as a stranger, and does
    not recognise its own messages. A malformed hex key already fails loudly, so
    a malformed bech32 key must too.
    """
    bech = bech.strip()
    if bech.lower() != bech and bech.upper() != bech:
        raise ValueError("bech32: mixed case")
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        raise ValueError("bech32: no separator")
    hrp, data_part = bech[:pos], bech[pos + 1 :]
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError as exc:
        raise ValueError("bech32: bad character") from exc

    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("bech32: checksum failed — the key is mistyped or truncated")

    # Regroup the payload's 5-bit words into bytes, checksum symbols excluded.
    acc = bits = 0
    out = bytearray()
    for value in data[:-6]:
        acc = (acc << 5) | value
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    # A valid encoding leaves under a byte of padding, and that padding is zero.
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        raise ValueError("bech32: invalid padding")
    return hrp, bytes(out)


def load_secret_key(raw: str) -> coincurve.PrivateKey:
    """Accept either a 64-char hex secret or an `nsec1…`.

    Buzz hands out both shapes depending on which tool minted the key, and an
    agent should not care which one its operator pasted.
    """
    raw = raw.strip()
    if raw.startswith("nsec"):
        hrp, key_bytes = _bech32_decode(raw)
        if hrp != "nsec" or len(key_bytes) != 32:
            raise ValueError("not a valid nsec")
        return coincurve.PrivateKey(key_bytes)
    return coincurve.PrivateKey.from_hex(raw)


def public_key_hex(secret: coincurve.PrivateKey) -> str:
    """The agent's x-only pubkey — always derived, never configured.

    A configured pubkey that disagrees with the configured secret is the worst
    class of bug available here: the agent stops recognising its own messages
    and answers itself forever. Deriving makes that unrepresentable.
    """
    return secret.public_key_xonly.format().hex()


def sign_event(secret: coincurve.PrivateKey, kind: int, tags: list, content: str = "") -> dict:
    """Build and sign a Nostr event (NIP-01 id, BIP-340 signature)."""
    event = {
        "pubkey": public_key_hex(secret),
        "created_at": int(time.time()),
        "kind": kind,
        "tags": tags,
        "content": content,
    }
    # NIP-01 serialisation is exact: no spaces, no key reordering, no ASCII
    # escaping. Any deviation changes the id and the relay rejects the event.
    serialised = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    event["id"] = hashlib.sha256(serialised).hexdigest()
    event["sig"] = secret.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def nip98_header(secret: coincurve.PrivateKey, method: str, url: str, body: bytes = b"") -> str:
    """An `Authorization: Nostr <base64>` value for one specific request.

    The `u` tag binds the token to the exact URL. The relay compares it against
    the URL it reconstructs from the request, and treats `localhost`,
    `127.0.0.1` and `::1` as three distinct hosts on purpose — so a token minted
    for the front door's local address can never be replayed upstream. That is
    precisely why the front door re-signs instead of forwarding.
    """
    tags = [["u", url], ["method", method.upper()]]
    if body:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = sign_event(secret, KIND_HTTP_AUTH, tags)
    return "Nostr " + b64encode(json.dumps(event).encode()).decode()
