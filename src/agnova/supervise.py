#!/usr/bin/env python3
"""Supervisor — bring an agent up, take it down, say what is wrong.

Two processes make an agent present in Buzz:

    front door   translates loopback into a properly-tunnelled relay connection
    buzz-acp     Block's own harness; holds the socket and drives the LLM

Neither is written by us except the first, and the first is transport only. All
agent behaviour — who to answer, dedup, queueing, prompt assembly — belongs to
`buzz-acp`, so upgrades to how Buzz works arrive by updating a binary.

Every command is idempotent and safe to run on a session start: `up` twice
starts nothing twice, and a broken environment produces a diagnosis rather than
a traceback.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agnova import config as agent_config  # noqa: E402
from agnova.config import PACKAGE_DIR, ROOT, AgentConfig  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def bad(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}!{RESET} {msg}")


# --- process bookkeeping -----------------------------------------------------


def pid_file(cfg: AgentConfig, service: str) -> Path:
    return cfg.var / f"{service}.pid"


def log_file(cfg: AgentConfig, service: str) -> Path:
    return cfg.var / f"{service}.log"


def running_pid(cfg: AgentConfig, service: str) -> int | None:
    """The live pid for a service, or None. Clears a stale file as a side effect."""
    path = pid_file(cfg, service)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)  # signal 0 asks "does this exist and may I signal it?"
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        path.unlink(missing_ok=True)
        return None


def spawn(cfg: AgentConfig, service: str, argv: list[str], env: dict[str, str], cwd: Path) -> int:
    """Start a service detached, so it outlives the shell that ran this."""
    log = log_file(cfg, service)
    handle = log.open("ab")
    handle.write(f"\n=== {service} starting {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    handle.flush()
    # S603: argv comes from this module and the agent config, never the network.
    process = subprocess.Popen(  # noqa: S603
        argv,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(cwd),
        start_new_session=True,  # setsid: survives the session that spawned it
    )
    pid_file(cfg, service).write_text(f"{process.pid}\n")
    return process.pid


def stop(cfg: AgentConfig, service: str) -> bool:
    pid = running_pid(cfg, service)
    if pid is None:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(30):
        if running_pid(cfg, service) is None:
            break
        time.sleep(0.2)
    pid_file(cfg, service).unlink(missing_ok=True)
    return True


# --- preflight ---------------------------------------------------------------


def buzz_acp_binary() -> str | None:
    # An explicit override wins, so a machine that already has the binary never
    # has to build it. Buzz Desktop ships `buzz-acp` inside the app bundle as a
    # Tauri sidecar; pointing at that is the fastest path on a laptop and skips
    # the Rust toolchain entirely.
    override = os.environ.get("BUZZ_ACP_BINARY", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None

    found = shutil.which("buzz-acp")
    if found:
        return found
    # The documented install path is a source build; a checkout left in /tmp is
    # the common case in a fresh container, so look there before giving up.
    # S108: the documented source-build location, checked only after PATH.
    for candidate in (Path("/tmp/buzz/target/release/buzz-acp"), ROOT / "vendor/buzz-acp"):  # noqa: S108
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def doctor(cfg: AgentConfig) -> int:
    """Diagnose without changing anything. Never exits non-zero for 'not running'."""
    problems = 0
    print(f"{DIM}agent{RESET} {cfg.label} ({cfg.name})   {DIM}home{RESET} {cfg.home}")
    print(f"{DIM}relay{RESET} {cfg.relay_url}  {DIM}via{RESET} {cfg.local_relay_url}")

    if cfg.secret_key:
        ok("BUZZ_PRIVATE_KEY present")
    else:
        bad("BUZZ_PRIVATE_KEY missing")
        problems += 1

    binary = buzz_acp_binary()
    if binary:
        ok(f"buzz-acp at {binary}")
    else:
        bad("buzz-acp not found — run `./agent install`")
        problems += 1

    # buzz-acp publishes replies itself, over its own relay socket. buzz-cli is
    # only the agent's *tool* surface, and only when wired up as an MCP server
    # via BUZZ_ACP_MCP_COMMAND — which is empty by default. Verified against
    # buzz-acp v0.5.2: an empty mcp_command registers no MCP servers at all.
    if shutil.which("buzz"):
        ok("buzz-cli on PATH (optional — the agent's tool surface, via BUZZ_ACP_MCP_COMMAND)")
    else:
        warn("buzz-cli not on PATH — replies still work; the agent just has no Buzz tools")

    if shutil.which(cfg.agent_command):
        ok(f"agent command `{cfg.agent_command}` on PATH")
    else:
        bad(f"agent command `{cfg.agent_command}` not found — run `./agent install`")
        problems += 1

    if not cfg.owner_pubkey and cfg.respond_to == "owner-only":
        bad(
            "respond-to is owner-only but BUZZ_OWNER_PUBKEY is unset — every event would be \
            dropped"
        )
        problems += 1

    if not cfg.auth_tag:
        warn("BUZZ_AUTH_TAG unset — the agent shows as unattested (cosmetic; nothing is blocked)")

    for service in ("frontdoor", "harness", "checkpoint"):
        pid = running_pid(cfg, service)
        (ok if pid else warn)(f"{service}: {'running, pid ' + str(pid) if pid else 'not running'}")

    return problems


# --- commands ----------------------------------------------------------------


def up(cfg: AgentConfig) -> int:
    binary = buzz_acp_binary()
    if not binary:
        bad("buzz-acp not found — run `./agent install`")
        return 1

    if not cfg.uses_frontdoor:
        ok(f"direct to {cfg.relay_url} — no front door needed on this host")
    elif running_pid(cfg, "frontdoor"):
        ok("front door already up")
    else:
        env = dict(os.environ)
        env.update({"BUZZ_RELAY_URL": cfg.relay_url, "BUZZ_PRIVATE_KEY": cfg.secret_key})
        pid = spawn(
            cfg,
            "frontdoor",
            [
                sys.executable,
                str(PACKAGE_DIR / "frontdoor.py"),
                "--port",
                str(cfg.frontdoor_port),
            ],
            env,
            ROOT,
        )
        time.sleep(1.5)
        if running_pid(cfg, "frontdoor"):
            ok(f"front door up on 127.0.0.1:{cfg.frontdoor_port} (pid {pid})")
        else:
            bad(f"front door failed — see {log_file(cfg, 'frontdoor').relative_to(ROOT)}")
            return 1

    if running_pid(cfg, "harness"):
        ok("buzz-acp already up")
        return 0

    if cfg.checkpoint_paths and not running_pid(cfg, "checkpoint"):
        spawn(
            cfg,
            "checkpoint",
            [sys.executable, str(PACKAGE_DIR / "checkpoint.py"), cfg.name],
            dict(os.environ),
            cfg.home,
        )
        ok(f"checkpoint up — {', '.join(cfg.checkpoint_paths)} every {cfg.checkpoint_interval}s")

    pid = spawn(cfg, "harness", [binary], cfg.harness_env(), cfg.home)
    # buzz-acp fails fast on a bad relay or a missing agent command, so a short
    # wait is enough to turn "started" into "actually working".
    time.sleep(4)
    if running_pid(cfg, "harness"):
        ok(f"buzz-acp up (pid {pid}) — {cfg.label} is present in Buzz")
        return 0
    bad(f"buzz-acp exited — see {log_file(cfg, 'harness').relative_to(ROOT)}")
    tail = log_file(cfg, "harness").read_text().strip().splitlines()[-6:]
    for line in tail:
        print(f"  {DIM}{line}{RESET}")
    return 1


def down(cfg: AgentConfig) -> int:
    # Harness first: it publishes offline presence on the way out, which needs
    # the front door still standing.
    # Checkpoint last: it gets a final chance to save whatever the agent wrote
    # before the harness stopped.
    for service in ("harness", "frontdoor", "checkpoint"):
        (ok if stop(cfg, service) else warn)(
            f"{service} stopped"
            if running_pid(cfg, service) is None
            else f"{service} would not stop"
        )
    return 0


def status(cfg: AgentConfig) -> int:
    live = True
    expected = (["frontdoor"] if cfg.uses_frontdoor else []) + ["harness"]
    expected += ["checkpoint"] if cfg.checkpoint_paths else []
    for service in expected:
        pid = running_pid(cfg, service)
        live = live and pid is not None
        (ok if pid else bad)(f"{service}: {'pid ' + str(pid) if pid else 'down'}")
    return 0 if live else 1


def logs(cfg: AgentConfig, lines: int) -> int:
    for service in ("frontdoor", "harness", "checkpoint"):
        path = log_file(cfg, service)
        print(f"\n{DIM}── {path.relative_to(ROOT)} ──{RESET}")
        if path.exists():
            tail = path.read_text(errors="replace").splitlines()[-lines:]
            print("\n".join(tail) or "(empty)")
        else:
            print("(no log yet)")
    return 0


def install() -> int:
    """Build the pinned upstream and install the ACP adapter.

    Python rather than shell so the installed console script can do it: an agent
    repo consumes agnova as a dependency and has no checkout of this repo to run
    a script from.
    """
    from agnova import upstream

    lock = upstream.read_lock()
    print(f"{DIM}building{RESET} {lock['ref']} {DIM}{lock['sha'][:12]}{RESET} from {lock['repo']}")
    checkout = Path("/tmp/buzz")  # noqa: S108 — deliberately outside any repo; a reclaim discards it
    if not (checkout / ".git").is_dir():
        run_step(["git", "clone", "--filter=blob:none", lock["repo"], str(checkout)])
    run_step(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", lock["sha"]])
    run_step(["git", "-C", str(checkout), "checkout", "--detach", lock["sha"]])

    # `cargo install`, never `cargo build`: it places the binary in ~/.cargo/bin,
    # which survives a sandbox reclaim. A build tree under /tmp does not.
    #
    # buzz-cli is NOT built. buzz-acp publishes replies over its own relay
    # socket; buzz-cli is only the agent's tool surface, and only when
    # BUZZ_ACP_MCP_COMMAND names it. Build it separately if you want it.
    run_step(["cargo", "install", "--path", str(checkout / "crates/buzz-acp"), "--locked"])
    run_step(["npm", "install", "-g", "@agentclientprotocol/claude-agent-acp"])
    ok("installed — next: agnova doctor <agent>")
    return 0


def run_step(cmd: list[str]) -> None:
    print(f"{DIM}$ {' '.join(cmd)}{RESET}")
    out = subprocess.run(cmd, check=False)  # noqa: S603 — fixed argv, shell=False
    if out.returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}")


#: Commands that act on the machine or the version pin rather than on one
#: agent. They must dispatch before a config is loaded — `install` on a fresh
#: box has no agent configured yet, and requiring one would be a chicken-and-egg.
def _hostwide(command: str, agent: str | None) -> int | None:
    from agnova import runtime, upstream

    if command == "install":
        return install()
    if command == "runtime":
        # `agnova runtime <status|check|install>` — the agent's own agnova pin.
        # The subcommand rides in the `agent` slot; these never take an agent.
        return {"status": runtime.status, "check": runtime.check, "install": runtime.install}.get(
            agent or "status", runtime.status
        )()
    if command == "init":
        from agnova.config import ROOT
        from agnova.scaffold import init

        if not agent:
            raise SystemExit("agnova init <name> — name the agent")
        return init(agent, ROOT)
    if command == "upstream-check":
        return upstream.check()
    if command == "upstream-update":
        return upstream.update(agent)
    return None


def _agent_command(command: str, cfg: AgentConfig, lines: int, publish: bool = False) -> int:
    from agnova import selftest

    if command == "up":
        return up(cfg)
    if command == "down":
        return down(cfg)
    if command == "restart":
        down(cfg)
        return up(cfg)
    if command == "status":
        return status(cfg)
    if command == "logs":
        return logs(cfg, lines)
    if command == "selftest":
        selftest.run(cfg.name)
        return 0
    if command == "engram":
        from agnova import engram

        if publish:
            return engram.publish(cfg)
        print(engram.render(cfg), end="")
        return 0
    # doctor: a diagnosis is not a failure — this must never break a session start.
    doctor(cfg)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Buzz agent.")
    parser.add_argument(
        "command",
        choices=[
            "up",
            "down",
            "restart",
            "status",
            "doctor",
            "logs",
            "selftest",
            "engram",
            "install",
            "init",
            "runtime",
            "upstream-check",
            "upstream-update",
        ],
    )
    parser.add_argument(
        "agent",
        nargs="?",
        help="agent name (see agents/*.env); for `runtime`, one of status|check|install",
    )
    parser.add_argument("--lines", type=int, default=25)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="engram: publish to the relay instead of printing",
    )
    args = parser.parse_args()

    code = _hostwide(args.command, args.agent)
    if code is not None:
        sys.exit(code)

    sys.exit(_agent_command(args.command, agent_config.load(args.agent), args.lines, args.publish))


if __name__ == "__main__":
    main()
