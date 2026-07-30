#!/usr/bin/env python3
"""Pin, inspect and update the upstream build this agent runs on.

This runtime deliberately owns almost no agent behaviour — filtering, dedup,
queueing, prompt assembly and presence all belong to `buzz-acp`. That makes the
upstream version the most consequential fact about how the agent behaves, which
is exactly why it must not float.

`./agent install` used to clone upstream's default branch. That meant every fresh
container silently ran whatever shipped that day, and a behaviour change would
arrive with no diff anywhere in this repo. Now the version lives in
`upstream.lock` and moves only when someone moves it.

    just upstream-check            what is pinned, and what is available
    bin/agnova upstream-update           move to the newest release
    bin/agnova upstream-update v0.5.1    move to a specific ref, or roll back
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LOCK = Path(__file__).resolve().parent / "upstream.lock"
# S108: a build checkout, not a secret store — it holds a public git clone and
# is recreated by `install`. Deliberately outside the repo so a reclaimed
# sandbox discards it rather than leaving a stale tree behind.
CHECKOUT = Path("/tmp/buzz")  # noqa: S108

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# vMAJOR.MINOR.PATCH only. Release candidates and one-off tags are not things
# an unattended container should ever land on by accident.
SEMVER_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def read_lock() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_lock(repo: str, ref: str, sha: str) -> None:
    text = LOCK.read_text()
    text = re.sub(r"^repo = .*$", f"repo = {repo}", text, flags=re.M)
    text = re.sub(r"^ref = .*$", f"ref = {ref}", text, flags=re.M)
    text = re.sub(r"^sha = .*$", f"sha = {sha}", text, flags=re.M)
    LOCK.write_text(text)


def remote_tags(repo: str) -> list[tuple[tuple[int, int, int], str, str]]:
    """Every release tag upstream publishes, newest last."""
    # S603/S607: `git` is resolved from PATH deliberately — pinning an absolute
    # path would break every environment that installs it somewhere else. The
    # only interpolated value is the repo URL from the lock file.
    argv = ["git", "ls-remote", "--tags", "--refs", repo]  # noqa: S607
    out = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise SystemExit(f"could not reach {repo}:\n{out.stderr.strip()}")

    releases = []
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        match = SEMVER_TAG.match(name)
        if match:
            releases.append((tuple(int(p) for p in match.groups()), name, sha.strip()))
    return sorted(releases)


def check() -> int:
    lock = read_lock()
    releases = remote_tags(lock["repo"])
    if not releases:
        raise SystemExit("upstream publishes no release tags — cannot resolve a version")

    latest_version, latest_name, latest_sha = releases[-1]
    print(f"{DIM}pinned {RESET}{lock['ref']}  {DIM}{lock['sha'][:12]}{RESET}")
    print(f"{DIM}latest {RESET}{latest_name}  {DIM}{latest_sha[:12]}{RESET}")

    if lock["sha"] == latest_sha:
        print(f"{GREEN}✓{RESET} up to date")
        return 0

    newer = [
        name
        for version, name, _ in releases
        if SEMVER_TAG.match(lock["ref"])
        and version > tuple(int(p) for p in SEMVER_TAG.match(lock["ref"]).groups())
    ]
    if newer:
        print(f"{YELLOW}!{RESET} {len(newer)} newer release(s): {', '.join(newer[-5:])}")
    else:
        print(
            f"{YELLOW}!{RESET} pinned ref is not a release tag — "
            f"`bin/agnova upstream-update` moves to {latest_name}"
        )
    print(f"\n  bin/agnova upstream-update            -> {latest_name}")
    print("  bin/agnova upstream-update <ref>      -> a specific version, or roll back")
    return 0


def update(target: str | None) -> int:
    lock = read_lock()
    releases = remote_tags(lock["repo"])

    if target is None:
        _, target, sha = releases[-1]
    else:
        match = next((r for r in releases if r[1] == target), None)
        if match is None:
            available = ", ".join(name for _, name, _ in releases[-8:])
            raise SystemExit(f"no release tag {target!r}. Recent: {available}")
        sha = match[2]

    if sha == lock["sha"]:
        print(f"{GREEN}✓{RESET} already on {target}")
        return 0

    write_lock(lock["repo"], target, sha)
    print(f"{GREEN}✓{RESET} pinned {lock['ref']} -> {target} ({sha[:12]})")
    print(f"\n{DIM}The lock is updated but nothing is built yet. Next:{RESET}")
    print("  ./agent install         rebuild at the new pin")
    print("  bin/agnova restart         swap the running agent onto it")
    print("  bin/agnova selftest        confirm the transport still works")
    print(f"\n{DIM}Roll back with: just upstream-update {lock['ref']}{RESET}")
    return 0


def clone_args() -> int:
    """Emit the shell for `./agent install` to fetch exactly the pinned commit.

    A tag alone is not enough: tags can be moved upstream. Fetching the tag and
    then checking out the recorded sha means the lock is what actually decides
    which code runs.
    """
    lock = read_lock()
    print(
        f"rm -rf {CHECKOUT} && "
        f"git clone --filter=blob:none --no-checkout {lock['repo']} {CHECKOUT} && "
        f"git -C {CHECKOUT} fetch --depth 1 origin {lock['sha']} && "
        f"git -C {CHECKOUT} checkout --detach {lock['sha']}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["check", "update", "clone-args"])
    parser.add_argument("ref", nargs="?")
    args = parser.parse_args()

    if args.command == "check":
        sys.exit(check())
    if args.command == "update":
        sys.exit(update(args.ref))
    sys.exit(clone_args())


if __name__ == "__main__":
    main()
