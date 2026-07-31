"""Agent configuration — one agent is one env file, never a copy of the code.

Everything that differs between agents lives in `agents/<name>.env`. Everything
that differs between *hosts* (keys, relay URL, proxy) lives in the environment.
Nothing agent-specific belongs in `agnova/`, and nothing secret belongs in
`agents/`.

Precedence is deliberate: the process environment wins over the env file, so a
one-off override never needs a file edit, and a secret can never be
accidentally committed by being written where a default lives.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# src/ layout: parents[2] is the repo root, where agents/ and var/ live.
ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = ROOT / "agents"
VAR_DIR = ROOT / "var"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-shaped file. Comments and blank lines ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, raw = match.groups()
        raw = raw.strip()
        # Quotes first, then comments. The other order corrupts any quoted
        # value containing a literal '#': the comment split truncates it, and
        # the surviving fragment no longer ends in a quote, so the unquoting
        # step silently declines and stores a mangled value with a stray
        # leading quote.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            values[key] = raw[1:-1]
            continue
        if raw[:1] in ('"', "'"):
            closing = raw.find(raw[0], 1)
            if closing != -1:
                values[key] = raw[1:closing]
                continue
        values[key] = raw.split(" #", 1)[0].strip()
    return values


@dataclass
class AgentConfig:
    """Everything the runtime needs to run one agent."""

    name: str
    label: str
    home: Path
    relay_url: str
    secret_key: str = field(repr=False)
    owner_pubkey: str
    frontdoor_port: int
    agent_command: str
    respond_to: str
    auth_tag: str | None
    transport: str = "auto"
    checkpoint_paths: list[str] = field(default_factory=list)
    checkpoint_interval: int = 900
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def uses_frontdoor(self) -> bool:
        """Whether this host needs the transport shim at all.

        The front door exists for exactly one reason: an egress proxy that
        refuses WebSocket upgrades. On a host without one there is nothing to
        correct, so the harness talks to the relay directly and this whole
        component stays out of the path.

        `auto` decides by looking for a proxy; `direct` and `frontdoor` force
        the answer for hosts where the guess would be wrong.
        """
        if self.transport == "direct":
            return False
        if self.transport == "frontdoor":
            return True
        return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))

    @property
    def local_relay_url(self) -> str:
        """What the harness is pointed at — the front door, or the relay itself."""
        if not self.uses_frontdoor:
            return self.relay_url
        return f"ws://127.0.0.1:{self.frontdoor_port}"

    @property
    def var(self) -> Path:
        return VAR_DIR / self.name

    def path(self, kind: str) -> Path:
        return self.var / kind

    def harness_env(self) -> dict[str, str]:
        """The environment `buzz-acp` is launched with.

        `BUZZ_RELAY_URL` points at the front door on purpose; the front door
        holds the real relay URL. `IS_SANDBOX` is required because these
        containers run as root and the Claude Agent SDK otherwise refuses to
        start its subprocess.
        """
        env = dict(os.environ)
        env.update(self.env)
        env.update(
            {
                "BUZZ_RELAY_URL": self.local_relay_url,
                "BUZZ_PRIVATE_KEY": self.secret_key,
                "BUZZ_ACP_AGENT_COMMAND": self.agent_command,
                "BUZZ_ACP_RESPOND_TO": self.respond_to,
                "IS_SANDBOX": "1",
            }
        )
        if self.owner_pubkey:
            env["BUZZ_ACP_AGENT_OWNER"] = self.owner_pubkey
        if self.auth_tag:
            env["BUZZ_AUTH_TAG"] = self.auth_tag
        return env


def load(name: str | None = None) -> AgentConfig:
    name = (name or os.environ.get("BUZZ_AGENT_NAME") or "").strip().lower()
    if not name:
        # `example` is the template for a new agent, not a runnable one — it has
        # no owner and no key, so offering it here would only produce a
        # confusing second failure.
        available = sorted(p.stem for p in AGENTS_DIR.glob("*.env") if p.stem != "example")
        raise SystemExit(
            "no agent named. Pass one (`./agent up <name>`) or set BUZZ_AGENT_NAME.\n"
            f"available: {', '.join(available) or '(none — see agents/example.env)'}"
        )

    env_path = AGENTS_DIR / f"{name}.env"
    if not env_path.exists():
        raise SystemExit(f"no config for agent {name!r} — expected {env_path.relative_to(ROOT)}")

    file_values = read_env_file(env_path)

    def value(key: str, default: str = "") -> str:
        # Environment beats file: a host override should never require an edit.
        return (os.environ.get(key) or file_values.get(key) or default).strip()

    secret = value("BUZZ_PRIVATE_KEY")
    if not secret:
        raise SystemExit(
            f"BUZZ_PRIVATE_KEY is not set for {name}. It is a secret: it belongs in the\n"
            f"environment, never in {env_path.relative_to(ROOT)}."
        )

    home = Path(value("BUZZ_AGENT_HOME", str(ROOT))).expanduser()
    config = AgentConfig(
        name=name,
        label=value("BUZZ_AGENT_LABEL", name.capitalize()),
        home=home,
        relay_url=value("BUZZ_RELAY_URL"),
        secret_key=secret,
        owner_pubkey=value("BUZZ_OWNER_PUBKEY"),
        frontdoor_port=int(value("BUZZ_FRONTDOOR_PORT", "8443")),
        agent_command=value("BUZZ_ACP_AGENT_COMMAND", "claude-agent-acp"),
        respond_to=value("BUZZ_ACP_RESPOND_TO", "owner-only"),
        auth_tag=value("BUZZ_AUTH_TAG") or None,
        transport=value("AGNOVA_TRANSPORT", "auto").lower(),
        # Named explicitly, never inferred: a timer that commits a whole home
        # directory will eventually commit somebody's half-finished work.
        checkpoint_paths=[
            p.strip() for p in value("AGENT_CHECKPOINT_PATHS").split(",") if p.strip()
        ],
        checkpoint_interval=max(60, int(value("AGENT_CHECKPOINT_INTERVAL", "900"))),
        env={k: v for k, v in file_values.items() if k.startswith("BUZZ_ACP_")},
    )
    if not config.relay_url:
        raise SystemExit(f"BUZZ_RELAY_URL is not set for {name}")
    config.var.mkdir(parents=True, exist_ok=True)
    return config
