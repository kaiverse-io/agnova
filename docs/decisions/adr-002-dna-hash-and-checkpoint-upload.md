# ADR-002 — DNA hash algorithm and checkpoint upload

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** founder / control-plane MVP plan

## Context

Aither overlays ship `AGENT_DNA_HASH`. Agnova must fail-closed on mismatch.
Memory durability moves from GitHub `git push` to optional generic HTTP upload
of a git bundle (control-plane agnostic).

## Decision

1. **DNA hash v0.1:** For each relative path in `AGENT_DNA_PATHS` (default:
   DNA.md, IDENTITY.md, SOUL.md, DOMAIN.md, AGENTS.md, USER.md), if the file
   exists under the agent home, append `relpath + NUL + content + LF` (UTF-8 /
   raw bytes). Digest = SHA-256; compare as `sha256:{hex}`. Missing optional
   files are skipped. Aither and Agnova must use this exact algorithm.

2. **Checkpoint:** After a local commit of configured paths, if a git remote
   exists, push as today. If `AGENT_CHECKPOINT_UPLOAD_URL` + token are set,
   also `git bundle create` of `HEAD` and POST per
   aither `docs/reference/contracts/checkpoint-upload.md`. Upload failures are
   logged; they must not kill the agent.

3. **agnova-memory:** Stdlib MCP stdio server; default backend `git`. No
   `aither`/`qortia` imports. Qortia backend deferred until Qortia eval gate.

## Consequences

Hash algorithm is a cross-repo contract — change only with a version bump in
both overlays and this ADR.
