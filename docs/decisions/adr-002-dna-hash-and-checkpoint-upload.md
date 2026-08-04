# ADR-002 — DNA hash algorithm and checkpoint upload

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** founder

## Context

An operator (or control plane) may ship `AGENT_DNA_HASH` with an agent overlay.
Agnova must fail-closed on mismatch. Memory durability moves from GitHub
`git push` alone to an optional generic HTTP upload of a git bundle
(control-plane agnostic).

## Decision

1. **DNA hash v0.1:** For each relative path in `AGENT_DNA_PATHS` (default:
   DNA.md, IDENTITY.md, SOUL.md, DOMAIN.md, AGENTS.md, USER.md), if the file
   exists under the agent home, append `relpath + NUL + content + LF` (UTF-8 /
   raw bytes). Digest = SHA-256; compare as `sha256:{hex}`. Missing optional
   files are skipped. Whoever mints the overlay hash and Agnova must use this
   exact algorithm.

2. **Checkpoint:** After a local commit of configured paths, if a git remote
   exists, push as today. If `AGENT_CHECKPOINT_UPLOAD_URL` + token are set,
   also `git bundle create` of `HEAD` and `POST` the bundle bytes to that URL
   with `Authorization: Bearer <token>`, `Content-Type: application/octet-stream`,
   and header `X-Agent-Pubkey: <hex>` when a Nostr key is available. Upload
   failures are logged; they must not kill the agent.

3. **agnova-memory:** Stdlib MCP stdio server; default backend `git`. No
   in-process control-plane or memory-engine imports. An HTTP memory backend
   (e.g. [Qortia](https://github.com/kaiverse-io/qortia)) is deferred until that
   product's eval gate.

## Consequences

Hash algorithm is a cross-component contract — change only with a version bump
in this ADR and every overlay that mints `AGENT_DNA_HASH`.
