# Vendored eval datasets

Git-tracked, not gitignored — the point is that `run_scale_eval.py` and
`run_scale_eval_qortia.py` run offline against a real, externally-judged corpus with
no network access and no re-download on every run. `evals/.cache/` (gitignored) is only
where a fresh `--refetch` lands before being vendored here by hand; it is never what an
eval reads.

## fiqa/

BEIR / FiQA-2018 — 57,638 user-written financial-forum posts (StackExchange-style Q&A),
6,648 queries, 1,706 human relevance judgements on the `test` split.

- Source: https://github.com/beir-cellar/beir
- Fetched from: `https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/fiqa.zip`
- License: CC BY-SA 4.0 — https://creativecommons.org/licenses/by-sa/4.0/
- Vendored: 2026-08-11
- Files kept: `corpus.jsonl`, `queries.jsonl`, `qrels/test.tsv` (the `train`/`dev` qrels
  splits from the original zip are dropped — unused here)

Used for: `run_scale_eval.py` (git-backend recall, baseline vs BM25) and
`run_scale_eval_qortia.py` (Qortia semantic recall), scored against the *same* 100
sampled queries and relevance judgements so the two are comparable.

No `wikiann/` here: an earlier version of this directory vendored it for a
qortia-internal NER eval (`run_ner_eval_qortia.py`) that had nothing to do with
agnova's own `MemoryBackend` (the git backend has no NER at all) and reached
into qortia's Postgres via `docker exec` from across the repo boundary AGENTS.md
reserves for HTTP only. Moved to qortia's own `evals/` (`run_ner_eval.py`),
which now runs it over HTTP against qortia's own eval-mode routes with no
database reach-around needed.
