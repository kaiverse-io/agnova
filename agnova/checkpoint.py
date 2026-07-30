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
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agnova import config as agent_config  # noqa: E402


def tail(out: str) -> str:
    """Last line of git output — enough to diagnose, short enough to log."""
    return out.splitlines()[-1] if out else ""


def run(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    result = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
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


def checkpoint_once(home: Path, paths: list[str], label: str, log=print) -> bool:
    """Commit and push the configured paths. Returns True if anything was saved."""
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

    code, out = run(["git", "push"], home, timeout=180)
    if code == 0:
        log("checkpoint: committed and pushed")
        return True

    # The remote moved. Rebase our checkpoint on top and try once more; if that
    # does not work, leave the commit sitting locally and say so — it will go
    # out on the next tick, and a loud log beats a silent divergence.
    log(f"checkpoint: push rejected, rebasing — {tail(out)}")
    code, out = run(["git", "pull", "--rebase"], home, timeout=180)
    if code != 0:
        log(f"checkpoint: rebase failed, commit is local only — {tail(out)}")
        return False
    code, out = run(["git", "push"], home, timeout=180)
    if code == 0:
        log("checkpoint: committed and pushed after rebase")
        return True
    log(f"checkpoint: push still failing, commit is local only — {tail(out)}")
    return False


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

    if args.once:
        checkpoint_once(cfg.home, cfg.checkpoint_paths, cfg.name, log)
        return

    while True:
        try:
            checkpoint_once(cfg.home, cfg.checkpoint_paths, cfg.name, log)
        except subprocess.TimeoutExpired:
            log("checkpoint: git timed out — will retry next tick")
        except Exception as exc:  # a checkpoint loop must never be what kills the agent
            log(f"checkpoint: unexpected error — {exc}")
        time.sleep(cfg.checkpoint_interval)


if __name__ == "__main__":
    main()
