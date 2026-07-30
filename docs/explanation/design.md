# Design

Why this runtime exists, what it refuses to do, and the reasoning behind the
parts that look strange.

## Motivation

An agent that only answers when someone opens a terminal is not a colleague. To
be genuinely present in a team's chat, an agent needs to be *there* — reachable
in the same rooms, visible as online, answering in seconds rather than at the
top of the hour.

[Buzz](https://github.com/block/buzz) already solves the hard parts of that.
Its `buzz-acp` harness holds a live WebSocket to the relay, decides which events
are addressed to the agent, deduplicates and queues them, keeps agent processes
warm, publishes presence and typing indicators, and speaks ACP to Claude Code,
goose or Codex.

The obstacle was environmental. In a Claude Code cloud sandbox — the cheapest
place to run an agent that already has a repository, a memory directory and a
model subscription — `buzz-acp` could not connect. Everything here follows from
removing that single obstacle, and from a decision about what *not* to build.

## The decision that shapes everything

The first version of this repo answered "we can't hold a socket" by writing an
HTTP poll loop: fetch new messages every 15 seconds, filter them, compose a
reply with `claude -p`, track a cursor in git.

It worked. It was also ~950 lines reimplementing — badly, and in a way that
drifted further from upstream with each Buzz release — what `buzz-acp` already
did. Mention filtering, exactly-once handling, prompt assembly, reply gating:
all of it existed upstream, maintained by the people who define the semantics.

**The rule this runtime is built on: own the stable half, rent the moving
half.** The Nostr relay protocol is frozen. Buzz's agent semantics are not.
So we own ~780 lines of transport and lifecycle, and rent ~34,500 lines of
agent behaviour. When Buzz changes how agents work, we update a binary.

Everything that looks like an omission here is that rule being applied. There
is no reply filter, no cursor, no prompt template, no dedup logic — not because
they are unnecessary, but because they are not ours.

## The transport problem

Outbound traffic from the sandbox is meant to go through an agent proxy. There
are two ways to reach a host through it:

| path | WebSocket upgrade |
|---|---|
| `CONNECT` tunnel (the polite path) | **101 Switching Protocols** |
| direct dial, transparently intercepted | **403 Forbidden** |

`buzz-cli` takes the first path because reqwest reads `HTTPS_PROXY`.
`buzz-acp` takes the second because `tokio-tungstenite` does not. That is the
entire difference — not policy, not TLS configuration, not the relay.

This took four rounds to establish, and three of them reached a confident wrong
answer:

1. `buzz-acp` failed with `UnknownIssuer`, which was written up as proof that
   WebSockets were blocked. It proved nothing of the sort — TLS fails before an
   upgrade is attempted. The real cause was a compile-time CA-store choice
   (`rustls-tls-webpki-roots`).
2. Rebuilt with native roots; TLS succeeded and the failure became `403` on the
   upgrade. Read as a policy block. A second, non-Rust client failed the same
   way, which felt like corroboration — both were dialling direct.
3. A design was written for a ~300-line shim that would emulate a Nostr relay
   over the HTTP bridge, since "push is impossible here."
4. Someone finally tried the same upgrade through a `CONNECT` tunnel. `101`.

The lesson is in the repo because it is the expensive kind: two independent
clients failing identically is not corroboration when they share the mistake.

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │  sandbox (loopback: proxy-exempt)   │
   Buzz relay ◀─────┤                                     │
   (CONNECT+TLS)    │  front door ◀──ws://127.0.0.1──┐    │
                    │   (holds the key, re-signs)    │    │
                    │                            buzz-acp │
                    │                                │    │
                    │                         stdio (ACP) │
                    │                                ▼    │
                    │                     claude-agent-acp│
                    │                                │    │
                    │                         buzz-cli ───┼──▶ relay (HTTP)
                    └─────────────────────────────────────┘
```

The agent replies using `buzz-cli`, which reaches the relay directly over HTTP
— it already honours the proxy, so it never needs the front door.

### Why the front door rewrites three things

All three have the same root cause: `buzz-acp` believes it is talking to
`127.0.0.1`, and the relay checks that belief in three independent places.

**1. The `Host` header.** The relay binds each connection to a community by
hostname, and a CDN sits in front of it. A request arriving with
`Host: 127.0.0.1:8443` is rejected with 403 before the relay ever sees it.

**2. The NIP-98 `Authorization` token.** `buzz-acp` also uses the relay's HTTP
bridge (`POST /query`, `POST /events`) — channel discovery runs entirely over
it — and derives that base URL from the relay URL it was given. NIP-98 signs the
request URL in a `u` tag. Buzz's verifier treats `localhost`, `127.0.0.1` and
`::1` as three distinct hosts *on purpose*, documented as closing a
host-check side door. A token minted for loopback can never verify upstream, so
the front door discards it and mints its own.

**3. The NIP-42 `AUTH` event's `relay` tag.** The relay reconstructs its own
public URL from config plus the bound tenant and compares it, scheme included,
against the tag. A client connected to loopback signs the loopback URL and is
rejected with `auth-required: verification failed`. The front door rewrites
that one tag and re-signs, preserving every other tag — notably a NIP-OA `auth`
tag, without which an owner-attested agent silently loses its attestation.

2 and 3 are why the front door needs the agent's private key. It re-signs only
events already carrying that agent's pubkey; anything else passes through
untouched, so it cannot launder another identity.

### Why one request per connection

The front door forces `Connection: close` on forwarded HTTP requests. That is a
correctness requirement, not a simplification: a client reusing a connection
sends its second request without a fresh head, which would reach the relay with
the loopback `Host` and a loopback-signed token. The CDN answers 403. Request
volume here is a handful per minute, so the cost is irrelevant and the failure
mode it prevents is silent and confusing.

### Why frames are parsed only until AUTH

The WebSocket client-to-relay direction is parsed frame by frame *only* until
the NIP-42 `AUTH` frame has been handled, then the connection drops to raw
byte piping. There is nothing left to correct after that, and every frame we
re-encode is a frame we can corrupt.

## What this runtime does not fix

**Reclaim.** `buzz-acp` is a long-lived process in a sandbox that is reclaimed
after conversational inactivity. Nothing here changes that, and nothing running
*inside* a container can — see
[session-lifetime.md](session-lifetime.md). If an
availability floor matters, put a scheduled Routine underneath.

**Two harnesses, one identity.** Nothing prevents someone starting a second
harness by hand with the same key. Every message would then be answered twice.
`bin/agnova up` is idempotent; discipline covers the rest.

## Trade-offs taken deliberately

| Choice | Cost | Why anyway |
|---|---|---|
| Rent `buzz-acp` rather than own a poll loop | a Rust binary to build; upstream can break us | agent semantics are Buzz's to define, and ours drifted |
| Front door holds the private key | a key-bearing process on the host | re-signing *is* the fix; loopback-only binding bounds it |
| `Connection: close` per request | an extra TCP+TLS handshake per call | eliminates a silent 403 class |
| Python for the front door | a second language in the tree | ~330 lines of asyncio against a frozen protocol, readable by anyone |
| Agent home defaults to the repo | the agent boots with full repo context | the pool keeps processes warm, so it is paid once per process, not per message |
