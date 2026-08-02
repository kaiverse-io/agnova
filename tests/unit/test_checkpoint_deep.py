from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agnova import checkpoint


def _init_git(home: Path) -> None:
    subprocess.run(["git", "init"], cwd=home, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=home,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=home, check=True, capture_output=True)
    (home / "MEMORY.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "MEMORY.md"], cwd=home, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=home, check=True, capture_output=True)


def test_tail_returns_last_line_or_empty() -> None:
    assert checkpoint.tail("") == ""
    assert checkpoint.tail("one\ntwo\nthree") == "three"


def test_in_progress_detects_git_operations(tmp_path: Path) -> None:
    _init_git(tmp_path)
    assert checkpoint.in_progress(tmp_path) is False
    (tmp_path / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    assert checkpoint.in_progress(tmp_path) is True


def test_in_progress_when_not_a_repo(tmp_path: Path) -> None:
    assert checkpoint.in_progress(tmp_path) is True


def test_dirty_and_has_remote(tmp_path: Path) -> None:
    _init_git(tmp_path)
    assert checkpoint.dirty(tmp_path, ["MEMORY.md"]) is False
    assert checkpoint.has_remote(tmp_path) is False
    (tmp_path / "MEMORY.md").write_text("changed\n", encoding="utf-8")
    assert checkpoint.dirty(tmp_path, ["MEMORY.md"]) is True
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example/repo.git"],
        cwd=tmp_path,
        check=True,
    )
    assert checkpoint.has_remote(tmp_path) is True


def test_create_bundle_success_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git(tmp_path)
    bundle = tmp_path / "out.bundle"
    assert checkpoint.create_bundle(tmp_path, bundle) is True
    assert bundle.is_file()
    monkeypatch.setattr(checkpoint, "run", lambda *args, **kwargs: (1, "bundle failed"))
    assert checkpoint.create_bundle(tmp_path, tmp_path / "missing.bundle") is False


def test_upload_bundle_validates_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(b"bundle")
    assert not checkpoint.upload_bundle("ftp://bad", "tok", "ab" * 32, bundle)
    assert "invalid AGENT_CHECKPOINT_UPLOAD_URL" in capsys.readouterr().out
    assert not checkpoint.upload_bundle("http:///no-host", "tok", "ab" * 32, bundle)


def test_upload_bundle_posts_successfully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(b"payload")
    captured: dict[str, object] = {}

    class Response:
        status = 201

        def read(self) -> bytes:
            return b""

    class Conn:
        def __init__(self, host: str, port: int | None = None, timeout: int | None = None) -> None:
            self.host = host

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            captured["request"] = (method, path, body, headers)

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(checkpoint.http.client, "HTTPConnection", Conn)
    assert checkpoint.upload_bundle("http://cp.example/upload", "tok", "ab" * 32, bundle)
    assert captured["request"][0] == "POST"
    assert "uploaded git bundle" in capsys.readouterr().out


def test_upload_bundle_https_and_http_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(b"x")

    class Response:
        status = 500

        def read(self) -> bytes:
            return b""

    class HttpsConn:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            self.host = host

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(checkpoint.http.client, "HTTPSConnection", HttpsConn)
    assert not checkpoint.upload_bundle("https://cp.example/upload", "tok", "ab" * 32, bundle)
    assert "HTTP 500" in capsys.readouterr().out

    def raise_os(*args: object, **kwargs: object) -> None:
        raise OSError("network down")

    monkeypatch.setattr(checkpoint.http.client, "HTTPConnection", raise_os)
    assert not checkpoint.upload_bundle("http://cp.example/upload", "tok", "ab" * 32, bundle)
    assert "bundle upload failed" in capsys.readouterr().out


def test_push_after_commit_without_remote(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / "MEMORY.md").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "add", "MEMORY.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "local"], cwd=tmp_path, check=True, capture_output=True)
    logs: list[str] = []
    checkpoint._push_after_commit(tmp_path, logs.append)
    assert any("no git remote" in line for line in logs)


def test_push_after_commit_rebases_on_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
        del cwd, timeout
        calls.append(args)
        if args[:2] == ["git", "push"] and len(calls) == 1:
            return (1, "rejected\n")
        if args[:3] == ["git", "pull", "--rebase"]:
            return (0, "ok")
        if args[:2] == ["git", "push"] and len(calls) == 3:
            return (0, "pushed")
        return (0, "ok")

    monkeypatch.setattr(checkpoint, "run", fake_run)
    monkeypatch.setattr(checkpoint, "has_remote", lambda home: True)
    logs: list[str] = []
    checkpoint._push_after_commit(tmp_path, logs.append)
    assert calls[0][:2] == ["git", "push"]
    assert calls[1][:3] == ["git", "pull", "--rebase"]
    assert any("after rebase" in line for line in logs)


def test_maybe_upload_bundle_skips_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_CHECKPOINT_UPLOAD_URL", raising=False)
    checkpoint._maybe_upload_bundle(
        tmp_path, print, upload_url=None, upload_token=None, secret_key=None
    )
    assert capsys.readouterr().out == ""
    checkpoint._maybe_upload_bundle(
        tmp_path, print, upload_url="http://cp.example", upload_token="", secret_key="11" * 32
    )
    assert "token or key missing" in capsys.readouterr().out


def test_checkpoint_once_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_git(tmp_path)
    logs: list[str] = []
    (tmp_path / ".git" / "REBASE_HEAD").write_text("x\n", encoding="utf-8")
    assert not checkpoint.checkpoint_once(tmp_path, ["MEMORY.md"], "scout", logs.append)
    assert any("merge or rebase" in line for line in logs)
    (tmp_path / ".git" / "REBASE_HEAD").unlink()
    assert not checkpoint.checkpoint_once(tmp_path, ["MEMORY.md"], "scout", logs.append)
    (tmp_path / "MEMORY.md").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(checkpoint, "_push_after_commit", lambda home, log: None)
    monkeypatch.setattr(checkpoint, "_maybe_upload_bundle", lambda *args, **kwargs: None)
    assert checkpoint.checkpoint_once(tmp_path, ["MEMORY.md"], "scout", lambda *_: None)
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "scout: checkpoint memory" in log.stdout


def test_checkpoint_once_handles_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / "MEMORY.md").write_text("changed\n", encoding="utf-8")
    logs: list[str] = []
    real_run = checkpoint.run

    def fail_add(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
        if args[:3] == ["git", "add", "--"]:
            return (1, "add failed")
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(checkpoint, "run", fail_add)
    assert not checkpoint.checkpoint_once(tmp_path, ["MEMORY.md"], "scout", logs.append)
    assert any("git add failed" in line for line in logs)

    def fail_commit(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
        if args[:2] == ["git", "commit"]:
            return (1, "nothing to commit")
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(checkpoint, "run", fail_commit)
    (tmp_path / "MEMORY.md").write_text("again\n", encoding="utf-8")
    assert not checkpoint.checkpoint_once(tmp_path, ["MEMORY.md"], "scout", logs.append)
    assert any("nothing committed" in line for line in logs)


def test_main_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        checkpoint.agent_config,
        "load",
        lambda agent: SimpleNamespace(
            checkpoint_paths=[],
            home=tmp_path,
            name="scout",
            checkpoint_interval=60,
            checkpoint_upload_url=None,
            checkpoint_token=None,
            secret_key=None,
        ),
    )
    monkeypatch.setattr(checkpoint.sys, "argv", ["checkpoint"])
    checkpoint.main()

    calls: list[bool] = []
    monkeypatch.setattr(checkpoint, "checkpoint_once", lambda *args, **kwargs: calls.append(True))
    monkeypatch.setattr(
        checkpoint.agent_config,
        "load",
        lambda agent: SimpleNamespace(
            checkpoint_paths=["MEMORY.md"],
            home=tmp_path,
            name="scout",
            checkpoint_interval=1,
            checkpoint_upload_url=None,
            checkpoint_token=None,
            secret_key="11" * 32,
        ),
    )
    monkeypatch.setattr(checkpoint.sys, "argv", ["checkpoint", "scout", "--once"])
    checkpoint.main()
    assert calls == [True]

    n = {"v": 0}

    def flaky(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        n["v"] += 1
        if n["v"] == 1:
            raise subprocess.TimeoutExpired(cmd="git", timeout=120)
        if n["v"] == 2:
            raise RuntimeError("boom")
        raise KeyboardInterrupt

    monkeypatch.setattr(checkpoint, "checkpoint_once", flaky)
    monkeypatch.setattr(checkpoint.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(checkpoint.sys, "argv", ["checkpoint", "scout"])
    with pytest.raises(KeyboardInterrupt):
        checkpoint.main()
    assert n["v"] == 3


def test_maybe_upload_bundle_upload_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_git(tmp_path)
    uploaded: list[str] = []
    monkeypatch.setattr(
        checkpoint,
        "upload_bundle",
        lambda url, token, pubkey, bundle, log=print: uploaded.append(url) or True,
    )
    checkpoint._maybe_upload_bundle(
        tmp_path,
        lambda *_: None,
        upload_url="http://cp.example/upload",
        upload_token="tok",
        secret_key="11" * 32,
    )
    assert uploaded == ["http://cp.example/upload"]
    monkeypatch.setattr(checkpoint, "create_bundle", lambda home, dest: False)
    checkpoint._maybe_upload_bundle(
        tmp_path, print, upload_url="http://cp.example", upload_token="tok", secret_key="11" * 32
    )
    assert "git bundle create failed" in capsys.readouterr().out
