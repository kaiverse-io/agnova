"""Git/filesystem memory backend — durable via checkpoint, searchable via grep."""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from agnova.memory import MemoryItem

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class GitMemoryBackend:
    def __init__(self, home: Path) -> None:
        self.home = home.resolve()
        self.memory_dir = self.home / "memory"
        self.entries_dir = self.memory_dir / "entries"
        self.memory_md = self.home / "MEMORY.md"

    def _safe_id(self, memory_id: str) -> str:
        if not _ID_RE.match(memory_id):
            raise ValueError(f"invalid memory id: {memory_id!r}")
        return memory_id

    def context(self, budget: int | None = None) -> str:
        parts: list[str] = []
        if self.memory_md.is_file():
            parts.append(self.memory_md.read_text(encoding="utf-8", errors="replace"))
        if self.memory_dir.is_dir():
            days = sorted(self.memory_dir.glob("????-??-??.md"), reverse=True)[:7]
            for day in reversed(days):
                parts.append(f"## {day.stem}\n{day.read_text(encoding='utf-8', errors='replace')}")
        text = "\n\n".join(parts).strip()
        if budget is not None and budget > 0 and len(text) > budget:
            return text[:budget]
        return text

    def recall(self, query: str, filters: dict[str, Any] | None = None) -> list[MemoryItem]:
        del filters  # reserved for qortia backend
        needle = query.lower().strip()
        if not needle:
            return []
        hits: list[MemoryItem] = []
        paths: list[Path] = []
        if self.memory_md.is_file():
            paths.append(self.memory_md)
        if self.memory_dir.is_dir():
            paths.extend(sorted(self.memory_dir.glob("*.md")))
            if self.entries_dir.is_dir():
                paths.extend(sorted(self.entries_dir.glob("*.md")))
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle not in text.lower():
                continue
            mid = path.stem if path.parent == self.entries_dir else f"file:{path.name}"
            hits.append(MemoryItem(id=mid, content=text.strip(), type="note"))
            if len(hits) >= 20:
                break
        return hits

    def remember(self, items: list[dict[str, Any]]) -> list[MemoryItem]:
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        today = self.memory_dir / f"{date.today().isoformat()}.md"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        stored: list[MemoryItem] = []
        daily_lines: list[str] = []
        for raw in items:
            content = str(raw.get("content", "")).strip()
            if not content:
                continue
            mid = self._safe_id(str(raw.get("id") or uuid.uuid4().hex[:12]))
            mtype = str(raw.get("type") or "note")
            raw_meta = raw.get("metadata")
            meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
            body = f"---\nid: {mid}\ntype: {mtype}\n---\n\n{content}\n"
            (self.entries_dir / f"{mid}.md").write_text(body, encoding="utf-8")
            daily_lines.append(f"- [{mid}] {content}")
            stored.append(MemoryItem(id=mid, content=content, type=mtype, metadata=meta))
        if daily_lines:
            prev = today.read_text(encoding="utf-8") if today.is_file() else f"# {today.stem}\n\n"
            today.write_text(prev.rstrip() + "\n" + "\n".join(daily_lines) + "\n", encoding="utf-8")
        return stored

    def forget(self, memory_id: str) -> bool:
        mid = self._safe_id(memory_id)
        path = self.entries_dir / f"{mid}.md"
        if not path.is_file():
            return False
        path.unlink()
        return True
