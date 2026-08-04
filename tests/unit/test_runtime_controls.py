from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import coincurve
import pytest

from agnova import config, engram, mint_auth_tag, runtime, scaffold, selftest, supervise, upstream
from agnova.config import AgentConfig
from agnova.memory import mcp_server
from agnova.memory.git_backend import GitMemoryBackend

OWNER_SECRET = "00" * 31 + "01"
OWNER_PUBKEY = mint_auth_tag.VECTOR["owner_pubkey"]
AGENT_PUBKEY = mint_auth_tag.VECTOR["agent_pubkey"]


def _cfg(tmp_path: Path, **overrides: object) -> AgentConfig:
    values = {
        "name": "scout",
        "label": "Scout",
        "home": tmp_path,
        "relay_url": "wss://relay.example",
        "secret_key": "11" * 32,
        "owner_pubkey": OWNER_PUBKEY,
        "frontdoor_port": 8844,
        "agent_command": "agent-cmd",
        "respond_to": "owner-only",
        "auth_tag": None,
    }
    values.update(overrides)
    return AgentConfig(**values)


def test_workspace_prefers_explicit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGNOVA_HOME", str(tmp_path))

    assert config._workspace() == tmp_path.resolve()


def test_workspace_uses_git_root_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGNOVA_HOME", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{tmp_path}\n"),
    )

    assert config._workspace() == tmp_path.resolve()


def test_workspace_falls_back_to_cwd_on_git_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGNOVA_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    def fail_git(*args: object, **kwargs: object) -> object:
        raise OSError("git unavailable")

    monkeypatch.setattr(config.subprocess, "run", fail_git)

    assert config._workspace() == tmp_path.resolve()


def test_load_merges_file_and_environment_with_host_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agents = tmp_path / "agents"
    var = tmp_path / "var"
    agents.mkdir()
    (agents / "scout.env").write_text(
        "\n".join(
            [
                "BUZZ_AGENT_LABEL=File Scout",
                "BUZZ_RELAY_URL=wss://file-relay.example",
                "BUZZ_PRIVATE_KEY=file-secret",
                "BUZZ_OWNER_PUBKEY=file-owner",
                "BUZZ_FRONTDOOR_PORT=9001",
                "BUZZ_ACP_AGENT_COMMAND=file-agent",
                "BUZZ_ACP_RESPOND_TO=owner-only",
                'BUZZ_AUTH_TAG=["auth"]',
                "BUZZ_AGENT_HOME=.",
                "AGNOVA_TRANSPORT=direct",
                "AGENT_ENGRAM_PATHS=SOUL.md, PRINCIPLES.md",
                "AGENT_CHECKPOINT_PATHS=memory/, MEMORY.md",
                "AGENT_CHECKPOINT_INTERVAL=5",
                "AGENT_CHECKPOINT_UPLOAD_URL=https://checkpoint.example/upload",
                "AGENT_CHECKPOINT_TOKEN=file-token",
                "AGENT_DNA_HASH=sha256:" + "a" * 64,
                "AGENT_DNA_PATHS=SOUL.md,USER.md",
                "AGENT_MEMORY_BACKEND=git",
                "BUZZ_ACP_EXTRA=kept-for-harness",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "VAR_DIR", var)
    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "env-secret")
    monkeypatch.setenv("BUZZ_RELAY_URL", "wss://env-relay.example")
    monkeypatch.setenv("AGENT_CHECKPOINT_TOKEN", "env-token")

    cfg = config.load("Scout")

    assert cfg.name == "scout"
    assert cfg.secret_key == "env-secret"
    assert cfg.relay_url == "wss://env-relay.example"
    assert cfg.checkpoint_interval == 60
    assert cfg.engram_paths == ["SOUL.md", "PRINCIPLES.md"]
    assert cfg.checkpoint_paths == ["memory/", "MEMORY.md"]
    assert cfg.dna_paths == ["SOUL.md", "USER.md"]
    assert cfg.env["BUZZ_ACP_EXTRA"] == "kept-for-harness"
    assert "BUZZ_ACP_AGENT_COMMAND" in cfg.env
    assert cfg.var.is_dir()

    env = cfg.harness_env()
    assert env["BUZZ_RELAY_URL"] == "wss://env-relay.example"
    assert env["BUZZ_PRIVATE_KEY"] == "env-secret"
    assert env["BUZZ_ACP_AGENT_OWNER"] == "file-owner"
    assert env["BUZZ_AUTH_TAG"] == '["auth"]'
    assert env["AGENT_CHECKPOINT_TOKEN"] == "env-token"
    assert env["AGENT_MEMORY_BACKEND"] == "git"


def test_load_reports_actionable_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "example.env").write_text("", encoding="utf-8")
    (agents / "scout.env").write_text("BUZZ_RELAY_URL=wss://relay.example\n", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.delenv("BUZZ_AGENT_NAME", raising=False)
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BUZZ_RELAY_URL", raising=False)

    with pytest.raises(SystemExit, match="available: scout"):
        config.load()
    with pytest.raises(SystemExit, match="expected agents/missing.env"):
        config.load("missing")
    with pytest.raises(SystemExit, match="BUZZ_PRIVATE_KEY is not set"):
        config.load("scout")

    (agents / "scout.env").write_text("BUZZ_PRIVATE_KEY=secret\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="BUZZ_RELAY_URL is not set"):
        config.load("scout")


@pytest.mark.parametrize(
    ("transport", "proxy", "expected"),
    [
        ("direct", "http://proxy", False),
        ("frontdoor", "", True),
        ("auto", "http://proxy", True),
        ("auto", "", False),
    ],
)
def test_agent_config_transport_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: str, proxy: str, expected: bool
) -> None:
    if proxy:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    else:
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
    cfg = _cfg(tmp_path, transport=transport)

    assert cfg.uses_frontdoor is expected
    assert cfg.local_relay_url == (
        f"ws://127.0.0.1:{cfg.frontdoor_port}" if expected else cfg.relay_url
    )
    assert cfg.path("frontdoor.pid") == cfg.var / "frontdoor.pid"


def test_git_memory_edges(tmp_path: Path) -> None:
    backend = GitMemoryBackend(tmp_path)
    (tmp_path / "MEMORY.md").write_text("Durable Dharma\n", encoding="utf-8")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    for index in range(8):
        (memory_dir / f"2026-08-{index + 1:02d}.md").write_text(f"day {index}\n", encoding="utf-8")

    context = backend.context(budget=30)
    assert context.startswith("Durable Dharma")
    assert len(context) <= 30
    assert backend.recall("") == []
    assert backend.recall("dharma")[0].id == "file:MEMORY.md"

    with pytest.raises(ValueError, match="invalid memory id"):
        backend.remember([{"id": "../escape", "content": "bad"}])
    with pytest.raises(ValueError, match="invalid memory id"):
        backend.forget("../escape")


def test_mcp_backend_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUZZ_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("AGNOVA_HOME", raising=False)
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "git")

    assert mcp_server._backend().home == tmp_path.resolve()

    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "qortia")
    with pytest.raises(SystemExit, match="not implemented yet"):
        mcp_server._backend()


def test_mcp_dispatch_covers_tools_and_json_rpc(tmp_path: Path) -> None:
    backend = GitMemoryBackend(tmp_path)

    assert mcp_server._handle(backend, {"id": 1, "method": "initialize"})["result"][
        "capabilities"
    ] == {"tools": {}}
    assert mcp_server._handle(backend, {"method": "notifications/initialized"}) is None
    assert mcp_server._handle(backend, {"method": "missing"}) is None
    assert mcp_server._handle(backend, {"id": 9, "method": "missing"})["error"]["code"] == -32601

    remembered = mcp_server._handle(
        backend,
        {
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {"items": [{"id": "lesson", "type": "lesson", "content": "Dharma"}]},
            },
        },
    )
    assert json.loads(remembered["result"]["content"][0]["text"])[0]["id"] == "lesson"

    for tool_name, arguments in (
        ("context", {"budget": 20}),
        ("recall", {"query": "dharma", "filters": {"type": "lesson"}}),
        ("forget", {"id": "lesson"}),
    ):
        reply = mcp_server._handle(
            backend,
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        assert "content" in reply["result"]

    error = mcp_server._handle(
        backend,
        {
            "id": 4,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": {"items": "not-a-list"}},
        },
    )
    assert error["result"]["isError"] is True
    assert "items must be an array" in error["result"]["content"][0]["text"]


def test_mcp_main_ignores_bad_lines_and_flushes_replies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mcp_server, "_backend", lambda: GitMemoryBackend(tmp_path))
    monkeypatch.setattr(
        mcp_server.sys,
        "stdin",
        io.StringIO('not-json\n\n{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'),
    )
    stdout = io.StringIO()
    monkeypatch.setattr(mcp_server.sys, "stdout", stdout)

    mcp_server.main()

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["tools"] == mcp_server.TOOLS


def test_runtime_read_lock_and_missing_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    (tmp_path / runtime.LOCK).write_text(
        "# comment\nrepo = https://example/repo.git\nignored\nref = main\nsha = abc123\n",
        encoding="utf-8",
    )

    assert runtime.read_lock() == {
        "repo": "https://example/repo.git",
        "ref": "main",
        "sha": "abc123",
    }

    (tmp_path / runtime.LOCK).write_text("repo = r\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing: ref, sha"):
        runtime.read_lock()

    (tmp_path / runtime.LOCK).unlink()
    with pytest.raises(SystemExit, match="no runtime.lock"):
        runtime.read_lock()


def test_runtime_installed_sha_from_direct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dist:
        def __init__(self, raw: str | None) -> None:
            self.raw = raw

        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return self.raw

    monkeypatch.setattr(
        runtime.metadata,
        "distribution",
        lambda name: Dist('{"vcs_info":{"commit_id":"abc"}}'),
    )
    assert runtime.installed_sha() == "abc"

    monkeypatch.setattr(runtime.metadata, "distribution", lambda name: Dist("{}"))
    assert runtime.installed_sha() is None

    def missing(name: str) -> object:
        raise runtime.metadata.PackageNotFoundError

    monkeypatch.setattr(runtime.metadata, "distribution", missing)
    assert runtime.installed_sha() is None


def test_runtime_status_check_and_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = {"repo": "https://github.com/km2411/agnova.git", "ref": "main", "sha": "abcdef"}
    monkeypatch.setattr(runtime, "read_lock", lambda: lock)
    monkeypatch.setattr(runtime, "installed_sha", lambda: None)
    assert runtime.status() == 0
    assert "not a pinned install" in capsys.readouterr().out

    monkeypatch.setattr(runtime, "installed_sha", lambda: "abcdef")
    assert runtime.status() == 0
    assert "match" in capsys.readouterr().out

    monkeypatch.setattr(runtime, "installed_sha", lambda: "000000")
    assert runtime.status() == 1
    assert "drift" in capsys.readouterr().out

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="abcdef refs/heads/main\n"),
    )
    assert runtime.check() == 0

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="no route"),
    )
    with pytest.raises(SystemExit, match="could not reach"):
        runtime.check()

    monkeypatch.setattr(runtime, "installed_sha", lambda: "abcdef")
    assert runtime.install() == 0

    calls: list[list[str]] = []
    monkeypatch.setattr(runtime, "installed_sha", lambda: "old")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    assert runtime.install() == 0
    assert calls[0][:4] == [runtime.sys.executable, "-m", "pip", "install"]
    assert calls[0][-1] == "agnova @ git+https://github.com/km2411/agnova@abcdef"

    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2),
    )
    with pytest.raises(SystemExit, match="pip install failed"):
        runtime.install()


def test_upstream_release_resolution_and_lock_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / "upstream.lock"
    lock.write_text(
        "repo = https://example/buzz.git\nref = v1.0.0\nsha = oldsha\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(upstream, "LOCK", lock)

    fake_stdout = "\n".join(
        [
            "zzz\trefs/tags/not-semver",
            "sha200\trefs/tags/v2.0.0",
            "sha101\trefs/tags/v1.0.1",
        ]
    )
    monkeypatch.setattr(
        upstream.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=fake_stdout, stderr=""),
    )

    assert upstream.read_lock()["sha"] == "oldsha"
    assert upstream.remote_tags("https://example/buzz.git") == [
        ((1, 0, 1), "v1.0.1", "sha101"),
        ((2, 0, 0), "v2.0.0", "sha200"),
    ]
    assert upstream.check() == 0
    assert "newer release" in capsys.readouterr().out
    assert upstream.update("v1.0.1") == 0
    assert "ref = v1.0.1" in lock.read_text(encoding="utf-8")
    assert "sha = sha101" in lock.read_text(encoding="utf-8")

    assert upstream.update("v1.0.1") == 0
    assert "already on v1.0.1" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="no release tag"):
        upstream.update("v9.9.9")

    upstream.clone_args()
    assert "git clone --filter=blob:none" in capsys.readouterr().out


def test_upstream_remote_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        upstream.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="denied"),
    )

    with pytest.raises(SystemExit, match="could not reach"):
        upstream.remote_tags("https://example/buzz.git")


def test_scaffold_init_writes_agent_repository_and_preserves_existing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scaffold, "_running_version", lambda: ("v0.1.0", "abc123"))

    assert scaffold.init("Scout", tmp_path, owner=OWNER_PUBKEY, relay="wss://relay.example") == 0

    assert "repo = https://github.com/km2411/agnova.git" in (tmp_path / "runtime.lock").read_text(
        encoding="utf-8"
    )
    env = (tmp_path / "agents/scout.env").read_text(encoding="utf-8")
    assert f"BUZZ_OWNER_PUBKEY={OWNER_PUBKEY}" in env
    assert "AGENT_ENGRAM_PATHS=SOUL.md,PRINCIPLES.md" in env
    assert (tmp_path / "memory").is_dir()
    assert (tmp_path / "skills/.gitkeep").is_file()
    assert "Scout scaffolded" in capsys.readouterr().out

    (tmp_path / "SOUL.md").write_text("custom\n", encoding="utf-8")
    assert scaffold.init("scout", tmp_path) == 0
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "custom\n"
    assert "exists, left alone" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="plain identifier"):
        scaffold.init("not-a-name", tmp_path)


def test_scaffold_running_version_handles_editable_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dist:
        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return None

    monkeypatch.setattr(scaffold.metadata, "distribution", lambda name: Dist())
    monkeypatch.setattr(scaffold.metadata, "version", lambda name: "1.2.3")

    assert scaffold._running_version() == ("v1.2.3", "")

    def missing(name: str) -> object:
        raise scaffold.metadata.PackageNotFoundError

    monkeypatch.setattr(scaffold.metadata, "distribution", missing)
    monkeypatch.setattr(scaffold.metadata, "version", missing)

    assert scaffold._running_version() == ("main", "")


def test_engram_render_requires_declared_existing_sources(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, engram_paths=["SOUL.md", "PRINCIPLES.md"])
    (tmp_path / "SOUL.md").write_text("soul\n", encoding="utf-8")
    (tmp_path / "PRINCIPLES.md").write_text("principles\n\n", encoding="utf-8")

    assert engram.render(cfg) == "soul\n\n---\n\nprinciples\n"

    with pytest.raises(SystemExit, match="engram source missing"):
        engram.render(_cfg(tmp_path, engram_paths=["MISSING.md"]))
    with pytest.raises(SystemExit, match="no AGENT_ENGRAM_PATHS"):
        engram.render(_cfg(tmp_path, engram_paths=[]))


def test_engram_publish_skips_matching_digest_and_publishes_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path, engram_paths=["SOUL.md"], owner_pubkey=OWNER_PUBKEY)
    (tmp_path / "SOUL.md").write_text("soul\n", encoding="utf-8")
    body = engram.render(cfg)
    digest = engram.hashlib.sha256(body.encode()).hexdigest()

    calls: list[tuple[list[str], str | None]] = []

    def matching_buzz(args: list[str], stdin: str | None = None) -> object:
        calls.append((args, stdin))
        return SimpleNamespace(returncode=0, stdout=digest, stderr="")

    monkeypatch.setattr(engram, "_buzz", matching_buzz)
    assert engram.publish(cfg) == 0
    assert calls == [(["mem", "hash", "core", "--owner", OWNER_PUBKEY], None)]
    assert "already matches" in capsys.readouterr().out

    def publishing_buzz(args: list[str], stdin: str | None = None) -> object:
        calls.append((args, stdin))
        if args[:3] == ["mem", "hash", "core"]:
            return SimpleNamespace(returncode=0, stdout="different", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    calls.clear()
    monkeypatch.setattr(engram, "_buzz", publishing_buzz)
    monkeypatch.setattr(engram.time, "sleep", lambda seconds: None)
    assert engram.publish(cfg) == 0
    assert calls[-1] == (["mem", "set", "core", "-", "--owner", OWNER_PUBKEY], body)
    assert "published core engram" in capsys.readouterr().out

    def failing_buzz(args: list[str], stdin: str | None = None) -> object:
        if args[:3] == ["mem", "hash", "core"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="relay denied")

    monkeypatch.setattr(engram, "_buzz", failing_buzz)
    with pytest.raises(SystemExit, match="publish failed: relay denied"):
        engram.publish(cfg)

    with pytest.raises(SystemExit, match="BUZZ_OWNER_PUBKEY"):
        engram.publish(_cfg(tmp_path, engram_paths=["SOUL.md"], owner_pubkey=""))


def test_mint_auth_tag_vector_selftest_and_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        mint_auth_tag.digest(AGENT_PUBKEY, mint_auth_tag.VECTOR["conditions"]).hex()
        == (mint_auth_tag.VECTOR["digest"])
    )
    assert mint_auth_tag.selftest() == 0
    assert "SELFTEST: PASS" in capsys.readouterr().out

    monkeypatch.setenv("BUZZ_OWNER_PRIVATE_KEY", OWNER_SECRET)
    assert mint_auth_tag.mint(AGENT_PUBKEY, "created_at<1800000000") == 0
    printed = capsys.readouterr().out
    tag = json.loads(next(line for line in printed.splitlines() if line.startswith('["auth"')))
    assert tag[:3] == ["auth", OWNER_PUBKEY, "created_at<1800000000"]
    assert coincurve.PublicKeyXOnly(bytes.fromhex(OWNER_PUBKEY)).verify(
        bytes.fromhex(tag[3]), mint_auth_tag.digest(AGENT_PUBKEY, tag[2])
    )


@pytest.mark.parametrize(
    ("env_value", "agent", "message"),
    [
        ("", AGENT_PUBKEY, "BUZZ_OWNER_PRIVATE_KEY is unset"),
        ("nsec1nothex", AGENT_PUBKEY, "give the key as hex"),
        (OWNER_SECRET, OWNER_PUBKEY, "self-attestation is invalid"),
    ],
)
def test_mint_auth_tag_rejects_invalid_invocations(
    monkeypatch: pytest.MonkeyPatch, env_value: str, agent: str, message: str
) -> None:
    if env_value:
        monkeypatch.setenv("BUZZ_OWNER_PRIVATE_KEY", env_value)
    else:
        monkeypatch.delenv("BUZZ_OWNER_PRIVATE_KEY", raising=False)

    with pytest.raises(SystemExit, match=message):
        mint_auth_tag.mint(agent, "")


def test_selftest_upgrade_parses_status_and_auth_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    class Sock:
        def __init__(self) -> None:
            self.sent = b""
            self.reads = [
                b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
                b'\x81\x0f["AUTH","abc"]',
            ]

        def sendall(self, data: bytes) -> None:
            self.sent += data

        def recv(self, size: int) -> bytes:
            del size
            return self.reads.pop(0)

        def settimeout(self, timeout: int) -> None:
            assert timeout == 15

        def close(self) -> None:
            pass

    sock = Sock()
    monkeypatch.setattr(selftest.socket, "create_connection", lambda *args, **kwargs: sock)
    passed, detail = selftest.upgrade(8443)

    assert passed is True
    assert "101 Switching Protocols" in detail
    assert '["AUTH","abc"]' in detail
    assert b"Host: 127.0.0.1:8443" in sock.sent


def test_selftest_query_posts_signed_loopback_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def read(self) -> bytes:
            return b"ok"

    class Conn:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            captured["connect"] = (host, port, timeout)

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            captured["request"] = (method, path, body, headers)

        def getresponse(self) -> Response:
            return Response()

    monkeypatch.setattr(selftest.http.client, "HTTPConnection", Conn)
    secret = coincurve.PrivateKey.from_hex("11" * 32)

    passed, detail = selftest.query(8844, secret)

    assert passed is True
    assert detail == "200 ok"
    method, path, body, headers = captured["request"]
    assert (method, path) == ("POST", "/query")
    assert b'"kinds": [9]' in body
    assert headers["Authorization"].startswith("Nostr ")


def test_selftest_run_exits_nonzero_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = SimpleNamespace(
        name="scout",
        secret_key="11" * 32,
        frontdoor_port=8844,
        relay_url="wss://relay.example",
    )
    monkeypatch.setattr(selftest.agent_config, "load", lambda agent: cfg)
    monkeypatch.setattr(selftest, "upgrade", lambda port: (True, "101 auth"))
    monkeypatch.setattr(selftest, "query", lambda port, secret: (False, "403 denied"))

    with pytest.raises(SystemExit) as exc:
        selftest.run("scout")

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "pubkey" in output
    assert "HTTP bridge with re-signed NIP-98: 403 denied" in output
    assert "Is the front door up?" in output


def test_supervise_process_bookkeeping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    cfg = _cfg(tmp_path)
    cfg.var.mkdir(parents=True)

    assert supervise.running_pid(cfg, "harness") is None
    supervise.pid_file(cfg, "harness").write_text("not-a-pid\n", encoding="utf-8")
    assert supervise.running_pid(cfg, "harness") is None
    assert not supervise.pid_file(cfg, "harness").exists()

    supervise.pid_file(cfg, "harness").write_text("123\n", encoding="utf-8")
    monkeypatch.setattr(supervise.os, "kill", lambda pid, sig: None)
    assert supervise.running_pid(cfg, "harness") == 123

    class Process:
        pid = 456

    popen_calls: list[tuple[list[str], Path]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> Process:
        popen_calls.append((argv, Path(str(kwargs["cwd"]))))
        return Process()

    monkeypatch.setattr(supervise.subprocess, "Popen", fake_popen)
    assert supervise.spawn(cfg, "frontdoor", ["cmd"], {"ENV": "1"}, tmp_path) == 456
    assert supervise.pid_file(cfg, "frontdoor").read_text(encoding="utf-8") == "456\n"
    assert "frontdoor starting" in supervise.log_file(cfg, "frontdoor").read_text(encoding="utf-8")
    assert popen_calls == [(["cmd"], tmp_path)]


def test_supervise_stop_terminates_process_group_and_cleans_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    cfg = _cfg(tmp_path)
    cfg.var.mkdir(parents=True)
    supervise.pid_file(cfg, "harness").write_text("123\n", encoding="utf-8")
    states = iter([123, 123, None])
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: next(states))
    monkeypatch.setattr(supervise.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(supervise.os, "killpg", lambda pgid, sig: signals.append(("pg", pgid, sig)))
    monkeypatch.setattr(supervise.time, "sleep", lambda seconds: None)

    assert supervise.stop(cfg, "harness") is True
    assert signals == [("pg", 999, supervise.signal.SIGTERM)]
    assert not supervise.pid_file(cfg, "harness").exists()

    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: None)
    assert supervise.stop(cfg, "harness") is False


def test_supervise_buzz_acp_binary_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "buzz-acp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("BUZZ_ACP_BINARY", str(binary))
    assert supervise.buzz_acp_binary() == str(binary)

    monkeypatch.setenv("BUZZ_ACP_BINARY", str(tmp_path / "missing"))
    assert supervise.buzz_acp_binary() is None

    monkeypatch.delenv("BUZZ_ACP_BINARY", raising=False)
    monkeypatch.setattr(supervise.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert supervise.buzz_acp_binary() == "/usr/bin/buzz-acp"


def test_supervise_doctor_status_logs_and_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(supervise, "ROOT", tmp_path)
    cfg = _cfg(
        tmp_path, secret_key="", owner_pubkey="", auth_tag=None, checkpoint_paths=["MEMORY.md"]
    )
    cfg.var.mkdir(parents=True)
    (cfg.var / "harness.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    monkeypatch.setattr(supervise, "buzz_acp_binary", lambda: None)
    monkeypatch.setattr(supervise.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        supervise, "running_pid", lambda cfg, service: 77 if service == "harness" else None
    )

    doctor_rc = supervise.doctor(cfg)
    assert doctor_rc >= 3
    doctor_out = capsys.readouterr().out
    assert "BUZZ_PRIVATE_KEY missing" in doctor_out
    assert supervise.status(cfg) == 1
    status_out = capsys.readouterr().out
    assert "harness" in status_out
    assert supervise.logs(cfg, 2) == 0
    logs_out = capsys.readouterr().out
    assert "two" in logs_out and "three" in logs_out

    stopped: list[str] = []
    monkeypatch.setattr(supervise, "stop", lambda cfg, service: stopped.append(service) or True)
    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: None)
    assert supervise.down(cfg) == 0
    assert stopped == ["harness", "frontdoor", "checkpoint"]


def test_supervise_up_starts_needed_services_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    cfg = _cfg(tmp_path, transport="frontdoor", checkpoint_paths=["MEMORY.md"])
    cfg.var.mkdir(parents=True)
    monkeypatch.setattr(supervise, "_enforce_dna", lambda cfg: 0)
    monkeypatch.setattr(supervise, "buzz_acp_binary", lambda: "/bin/buzz-acp")
    monkeypatch.setattr(supervise.time, "sleep", lambda seconds: None)
    live: set[str] = set()
    spawns: list[tuple[str, list[str], Path]] = []

    def fake_spawn(
        cfg: AgentConfig, service: str, argv: list[str], env: dict[str, str], cwd: Path
    ) -> int:
        del env
        spawns.append((service, argv, cwd))
        live.add(service)
        return 100 + len(spawns)

    monkeypatch.setattr(supervise, "spawn", fake_spawn)
    monkeypatch.setattr(
        supervise, "running_pid", lambda cfg, service: 42 if service in live else None
    )

    assert supervise.up(cfg) == 0
    assert [service for service, _, _ in spawns] == ["frontdoor", "checkpoint", "harness"]
    assert spawns[-1][1] == ["/bin/buzz-acp"]
    assert "buzz-acp up" in capsys.readouterr().out

    monkeypatch.setattr(supervise, "_enforce_dna", lambda cfg: 1)
    assert supervise.up(cfg) == 1

    monkeypatch.setattr(supervise, "_enforce_dna", lambda cfg: 0)
    monkeypatch.setattr(supervise, "buzz_acp_binary", lambda: None)
    assert supervise.up(cfg) == 1


def test_supervise_frontdoor_and_harness_failure_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(supervise, "ROOT", tmp_path)
    cfg = _cfg(tmp_path, transport="frontdoor", checkpoint_paths=[])
    # Force var paths under the patched ROOT so relative_to(ROOT) succeeds.
    monkeypatch.setattr(
        AgentConfig,
        "var",
        property(lambda self: tmp_path / "var" / self.name),
    )
    cfg.var.mkdir(parents=True)
    monkeypatch.setattr(supervise.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(supervise, "running_pid", lambda cfg, service: None)
    monkeypatch.setattr(supervise, "spawn", lambda *args, **kwargs: 123)

    assert supervise._ensure_frontdoor(_cfg(tmp_path, transport="direct")) == 0
    assert supervise._ensure_frontdoor(cfg) == 1
    assert "front door failed" in capsys.readouterr().out

    # Frontdoor appears up so harness failure path is reachable.
    monkeypatch.setattr(
        supervise,
        "running_pid",
        lambda cfg, service: 9 if service == "frontdoor" else None,
    )
    monkeypatch.setattr(supervise, "_enforce_dna", lambda cfg: 0)
    monkeypatch.setattr(supervise, "buzz_acp_binary", lambda: "/bin/buzz-acp")
    supervise.log_file(cfg, "harness").write_text("a\nb\nc\n", encoding="utf-8")
    assert supervise.up(cfg) == 1
    assert "buzz-acp exited" in capsys.readouterr().out


def test_supervise_command_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(supervise, "up", lambda cfg: calls.append("up") or 10)
    monkeypatch.setattr(supervise, "down", lambda cfg: calls.append("down") or 0)
    monkeypatch.setattr(supervise, "status", lambda cfg: calls.append("status") or 11)
    monkeypatch.setattr(supervise, "logs", lambda cfg, lines: calls.append(f"logs:{lines}") or 12)
    monkeypatch.setattr(supervise, "doctor", lambda cfg: calls.append("doctor") or 13)
    monkeypatch.setattr(selftest, "run", lambda name: calls.append(f"selftest:{name}"))
    monkeypatch.setattr(engram, "render", lambda cfg: "rendered\n")
    monkeypatch.setattr(engram, "publish", lambda cfg: calls.append("publish") or 14)

    assert supervise._agent_command("up", cfg, 25) == 10
    assert supervise._agent_command("restart", cfg, 25) == 10
    assert supervise._agent_command("status", cfg, 25) == 11
    assert supervise._agent_command("logs", cfg, 7) == 12
    assert supervise._agent_command("selftest", cfg, 25) == 0
    assert supervise._agent_command("engram", cfg, 25) == 0
    assert supervise._agent_command("engram", cfg, 25, publish=True) == 14
    assert supervise._agent_command("unknown", cfg, 25) == 0
    assert "rendered" in capsys.readouterr().out
    assert calls == [
        "up",
        "down",
        "up",
        "status",
        "logs:7",
        "selftest:scout",
        "publish",
        "doctor",
    ]


def test_supervise_hostwide_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(supervise, "install", lambda: 20)
    monkeypatch.setattr(runtime, "status", lambda: 21)
    monkeypatch.setattr(runtime, "check", lambda: 22)
    monkeypatch.setattr(runtime, "install", lambda: 23)
    monkeypatch.setattr(scaffold, "init", lambda name, root: 24 if name == "scout" else 99)
    monkeypatch.setattr(upstream, "check", lambda: 25)
    monkeypatch.setattr(upstream, "update", lambda target: 26 if target == "v1.0.0" else 27)

    assert supervise._hostwide("install", None) == 20
    assert supervise._hostwide("runtime", None) == 21
    assert supervise._hostwide("runtime", "check") == 22
    assert supervise._hostwide("runtime", "install") == 23
    assert supervise._hostwide("upstream-check", None) == 25
    assert supervise._hostwide("upstream-update", "v1.0.0") == 26
    assert supervise._hostwide("other", None) is None

    monkeypatch.setattr(config, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="agnova init <name>"):
        supervise._hostwide("init", None)
    assert supervise._hostwide("init", "scout") == 24


def test_supervise_install_and_run_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        upstream, "read_lock", lambda: {"repo": "repo", "ref": "v1", "sha": "abcdef"}
    )
    steps: list[list[str]] = []
    monkeypatch.setattr(supervise, "run_step", lambda cmd: steps.append(list(cmd)))

    assert supervise.install() == 0
    assert steps[0][:3] == ["git", "clone", "--filter=blob:none"]
    assert steps[-1] == ["npm", "install", "-g", "@agentclientprotocol/claude-agent-acp"]
    assert "installed" in capsys.readouterr().out


def test_supervise_run_step_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        supervise.subprocess,
        "run",
        lambda cmd, check=False: SimpleNamespace(returncode=0),
    )
    supervise.run_step(["true"])
    assert "true" in capsys.readouterr().out

    monkeypatch.setattr(
        supervise.subprocess,
        "run",
        lambda cmd, check=False: SimpleNamespace(returncode=1),
    )
    with pytest.raises(SystemExit, match="failed: false"):
        supervise.run_step(["false"])
