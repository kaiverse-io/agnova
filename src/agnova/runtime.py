"""The agnova version an agent runs on.

An agent repository declares its runtime in `runtime.lock` — repo, ref, sha —
and nothing else. No bootstrap script, no vendored code, no per-agent tooling:
the same three lines of data in every agent, and every behaviour that reads them
lives here.

Deliberately not a self-updater. Moving the pin is a commit, reviewed like any
other change to behaviour, which is the rule agnova applies to the `buzz-acp`
version it rents one level down.

The chicken-and-egg is real but confined to one line: something must install
*some* agnova before `agnova runtime` can run. That is the environment setup
script's single job, and it needs no file in the agent repo:

    pip install "agnova @ git+<repo>@$(sed -n 's/^sha = //p' runtime.lock)"
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import subprocess
import sys

from agnova.config import ROOT

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"

LOCK = "runtime.lock"


def read_lock() -> dict[str, str]:
    """Parse the agent's `runtime.lock`. Same shape as agnova's `upstream.lock`."""
    path = ROOT / LOCK
    if not path.exists():
        raise SystemExit(
            f"no {LOCK} in {ROOT}.\n"
            f"An agent repository declares its runtime there. "
            f"Create one with `agnova init <name>`."
        )
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    missing = {"repo", "ref", "sha"} - values.keys()
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(sorted(missing))}")
    return values


def installed_sha() -> str | None:
    """The commit pip actually installed, from the direct_url.json it records.

    The package version is useless for this: it does not change between commits,
    so pip sees the same version present and skips the install — the pin then
    silently fails to apply. The recorded commit is the only honest answer.
    """
    try:
        raw = metadata.distribution("agnova").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        return str(json.loads(raw)["vcs_info"]["commit_id"])
    except (KeyError, ValueError):
        return None


def status() -> int:
    lock = read_lock()
    have = installed_sha()
    print(f"{DIM}pinned   {RESET}{lock['ref']}  {DIM}{lock['sha'][:12]}{RESET}")
    if have is None:
        print(f"{DIM}installed{RESET} (not a pinned install — editable or from a path)")
        return 0
    print(f"{DIM}installed{RESET} {DIM}{have[:12]}{RESET}")
    if have == lock["sha"]:
        print(f"{GREEN}✓{RESET} match")
        return 0
    print(f"{YELLOW}!{RESET} drift — run `agnova runtime install`")
    return 1


def check() -> int:
    lock = read_lock()
    print(f"{DIM}pinned{RESET} {lock['ref']}  {DIM}{lock['sha'][:12]}{RESET}")
    out = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        ["git", "ls-remote", lock["repo"], "refs/heads/main"],  # noqa: S607 — git from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"could not reach {lock['repo']} — offline, or no access from this host")
    remote = out.stdout.split()[0]
    print(f"{DIM}main  {RESET} {DIM}{remote[:12]}{RESET}")
    if remote == lock["sha"]:
        print(f"{GREEN}✓{RESET} up to date")
        return 0
    print(f"\n{YELLOW}!{RESET} the runtime has moved. To adopt it, edit {LOCK} and commit —")
    print("  review the diff first; this is not applied automatically.")
    return 0


def install() -> int:
    """Install exactly the pinned build, replacing whatever is here."""
    lock = read_lock()
    if installed_sha() == lock["sha"]:
        print(f"{GREEN}✓{RESET} already at {lock['ref']} ({lock['sha'][:12]})")
        return 0
    url = f"agnova @ git+{lock['repo'].removesuffix('.git')}@{lock['sha']}"
    print(f"{DIM}installing{RESET} {lock['ref']} {DIM}{lock['sha'][:12]}{RESET}")
    # --force-reinstall because the version string does not change between
    # commits; without it pip sees agnova present and does nothing.
    out = subprocess.run(  # noqa: S603 — fixed argv, shell=False
        [sys.executable, "-m", "pip", "install", "--quiet", "--force-reinstall", url],
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit("pip install failed")
    print(f"{GREEN}✓{RESET} installed {lock['sha'][:12]}")
    return 0
