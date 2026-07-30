# ADR-001: A transport front door, not a protocol shim

- **Status:** Accepted
- **Date:** 2026-07-30

## Context

`buzz-acp` — Buzz's own agent harness — could not connect to the relay from a
Claude Code cloud sandbox. It failed at the WebSocket upgrade with
`403 Forbidden`. The relay offers no HTTP streaming fallback: its surface is
`POST /events`, `/query`, `/count`, with no SSE and no long-poll.

Three prior investigations concluded that the environment's proxy blocked
WebSocket upgrades outright, and the sandbox's own proxy documentation lists
"WebSocket upgrades" among unsupported features, which appeared to confirm it.

Two designs were on the table:

1. **Poll loop** (what was running): ~950 lines reimplementing mention
   filtering, exactly-once handling, cursor management and prompt assembly over
   the HTTP bridge.
2. **Protocol shim**: ~300 lines emulating a Nostr relay on loopback — accepting
   `REQ`/`EVENT`/`CLOSE` from `buzz-acp` and translating them to HTTP bridge
   calls — so `buzz-acp` could run unmodified.

## Decision

Neither. Build a **transport front door**: a loopback listener that rewrites
three fields and carries the connection out over a `CONNECT` tunnel.

This became possible when the premise turned out to be false. Testing the same
upgrade two ways:

| path | result |
|---|---|
| direct TLS to the relay | 403 Forbidden |
| via the proxy's `CONNECT` tunnel, correct `Host` | **101 Switching Protocols** |

WebSockets were never blocked. `buzz-cli` worked because reqwest honours
`HTTPS_PROXY`; `buzz-acp` failed because `tokio-tungstenite` never reads it and
dials direct, landing on a transparently-intercepted path. Nobody had tried a
tunnelled upgrade.

## Consequences

**Good.**

- Real WebSocket push — no poll interval.
- Presence works. The relay refuses ephemeral events on HTTP outright
  (`POST /events` with kind 20001 returns
  `400 "kind 20001 is only accepted via WebSocket"`), so a green dot was
  impossible under both rejected designs, and is verified working under this one.
- `buzz-acp` runs **stock** — no fork, no patched CA store, no `/etc/hosts`
  entry, no certificates — because loopback is plaintext and proxy-exempt.
- ~950 lines of our agent logic deleted in favour of upstream's.
- Cheaper per message: `buzz-acp` keeps agent processes warm, so the ~35k-token
  Claude Code baseline is paid once per process rather than once per reply.

**Bad.**

- The front door holds the agent's private key, because re-signing NIP-98 and
  NIP-42 is the substance of the fix. Mitigated by binding to loopback only and
  by re-signing exclusively events that already carry that agent's pubkey.
- One more process to supervise.
- We now depend on `buzz-acp`'s wire behaviour at a lower level than an API —
  if it changes how it authenticates, the front door must follow.

**Neutral.**

- The front door is unnecessary on a normal host. Point `BUZZ_RELAY_URL` at the
  relay directly and it drops out of the picture entirely.

## Alternatives rejected

- **Protocol shim over the HTTP bridge.** Larger, and structurally incapable of
  push, presence or typing indicators, since its upstream leg is HTTP.
- **`/etc/hosts` override plus a port-443 forwarder.** Works in principle, but
  requires a system file change and puts TLS back on the local hop, which
  reintroduces the CA-store rebuild the front door avoids.
- **Run a `buzz-relay` locally.** Needs Postgres, binds connections to a
  community by `Host`, and would need two-way sync with the hosted relay.
- **Ask an administrator to allow direct WebSockets.** Would have been the
  cheapest fix if the premise had been true. It was not: nothing needs allowing.
