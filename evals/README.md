# evals/

Real memory-quality harness for agnova's git-backed memory (not a chassis placeholder —
see [`kaiverse-io/qortia`](https://github.com/kaiverse-io/qortia)'s `evals/` for the
equivalent server-side harness that already existed for `QortiaMemoryBackend`).

## Why this exists

`GitMemoryBackend` (`src/agnova/memory/git_backend.py`) is the default memory backend —
every agent that hasn't been pointed at a Qortia instance uses it. Until this harness,
nothing had ever measured its recall quality: one narrow two-file test was the entire
coverage. `QortiaMemoryBackend` needs no equivalent harness — it does no local ranking at
all (`qortia_backend.py` is a thin HTTP client; all scoring happens server-side, already
covered by Qortia's own REH).

`GitMemoryBackend` has no semantic model by design (that's Qortia's job) — its ranking is
pure `(match_count, mtime)`. So this harness measures whether that ranking is *robust*,
not whether it "understands" paraphrase. A case failing because two texts share no
vocabulary at all is not a bug, it's the backend's documented ceiling. A case failing
because of a real matching/ranking defect is.

## Harness

| Script | Role |
|--------|------|
| `run_recall_eval.py` | Recall@5/@10, MRR, and explicit expectation checks against a real `GitMemoryBackend` on a real temp directory — no server, no mocks |

```bash
uv run python evals/run_recall_eval.py evals/datasets/git_recall_v1.json
```

Report: `evals/results/git_recall_latest.json`.

## What building this found (and fixed)

Two real bugs in `git_backend.py`, both caught by the first real run of this harness, both
fixed in the same change:

1. **`recall()` matched the entire query as one literal phrase.** A query like
   `"rate limiting AuthService"` only matched a file containing that exact substring
   verbatim — every ordinary multi-word query returned zero results unless it happened to
   quote the source exactly. Fixed: the query is now split into words and ANDed
   (word-boundary, case-insensitive) — matching Qortia's own `plainto_tsquery` precedent
   (`docs/03-eval-strategy.md` in the qortia repo: exact-token, no stemming, no fuzzy
   fallback — the fix keeps that same determinism principle, just per-word instead of
   whole-phrase).
2. **Raw substring matching had no word boundary.** A query for `"cat"` matched inside
   `"category"`, letting an unrelated file inflate its match count — and outrank the real
   answer — purely by containing the query as a substring of a longer word. Fixed with
   `\bterm\b` regex matching.

A third, smaller bug: `_RECALL_MAX_TOTAL_CHARS` is documented as a hard cap but the loop
checked it *after* appending a snippet, so the combined total could exceed it by one
snippet's length (measured: 4022 chars against a 4000 cap). Fixed to check prospectively.

## What's a real limitation, not a bug

`geh-002` (in `datasets/git_recall_v1.json`) seeds a ground truth mentioning "Redis" once
against a hard negative repeating "Redis" five times. Match-count-primary ranking lets the
hard negative outrank the true answer — the harness deliberately does *not* assert
top-5 ranking on that case (see `ground_truth_in_top_5` in the dataset / `run_recall_eval.py`'s
`_check_ground_truth_ranking`), because "fix" here would mean adding frequency-weighted or
semantic scoring — i.e. becoming Qortia. That tradeoff belongs to whoever picks a backend,
not something this harness should paper over.

## Current results (first real run, 2026-08-09)

10/10 cases pass. `case_pass_rate=1.000`, `recall_at_5=0.889`, `recall_at_10=0.889`,
`mrr=0.481` — floors set at 5% below this baseline (`RECALL_AT_5_FLOOR=0.80`,
`MRR_FLOOR=0.40` in `run_recall_eval.py`), matching Qortia REH's own convention for setting
floors from a measured number rather than a guessed target.
