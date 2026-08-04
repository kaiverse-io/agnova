# Agnova Architecture

> Hand-maintained, not generated. `just ci-arch` fails CI if a top-level module/package under
> `src/agnova/` has no matching section here — see `AGENTS.md` "Architecture
> documentation". Diagrams are plain ASCII/Unicode box-drawing text in an untagged fenced code
> block, not Mermaid — a diagram you can update in the same PR as the code, not a separate
> rendering step.

## Executive Summary

Agnova is a **Buzz agent runtime harness**: it supervises stock `buzz-acp`, optionally puts a
TLS front door in front of the relay, checkpoints named memory paths (git push and/or HTTP
git-bundle upload), and enforces operator-owned DNA integrity at boot. Buzz is the only
channel today; a multi-protocol adapter seam is deferred. It must not import Aither or
Qortia — control plane and memory engine talk over HTTP/OpenAPI only.

## System Overview

```
  Buzz relay  <──TLS──>  frontdoor (optional)  <──ws──>  buzz-acp (ACP harness)
                                                              │
  operator / Aither overlay ──DNA hash──> supervise.up ───────┤
                                                              │
  checkpoint loop ──git commit/push──┐                        │
                     └─HTTP git bundle─> control plane         │
                                                              │
  agnova-memory MCP (stdio) <──git backend──> memory/ files
```

## Components

Each top-level module or package directly under `src/agnova/` gets a `##`
section below. `just ci-arch` only checks that the section exists.

## checkpoint

Purpose: interval loop that commits configured paths and optionally pushes / uploads a git
bundle (`AGENT_CHECKPOINT_UPLOAD_URL`). Knows nothing about memory semantics.
Depends on: `config`, `nostr` (pubkey header for upload).

## config

Purpose: load `AgentConfig` from env + `agents/<name>.env`; build harness env.
Depends on: `dna.paths_from_env`.

## dna

Purpose: DNA hash v0.1 (`sha256:` of ordered `relpath\\0content\\n`); fail-closed enforce at
boot; best-effort chmod readonly.
Depends on: stdlib only.

## engram

Purpose: render identity engram material from the agent home for the harness.
Depends on: filesystem under agent home.

## frontdoor

Purpose: loopback WebSocket/TLS proxy between `buzz-acp` and the real Buzz relay
(CONNECT / HTTPS_PROXY aware).
Depends on: `nostr` for auth material when required.

## memory

Purpose: `agnova-memory` MCP stdio server + git filesystem backend
(`context|recall|remember|forget`). Qortia backend deferred.
Depends on: stdlib + agent home paths. Forbids importing `aither`/`qortia`.

## mint_auth_tag

Purpose: CLI helper to mint NIP-OA owner attestation tags.
Depends on: `nostr`.

## nostr

Purpose: Nostr key load/derive helpers used by harness and checkpoint upload headers.
Depends on: coincurve / crypto primitives as packaged.

## runtime

Purpose: low-level process spawn/pid helpers for supervised children.
Depends on: stdlib.

## scaffold

Purpose: shape a new agent home / repo layout so agents do not maintain runtime code.
Depends on: templates under the package.

## selftest

Purpose: prove transport without spending a model token.
Depends on: `config`, frontdoor/harness paths.

## supervise

Purpose: CLI entry (`agnova up|down|status|doctor|…`); DNA enforce then start frontdoor,
checkpoint, harness.
Depends on: `config`, `dna`, `checkpoint`, `runtime`, `frontdoor`.

## upstream

Purpose: pin/fetch upstream `buzz-acp` / related binaries per `upstream.lock`.
Depends on: network + lockfile.

## Known Limitations

- Memory MCP `qortia` backend not implemented until Qortia G1 scored evals.
- Checkpoint upload is best-effort; failures must not kill the agent.
- Warm Cursor Cloud / Agnova cohabitation is spike-gated (Aither Phase 4).
