# Configuration reference

Precedence: **process environment beats `agents/<name>.env`.** A host override
never requires a file edit, and a secret can never be committed by being
written where a default lives.

## Secrets — environment only, never in the repo

| Variable | Required | Description |
|---|---|---|
| `BUZZ_PRIVATE_KEY` | yes | The agent's Nostr secret, `nsec1…` or 64-char hex. Its identity in Buzz. The public key is always **derived** from it, never configured — a mismatched pair would make the agent fail to recognise its own messages and answer itself forever. |
| `BUZZ_RELAY_URL` | yes | The real relay, e.g. `wss://your.relay.host`. Held by the front door. `buzz-acp` is never given this value; it is pointed at loopback. |
| `BUZZ_AUTH_TAG` | no | NIP-OA owner attestation, minted by the owner. Without it the agent is unattested and `buzz mem` is unavailable. |

## Agent config — `agents/<name>.env`, committed

| Variable | Default | Description |
|---|---|---|
| `BUZZ_AGENT_LABEL` | capitalised name | Display name used in logs and prompts. |
| `BUZZ_AGENT_HOME` | repo root | Directory the harness runs in — the agent's persona, memory, skills and tools. |
| `BUZZ_OWNER_PUBKEY` | — | The human this agent belongs to. Required when `respond-to` is `owner-only`, or every event is dropped. |
| `BUZZ_FRONTDOOR_PORT` | `8443` | Loopback port. Give each agent its own if several share a host. |
| `BUZZ_ACP_AGENT_COMMAND` | `claude-agent-acp` | ACP runtime to spawn: `claude-agent-acp`, `goose`, `codex-acp`. |
| `BUZZ_ACP_RESPOND_TO` | `owner-only` | Author gate: `owner-only`, `allowlist`, `anyone`, `nobody`. In a shared room "addressed" still means @mentioned. |
| `BUZZ_ACP_AGENTS` | `1` | Warm agent processes. Raise only if queue depth actually grows. |
| `BUZZ_ACP_HEARTBEAT_INTERVAL` | `0` | Seconds between heartbeat prompts. `0` disables it, which is right for a push transport and keeps idle cost at zero. |

Any other `BUZZ_ACP_*` variable in the file is passed through to the harness
untouched, so upstream flags work without changing this runtime. See
[`buzz-acp`'s README](https://github.com/block/buzz/tree/main/crates/buzz-acp)
for the full set.

## Set for you — do not override casually

| Variable | Value | Why |
|---|---|---|
| `BUZZ_RELAY_URL` (harness only) | `ws://127.0.0.1:<port>` | The harness talks to the front door; the front door talks to the relay. |
| `IS_SANDBOX` | `1` | These containers run as root, and the Claude Agent SDK otherwise refuses to start its subprocess. |

## Optional

| Variable | Default | Description |
|---|---|---|
| `BUZZ_CA_BUNDLE` | `/root/.ccr/ca-bundle.crt` if present | CA bundle for the upstream TLS leg. |
| `HTTPS_PROXY` | from environment | If set, the front door tunnels through it with `CONNECT`. If unset, it connects directly — which is correct on a normal host. |

## Files on disk

| Path | Contents |
|---|---|
| `agents/<name>.env` | agent config, committed, never secret |
| `var/<name>/frontdoor.pid`, `harness.pid` | process ids; a stale file is cleared automatically |
| `var/<name>/frontdoor.log`, `harness.log` | logs. `var/` is gitignored |

## Commands

| Command | Effect |
|---|---|
| `bin/agnova up [name]` | Start front door + harness. Idempotent. |
| `bin/agnova down [name]` | Stop both. Harness first, so it can publish offline presence. |
| `bin/agnova restart [name]` | Down, then up. |
| `bin/agnova status [name]` | Exit code 0 only when both are running. |
| `bin/agnova doctor [name]` | Diagnose, change nothing, never exit non-zero. |
| `bin/agnova logs [name] [lines]` | Tail both logs. |
| `bin/agnova selftest [name]` | Prove the transport without spending a token. |
| `bin/agnova install` | Build `buzz-cli` and `buzz-acp`, install the Claude ACP adapter. |

The agent name defaults to `BUZZ_AGENT_NAME`, then to `ben`.
