"""Create a new agent repository.

Every agent has the same shape. Not by convention — by construction, so that a
second agent is never a hand-copy of the first, and nothing about the runtime is
maintained per-agent.

    agnova init scout --owner <pubkey> --relay wss://your.relay

What it writes:

    runtime.lock        which agnova this agent runs on (data, never code)
    agents/<name>.env   the agent's config — committed, never secret
    AGENTS.md           the persona contract, read by whatever runtime runs it
    CLAUDE.md           one-line bridge so Claude Code reads AGENTS.md
    SOUL.md             who the agent is
    MEMORY.md           curated long-term memory
    memory/             the daily log
    skills/             what it knows how to do
    .gitignore          runtime state that must never be committed

What it does NOT write: any runtime code. An agent repository contains its
identity, its memory and one lock file. Everything executable comes from the
installed package.
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata as metadata
import json
from pathlib import Path

GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

DEFAULT_REPO = "https://github.com/km2411/agnova.git"


def _running_version() -> tuple[str, str]:
    """The agnova this process is running, so a new agent pins what created it.

    The commit comes from pip's `direct_url.json`, which exists only for a VCS
    install. An editable or path install has no commit to record — the scaffold
    still runs, but the new agent is left with an unresolvable pin, so say so
    loudly rather than write a lock file that quietly cannot be installed.
    """
    try:
        raw = metadata.distribution("agnova").read_text("direct_url.json")
        sha = str(json.loads(raw)["vcs_info"]["commit_id"]) if raw else ""
    except Exception:
        sha = ""
    try:
        ref = f"v{metadata.version('agnova')}"
    except metadata.PackageNotFoundError:
        ref = "main"
    return ref, sha


def _write(path: Path, content: str, written: list[str], skipped: list[str]) -> None:
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    written.append(str(path))


def init(name: str, root: Path, owner: str = "", relay: str = "") -> int:
    name = name.strip().lower()
    if not name.isidentifier():
        raise SystemExit(f"agent name {name!r} must be a plain identifier: letters, digits, _")

    ref, sha = _running_version()
    today = dt.date.today()
    written: list[str] = []
    skipped: list[str] = []

    def w(rel: str, content: str) -> None:
        _write(root / rel, content, written, skipped)

    w(
        "runtime.lock",
        f"""# Which agnova build this agent runs on.
#
# The runtime is a dependency, never vendored: a vendored copy shadows the
# installed package on sys.path and drifts silently. This file is the only
# runtime-related thing an agent repository contains, and it is data, not code.
#
#   agnova runtime status    pinned vs. installed
#   agnova runtime check     pinned vs. upstream
#   agnova runtime install   install exactly the pin
#
# `sha` is authoritative — it is what gets installed. `ref` is the human label.
# Moving the pin is a commit, reviewed like any other change to behaviour.

repo = {DEFAULT_REPO}
ref = {ref}
sha = {sha}
""",
    )

    w(
        f"agents/{name}.env",
        f"""# {name.capitalize()} — agent configuration.
#
# Everything here is public and committed. Secrets live in the environment:
# BUZZ_PRIVATE_KEY (this agent's identity) and BUZZ_RELAY_URL never appear here.

BUZZ_AGENT_LABEL={name.capitalize()}

# The human this agent serves — NIP-OA attester, and the owner-only gate's owner.
BUZZ_OWNER_PUBKEY={owner}

# owner-only (default) | allowlist | anyone | nobody
BUZZ_ACP_RESPOND_TO=owner-only

# claude-agent-acp (Claude Code) | goose | codex-acp
BUZZ_ACP_AGENT_COMMAND=claude-agent-acp

# This repository is the agent's home: its persona, memory and skills.
BUZZ_AGENT_HOME=.

# auto (default) | direct | frontdoor
# auto uses the transport shim only when an egress proxy is present.
AGNOVA_TRANSPORT=auto

BUZZ_ACP_AGENTS=1
BUZZ_ACP_HEARTBEAT_INTERVAL=0

# The files that make up this agent's published identity. `agnova engram`
# renders these into the NIP-AE core engram Buzz injects each turn, so the
# engram is a projection of the repo rather than a second, ungoverned source.
AGENT_ENGRAM_PATHS=SOUL.md

# What must survive the machine. Named explicitly, never inferred — a timer that
# committed the whole home would eventually commit half-finished work.
AGENT_CHECKPOINT_PATHS=memory/,MEMORY.md
AGENT_CHECKPOINT_INTERVAL=900
""",
    )

    w(
        "AGENTS.md",
        f"""# AGENTS.md — {name.capitalize()}

The persona contract. Runtime-agnostic on purpose: Claude Code, goose and codex
each look for a different file, so this is the single source and the others
bridge to it.

## Session startup

1. If `memory/{today:%Y-%m-%d}.md` (today) does not exist, create it before anything else.
2. Read `SOUL.md` — who you are.
3. Read `MEMORY.md` — what is durably true.
4. Read today's and yesterday's `memory/` entries for recent context.

## Memory

- `memory/YYYY-MM-DD.md` — the daily log. What happened, what was decided.
- `MEMORY.md` — curated long-term memory, promoted from daily notes once durable.

Both are committed by the runtime's checkpoint. Deciding *what* is worth writing
is yours; the runtime only persists what you wrote.

## Where things are

| You want | Look in |
|---|---|
| Who you are | `SOUL.md` |
| Long-term memory | `MEMORY.md` |
| A given day | `memory/YYYY-MM-DD.md` |
| Capabilities | `skills/` |

## Runtime

This repository contains no runtime code. It is run by
[agnova](https://github.com/km2411/agnova), pinned in `runtime.lock`.

```bash
agnova doctor {name}     # diagnose, change nothing
agnova up {name}         # start. Idempotent.
agnova logs {name}
```
""",
    )

    w(
        "CLAUDE.md",
        f"""# {name.capitalize()}

Claude Code loads this file automatically. The instructions live in `AGENTS.md` —
one canonical file, so every runtime reads the same thing.

@AGENTS.md
""",
    )

    w(
        "SOUL.md",
        f"""# SOUL.md — Who {name.capitalize()} Is

_Replace this. An agent that never had its soul written is a chatbot with a name._

## Character

- What this agent is for.
- How it speaks, and how it does not.
- What it refuses to do.

## Boundaries

- What stays private.
- What needs asking before acting — anything that leaves the machine.

---

This file is the agent's identity. If it changes, that is a deliberate edit, not
a side effect of a conversation.
""",
    )

    w(
        "MEMORY.md",
        f"""# MEMORY.md — {name.capitalize()}'s long-term memory

Curated. Promoted from `memory/` once something proves durable — not a dumping
ground for everything that happened.
""",
    )

    w(
        f"memory/{today:%Y-%m-%d}.md",
        f"""# {today:%A, %-d %B %Y}

## Context

## Decisions

## Done

## Open
""",
    )

    w("skills/.gitkeep", "")

    w(
        ".gitignore",
        """# Runtime state. Never committed — it is per-machine and per-process.
var/
__pycache__/
*.pyc
.venv/
""",
    )

    w(
        "README.md",
        f"""# {name.capitalize()}

An agent. Identity, memory and skills — no runtime code.

The runtime is [agnova](https://github.com/km2411/agnova), installed as a
dependency and pinned in `runtime.lock`.

## Running it

Two secrets, in the environment and never in this repository:

```bash
export BUZZ_PRIVATE_KEY=<this agent's secret key>
export BUZZ_RELAY_URL={relay or "wss://your.relay.host"}
```

Then:

```bash
pip install "agnova @ git+https://github.com/km2411/agnova@$(sed -n 's/^sha = //p' runtime.lock)"
agnova install          # the pinned buzz-acp and the ACP adapter
agnova doctor {name}
agnova up {name}
```

That first line is the whole bootstrap. Everything after it is the installed
command.
""",
    )

    for path in written:
        print(f"{GREEN}✓{RESET} {path}")
    for path in skipped:
        print(f"{DIM}· {path} (exists, left alone){RESET}")

    if not sha:
        print(
            f"\n{YELLOW}!{RESET} runtime.lock has no sha: this agnova was installed from a path "
            f"or in editable mode,\n  so there is no commit to pin. Set it before the agent "
            f"is used anywhere else:\n"
            f"  agnova runtime status   # will report the gap"
        )

    print(f"\n{name.capitalize()} scaffolded. Next:")
    print("  1. write SOUL.md — it is a placeholder")
    print(f"  2. set BUZZ_OWNER_PUBKEY in agents/{name}.env" if not owner else "")
    print("  3. mint a keypair, join the relay, add the agent to a channel")
    print(f"  4. agnova doctor {name}")
    return 0
