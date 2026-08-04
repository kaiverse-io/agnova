# Agnova

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![CI](https://github.com/kaiverse-io/agnova/actions/workflows/ci.yaml/badge.svg)](https://github.com/kaiverse-io/agnova/actions/workflows/ci.yaml)

**Run an AI agent as a first-class participant in a [Buzz](https://github.com/block/buzz) workspace** — including from sandboxes that cannot open a WebSocket.

Agnova owns the thin transport and lifecycle layer. Agent behavior stays in upstream [`buzz-acp`](https://github.com/block/buzz/tree/main/crates/buzz-acp). An agent is a **config file plus a home directory**, never a fork of the runtime.

```bash
bin/agnova install       # build the pinned upstream, once per machine
bin/agnova up scout      # front door + harness; idempotent
bin/agnova doctor        # diagnose, change nothing
bin/agnova selftest      # prove the transport without spending a token
```

```bash
cp agents/example.env agents/scout.env
export BUZZ_PRIVATE_KEY=<scout's own key>   # never commit secrets
bin/agnova up scout
```

## Why it exists

Buzz already ships `buzz-acp` (~34k lines): WebSocket to the relay, mention gating, queues, warm ACP processes, presence — talking to Claude Code, goose, or Codex.

**Agnova reimplements none of that.** It exists because `buzz-acp` could not open a socket from inside a proxied cloud sandbox:

| Path | WebSocket upgrade |
|---|---|
| Ask the proxy for a tunnel (`CONNECT`) | **101 Switching Protocols** |
| Dial the host directly (transparent intercept) | **403 Forbidden** |

`buzz-cli` works (reqwest honours `HTTPS_PROXY`). `buzz-acp` fails (`tokio-tungstenite` never reads it). Agnova listens in plaintext on loopback and carries the connection out properly:

```
buzz-acp ──ws://127.0.0.1──▶ front door ──CONNECT + TLS──▶ relay
```

Stock `buzz-acp` — no fork, no patched build, no certificates. Three in-flight corrections keep the client's loopback belief honest with the relay: `Host`, NIP-98 `Authorization`, and NIP-42 `AUTH` `relay` tag. Details in [ADR-001](docs/decisions/adr-001-front-door.md).

**On a normal host none of this is needed.** Point `BUZZ_RELAY_URL` at the relay and the front door drops out.

| Layer | Approx. size | Maintained by |
|---|---|---|
| Front door, keys/NIP-98, supervise, checkpoint | ~1k lines | this project |
| `buzz-acp` — everything an agent *does* | ~34.5k lines | Block |

**Own the stable half, rent the moving half.** Relay protocol is frozen; Buzz agent semantics are not. Improvements arrive by bumping a version pin.

## Memory

Agnova persists an agent's memory; it never decides what belongs in it. `checkpoint.py` commits and pushes named paths on an interval — no model, no tokens. With no paths configured it does nothing on purpose.

## Requirements

`bin/agnova install` handles setup:

- `buzz-cli` and `buzz-acp` from the pin in `agnova/upstream.lock` (never upstream `main`)
- An ACP runtime: `@agentclientprotocol/claude-agent-acp`, `goose`, or `codex-acp`
- Python 3.11+ with `coincurve` — the only runtime dependency, intentionally small (this process holds a private key)

Claude Code needs no `ANTHROPIC_API_KEY`: the adapter wraps the Agent SDK and inherits local subscription auth. `IS_SANDBOX=1` is set for you when containers run as root.

## Documentation

- [Design](docs/explanation/design.md) — motivation, architecture, trade-offs
- [ADR-001](docs/decisions/adr-001-front-door.md) — transport front door, not a protocol shim
- [Session lifetime](docs/explanation/session-lifetime.md) — idle, reclaim, and what nothing here can prevent
- [Set up the environment](docs/how-to/set-up-the-environment.md)
- [Run a new agent](docs/how-to/run-a-new-agent.md)
- [Configuration](docs/reference/configuration.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
