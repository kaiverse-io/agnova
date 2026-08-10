"""Tests for the Qortia HTTP memory backend.

All HTTP calls are mocked at `http.client.HTTP(S)Connection` — same technique
`test_checkpoint_deep.py` uses for `checkpoint.upload_bundle` — so nothing here
touches a real socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agnova.memory.qortia_backend import (
    QortiaError,
    QortiaMemoryBackend,
    QortiaNotFoundError,
)


@dataclass
class _Captured:
    """What the fake connection saw. Typed so tests unpack without a cast/ignore."""

    connect: tuple[str, int | None, float | None] | None = None
    request: tuple[str, str, bytes | None, dict[str, str]] | None = None
    closed: bool = False

    def sent_json(self) -> object:
        assert self.request is not None
        body = self.request[2]
        assert body is not None
        return json.loads(body)


def _fake_conn(status: int, body: bytes, captured: _Captured) -> type:
    """A fake http.client connection that records the request and replays a canned response."""

    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return body

    class Conn:
        def __init__(
            self, host: str, port: int | None = None, timeout: float | None = None
        ) -> None:
            captured.connect = (host, port, timeout)

        def request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            captured.request = (method, path, body, headers or {})

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            captured.closed = True

    return Conn


def _backend(url: str = "https://qortia.example") -> QortiaMemoryBackend:
    return QortiaMemoryBackend(url, "key-123", "agent-abc")


def _install(monkeypatch: pytest.MonkeyPatch, status: int, payload: object) -> _Captured:
    captured = _Captured()
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    conn = _fake_conn(status, body, captured)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPConnection", conn)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", conn)
    return captured


# ── construction ─────────────────────────────────────────────────────────────


def test_init_rejects_invalid_urls() -> None:
    with pytest.raises(ValueError, match="QORTIA_URL"):
        QortiaMemoryBackend("not-a-url", "key", "agent")
    with pytest.raises(ValueError, match="QORTIA_URL"):
        QortiaMemoryBackend("ftp://qortia.example", "key", "agent")
    with pytest.raises(ValueError, match="QORTIA_URL"):
        QortiaMemoryBackend("http://", "key", "agent")


# ── context ──────────────────────────────────────────────────────────────────


def test_context_renders_and_sends_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(
        monkeypatch,
        200,
        {
            "org_chart": [{"title": "Org", "content": "flat", "importance": None}],
            "processes": [],
            "handoffs": [],
            "weekly_summary": None,
            "memories": {
                "mental_models": [],
                "decisions": [{"title": None, "content": "shipped v1", "importance": None}],
                "lessons": [],
            },
        },
    )
    backend = _backend()

    text = backend.context()

    assert "## Org\nflat" in text
    assert "shipped v1" in text
    assert captured.request is not None
    method, path, body, headers = captured.request
    assert (method, path) == ("GET", "/v1/context")
    assert body is None
    assert headers["Authorization"] == "Bearer key-123"
    assert headers["X-Agent-Id"] == "agent-abc"
    assert "Content-Type" not in headers


def test_context_truncates_to_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single entry that can't fit under budget is dropped whole, not
    sliced — text[:budget] used to return "a"*10, a mid-content fragment
    with no indication anything was cut. budget=10 is also too small to fit
    the drop-marker itself, so the correct result is "", not a fragment."""
    _install(
        monkeypatch,
        200,
        {
            "org_chart": [],
            "processes": [],
            "handoffs": [],
            "weekly_summary": {"title": None, "content": "a" * 500, "importance": None},
            "memories": {"mental_models": [], "decisions": [], "lessons": []},
        },
    )
    backend = _backend()

    assert backend.context(budget=10) == ""


def test_context_drops_whole_entries_and_marks_what_was_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        200,
        {
            "org_chart": [],
            "processes": [],
            "handoffs": [],
            "weekly_summary": None,
            "memories": {
                "mental_models": [],
                "decisions": [],
                "lessons": [
                    {"content": "short important lesson", "importance": 0.95},
                    {"content": "b" * 500, "importance": 0.9},
                ],
            },
        },
    )
    backend = _backend()

    out = backend.context(budget=200)

    assert "short important lesson" in out
    assert "b" * 500 not in out
    assert "1 lower-importance entry omitted" in out


def test_context_empty_response_is_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        200,
        {
            "org_chart": [],
            "processes": [],
            "handoffs": [],
            "weekly_summary": None,
            "memories": {"mental_models": [], "decisions": [], "lessons": []},
        },
    )
    assert _backend().context() == ""


# ── recall ───────────────────────────────────────────────────────────────────


def test_recall_empty_query_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not make an HTTP call for an empty query")

    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", explode)
    assert _backend().recall("   ") == []


def test_recall_sends_known_filters_and_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(
        monkeypatch,
        200,
        {
            "results": [
                {
                    "id": "mem-1",
                    "type": "lesson",
                    "scope": "private",
                    "content": "dharma is durable",
                    "importance": 0.8,
                    "created_at": "2026-08-01T00:00:00Z",
                    "entity_summary": None,
                    "linked_via": None,
                    "valid_from": None,
                    "valid_until": None,
                }
            ]
        },
    )
    backend = _backend()

    hits = backend.recall(
        "dharma", filters={"scope": "private", "rerank": True, "bogus": "dropped"}
    )

    assert len(hits) == 1
    assert hits[0].id == "mem-1"
    assert hits[0].type == "lesson"
    assert hits[0].metadata["scope"] == "private"
    assert hits[0].metadata["importance"] == 0.8
    assert "bogus" not in hits[0].metadata

    assert captured.request is not None
    method, path, _body, _headers = captured.request
    assert (method, path) == ("POST", "/v1/recall")
    assert captured.sent_json() == {"query": "dharma", "scope": "private", "rerank": True}


def test_recall_missing_results_key_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 200, {})
    assert _backend().recall("dharma") == []


# ── remember ─────────────────────────────────────────────────────────────────


def test_remember_empty_items_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not make an HTTP call for an empty batch")

    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", explode)
    assert _backend().remember([]) == []


def test_remember_whitelists_fields_and_zips_server_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, 200, {"ids": ["server-id-1"]})
    backend = _backend()

    stored = backend.remember(
        [
            {
                "id": "client-supplied-id-should-be-dropped",
                "type": "lesson",
                "content": "learned about dharma",
                "metadata": {"k": "v"},
                "unexpected_field": "should also be dropped",
            }
        ]
    )

    assert len(stored) == 1
    assert stored[0].id == "server-id-1"  # server id wins, not the client-supplied one
    assert stored[0].content == "learned about dharma"
    assert stored[0].type == "lesson"
    assert stored[0].metadata == {"k": "v"}

    assert captured.sent_json() == {
        "memories": [{"type": "lesson", "content": "learned about dharma", "metadata": {"k": "v"}}]
    }


def test_remember_id_count_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 200, {"ids": []})
    with pytest.raises(ValueError):
        _backend().remember([{"type": "lesson", "content": "learned about dharma"}])


# ── forget ───────────────────────────────────────────────────────────────────


def test_forget_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, 200, {"id": "mem-1"})
    assert _backend().forget("mem-1") is True
    assert captured.sent_json() == {"id": "mem-1"}


def test_forget_false_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 404, {"detail": "Memory not found"})
    assert _backend().forget("missing") is False


def test_2xx_empty_body_is_treated_as_empty_object(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _Captured()
    conn = _fake_conn(200, b"", captured)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", conn)
    # forget() only cares whether _request raises; a 2xx with no body is still success.
    assert _backend().forget("mem-1") is True


# ── work order id ────────────────────────────────────────────────────────────


def test_recall_and_remember_send_the_same_work_order_id_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One backend instance = one work order (see the constructor docstring)
    — recall() and remember() must agree on the id, or Qortia's confidence
    decay (ADR-125) would attribute a session's reads to the wrong order."""
    captured = _install(monkeypatch, 200, {"results": []})
    backend = _backend()

    backend.recall("dharma")
    recall_headers = captured.request[3] if captured.request else {}

    captured2 = _install(monkeypatch, 200, {"ids": ["mem-1"]})
    backend.remember([{"type": "episodic", "content": "learned about dharma today"}])
    remember_headers = captured2.request[3] if captured2.request else {}

    assert recall_headers["X-Work-Order-Id"] == remember_headers["X-Work-Order-Id"]
    assert recall_headers["X-Work-Order-Id"]  # non-empty


def test_work_order_id_is_overridable_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, 200, {"results": []})
    backend = QortiaMemoryBackend(
        "https://qortia.example", "key-123", "agent-abc", work_order_id="fixed-wo-1"
    )

    backend.recall("dharma")

    assert captured.request is not None
    assert captured.request[3]["X-Work-Order-Id"] == "fixed-wo-1"


# ── outcome ──────────────────────────────────────────────────────────────────


def test_outcome_posts_the_work_order_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, 200, {"work_order_id": "fixed-wo-1", "outcome": "SUCCESS"})
    backend = QortiaMemoryBackend(
        "https://qortia.example", "key-123", "agent-abc", work_order_id="fixed-wo-1"
    )

    result = backend.outcome("SUCCESS")

    assert result is True
    assert captured.request is not None
    method, path, _body, _headers = captured.request
    assert (method, path) == ("POST", "/v1/outcome")
    assert captured.sent_json() == {"work_order_id": "fixed-wo-1", "outcome": "SUCCESS"}


def test_outcome_propagates_a_rejected_value_as_qortia_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`outcome()` doesn't validate client-side (see agnova.memory.OUTCOME_VALUES) —
    Qortia is authoritative and 422s anything else."""
    _install(monkeypatch, 422, {"detail": "invalid outcome"})
    with pytest.raises(QortiaError, match="422"):
        _backend().outcome("MAYBE")


# ── error paths shared by all four operations ────────────────────────────────


def test_non_2xx_raises_with_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 500, {"detail": "internal error"})
    with pytest.raises(QortiaError, match="HTTP 500.*internal error"):
        _backend().recall("dharma")


def test_non_2xx_empty_body_reports_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _Captured()
    conn = _fake_conn(500, b"", captured)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", conn)
    with pytest.raises(QortiaError, match=r"\(empty body\)"):
        _backend().recall("dharma")


def test_non_2xx_json_without_detail_key_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, 500, {"error": "boom", "code": 7})
    with pytest.raises(QortiaError, match="boom"):
        _backend().recall("dharma")


def test_non_2xx_non_json_body_is_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _Captured()
    conn = _fake_conn(503, b"upstream unavailable", captured)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", conn)
    with pytest.raises(QortiaError, match="upstream unavailable"):
        _backend().recall("dharma")


def test_network_failure_raises_qortia_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os(*args: object, **kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", raise_os)
    with pytest.raises(QortiaError, match="connection refused"):
        _backend().recall("dharma")


def test_invalid_json_response_raises_qortia_error(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _Captured()
    conn = _fake_conn(200, b"not json", captured)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", conn)
    with pytest.raises(QortiaError, match="invalid JSON"):
        _backend().recall("dharma")


def test_404_raises_not_found_subclass_of_qortia_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, 404, {"detail": "not found"})
    with pytest.raises(QortiaNotFoundError):
        _backend().recall("dharma")
    # QortiaNotFoundError must still be catchable as the base error.
    _install(monkeypatch, 404, {"detail": "not found"})
    with pytest.raises(QortiaError):
        _backend().recall("dharma")


# ── wire details: scheme selection and base-path prefixing ───────────────────


def test_http_scheme_uses_http_connection_not_https(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _Captured()
    conn = _fake_conn(200, json.dumps({"results": []}).encode(), captured)

    def explode_https(*args: object, **kwargs: object) -> None:
        raise AssertionError("plain http:// must not open an HTTPSConnection")

    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPConnection", conn)
    monkeypatch.setattr("agnova.memory.qortia_backend.http.client.HTTPSConnection", explode_https)

    backend = _backend("http://qortia.internal:9000")
    backend.recall("dharma")

    assert captured.connect == ("qortia.internal", 9000, 30.0)


def test_base_url_path_prefix_is_joined_with_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, 200, {"results": []})
    backend = _backend("https://gateway.example/qortia/")

    backend.recall("dharma")

    assert captured.request is not None
    assert captured.request[1] == "/qortia/v1/recall"
