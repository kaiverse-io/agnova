from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import coincurve
import pytest

from agnova import config, frontdoor, mint_auth_tag, nostr, supervise, upstream
from agnova.config import AgentConfig


def test_nostr_bech32_errors_and_nsec_loading() -> None:
    with pytest.raises(ValueError, match="mixed case"):
        nostr._bech32_decode("Nsec1ABC")

    with pytest.raises(ValueError, match="no separator"):
        nostr._bech32_decode("nsec")

    with pytest.raises(ValueError, match="checksum failed"):
        nostr._bech32_decode("nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq")


def test_nostr_nip98_header_includes_payload_hash() -> None:
    secret = nostr.load_secret_key("11" * 32)
    body = b'{"kinds":[9]}'
    header = nostr.nip98_header(secret, "post", "https://relay.example/query", body)
    assert header.startswith("Nostr ")


def test_mint_auth_tag_main_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BUZZ_OWNER_PRIVATE_KEY", mint_auth_tag.VECTOR["owner_secret"])
    monkeypatch.setattr(
        mint_auth_tag.sys,
        "argv",
        ["mint-auth-tag", "--agent", mint_auth_tag.VECTOR["agent_pubkey"]],
    )
    import runpy

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("agnova.mint_auth_tag", run_name="__main__")
    assert exc.value.code == 0
    assert '["auth"' in capsys.readouterr().out


def test_upstream_check_up_to_date_and_no_tags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = upstream.LOCK
    original = lock.read_text(encoding="utf-8")
    try:
        lock.write_text(
            "repo = https://example/buzz.git\nref = v1.0.0\nsha = same\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            upstream,
            "remote_tags",
            lambda repo: [((1, 0, 0), "v1.0.0", "same")],
        )
        assert upstream.check() == 0
        assert "up to date" in capsys.readouterr().out

        monkeypatch.setattr(upstream, "remote_tags", lambda repo: [])
        with pytest.raises(SystemExit, match="no release tags"):
            upstream.check()
    finally:
        lock.write_text(original, encoding="utf-8")


def test_upstream_check_non_semver_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = upstream.LOCK
    original = lock.read_text(encoding="utf-8")
    try:
        lock.write_text(
            "repo = https://example/buzz.git\nref = main\nsha = old\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            upstream,
            "remote_tags",
            lambda repo: [((1, 0, 0), "v1.0.0", "new")],
        )
        assert upstream.check() == 0
        assert "not a release tag" in capsys.readouterr().out
    finally:
        lock.write_text(original, encoding="utf-8")


def test_upstream_update_defaults_to_latest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = upstream.LOCK
    original = lock.read_text(encoding="utf-8")
    try:
        lock.write_text(
            "repo = https://example/buzz.git\nref = v1.0.0\nsha = old\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            upstream,
            "remote_tags",
            lambda repo: [((1, 0, 0), "v1.0.0", "old"), ((2, 0, 0), "v2.0.0", "newsha")],
        )
        assert upstream.update(None) == 0
        text = lock.read_text(encoding="utf-8")
        assert "ref = v2.0.0" in text
        assert "sha = newsha" in text
        assert "pinned v1.0.0" in capsys.readouterr().out
    finally:
        lock.write_text(original, encoding="utf-8")


def test_upstream_main_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "check", lambda: 3)
    monkeypatch.setattr(upstream.sys, "argv", ["upstream", "check"])
    with pytest.raises(SystemExit) as exc:
        upstream.main()
    assert exc.value.code == 3

    monkeypatch.setattr(upstream, "update", lambda ref: 4)
    monkeypatch.setattr(upstream.sys, "argv", ["upstream", "update", "v1.0.0"])
    with pytest.raises(SystemExit) as exc:
        upstream.main()
    assert exc.value.code == 4

    monkeypatch.setattr(upstream, "clone_args", lambda: 5)
    monkeypatch.setattr(upstream.sys, "argv", ["upstream", "clone-args"])
    with pytest.raises(SystemExit) as exc:
        upstream.main()
    assert exc.value.code == 5


def test_config_parse_env_quoted_and_comment_edge_cases(tmp_path: Path) -> None:
    env = tmp_path / "edge.env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "BAD LINE",
                'QUOTED="value with # hash"',
                "SINGLE='one'",
                "TRAIL=value # comment",
            ]
        ),
        encoding="utf-8",
    )
    parsed = config.read_env_file(env)
    assert parsed["QUOTED"] == "value with # hash"
    assert parsed["SINGLE"] == "one"
    assert parsed["TRAIL"] == "value"
    assert "BAD LINE" not in parsed


def test_supervise_stop_falls_back_to_single_process_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    cfg = AgentConfig(
        name="scout",
        label="Scout",
        home=tmp_path,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="aa" * 32,
        frontdoor_port=8844,
        agent_command="agent-cmd",
        respond_to="owner-only",
        auth_tag=None,
    )
    cfg.var.mkdir(parents=True)
    supervise.pid_file(cfg, "harness").write_text("123\n", encoding="utf-8")
    killed: list[tuple[int, int]] = []
    states = iter([123, None])

    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: next(states))
    monkeypatch.setattr(supervise.os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError))
    monkeypatch.setattr(supervise.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(supervise.time, "sleep", lambda seconds: None)

    assert supervise.stop(cfg, "harness") is True
    assert killed == [(123, supervise.signal.SIGTERM)]


def test_supervise_buzz_acp_finds_vendor_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vendor = tmp_path / "vendor" / "buzz-acp"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("#!/bin/sh\n", encoding="utf-8")
    vendor.chmod(0o755)
    monkeypatch.delenv("BUZZ_ACP_BINARY", raising=False)
    monkeypatch.setattr(supervise.shutil, "which", lambda name: None)
    monkeypatch.setattr(supervise, "ROOT", tmp_path)
    assert supervise.buzz_acp_binary() == str(vendor)


def test_supervise_enforce_dna_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agnova import dna

    (tmp_path / "SOUL.md").write_text("soul\n", encoding="utf-8")
    expected = dna.compute_hash(tmp_path, ["SOUL.md"])
    cfg = AgentConfig(
        name="scout",
        label="Scout",
        home=tmp_path,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="aa" * 32,
        frontdoor_port=8844,
        agent_command="agent-cmd",
        respond_to="owner-only",
        auth_tag=None,
        dna_hash=expected,
        dna_paths=["SOUL.md"],
    )
    monkeypatch.setattr(supervise, "bad", lambda msg: None)
    monkeypatch.setattr(supervise, "ok", lambda msg: None)
    assert supervise._enforce_dna(cfg) == 0


def test_supervise_ensure_frontdoor_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(supervise, "ROOT", tmp_path)
    cfg = AgentConfig(
        name="scout",
        label="Scout",
        home=tmp_path,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="aa" * 32,
        frontdoor_port=8844,
        agent_command="agent-cmd",
        respond_to="owner-only",
        auth_tag=None,
        transport="frontdoor",
    )
    cfg.var.mkdir(parents=True)
    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: 42)
    monkeypatch.setattr(supervise, "ok", lambda msg: None)
    assert supervise._ensure_frontdoor(cfg) == 0


def test_supervise_status_includes_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentConfig(
        name="scout",
        label="Scout",
        home=tmp_path,
        relay_url="wss://relay.example",
        secret_key="11" * 32,
        owner_pubkey="aa" * 32,
        frontdoor_port=8844,
        agent_command="agent-cmd",
        respond_to="owner-only",
        auth_tag=None,
        transport="direct",
        checkpoint_paths=["MEMORY.md"],
    )
    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: 9)
    monkeypatch.setattr(supervise, "ok", lambda msg: None)
    assert supervise.status(cfg) == 0


def test_supervise_main_hostwide_and_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(supervise, "_hostwide", lambda command, agent: 0)
    monkeypatch.setattr(supervise.sys, "argv", ["agnova", "install"])
    with pytest.raises(SystemExit) as exc:
        supervise.main()
    assert exc.value.code == 0

    monkeypatch.setattr(supervise, "_hostwide", lambda command, agent: None)
    monkeypatch.setattr(supervise, "_agent_command", lambda *args, **kwargs: 7)
    monkeypatch.setattr(supervise.agent_config, "load", lambda agent: SimpleNamespace())
    monkeypatch.setattr(supervise.sys, "argv", ["agnova", "doctor", "scout"])
    with pytest.raises(SystemExit) as exc:
        supervise.main()
    assert exc.value.code == 7


def test_frontdoor_handle_timeout_closes_client() -> None:
    class SlowReader:
        async def readuntil(self, sep: bytes) -> bytes:
            del sep
            raise TimeoutError

    class ClientWriter:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def drain(self) -> None:
            pass

        def write(self, data: bytes) -> None:
            pass

    async def run() -> None:
        door = frontdoor.FrontDoor(
            frontdoor.Upstream("wss://relay.example", None, None),
            nostr.load_secret_key("11" * 32),
        )
        out = ClientWriter()
        await door.handle(SlowReader(), out)  # type: ignore[arg-type]
        assert out.closed

    asyncio.run(run())


def test_frontdoor_read_ws_frame_127_length() -> None:
    payload = b"z" * 70000
    frame = frontdoor.encode_ws_frame(frontdoor.OPCODE_TEXT, payload)

    async def read_frame() -> tuple[int, bool, bytes, bytes]:
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await frontdoor.read_ws_frame(reader)

    opcode, fin, decoded, _raw = asyncio.run(read_frame())
    assert opcode == frontdoor.OPCODE_TEXT
    assert fin is True
    assert decoded == payload


def test_frontdoor_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontdoor, "main", lambda: None)
    import runpy

    with pytest.raises(SystemExit):
        runpy.run_module("agnova.frontdoor", run_name="__main__")
