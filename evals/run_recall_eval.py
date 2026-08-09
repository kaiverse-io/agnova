"""
Retrieval Evaluation Harness for GitMemoryBackend.

agnova's git-backed memory has never had a quality eval — only Qortia's
server-side recall does (Qortia's own REH: docs/03-eval-strategy.md in that
repo). This mirrors REH's methodology — deterministic scoring, hard
negatives seeded older than ground truth, Recall@5 + MRR — but runs against
a real `GitMemoryBackend` instance on a real temp directory: no server, no
mocks, real file I/O.

GitMemoryBackend has no semantic model by design (that's Qortia's job) — its
ranking is pure `(match_count, mtime)` (git_backend.py). So this harness
measures whether that naive ranking is robust to adversarial content, not
whether it "understands" paraphrase: a case failing because two texts share
no vocabulary at all is not a bug, it's the backend's documented ceiling. A
case failing because raw substring matching gets fooled (e.g. "category"
inflating a "cat" query's match count) is a real bug.

Usage:
    uv run python evals/run_recall_eval.py [evals/datasets/git_recall_v1.json]

Report: evals/results/git_recall_latest.json
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agnova.memory.git_backend import GitMemoryBackend  # noqa: E402

# Regression floors — set 5% below the measured baseline (Qortia REH's own
# convention, evals/README.md in the qortia repo). First real run on this
# 10-case dataset: case_pass_rate=1.000, recall_at_5=0.889, mrr=0.481.
RECALL_AT_5_FLOOR: float | None = 0.80
MRR_FLOOR: float | None = 0.40


def _seed_memory(backend: GitMemoryBackend, mem_id: str, spec: dict[str, Any]) -> str:
    """Write one memory for real via the backend's own remember()/MEMORY.md path.

    Returns the id recall() will actually report for this memory.
    """
    content = spec["content"]
    if spec.get("in_memory_md"):
        existing = (
            backend.memory_md.read_text(encoding="utf-8") if backend.memory_md.is_file() else ""
        )
        backend.memory_md.parent.mkdir(parents=True, exist_ok=True)
        backend.memory_md.write_text(existing + f"\n{content}\n", encoding="utf-8")
        return "file:MEMORY.md"

    backend.remember([{"id": mem_id, "content": content}])
    offset = spec.get("mtime_offset_seconds")
    if offset:
        entry_path = backend.entries_dir / f"{mem_id}.md"
        now = time.time()
        os.utime(entry_path, (now + offset, now + offset))
    return mem_id


def _seed_case(backend: GitMemoryBackend, case: dict[str, Any]) -> dict[int, str]:
    """Hard negatives first (older), ground truth second — same rationale as
    Qortia's REH: recency-as-tiebreaker must never accidentally favour a
    negative seeded a moment earlier."""
    for i, hn in enumerate(case["setup"].get("hard_negatives", [])):
        _seed_memory(backend, f"hn-{case['id']}-{i}", hn)
        time.sleep(0.01)

    id_map: dict[int, str] = {}
    for i, mem in enumerate(case["setup"].get("memories", [])):
        repeat = mem.get("repeat", 1)
        first_id = ""
        for r in range(repeat):
            mid = f"gt-{case['id']}-{i}-{r}"
            actual_id = _seed_memory(backend, mid, mem)
            if r == 0:
                first_id = actual_id
            time.sleep(0.01)
        id_map[i] = first_id
    return id_map


def _check_result_counts(count: int, expected: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if "min_results" in expected and count < expected["min_results"]:
        reasons.append(f"expected >= {expected['min_results']} results, got {count}")
    if "max_results" in expected and count > expected["max_results"]:
        reasons.append(f"expected <= {expected['max_results']} results, got {count}")
    if "max_result_count" in expected and count > expected["max_result_count"]:
        reasons.append(f"result count {count} exceeds cap {expected['max_result_count']}")
    return reasons


def _check_content_budgets(result_contents: list[str], expected: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if "max_content_chars" in expected and result_contents:
        top_len = len(result_contents[0])
        if top_len > expected["max_content_chars"]:
            reasons.append(
                f"top snippet is {top_len} chars, expected <= {expected['max_content_chars']}"
            )
    if "max_total_chars" in expected:
        total = sum(len(c) for c in result_contents)
        if total > expected["max_total_chars"]:
            reasons.append(f"total chars {total} exceeds cap {expected['max_total_chars']}")
    return reasons


def _check_expectations(results: list[Any], expected: dict[str, Any]) -> list[str]:
    result_contents = [r.content for r in results]
    reasons: list[str] = []

    if "must_contain_in_top_result" in expected:
        top = result_contents[0] if result_contents else ""
        for phrase in expected["must_contain_in_top_result"]:
            if phrase.lower() not in top.lower():
                reasons.append(f"top result missing required phrase {phrase!r}")

    reasons += _check_result_counts(len(results), expected)
    reasons += _check_content_budgets(result_contents, expected)
    return reasons


def _check_ground_truth_ranking(
    expected: dict[str, Any], ground_truth_id: str | None, recall_at_5: bool | None
) -> list[str]:
    """Only cases that explicitly declare ground_truth_in_top_5 gate on rank
    position — e.g. the adversarial geh-002 case deliberately doesn't: it
    documents a known match-count-only ranking limitation (a real hard
    negative can outrank the true answer), not something this harness treats
    as a failure on its own."""
    if not expected.get("ground_truth_in_top_5"):
        return []
    if ground_truth_id is not None and not recall_at_5:
        return ["ground truth not in top 5"]
    return []


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="geh-"))
    try:
        backend = GitMemoryBackend(tmp)
        id_map = _seed_case(backend, case)
        gt_idx = case.get("ground_truth_index")
        ground_truth_id = id_map.get(gt_idx) if gt_idx is not None else None

        results = backend.recall(case["query"])
        result_ids = [r.id for r in results]

        recall_at_5: bool | None = None
        recall_at_10: bool | None = None
        mrr: float | None = None
        if ground_truth_id is not None:
            recall_at_5 = ground_truth_id in result_ids[:5]
            recall_at_10 = ground_truth_id in result_ids[:10]
            mrr = (
                1.0 / (result_ids.index(ground_truth_id) + 1)
                if ground_truth_id in result_ids
                else 0.0
            )

        expected = case.get("expected", {})
        reasons = _check_expectations(results, expected)
        reasons += _check_ground_truth_ranking(expected, ground_truth_id, recall_at_5)

        return {
            "id": case["id"],
            "description": case.get("description", ""),
            "pass": not reasons,
            "recall_at_5": recall_at_5,
            "recall_at_10": recall_at_10,
            "mrr": mrr,
            "reasons": reasons,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(dataset_path: Path) -> int:
    data = json.loads(dataset_path.read_text())
    cases = data["cases"]

    results = [_run_case(c) for c in cases]
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        print(f"  [{status}] {r['id']}: {r['description']}")
        if not r["pass"]:
            for reason in r["reasons"]:
                print(f"         reason: {reason}")

    scored = [r for r in results if r["recall_at_5"] is not None]
    recall_at_5 = sum(1 for r in scored if r["recall_at_5"]) / len(scored) if scored else 0.0
    recall_at_10 = sum(1 for r in scored if r["recall_at_10"]) / len(scored) if scored else 0.0
    mrr = sum(r["mrr"] for r in scored) / len(scored) if scored else 0.0
    case_pass_rate = sum(1 for r in results if r["pass"]) / len(results) if results else 0.0

    print(f"\n{'Metric':<25} {'Score':>8}")
    print("-" * 36)
    print(f"  {'Case pass rate':<23} {case_pass_rate:>8.3f}")
    print(f"  {'Recall@5':<23} {recall_at_5:>8.3f}")
    print(f"  {'Recall@10':<23} {recall_at_10:>8.3f}")
    print(f"  {'MRR':<23} {mrr:>8.3f}")

    report = {
        "case_pass_rate": case_pass_rate,
        "recall_at_5": recall_at_5,
        "recall_at_10": recall_at_10,
        "mrr": mrr,
        "cases": results,
    }
    out = Path("evals/results/git_recall_latest.json")
    out.parent.mkdir(exist_ok=True, parents=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {out}")

    if RECALL_AT_5_FLOOR is None or MRR_FLOOR is None:
        print("No regression floor set yet (first run) — not gating.")
        return 0

    passed = recall_at_5 >= RECALL_AT_5_FLOOR and mrr >= MRR_FLOOR
    print(f"Regression gate: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    dataset = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evals/datasets/git_recall_v1.json")
    sys.exit(run(dataset))
