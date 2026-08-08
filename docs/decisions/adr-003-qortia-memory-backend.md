# ADR-003 — Qortia memory backend: HTTP-only, stdlib client, no schema mirroring

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** founder

## Context

ADR-002 deferred an HTTP memory backend "until [Qortia's] eval gate." Qortia
(github.com/kaiverse-io/qortia) now has a working, standalone FastAPI service with
stable `/v1/context`, `/v1/recall`, `/v1/remember`, `/v1/forget` routes (confirmed by
reading `qortia/remember.py`, `recall.py`, `auth.py`, `models.py` directly, not
assumed from a prior summary — which turned out to differ from the shipped contract
in several places; see "Contract deltas" below). `mcp_server._backend()` has raised
`SystemExit` for `AGENT_MEMORY_BACKEND=qortia` since G1; this ADR closes that gap.

Two decisions were live:

1. What HTTP client to use. `pyproject.toml` deliberately caps runtime dependencies
   at one (`coincurve`, for NIP-98/NIP-42 signing) because this process holds an
   agent's private key, and a small dependency tree is part of that being
   defensible.
2. How tightly the client should mirror Qortia's request/response schema.

## Decision

1. **Stdlib `http.client`, not a new dependency.** `checkpoint.py`'s
   `upload_bundle` already POSTs over `http.client` for the control-plane bundle
   upload; `QortiaMemoryBackend._request` follows the same shape (open a
   connection per call, `HTTPSConnection`/`HTTPConnection` by scheme, explicit
   timeout, read the whole body, close). No `requests`/`httpx`. `coincurve` stays
   the only runtime dependency.

2. **HTTP only, never `import qortia`.** `.importlinter`'s
   `no-in-process-memory-engine` contract already forbids importing the `qortia`
   package from anywhere under `agnova`; this backend talks JSON over the four
   routes above and nothing else. Matches the cross-repo rule in every project's
   AGENTS.md: aither, agnova and qortia talk HTTP/OpenAPI only, never via
   in-process Python imports.

3. **Auth**: `Authorization: Bearer <QORTIA_API_KEY>` + `X-Agent-Id: <QORTIA_AGENT_ID>`
   on every call, reading env vars the control plane already sets into every agent
   container (no new names to wire on that side).

4. **Don't mirror Qortia's server-side validation client-side.** Qortia's own
   pydantic models (`extra="forbid"`, a closed 6-value `type` enum, a 5-word
   content minimum, etc.) are the single source of truth. `QortiaMemoryBackend`
   whitelists which fields it forwards (so an unknown field doesn't 422 an entire
   `/v1/remember` batch) but does not re-implement Qortia's validation rules — a
   rejected request surfaces as a `QortiaError` carrying Qortia's own error
   `detail`, rather than failing differently (or silently passing) against a
   client-side copy of rules that could drift from the server's.

5. **`context()`'s `budget` stays client-side.** `GET /v1/context` takes no query
   parameters; Qortia returns a structured bundle (org chart / processes /
   handoffs / weekly summary / mental models / decisions / lessons), which this
   backend flattens into the single string `MemoryBackend.context() -> str`
   requires, then truncates to `budget` exactly like `GitMemoryBackend` already
   does — so callers see identical truncation semantics regardless of backend.

## Contract deltas (what changed between the assumed and the verified shape)

The task that produced this backend started from a remembered summary of Qortia's
routes. Reading `qortia/models.py` directly turned up real differences, all
now reflected in `qortia_backend.py`:

- `POST /v1/remember`'s body key is `memories`, not `items`, and each entry's
  `type` is a required, closed 6-value enum — there is no `"note"` default the
  way `GitMemoryBackend` has one.
- `RememberResponse` returns `{"ids": [...]}` only; content/type are not echoed
  back. `QortiaMemoryBackend.remember()` reconstructs `MemoryItem`s by
  zipping (`strict=True`) the returned ids against the original input in order.
- `POST /v1/recall`'s second parameter is not a free-form `filters` object — it's
  named fields (`scope`, `type`, `entities`, `as_of`, `lang`, `rerank`) with their
  own closed vocabularies. Unrecognised keys in the `filters` dict this backend's
  `recall()` receives are dropped, not forwarded.
- `POST /v1/forget`'s `id` is a UUID server-side and the response carries no
  boolean; success is read off HTTP status (200 vs. 404) instead.
- The `Authorization: Bearer` + `X-Agent-Id` auth shape was the one part that
  matched exactly.

## Consequences

**Good.** No new dependency; no in-process coupling to Qortia's own dependency
tree (asyncpg, FastAPI, spaCy, …); validation stays single-sourced at Qortia.

**Bad.** A wire-shape change on Qortia's side (e.g. renaming `memories` back to
something else, or changing the recall filter fields) breaks this client with no
compile-time signal — same risk profile as the front door's dependency on
`buzz-acp`'s wire behaviour (ADR-001). Mitigated the same way: read the real
source before trusting a summary, and keep the surface this backend depends on
(four routes, one auth shape) as small as possible.

**Neutral.** `QortiaMemoryBackend` has been exercised against mocked HTTP only
(see `tests/unit/test_qortia_backend.py`) — not yet against a live Qortia
instance end-to-end.
