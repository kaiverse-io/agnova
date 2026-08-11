---
name: bm25-lost-to-a-duplicate-document-fix
description: On git memory recall, BM25 measurably regressed the eval while a 35-line duplicate-suppression fix took MRR 0.481 -> 0.833.
metadata:
  type: project
---

Measured with `evals/run_recall_eval.py` on `git_recall_v1` (2026-08-11):

| Change | Case pass | MRR |
| --- | --- | --- |
| baseline | 1.000 | 0.481 |
| suppress the daily log's duplicate of each entry | 1.000 | **0.833** |
| …then add BM25 on top | 0.900 | 0.778 |

The MRR ceiling was never the ranking function. `remember()` writes each memory
twice — `entries/<id>.md` plus a `- [<id>] …` line in that day's log — and
`recall()` scanned both. The log accumulates the whole day (wins on match count)
and is rewritten on every `remember()` (wins on mtime), so the aggregate
duplicate beat the entry it duplicates on *both* ranking keys. Diagnosis: in 7 of
the 8 imperfect cases the rank-1 result was `file:<today>.md`.

**BM25 made it worse and was reverted.** Its length normalisation broke `geh-004`
(equal-relevance tie-break by recency): it manufactures a score difference out of
incidental length, the tie disappears, and the older hard negative wins. BM25 is
tuned for corpora with high length variance — agent memory entries are short,
atomic and uniform, so length normalisation invents signal from noise.

**How to apply:** before adding a retrieval technique here on reputation
(BM25, embeddings, graph indexes), run the eval first and diagnose *what actually
outranks ground truth* — a throwaway script printing the ranked ids per case
found this in one pass. Duplicate/near-duplicate documents in the corpus dominate
ranking-function quality at this scale. Relevant to the `agnova kb` /
`INDEX_BACKEND` discussion, where the proposals were all indexing strategies.
See [[graphify-prose-recovers-only-authored-structure]].
