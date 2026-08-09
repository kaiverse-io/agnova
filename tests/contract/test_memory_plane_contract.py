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


@pytest.mark.xfail(strict=True, reason="BUZZ_ACP_MCP_COMMAND is never set by harness_env()")
def test_harness_env_registers_the_memory_mcp_server(tmp_path: Path) -> None:
    """`agnova up` must hand buzz-acp a command that serves agnova-memory.

    Without this the agent has no context/recall/remember/forget at all, and
    every backend behind AGENT_MEMORY_BACKEND is unreachable code.
    """
    env = _config(tmp_path).harness_env()

    assert env.get("BUZZ_ACP_MCP_COMMAND"), (
        "harness_env() sets AGENT_MEMORY_BACKEND but never BUZZ_ACP_MCP_COMMAND, "
        "so buzz-acp registers no MCP servers and the agent gets no memory tools"
    )
    assert "agnova-memory" in env["BUZZ_ACP_MCP_COMMAND"]


@pytest.mark.xfail(strict=True, reason="AGENT_MEMORY_BACKEND is set but nothing reads it")
def test_qortia_backend_selection_reaches_the_harness(tmp_path: Path) -> None:
    """Choosing the qortia backend must actually change what the agent can do.

    Today `memory_backend="qortia"` only sets an env var read by a process
    nothing launches — a config knob wired to nothing.
    """
    env = _config(tmp_path, memory_backend="qortia").harness_env()

    assert env["AGENT_MEMORY_BACKEND"] == "qortia"
    assert env.get("BUZZ_ACP_MCP_COMMAND"), (
        "AGENT_MEMORY_BACKEND=qortia is inert: no MCP command means no process " "ever reads it"
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


@pytest.mark.xfail(strict=True, reason="MemoryItem.type defaults to 'note', outside Qortia's enum")
def test_default_memory_type_is_valid_on_every_backend() -> None:
    """A remember() that succeeds on `git` must not 422 the batch on `qortia`."""
    default_type = MemoryItem(id="x", content="y").type

    assert default_type in QORTIA_TYPES, (
        f"MemoryBackend's default type {default_type!r} is not in Qortia's closed "
        f"enum {sorted(QORTIA_TYPES)} — the seam's own default is invalid on half "
        f"its implementations"
    )


@pytest.mark.xfail(strict=True, reason="GitMemoryBackend stamps type='note' by default")
def test_git_backend_writes_types_qortia_accepts(tmp_path: Path) -> None:
    """The default backend must not mint a vocabulary the other one rejects."""
    stored = GitMemoryBackend(tmp_path).remember(
        [{"content": "the relay rejects loopback-signed NIP-98 tokens on purpose"}]
    )

    assert stored[0].type in QORTIA_TYPES, (
        f"GitMemoryBackend stamped type={stored[0].type!r}; switching "
        f"AGENT_MEMORY_BACKEND to qortia turns every such memory into a 422"
    )


@pytest.mark.xfail(strict=True, reason="remember's type schema is a bare {'type': 'string'}")
def test_mcp_schema_publishes_the_memory_type_enum() -> None:
    """The agent must be able to read the vocabulary, not guess it.

    A tool whose schema says `{"type": "string"}` for a closed six-value enum
    is a tool the model will call wrongly and then stop calling — and, per
    F2 above, "wrongly" here means the omission itself 422s on qortia, since
    `type` is not even in the item's `required` list.
    """
    remember = next(t for t in TOOLS if t["name"] == "remember")
    item = remember["inputSchema"]["properties"]["items"]["items"]
    type_schema = item["properties"]["type"]

    assert "enum" in type_schema, (
        "remember's `type` is advertised as a free-form string; the agent has no "
        "way to discover episodic/experiential/mental_model/decision/lesson/short_term"
    )
    assert set(type_schema["enum"]) == QORTIA_TYPES


@pytest.mark.xfail(strict=True, reason="the 5-word floor and ttl_seconds rule are undocumented")
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
    """A context bundle where every section is bigger than a small budget."""
    filler = "x " * 400
    return {
        "org_chart": [{"title": "Org", "content": f"org chart {filler}"}],
        "processes": [{"title": "Process", "content": f"process {filler}"}],
        "handoffs": [{"title": "Handoff", "content": f"handoff {filler}"}],
        "weekly_summary": {"title": "Week", "content": f"weekly {filler}"},
        "memories": {
            "decisions": [{"content": f"DECISION-MARKER {filler}"}],
            "mental_models": [{"content": f"MODEL-MARKER {filler}"}],
            "lessons": [{"content": f"LESSON-MARKER {filler}"}],
        },
    }


@pytest.mark.xfail(strict=True, reason="context() truncates in render order, not importance order")
def test_context_keeps_the_most_important_memories_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under budget pressure, drop 0.3-importance episodics before 0.95 lessons.

    Today the render order is org_chart -> processes -> handoffs -> weekly ->
    decisions -> mental_models -> lessons, followed by text[:budget]. That is
    close to the exact inverse of Qortia's own importance priors. It is also,
    per a live /v1/context read, unfixable client-side as written: decisions
    are selected without `importance` at all (remember.py's get_context does
    `MemoryEntry(content=r["content"])` for decisions but
    `MemoryEntry(content=..., importance=r["importance"])` for mental_models
    and lessons) — see the qortia-side P2 fix this depends on.
    """
    backend = QortiaMemoryBackend("http://qortia.test", "key", "agent-id")
    monkeypatch.setattr(backend, "_request", lambda *a, **k: _bundle())

    out = backend.context(budget=1200)

    assert "LESSON-MARKER" in out, (
        f"lessons (importance {QORTIA_IMPORTANCE['lesson']}) were truncated away "
        f"while org-chart boilerplate survived — render order ignores importance"
    )


@pytest.mark.xfail(strict=True, reason="context() byte-slices mid-record with no drop marker")
def test_context_does_not_cut_a_record_mid_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping whole records beats slicing bytes: a half-record is a hallucination risk."""
    backend = QortiaMemoryBackend("http://qortia.test", "key", "agent-id")
    monkeypatch.setattr(backend, "_request", lambda *a, **k: _bundle())

    out = backend.context(budget=1200)

    assert (
        out.endswith(("\n", ".", "]", ")")) or "omitted" in out.lower()
    ), "context() ends mid-token with no marker saying anything was dropped"


# ── F4 · git recall returns whole documents ─────────────────────────────────


@pytest.mark.xfail(strict=True, reason="GitMemoryBackend.recall() returns whole matched files")
def test_recall_returns_snippets_not_whole_documents(tmp_path: Path) -> None:
    """A retrieval step must cost less context than skipping retrieval.

    GitMemoryBackend matches a substring and then returns the entire file —
    so recall against a mature MEMORY.md returns all of MEMORY.md.
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


@pytest.mark.xfail(strict=True, reason="GitMemoryBackend.recall() has no ranking of any kind")
def test_recall_ranks_results(tmp_path: Path) -> None:
    """Recency + match count + section depth beats first-file-wins ordering."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "2026-01-01.md").write_text("frontdoor mentioned once\n")
    (mem / "2026-08-01.md").write_text("frontdoor frontdoor frontdoor — the real answer\n")

    hits = GitMemoryBackend(tmp_path).recall("frontdoor")

    assert (
        "the real answer" in hits[0].content
    ), "results come back in glob order; there is no scoring of any kind"


# ── F5 · consolidation is not reachable from the agent ──────────────────────


@pytest.mark.xfail(strict=True, reason="the MCP tool list has no reflect entry")
def test_mcp_exposes_a_reflect_tool() -> None:
    """Consolidation must be reachable from the agent, not only from a worker cron.

    Qortia's POST /v1/reflect is a normal agent-authed endpoint (confirmed by
    reading qortia/src/qortia/reflect.py — it takes only `AgentIdentity`), so
    this is a pure agnova-side gap, not an operational dependency on someone
    separately running `qortia-worker --only idle-reflect`.
    """
    assert "reflect" in {t["name"] for t in TOOLS}, (
        "no reflect tool on the MCP surface — consolidation can only be triggered "
        "by the idle-reflect background worker, never by the agent itself"
    )


# ── Progressive disclosure · the scaffold ships no Tier-1 index ─────────────


@pytest.mark.xfail(strict=True, reason="scaffold writes skills/.gitkeep and nothing else")
def test_scaffold_writes_a_discoverable_skill_index(tmp_path: Path) -> None:
    """A new agent needs names+descriptions always in context, bodies on match.

    agnova dogfoods this on itself (.agents/skills/<name>/SKILL.md) but the
    agent it scaffolds gets an empty skills/.gitkeep.
    """
    init("scout", tmp_path, owner="abc", relay="wss://relay.example")

    skills = list((tmp_path / "skills").rglob("SKILL.md"))
    index = tmp_path / "skills" / "INDEX.md"

    assert skills or index.is_file(), (
        "scaffold writes skills/.gitkeep and nothing else — no SKILL.md convention, "
        "no index, so there is nothing for progressive disclosure to disclose"
    )


@pytest.mark.xfail(strict=True, reason="knowledge/ ships a README but no per-entry index")
def test_scaffold_writes_a_knowledge_index(tmp_path: Path) -> None:
    """knowledge/ accumulates by design; without an index it must be read whole or grepped."""
    init("scout", tmp_path, owner="abc", relay="wss://relay.example")

    assert (
        tmp_path / "knowledge" / "INDEX.md"
    ).is_file(), "knowledge/ ships a prose README but no one-line-per-entry index"
