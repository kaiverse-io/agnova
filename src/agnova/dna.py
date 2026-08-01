"""DNA integrity — fail-closed at boot when AGENT_DNA_HASH is set.

Hash algorithm (v0.1, must match Aither overlay):
  For each path in AGENT_DNA_PATHS (or defaults), if the file exists relative
  to the agent home, append UTF-8 bytes of ``{relpath}\\0{content}\\n``.
  digest = sha256 of the concatenation; stored/compared as ``sha256:{hex}``.

Missing files among the configured list are skipped (so overlays can omit
optional USER.md). An empty concatenation is still a defined hash.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path

DEFAULT_DNA_PATHS: tuple[str, ...] = (
    "DNA.md",
    "IDENTITY.md",
    "SOUL.md",
    "DOMAIN.md",
    "AGENTS.md",
    "USER.md",
)


def parse_hash(value: str) -> str:
    """Normalize to lowercase hex without prefix."""
    raw = value.strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError(f"invalid AGENT_DNA_HASH: {value!r}")
    return raw


def compute_hash(home: Path, paths: Sequence[str]) -> str:
    """Return ``sha256:{hex}`` for the ordered DNA package under home."""
    h = hashlib.sha256()
    for rel in paths:
        path = (home / rel).resolve()
        try:
            path.relative_to(home.resolve())
        except ValueError as exc:
            raise ValueError(f"DNA path escapes home: {rel}") from exc
        if not path.is_file():
            continue
        content = path.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(content)
        h.update(b"\n")
    return f"sha256:{h.hexdigest()}"


def lock_readonly(home: Path, paths: Sequence[str]) -> None:
    """Best-effort chmod a-w. Soft on root; hash-at-boot is the real gate."""
    for rel in paths:
        path = home / rel
        if not path.is_file():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def enforce(
    home: Path,
    *,
    expected: str | None,
    paths: Sequence[str] | None = None,
    readonly: bool = True,
) -> str | None:
    """Verify DNA hash when expected is set. Returns computed hash or None if skipped.

    Raises SystemExit on mismatch (fail-closed).
    """
    dna_paths = list(paths) if paths is not None else list(DEFAULT_DNA_PATHS)
    if not expected or not expected.strip():
        return None
    want = parse_hash(expected)
    got = compute_hash(home, dna_paths)
    got_hex = parse_hash(got)
    if got_hex != want:
        raise SystemExit(
            f"DNA integrity check failed — expected sha256:{want}, got {got}. "
            "Refusing to start (fail-closed)."
        )
    if readonly:
        lock_readonly(home, dna_paths)
    return got


def paths_from_env(raw: str | None = None) -> list[str]:
    value = (raw if raw is not None else os.environ.get("AGENT_DNA_PATHS", "")).strip()
    if not value:
        return list(DEFAULT_DNA_PATHS)
    return [p.strip() for p in value.split(",") if p.strip()]
