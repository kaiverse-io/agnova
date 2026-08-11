"""Memory backends for agnova-memory MCP — HTTP/stdio only; no in-process engine imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# The cross-backend contract: Qortia's own closed enum (qortia/src/qortia/
# models.py MemoryItem.type — extra="forbid", so an unrecognised value 422s
# the whole /v1/remember batch). Every MemoryBackend implementation must
# only ever write one of these — see tests/contract/test_backend_conformance.py,
# which every implementation is required to pass.
MEMORY_TYPES = frozenset(
    {"episodic", "experiential", "mental_model", "decision", "lesson", "short_term"}
)

# episodic (importance 0.3, Qortia's lowest prior) is the closest fit for "an
# agent stored something without saying what kind" — not "note", which isn't
# in MEMORY_TYPES and previously 422'd the entire batch on the qortia backend
# with no indication why. Qortia additionally requires >=5 words of content
# and rejects/requires `ttl_seconds` depending on this value — see the
# `remember` tool's schema in mcp_server.py for where that's surfaced to the
# calling agent.
DEFAULT_MEMORY_TYPE = "episodic"

# Qortia's OutcomeRequest.outcome (qortia/src/qortia/models.py) — the three
# values `/v1/outcome` accepts. Mirrored here so mcp_server.py's `outcome`
# tool schema and any caller validating client-side share one source of
# truth, the same pattern MEMORY_TYPES already sets for `remember`.
OUTCOME_VALUES = frozenset({"SUCCESS", "MINOR_FAILURE", "CRITICAL_FAILURE"})


@dataclass
class MemoryItem:
    id: str
    content: str
    type: str = DEFAULT_MEMORY_TYPE
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryBackend(Protocol):
    def context(self, budget: int | None = None) -> str: ...

    def recall(self, query: str, filters: dict[str, Any] | None = None) -> list[MemoryItem]: ...

    def get(self, memory_id: str, *, max_chars: int | None = None) -> str: ...

    def remember(self, items: list[dict[str, Any]]) -> list[MemoryItem]: ...

    def forget(self, memory_id: str) -> bool: ...

    def reflect(self) -> dict[str, int]: ...

    def outcome(self, result: str) -> bool: ...
