# ADR-004 — Adapter seam: deferred until a second channel exists

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** founder
- **Supersedes:** issue #1 (intent record, now this ADR)

## Context

Agnova is Buzz-shaped: `frontdoor.py` speaks Nostr (NIP-98, NIP-42) and
`supervise.py` launches `buzz-acp`. Most of the rest — config, supervision,
checkpoint, DNA, doctor, the selftest contract, the pid/log layout — is already
protocol-agnostic.

The eventual shape is a protocol-agnostic core plus `adapters/buzz/`, so a Slack
or Matrix adapter is an addition rather than a refactor. The question is when to
cut that seam.

## Decision

**Do not build the adapter seam yet.** An abstraction designed from one example
encodes that example's accidents as the general case, and the second adapter
then fights it. Cut the seam when a second channel genuinely exists, and let that
channel shape it.

Two cheap invariants are kept true in the meantime, so the eventual cut stays
small:

1. **The checkpoint stays protocol-free.** It takes paths and keeps them durable.
   It knows nothing about Buzz and nothing about memory.
2. **New runtime-owned names carry no `BUZZ_` prefix.** A variable that agnova
   owns is named `AGENT_*` or `AGNOVA_*` (`AGENT_CHECKPOINT_PATHS`,
   `AGENT_MEMORY_BACKEND`, `AGENT_DNA_HASH`, `AGNOVA_HOME`, …). A variable that
   upstream reads keeps upstream's name (`BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`,
   `BUZZ_AUTH_TAG`, `BUZZ_ACP_*`) — renaming another project's contract would be
   a lie.

## Known exceptions

Issue #1 stated invariant 2 as already true. It is not. These variables are read
only by agnova, yet carry the `BUZZ_` prefix (checked against a block/buzz
checkout at `e982f70`; none appears anywhere under `crates/`):

| Variable | Read by |
|---|---|
| `BUZZ_AGENT_HOME` | `config.py`, `scaffold.py`, `memory/mcp_server.py` |
| `BUZZ_AGENT_NAME` | `config.py` |
| `BUZZ_AGENT_LABEL` | `config.py`, `scaffold.py` |
| `BUZZ_FRONTDOOR_PORT` | `config.py`, `frontdoor.py` |
| `BUZZ_OWNER_PUBKEY` | `config.py`, `engram.py`, `scaffold.py`, `supervise.py` |
| `BUZZ_OWNER_PRIVATE_KEY` | `mint_auth_tag.py` |
| `BUZZ_CA_BUNDLE` | `frontdoor.py` |

They are **not renamed now**: every agent's committed `agents/<name>.env` uses
them, and a rename with nothing to gain until a second adapter exists is churn.
When the seam is cut, rename them to `AGENT_*` with the old names accepted as
aliases for one release. `BUZZ_AGENT_HOME` may instead be replaced outright by
upstream's channel-scoped cwd (block/buzz#3822, tracked in issue #4).

No new variable may join this list.

## Consequences

- One protocol's assumptions stay concentrated in `frontdoor.py` and the
  harness launch in `supervise.py`, which is where the seam will be cut.
- A reviewer rejects a new runtime-owned `BUZZ_*` name by pointing here.
- Revisit when a second channel is real — not when one is hypothetical.
