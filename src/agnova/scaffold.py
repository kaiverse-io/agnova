"""Create a new agent repository.

Every agent has the same shape. Not by convention — by construction, so that a
second agent is never a hand-copy of the first, and nothing about the runtime is
maintained per-agent.

    agnova init scout --owner <pubkey> --relay wss://your.relay

What it writes:

    runtime.lock        which agnova this agent runs on (data, never code)
    agents/<name>.env   the agent's config — committed, never secret
    AGENTS.md           the operating contract, read by whatever runtime runs it
    CLAUDE.md           one-line bridge so Claude Code reads AGENTS.md
    SOUL.md             who the agent is
    PRINCIPLES.md       the owner's philosophy — governs everything below it
    USER.md             who the owner is
    MEMORY.md           curated long-term memory
    memory/             the daily log — thoughts, as they happen
    knowledge/          knowledge made from thoughts: structured, sourced
    ideas/              knowledge organised into ideas, governed by PRINCIPLES.md
    skills/             what it knows how to do
    .gitignore          runtime state that must never be committed

The four content layers are a pipeline, not four folders that happen to exist:

    thought ──▶ knowledge ──▶ idea
                                 ▲
                          PRINCIPLES.md governs

A thought is raw — something the owner said, wrote or handed over. Knowledge is
what an agent makes of it: structured, sourced, connected to what is already
there. An idea is knowledge organised into something that can be acted on, and
what makes it a *good* idea is the owner's philosophy, not the agent's taste.
That is why PRINCIPLES.md sits above the pipeline rather than inside it.

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

DEFAULT_REPO = "https://github.com/kaiverse-io/agnova.git"


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
AGENT_ENGRAM_PATHS=SOUL.md,PRINCIPLES.md

# What must survive the machine. Named explicitly, never inferred — a timer that
# committed the whole home would eventually commit half-finished work.
AGENT_CHECKPOINT_PATHS=memory/,MEMORY.md,knowledge/,ideas/,PRINCIPLES.md
AGENT_CHECKPOINT_INTERVAL=900
""",
    )

    w(
        "AGENTS.md",
        f"""# AGENTS.md — {name.capitalize()}

The operating contract. Runtime-agnostic on purpose: Claude Code, goose and codex
each look for a different file, so this is the single source and the others bridge
to it.

## Session startup

1. If `memory/{today:%Y-%m-%d}.md` (today) does not exist, create it before anything else.
2. Read `SOUL.md` — who you are.
3. Read `PRINCIPLES.md` — what governs your judgement. Not optional; it is the
   difference between organising well and organising to your own taste.
4. Read `USER.md` — who you serve.
5. Read `MEMORY.md`, then today's and yesterday's `memory/` entries.
6. Read `skills/INDEX.md` and `knowledge/INDEX.md` — names and one-line
   descriptions only. Fetch a skill's `SKILL.md` or a knowledge entry's body
   only once its description matches the task in front of you; reading
   every body up front defeats the point of the index.

## The pipeline

```
thought ──▶ knowledge ──▶ idea
                             ▲
                      PRINCIPLES.md governs
```

- **Thought** — raw. Something said, written or handed over. Lands in
  `memory/YYYY-MM-DD.md` if it happened in a session, or arrives as a file.
- **Knowledge** — what you make of a thought: structured, sourced, connected to
  what is already in `knowledge/`. A thought becomes knowledge when you can say
  where it came from and what it relates to.
- **Idea** — knowledge organised into something actionable, in `ideas/`. What
  makes an idea *good* is the owner's philosophy, not your taste — which is why
  `PRINCIPLES.md` sits above this and not inside it.

Moving something down this pipeline is the work, not housekeeping. A thought left
raw is a thought lost; an idea that contradicts `PRINCIPLES.md` is one you should
say so about rather than quietly file.

## Memory

- `memory/YYYY-MM-DD.md` — the daily log. What happened, what was decided.
- `MEMORY.md` — curated long-term memory, promoted from daily notes once durable.

Both are committed by the runtime's checkpoint. Deciding *what* is worth writing
is yours; the runtime only persists what you wrote.

## Where things are

| You want | Look in |
|---|---|
| Who you are | `SOUL.md` |
| What governs judgement | `PRINCIPLES.md` |
| Who you serve | `USER.md` |
| Long-term memory | `MEMORY.md` |
| A given day | `memory/YYYY-MM-DD.md` |
| Structured knowledge | `knowledge/` (`knowledge/INDEX.md` first) |
| Organised ideas | `ideas/` |
| Capabilities | `skills/` (`skills/INDEX.md` first) |

## What does not belong here

Project design, specifications and source code belong to the project's own
repository. The test is form, not subject: **if it could be implemented from, it
is a spec** — and a spec held here goes stale and misleads.

## Runtime

This repository contains no runtime code. It is run by
[agnova](https://github.com/kaiverse-io/agnova), pinned in `runtime.lock`.

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

    w(
        "USER.md",
        """# USER.md — who this agent serves

_Replace this. An agent that does not know who it works for cannot tell a good
answer from a plausible one._

- **Name:**
- **What to call them:**
- **Pronouns:**
- **Timezone:**
- **Location:**

## Context

- What they are working on.
- What they care about.
- How they like to be dealt with — and how they do not.

Facts only, and only facts they gave you. Never infer a preference and record it
as though it were stated; an invented one is applied with the same confidence as
a real one and is much harder to notice.
""",
    )

    w(
        "PRINCIPLES.md",
        """# PRINCIPLES.md — what governs judgement here

_The owner's philosophy and principles. Not the agent's opinions — those go
elsewhere. This file is the standard an idea is measured against._

Replace this with what the owner actually believes. **Do not invent it.** A
principle nobody stated is worse than no principle: it will be applied
confidently and wrongly. Where something is unknown, leave it marked unknown and
ask.

## Principles

<!-- One per heading. State it, then cite where it came from — a conversation,
     a document, a decision. A principle without a source is a guess. -->

### (example — replace)
**Statement.** What is believed.
**Source.** Where it was said or written.
**In practice.** What it rules in, and what it rules out.

## Open

- What has not been articulated yet, and should be asked about.
""",
    )

    w(
        "knowledge/README.md",
        """# knowledge/

Knowledge made from thoughts. Structured, sourced, connected.

A thought becomes knowledge when you can say **where it came from** and **what it
relates to**. Until then it is raw material sitting in a daily note.

Every entry carries its provenance. Knowledge whose source is unknown cannot be
trusted later, and an agent that cannot say why it believes something is guessing
with extra steps.

This is the library. The diary is `memory/`. New entries go in `INDEX.md` too —
one line each, so the whole catalogue can be scanned without opening a file.
""",
    )

    w(
        "knowledge/INDEX.md",
        """# Knowledge index

<!-- One line per entry:  - [Title](slug.md) — one-line hook.
     Read before the entries themselves — names and hooks first, bodies on
     demand. A routing tree costs more than it returns; keep this flat. -->
""",
    )

    w(
        "ideas/README.md",
        """# ideas/

Knowledge organised into something that can be acted on.

An idea is not a longer note. It is knowledge shaped into a claim, a plan or a
direction — with enough structure that it can be argued with.

Ideas are governed by `PRINCIPLES.md`. When one contradicts a stated principle,
say so rather than filing it quietly: the disagreement is the useful part, and it
usually means either the idea is wrong or a principle has changed.
""",
    )

    w(
        "skills/INDEX.md",
        """# Skill index

<!-- One line per skill:  - [Name](name/SKILL.md) — one-line, skippable description.
     Read this before any SKILL.md body — names and descriptions always in
     context, bodies loaded only when a description matches the task. -->

- [example](example/SKILL.md) — starter skill; replace or delete it.
""",
    )

    w(
        "skills/example/SKILL.md",
        f"""# Example Skill

> This is a starter skill for {name.capitalize()} — replace or delete it, and
> update `skills/INDEX.md` to match.
> Skills are reusable agent capabilities. Standard: https://agentskills.io

## What this skill does

Describe what the skill accomplishes in 1-2 sentences. This line, plus the
INDEX.md entry, is what gets read every session — write it to be skippable
by a task this skill has nothing to do with.

## Steps

1. Step one — describe what the agent should do.
2. Step two — be specific about commands, file paths, expected output.
3. Verify — describe what success looks like.

## When to use

- Situation A where this skill is appropriate.

## When NOT to use

- Situation where a different approach is better.
""",
    )

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

The runtime is [agnova](https://github.com/kaiverse-io/agnova), installed as a
dependency and pinned in `runtime.lock`.

## Running it

Two secrets, in the environment and never in this repository:

```bash
export BUZZ_PRIVATE_KEY=<this agent's secret key>
export BUZZ_RELAY_URL={relay or "wss://your.relay.host"}
```

Then:

```bash
pip install \
  "agnova @ git+https://github.com/kaiverse-io/agnova@$(
    sed -n 's/^sha = //p' runtime.lock)"
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
