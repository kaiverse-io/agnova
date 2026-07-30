#!/usr/bin/env python3
"""Mint a NIP-OA owner attestation for an agent key.

    agnova/mint-auth-tag.py --selftest
    agnova/mint-auth-tag.py --agent <agent-pubkey-hex> [--conditions '...']

**Run this as the owner, on your own machine — never as the agent, and never
inside a sandbox.**

The attestation is the owner saying "this agent is mine", so it is signed with
the owner key. An agent able to mint its own would be attesting to nothing, and
NIP-OA makes that explicit: if the owner pubkey equals the event pubkey, the
tag is invalid and MUST be rejected. The agent never sees this key; the script
reads it from BUZZ_OWNER_PRIVATE_KEY and prints only the resulting public tag.

An owner key is also strictly more powerful than any single agent key, because
it attests to all of them. That is why it does not belong in a deployment
environment: mint offline, deploy only the tag.

## Why this exists

An agent onboarded through a client's built-in agent flow usually receives an
attestation automatically. An agent that joined by claiming an invite is a
plain relay member and does not — so its messages carry no `auth` tag and
clients show its owner as unavailable.

`buzz agents draft-update` does not help: it targets agents already in the
relay's agent directory. `buzz-sdk` exposes `compute_auth_tag` but no CLI
surfaces it. Hence this — the smallest thing that closes the gap, built against
the spec at docs/nips/NIP-OA.md in block/buzz.

## After minting

Set the printed JSON as BUZZ_AUTH_TAG alongside BUZZ_PRIVATE_KEY. buzz-cli
injects it into every event the agent signs, and buzz-acp presents it on the
relay connection. Treat it as a secret: it is a reusable capability, so keep it
out of chat and version control, and re-mint if it leaks.

A `created_at<...` clause bounds which events it covers, but note NIP-OA's own
caveat: those clauses constrain a field the agent itself controls, so they are
not wall-clock expiry and not a revocation mechanism against a misbehaving
agent.
"""

import argparse
import hashlib
import json
import os
import sys

try:
    import coincurve
except ImportError:
    sys.exit("needs coincurve:  pip install coincurve")

DOMAIN = "nostr:agent-auth:"

# From NIP-OA's own test vector. If this stops passing, the preimage
# construction below has drifted from the spec and the tag would be rejected.
VECTOR = {
    "owner_secret": "00" * 31 + "01",
    "owner_pubkey": "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
    "agent_pubkey": "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
    "conditions": "kind=1&created_at<1713957000",
    "digest": "08cdecd55af4c28d3801fd69615dcf5cc04fab3bc134b38a840bf157197069a6",
    "sig": (
        "8b7df2575caf0a108374f8471722b233c53f9ff827a8b0f91861966c3b9dd5cb"
        "2e189eae9f49d72187674c2f5bd244145e10ff86c9f257ffe65a1ee5f108b369"
    ),
}


def digest(agent_pubkey, conditions):
    """SHA256 of `nostr:agent-auth:<agent_pubkey>:<conditions>`.

    The conditions string is signed verbatim — the spec forbids reordering,
    deduplicating or normalising it, since verifiers rebuild the preimage from
    the exact bytes in the tag.
    """
    preimage = f"{DOMAIN}{agent_pubkey}:{conditions}".encode()
    return hashlib.sha256(preimage).digest()


def selftest():
    v = VECTOR
    d = digest(v["agent_pubkey"], v["conditions"])
    ok_digest = d.hex() == v["digest"]
    print(f"digest matches spec vector: {ok_digest}")

    owner = coincurve.PrivateKey.from_hex(v["owner_secret"])
    ok_pub = owner.public_key_xonly.format().hex() == v["owner_pubkey"]
    print(f"owner pubkey derivation:    {ok_pub}")

    # Verify the spec's published signature rather than re-deriving it:
    # BIP-340 signing takes auxiliary randomness, so a fresh signature is
    # valid but need not be byte-identical to the vector's.
    xonly = coincurve.PublicKeyXOnly(bytes.fromhex(v["owner_pubkey"]))
    ok_sig = xonly.verify(bytes.fromhex(v["sig"]), d)
    print(f"spec signature verifies:    {ok_sig}")

    passed = ok_digest and ok_pub and ok_sig
    print("SELFTEST:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


def mint(agent_pubkey, conditions):
    secret = os.environ.get("BUZZ_OWNER_PRIVATE_KEY", "").strip()
    if not secret:
        sys.exit(
            "BUZZ_OWNER_PRIVATE_KEY is unset.\n"
            "Export the OWNER key — the one that owns the community, not the agent's:\n"
            "  export BUZZ_OWNER_PRIVATE_KEY=<hex>"
        )
    if secret.startswith("nsec"):
        sys.exit("give the key as hex, not nsec (this script does no bech32 decoding)")

    owner = coincurve.PrivateKey.from_hex(secret)
    owner_pubkey = owner.public_key_xonly.format().hex()
    if owner_pubkey == agent_pubkey:
        sys.exit("owner and agent pubkeys are identical — self-attestation is invalid per NIP-OA")

    sig = owner.sign_schnorr(digest(agent_pubkey, conditions)).hex()
    tag = ["auth", owner_pubkey, conditions, sig]

    print("\nBUZZ_AUTH_TAG (set this in the environment, treat as a secret):\n")
    print(json.dumps(tag))
    print(f"\nowner:      {owner_pubkey}")
    print(f"agent:      {agent_pubkey}")
    print(f"conditions: {conditions or '(none — no additional constraints)'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--selftest", action="store_true", help="verify against the NIP-OA spec vector")
    ap.add_argument("--agent", help="agent pubkey (64-char hex) to attest")
    ap.add_argument(
        "--conditions",
        default="",
        help="NIP-OA conditions, e.g. 'created_at<1800000000'. Empty = unconstrained.",
    )
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if not args.agent:
        ap.error("--agent is required (or use --selftest)")
    sys.exit(mint(args.agent, args.conditions))
