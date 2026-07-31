# Set up the environment

Everything an operator has to do by hand, once, so that any fresh container can
bring an agent up without further help.

Read this end to end before starting — step 4 depends on a value produced in
step 3.

---

## 1. The setup script (CCR environment settings)

A cloud sandbox is reclaimed after inactivity, and the replacement arrives with
nothing built. Without this step, every reclaim costs ~4 minutes of manual
rebuilding before an agent can start.

Paste this into **the environment's setup script** field (claude.ai → Claude
Code → environments). It runs once when a container is created:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Build the pinned upstream. The version comes from agnova/upstream.lock in
# the repo, never from upstream's default branch — see that file for why.
cd "$(git rev-parse --show-toplevel)"
eval "$(python3 -m agnova.upstream clone-args)"

# `cargo install`, never `cargo build`: it places the binary in ~/.cargo/bin,
# which survives a sandbox reclaim. A build tree under /tmp does not — measured.
cargo install --path /tmp/buzz/crates/buzz-acp --locked

# The ACP adapter for Claude Code.
npm install -g @agentclientprotocol/claude-agent-acp

# Signing, for the front door.
pip install --quiet coincurve

# Optional: the agent's Buzz *tool* surface. Only needed if you set
# BUZZ_ACP_MCP_COMMAND — replies do not go through it. Costs ~2 min to build.
# cargo install --path /tmp/buzz/crates/buzz-cli --locked
```

This does **not** start the agent — see step 6.

`buzz-cli` is deliberately commented out. `buzz-acp` publishes replies over its
own relay socket; `buzz-cli` is an MCP server the agent may call as a *tool*, and
only when `BUZZ_ACP_MCP_COMMAND` names it — which is empty by default. Installing
it unconditionally builds a Rust crate that most agents never invoke.

---

## 2. Environment variables

Three, and only one of them is genuinely optional.

Set these in **CCR environment settings**, never in the repo:

| Variable | Secret | Required | What |
|---|---|---|---|
| `BUZZ_PRIVATE_KEY` | **yes** | yes | The agent's Nostr secret (`nsec1…` or 64-char hex). Its identity. |
| `BUZZ_RELAY_URL` | no | yes | The real relay, e.g. `wss://example_project.communities.buzz.xyz`. |
| `BUZZ_AUTH_TAG` | **yes** | recommended | Owner attestation from step 3. Without it the agent shows as unattested. Cosmetic: nothing is blocked by its absence. |

**Do not set these** — the runtime sets them and an override will break things:

| Variable | Set to | Why |
|---|---|---|
| `BUZZ_RELAY_URL` (for the harness) | `ws://127.0.0.1:<port>` | The harness talks to the front door; only the front door knows the real relay. |
| `IS_SANDBOX` | `1` | These containers run as root, and the Claude Agent SDK otherwise refuses to spawn its subprocess. |

Everything else lives in `agents/<name>.env`, is committed, and is not secret —
label, owner pubkey, respond-to gate, agent command, home directory, front door
port. Full list in [configuration.md](../reference/configuration.md).

**The public key is never configured.** It is always derived from
`BUZZ_PRIVATE_KEY`. A mismatched pair would make an agent fail to recognise its
own messages and answer itself forever, so the runtime makes that
unrepresentable.

---

## 3. Owner attestation (NIP-OA)

### What it is

A signature from *your* key saying "this agent is mine." Agents created through
Buzz Desktop's agent flow get one automatically. An agent that joined by
claiming an invite — as Scout did — does not, which is why Buzz shows
"owner unavailable" on its profile.

It is **cosmetic for this runtime**. `buzz mem` needs the owner's *public* key
to derive the agent-owner encryption key, and takes it from `--owner` or from
this tag as a convenience — so nothing is blocked by skipping attestation.

### What you need

| | |
|---|---|
| Your owner private key | as `BUZZ_OWNER_PRIVATE_KEY` in your shell |
| The agent's **public** key | 64-char hex. Scout's: `<agent-pubkey>` |
| Python with `coincurve` | `pip install coincurve` |

### Run it on your own machine, not in a sandbox

This is the one step that should not happen in a cloud container. Your owner
key is strictly more powerful than any agent key — it attests to *all* your
agents — and pasting it into a sandbox puts it in that container's environment
and potentially in a session transcript.

```bash
# On your machine, from a checkout of this repo:
BUZZ_OWNER_PRIVATE_KEY=<your secret> \
  python3 src/agnova/mint_auth_tag.py --agent <agent-pubkey-hex>
```

Verify the tool first if you like — it checks itself against NIP-OA's published
test vector and touches no keys of yours:

```bash
python3 src/agnova/mint_auth_tag.py --selftest
# digest matches spec vector: True
# owner pubkey derivation:    True
# spec signature verifies:    True
# SELFTEST: PASS
```

### Optional: bound its lifetime

```bash
--conditions 'kind=9&created_at<1800000000'
```

An attestation is a **reusable capability**. Scoping it with an expiry gives you
revocation without re-keying the agent. Without conditions it does not expire.

### Then

Set the printed JSON as `BUZZ_AUTH_TAG` (step 2). Treat it as a secret — keep
it out of chat and git, and re-mint if it leaks.

---

## 4. Relay membership and room access

Both are Buzz-side, and both need a human with authority. They are separate:
membership lets an agent connect; it does not put it in any room.

```bash
# Membership — claim an invite, or with relay DB access:
buzz-admin add-member --pubkey <agent-pubkey>

# Room access — the room owner runs this, per room:
buzz channels add-member --channel <ROOM_UUID> --pubkey <agent-pubkey> --role bot
```

A DM channel works without the room step. Named rooms do not.

---

## 5. Verify

```bash
bin/agnova doctor          # names anything missing, changes nothing
bin/agnova up
bin/agnova selftest        # three assertions, no tokens spent
```

`doctor` warns rather than fails when `BUZZ_AUTH_TAG` is unset, because an
unattested agent still works — it just looks unowned.

---

## 6. Optional: start automatically

The session-start hook can run `bin/agnova up`, so a reclaimed container brings the
agent back without anyone asking.

**Decide this deliberately.** It means every future session — including ones
opened for unrelated work — silently starts a process that posts to your
workspace under the agent's identity. That is a real escalation from "it starts
when I start it," and it is why the permission system refuses to wire it
without an explicit instruction.

---

## What none of this fixes

The agent still dies when the sandbox is reclaimed. Steps 1 and 6 make recovery
fast and automatic; they do not make it unnecessary, and nothing running inside
a container can — see
[session-lifetime.md](../explanation/session-lifetime.md).

For availability that does not depend on a session being open, run `buzz-acp` on
a host that is never reclaimed. There the front door is unnecessary: point
`BUZZ_RELAY_URL` straight at the relay.
