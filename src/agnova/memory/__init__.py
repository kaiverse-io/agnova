"""Memory backends for agnova-memory MCP — HTTP/stdio only; no in-process engine imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class MemoryItem:
    id: str
    content: str
    type: str = "note"
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryBackend(Protocol):
    def context(self, budget: int | None = None) -> str: ...

    def recall(self, query: str, filters: dict[str, Any] | None = None) -> list[MemoryItem]: ...

    def remember(self, items: list[dict[str, Any]]) -> list[MemoryItem]: ...

    def forget(self, memory_id: str) -> bool: ...
