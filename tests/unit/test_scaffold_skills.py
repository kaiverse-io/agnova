"""Scaffolded skills must be valid SKILL.md files, and `skills/INDEX.md` must list them."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agnova import scaffold


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the simple `key: value` frontmatter a starter skill uses; fail on anything odd."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path}: no frontmatter block"
    end = lines.index("---", 1)
    out: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, raw = line.partition(": ")
        raw = raw.strip()
        if raw.startswith('"'):
            out[key] = json.loads(raw)
        else:
            assert ": " not in raw, f"{path}: unquoted ': ' in {key}"
            out[key] = raw
    return out


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(scaffold, "_running_version", lambda: ("v0.1.0", "abc123"))
    assert scaffold.init("Scout", tmp_path) == 0
    return tmp_path


def test_scaffolded_skills_have_valid_frontmatter(agent: Path) -> None:
    skills = sorted(p for p in (agent / "skills").iterdir() if p.is_dir())
    assert skills, "the scaffold ships no starter skill"
    for skill in skills:
        meta = _frontmatter(skill / "SKILL.md")
        assert meta["name"] == skill.name
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"])
        assert 0 < len(meta["description"]) <= 1024


def test_scaffolded_skills_match_the_index(agent: Path) -> None:
    index = (agent / "skills/INDEX.md").read_text(encoding="utf-8")
    index = re.sub(r"<!--.*?-->", "", index, flags=re.DOTALL)  # the format hint is not an entry
    listed = set(re.findall(r"\]\(([a-z0-9-]+)/SKILL\.md\)", index))
    on_disk = {p.name for p in (agent / "skills").iterdir() if p.is_dir()}
    assert listed == on_disk
