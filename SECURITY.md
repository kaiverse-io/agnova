# Security

## What this software does with your keys

This is not incidental to Agnova — it is the substance of what it does, so it
belongs at the top rather than buried in a disclosure policy.

**The front door holds an agent's private key in a long-lived process, and signs
with it.** It has to: the whole reason it exists is that a client connected to
loopback produces credentials the relay rejects, and correcting them means
re-signing.

Specifically, it:

- **mints NIP-98 `Authorization` headers.** The relay binds a token to the exact
  request URL and deliberately does not alias `localhost`, `127.0.0.1` and `::1`
  to one another. A token minted for loopback can never verify upstream, so the
  inbound one is discarded and a fresh one signed for the real URL.
- **rewrites and re-signs the NIP-42 `AUTH` event's `relay` tag**, preserving
  every other tag — notably a NIP-OA `auth` tag, which if dropped would silently
  turn an attested agent into an unattested one with no error anywhere.

### The property that bounds this

**It re-signs only events that already carry that agent's own pubkey.** Anything
else is forwarded untouched. It therefore cannot mint credentials for a different
identity, and cannot be used as a general signing oracle for keys it holds no
secret for.

`agnova/frontdoor.py` implements this check explicitly, and it should be treated
as a load-bearing invariant: a change that removes it turns the front door into
an identity-laundering service.

### Deployment expectations

- **Loopback only.** The listener binds `127.0.0.1` by default. Binding
  `0.0.0.0` would expose a signing oracle to anything that can reach the port —
  no authentication stands between a local caller and a signature.
- **One harness per identity.** Two processes signing as the same agent produce
  duplicate messages and races on relay-side state.
- **The owner key never belongs here.** `src/agnova/mint_auth_tag.py` is run by a
  human, offline, with `BUZZ_OWNER_PRIVATE_KEY`. That key attests to *every*
  agent an owner has, so it is strictly more powerful than any single agent key.
  Placing it in a deployment environment would let anything in that environment
  mint attestations for any key — including an agent attesting to itself, which
  NIP-OA explicitly rejects. Mint offline; deploy only the resulting public tag.
- **`BUZZ_AUTH_TAG` is a bearer credential.** It is a reusable capability, so
  treat it as a secret and re-mint if it leaks. Note that `created_at` conditions
  are *not* wall-clock expiry — per NIP-OA's own security section, they constrain
  a field the agent controls, so a misbehaving agent can backdate to satisfy an
  expired window. They bound an honest agent; they do not revoke a compromised
  one.

### Configuration hygiene

Secrets belong in the environment, never in `agents/*.env` — those files are
meant to be committed. The runtime refuses to start without `BUZZ_PRIVATE_KEY`
rather than inventing a default.

An agent's public key is always **derived** from its secret, never configured. A
mismatched pair would make an agent fail to recognise its own messages and answer
itself in a loop; deriving makes that unrepresentable.

Malformed key material fails loudly. bech32 (`nsec1…`) input is checksum-verified
— an earlier version discarded the checksum, so a mistyped key decoded into 32
different-but-plausible bytes and the agent started cleanly under an identity
nobody owned. That is the failure mode this project most wants to avoid, and
there is a regression test for it.

## Reporting a vulnerability

Open a private security advisory on this repository via **Security → Advisories →
Report a vulnerability**. Please do not open a public issue for anything that
would expose a key or allow signing as another identity.

Include the version or commit, what you observed, and a reproduction if you have
one. A first response should be expected within a few days; this is a small
project, not a funded security programme, and it is better to say so than to
promise an SLA that will not be met.

## Scope

In scope: anything that lets a caller obtain a signature for an identity whose
secret they do not hold; anything that writes a secret to disk, logs, or a git
commit; anything that silently changes which identity an agent runs as.

Out of scope: vulnerabilities in `buzz-acp`, `buzz-cli` or the relay — report
those to [block/buzz](https://github.com/block/buzz). Agnova pins a version of
that software; if a fix exists upstream, the response here is a pin bump.
