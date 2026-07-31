"""Render an agent's NIP-AE core engram from its repository.

Buzz injects a `core` engram into every turn. That makes it a source of identity,
and an agent must not have two: a hand-written engram silently disagrees with the
repository the moment `SOUL.md` changes, with no diff to show it.

So the engram is *generated* — a projection of the files, never edited directly.
Identity changes by editing the repository; this re-renders and publishes it. One
source, two surfaces, which also preserves the one thing engrams are genuinely
good at: an owner reading an agent's identity without access to the host it runs
on.

    agnova engram <agent>             print what would be published
    agnova engram <agent> --publish   publish it, if it differs from what is live

Which files make up the projection is per-agent, declared in `agents/<name>.env`
as `AGENT_ENGRAM_PATHS`. The runtime renders whatever is named and never decides
what identity is.
"""

from __future__ import annotations

import hashlib
import subprocess
import time

from agnova.config import AgentConfig

GREEN = "\033[32m"
DIM = "\033[2m"
RESET = "\033[0m"


def render(cfg: AgentConfig) -> str:
    """Concatenate the declared identity files, in the order declared."""
    parts: list[str] = []
    for rel in cfg.engram_paths:
        path = cfg.home / rel
        if not path.exists():
            raise SystemExit(f"engram source missing: {path}")
        parts.append(path.read_text().strip())
    if not parts:
        raise SystemExit(
            f"no AGENT_ENGRAM_PATHS set for {cfg.name}.\n"
            f"Name the files that define this agent's identity, e.g.\n"
            f"  AGENT_ENGRAM_PATHS=SOUL.md,USER.md"
        )
    return "\n\n---\n\n".join(parts) + "\n"


def _buzz(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, shell=False
        ["buzz", *args],  # noqa: S607 — buzz resolved from PATH by design
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def publish(cfg: AgentConfig) -> int:
    if not cfg.owner_pubkey:
        raise SystemExit(
            f"BUZZ_OWNER_PUBKEY is not set for {cfg.name} — `buzz mem` writes need an owner."
        )
    body = render(cfg)
    digest = hashlib.sha256(body.encode()).hexdigest()

    live = _buzz(["mem", "hash", "core", "--owner", cfg.owner_pubkey])
    if live.returncode == 0 and live.stdout.strip() == digest:
        print(f"{GREEN}✓{RESET} core engram already matches the repo — nothing to publish")
        return 0

    # Two `buzz` calls minting a NIP-98 header in the same wall-clock second can
    # trip the relay's replay guard on the second one. A second of slack is
    # cheap; a failed publish is not.
    time.sleep(1)

    out = _buzz(["mem", "set", "core", "-", "--owner", cfg.owner_pubkey], stdin=body)
    if out.returncode != 0:
        raise SystemExit(f"publish failed: {out.stderr.strip() or out.stdout.strip()}")
    print(f"{GREEN}✓{RESET} published core engram {DIM}sha256 {digest[:12]}{RESET}")
    return 0
