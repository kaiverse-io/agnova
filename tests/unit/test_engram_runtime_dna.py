"""Unit tests for engram, runtime lock, DNA edges, config helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agnova import dna, engram, runtime
from agnova.config import AgentConfig


def _cfg(home: Path, **kwargs: object) -> AgentConfig:
    base: dict = dict(
        name="ben",
        label="Scout",
        home=home,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="ownerpub",
        frontdoor_port=18790,
        agent_command="buzz-acp",
        respond_to="mentions",
        auth_tag=None,
        engram_paths=["SOUL.md"],
        dna_hash=None,
        dna_paths=list(dna.DEFAULT_DNA_PATHS),
    )
    base.update(kwargs)
    return AgentConfig(**base)  # type: ignore[arg-type]


def test_engram_render_and_missing(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("soul text\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("user text\n", encoding="utf-8")
    cfg = _cfg(tmp_path, engram_paths=["SOUL.md", "USER.md"])
    out = engram.render(cfg)  # type: ignore[arg-type]
    assert "soul text" in out and "user text" in out
    assert "---" in out

    cfg_missing = _cfg(tmp_path, engram_paths=["NOPE.md"])
    with pytest.raises(SystemExit, match="missing"):
        engram.render(cfg_missing)  # type: ignore[arg-type]

    cfg_empty = _cfg(tmp_path, engram_paths=[])
    with pytest.raises(SystemExit, match="AGENT_ENGRAM_PATHS"):
        engram.render(cfg_empty)  # type: ignore[arg-type]


def test_engram_publish_match_and_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "SOUL.md").write_text("x\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    body = engram.render(cfg)  # type: ignore[arg-type]
    digest = __import__("hashlib").sha256(body.encode()).hexdigest()

    calls: list[list[str]] = []

    def fake_buzz(args, stdin=None):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        if args[:3] == ["mem", "hash", "core"]:
            return MagicMock(returncode=0, stdout=digest + "\n", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(engram, "_buzz", fake_buzz)
    monkeypatch.setattr(engram.time, "sleep", lambda _: None)
    assert engram.publish(cfg) == 0  # type: ignore[arg-type]
    assert any(c[:3] == ["mem", "hash", "core"] for c in calls)

    # mismatch → set
    def fake_buzz2(args, stdin=None):  # type: ignore[no-untyped-def]
        if args[:3] == ["mem", "hash", "core"]:
            return MagicMock(returncode=0, stdout="deadbeef\n", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(engram, "_buzz", fake_buzz2)
    assert engram.publish(cfg) == 0  # type: ignore[arg-type]

    cfg_no_owner = _cfg(tmp_path, owner_pubkey="")
    with pytest.raises(SystemExit, match="BUZZ_OWNER_PUBKEY"):
        engram.publish(cfg_no_owner)  # type: ignore[arg-type]


def test_engram_publish_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "SOUL.md").write_text("x\n", encoding="utf-8")
    cfg = _cfg(tmp_path)

    def fake_buzz(args, stdin=None):  # type: ignore[no-untyped-def]
        if args[:3] == ["mem", "hash", "core"]:
            return MagicMock(returncode=1, stdout="", stderr="no")
        return MagicMock(returncode=1, stdout="", stderr="fail")

    monkeypatch.setattr(engram, "_buzz", fake_buzz)
    monkeypatch.setattr(engram.time, "sleep", lambda _: None)
    with pytest.raises(SystemExit, match="publish failed"):
        engram.publish(cfg)  # type: ignore[arg-type]


def test_runtime_read_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="no runtime.lock"):
        runtime.read_lock()
    (tmp_path / "runtime.lock").write_text("repo = x\nref = main\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing"):
        runtime.read_lock()
    (tmp_path / "runtime.lock").write_text(
        "# comment\nrepo = r\nref = main\nsha = deadbeef\n",
        encoding="utf-8",
    )
    assert runtime.read_lock() == {"repo": "r", "ref": "main", "sha": "deadbeef"}


def test_runtime_installed_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dist:
        def read_text(self, name: str) -> str | None:
            if name == "direct_url.json":
                return json.dumps({"vcs_info": {"commit_id": "abc123"}})
            return None

    monkeypatch.setattr(runtime.metadata, "distribution", lambda _: Dist())
    assert runtime.installed_sha() == "abc123"

    def missing(_: str):  # type: ignore[no-untyped-def]
        raise runtime.metadata.PackageNotFoundError("agnova")

    monkeypatch.setattr(runtime.metadata, "distribution", missing)
    assert runtime.installed_sha() is None


def test_dna_parse_and_escape(tmp_path: Path) -> None:
    assert dna.parse_hash("SHA256:" + "a" * 64) == "a" * 64
    with pytest.raises(ValueError):
        dna.parse_hash("not-a-hash")
    (tmp_path / "SOUL.md").write_text("x", encoding="utf-8")
    # path escape
    with pytest.raises(ValueError, match="escapes"):
        dna.compute_hash(tmp_path, ["../etc/passwd"])
    dna.lock_readonly(tmp_path, ["SOUL.md", "NOPE.md"])


def test_dna_enforce_ok_and_lock(tmp_path: Path) -> None:
    (tmp_path / "DNA.md").write_text("constitution\n", encoding="utf-8")
    got = dna.compute_hash(tmp_path, ["DNA.md"])
    assert dna.enforce(tmp_path, expected=got, paths=["DNA.md"], readonly=True) == got
