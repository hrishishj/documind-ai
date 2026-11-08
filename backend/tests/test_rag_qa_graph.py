import pytest
from docx import Document
from langgraph.checkpoint.memory import MemorySaver

from app.core.errors import ExternalServiceError
from app.rag.answer import verify_citations
from app.rag.retrieve import Source, lexical_score
from app.services.storage import LocalStorage
from app.workflows.graph import Deps, build_graph
from tests.fakes import fake_indexer
from tests.test_workflow_graph import FakeLLM


@pytest.fixture(autouse=True)
def lenient_cutoff(monkeypatch):
    # the fake bag-of-words embedder is not calibrated like bge; the real cutoff is measured in test_rag_eval.py
    monkeypatch.setattr("app.rag.answer.MIN_RELEVANCE", 0.25)


@pytest.fixture()
def world(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    idx = fake_indexer()
    d = Document()
    d.add_heading("Annual Report", 1)
    d.add_paragraph("Total revenue for fiscal 2026 was 331,839 million, up 18 percent from 281,724 million.")
    d.add_paragraph("The company is headquartered in Redmond, Washington.")
    d.add_paragraph("Research and development expense was 35,562 million in fiscal 2026.")
    p = tmp_path / "r.docx"
    d.save(p)
    stored, _ = storage.save_upload("sA", p.read_bytes(), "docx")
    idx.index_file("sA", "f1", "annual.docx", stored)
    files = [{"id": "f1", "name": "annual.docx", "path": storage.rel(stored), "format": "docx", "kind": "upload", "index_status": "ready"}]
    return storage, idx, files


def ask(world, llm, message, sid="sA", files=None, **kw):
    storage, idx, fl = world
    g = build_graph(Deps(llm, storage, retriever=idx), checkpointer=MemorySaver())
    return g.invoke({"session_id": sid, "user_message": message, "history": [], "files": fl if files is None else files},
                    {"configurable": {"thread_id": sid}})


def test_grounded_answer_carries_validated_citations_with_locators(world):
    llm = FakeLLM(answers=[{"answer": "Total revenue in fiscal 2026 was 331,839 million [1].", "answerable": True}])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert out["intent"] == "chat" and "331,839" in out["response"] and "[1]" in out["response"]
    c = out["citations"]
    assert len(c) == 1 and c[0]["n"] == 1 and c[0]["file_name"] == "annual.docx" and c[0]["file_id"] == "f1"
    assert c[0]["locator"].startswith("¶") and "331,839" in c[0]["snippet"]
    kind, system, _ = llm.calls[0]
    assert kind == "qa" and "ONLY the numbered sources" in system and "annual.docx" in system


def test_invented_citation_markers_are_stripped_and_never_become_citations(world):
    llm = FakeLLM(answers=[{"answer": "Revenue was 331,839 million [1][7]. Margins rose [9].", "answerable": True}])
    out = ask(world, llm, "What was total revenue?")
    assert "[7]" not in out["response"] and "[9]" not in out["response"] and "[1]" in out["response"]
    assert [c["n"] for c in out["citations"]] == [1]


def test_not_found_answers_have_no_citations(world):
    llm = FakeLLM(answers=[{"answer": "I couldn't find that in your documents.", "answerable": False}])
    out = ask(world, llm, "What is the revenue of total fiscal quarterly profit?")
    assert out["citations"] == [] and "couldn't find" in out["response"]


def test_answer_without_any_valid_source_marker_is_flagged_not_silently_trusted(world):
    llm = FakeLLM(answers=[{"answer": "Total revenue was around 330 billion dollars.", "answerable": True}])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert out["citations"] == []   # "330" appears nowhere in the sources, so nothing can be attributed
    assert "couldn't tie this answer to a specific passage" in out["response"]


def test_forgotten_markers_are_recovered_when_the_figures_appear_verbatim_in_a_source(world):
    llm = FakeLLM(answers=[{"answer": "The company spent $35,562 million on research and development in fiscal 2026.", "answerable": True}])
    out = ask(world, llm, "How much was research and development expense in fiscal 2026?")
    assert [c["n"] for c in out["citations"]] == [1] and out["citations"][0]["file_name"] == "annual.docx"
    assert out["response"].rstrip().endswith("[1]") and "couldn't tie" not in out["response"]
    assert "35,562" in out["citations"][0]["snippet"]  # the attributed passage really contains the figure


def test_an_empty_answer_never_becomes_a_blank_or_generic_done(world):
    """Regression (live): answerable=false with an empty answer text produced the reply 'Done.'"""
    for payload in ({"answer": "", "answerable": False}, {"answer": "   ", "answerable": True}):
        out = ask(world, FakeLLM(answers=[payload]), "What is the CEO's annual salary for fiscal 2026 total revenue?")
        assert out["response"] == "I couldn't find that in your documents." and out["citations"] == []


def test_answer_text_is_normalised_so_copy_and_search_work(world):
    llm = FakeLLM(answers=[{"answer": "Revenue was 331,839\u202fmillion\u00a0dollars\u200b [1].", "answerable": True}])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert "331,839 million dollars [1]" in out["response"] and "\u202f" not in out["response"]


def test_unrelated_message_is_plain_chat_without_calling_the_qa_model(world, monkeypatch):
    monkeypatch.setattr("app.rag.answer.MIN_RELEVANCE", 0.6)  # an unrelated message scores far below this
    llm = FakeLLM(texts=["Hi! How can I help?"])
    out = ask(world, llm, "zebra quantum pineapple")
    assert out["response"].startswith("Hi!") and out["citations"] == []
    assert [k for k, _, _ in llm.calls] == ["text"]


def test_files_still_indexing_are_mentioned_instead_of_guessing(world):
    storage, idx, files = world
    pending = [{**files[0], "index_status": "indexing"}]
    llm = FakeLLM(texts=["Let me check."])
    out = ask(world, llm, "What was total revenue in fiscal 2026?", files=pending)
    assert "still being indexed" in out["response"] and out["citations"] == []
    assert [k for k, _, _ in llm.calls] == ["text"]  # no retrieval attempted against a half-built index


def test_retrieval_failure_degrades_to_plain_chat(world):
    storage, idx, files = world

    class Broken:
        def search(self, *a, **k):
            raise RuntimeError("pinecone down")
    llm = FakeLLM(texts=["Sorry, I can only chat right now."])
    g = build_graph(Deps(llm, storage, retriever=Broken()), checkpointer=MemorySaver())
    out = g.invoke({"session_id": "sA", "user_message": "What was total revenue?", "history": [], "files": files},
                   {"configurable": {"thread_id": "x"}})
    assert out["response"].startswith("Sorry") and out["citations"] == []


def test_a_malformed_reply_is_retried_once_and_then_succeeds(world):
    from app.llm.provider import StructuredOutputError
    llm = FakeLLM(answers=[StructuredOutputError("no tool call"), {"answer": "Revenue was 331,839 million [1].", "answerable": True}])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert out["citations"] and "331,839" in out["response"] and [k for k, _, _ in llm.calls] == ["qa", "qa"]


def test_two_malformed_replies_say_so_instead_of_claiming_the_model_is_unreachable(world):
    from app.llm.provider import StructuredOutputError
    llm = FakeLLM(answers=[StructuredOutputError("no tool call"), StructuredOutputError("no tool call")])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert "couldn't put together a reliable answer" in out["response"] and "couldn't reach" not in out["response"]
    assert out["citations"] == [] and not out.get("error")


def test_llm_outage_while_answering_is_contained(world):
    llm = FakeLLM(answers=[ExternalServiceError("down")])
    out = ask(world, llm, "What was total revenue in fiscal 2026?")
    assert "couldn't reach the language model" in out["response"]


def test_one_conversation_can_never_retrieve_another_conversations_documents(world):
    llm = FakeLLM(texts=["I don't see any documents."])
    out = ask(world, llm, "What was total revenue in fiscal 2026?", sid="sB")  # same files list, different namespace
    assert out["citations"] == [] and [k for k, _, _ in llm.calls] == ["text"]


def test_citations_reset_every_turn(world):
    storage, idx, files = world
    llm = FakeLLM(answers=[{"answer": "Revenue was 331,839 million [1].", "answerable": True}], texts=["Sure."])
    g = build_graph(Deps(llm, storage, retriever=idx), checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}
    t1 = g.invoke({"session_id": "sA", "user_message": "What was total revenue in fiscal 2026?", "history": [], "files": files}, cfg)
    assert t1["citations"]
    t2 = g.invoke({"session_id": "sA", "user_message": "zebra quantum pineapple", "history": [], "files": []}, cfg)
    assert t2["citations"] == []  # stale citations from turn 1 must not attach to turn 2's answer


# ------------------------------------------------------------------ units
def S(n, text="x"):
    return Source(n, "f", "a.docx", "¶1", "", text, 0.9, 0.9)


def test_the_quoted_evidence_shows_the_supporting_lines_not_just_the_start_of_the_passage():
    """Regression (seen in the browser): the snippet for a spreadsheet citation began with a title row and never showed
    the 331,839 the answer relied on."""
    from app.rag.answer import evidence_snippet
    filler = [f"Segment line number {i} with a longer descriptive label | {10000 + i} | {9000 + i} | {i % 9}%" for i in range(12)]
    passage = "\n".join(["[Sheet inc] Fiscal Year 2026 Compared with Fiscal Year 2025", "(In millions, except percentages)",
                         "Productivity and Business Processes | 139996 | 120810 | 16%", "More Personal Computing | 47000 | 41000 | 15%",
                         "Intelligent Cloud | 144000 | 118000 | 22%", *filler,
                         "Total revenue | 331839 | 281724 | 18%", "Operating income | 155237 | 128528 | 21%"])
    assert len(passage) > 900                                            # long enough that excerpting really happens
    s = evidence_snippet(passage, "Total revenue was $331,839 million, up from $281,724 million [1].", "What was total revenue in fiscal 2026?")
    assert "Total revenue | 331839 | 281724" in s and len(s) <= 320
    assert "Intelligent Cloud" not in s                                   # irrelevant rows are left out
    short = "Commission File Number 001-37845"
    assert evidence_snippet(short, "It is 001-37845 [1].", "file number") == short       # short passages are shown whole
    assert evidence_snippet(passage, "No figures here.", "") == evidence_snippet(passage, "No figures here.", "")  # no signal: head


def test_evidence_falls_back_to_the_head_when_nothing_matches_and_never_exceeds_the_limit():
    from app.rag.answer import evidence_snippet
    passage = "\n".join(f"Row {i} | alpha beta gamma delta" for i in range(60))
    s = evidence_snippet(passage, "Something unrelated", "")
    assert s.startswith("Row 0") and len(s) <= 320
    many = "\n".join(f"Total revenue line {i} | {100000 + i}" for i in range(60))
    assert len(evidence_snippet(many, "revenue 100001 100002 100003 100004", "total revenue")) <= 320


def test_citation_snippets_in_the_answer_show_the_evidence(world):
    llm = FakeLLM(answers=[{"answer": "Research and development expense was 35,562 million [1].", "answerable": True}])
    out = ask(world, llm, "How much was research and development expense in fiscal 2026?")
    assert "35,562" in out["citations"][0]["snippet"]


def test_attribution_ignores_years_and_needs_real_figures():
    from app.rag.answer import attribute_sources
    srcs = [S(1, "Fiscal 2026 and 2025 were good years."), S(2, "Revenue | 331,839 | 281,724"), S(3, "R&D | 35,562")]
    assert attribute_sources("It happened in 2026.", srcs) == []                       # a year alone proves nothing
    assert attribute_sources("The company is large.", srcs) == []                      # no figures: cannot be verified
    assert [s.n for s in attribute_sources("Revenue rose to $331,839 million from 281,724.", srcs)] == [2]
    assert [s.n for s in attribute_sources("Revenue 331,839 and R&D 35,562.", srcs)] == [2, 3]  # best coverage first, <= 2
    assert attribute_sources("Revenue was 999,999.", srcs) == []                       # a figure that is in no source


def test_verify_citations_orders_and_dedupes():
    text, cited, removed = verify_citations("A [2]. B [1]. C [2]. D [5].", [S(1), S(2), S(3)])
    assert [s.n for s in cited] == [2, 1] and removed == [5] and "[5]" not in text and "D ." not in text


def test_verify_citations_cleans_spacing_left_by_removed_markers():
    text, _, _ = verify_citations("Revenue grew [9] strongly [1] .", [S(1)])
    assert "[9]" not in text and "  " not in text


def test_lexical_score_matches_numbers_regardless_of_thousands_separators():
    assert lexical_score("revenue 331,839", "Total revenue | 331839 | 281724") > 0.5
    assert lexical_score("what is the weather", "Total revenue | 331839") == 0.0


def test_the_calibrated_constants_stay_at_their_measured_values():
    """Pins the tuned numbers (read from source, because this module monkeypatches the live value). Changing them
    means re-running tests/test_rag_eval.py and updating docs/RAG_EVAL.md."""
    import ast
    from pathlib import Path

    def consts(path):
        out = {}
        for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant):
                out[node.targets[0].id] = node.value.value
        return out
    base = Path(__file__).resolve().parents[1] / "app" / "rag"
    assert consts(base / "answer.py")["MIN_RELEVANCE"] == 0.55 and consts(base / "answer.py")["TOP_K"] == 8
    assert consts(base / "retrieve.py")["LEX_WEIGHT"] == 0.15
