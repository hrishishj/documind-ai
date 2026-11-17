"""Tracing, metrics and cost: what is recorded, that it reaches the API, and that it never leaks content."""
import json

import pytest

from app.llm.provider import GroqLLM, TokenGovernor
from app.observability.metrics import Metrics
from app.observability.trace import RunTrace, cost_usd, span, use_trace
from app.research.search import SearchError
from tests.fakes import FakeWeb
from tests.test_api import FakeLLM, GOOD, docx_plan, make_client, new_session, sse, upload


def test_span_is_a_noop_outside_a_trace():
    with span("x", "tool") as s:
        s.set(a=1)  # must not raise


def test_spans_nest_and_record_failures():
    t = RunTrace("s")
    with use_trace(t):
        with span("outer", "node"):
            with pytest.raises(ValueError):
                with span("inner", "tool", n=1):
                    raise ValueError("boom")
    outer = next(s for s in t.spans if s.name == "outer")
    inner = next(s for s in t.spans if s.name == "inner")
    assert inner.parent == outer.id and inner.status == "error" and "ValueError: boom" in inner.error
    assert outer.status == "ok" and t.totals()["errors"] == 1


def test_cost_uses_configured_prices():
    assert cost_usd(1_000_000, 0) == pytest.approx(0.15) and cost_usd(0, 1_000_000) == pytest.approx(0.60)


def _llm(retries=2):
    llm = GroqLLM.__new__(GroqLLM)
    llm.retries, llm.governor, llm._messages = retries, TokenGovernor(limit=10_000), GroqLLM._messages
    llm._kw = {"model": "m"}
    return llm


def test_llm_calls_record_tokens_and_every_retry(monkeypatch):
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda s: None)

    class RateLimitError(Exception):
        pass

    class Msg:
        usage_metadata = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}

    calls = []

    class R:
        def invoke(self, m):
            calls.append(1)
            if len(calls) == 1:
                raise RateLimitError("rate limit exceeded")
            return {"raw": Msg(), "parsed": object(), "parsing_error": None}

    t = RunTrace("s")
    with use_trace(t):
        _llm()._call(lambda: R(), "sys", "user", None, 100)
    llm_spans = t.llm_spans()
    assert [s.status for s in llm_spans] == ["error", "ok"] and [s.attrs["attempt"] for s in llm_spans] == [0, 1]
    assert "RateLimitError" in llm_spans[0].error
    tot = t.totals()
    assert (tot["llm_calls"], tot["llm_failures"], tot["llm_retries"]) == (2, 1, 1)
    assert (tot["input_tokens"], tot["output_tokens"]) == (1000, 200)
    assert tot["cost_usd"] == pytest.approx(1000 * 0.15e-6 + 200 * 0.60e-6)


def test_unusable_structured_output_is_a_failed_span(monkeypatch):
    class Msg:
        usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    class R:
        def invoke(self, m):
            return {"raw": Msg(), "parsed": None, "parsing_error": "bad"}

    t = RunTrace("s")
    with use_trace(t):
        _llm(0)._call(lambda: R(), "s", "u", None, 100)
    assert t.llm_spans()[0].status == "error"


# ---------------------------------------------------------------- through the real API + graph
def _chat(c, sid, msg):
    return sse(c.post(f"/sessions/{sid}/chat", json={"message": msg}))


def test_an_edit_turn_is_traced_end_to_end_and_readable_over_the_api(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(GOOD, summary="Retitled.")])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        ev = _chat(c, sid, "Change the title to FY2025 Annual Report")
        final = ev[-1][1]
        run_id = final["run_id"]
        assert ev[0][1]["run_id"] == run_id
        runs = c.get(f"/sessions/{sid}/runs").json()
        assert [r["run_id"] for r in runs] == [run_id] and runs[0]["status"] == "ok" and runs[0]["intent"] == "edit"
        tr = c.get(f"/sessions/{sid}/runs/{run_id}").json()
        names = [s["name"] for s in tr["spans"]]
        # context reached the LangGraph worker threads: every executed node has a span
        for n in ("route", "understand", "plan", "validate", "render", "respond"):
            assert n in names, names
        assert all(s["kind"] == "node" and s["status"] == "ok" and s["duration_ms"] >= 0 for s in tr["spans"])
        assert tr["duration_ms"] > 0
        # metadata only: neither the user's message nor document text is in the stored trace
        blob = json.dumps(tr)
        assert "FY2025" not in blob and "Quarterly Report" not in blob


def test_a_tool_call_is_a_child_of_its_node_and_a_failure_is_recorded(test_database, tmp_path):
    web = FakeWeb(error=SearchError("all engines failed"))
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Probably."]), web=web) as c:
        sid = new_session(c)
        final = _chat(c, sid, "Search the web: what is the energy density of solid-state batteries?")[-1][1]
        tr = c.get(f"/sessions/{sid}/runs/{final['run_id']}").json()
        node = next(s for s in tr["spans"] if s["name"] == "research")
        tool = next(s for s in tr["spans"] if s["name"] == "web.search")
        assert tool["parent"] == node["id"] and tool["kind"] == "tool"
        assert tool["status"] == "error" and "all engines failed" in tool["error"]
        assert tr["tool_calls"] == 1 and tr["errors"] >= 1


def test_a_model_outage_is_recorded_as_an_error_run(test_database, tmp_path, docx_bytes):
    from app.core.errors import ExternalServiceError
    llm = FakeLLM(plans=[ExternalServiceError("LLM call failed: down")] * 3)
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        final = _chat(c, sid, "Change the title to X")[-1][1]
        run = c.get(f"/sessions/{sid}/runs").json()[0]
        assert run["run_id"] == final["run_id"] and run["status"] == "error"
        tr = c.get(f"/sessions/{sid}/runs/{run['run_id']}").json()
        assert any(s["name"] == "plan" and s["status"] == "error" for s in tr["spans"])


def test_other_clients_cannot_read_a_trace(test_database, tmp_path):
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Hi"])) as c:
        sid = new_session(c)
        rid = _chat(c, sid, "hello there")[-1][1]["run_id"]
        assert c.get(f"/sessions/{sid}/runs/{rid}").status_code == 200
        c.cookies.clear()
        assert c.get(f"/sessions/{sid}/runs").status_code == 404
        assert c.get(f"/sessions/{sid}/runs/{rid}").status_code == 404


def test_metrics_endpoint_counts_runs(test_database, tmp_path):
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Hi", "Hi again"])) as c:
        sid = new_session(c)
        _chat(c, sid, "hello there")
        _chat(c, sid, "hello again")
        body = c.get("/metrics").text
        assert 'documind_runs_total{intent="chat",status="ok"} 2' in body
        assert "documind_run_seconds_count 2" in body


def test_metrics_render_buckets_are_cumulative():
    m = Metrics()
    tot = {"input_tokens": 10, "output_tokens": 5, "llm_calls": 1, "llm_failures": 0, "llm_retries": 0, "tool_calls": 0,
           "errors": 0, "cost_usd": 0.001}
    m.observe_run("edit", "ok", 3000, tot)
    out = m.render()
    assert 'documind_run_seconds_bucket{le="2"} 0' in out and 'documind_run_seconds_bucket{le="5"} 1' in out
    assert 'documind_run_seconds_bucket{le="160"} 1' in out and 'documind_tokens_total{direction="input"} 10' in out
