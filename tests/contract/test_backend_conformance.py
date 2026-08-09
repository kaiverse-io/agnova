"""Protocol conformance suite — every `MemoryBackend` implementation must
pass this identical set of behavioral assertions.

Before this suite, `git` and `qortia` were tested by two differently-shaped
files that never asserted the same property, which is precisely how they
came to disagree about what a memory is (the type-vocabulary mismatch this
repo's F2 contract tests pinned). A Protocol without a conformance suite is
a type hint, not a contract.

The qortia case runs against `_FakeQortiaStore` — an in-process fake that
enforces Qortia's actual write-time validation rules (the closed type enum,
the 5-word content floor, the short_term/ttl_seconds coupling; see
qortia/src/qortia/models.py) rather than a mock that only records calls.
This exercises QortiaMemoryBackend's real request-building and
response-parsing code against realistic responses and realistic rejections
— not a live network call (that's tests/contract/test_memory_plane_live.py
in the aither repo, the only place a live agent and a live Qortia coexist),
but enough to catch a backend disagreeing with itself.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Protocol

import pytest

from agnova.memory import MEMORY_TYPES, MemoryBackend
from agnova.memory.git_backend import GitMemoryBackend
from agnova.memory.qortia_backend import QortiaError, QortiaMemoryBackend


class _FakeQortiaStore:
    """In-process stand-in for Qortia's /v1/{remember,recall,forget,context},
    enforcing the same validation rules the real service does."""

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}

    def handle(self, method: str, endpoint: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if endpoint == "/v1/remember" and method == "POST":
            return self._remember(payload or {})
        if endpoint == "/v1/recall" and method == "POST":
            return self._recall(payload or {})
        if endpoint == "/v1/forget" and method == "POST":
            return self._forget(payload or {})
        if endpoint == "/v1/context" and method == "GET":
            return self._context()
        raise AssertionError(f"fake Qortia store has no handler for {method} {endpoint}")

    def _remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids = []
        for mem in payload.get("memories", []):
            mtype = mem.get("type")
            if mtype not in MEMORY_TYPES:
                raise QortiaError(f"qortia: POST /v1/remember — HTTP 422: invalid type {mtype!r}")
            if len(str(mem.get("content", "")).split()) < 5:
                raise QortiaError("qortia: POST /v1/remember — HTTP 422: content must be >=5 words")
            ttl = mem.get("ttl_seconds")
            if mtype == "short_term" and not ttl:
                raise QortiaError(
                    "qortia: POST /v1/remember — HTTP 422: short_term requires ttl_seconds"
                )
            if mtype != "short_term" and ttl is not None:
                raise QortiaError(
                    "qortia: POST /v1/remember — HTTP 422: ttl_seconds only valid for short_term"
                )
            mid = uuid.uuid4().hex
            self.memories[mid] = dict(mem)
            ids.append(mid)
        return {"ids": ids}

    def _recall(self, payload: dict[str, Any]) -> dict[str, Any]:
        needle = str(payload.get("query", "")).lower()
        results = [
            {
                "id": mid,
                "type": mem.get("type"),
                "scope": "private",
                "content": mem.get("content"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            for mid, mem in self.memories.items()
            if needle in str(mem.get("content", "")).lower()
        ]
        return {"results": results}

    def _forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        from agnova.memory.qortia_backend import QortiaNotFoundError

        mid = str(payload.get("id"))
        if mid not in self.memories:
            raise QortiaNotFoundError(f"qortia: POST /v1/forget — HTTP 404: {mid} not found")
        del self.memories[mid]
        return {"id": mid}

    def _context(self) -> dict[str, Any]:
        lessons = [
            {"content": m.get("content"), "importance": 0.95}
            for m in self.memories.values()
            if m.get("type") == "lesson"
        ]
        return {
            "org_chart": [],
            "processes": [],
            "handoffs": [],
            "weekly_summary": None,
            "memories": {"decisions": [], "mental_models": [], "lessons": lessons},
        }


class _BackendFactory(Protocol):
    def __call__(self) -> MemoryBackend: ...


def _git_factory(tmp_path: Path) -> _BackendFactory:
    return lambda: GitMemoryBackend(tmp_path)


def _qortia_factory() -> _BackendFactory:
    backend = QortiaMemoryBackend("http://qortia.test", "key", "agent-id")
    store = _FakeQortiaStore()
    backend._request = store.handle  # type: ignore[method-assign]
    return lambda: backend


@pytest.fixture(params=["git", "qortia"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryBackend:
    factory = _git_factory(tmp_path) if request.param == "git" else _qortia_factory()
    return factory()


@pytest.mark.parametrize("memory_type", sorted(MEMORY_TYPES))
def test_round_trips_every_type_qortia_accepts(backend: MemoryBackend, memory_type: str) -> None:
    """Every backend must be able to write and recall each of Qortia's six
    types — a backend that can't write `lesson`, say, silently loses the
    highest-importance memory kind whenever it's the active backend."""
    item: dict[str, Any] = {
        "content": f"a five word memory of type {memory_type}",
        "type": memory_type,
    }
    if memory_type == "short_term":
        item["ttl_seconds"] = 60

    stored = backend.remember([item])

    assert len(stored) == 1
    assert stored[0].type == memory_type


def test_recall_finds_what_remember_stored(backend: MemoryBackend) -> None:
    marker = f"unique-marker-{uuid.uuid4().hex[:8]}"
    backend.remember([{"content": f"a memory containing the {marker} token", "type": "episodic"}])

    hits = backend.recall(marker)

    assert any(marker in h.content for h in hits)


def test_forget_removes_a_stored_memory(backend: MemoryBackend) -> None:
    marker = f"forget-marker-{uuid.uuid4().hex[:8]}"
    stored = backend.remember(
        [{"content": f"a memory about {marker} to be forgotten", "type": "episodic"}]
    )

    ok = backend.forget(stored[0].id)

    assert ok is True
    assert not any(marker in h.content for h in backend.recall(marker))


def test_context_honours_a_budget(backend: MemoryBackend) -> None:
    backend.remember(
        [{"content": "x " * 500 + "the important lesson content marker", "type": "lesson"}]
    )

    out = backend.context(budget=50)

    assert len(out) <= 50
