"""Git/filesystem memory backend — durable via checkpoint, searchable via grep."""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from agnova.memory import DEFAULT_MEMORY_TYPE, MemoryItem

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_SNIPPET_WINDOW = 120  # chars of context kept on each side of a match
_SNIPPET_MAX_MATCHES = (
    3  # windows joined per file, beyond which "… N more matches" replaces the rest
)
_RECALL_MAX_HITS = 20
_RECALL_MAX_TOTAL_CHARS = 4000  # hard cap across all returned snippets combined


def _snippet(text: str, terms: list[str], *, window: int = _SNIPPET_WINDOW) -> tuple[str, int]:
    """Windows of context around each match, joined — never the whole file.

    `terms` are ANDed and word-boundary matched (case-insensitive): every
    term must appear as a whole word somewhere in the text, or the file
    doesn't qualify at all — the old behaviour treated the *entire* query as
    one literal substring (`recall("rate limiting AuthService")` matched
    only if that exact phrase appeared verbatim, so ordinary multi-word
    queries returned nothing unless they happened to quote the source
    exactly), and matched raw substrings with no word boundary (a query for
    "cat" matched inside "category"), which could let an unrelated file
    outrank the real answer purely by containing the query as a substring
    of a longer, unrelated word many times.

    Returns (snippet_text, match_count) — match_count sums occurrences
    across all terms and is the primary ranking key in recall().
    """
    if not terms:
        return "", 0

    matches: list[tuple[int, int]] = []  # (position, matched length)
    for term in terms:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        term_matches = [(m.start(), len(term)) for m in pattern.finditer(text)]
        if not term_matches:
            return "", 0  # AND semantics: every term must be present
        matches.extend(term_matches)
    matches.sort(key=lambda m: m[0])

    windows: list[str] = []
    for idx, length in matches[:_SNIPPET_MAX_MATCHES]:
        lo = max(0, idx - window)
        hi = min(len(text), idx + length + window)
        piece = text[lo:hi].strip()
        windows.append(f"…{piece}…" if lo > 0 or hi < len(text) else piece)
    remaining = len(matches) - len(windows)
    joined = "\n[...]\n".join(windows)
    if remaining > 0:
        joined += f"\n[... {remaining} more match{'es' if remaining != 1 else ''} in this file ...]"
    return joined, len(matches)


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
        """Snippet windows around each match, ranked by match count then
        recency — never a whole file. The old behaviour returned
        `content=text.strip()` on any hit, so recall against a mature
        MEMORY.md returned all of MEMORY.md: a retrieval step that could
        cost more context than skipping retrieval entirely.

        The query is split into words and ANDed, word-boundary matched
        (see `_snippet`) — not treated as one literal phrase. A query for
        "rate limiting AuthService" now matches a file mentioning all three
        words in any order/position, not only a file containing that exact
        substring verbatim (evals/run_recall_eval.py caught this: every
        multi-word query in the eval dataset returned zero results under
        the old whole-phrase-substring behaviour)."""
        del filters  # reserved for qortia backend
        terms = re.findall(r"\w+", query.lower())
        if not terms:
            return []
        paths: list[Path] = []
        if self.memory_md.is_file():
            paths.append(self.memory_md)
        if self.memory_dir.is_dir():
            paths.extend(self.memory_dir.glob("*.md"))
            if self.entries_dir.is_dir():
                paths.extend(self.entries_dir.glob("*.md"))

        scored: list[tuple[int, float, str, str]] = []  # (match_count, mtime, id, snippet)
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            snippet, count = _snippet(text, terms)
            if count == 0:
                continue
            mid = path.stem if path.parent == self.entries_dir else f"file:{path.name}"
            scored.append((count, mtime, mid, snippet))

        # Most matches first; among ties, most recently modified first —
        # recency as a proxy for relevance when match count doesn't decide.
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

        # Checked *before* adding, not after: checking post-hoc lets the one
        # snippet that crosses the line still get included in full, so the
        # combined total can exceed the "hard cap" the constant promises
        # (evals/run_recall_eval.py's geh-010 caught this: 4022 chars back
        # when the check ran after appending).
        hits: list[MemoryItem] = []
        total_chars = 0
        for _count, _mtime, mid, snippet in scored[:_RECALL_MAX_HITS]:
            if total_chars + len(snippet) > _RECALL_MAX_TOTAL_CHARS:
                break
            hits.append(MemoryItem(id=mid, content=snippet, type=DEFAULT_MEMORY_TYPE))
            total_chars += len(snippet)
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
            mtype = str(raw.get("type") or DEFAULT_MEMORY_TYPE)
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
        """Remove the entry file *and* its pointer line from whichever daily
        log remember() wrote it into — recall() searches both, so leaving
        the pointer behind makes a forgotten memory still recallable via
        its daily-log copy of the content."""
        mid = self._safe_id(memory_id)
        path = self.entries_dir / f"{mid}.md"
        found = path.is_file()
        if found:
            path.unlink()

        if self.memory_dir.is_dir():
            marker = f"- [{mid}] "
            for daily in self.memory_dir.glob("*.md"):
                try:
                    text = daily.read_text(encoding="utf-8")
                except OSError:
                    continue
                lines = text.splitlines(keepends=True)
                kept = [ln for ln in lines if not ln.startswith(marker)]
                if len(kept) != len(lines):
                    found = True
                    daily.write_text("".join(kept), encoding="utf-8")

        return found

    def reflect(self) -> dict[str, int]:
        """No automated consolidation on this backend — promoting a daily
        log entry into MEMORY.md is still the agent's own judgment call,
        per AGENTS.md's "promoted from memory/ once something proves
        durable." Present on the Protocol (and the reflect MCP tool) so a
        naive caller doesn't crash switching backends; it just has nothing
        to report."""
        return {"memories_written": 0, "reflection_counter": 0}
