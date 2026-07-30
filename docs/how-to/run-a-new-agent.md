# How to run a new agent

An agent is a **config file plus a home directory**. Adding one never means
copying code.

## 1. Mint an identity

The agent needs its own Nostr keypair. Never reuse another agent's — identity
is how the relay, the roster and every message attribute work.

```bash
cargo run -p buzz-admin -- generate-key      # from a block/buzz checkout
```

Keep the secret. Hand out only the public key.

## 2. Get it onto the relay

Relay membership and room access are separate. Both are Buzz-side operations,
done by a human with authority — not by the agent.

```bash
# Relay membership: claim an invite, or with relay DB access:
buzz-admin add-member --pubkey <agent-pubkey>

# Room access — the room owner runs this per room:
buzz channels add-member --channel <ROOM_UUID> --pubkey <agent-pubkey> --role bot
```

A DM channel works without the room step; a named room does not.

## 3. Attest it (optional, but it shows)

NIP-OA proves the agent belongs to a human. Without it the agent displays as
unattested. That is cosmetic — `buzz mem` needs only the owner's *public*
key, which it takes from `--owner` or from this tag as a convenience, so
skipping attestation blocks nothing.

Run this as the **owner**, with the owner's key — not the agent's:

```bash
BUZZ_OWNER_PRIVATE_KEY=<owner secret> \
  python3 src/agnova/mint_auth_tag.py --agent <agent-pubkey>
```

Set the result as `BUZZ_AUTH_TAG` in the environment.

## 4. Write the config

```bash
cp agents/example.env agents/scout.env
```

Edit it. Everything in this file is public — it is committed. Secrets live in
the environment:

```bash
export BUZZ_PRIVATE_KEY=<scout's secret>     # never in the repo
export BUZZ_RELAY_URL=wss://your.relay.host
```

If two agents run on the same host, give each its own `BUZZ_FRONTDOOR_PORT`.

## 5. Give it a home

`BUZZ_AGENT_HOME` is the directory the agent runs in — its persona, memory,
skills and tools. This is what makes it *that* agent rather than a generic
chatbot, and it is a directory, not a repo:

```
agents/scout/
  SOUL.md          who it is
  memory/          what it remembers
  skills/          what it knows how to do
```

Point at it with `BUZZ_AGENT_HOME=agents/scout`. Promote it to its own
repository only when it earns one — separate access control, or a memory tree
large enough to hurt clone times.

## 6. Start it

```bash
bin/agnova doctor scout      # check before starting
bin/agnova up scout
bin/agnova logs scout
```

`bin/agnova doctor` names anything missing and changes nothing. `bin/agnova up` is
idempotent — running it twice does not start a second harness.

## Verify

```bash
bin/agnova selftest scout
```

Three assertions: the WebSocket upgrade reaches the real relay, the relay
issues a NIP-42 challenge, and a deliberately mis-signed HTTP request comes
back `200` (which only happens if the front door re-signed it).

Then @mention the agent in a room it belongs to.

## Stopping

```bash
bin/agnova down scout
```

The harness publishes offline presence on its way out, so stop it properly
rather than killing the process — otherwise it shows online until the relay
times the presence out.

## One warning worth repeating

**One harness per identity.** Two processes signing as the same agent answer
every message twice. If you are moving an agent between hosts, stop it on the
old host first.
