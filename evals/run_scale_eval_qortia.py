"""Qortia semantic recall, scored on the identical corpus/query slice as
run_scale_eval.py's lexical arms — so "does semantic search help" has a real
number next to "does BM25 help," not just each measured in isolation.

Requires a running Qortia stack (docs/README.md in the qortia repo):

    cd <qortia repo> && docker compose up -d --build
    docker compose exec ollama ollama pull bge-m3

Then, from this repo:

    QORTIA_URL=http://host.docker.internal:8081 \\
    QORTIA_ADMIN_TOKEN=<value from the qortia repo's .env> \\
    uv run python evals/run_scale_eval_qortia.py [--sizes 276,1000,10000] [--queries 100]

Reuses run_scale_eval's `_fetch`/`_load` (same FiQA cache, same seed, same
`_keywords` cutoff for a like-for-like probe) so the query set and the
must-keep relevant-document set are byte-identical to the lexical run — same
100 sampled queries, same 276 always-present relevant docs, same filler
draw. `--terms 0` sends the full natural-language question instead of the
first N keywords — meaningful here in a way it wasn't for the git backend,
since Qortia has no AND gate to collapse on stopwords.

Each corpus size gets its own freshly provisioned agent (recall is scoped
`WHERE agent_id = $2` in qortia/recall.py) so runs never share documents.
Seeding goes through the real `/v1/remember` — real embeddings, no shortcut —
batched (500/request; no server-side cap found) since that's the part with a
real cost (~70 docs/sec against local CPU-only Ollama).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_scale_eval import _fetch, _keywords, _load  # noqa: E402

_BATCH = 500

# qortia/src/qortia/reflect.py: run_embedding_worker() sleeps 10s between
# batches of EMBEDDING_BATCH_SIZE=50. remember() returns as soon as the row is
# inserted with embedding=NULL — recall's ANN search cannot see it until the
# worker catches up. An early version of this script queried immediately
# after seeding and silently scored 0 on every arm; this polls the row that
# fact until the backlog for this agent is actually drained.
_DB_CONTAINER = os.environ.get("QORTIA_DB_CONTAINER", "qortia-db-1")


def _pending_embeddings(agent_id: str) -> int:
    # agent_id is this process's own just-created UUID (agnova.provisioning
    # response), not attacker input; docker resolved from PATH by design,
    # matching agnova.engram's own subprocess convention.
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
            "-c",
            f"SELECT count(*) FROM hindsight_memories "  # noqa: S608
            f"WHERE agent_id = '{agent_id}' AND embedding IS NULL AND embedding_attempts < 3;",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _wait_for_embeddings(agent_id: str, *, expected: int, timeout: float = 900.0) -> float:
    t0 = time.perf_counter()
    while True:
        pending = _pending_embeddings(agent_id)
        elapsed = time.perf_counter() - t0
        if pending == 0:
            return elapsed
        if elapsed > timeout:
            raise TimeoutError(
                f"{pending}/{expected} embeddings still pending after {timeout:.0f}s"
            )
        time.sleep(3)


class _Admin:
    def __init__(self, base_url: str, admin_token: str) -> None:
        p = urlparse(base_url)
        assert p.hostname
        self.host, self.port, self.scheme = p.hostname, p.port, p.scheme
        self.token = admin_token

    def _post(self, path: str, body: dict[str, Any], auth: str) -> dict[str, Any]:
        conn = (
            http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        )(self.host, self.port, timeout=60)
        conn.request(
            "POST",
            path,
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": auth},
        )
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} -> {resp.status}: {data.decode()[:300]}")
        return json.loads(data)  # type: ignore[no-any-return]

    def provision_agent(self, tenant_id: str) -> tuple[str, str]:
        """Returns (agent_id, api_key) for a fresh agent under `tenant_id`."""
        agent = self._post("/v1/admin/agents", {"tenant_id": tenant_id}, f"Bearer {self.token}")
        key = self._post("/v1/admin/keys", {"tenant_id": tenant_id}, f"Bearer {self.token}")
        return agent["agent_id"], key["api_key"]

    def provision_tenant(self, name: str) -> str:
        t = self._post("/v1/admin/tenants", {"name": name}, f"Bearer {self.token}")
        return t["tenant_id"]  # type: ignore[no-any-return]


class _Client:
    """Minimal /v1/remember + /v1/recall client — mirrors QortiaMemoryBackend's
    wire format (agnova/src/agnova/memory/qortia_backend.py) without importing
    across the repo boundary AGENTS.md forbids."""

    def __init__(self, base_url: str, api_key: str, agent_id: str) -> None:
        p = urlparse(base_url)
        assert p.hostname
        self.host, self.port, self.scheme = p.hostname, p.port, p.scheme
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Agent-Id": agent_id,
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        conn = (
            http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        )(self.host, self.port, timeout=120)
        conn.request("POST", path, body=json.dumps(body).encode(), headers=self.headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"POST {path} -> {resp.status}: {data.decode()[:500]}")
        return json.loads(data)  # type: ignore[no-any-return]

    def remember_batch(self, items: list[tuple[str, str]]) -> dict[str, str]:
        """items = [(fiqa_corpus_id, content), ...]. Returns {qortia_id: fiqa_corpus_id}."""
        memories = [{"type": "episodic", "content": text[:8000]} for _, text in items]
        resp = self._post("/v1/remember", {"memories": memories})
        return dict(zip(resp["ids"], (cid for cid, _ in items), strict=True))

    def recall(self, query: str) -> list[dict[str, Any]]:
        resp = self._post("/v1/recall", {"query": query, "scope": "all"})
        return resp["results"]  # type: ignore[no-any-return]


def _seed(client: _Client, docs: dict[str, str]) -> dict[str, str]:
    """Batched remember(); returns {qortia_id: fiqa_corpus_id} for scoring.

    qortia.models.MemoryItem.content_not_empty rejects anything under 5 words,
    422ing the *whole* batch it's in (qortia's own test_remember_batch_atomicity
    confirms this is deliberate, not a bug there) — real FiQA content hits this:
    short forum replies like "Yes, that's correct." (3 words) are ordinary,
    plausible content an agent's episodic memory would legitimately try to
    store. 73/57,638 FiQA docs (0.13%) are under the floor; filtered here since
    there is no honest way to satisfy the validator short of fabricating words
    that were never in the source text, which would corrupt what's actually
    being retrieved. Reported as a finding in its own right, not just an eval
    workaround — a real caller hits the identical 422 on the identical content.
    """
    fitted = {i: t for i, t in docs.items() if len(t.split()) >= 5}
    skipped = len(docs) - len(fitted)
    if skipped:
        print(f"  skipping {skipped} doc(s) under qortia's 5-word content floor", file=sys.stderr)
    id_map: dict[str, str] = {}
    items = list(fitted.items())
    for i in range(0, len(items), _BATCH):
        id_map.update(client.remember_batch(items[i : i + _BATCH]))
    return id_map


def _score(
    client: _Client, probes: list[tuple[str, set[str]]], id_map: dict[str, str]
) -> dict[str, float]:
    recall_5, rr, prec_budget, chars, zeros, elapsed = [], [], [], [], 0, []
    for query, relevant in probes:
        t0 = time.perf_counter()
        results = client.recall(query)
        elapsed.append(time.perf_counter() - t0)
        # Map each Qortia-minted id back to the FiQA doc it embeds, so the same
        # human relevance judgements (qrels) score this arm.
        fiqa_ids = [id_map.get(r["id"], r["id"]) for r in results]
        if not fiqa_ids:
            zeros += 1
        recall_5.append(1.0 if any(i in relevant for i in fiqa_ids[:5]) else 0.0)
        rank = next((n for n, i in enumerate(fiqa_ids, 1) if i in relevant), None)
        rr.append(1.0 / rank if rank else 0.0)
        total = sum(len(r["content"]) for r in results)
        earned = sum(
            len(r["content"]) for r, fid in zip(results, fiqa_ids, strict=True) if fid in relevant
        )
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
    ap.add_argument("--sizes", default="276,1000,10000")
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--terms", type=int, default=0, help="0 = full natural-language query")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--base-url", default=os.environ.get("QORTIA_URL", "http://host.docker.internal:8081")
    )
    ap.add_argument("--admin-token", default=os.environ.get("QORTIA_ADMIN_TOKEN", ""))
    args = ap.parse_args()
    if not args.admin_token:
        print("QORTIA_ADMIN_TOKEN is required (env or --admin-token)", file=sys.stderr)
        return 2

    import random

    corpus, queries, qrels = _load(_fetch())
    rng = random.Random(args.seed)  # noqa: S311 — reproducible query sampling, not crypto
    judged = sorted(q for q, rel in qrels.items() if rel & corpus.keys())
    sampled = rng.sample(judged, min(args.queries, len(judged)))
    must_keep = {d for q in sampled for d in qrels[q] if d in corpus}
    filler = [d for d in sorted(corpus) if d not in must_keep]
    rng.shuffle(filler)

    admin = _Admin(args.base_url, args.admin_token)
    tenant_id = admin.provision_tenant("scale-eval")
    print(f"tenant {tenant_id}")

    print(
        f"\nFiQA-2018 · qortia semantic recall · {len(sampled)} judged queries · "
        f"{len(must_keep):,} relevant docs always present\n"
    )
    header = (
        f"{'size':>7} {'query':>10} {'R@5':>6} {'MRR':>6} {'prec@bud':>9} "
        f"{'chars':>7} {'zero%':>6} {'ms/q':>8}"
    )
    print(header)
    print("-" * len(header))

    report: dict[str, Any] = {"dataset": "beir/fiqa-2018", "backend": "qortia", "runs": []}
    for size in [int(s) for s in args.sizes.split(",")]:
        ids = list(must_keep) + filler[: max(0, size - len(must_keep))]
        docs = {i: corpus[i] for i in ids}

        agent_id, api_key = admin.provision_agent(tenant_id)
        client = _Client(args.base_url, api_key, agent_id)

        t0 = time.perf_counter()
        id_map = _seed(client, docs)
        write_s = time.perf_counter() - t0
        print(
            f"  wrote {len(docs):,} docs in {write_s:.0f}s ({len(docs) / write_s:.0f}/s)",
            file=sys.stderr,
        )

        embed_s = _wait_for_embeddings(agent_id, expected=len(docs))
        print(f"  embeddings drained after {embed_s:.0f}s", file=sys.stderr)
        seed_s = write_s + embed_s

        query_style = "keywords" if args.terms else "full-question"
        # _keywords resolves to Any: sys.path.insert import above is opaque to mypy.
        probe_text = (
            (lambda q: _keywords(queries[q], args.terms))  # type: ignore[no-untyped-call]
            if args.terms
            else (lambda q: queries[q])
        )
        probes = [(probe_text(q), qrels[q]) for q in sampled]

        m = _score(client, probes, id_map)
        print(
            f"{len(docs):>7,} {query_style:>10} {m['recall_at_5']:>6.3f} {m['mrr']:>6.3f} "
            f"{m['precision_at_budget']:>9.3f} {m['chars_returned']:>7.0f} "
            f"{m['zero_result_rate'] * 100:>5.0f}% {m['ms_per_query']:>7.0f}ms"
        )
        report["runs"].append(
            {"size": len(docs), "query_style": query_style, "seed_seconds": seed_s, **m}
        )

    out = Path(__file__).resolve().parent / "results" / "scale_qortia_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
