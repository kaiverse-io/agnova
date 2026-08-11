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

## wikiann/

WikiANN (PAN-X) — Wikipedia-derived named-entity tagging, `PER`/`ORG`/`LOC` spans in
IOB2, per-language `test` splits.

- Source: https://huggingface.co/datasets/unimelb-nlp/wikiann (fetched via the public
  `datasets-server.huggingface.co/rows` JSON API — no `datasets` library dependency)
- License: ODC-BY (per the HF dataset card)
- Vendored: 2026-08-11
- Languages kept: `hi`, `bn`, `ta`, `te`, `mr` (qortia's `_INDIC_MODEL` routing table,
  `qortia/src/qortia/knowledge.py`) + `en` (qortia's non-Indic default path) + one
  control language outside both (`de`), to exercise the `ner_lang_unsupported` fallback
  path rather than only the two designed-for routes.

Used for: the qortia multilingual-NER eval (entity extraction against `PER→PERSON`,
`ORG→ORG`, `LOC→GPE` — the same map `_INDIC_LABEL_MAP` in `knowledge.py` uses), not yet
run against the git backend (no NER exists there — see the memory note on why a
model-backed NER doesn't belong in agnova's process).
