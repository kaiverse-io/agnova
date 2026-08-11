"""Qortia multilingual NER, scored against WikiANN gold spans.

Everything else in this eval directory tests recall — this tests a different
code path entirely: qortia.knowledge.extract_entities_with_types(), called
synchronously inside remember() (see qortia/src/qortia/remember.py), which
routes hi/bn/ta/te/mr to the multilingual xx_ent_wiki_sm spaCy pipeline and
everything else (including the "unsupported" control language here) through
en_core_web_sm with a logged ner_lang_unsupported/fallback:en warning.

That routing table has never been measured against real text in any of the
five Indic languages it claims to support — this is that measurement, using
WikiANN (evals/datasets/wikiann/, vendored — see evals/datasets/README.md for
source/license/fetch_wikiann.py) because its PER/ORG/LOC tags map directly onto
qortia's own _INDIC_LABEL_MAP (PER->PERSON, ORG->ORG, LOC->GPE).

Unlike embeddings (async, worker-drained — see run_scale_eval_qortia.py's
_wait_for_embeddings), entity extraction runs inline in the remember() request,
so results are read back immediately with no drain wait: this script POSTs each
WikiANN sentence through /v1/remember with an explicit `lang`, then reads the
`entities` column straight back via `docker exec <db> psql` (there is no public
endpoint that returns per-item entities — entity_graph aggregates across items,
it doesn't expose them per source row).

Two matching modes are reported, because they answer different questions:
    text match      did qortia find *an* entity overlapping the gold span at
                     all, regardless of label — "is NER finding real things"
    text+type match did it also land on the right qortia-side type — "is the
                     Indic label mapping correct," specifically testing
                     _INDIC_LABEL_MAP for the five Indic languages
Matching is substring-based both directions after casefolding, not exact
string equality — spaCy's exact tokenisation/boundary choices routinely
differ from WikiANN's pre-tokenised gold by a trailing particle or punctuation
mark, and exact-match would undercount correct extractions on that alone
rather than on the model being wrong.

zero_extraction_rate is the sharpest single number here: the fraction of
sentences with >=1 gold entity where qortia found none at all. That is a
silent failure an agent has no way to detect from the API response — remember()
returns 200 either way — and it is exactly the failure mode
evals/README.md's own log-mining principle exists to catch: a passing HTTP
status is not evidence the request did what it looks like it did.

Usage:
    uv run python evals/run_ner_eval_qortia.py [--n 100] [--langs hi,bn,en]
    QORTIA_URL=... QORTIA_ADMIN_TOKEN=... (env or --base-url/--admin-token)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DATASET_DIR = Path(__file__).resolve().parent / "datasets" / "wikiann"
_BATCH = 100
_DB_CONTAINER = os.environ.get("QORTIA_DB_CONTAINER", "qortia-db-1")

# qortia/src/qortia/knowledge.py's own map, mirrored here so gold WikiANN types
# score against what qortia is actually trying to produce, not WikiANN's raw
# PER/ORG/LOC. GPE is the deliberate LOC standin qortia itself uses.
_GOLD_TYPE_MAP = {"PER": "PERSON", "ORG": "ORG", "LOC": "GPE"}


class _Admin:
    def __init__(self, base_url: str, admin_token: str) -> None:
        p = urlparse(base_url)
        assert p.hostname
        self.host, self.port, self.scheme = p.hostname, p.port, p.scheme
        self.token = admin_token

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        conn = (
            http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        )(self.host, self.port, timeout=60)
        conn.request(
            "POST",
            path,
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} -> {resp.status}: {data.decode()[:300]}")
        return json.loads(data)  # type: ignore[no-any-return]

    def provision(self) -> tuple[str, str, str]:
        tenant = self._post("/v1/admin/tenants", {"name": "ner-eval"})["tenant_id"]
        agent = self._post("/v1/admin/agents", {"tenant_id": tenant})["agent_id"]
        key = self._post("/v1/admin/keys", {"tenant_id": tenant})["api_key"]
        return tenant, agent, key


def _remember_batch(
    base_url: str, api_key: str, agent_id: str, items: list[tuple[str, str]]
) -> list[str]:
    """items = [(text, lang), ...]. Returns qortia ids in request order."""
    p = urlparse(base_url)
    assert p.hostname
    conn = (http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection)(
        p.hostname, p.port, timeout=120
    )
    body = {
        "memories": [{"type": "episodic", "content": text, "lang": lang} for text, lang in items]
    }
    conn.request(
        "POST",
        "/v1/remember",
        body=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Agent-Id": agent_id,
        },
    )
    resp = conn.getresponse()
    data = resp.read()
    if resp.status >= 300:
        raise RuntimeError(f"POST /v1/remember -> {resp.status}: {data.decode()[:500]}")
    return json.loads(data)["ids"]  # type: ignore[no-any-return]


def _read_back(ids: list[str]) -> dict[str, tuple[list[tuple[str, str]], str]]:
    """{id: (entities, effective_lang)} via psql — see module docstring for why
    this is the only way to read what remember() actually extracted."""
    # ids are qortia's own just-generated UUIDs (this process's earlier
    # _remember_batch response), not attacker input; docker resolved from
    # PATH by design, matching agnova.engram's own subprocess convention.
    id_list = ",".join(f"'{i}'" for i in ids)
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            _DB_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "qortia",
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            f"SELECT id, entities, lang FROM hindsight_memories WHERE id IN ({id_list});",  # noqa: S608
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = {}
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        rid, entities_json, lang = line.split("\t")
        pairs = [(t[0], t[1]) for t in json.loads(entities_json)]
        result[rid] = (pairs, lang)
    return result


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def _matches(pred_text: str, gold_text: str) -> bool:
    p, g = _norm(pred_text), _norm(gold_text)
    return bool(p) and bool(g) and (p in g or g in p)


def _score_language(
    examples: list[dict[str, Any]], predicted: list[list[tuple[str, str]]]
) -> dict[str, Any]:
    text_tp = type_tp = n_pred = n_gold = zero_extraction = 0
    for ex, preds in zip(examples, predicted, strict=True):
        gold = ex["gold"]  # [{"type": "PER"/"ORG"/"LOC", "text": ...}, ...]
        n_gold += len(gold)
        n_pred += len(preds)
        if gold and not preds:
            zero_extraction += 1
        matched_gold_idx: set[int] = set()
        for p_text, p_type in preds:
            for gi, g in enumerate(gold):
                if gi in matched_gold_idx:
                    continue
                if _matches(p_text, g["text"]):
                    text_tp += 1
                    if _GOLD_TYPE_MAP.get(g["type"]) == p_type:
                        type_tp += 1
                    matched_gold_idx.add(gi)
                    break
    precision = text_tp / n_pred if n_pred else 0.0
    recall = text_tp / n_gold if n_gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    type_precision = type_tp / n_pred if n_pred else 0.0
    return {
        "n_examples": len(examples),
        "n_gold_entities": n_gold,
        "n_predicted_entities": n_pred,
        "text_precision": precision,
        "text_recall": recall,
        "text_f1": f1,
        "type_precision": type_precision,  # of text matches, fraction also correctly typed
        "zero_extraction_rate": zero_extraction / len(examples) if examples else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="examples per language")
    ap.add_argument("--langs", default="hi,bn,ta,te,mr,en,de")
    ap.add_argument(
        "--base-url", default=os.environ.get("QORTIA_URL", "http://host.docker.internal:8081")
    )
    ap.add_argument("--admin-token", default=os.environ.get("QORTIA_ADMIN_TOKEN", ""))
    args = ap.parse_args()
    if not args.admin_token:
        print("QORTIA_ADMIN_TOKEN is required (env or --admin-token)", file=sys.stderr)
        return 2

    admin = _Admin(args.base_url, args.admin_token)
    tenant_id, agent_id, api_key = admin.provision()
    print(f"tenant {tenant_id} agent {agent_id}\n")

    langs = args.langs.split(",")
    header = (
        f"{'lang':>5} {'n':>4} {'gold':>5} {'pred':>5} {'P':>6} {'R':>6} {'F1':>6} "
        f"{'type-P':>7} {'zero%':>6}  effective-lang(sample)"
    )
    print(header)
    print("-" * len(header))

    report: dict[str, Any] = {"dataset": "wikiann", "n_per_lang": args.n, "runs": []}
    for lang in langs:
        path = DATASET_DIR / f"{lang}.json"
        if not path.is_file():
            print(f"{lang:>5}  (not vendored — run evals/fetch_wikiann.py first)", file=sys.stderr)
            continue
        examples = json.loads(path.read_text(encoding="utf-8"))[: args.n]

        all_ids: list[str] = []
        entities_by_id: dict[str, list[tuple[str, str]]] = {}
        effective_langs: list[str] = []
        for i in range(0, len(examples), _BATCH):
            chunk = examples[i : i + _BATCH]
            ids = _remember_batch(
                args.base_url, api_key, agent_id, [(e["text"], lang) for e in chunk]
            )
            all_ids.extend(ids)
            rows = _read_back(ids)
            for rid in ids:
                ents, eff_lang = rows.get(rid, ([], "?"))
                entities_by_id[rid] = ents
                effective_langs.append(eff_lang)

        # Same order as `examples`: remember() appends ids in request order
        # (qortia/src/qortia/remember.py), and each chunk is requested in order.
        predicted = [entities_by_id[rid] for rid in all_ids]
        m = _score_language(examples, predicted)
        eff_sample = ",".join(sorted(set(effective_langs))[:4])
        print(
            f"{lang:>5} {m['n_examples']:>4} {m['n_gold_entities']:>5} "
            f"{m['n_predicted_entities']:>5} "
            f"{m['text_precision']:>6.3f} {m['text_recall']:>6.3f} {m['text_f1']:>6.3f} "
            f"{m['type_precision']:>7.3f} {m['zero_extraction_rate'] * 100:>5.0f}%  {eff_sample}"
        )
        report["runs"].append({"lang": lang, "effective_langs_seen": eff_sample, **m})

    out = Path(__file__).resolve().parent / "results" / "ner_qortia_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
