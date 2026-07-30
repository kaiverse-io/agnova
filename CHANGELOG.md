# Changelog

Notable changes to Agnova. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [Unreleased]

Extracted from the agent repository it was built in. No functional change from
the code that was verified running there.

### Added

- **Front door** (`agnova/frontdoor.py`) — a local plaintext entrance to a Buzz
  relay, so stock `buzz-acp` runs in environments where it cannot dial the relay
  directly. Corrects the `Host` header, re-signs NIP-98 `Authorization`, and
  rewrites and re-signs the NIP-42 `AUTH` event's `relay` tag while preserving
  every other tag.
- **Supervisor** (`agnova/supervise.py`) — idempotent `up`/`down`/`restart`/
  `status`/`doctor`/`logs` over front door, harness and checkpoint.
- **Checkpoint** (`agnova/checkpoint.py`) — commits and pushes named paths on an
  interval so an agent's memory survives an ephemeral host. No model, no tokens.
  Opt-in and explicitly scoped.
- **Upstream pinning** (`agnova/upstream.lock`, `upstream.py`) — the `buzz-acp`
  version is pinned by ref *and* commit, with `upstream-check` / `upstream-update`
  for reviewable moves and rollbacks.
- **Selftest** (`agnova/selftest.py`) — three assertions that prove the transport
  against a live relay without spending a token.
- **NIP-OA minting** (`src/agnova/mint_auth_tag.py`) — owner attestation, run offline
  by a human, self-checked against the spec's published test vector.
- 26 offline unit tests.

### Fixed

Found by an audit after the code was written and believed correct; all three now
have regression tests.

- **bech32 checksum was not verified.** A mistyped or truncated `nsec` decoded
  into 32 different-but-plausible bytes instead of failing, so an agent would
  start cleanly under an identity nobody owned and not recognise its own
  messages.
- **`.env` parsing corrupted quoted values containing `#`.** Comment-stripping
  ran before quote handling, truncating the value and storing a mangled fragment.
- **The CONNECT handshake and upstream TLS setup had no timeouts**, unlike every
  other read in the file — a proxy that accepted a connection and never answered
  leaked one parked connection per attempt, in exactly the failure mode the front
  door exists to survive.
