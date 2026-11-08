import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.router import RouteDecision, quick_route
from app.core.errors import ExternalServiceError
from app.document_engine.plan import EditPlan
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.services.storage import LocalStorage
from app.workflows.graph import MAX_ATTEMPTS, Deps, build_graph


class FakeLLM:
    """Scripted stand-in for Groq. Records every call so tests can assert what was (not) sent to the model."""

    def __init__(self, plans=(), routes=(), texts=()):
        self.plans, self.routes, self.texts = list(plans), list(routes), list(texts)
        self.calls: list[tuple[str, str, str]] = []  # (kind, system, user)

    def structured(self, schema, system, user, history=None, max_tokens=None):
        kind = "route" if schema is RouteDecision else "plan"
        self.calls.append((kind, system, user))
        item = (self.routes if kind == "route" else self.plans).pop(0)
        if isinstance(item, Exception):
            raise item
        return item if not isinstance(item, dict) else (item if kind == "route" else schema.model_validate(item)) \
            if kind == "plan" else RouteDecision(**item)

    def text(self, system, user, history=None, max_tokens=None):
        self.calls.append(("text", system, user))
        item = self.texts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def env(tmp_path):
    return LocalStorage(tmp_path / "store")


def upload(storage, builder, ext, name, sid="s1", id_="f1"):
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp()) / f"x.{ext}"
    builder(tmp)
    path, _ = storage.save_upload(sid, tmp.read_bytes(), ext)
    return {"id": id_, "name": name, "path": storage.rel(path), "format": ext, "kind": "upload"}


def run(deps, files, message, sid="s1", history=(), cp=None, thread="t1"):
    g = build_graph(deps, checkpointer=cp)
    cfg = {"configurable": {"thread_id": thread}} if cp else None
    return g.invoke({"session_id": sid, "user_message": message, "history": list(history), "files": files}, cfg)


def docx_plan(*ops, summary="Updated."):
    return {"format": "docx", "summary": summary, "ops": list(ops)}


GOOD = {"op": "docx.replace_text", "paragraph": 0, "text": "FY2025 Annual Report"}
BAD = {"op": "docx.replace_text", "paragraph": 99, "text": "x"}


# ---------------------------------------------------------------- happy paths
def test_docx_edit_creates_artifact_without_calling_the_router(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[docx_plan(GOOD, summary="Retitled the report.")])
    out = run(Deps(llm, env), [f], "Change the title to FY2025 Annual Report")
    assert [c[0] for c in llm.calls] == ["plan"]  # quick-route handled routing: exactly one model call
    a = out["artifact"]
    assert a["version"] == 1 and a["format"] == "docx" and a["name"] == "report (edited).docx"
    assert env.resolve(a["path"]).exists() and out["render"]["preserved"] is True
    assert "Retitled the report." in out["response"] and "preserved" in out["response"]
    # the planner saw an outline, not a JSON dump, and the user's request
    assert "P0 [Heading 1] Quarterly Report" in llm.calls[0][1] and "Change the title" in llm.calls[0][2]


def test_pptx_and_xlsx_edits(env):
    p = upload(env, build_pptx, "pptx", "deck.pptx", id_="p1")
    x = upload(env, build_xlsx, "xlsx", "book.xlsx", id_="x1")
    llm = FakeLLM(plans=[
        {"format": "pptx", "summary": "Added an agenda slide.", "ops": [
            {"op": "pptx.add_slide", "layout": "Title and Content", "texts": {"0": "Agenda", "1": "Intro\nResults"}}]},
        {"format": "xlsx", "summary": "Added West.", "ops": [
            {"op": "xlsx.append_row", "sheet": "Report", "values": ["West", 1, 0.1, "2024-04-01"]}]}])
    o1 = run(Deps(llm, env), [p], "Add an agenda slide")
    o2 = run(Deps(llm, env), [x], "Add a row for West")
    assert o1["artifact"]["format"] == "pptx" and o2["artifact"]["format"] == "xlsx"
    assert o1["render"]["preserved"] and o2["render"]["preserved"]


def test_followup_edit_makes_version_2_of_the_same_artifact_and_keeps_v1(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[docx_plan(GOOD), docx_plan({"op": "docx.replace_text", "paragraph": 1, "text": "Second edit"})])
    deps = Deps(llm, env)
    first = run(deps, [f], "Change the title")["artifact"]
    art = {"id": first["id"], "name": first["name"], "path": first["path"], "format": "docx", "kind": "artifact", "version": 1}
    second = run(deps, [f, art], "Now make the intro say Second edit")["artifact"]
    assert second["id"] == first["id"] and second["version"] == 2 and second["path"] != first["path"]
    assert env.resolve(first["path"]).exists() and env.resolve(second["path"]).exists()  # v1 kept
    assert second["from_file"] == first["id"]
    assert "(edited) (edited)" not in second["name"]


# ---------------------------------------------------------------- retry loop
def test_invalid_plan_is_retried_with_the_validator_errors_fed_back(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[docx_plan(BAD), docx_plan(GOOD)])
    out = run(Deps(llm, env), [f], "Change the title")
    assert out["attempts"] == 2 and out["artifact"] is not None
    retry_user = llm.calls[1][2]
    assert "rejected by the validator" in retry_user and "paragraph 99 out of range" in retry_user


def test_gives_up_after_max_attempts_with_an_honest_message(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[docx_plan(BAD)] * MAX_ATTEMPTS)
    out = run(Deps(llm, env), [f], "Change the title")
    assert out["artifact"] is None and out["attempts"] == MAX_ATTEMPTS
    assert "wasn't able" in out["response"] and "paragraph 99" in out["response"]
    assert len([c for c in llm.calls if c[0] == "plan"]) == MAX_ATTEMPTS  # bounded: no infinite loop


def test_schema_violation_from_the_model_triggers_a_retry(env):
    f = upload(env, build_docx, "docx", "report.docx")

    class Flaky(FakeLLM):
        def structured(self, schema, system, user, history=None, max_tokens=None):
            if not self.calls:
                self.calls.append(("plan", system, user))
                EditPlan.model_validate({"format": "docx", "ops": [{"op": "docx.replace_text", "paragraph": -1, "text": "x"}]})
            return super().structured(schema, system, user, history, max_tokens)

    llm = Flaky(plans=[docx_plan(GOOD)])
    out = run(Deps(llm, env), [f], "Change the title")
    assert out["artifact"] is not None and out["attempts"] == 2
    assert "schema violation" in llm.calls[1][2]


def test_unparseable_model_output_is_retried_not_fatal(env):
    from app.llm.provider import StructuredOutputError
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[StructuredOutputError("the model's output did not match the schema: bad json"), docx_plan(GOOD)])
    out = run(Deps(llm, env), [f], "Change the title")
    assert out["artifact"] is not None and out["attempts"] == 2
    assert "did not match the schema" in llm.calls[1][2]  # the reason is fed back to the model


def test_model_declining_returns_its_explanation_and_creates_nothing(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[docx_plan(summary="I need the new company name before I can replace it.")])
    out = run(Deps(llm, env), [f], "Change the company name")
    assert out["artifact"] is None and "need the new company name" in out["response"]


# ---------------------------------------------------------------- routing
def test_question_goes_to_chat_with_a_document_outline_and_no_edit(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(texts=["The report is titled Quarterly Report."])
    out = run(Deps(llm, env), [f], "What is the title of the document?")
    assert out["response"].startswith("The report is titled") and out["artifact"] is None
    kind, system, _ = llm.calls[0]
    assert kind == "text" and "P0 [Heading 1] Quarterly Report" in system and "ONLY from the outline" in system


def test_edit_request_without_a_file_asks_for_one_and_costs_no_tokens(env):
    llm = FakeLLM()
    out = run(Deps(llm, env), [], "Please change the title to Foo")
    assert "attach" in out["response"].lower() and llm.calls == []


def test_ambiguous_target_uses_the_llm_router_and_validates_its_answer(env):
    a = upload(env, build_docx, "docx", "alpha.docx", id_="a")
    b = upload(env, build_docx, "docx", "beta.docx", id_="b")
    llm = FakeLLM(routes=[{"intent": "edit", "target_file_id": "b", "reason": "x"}], plans=[docx_plan(GOOD)])
    out = run(Deps(llm, env), [a, b], "Update the title please")
    assert [c[0] for c in llm.calls] == ["route", "plan"] and out["target"]["id"] == "b"


def test_router_invented_file_id_is_not_trusted(env):
    a = upload(env, build_docx, "docx", "alpha.docx", id_="a")
    b = upload(env, build_docx, "docx", "beta.docx", id_="b")
    llm = FakeLLM(routes=[{"intent": "edit", "target_file_id": "does-not-exist", "reason": "x"}])
    out = run(Deps(llm, env), [a, b], "Update the title please")
    assert out["artifact"] is None and "Which document" in out["response"] and "alpha.docx" in out["response"]


def test_file_named_in_the_message_wins_without_an_llm_router_call(env):
    a = upload(env, build_docx, "docx", "alpha.docx", id_="a")
    b = upload(env, build_docx, "docx", "beta.docx", id_="b")
    llm = FakeLLM(plans=[docx_plan(GOOD)])
    out = run(Deps(llm, env), [a, b], "In beta change the title")
    assert out["target"]["id"] == "b" and [c[0] for c in llm.calls] == ["plan"]


def test_pdf_is_never_an_edit_target(env):
    d = upload(env, build_docx, "docx", "r.docx", id_="d")
    pdf = {"id": "p", "name": "deck.pdf", "path": d["path"], "format": "pdf", "kind": "upload"}
    dec, target = quick_route("change the title", [pdf])
    assert dec.intent == "clarify" and target is None


@pytest.mark.parametrize("msg,expected", [
    ("Slide 3's title should be Results", "edit"), ("make it shorter", "edit"), ("Hello there", "chat"),
    ("What does slide 2 say?", "chat"), ("Explain the revenue table", "chat")])
def test_quick_route_classification(msg, expected):
    f = [{"id": "f", "name": "r.docx", "path": "x", "format": "docx", "kind": "upload"}]
    got = quick_route(msg, f)
    assert got is not None and got[0].intent == expected


def test_how_do_i_question_is_left_to_the_llm():
    f = [{"id": "f", "name": "r.docx", "path": "x", "format": "docx", "kind": "upload"}]
    assert quick_route("How do I change the margins?", f) is None


# ---------------------------------------------------------------- failure containment
def test_llm_outage_during_planning_is_a_friendly_message_not_a_crash(env):
    f = upload(env, build_docx, "docx", "report.docx")
    llm = FakeLLM(plans=[ExternalServiceError("LLM call failed: RateLimitError")])
    out = run(Deps(llm, env), [f], "Change the title")
    assert out["artifact"] is None and "couldn't reach the language model" in out["response"]


def test_llm_outage_during_chat_is_contained(env):
    llm = FakeLLM(texts=[ExternalServiceError("down")])
    out = run(Deps(llm, env), [], "Hello there")
    assert "couldn't reach" in out["response"]


def test_analysis_is_cached_by_content_hash(env):
    f = upload(env, build_docx, "docx", "report.docx")
    deps = Deps(FakeLLM(plans=[docx_plan(GOOD), docx_plan(GOOD)]), env)
    run(deps, [f], "Change the title")
    run(deps, [f], "Change the title again")
    assert len(deps.profiles) == 1


def test_state_from_the_previous_turn_never_leaks_into_the_next(env):
    """Regression: with a persistent thread, a stale `response` made turn 2 look like 'the model declined'."""
    f = upload(env, build_docx, "docx", "report.docx")
    cp = MemorySaver()
    llm = FakeLLM(plans=[docx_plan(GOOD), docx_plan({"op": "docx.replace_text", "paragraph": 1, "text": "Second"})],
                  texts=["A chat answer."])
    deps = Deps(llm, env)
    t1 = run(deps, [f], "Change the title", cp=cp, thread="s")
    art = {"id": t1["artifact"]["id"], "name": t1["artifact"]["name"], "path": t1["artifact"]["path"],
           "format": "docx", "kind": "artifact", "version": 1}
    t2 = run(deps, [f, art], "Now make the intro say Second", cp=cp, thread="s")
    assert t2["artifact"] is not None and t2["artifact"]["version"] == 2 and not t2.get("declined")
    t3 = run(deps, [f, art], "Hello there", cp=cp, thread="s")  # a chat turn after edits carries no artifact/plan
    assert t3["response"] == "A chat answer." and t3["artifact"] is None and t3["plan"] is None


def test_checkpointed_graph_runs_under_a_thread_id(env):
    f = upload(env, build_docx, "docx", "report.docx")
    cp = MemorySaver()
    llm = FakeLLM(plans=[docx_plan(GOOD)])
    out = run(Deps(llm, env), [f], "Change the title", cp=cp, thread="session-42")
    assert out["artifact"] is not None
    assert cp.get_tuple({"configurable": {"thread_id": "session-42"}}) is not None  # state persisted for the session
