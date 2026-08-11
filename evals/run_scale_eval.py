"""Scale + context-economy eval for GitMemoryBackend, on a real public corpus.

`run_recall_eval.py` measures ranking robustness on 10 hand-written cases whose
corpora are 2-4 documents. That is the wrong size to answer the question an
agent with a large memory actually has: once the corpus is bigger than the
recall budget, ranking stops being cosmetic and *becomes* context management —
it decides what the model sees and what it never learns exists. At 3 documents
everything relevant fits in the budget no matter how you sort it.

It is also the wrong size to evaluate BM25 specifically. Under the AND gate
every candidate contains every term, so document frequency equals the candidate
count and IDF is an identical constant on every document: at N=3 it cannot
discriminate anything, and length normalisation has nothing to normalise. A
BM25 result measured there says nothing about N=50,000.

So this harness uses a real, public, externally-judged corpus instead of
hand-written cases or generated text:

    BEIR / FiQA-2018 — 57,638 user-written financial forum posts, 648 test
    queries, 1,706 human relevance judgements.
    https://github.com/beir-cellar/beir  (CC BY-SA 4.0)

Real prose matters here because the properties under test are properties of the
*corpus*: length variance (FiQA: median 90 words, p90 270, max 2,973), natural
vocabulary distribution, and human-judged relevance. Synthetic text would just
encode whatever distribution the author imagined, which is the assumption being
tested.

Two arms, same corpus, same queries:

    baseline — GitMemoryBackend.recall() exactly as shipped: (match_count, mtime)
    bm25     — Bm25GitMemoryBackend below, an unshipped candidate

Metrics, beyond the usual recall/MRR:

    precision@budget  fraction of returned characters that belong to a judged
                      relevant document — how much of the context spend was
                      earned rather than wasted
    chars_returned    absolute context cost of one recall(); lower at equal
                      recall is strictly better
    zero_result_rate  queries returning nothing, which an agent answers by
                      re-querying or dumping raw context — a context failure
                      that never shows up in a ranking metric

Usage:
    uv run python evals/run_scale_eval.py [--sizes 1000,10000] [--queries 50]

The corpus is vendored, git-tracked, at evals/datasets/fiqa/ (~47MB uncompressed;
see evals/datasets/README.md for provenance and license) — a clone of this repo
runs the eval with no network access. Falls back to downloading into the
gitignored evals/.cache/ if the vendored copy is ever missing.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import re
import statistics
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agnova.memory import DEFAULT_MEMORY_TYPE, MemoryItem  # noqa: E402
from agnova.memory.git_backend import (  # noqa: E402
    _RECALL_MAX_HITS,
    _RECALL_MAX_TOTAL_CHARS,
    GitMemoryBackend,
    _snippet,
)

BEIR_URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/fiqa.zip"
CACHE = Path(__file__).resolve().parent / ".cache"
# Git-tracked copy — see evals/datasets/README.md. Checked first so a clone of this
# repo runs the eval offline; CACHE is only where a fresh `--refetch` would land.
VENDORED = Path(__file__).resolve().parent / "datasets" / "fiqa"

# Query-side stopwords. FiQA queries are full natural-language questions
# ("How to deposit a cheque issued to an associate in my business...") and
# recall() ANDs every term, so the raw question matches nothing — measured
# below as the zero-result rate. An agent would realistically pass content
# words, so the ranking arms are scored on those; the raw-question collapse is
# reported separately rather than hidden by this reduction.
_STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from had has have how i if in into is it
    its me my no not of on or should so than that the their them then there these they this to
    us was we what when where which who why will with would you your""".split()
)


# ── corpus ──────────────────────────────────────────────────────────────────


def _fetch() -> Path:
    """The vendored copy if present (the normal case — see evals/datasets/README.md),
    else the download cache, else download BEIR/FiQA fresh into the cache."""
    if (VENDORED / "corpus.jsonl").is_file():
        return VENDORED
    target = CACHE / "fiqa"
    if (target / "corpus.jsonl").is_file():
        return target
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"downloading {BEIR_URL} …", flush=True)
    with urllib.request.urlopen(BEIR_URL, timeout=300) as resp:  # noqa: S310 - pinned https host
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(CACHE)
    print(f"extracted to {target}", flush=True)
    return target


def _load(dataset: Path) -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    corpus = {}
    for line in (dataset / "corpus.jsonl").read_text(encoding="utf-8").splitlines():
        doc = json.loads(line)
        text = f"{doc.get('title', '')} {doc['text']}".strip()
        corpus[doc["_id"]] = text
    queries = {}
    for line in (dataset / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        queries[q["_id"]] = q["text"]
    qrels: dict[str, set[str]] = {}
    with (dataset / "qrels" / "test.tsv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if int(row["score"]) > 0:
                qrels.setdefault(row["query-id"], set()).add(row["corpus-id"])
    return corpus, queries, qrels


def _seed(home: Path, docs: dict[str, str]) -> None:
    """Write `docs` in exactly the layout remember() produces.

    remember() re-reads and rewrites the whole daily log on every call, so
    seeding 50k memories through it is quadratic. This writes the same bytes in
    one pass; `_verify_seed_matches_remember` asserts the two agree.
    """
    entries = home / "memory" / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    pointers = []
    for mid, content in docs.items():
        (entries / f"{mid}.md").write_text(
            f"---\nid: {mid}\ntype: {DEFAULT_MEMORY_TYPE}\n---\n\n{content}\n", encoding="utf-8"
        )
        pointers.append(f"- [{mid}] {content}")
    day = home / "memory" / f"{date.today().isoformat()}.md"
    # remember() rstrips the "# <date>\n\n" header before appending, so there is
    # no blank line between header and first pointer. Match it exactly.
    day.write_text(f"# {day.stem}\n" + "\n".join(pointers) + "\n", encoding="utf-8")


def _verify_seed_matches_remember() -> None:
    """The fast seeder must be byte-identical to remember(), or nothing below counts."""
    docs = {"aaa1": "the canary check runs first", "bbb2": "unrelated invoice note here"}

    slow = Path(tempfile.mkdtemp()) / "home"
    slow.mkdir(parents=True)
    backend = GitMemoryBackend(slow)
    backend.remember([{"id": k, "content": v} for k, v in docs.items()])

    fast = Path(tempfile.mkdtemp()) / "home"
    fast.mkdir(parents=True)
    _seed(fast, docs)

    for rel in ("memory/entries/aaa1.md", "memory/entries/bbb2.md"):
        assert (slow / rel).read_text() == (fast / rel).read_text(), f"seeder diverges at {rel}"
    slow_day = next((slow / "memory").glob("????-??-??.md"))
    assert slow_day.read_text() == (fast / "memory/2026-08-11.md").read_text(), (
        "seeder diverges on the daily log"
    )


# ── the candidate ranking ───────────────────────────────────────────────────


class Bm25GitMemoryBackend(GitMemoryBackend):  # type: ignore[misc]  # sys.path.insert import above is opaque to mypy, not a real Any
    """BM25 relevance instead of raw match count. Not shipped — under test.

    Kept in the eval rather than in src/ so the baseline arm exercises the real
    production recall() unchanged, and this only graduates if it earns it.
    """

    K1 = 1.5
    B = 0.75

    def recall(  # noqa: C901 — one-off eval harness variant, not shipped; not worth splitting
        self, query: str, filters: dict[str, Any] | None = None
    ) -> list[MemoryItem]:
        del filters
        terms = re.findall(r"\w+", query.lower())
        if not terms:
            return []

        paths: list[Path] = []
        if self.memory_md.is_file():
            paths.append(self.memory_md)
        if self.memory_dir.is_dir():
            paths.extend(self.memory_dir.glob("*.md"))
            if self.entries_dir.is_dir():
                paths.extend(self.entries_dir.glob("*.md"))
        entry_ids = (
            {p.stem for p in self.entries_dir.glob("*.md")} if self.entries_dir.is_dir() else set()
        )

        from agnova.memory.git_backend import _without_entry_pointers

        seen: list[tuple[str, float, str, dict[str, int], int]] = []
        doc_freq: Counter[str] = Counter()
        total_len = 0
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if path.parent == self.memory_dir:
                text = _without_entry_pointers(text, entry_ids)
            # One tokenising pass per document: word-level counts (matching the
            # baseline's \b-anchored matching, so "cat" never scores on
            # "category") and the length BM25 normalises by.
            tokens = Counter(re.findall(r"\w+", text.lower()))
            freqs = {t: tokens[t] for t in terms}
            doc_len = sum(tokens.values())
            total_len += doc_len
            for t, f in freqs.items():
                if f:
                    doc_freq[t] += 1
            if not all(freqs[t] for t in terms):
                continue  # same AND gate as the baseline, so only ranking differs
            snippet, count = _snippet(text, terms)
            if count == 0:
                continue
            mid = path.stem if path.parent == self.entries_dir else f"file:{path.name}"
            seen.append((mid, mtime, snippet, freqs, doc_len))

        n_docs = max(len(paths), 1)
        avg_len = (total_len / n_docs) or 1.0
        scored = []
        for mid, mtime, snippet, freqs, doc_len in seen:
            score = 0.0
            for term, freq in freqs.items():
                df = doc_freq[term]
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                norm = 1 - self.B + self.B * (doc_len / avg_len)
                score += idf * (freq * (self.K1 + 1)) / (freq + self.K1 * norm)
            scored.append((score, mtime, mid, snippet))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

        hits: list[MemoryItem] = []
        total_chars = 0
        for _score, _mtime, mid, snippet in scored[:_RECALL_MAX_HITS]:
            if total_chars + len(snippet) > _RECALL_MAX_TOTAL_CHARS:
                break
            hits.append(MemoryItem(id=mid, content=snippet, type=DEFAULT_MEMORY_TYPE))
            total_chars += len(snippet)
        return hits


# ── scoring ─────────────────────────────────────────────────────────────────


def _keywords(query: str, n: int) -> str:
    """The first `n` content words — recall() ANDs them, so `n` is the whole story."""
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in _STOPWORDS and len(w) > 2]
    return " ".join(words[:n])


def _score_arm(backend: GitMemoryBackend, probes: list[tuple[str, set[str]]]) -> dict[str, float]:
    recall_5, rr, prec_budget, chars, zeros, elapsed = [], [], [], [], 0, []
    for query, relevant in probes:
        t0 = time.perf_counter()
        hits = backend.recall(query)
        elapsed.append(time.perf_counter() - t0)
        ids = [h.id for h in hits]
        if not ids:
            zeros += 1
        recall_5.append(1.0 if any(i in relevant for i in ids[:5]) else 0.0)
        rank = next((n for n, i in enumerate(ids, 1) if i in relevant), None)
        rr.append(1.0 / rank if rank else 0.0)
        total = sum(len(h.content) for h in hits)
        earned = sum(len(h.content) for h in hits if h.id in relevant)
        chars.append(total)
        prec_budget.append(earned / total if total else 0.0)
    return {
        "recall_at_5": statistics.mean(recall_5),
        "mrr": statistics.mean(rr),
        "precision_at_budget": statistics.mean(prec_budget),
        "chars_returned": statistics.mean(chars),
        "zero_result_rate": zeros / len(probes),
        "ms_per_query": statistics.mean(elapsed) * 1000,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="10,1000,10000,50000")
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--terms", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    _verify_seed_matches_remember()
    corpus, queries, qrels = _load(_fetch())
    rng = random.Random(args.seed)  # noqa: S311 — reproducible query sampling, not crypto

    judged = sorted(q for q, rel in qrels.items() if rel & corpus.keys())
    sampled = rng.sample(judged, min(args.queries, len(judged)))
    must_keep = {d for q in sampled for d in qrels[q] if d in corpus}
    filler = [d for d in sorted(corpus) if d not in must_keep]
    rng.shuffle(filler)

    print(
        f"\nFiQA-2018 · {len(corpus):,} docs · {len(sampled)} judged queries · "
        f"{len(must_keep):,} relevant docs always present"
    )

    sizes = [int(s) for s in args.sizes.split(",")]
    report: dict[str, Any] = {"dataset": "beir/fiqa-2018", "queries": len(sampled), "runs": []}

    # Every corpus is built once and reused by both arms and every probe set.
    homes: dict[int, tuple[Path, int]] = {}
    for size in sizes:
        ids = list(must_keep) + filler[: max(0, size - len(must_keep))]
        home = Path(tempfile.mkdtemp()) / "home"
        home.mkdir(parents=True)
        _seed(home, {i: corpus[i] for i in ids})
        homes[size] = (home, len(ids))

    # ── the AND gate, as a function of how many words the agent asks with ──
    biggest, n_big = homes[max(sizes)]
    print(f"\nAND-gate collapse · {n_big:,}-document corpus · every term must appear\n")
    print(f"{'query terms':>12} {'returns nothing':>16} {'R@5':>7}")
    print("-" * 38)
    for n in (1, 2, 3, 4, 6):
        probes = [(_keywords(queries[q], n), qrels[q]) for q in sampled]
        m = _score_arm(GitMemoryBackend(biggest), probes)
        print(f"{n:>12} {m['zero_result_rate'] * 100:>15.0f}% {m['recall_at_5']:>7.3f}")
        report["runs"].append({"size": n_big, "arm": f"and-gate-{n}-terms", **m})
    raw = _score_arm(GitMemoryBackend(biggest), [(queries[q], qrels[q]) for q in sampled])
    print(
        f"{'full question':>12} {raw['zero_result_rate'] * 100:>15.0f}% {raw['recall_at_5']:>7.3f}"
    )
    report["runs"].append({"size": n_big, "arm": "and-gate-full-question", **raw})

    # ── ranking: baseline vs BM25, where retrieval actually happens ──
    print(f"\nRanking arms · {args.terms} query terms\n")
    header = (
        f"{'size':>7} {'arm':>9} {'R@5':>6} {'MRR':>6} {'prec@bud':>9} "
        f"{'chars':>7} {'zero%':>6} {'ms/q':>8}"
    )
    print(header)
    print("-" * len(header))
    for size in sizes:
        home, n_docs = homes[size]
        probes = [(_keywords(queries[q], args.terms), qrels[q]) for q in sampled]
        for arm, backend in (
            ("baseline", GitMemoryBackend(home)),
            ("bm25", Bm25GitMemoryBackend(home)),
        ):
            m = _score_arm(backend, probes)
            print(
                f"{n_docs:>7,} {arm:>9} {m['recall_at_5']:>6.3f} {m['mrr']:>6.3f} "
                f"{m['precision_at_budget']:>9.3f} {m['chars_returned']:>7.0f} "
                f"{m['zero_result_rate'] * 100:>5.0f}% {m['ms_per_query']:>7.0f}ms"
            )
            report["runs"].append({"size": n_docs, "arm": arm, "terms": args.terms, **m})
        print()

    out = Path(__file__).resolve().parent / "results" / "scale_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rel = out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out
    print(f"Report written to {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
