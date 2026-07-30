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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agnova import config as agent_config  # noqa: E402
from agnova.config import ROOT, AgentConfig  # noqa: E402

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
    process = subprocess.Popen(
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
    found = shutil.which("buzz-acp")
    if found:
        return found
    # The documented install path is a source build; a checkout left in /tmp is
    # the common case in a fresh container, so look there before giving up.
    for candidate in (Path("/tmp/buzz/target/release/buzz-acp"), ROOT / "vendor/buzz-acp"):
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

    if shutil.which("buzz"):
        ok("buzz-cli on PATH (the agent's own tool surface)")
    else:
        warn("buzz-cli not on PATH — the agent can receive but not reply")

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

    if running_pid(cfg, "frontdoor"):
        ok("front door already up")
    else:
        env = dict(os.environ)
        env.update({"BUZZ_RELAY_URL": cfg.relay_url, "BUZZ_PRIVATE_KEY": cfg.secret_key})
        pid = spawn(
            cfg,
            "frontdoor",
            [
                sys.executable,
                str(ROOT / "agnova" / "frontdoor.py"),
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
            [sys.executable, str(ROOT / "agnova" / "checkpoint.py"), cfg.name],
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
    expected = ["frontdoor", "harness"] + (["checkpoint"] if cfg.checkpoint_paths else [])
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Buzz agent.")
    parser.add_argument("command", choices=["up", "down", "restart", "status", "doctor", "logs"])
    parser.add_argument("agent", nargs="?", help="agent name (see agents/*.env)")
    parser.add_argument("--lines", type=int, default=25)
    args = parser.parse_args()

    cfg = agent_config.load(args.agent)
    if args.command == "up":
        sys.exit(up(cfg))
    if args.command == "down":
        sys.exit(down(cfg))
    if args.command == "restart":
        down(cfg)
        sys.exit(up(cfg))
    if args.command == "status":
        sys.exit(status(cfg))
    if args.command == "doctor":
        # A diagnosis is not a failure — this must never break a session start.
        doctor(cfg)
        sys.exit(0)
    if args.command == "logs":
        sys.exit(logs(cfg, args.lines))


if __name__ == "__main__":
    main()
