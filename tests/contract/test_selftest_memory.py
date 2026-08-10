"""`agnova selftest --memory` against a real subprocess — the same instinct
as selftest.py's transport check (spawn, speak the real protocol, assert
in the order things break), applied to the memory plane. No mocks in the
happy-path test: it spawns the actual `agnova-memory` console script and
speaks real JSON-RPC over its stdio, the same way buzz-acp would.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agnova import config as agent_config
from agnova import selftest
from agnova.config import AgentConfig
from agnova.scaffold import init

pytestmark = pytest.mark.skipif(
    shutil.which("agnova-memory") is None,
    reason="agnova-memory console script not on PATH (pip install -e . / uv sync)",
)


def _cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        name="scout",
        label="Scout",
        home=tmp_path,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="a" * 64,
        frontdoor_port=8844,
        agent_command="claude-agent-acp",
        respond_to="owner-only",
        auth_tag=None,
    )


def test_selftest_memory_round_trips_against_a_real_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    init("scout", tmp_path, owner="a" * 64, relay="wss://relay.example")
    monkeypatch.setattr(agent_config, "load", lambda agent: _cfg(tmp_path))

    with pytest.raises(SystemExit) as exc:
        selftest.run_memory("scout")

    assert exc.value.code == 0


def test_selftest_memory_fails_loudly_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(agent_config, "load", lambda agent: _cfg(tmp_path))
    monkeypatch.setattr(selftest.shutil, "which", lambda _name: None)

    with pytest.raises(SystemExit) as exc:
        selftest.run_memory("scout")

    assert exc.value.code == 1
    assert "agnova-memory not on PATH" in capsys.readouterr().out
