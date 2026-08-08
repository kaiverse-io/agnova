"""Qortia memory backend — HTTP client for a standalone Qortia memory service.

Talks to Qortia over `POST/GET /v1/{context,recall,remember,forget}` only (see
docs/decisions/adr-003-qortia-memory-backend.md for the contract this was verified
against and why). Stdlib `http.client`, matching the precedent already set by
`checkpoint.py`'s `upload_bundle` — `coincurve` is this package's only *runtime*
dependency on purpose (see pyproject.toml), and a memory backend is not a reason to
widen that tree.

Never imports the `qortia` package in-process — `.importlinter` forbids it, and the
two services are meant to talk HTTP/OpenAPI only, same as every other boundary in
this stack (see AGENTS.md).
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import urlparse

from agnova.memory import MemoryItem

_TIMEOUT = 30.0

# Qortia's per-item request model (`qortia.models.MemoryItem`) is `extra="forbid"` —
# an unrecognised key 422s the *whole* /v1/remember batch. Only these are ever sent;
# notably `id` is dropped even if a caller supplies one, since Qortia mints its own.
_REMEMBER_FIELDS = frozenset(
    {"type", "content", "source_task_id", "metadata", "ttl_seconds", "lang"}
)

# /v1/recall has no generic "filters" object on the wire — these are its real,
# named fields (`qortia.models.RecallRequest`). Anything else in the `filters`
# dict this backend's `recall()` receives is silently dropped rather than sent.
_RECALL_FILTER_FIELDS = frozenset({"scope", "type", "entities", "as_of", "lang", "rerank"})

# Extra RecallResult fields folded into MemoryItem.metadata (never serialised as
# top-level MemoryItem attributes, since that dataclass only has id/content/type).
_RECALL_META_FIELDS = (
    "scope",
    "importance",
    "created_at",
    "entity_summary",
    "linked_via",
    "valid_from",
    "valid_until",
)


class QortiaError(RuntimeError):
    """A Qortia HTTP call failed: network error, bad response, or non-2xx status."""


class QortiaNotFoundError(QortiaError):
    """Qortia returned 404 — the id does not exist. `forget()` turns this into False."""


class QortiaMemoryBackend:
    """HTTP client for a standalone Qortia memory service.

    Matches the shape of `GitMemoryBackend` (`context`/`recall`/`remember`/`forget`,
    see `agnova.memory.MemoryBackend`) so `mcp_server._backend()` can select either
    backend behind the same protocol. Holds no filesystem state — everything lives
    on the Qortia side, addressed by tenant (`api_key`) and agent (`agent_id`).
    """

    def __init__(
        self, base_url: str, api_key: str, agent_id: str, *, timeout: float = _TIMEOUT
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"QORTIA_URL is not a valid http(s) URL: {base_url!r}")
        # Pulled out as their own typed fields rather than keeping the ParseResult:
        # `parsed.hostname` is `str | None` in general, and the guard above only
        # narrows it to `str` within this method — not for `self._parsed.hostname`
        # read back later from `_request`.
        self._scheme = parsed.scheme
        self._hostname: str = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._api_key = api_key
        self._agent_id = agent_id
        self._timeout = timeout

    # ── wire ─────────────────────────────────────────────────────────────

    def _path(self, endpoint: str) -> str:
        return f"{self._base_path}{endpoint}" if self._base_path else endpoint

    def _request(
        self, method: str, endpoint: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Agent-Id": self._agent_id,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))

        path = self._path(endpoint)
        try:
            conn: http.client.HTTPConnection
            if self._scheme == "https":
                conn = http.client.HTTPSConnection(
                    self._hostname, self._port or 443, timeout=self._timeout
                )
            else:
                conn = http.client.HTTPConnection(
                    self._hostname, self._port or 80, timeout=self._timeout
                )
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            conn.close()
        except OSError as exc:
            raise QortiaError(f"qortia: {method} {path} failed — {exc}") from exc

        if status == 404:
            raise QortiaNotFoundError(f"qortia: {method} {path} — not found")
        if not 200 <= status < 300:
            raise QortiaError(f"qortia: {method} {path} — HTTP {status}: {_error_detail(raw)}")
        if not raw:
            return {}
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QortiaError(f"qortia: {method} {path} — invalid JSON response") from exc
        return data

    # ── MemoryBackend protocol ──────────────────────────────────────────────

    def context(self, budget: int | None = None) -> str:
        data = self._request("GET", "/v1/context", None)
        parts: list[str] = []
        for key in ("org_chart", "processes", "handoffs"):
            for entry in data.get(key) or []:
                parts.append(_render_entry(entry))
        weekly = data.get("weekly_summary")
        if weekly:
            parts.append(_render_entry(weekly))
        memories = data.get("memories") or {}
        for key in ("decisions", "mental_models", "lessons"):
            for entry in memories.get(key) or []:
                parts.append(_render_entry(entry))
        text = "\n\n".join(part for part in parts if part).strip()
        if budget is not None and budget > 0 and len(text) > budget:
            return text[:budget]
        return text

    def recall(self, query: str, filters: dict[str, Any] | None = None) -> list[MemoryItem]:
        if not query.strip():
            return []
        payload: dict[str, Any] = {"query": query}
        for key, value in (filters or {}).items():
            if key in _RECALL_FILTER_FIELDS and value is not None:
                payload[key] = value
        data = self._request("POST", "/v1/recall", payload)
        items: list[MemoryItem] = []
        for result in data.get("results") or []:
            meta = {key: result[key] for key in _RECALL_META_FIELDS if result.get(key) is not None}
            items.append(
                MemoryItem(
                    id=str(result["id"]),
                    content=str(result.get("content", "")),
                    type=str(result.get("type", "")),
                    metadata=meta,
                )
            )
        return items

    def remember(self, items: list[dict[str, Any]]) -> list[MemoryItem]:
        if not items:
            return []
        memories = [
            {
                key: value
                for key, value in raw.items()
                if key in _REMEMBER_FIELDS and value is not None
            }
            for raw in items
        ]
        data = self._request("POST", "/v1/remember", {"memories": memories})
        ids = data.get("ids") or []
        stored: list[MemoryItem] = []
        for raw, memory_id in zip(items, ids, strict=True):
            raw_meta = raw.get("metadata")
            meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            stored.append(
                MemoryItem(
                    id=str(memory_id),
                    content=str(raw.get("content", "")),
                    type=str(raw.get("type", "")),
                    metadata=meta,
                )
            )
        return stored

    def forget(self, memory_id: str) -> bool:
        try:
            self._request("POST", "/v1/forget", {"id": memory_id})
        except QortiaNotFoundError:
            return False
        return True


def _render_entry(entry: dict[str, Any]) -> str:
    title = entry.get("title")
    content = str(entry.get("content", "")).strip()
    return f"## {title}\n{content}" if title else content


def _error_detail(raw: bytes) -> str:
    if not raw:
        return "(empty body)"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200].decode("utf-8", errors="replace")
    if isinstance(parsed, dict) and "detail" in parsed:
        return str(parsed["detail"])
    return str(parsed)[:200]
