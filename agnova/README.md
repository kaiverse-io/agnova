# Agent runtime

Run an AI agent as a first-class participant in a [Buzz](https://github.com/block/buzz)
workspace — from a Claude Code cloud sandbox, where it otherwise cannot connect at all.

```bash
bin/agnova up <name>       # front door + buzz-acp; idempotent
bin/agnova doctor          # diagnose, change nothing
bin/agnova selftest        # prove the transport without spending a token
bin/agnova down <name>
```

An agent is a **config file**, not a copy of the code:

```bash
cp agents/example.env agents/scout.env
export BUZZ_PRIVATE_KEY=<scout's own key>
bin/agnova up scout
```

## What this is, and what it deliberately is not

Buzz already ships [`buzz-acp`](https://github.com/block/buzz/tree/main/crates/buzz-acp):
a 34,000-line harness that holds a WebSocket to the relay, decides which events
deserve an answer, queues and deduplicates them, keeps a pool of agent
processes warm, and speaks [ACP](https://agentclientprotocol.com/) to whichever
runtime you like — Claude Code, goose, or Codex.

**This runtime does not reimplement any of that.** It exists because
`buzz-acp` could not open a socket from inside a Claude Code cloud environment,
and everything here is in service of removing that one obstacle:

| | lines | owned by |
|---|---|---|
| `frontdoor.py` — transport correction | ~330 | us |
| `nostr.py` — keys, signing, NIP-98 | ~120 | us |
| `config.py` + `supervise.py` — config and lifecycle | ~330 | us |
| `buzz-acp` — everything an agent actually *does* | ~34,500 | Block |

Agent behaviour is upstream's, so improvements to how Buzz agents work arrive
by updating a binary. A previous version of this repo did the opposite — a
hand-written poll loop reimplementing mention filtering, dedup and prompt
assembly — and every Buzz release made it more wrong.

## The obstacle, precisely

Outbound traffic from a cloud sandbox goes through an agent proxy. There are
two ways to get there, and they do not behave the same:

| path | WebSocket upgrade |
|---|---|
| ask the proxy for a tunnel (`CONNECT`) | **101 Switching Protocols** |
| dial the host directly, be transparently intercepted | **403 Forbidden** |

`buzz-cli` works because reqwest honours `HTTPS_PROXY`. `buzz-acp` fails
because `tokio-tungstenite` never reads it. Same host, same network, same
relay — one library asks for a tunnel and the other does not.

So the front door listens in plaintext on loopback (exempt from the proxy, and
plaintext means no CA-trust question) and carries the connection out over a
proper tunnel:

```
buzz-acp ──ws://127.0.0.1──▶ front door ──CONNECT + TLS──▶ relay
```

`buzz-acp` runs **stock**: no fork, no patched build, no `/etc/hosts` entry, no
certificates. It is simply told the relay lives on `127.0.0.1`.

Three things are corrected in flight, all because the client believes it is
talking to loopback and the relay checks that belief — the `Host` header, the
NIP-98 `Authorization` token, and the NIP-42 `AUTH` event's `relay` tag. See
[docs/explanation/design.md](../docs/explanation/design.md) for why each one is
load-bearing, and [ADR-001](../docs/decisions/adr-001-front-door.md) for the
alternatives that were tried and rejected.

## Layout

```
agnova/
  frontdoor.py    the transport fix — a local plaintext entrance to the relay
  nostr.py        keys, event signing, NIP-98 headers
  config.py       agents/<name>.env + environment -> AgentConfig
  supervise.py    up / down / status / doctor / logs
  selftest.py     three assertions that prove the transport
  mint-auth-tag.py  NIP-OA owner attestation (run by the owner, not the agent)
agents/
  <name>.env      one agent, one file. Never secrets.
var/<name>/       pids and logs. Gitignored.
```

## Upstream is pinned

The whole design is renting `buzz-acp` rather than owning agent logic, which
makes *which version* we rent the most consequential fact about the agent's
behaviour. So it does not float:

```bash
bin/agnova upstream-check          # what is pinned, what is available
bin/agnova upstream-update         # move to the newest release
bin/agnova upstream-update v0.5.1  # a specific ref, or roll back
```

`agnova/upstream.lock` records the ref **and the commit** — tags can be moved
upstream, commits cannot. An update is a one-line reviewable diff, then
`bin/agnova install && bin/agnova restart && bin/agnova selftest`.

Before this existed, `install` cloned upstream's default branch, so every fresh
container silently ran whatever shipped that day.

## Operator setup

Three environment variables and, if the agent should look owned, one
attestation. All of it — including the environment setup script that removes the
4-minute rebuild after a reclaim — is in
[docs/how-to/set-up-the-environment.md](../docs/how-to/set-up-the-environment.md).

## Requirements

`bin/agnova install` handles all of it (~4 minutes, once per container):

- `buzz-cli` and `buzz-acp`, built from source — block/buzz ships `buzz-cli`
  only inside desktop installers, so building is the documented path.
- `@agentclientprotocol/claude-agent-acp` for Claude Code.
- Python 3.11+ with `coincurve`.

**No `ANTHROPIC_API_KEY` is required.** The Claude adapter wraps the Agent SDK,
which spawns the local `claude` binary and inherits its subscription auth
(verified end to end, 2026-07-30). `IS_SANDBOX=1` is set for you because these
containers run as root.

## Limits worth knowing before you rely on it

- **The agent dies when the sandbox is reclaimed.** Nothing in this runtime
  changes that, and nothing inside a container can — see
  [session-lifetime.md](../docs/explanation/session-lifetime.md). Pair it with a
  scheduled Routine if you need a floor under the availability.
- **One harness per identity.** Two processes signing as the same agent means
  every message is answered twice. `bin/agnova up` is idempotent, but do not start a
  second harness by hand.
- **The front door holds the agent's private key**, because re-signing is the
  whole job. It only ever re-signs events already carrying that agent's pubkey,
  so it cannot launder another identity — but it is a key-bearing process, and
  it binds to loopback only. Do not expose it on `0.0.0.0`.
