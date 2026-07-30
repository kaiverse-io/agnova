# Contributing

## Before anything else: what this project refuses to do

Agnova deliberately owns as little as possible. `buzz-acp` decides which events
deserve an answer, deduplicates them, queues them, assembles prompts, manages
agent processes and publishes presence. **Agnova does not, and a patch that adds
any of that will be declined** — however good the implementation.

The reasoning is in [design.md](docs/explanation/design.md), but in short: an
earlier version of this work reimplemented all of it, and every upstream release
made it more wrong. Own the stable half, rent the moving half.

Two other standing rules:

- **No agent memory in the runtime.** No agent's name, owner, pubkey, workspace,
  or persona belongs anywhere in `agnova/`. It takes paths and keeps them
  durable; what they contain is the agent's business. This is checked by reading,
  so please check it yourself before opening a PR.
- **No abstraction designed from one example.** The eventual protocol-agnostic
  core with pluggable adapters is real and wanted — after a second adapter
  exists, shaped by that one. Not before.

## Getting set up

```bash
bin/agnova install     # buzz-cli + buzz-acp at the pinned version, + the ACP adapter
bin/agnova test        # 26 offline unit tests
```

Python 3.11+. `coincurve` is the only runtime dependency and should stay that
way — this process holds a private key.

## Tests

```bash
bin/agnova test        # offline: no keys, no relay, no network. Runs anywhere.
bin/agnova selftest    # live probe: needs a real relay and a real agent key
```

Only the first belongs in CI. `selftest` opens a real WebSocket and signs with a
real identity; it is an operator tool.

**Every test in `tests/` corresponds to something that actually broke.** Three of
them cover defects found by audit after the code was written and believed
correct. New tests are welcome for real failure modes; tests that restate the
implementation are not.

If you change key handling, `.env` parsing, or URL derivation, add a case. Those
three areas have each produced a silent, hard-to-diagnose bug already.

## Changing the upstream pin

Do not edit `agnova/upstream.lock` by hand:

```bash
bin/agnova upstream-check
bin/agnova upstream-update           # newest release
bin/agnova upstream-update v0.5.1    # specific ref, or roll back
```

A pin bump is a behaviour change for every agent running this code, so it should
be its own commit with a note on what changed upstream — not folded into an
unrelated PR.

## Style

Match the surrounding code. Two things about it are intentional and worth
preserving:

- **Comments explain *why*, especially where the code looks odd.** `Connection:
  close` on every forwarded request is not an oversight, frames are parsed only
  until AUTH for a reason, and the CA bundle is discovered rather than
  hardcoded. If you remove a comment like that, the next person reverts your
  change and reintroduces the bug.
- **Failures are loud and specific.** Malformed key material raises; a missing
  secret exits with what to set; `doctor` reports rather than guesses. Silent
  fallbacks in a signing path are worse than a crash.

`ruff` config lives in `pyproject.toml`.

## Pull requests

Conventional-ish commit subjects (`agnova: …`, `docs: …`), imperative mood, and a
body explaining *why* when the change is not obvious.

State what you verified and how. "Ran the tests" is fine; "should work" is not —
this software signs messages under someone's identity, and a plausible-looking
change that breaks that fails quietly and confusingly.

If you find a defect while reading rather than while running, say so in the PR.
That is how three of the current tests came to exist.

## Security

Do not open a public issue for anything involving keys or signing as another
identity. See [SECURITY.md](SECURITY.md).
