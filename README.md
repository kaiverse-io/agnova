# Agnova

Run an AI agent as a first-class participant in a [Buzz](https://github.com/block/buzz)
workspace — including from sandboxes that cannot open a WebSocket.

```bash
bin/agnova install       # build the pinned upstream, once per machine
bin/agnova up scout      # front door + harness; idempotent
bin/agnova doctor        # diagnose, change nothing
bin/agnova selftest      # prove the transport without spending a token
```

An agent is a **config file plus a home directory**, never a copy of the code:

```bash
cp agents/example.env agents/scout.env
export BUZZ_PRIVATE_KEY=<scout's own key>
bin/agnova up scout
```

## What this is — and what it refuses to be

Buzz already ships [`buzz-acp`](https://github.com/block/buzz/tree/main/crates/buzz-acp):
~34,000 lines that hold a WebSocket to the relay, decide which events deserve an
answer, queue and deduplicate them, keep agent processes warm, publish presence
and typing indicators, and speak [ACP](https://agentclientprotocol.com/) to
Claude Code, goose or Codex.

**Agnova reimplements none of that.** It exists because `buzz-acp` could not open
a socket from inside a Claude Code cloud sandbox, and everything here serves
removing that one obstacle:

| | lines | maintained by |
|---|---|---|
| `frontdoor.py` — transport correction | ~350 | this project |
| `nostr.py` — keys, signing, NIP-98 | ~150 | this project |
| `config.py`, `supervise.py`, `checkpoint.py`, `upstream.py` | ~500 | this project |
| `buzz-acp` — everything an agent actually *does* | ~34,500 | Block |

The rule the design follows: **own the stable half, rent the moving half.** The
Nostr relay protocol is frozen; Buzz's agent semantics are not. So improvements
to how Buzz agents behave arrive by moving a version pin, not by editing code
here.

An earlier version of this work did the opposite — a hand-written poll loop
reimplementing mention filtering, dedup, cursors and prompt assembly. It worked,
and every Buzz release made it more wrong. That code is deleted.

## The obstacle, precisely

Outbound traffic from a cloud sandbox goes through a proxy. Two ways to get
there, and they do not behave the same:

| path | WebSocket upgrade |
|---|---|
| ask the proxy for a tunnel (`CONNECT`) | **101 Switching Protocols** |
| dial the host directly, be transparently intercepted | **403 Forbidden** |

`buzz-cli` works because reqwest honours `HTTPS_PROXY`. `buzz-acp` fails because
`tokio-tungstenite` never reads it. Same host, same network, same relay — one
library asks for a tunnel and the other does not.

So Agnova listens in plaintext on loopback (exempt from the proxy; plaintext
means no CA-trust question) and carries the connection out properly:

```
buzz-acp ──ws://127.0.0.1──▶ front door ──CONNECT + TLS──▶ relay
```

`buzz-acp` runs **stock** — no fork, no patched build, no `/etc/hosts` entry, no
certificates. It is simply told the relay lives on `127.0.0.1`.

Three things are corrected in flight, because the client believes it is talking
to loopback and the relay checks that belief in three independent places: the
`Host` header, the NIP-98 `Authorization` token, and the NIP-42 `AUTH` event's
`relay` tag. [ADR-001](docs/decisions/adr-001-front-door.md) covers why, and
which alternatives were tried and rejected.

**On a normal host none of this is needed.** Point `BUZZ_RELAY_URL` at the relay
and the front door drops out of the picture entirely.

## Memory

Agnova persists an agent's memory; it never decides what belongs in it.

`checkpoint.py` commits and pushes named paths on an interval — `git status`, and
a commit only when something changed. No model, no tokens. It is handed paths and
keeps them durable; what they *mean* is the agent's business. An agent that
records everything and one that records almost nothing run on the same unmodified
runtime.

With no paths configured it does nothing, deliberately: a timer that commits a
whole home directory eventually commits somebody's half-finished work.

## Documentation

- [Design](docs/explanation/design.md) — motivation, architecture, trade-offs
- [ADR-001](docs/decisions/adr-001-front-door.md) — a transport front door, not a protocol shim
- [Session lifetime](docs/explanation/session-lifetime.md) — what "idle" means, and why nothing here can prevent reclaim
- [Set up the environment](docs/how-to/set-up-the-environment.md) — every manual step, once
- [Run a new agent](docs/how-to/run-a-new-agent.md)
- [Configuration](docs/reference/configuration.md) — every setting

## Requirements

`bin/agnova install` handles all of it:

- `buzz-cli` and `buzz-acp`, built from the version pinned in
  `agnova/upstream.lock` — never from upstream's default branch. See
  [Upstream is pinned](#upstream-is-pinned).
- An ACP runtime: `@agentclientprotocol/claude-agent-acp`, `goose`, or `codex-acp`.
- Python 3.11+ with `coincurve` — the only runtime dependency, on purpose. This
  process holds an agent's private key, and a small dependency tree is part of
  that being defensible.

**Claude Code needs no `ANTHROPIC_API_KEY.`** The adapter wraps the Agent SDK,
which spawns the local `claude` binary and inherits its subscription auth
(verified end to end). `IS_SANDBOX=1` is set for you, since these containers run
as root.

## Upstream is pinned

Because the design rents `buzz-acp` rather than owning agent logic, *which
version* it rents is the most consequential fact about an agent's behaviour. So
it does not float:

```bash
bin/agnova upstream-check          # what is pinned, what is available
bin/agnova upstream-update         # move to the newest release
bin/agnova upstream-update v0.5.1  # a specific ref, or roll back
```

`agnova/upstream.lock` records the ref **and the commit** — tags can be moved
upstream, commits cannot. An update is a one-line reviewable diff, then
`bin/agnova install && bin/agnova restart && bin/agnova selftest`.

## Tests

```bash
bin/agnova test        # 26 offline unit tests: no keys, no relay, no network
bin/agnova selftest    # live transport probe — needs a real relay and a real key
```

Two different things. The unit suite belongs in CI; `selftest` does not.

## Limits worth knowing before relying on it

- **The agent dies when a cloud sandbox is reclaimed.** Nothing here changes
  that, and nothing inside a container can — see
  [session lifetime](docs/explanation/session-lifetime.md). Pair it with a
  scheduled trigger for an availability floor, or run on a host that is not
  reclaimed.
- **One harness per identity.** Two processes signing as the same agent answer
  every message twice. `up` is idempotent; discipline covers the rest.
- **The front door holds the agent's private key**, because re-signing is the
  fix. It re-signs only events already carrying that agent's pubkey, so it
  cannot launder another identity — but it is a key-bearing process, and it binds
  to loopback only. Never expose it on `0.0.0.0`.

## Licence

[Apache-2.0](LICENSE).
