"""Reliability under failure: each test breaks one dependency on purpose and asserts what the USER sees and what the TRACE says.

Covered faults: hung/failed web search, hung/failed vector store, model outage, model returning unusable output
every time, a bad plan that the validator rejects once. In every case the turn must finish promptly with an honest
message (never a 500, never a hang, never a claim that work happened when it did not).
"""
import threading
import time

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.core.errors import ExternalServiceError
from app.core.timeouts import CallTimeout, run_with_timeout
from app.document_engine.samples import build_docx
from app.llm.provider import StructuredOutputError
from app.observability.trace import RunTrace, span, use_trace
from app.services.storage import LocalStorage
from app.workflows.graph import Deps, build_graph
from tests.test_api import FakeLLM, GOOD, docx_plan, make_client, new_session, sse, upload
from tests.test_workflow_graph import BAD, FakeLLM as GraphLLM, upload as graph_upload


# ---------------------------------------------------------------- the deadline helper itself
def test_deadline_returns_the_value_and_propagates_errors():
    assert run_with_timeout(lambda: 41 + 1, 2, "x") == 42
    with pytest.raises(KeyError):
        run_with_timeout(lambda: {}["missing"], 2, "x")


def test_deadline_stops_waiting_and_does_not_block_exit():
    gate = threading.Event()
    t0 = time.perf_counter()
    with pytest.raises(CallTimeout, match="did not answer within 0.2s"):
        run_with_timeout(lambda: gate.wait(30), 0.2, "slow thing")
    assert time.perf_counter() - t0 < 2
    gate.set()  # release the abandoned daemon thread


def test_deadline_carries_the_trace_into_the_worker_thread():
    t2 = RunTrace("s")

    def work():
        with span("inner", "tool"):
            pass
    with use_trace(t2):
        run_with_timeout(work, 2, "x")
    assert [s.name for s in t2.spans] == ["inner"]  # a span finished in the worker thread lands in the caller's trace


# ---------------------------------------------------------------- web search faults, through the real API
class SlowWeb:
    def __init__(self):
        self.gate = threading.Event()

    def search(self, query, max_results=5):
        self.gate.wait(30)
        return []


Q = "Search the web: what is the energy density of solid-state batteries?"


def test_a_hung_web_search_cannot_hang_the_turn(test_database, tmp_path, monkeypatch):
    class S:
        tool_timeout_s = 0.3
    monkeypatch.setattr("app.workflows.graph.get_settings", lambda: S)
    web = SlowWeb()
    try:
        with make_client(test_database, tmp_path, llm=FakeLLM(texts=["Probably about 400 Wh/kg."]), web=web) as c:
            sid = new_session(c)
            t0 = time.perf_counter()
            ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": Q}))
            assert time.perf_counter() - t0 < 5
            final = ev[-1][1]
            assert ev[-1][0] == "final" and "not based on a web search" in final["content"] and final["citations"] == []
            tr = c.get(f"/sessions/{sid}/runs/{final['run_id']}").json()
            tool = next(s for s in tr["spans"] if s["name"] == "web.search")
            assert tool["status"] == "error" and "did not answer within" in tool["error"]
    finally:
        web.gate.set()


def test_search_engines_failing_one_after_another_are_reported_not_hidden(test_database, tmp_path):
    from app.research.search import FallbackSearch, SearchError

    class Dead:
        def search(self, q, n=5):
            raise SearchError("quota exceeded")
    with make_client(test_database, tmp_path, llm=FakeLLM(texts=["From memory."]), web=FallbackSearch([Dead(), Dead()])) as c:
        sid = new_session(c)
        final = sse(c.post(f"/sessions/{sid}/chat", json={"message": Q}))[-1][1]
        assert "not based on a web search" in final["content"] and "quota exceeded" in final["content"]


# ---------------------------------------------------------------- vector store faults (graph level)
class HangingIndex:
    class store:  # noqa: N801 - mimics Indexer.store for the span attribute
        pass

    def __init__(self):
        self.gate = threading.Event()

    def search(self, ns, q, k=6):
        self.gate.wait(30)
        return []


class BrokenIndex(HangingIndex):
    def search(self, ns, q, k=6):
        raise ConnectionError("pinecone unreachable")


@pytest.mark.parametrize("index_cls", [HangingIndex, BrokenIndex])
def test_vector_store_trouble_degrades_to_plain_chat(tmp_path, monkeypatch, index_cls):
    class S:
        tool_timeout_s = 0.3
    monkeypatch.setattr("app.workflows.graph.get_settings", lambda: S)
    env = LocalStorage(tmp_path / "store")
    f = graph_upload(env, build_docx, "docx", "report.docx")
    f["index_status"] = "ready"
    idx = index_cls()
    llm = GraphLLM(texts=["It is a quarterly report."])
    t = RunTrace("s")
    try:
        t0 = time.perf_counter()
        with use_trace(t):
            g = build_graph(Deps(llm, env, retriever=idx), checkpointer=MemorySaver())
            out = g.invoke({"session_id": "s1", "user_message": "What is this document about?", "history": [], "files": [f]},
                           {"configurable": {"thread_id": "t"}})
        assert time.perf_counter() - t0 < 5
        assert out["response"] == "It is a quarterly report." and out["citations"] == []
        tool = next(s for s in t.spans if s.name == "retrieval.search")
        assert tool.status == "error"
    finally:
        idx.gate.set()


# ---------------------------------------------------------------- model faults, through the real API
def test_a_model_outage_gives_an_honest_message_and_an_error_run(test_database, tmp_path):
    llm = FakeLLM(texts=[ExternalServiceError("LLM call failed: RateLimitError")])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": "hello there"}))
        assert ev[-1][0] == "final" and "couldn't reach the language model" in ev[-1][1]["content"]
        assert c.get(f"/sessions/{sid}/runs").json()[0]["status"] == "error"


def test_a_model_that_never_returns_a_valid_plan_ends_after_the_attempt_limit(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[StructuredOutputError("model did not return structured output")] * 3)
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Change the title to X"}))
        final = ev[-1][1]
        assert "wasn't able to produce a valid change" in final["content"] and "3 attempts" in final["content"]
        assert final["meta"] == {} and len(llm.calls) == 3          # no artifact, bounded cost: exactly 3 model calls
        run = c.get(f"/sessions/{sid}/runs").json()[0]
        assert run["status"] == "gave_up"
        tr = c.get(f"/sessions/{sid}/runs/{run['run_id']}").json()
        assert [s["name"] for s in tr["spans"]].count("plan") == 3


def test_a_rejected_plan_is_retried_with_feedback_and_the_retry_is_visible_in_the_trace(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(BAD), docx_plan(GOOD, summary="Retitled.")])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        final = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Change the title to FY2025 Annual Report"}))[-1][1]
        assert final["meta"]["artifact"]["version"] == 1
        # the second planner call was told why the first plan was rejected
        assert "rejected by the validator" in llm.calls[1][1] or "rejected by the validator" in llm.calls[1][2]
        tr = c.get(f"/sessions/{sid}/runs/{final['run_id']}").json()
        names = [s["name"] for s in tr["spans"]]
        assert names.count("plan") == 2 and names.count("validate") == 2
        first_validate = next(s for s in tr["spans"] if s["name"] == "validate")
        assert first_validate["attrs"].get("plan_errors", 0) >= 1


# ---------------------------------------------------------------- configuration of the real client
def test_the_groq_client_is_built_with_a_timeout_and_no_hidden_sdk_retries(monkeypatch):
    """Retries are owned by our loop (so every attempt is traced and rate-limited); the SDK must not retry silently."""
    from app.core.config import Settings
    import app.llm.provider as prov
    seen = {}

    class FakeChat:
        def __init__(self, **kw):
            seen.update(kw)
    monkeypatch.setattr("langchain_groq.ChatGroq", FakeChat)
    monkeypatch.setattr(prov, "get_settings", lambda: Settings(groq_api_key="k"))
    llm = prov.GroqLLM()
    llm._ChatGroq(max_tokens=10, **llm._kw)
    assert seen["timeout"] == 60.0 and seen["max_retries"] == 0 and llm.retries == 4


# ---------------------------------------------------------------- found by the first real-model eval run
def test_raw_api_error_text_never_reaches_the_user_and_does_not_stall_the_retry(monkeypatch):
    """Eval finding: when the model called a nonexistent tool, Groq's raw 400 JSON ended up in the chat reply, and the
    full max_tokens reservation stayed in the rate window, so every retry waited ~50 s."""
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda s: None)
    from app.llm.provider import GroqLLM, TokenGovernor
    llm = GroqLLM.__new__(GroqLLM)
    llm.retries, llm.governor, llm._messages = 0, TokenGovernor(limit=7200), GroqLLM._messages

    class R:
        def invoke(self, m):
            raise RuntimeError("Error code: 400 - tool_use_failed: attempted to call tool 'json' SECRET-DETAIL")
    with pytest.raises(StructuredOutputError) as ei:
        llm._call(lambda: R(), "s" * 600, "u" * 600, None, 1500)
    assert "SECRET-DETAIL" not in ei.value.message and "tool" in ei.value.message
    used = llm.governor._used(llm.governor.clock())
    assert used < 1500, f"the failed call still holds {used} tokens of the window"  # prompt + margin, not prompt + max_tokens


def test_give_up_message_does_not_expose_provider_errors(tmp_path):
    env = LocalStorage(tmp_path / "store")
    f = graph_upload(env, build_docx, "docx", "report.docx")
    raw = StructuredOutputError("the model did not return structured output (it replied in prose or called a tool that does not exist)")
    llm = GraphLLM(plans=[raw, raw, raw])
    g = build_graph(Deps(llm, env), checkpointer=MemorySaver())
    out = g.invoke({"session_id": "s1", "user_message": "Delete paragraph 99 of the document", "history": [], "files": [f]},
                   {"configurable": {"thread_id": "t"}})
    assert "valid edit plan" in out["response"] and "tool" not in out["response"] and "Error code" not in out["response"]
    # the retry prompt tells the model how to decline properly
    assert "empty ops list" in llm.calls[1][2]
