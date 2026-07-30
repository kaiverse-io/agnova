# Session lifetime, idleness, and why nothing here can prevent reclaim

An agent running in a Claude Code cloud sandbox dies when the sandbox is
reclaimed. This page records what "idle" actually means, what was measured, and
why no amount of cleverness inside the container changes the outcome.

## What the platform says

> **Environment expired** — Cloud sessions stop after a period of inactivity and
> the session's VM is reclaimed. On the web, the session is marked expired in
> the session list. Reopen the session from claude.ai/code to provision a fresh
> VM with your conversation history restored.
>
> — [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)

No timeout is published. The docs also note that a session left waiting on a
question can still be answered "up to environment expiry", which tells us the
clock is real but not how long it runs.

## What "inactivity" actually measures

This is the part worth being precise about, because the intuitive answer is
wrong.

**Inactivity means no conversation turns. It does not mean an idle machine.**

Measured directly: an earlier version of this agent ran a poll loop that, for
five and a half hours, made an HTTP request every 15 seconds, wrote files, made
git commits, and pushed them to GitHub. The container was doing continuous CPU,
disk, network and outbound-git work the entire time. It logged **1,204 clean
cycles and then stopped** — reclaimed at roughly **5h32m** with no conversation
turn in that window.

So the clock is driven by the session, not the sandbox. Busy processes do not
reset it, and no process inside the container can reset it, because reclaim is
decided by the platform on the conversation — a thing the container cannot
reach from the inside.

Treat ~5h30m as one observation, not a specification. It is not documented and
should not be relied on.

## What actually resets the clock

A turn in the conversation. There are exactly two ways to cause one without a
human typing:

| mechanism | shape | cost |
|---|---|---|
| [Routines](https://code.claude.com/docs/en/routines) | a cron schedule that fires a prompt into a session | one model turn per firing |
| `send_later` | a one-shot scheduled message into this session | one model turn |

Both cost real tokens every time they fire, and that cost is not small: a
scheduled firing that boots an agent's full instruction set can run tens of
thousands of tokens before it does anything useful. An earlier iteration of this
setup learned that the expensive way — roughly eleven scheduled sessions
overnight, each performing a full agent boot, consumed a third of a daily
allowance while the agent itself answered one message.

So a keep-warm ping is not free, and a *frequent* keep-warm ping is actively
expensive. Anything scheduled should be as small as it can be: a single
narrowly-scoped instruction, not a session that reads a memory directory first.

## The honest conclusion

There is no keep-alive to bake into the runtime. Uptime in a cloud sandbox is
bought with scheduled turns, and each one costs tokens — so it is a budget
decision, not an engineering one, and it belongs in the operator's hands rather
than hidden inside a supervisor.

The design that follows from this is two layers, not one:

- **The runtime is the good experience.** While a sandbox is alive, `buzz-acp`
  holds a real socket, so replies are pushed, presence is genuine, and latency
  is a network round trip.
- **A scheduled Routine is the floor.** It cannot make the agent fast, only
  present. Size the interval by what an outage costs, not by what feels
  responsive.

If continuous availability actually matters, the correct answer is not a
cleverer sandbox — it is a host that is not reclaimed. `buzz-acp` runs
unmodified on any always-on machine, and on such a host the front door in this
runtime is unnecessary: point `BUZZ_RELAY_URL` straight at the relay and delete
it from the picture.
