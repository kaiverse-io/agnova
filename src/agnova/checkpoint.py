#!/usr/bin/env python3
"""Checkpoint — make an agent's memory survive the machine it runs on.

A cloud sandbox is reclaimed after a period of inactivity, and the replacement
starts from the last pushed commit. Anything the agent wrote and did not push is
simply gone. That is not a rare edge case: it happened three times in a single
day of building this runtime.

So this loop commits and pushes the agent's own files on an interval. It runs no
model and costs no tokens — it is `git status`, and on the rare tick where
something changed, a commit and a push.

**It knows nothing about memory.** It is given a list of paths and told to keep
them durable. What those paths mean — a daily log, a knowledge base, a graph —
is the agent's business, never the runtime's.

**Opt-in, and explicitly scoped.** With no configured paths this does nothing at
all. Committing an entire home directory on a timer would sooner or later commit
somebody's half-finished work, so the paths must be named rather than inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agnova import config as agent_config  # noqa: E402
from agnova import nostr  # noqa: E402


def tail(out: str) -> str:
    """Last line of git output — enough to diagnose, short enough to log."""
    return out.splitlines()[-1] if out else ""


def run(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    # S603: argv is built here from a fixed list of git subcommands and the
    # operator-configured paths; nothing crosses from the network. shell=False.
    result = subprocess.run(  # noqa: S603
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def in_progress(home: Path) -> bool:
    """Is a merge, rebase or bisect underway? If so, keep hands off.

    Someone — a human, or a session — is mid-operation. Committing on top of
    that turns a recoverable state into a confusing one.
    """
    code, git_dir = run(["git", "rev-parse", "--git-dir"], home)
    if code != 0:
        return True
    root = (home / git_dir).resolve()
    return any(
        (root / marker).exists()
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")
    )


def dirty(home: Path, paths: list[str]) -> bool:
    code, out = run(["git", "status", "--porcelain", "--", *paths], home)
    return code == 0 and bool(out.strip())


def has_remote(home: Path) -> bool:
    code, out = run(["git", "remote"], home)
    return code == 0 and bool(out.strip())


def create_bundle(home: Path, dest: Path) -> bool:
    """Write a git bundle of HEAD to dest. Returns False on failure."""
    code, out = run(["git", "bundle", "create", str(dest), "HEAD"], home)
    return code == 0 and dest.is_file()


def upload_bundle(
    url: str,
    token: str,
    pubkey_hex: str,
    bundle: Path,
    log: Callable[..., None] = print,
) -> bool:
    """POST a git bundle to a generic control-plane URL (HTTP only; no in-process imports)."""
    body = bundle.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        log(f"checkpoint: invalid AGENT_CHECKPOINT_UPLOAD_URL — {url!r}")
        return False
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(body)),
        "X-Agent-Pubkey": pubkey_hex,
        "X-Bundle-Format": "git-bundle",
        "X-Content-Sha256": digest,
    }
    try:
        if parsed.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=60
            )
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
    except OSError as exc:
        log(f"checkpoint: bundle upload failed — {exc}")
        return False
    if status in (200, 201):
        log(f"checkpoint: uploaded git bundle ({len(body)} bytes)")
        return True
    log(f"checkpoint: bundle upload HTTP {status}")
    return False


def _push_after_commit(home: Path, log: Callable[..., None]) -> None:
    """Push HEAD; one rebase retry if the remote moved."""
    if not has_remote(home):
        log("checkpoint: committed (no git remote — skipping push)")
        return
    code, out = run(["git", "push"], home, timeout=180)
    if code == 0:
        log("checkpoint: committed and pushed")
        return
    # The remote moved. Rebase once; leave the commit local if that fails.
    log(f"checkpoint: push rejected, rebasing — {tail(out)}")
    code, out = run(["git", "pull", "--rebase"], home, timeout=180)
    if code != 0:
        log(f"checkpoint: rebase failed, commit is local only — {tail(out)}")
        return
    code, out = run(["git", "push"], home, timeout=180)
    if code == 0:
        log("checkpoint: committed and pushed after rebase")
    else:
        log(f"checkpoint: push still failing, commit is local only — {tail(out)}")


def _maybe_upload_bundle(
    home: Path,
    log: Callable[..., None],
    *,
    upload_url: str | None,
    upload_token: str | None,
    secret_key: str | None,
) -> None:
    url = (upload_url or os.environ.get("AGENT_CHECKPOINT_UPLOAD_URL") or "").strip()
    token = (upload_token or os.environ.get("AGENT_CHECKPOINT_TOKEN") or "").strip()
    if not url:
        return
    if not token or not secret_key:
        log("checkpoint: upload URL set but token or key missing — skipping upload")
        return
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "memory.bundle"
        if not create_bundle(home, bundle):
            log("checkpoint: git bundle create failed")
            return
        pubkey = nostr.public_key_hex(nostr.load_secret_key(secret_key))
        upload_bundle(url, token, pubkey, bundle, log)


def checkpoint_once(
    home: Path,
    paths: list[str],
    label: str,
    log: Callable[..., None] = print,
    *,
    upload_url: str | None = None,
    upload_token: str | None = None,
    secret_key: str | None = None,
) -> bool:
    """Commit paths, optionally push, optionally upload a git bundle."""
    if in_progress(home):
        log("checkpoint: a merge or rebase is in progress — skipping this tick")
        return False
    if not dirty(home, paths):
        return False

    code, out = run(["git", "add", "--", *paths], home)
    if code != 0:
        log(f"checkpoint: git add failed — {out}")
        return False

    # Staging is per-path, but another writer may have staged something else in
    # the meantime; commit only what we staged from our own paths.
    code, out = run(["git", "commit", "-m", f"{label}: checkpoint memory", "--", *paths], home)
    if code != 0:
        first = out.splitlines()[0] if out else "no output"
        log(f"checkpoint: nothing committed — {first}")
        return False

    _push_after_commit(home, log)
    _maybe_upload_bundle(
        home,
        log,
        upload_url=upload_url,
        upload_token=upload_token,
        secret_key=secret_key,
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("agent", nargs="?")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args()

    cfg = agent_config.load(args.agent)
    if not cfg.checkpoint_paths:
        print("checkpoint: no paths configured — nothing to do")
        return

    def log(message: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)

    log(
        f"checkpoint: watching {', '.join(cfg.checkpoint_paths)} in {cfg.home} "
        f"every {cfg.checkpoint_interval}s"
    )

    def once() -> None:
        checkpoint_once(
            cfg.home,
            cfg.checkpoint_paths,
            cfg.name,
            log,
            upload_url=cfg.checkpoint_upload_url,
            upload_token=cfg.checkpoint_token,
            secret_key=cfg.secret_key,
        )

    if args.once:
        once()
        return

    while True:
        try:
            once()
        except subprocess.TimeoutExpired:
            log("checkpoint: git timed out — will retry next tick")
        except Exception as exc:  # a checkpoint loop must never be what kills the agent
            log(f"checkpoint: unexpected error — {exc}")
        time.sleep(cfg.checkpoint_interval)


if __name__ == "__main__":
    main()
