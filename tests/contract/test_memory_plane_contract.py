"""Contract tests for agnova's memory plane — properties of the *composition*,
not of any one module in isolation.

`just ci-test` is 157 green at 94% coverage with `qortia_backend.py` at 100%,
while an agent launched by `agnova up` has no memory tools at all — every unit
test constructs a backend directly and calls methods on it, so nothing here
was ever exercised. These tests assert what the review at docs/decisions/
adr-003-qortia-memory-backend.md and the memory-plane proposal found by
reading source and, for F1/F2, by running a live agent against a live Qortia.

Each is `xfail(strict=True)`: red is the known, tracked state. A fix that
lands without removing the marker turns the suite red again — that is the
point. See docs/decisions/adrs/ for the fix tracking these findings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agnova.config import AgentConfig
from agnova.memory import MemoryItem
from agnova.memory.git_backend import GitMemoryBackend
from agnova.memory.mcp_server import TOOLS
from agnova.memory.qortia_backend import QortiaMemoryBackend
from agnova.scaffold import init

# Qortia's closed enum and its importance priors, transcribed from
# qortia/src/qortia/models.py:17-37. Duplicated deliberately: this is the
# cross-repo contract, and a test that imports it cannot catch drift.
QORTIA_TYPES = {
    "episodic",
    "experiential",
    "mental_model",
    "decision",
    "lesson",
    "short_term",
}
QORTIA_IMPORTANCE = {
    "lesson": 0.95,
    "decision": 0.9,
    "mental_model": 0.8,
    "experiential": 0.6,
    "episodic": 0.3,
    "short_term": 0.1,
}


def _config(home: Path, **overrides: Any) -> AgentConfig:
    base: dict[str, Any] = {
        "name": "scout",
        "label": "Scout",
        "home": home,
        "relay_url": "wss://relay.example",
        "secret_key": "nsec-not-a-real-key",
        "owner_pubkey": "abc123",
        "frontdoor_port": 8444,
        "agent_command": "claude-agent-acp",
        "respond_to": "owner-only",
        "auth_tag": None,
        "transport": "direct",
    }
    base.update(overrides)
    return AgentConfig(**base)


# ── F1 · the memory tool surface is never registered ────────────────────────
#
# Verified live, not just by reading source: a real container launch of this
# harness (docker inspect on a running agent image built from this repo) has
# no BUZZ_ACP_MCP_COMMAND in its environment. A launcher that writes a
# `.mcp.json` into the agent home does not help either — `.mcp.json` is a
# Claude Code CLI convention, and the harness here is buzz-acp ->
# `@agentclientprotocol/claude-agent-acp`, which sources MCP servers
# exclusively from `params.mcpServers` on the ACP `session/new` request
# (confirmed by grepping the adapter's compiled dist/ for `.mcp.json` — zero
# matches; mcpServers is read only from ACP session params, built by
# buzz-acp from --mcp-command/BUZZ_ACP_MCP_COMMAND). Any `.mcp.json` a
# launcher writes is dead code in this pipeline.


def test_harness_env_registers_the_memory_mcp_server(tmp_path: Path) -> None:
    """`agnova up` must hand buzz-acp a command that serves agnova-memory.

    Without this the agent has no context/recall/remember/forget at all, and
    every backend behind AGENT_MEMORY_BACKEND is unreachable code.

    Fixed: harness_env() now sets BUZZ_ACP_MCP_COMMAND (setdefault, so an
    operator's own .env can still claim the single MCP-server slot for
    something else — see the comment in AgentConfig.harness_env()).
    """
    env = _config(tmp_path).harness_env()

    assert env.get("BUZZ_ACP_MCP_COMMAND"), (
        "harness_env() sets AGENT_MEMORY_BACKEND but never BUZZ_ACP_MCP_COMMAND, "
        "so buzz-acp registers no MCP servers and the agent gets no memory tools"
    )
    assert "agnova-memory" in env["BUZZ_ACP_MCP_COMMAND"]


def test_qortia_backend_selection_reaches_the_harness(tmp_path: Path) -> None:
    """Choosing the qortia backend must actually change what the agent can do.

    Fixed alongside the harness_env() change above: AGENT_MEMORY_BACKEND is
    now read by agnova-memory, which is now actually launched.
    """
    env = _config(tmp_path, memory_backend="qortia").harness_env()

    assert env["AGENT_MEMORY_BACKEND"] == "qortia"
    assert env.get("BUZZ_ACP_MCP_COMMAND"), (
        "AGENT_MEMORY_BACKEND=qortia is inert: no MCP command means no process ever reads it"
    )


# ── F2 · the two backends disagree about what a memory is ───────────────────
#
# Verified live against a real Qortia (docker network qortia_net, fresh
# tenant/agent/key via /v1/admin/*): a remember() call with no `type` key
# does not get an "invalid value 'note'" 422 — Qortia never receives a `type`
# key at all (QortiaMemoryBackend.remember() only forwards keys present in
# the raw dict; it never applies MemoryItem's "note" default itself) and
# returns "Field required". The MCP tool schema still advertises `type` as
# optional (only "content" is in `required`) — that is a lie for the qortia
# backend, where omitting it 422s the whole batch.


def test_default_memory_type_is_valid_on_every_backend() -> None:
    """A remember() that succeeds on `git` must not 422 the batch on `qortia`.

    Fixed: MemoryItem.type now defaults to DEFAULT_MEMORY_TYPE = "episodic"
    (Qortia's lowest-importance-prior type — the closest fit for "an agent
    stored something without saying what kind"), not "note".
    """
    default_type = MemoryItem(id="x", content="y").type

    assert default_type in QORTIA_TYPES, (
        f"MemoryBackend's default type {default_type!r} is not in Qortia's closed "
        f"enum {sorted(QORTIA_TYPES)} — the seam's own default is invalid on half "
        f"its implementations"
    )


def test_git_backend_writes_types_qortia_accepts(tmp_path: Path) -> None:
    """The default backend must not mint a vocabulary the other one rejects.

    Fixed: git_backend.py now imports DEFAULT_MEMORY_TYPE from agnova.memory
    instead of hardcoding its own "note" fallback.
    """
    stored = GitMemoryBackend(tmp_path).remember(
        [{"content": "the relay rejects loopback-signed NIP-98 tokens on purpose"}]
    )

    assert stored[0].type in QORTIA_TYPES, (
        f"GitMemoryBackend stamped type={stored[0].type!r}; switching "
        f"AGENT_MEMORY_BACKEND to qortia turns every such memory into a 422"
    )


def test_mcp_schema_publishes_the_memory_type_enum() -> None:
    """The agent must be able to read the vocabulary, not guess it.

    A tool whose schema says `{"type": "string"}` for a closed six-value enum
    is a tool the model will call wrongly and then stop calling. Fixed: the
    `remember` tool's item schema now publishes MEMORY_TYPES as a JSON Schema
    `enum`.
    """
    remember = next(t for t in TOOLS if t["name"] == "remember")
    item = remember["inputSchema"]["properties"]["items"]["items"]
    type_schema = item["properties"]["type"]

    assert "enum" in type_schema, (
        "remember's `type` is advertised as a free-form string; the agent has no "
        "way to discover episodic/experiential/mental_model/decision/lesson/short_term"
    )
    assert set(type_schema["enum"]) == QORTIA_TYPES


def test_mcp_schema_documents_qortias_write_constraints() -> None:
    """Qortia rejects <5-word content and requires ttl_seconds for short_term.

    Neither rule appears anywhere the model can see it, so both surface as
    opaque tool errors after the fact.
    """
    remember = next(t for t in TOOLS if t["name"] == "remember")
    described = (remember["description"] + str(remember["inputSchema"])).lower()

    assert "ttl_seconds" in described, "the short_term/ttl_seconds rule is undiscoverable"
    assert "5" in described or "five" in described, "the 5-word content floor is undiscoverable"


# ── F3 · context truncation inverts importance ──────────────────────────────


def _bundle() -> dict[str, Any]:
    """A context bundle where every section is bigger than a small budget.

    memories.* entries carry `importance`, matching what a P2-fixed
    /v1/context actually returns for every type including decisions
    (previously decisions shipped without importance at all — see
    qortia/tests/contract's own test_every_context_entry_carries_importance).
    """
    filler = "x " * 400
    return {
        "org_chart": [{"title": "Org", "content": f"org chart {filler}"}],
        "processes": [{"title": "Process", "content": f"process {filler}"}],
        "handoffs": [{"title": "Handoff", "content": f"handoff {filler}"}],
        "weekly_summary": {"title": "Week", "content": f"weekly {filler}"},
        "memories": {
            "decisions": [
                {
                    "content": f"DECISION-MARKER {filler}",
                    "importance": QORTIA_IMPORTANCE["decision"],
                }
            ],
            "mental_models": [
                {
                    "content": f"MODEL-MARKER {filler}",
                    "importance": QORTIA_IMPORTANCE["mental_model"],
                }
            ],
            "lessons": [
                {"content": f"LESSON-MARKER {filler}", "importance": QORTIA_IMPORTANCE["lesson"]}
            ],
        },
    }


def test_context_keeps_the_most_important_memories_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under budget pressure, drop 0.3-importance episodics before 0.95 lessons.

    Fixed, and depended on the qortia-side P2 fix (decisions now carry
    importance too): context() now pools org_chart/processes/handoffs/
    weekly_summary and memories.* into one importance-ranked list —
    org-level content gets a fixed 0.5 prior (Qortia doesn't score it) so
    it can still be outranked by a 0.95 lesson under pressure, rather than
    being kept unconditionally ahead of every typed memory.
    """
    backend = QortiaMemoryBackend("http://qortia.test", "key", "agent-id")
    monkeypatch.setattr(backend, "_request", lambda *a, **k: _bundle())

    out = backend.context(budget=1200)

    assert "LESSON-MARKER" in out, (
        f"lessons (importance {QORTIA_IMPORTANCE['lesson']}) were truncated away "
        f"while org-chart boilerplate survived — render order ignores importance"
    )


def test_context_does_not_cut_a_record_mid_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping whole records beats slicing bytes: a half-record is a hallucination risk.

    Fixed: context() now drops whole entries and appends an "omitted" marker
    naming the count, instead of a raw text[:budget] slice.
    """
    backend = QortiaMemoryBackend("http://qortia.test", "key", "agent-id")
    monkeypatch.setattr(backend, "_request", lambda *a, **k: _bundle())

    out = backend.context(budget=1200)

    assert out.endswith(("\n", ".", "]", ")")) or "omitted" in out.lower(), (
        "context() ends mid-token with no marker saying anything was dropped"
    )


# ── F4 · git recall returns whole documents ─────────────────────────────────


def test_recall_returns_snippets_not_whole_documents(tmp_path: Path) -> None:
    """A retrieval step must cost less context than skipping retrieval.

    Fixed: recall() now returns windows of context around each match
    (_snippet()), not `content=text.strip()` on the whole file.
    """
    memory_md = tmp_path / "MEMORY.md"
    filler = "\n".join(f"- unrelated note number {i} about routine operations" for i in range(1500))
    memory_md.write_text(f"# MEMORY\n\n{filler}\n\n- the front door re-signs NIP-42 AUTH\n")

    hits = GitMemoryBackend(tmp_path).recall("re-signs NIP-42")

    assert hits, "expected a hit"
    returned = sum(len(h.content) for h in hits)
    assert returned < len(memory_md.read_text()) // 4, (
        f"recall returned {returned} chars for a {len(memory_md.read_text())}-char file — "
        f"it returns whole documents, with no snippet window, ranking or byte cap"
    )


def test_recall_does_not_return_the_daily_logs_copy_of_an_entry(tmp_path: Path) -> None:
    """remember() writes twice; recall() must not return both copies.

    Each memory lands in `entries/<id>.md` *and* as a `- [<id>] …` line in the
    day's log. The log accumulates every memory stored that day (so it wins on
    match count) and is rewritten on each remember() (so it wins on mtime),
    which put the aggregate duplicate above the entry it duplicates on both
    ranking keys — spending the caller's budget on the same content twice.
    evals/run_recall_eval.py scored this at MRR 0.481; suppressing the
    duplicate took it to 0.833.
    """
    backend = GitMemoryBackend(tmp_path)
    backend.remember([{"id": "mem-alpha", "content": "The canary check runs before the rollout."}])
    backend.remember([{"id": "mem-beta", "content": "Unrelated note on invoice reconciliation."}])

    hits = backend.recall("canary check")
    ids = [h.id for h in hits]

    assert ids, "expected a hit"
    assert ids[0] == "mem-alpha", f"entry should rank first, got {ids}"
    assert not [i for i in ids if i.startswith("file:")], (
        f"the daily log's duplicate of an entry is still being returned: {ids}"
    )


def test_recall_still_finds_daily_log_content_with_no_entry_file(tmp_path: Path) -> None:
    """Suppressing duplicates must not blind recall to log-only notes.

    Only lines pointing at an entry recall() already scans are dropped — a
    daily log written by hand (never through remember()) has no `- [<id>] `
    pointer backing it and must still match.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "2026-08-11.md").write_text(
        "# 2026-08-11\n\n- freeform note: the canary check flapped twice overnight\n",
        encoding="utf-8",
    )

    hits = GitMemoryBackend(tmp_path).recall("canary check")

    assert [h.id for h in hits] == ["file:2026-08-11.md"], (
        f"log-only content stopped matching: {[h.id for h in hits]}"
    )


def test_get_returns_the_full_entry_recall_only_snippets(tmp_path: Path) -> None:
    """get(id) with the id recall() handed back returns the whole stored
    entry, not the bounded window recall() itself returns."""
    backend = GitMemoryBackend(tmp_path)
    long_content = "The canary check runs first. " + "Unrelated filler sentence. " * 50
    backend.remember([{"id": "mem-1", "content": long_content}])

    hits = backend.recall("canary check")
    snippet = hits[0].content
    full = backend.get(hits[0].id)

    assert full.strip() == long_content.strip()
    assert len(full) > len(snippet), "get() should return more than recall()'s snippet window"


def test_get_by_daily_log_file_id_returns_the_whole_file(tmp_path: Path) -> None:
    """get('file:<name>') — the id recall() mints for MEMORY.md and daily
    logs — returns that whole file, same as the entry-id path does for
    entries/<id>.md."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    text = "# 2026-08-11\n\n- freeform note: the canary check flapped twice overnight\n"
    (memory_dir / "2026-08-11.md").write_text(text, encoding="utf-8")
    backend = GitMemoryBackend(tmp_path)

    hits = backend.recall("canary check")

    assert backend.get(hits[0].id) == text


def test_get_max_chars_never_exceeds_budget_even_when_marker_does_not_fit(
    tmp_path: Path,
) -> None:
    """A truncation marker that's itself longer than max_chars must not push
    the result over budget — 'never exceed' outranks 'always explain',
    same call context()'s own budget path already makes."""
    backend = GitMemoryBackend(tmp_path)
    backend.remember([{"id": "mem-1", "content": "word " * 200}])

    for max_chars in (1, 5, 20, 39, 40, 100):
        out = backend.get("mem-1", max_chars=max_chars)
        assert len(out) <= max_chars, f"max_chars={max_chars} but got {len(out)} chars back"


def test_get_unknown_id_raises(tmp_path: Path) -> None:
    backend = GitMemoryBackend(tmp_path)

    with pytest.raises(ValueError):
        backend.get("no-such-memory")


def test_get_rejects_path_traversal_via_file_prefix(tmp_path: Path) -> None:
    """The 'file:' id form is reachable with a caller-supplied string via
    the MCP tool surface, not just recall()'s own output — a '../' must be
    rejected, not resolved outside memory_dir/home."""
    backend = GitMemoryBackend(tmp_path)

    with pytest.raises(ValueError):
        backend.get("file:../../../etc/passwd")


def test_recall_ranks_results(tmp_path: Path) -> None:
    """Recency + match count + section depth beats first-file-wins ordering.

    Fixed: recall() now sorts by (match count, mtime) descending instead of
    glob order.
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "2026-01-01.md").write_text("frontdoor mentioned once\n")
    (mem / "2026-08-01.md").write_text("frontdoor frontdoor frontdoor — the real answer\n")

    hits = GitMemoryBackend(tmp_path).recall("frontdoor")

    assert "the real answer" in hits[0].content, (
        "results come back in glob order; there is no scoring of any kind"
    )


# ── F5 · consolidation is not reachable from the agent ──────────────────────


def test_mcp_exposes_a_reflect_tool() -> None:
    """Consolidation must be reachable from the agent, not only from a worker cron.

    Qortia's POST /v1/reflect is a normal agent-authed endpoint (confirmed by
    reading qortia/src/qortia/reflect.py — it takes only `AgentIdentity`), so
    this was a pure agnova-side gap, not an operational dependency on someone
    separately running `qortia-worker --only idle-reflect`.

    Fixed: `reflect` is now on TOOLS, backed by MemoryBackend.reflect() —
    QortiaMemoryBackend calls POST /v1/reflect; GitMemoryBackend is a
    documented no-op (no automated consolidation exists on that backend).
    """
    assert "reflect" in {t["name"] for t in TOOLS}, (
        "no reflect tool on the MCP surface — consolidation can only be triggered "
        "by the idle-reflect background worker, never by the agent itself"
    )


# ── Progressive disclosure · the scaffold ships no Tier-1 index ─────────────


def test_scaffold_writes_a_discoverable_skill_index(tmp_path: Path) -> None:
    """A new agent needs names+descriptions always in context, bodies on match.

    Fixed: scaffold now ports agnova's own dogfooded convention
    (.agents/skills/<name>/SKILL.md) onto the agent it scaffolds — a starter
    skills/example/SKILL.md plus skills/INDEX.md.
    """
    init("scout", tmp_path, owner="abc", relay="wss://relay.example")

    skills = list((tmp_path / "skills").rglob("SKILL.md"))
    index = tmp_path / "skills" / "INDEX.md"

    assert skills or index.is_file(), (
        "scaffold writes skills/.gitkeep and nothing else — no SKILL.md convention, "
        "no index, so there is nothing for progressive disclosure to disclose"
    )


def test_scaffold_writes_a_knowledge_index(tmp_path: Path) -> None:
    """knowledge/ accumulates by design; without an index it must be read whole or grepped.

    Fixed: scaffold now writes knowledge/INDEX.md alongside knowledge/README.md.
    """
    init("scout", tmp_path, owner="abc", relay="wss://relay.example")

    assert (tmp_path / "knowledge" / "INDEX.md").is_file(), (
        "knowledge/ ships a prose README but no one-line-per-entry index"
    )
