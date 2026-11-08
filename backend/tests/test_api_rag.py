"""Upload -> background indexing -> grounded chat -> persisted citations, through the real HTTP API."""

import pytest
from sqlalchemy import select

from app.database.models import Citation
from tests.fakes import fake_indexer
from tests.test_api import FakeLLM, make_client, new_session, sse, upload


@pytest.fixture(autouse=True)
def lenient_cutoff(monkeypatch):
    monkeypatch.setattr("app.rag.answer.MIN_RELEVANCE", 0.2)


@pytest.fixture()
def docx_bytes(tmp_path):
    from docx import Document
    d = Document()
    d.add_heading("Quarterly Report", 1)
    d.add_paragraph("Total revenue for the quarter was 4,210 million dollars.")
    d.add_paragraph("Headcount ended the year at 9,876 employees.")
    p = tmp_path / "r.docx"
    d.save(p)
    return p.read_bytes()


def test_upload_is_indexed_in_the_background_and_reports_status(test_database, tmp_path, docx_bytes):
    idx = fake_indexer()
    with make_client(test_database, tmp_path, indexer=idx) as c:
        sid = new_session(c)
        up = upload(c, sid, "report.docx", docx_bytes).json()["file"]
        assert up["index_status"] == "pending"                     # the response does not wait for embedding
        f = c.get(f"/sessions/{sid}").json()["files"][0]
        # a heading and two short paragraphs pack into one ~900-character passage
        assert f["index_status"] == "ready" and f["chunk_count"] == 1 and f["index_error"] == ""
        assert idx.store.count(sid) == f["chunk_count"]            # one namespace per conversation
        assert c.get("/health").json()["retrieval"] == "memory"


def test_question_gets_grounded_answer_with_persisted_citations(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(answers=[{"answer": "Total revenue for the quarter was 4,210 million dollars [1].", "answerable": True}])
    with make_client(test_database, tmp_path, llm=llm, indexer=fake_indexer()) as c:
        sid = new_session(c)
        upload(c, sid, "report.docx", docx_bytes)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": "What was total revenue for the quarter?"}))
        final = ev[-1]
        assert final[0] == "final" and "[1]" in final[1]["content"]
        cit = final[1]["citations"]
        assert cit[0]["ref"] == 1 and cit[0]["title"] == "report.docx" and cit[0]["locator"].startswith("¶")
        assert "4,210" in cit[0]["snippet"] and cit[0]["source_type"] == "document"
        # persisted: the history endpoint returns them with the message, so they survive a reload
        msgs = c.get(f"/sessions/{sid}").json()["messages"]
        stored = msgs[-1]["citations"]
        assert [(x["ref"], x["title"]) for x in stored] == [(1, "report.docx")] and stored[0]["file_id"]
        assert msgs[0]["citations"] == []                          # the user's message has none
        with test_database.session() as db:
            assert db.scalar(select(Citation).where(Citation.message_id == msgs[-1]["id"])).locator == stored[0]["locator"]


def test_deleting_the_conversation_deletes_its_vectors(test_database, tmp_path, docx_bytes):
    idx = fake_indexer()
    with make_client(test_database, tmp_path, indexer=idx) as c:
        a, b = new_session(c), new_session(c)
        upload(c, a, "r.docx", docx_bytes)
        upload(c, b, "r.docx", docx_bytes)
        assert idx.store.count(a) > 0 and idx.store.count(b) > 0
        c.delete(f"/sessions/{a}")
        assert idx.store.count(a) == 0 and idx.store.count(b) > 0   # the other conversation is untouched


def test_deleting_during_indexing_leaves_no_orphan_vectors(test_database, tmp_path, docx_bytes):
    """Race found by a live Docker test: delete lands mid-indexing, then the job writes its vectors."""
    idx = fake_indexer()
    original = idx.index_file
    with make_client(test_database, tmp_path, indexer=idx) as c:
        def delete_then_write(session_id, file_id, name, path):
            kept = tmp_path / "kept.docx"
            kept.write_bytes(path.read_bytes())                             # deleting the conversation removes the upload
            assert c.delete(f"/sessions/{session_id}").status_code == 204   # the user deletes mid-indexing
            n = original(session_id, file_id, name, kept)                   # ...and the job still writes vectors
            assert idx.store.count(session_id) > 0                          # (precondition: they really were written)
            return n
        idx.index_file = delete_then_write
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        assert c.get(f"/sessions/{sid}").status_code == 404
        assert idx.store.count(sid) == 0


def test_indexing_failure_is_reported_but_the_upload_still_works(test_database, tmp_path, docx_bytes):
    idx = fake_indexer()

    def boom(*a, **k):
        raise RuntimeError("embedding service exploded")
    idx.index_file = boom
    with make_client(test_database, tmp_path, indexer=idx) as c:
        sid = new_session(c)
        assert upload(c, sid, "r.docx", docx_bytes).status_code == 201
        f = c.get(f"/sessions/{sid}").json()["files"][0]
        assert f["index_status"] == "failed" and "embedding service exploded" in f["index_error"]
        assert c.get(f"/sessions/{sid}/files/{f['id']}/profile").status_code == 200   # template analysis unaffected


def test_a_pdf_without_text_is_marked_failed_with_a_helpful_reason(test_database, tmp_path):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()                                                  # a valid PDF whose only page has no text
    blank = doc.tobytes()
    with make_client(test_database, tmp_path, indexer=fake_indexer()) as c:
        sid = new_session(c)
        assert upload(c, sid, "scan.pdf", blank).status_code == 201
        f = c.get(f"/sessions/{sid}").json()["files"][0]
        assert f["index_status"] == "failed" and "scanned image" in f["index_error"]


def test_without_an_indexer_files_are_marked_skipped_and_chat_still_works(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(texts=["Hello!"])
    with make_client(test_database, tmp_path, llm=llm, indexer=None) as c:
        sid = new_session(c)
        up = upload(c, sid, "r.docx", docx_bytes).json()["file"]
        assert up["index_status"] == "skipped"
        assert sse(c.post(f"/sessions/{sid}/chat", json={"message": "Hello there"}))[-1][0] == "final"
        assert c.get("/health").json()["retrieval"] is None


def test_text_and_markdown_files_are_searchable_too(test_database, tmp_path):
    idx = fake_indexer()
    with make_client(test_database, tmp_path, indexer=idx) as c:
        sid = new_session(c)
        upload(c, sid, "notes.md", b"# Notes\n\nThe launch date is 14 March.\n\nBudget approved at 50k.")
        f = c.get(f"/sessions/{sid}").json()["files"][0]
        assert f["index_status"] == "ready" and f["chunk_count"] >= 1
        hits = idx.search(sid, "launch date", k=1)
        assert "14 March" in hits[0].text and hits[0].locator.startswith("line")
