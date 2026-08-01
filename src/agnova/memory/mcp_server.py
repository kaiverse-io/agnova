"""Minimal MCP stdio server for agnova-memory — stdlib only (no mcp SDK dep).

Implements enough of the MCP JSON-RPC surface for buzz-acp / Cursor:
initialize, tools/list, tools/call. Backends selected by AGENT_MEMORY_BACKEND.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from agnova.memory.git_backend import GitMemoryBackend

TOOLS = [
    {
        "name": "context",
        "description": "Bootstrap memory bundle for the current session",
        "inputSchema": {
            "type": "object",
            "properties": {"budget": {"type": "integer", "minimum": 1}},
        },
    },
    {
        "name": "recall",
        "description": "Search memories",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "filters": {"type": "object"},
            },
        },
    },
    {
        "name": "remember",
        "description": "Persist agent-chosen memories",
        "inputSchema": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["content"],
                        "properties": {
                            "type": {"type": "string"},
                            "content": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                    },
                }
            },
        },
    },
    {
        "name": "forget",
        "description": "Delete a memory by id",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    },
]


def _backend() -> GitMemoryBackend:
    home = Path(os.environ.get("BUZZ_AGENT_HOME") or os.environ.get("AGNOVA_HOME") or ".").resolve()
    name = (os.environ.get("AGENT_MEMORY_BACKEND") or "git").strip().lower()
    if name != "git":
        # qortia backend lands after G1; fail clearly rather than pretend.
        raise SystemExit(
            f"AGENT_MEMORY_BACKEND={name!r} is not implemented yet — use git (default)"
        )
    return GitMemoryBackend(home)


def _result_text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _call_tool(backend: GitMemoryBackend, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "context":
        budget = arguments.get("budget")
        return _result_text({"context": backend.context(int(budget) if budget else None)})
    if name == "recall":
        hits = backend.recall(str(arguments.get("query", "")), arguments.get("filters"))
        return _result_text([{"id": h.id, "type": h.type, "content": h.content} for h in hits])
    if name == "remember":
        items = arguments.get("items") or []
        if not isinstance(items, list):
            raise ValueError("items must be an array")
        stored = backend.remember(items)
        return _result_text([{"id": h.id, "type": h.type, "content": h.content} for h in stored])
    if name == "forget":
        ok = backend.forget(str(arguments.get("id", "")))
        return _result_text({"forgotten": ok})
    raise ValueError(f"unknown tool: {name}")


def _handle(backend: GitMemoryBackend, message: dict[str, Any]) -> dict[str, Any] | None:
    mid = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agnova-memory", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            result = _call_tool(backend, str(params.get("name")), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # tool errors are returned, not process-fatal
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            }
    if mid is not None:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main() -> None:
    backend = _backend()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _handle(backend, message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
