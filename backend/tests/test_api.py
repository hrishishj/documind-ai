import io
import json
import zipfile

import pytest
from docx import Document
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import func, select

from app.core.config import Settings
from app.database.models import Artifact, ArtifactVersion, Message, UploadedFile
from app.document_engine.samples import build_docx, build_pptx
from app.main import create_app
from tests.test_workflow_graph import FakeLLM, GOOD, docx_plan

NOKEY = dict(groq_api_key="")


def make_client(test_database, tmp_path, llm=None, indexer=None, **over):
    """`indexer=None` (default) disables document search, so these tests never load the real embedder or touch Pinecone."""
    settings = Settings(storage_dir=tmp_path / "store", **{**NOKEY, **over})
    app = create_app(settings, llm=llm, checkpointer=MemorySaver(), database=test_database, indexer=indexer)
    return TestClient(app)


@pytest.fixture()
def docx_bytes(tmp_path):
    p = tmp_path / "r.docx"
    build_docx(p)
    return p.read_bytes()


def sse(resp) -> list[tuple[str, dict]]:
    events, ev = [], None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: ") and ev:
            events.append((ev, json.loads(line[6:])))
    return events


def new_session(c) -> str:
    r = c.post("/sessions", json={})
    assert r.status_code == 201
    return r.json()["id"]


def upload(c, sid, name, data):
    return c.post(f"/sessions/{sid}/files", files={"file": (name, data, "application/octet-stream")})


# ---------------------------------------------------------------- sessions
def test_session_lifecycle(test_database, tmp_path):
    with make_client(test_database, tmp_path) as c:
        sid = new_session(c)
        assert any(s["id"] == sid for s in c.get("/sessions").json())
        d = c.get(f"/sessions/{sid}").json()
        assert d["title"] == "New chat" and d["messages"] == [] and d["files"] == [] and d["artifacts"] == []
        assert c.delete(f"/sessions/{sid}").status_code == 204
        r = c.get(f"/sessions/{sid}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- uploads
def test_upload_stores_under_a_generated_name_and_analyzes(test_database, tmp_path, docx_bytes):
    with make_client(test_database, tmp_path) as c:
        sid = new_session(c)
        r = upload(c, sid, "../../My Quarterly Report.docx", docx_bytes)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["file"]["original_name"] == "My Quarterly Report.docx" and body["file"]["kind"] == "docx"
        a = body["analysis"]
        assert a["format"] == "docx" and a["counts"]["tables"] == 1 and a["page"]["page_width_in"] == 8.5
        assert a["outline"][0]["text"] == "Quarterly Report"
        stored = [p for p in (tmp_path / "store" / sid / "uploads").iterdir()]
        assert len(stored) == 1 and stored[0].suffix == ".docx" and "Quarterly" not in stored[0].name  # server-chosen name
        prof = c.get(f"/sessions/{sid}/files/{body['file']['id']}/profile").json()
        assert prof["format"] == "docx" and prof["images"] and prof["tables"][0]["borders"]["top"]["color"] == "FF0000"
        dl = c.get(f"/sessions/{sid}/files/{body['file']['id']}/download")
        assert dl.status_code == 200 and dl.content == docx_bytes


@pytest.mark.parametrize("name,data,msg", [
    ("virus.exe", b"MZ", "not supported"), ("macro.docm", b"PK\x03\x04", "macro"),
    ("fake.docx", b"this is not a docx", "not a valid"), ("empty.docx", b"", "empty")])
def test_dangerous_uploads_are_rejected_with_a_clear_error(test_database, tmp_path, name, data, msg):
    with make_client(test_database, tmp_path) as c:
        sid = new_session(c)
        r = upload(c, sid, name, data)
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsafe_file" and msg in r.json()["error"]["message"]
        assert c.get(f"/sessions/{sid}").json()["files"] == []  # nothing recorded
        assert not (tmp_path / "store" / sid / "uploads").exists() or not list((tmp_path / "store" / sid / "uploads").iterdir())


def test_size_limit_is_enforced_without_reading_the_whole_file(test_database, tmp_path, docx_bytes):
    with make_client(test_database, tmp_path, max_upload_mb=1) as c:
        sid = new_session(c)
        r = upload(c, sid, "big.docx", docx_bytes + b"\x00" * (2 * 1024 * 1024))
        assert r.status_code == 400 and "larger than the 1 MB limit" in r.json()["error"]["message"]


def test_upload_to_a_missing_session_is_404(test_database, tmp_path, docx_bytes):
    with make_client(test_database, tmp_path) as c:
        assert upload(c, "no-such-session", "a.docx", docx_bytes).status_code == 404


def test_pdf_upload_has_no_template_analysis(test_database, tmp_path):
    with make_client(test_database, tmp_path) as c:
        sid = new_session(c)
        r = upload(c, sid, "doc.pdf", b"%PDF-1.7\n%fake")
        assert r.status_code == 201 and r.json()["analysis"] is None
        assert c.get(f"/sessions/{sid}/files/{r.json()['file']['id']}/profile").status_code == 404


def test_sessions_are_isolated_from_each_other(test_database, tmp_path, docx_bytes):
    with make_client(test_database, tmp_path) as c:
        a, b = new_session(c), new_session(c)
        fid = upload(c, a, "a.docx", docx_bytes).json()["file"]["id"]
        assert c.get(f"/sessions/{b}/files/{fid}/profile").status_code == 404
        assert c.get(f"/sessions/{b}/files/{fid}/download").status_code == 404


# ---------------------------------------------------------------- chat
def test_chat_stream_edit_and_download_roundtrip(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(GOOD, summary="Retitled."), docx_plan({"op": "docx.replace_text", "paragraph": 1, "text": "Second"})])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "report.docx", docx_bytes)
        r = c.post(f"/sessions/{sid}/chat", json={"message": "Change the title to FY2025 Annual Report"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev = sse(r)
        kinds = [e for e, _ in ev]
        assert kinds[0] == "start" and kinds[-1] == "final" and "error" not in kinds
        labels = [d["label"] for e, d in ev if e == "node"]
        assert "Planning the edit" in labels and "Applying changes to a copy of your file" in labels
        final = ev[-1][1]
        art = final["meta"]["artifact"]
        assert art["version"] == 1 and final["meta"]["render"]["preserved"] is True and "Retitled." in final["content"]

        dl = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/1/download")
        assert dl.status_code == 200
        from urllib.parse import unquote
        cd = unquote(dl.headers["content-disposition"])  # RFC 5987 encoded: filename*=utf-8''report%20%28edited%29_v1.docx
        assert cd.startswith("attachment") and "report (edited)_v1.docx" in cd  # sanitized, versioned name
        assert Document(io.BytesIO(dl.content)).paragraphs[0].text == "FY2025 Annual Report"  # the real edited file

        # follow-up edit => version 2 of the same artifact; version 1 is still downloadable and unchanged
        r2 = c.post(f"/sessions/{sid}/chat", json={"message": "Now make the intro say Second"})
        art2 = sse(r2)[-1][1]["meta"]["artifact"]
        assert art2["id"] == art["id"] and art2["version"] == 2
        v1 = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/1/download").content
        v2 = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/2/download").content
        assert Document(io.BytesIO(v1)).paragraphs[1].text != "Second" and Document(io.BytesIO(v2)).paragraphs[1].text == "Second"
        assert c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/3/download").status_code == 404

        d = c.get(f"/sessions/{sid}").json()
        assert [m["role"] for m in d["messages"]] == ["user", "assistant", "user", "assistant"]
        assert d["title"].startswith("Change the title")
        assert d["artifacts"][0]["versions"][1]["version"] == 2
        assert d["messages"][1]["meta"]["artifact"]["name"] == art["name"]


def test_artifact_downloads_do_not_cross_sessions(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(GOOD)])
    with make_client(test_database, tmp_path, llm=llm) as c:
        a, b = new_session(c), new_session(c)
        upload(c, a, "r.docx", docx_bytes)
        art = sse(c.post(f"/sessions/{a}/chat", json={"message": "Change the title"}))[-1][1]["meta"]["artifact"]
        assert c.get(f"/sessions/{b}/artifacts/{art['id']}/versions/1/download").status_code == 404


def test_plain_chat_turn_is_stored_without_an_artifact(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(texts=["Hi! Upload a document and tell me what to change."])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        final = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Hello there"}))[-1]
        assert final[0] == "final" and final[1]["meta"] == {} and final[1]["intent"] == "chat"
        with test_database.session() as db:
            assert db.scalar(select(func.count()).select_from(Artifact).where(Artifact.session_id == sid)) == 0


def test_chat_disabled_without_a_model(test_database, tmp_path):
    with make_client(test_database, tmp_path) as c:  # no llm, no key
        sid = new_session(c)
        r = c.post(f"/sessions/{sid}/chat", json={"message": "hi"})
        assert r.status_code == 502 and "GROQ_API_KEY" in r.json()["error"]["message"]
        assert c.get("/health").json()["chat_enabled"] is False


def test_unexpected_failure_inside_the_stream_becomes_an_error_event(test_database, tmp_path, docx_bytes):
    class Boom(FakeLLM):
        def text(self, *a, **k):
            raise RuntimeError("secret internal detail")
    with make_client(test_database, tmp_path, llm=Boom()) as c:
        sid = new_session(c)
        ev = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Hello there"}))
        assert ev[-1][0] == "error" and ev[-1][1]["code"] == "internal_error"
        assert "secret internal detail" not in json.dumps(ev)  # internals never leak to the client
        # the session is usable again afterwards (gate released)
        assert c.post(f"/sessions/{sid}/chat", json={"message": "Hello again"}).status_code == 200


def test_only_one_request_per_session_at_a_time(test_database, tmp_path):
    llm = FakeLLM(texts=["ok"])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        assert c.app.state.gate.acquire(sid)  # simulate a request already in flight
        r = c.post(f"/sessions/{sid}/chat", json={"message": "Hello"})
        assert r.status_code == 400 and "still running" in r.json()["error"]["message"]
        c.app.state.gate.release(sid)
        assert c.post(f"/sessions/{sid}/chat", json={"message": "Hello"}).status_code == 200


def test_chat_input_validation(test_database, tmp_path):
    with make_client(test_database, tmp_path, llm=FakeLLM()) as c:
        sid = new_session(c)
        assert c.post(f"/sessions/{sid}/chat", json={"message": ""}).status_code == 422
        assert c.post(f"/sessions/{sid}/chat", json={"message": "x" * 5000}).status_code == 422
        assert c.post("/sessions/nope/chat", json={"message": "hi"}).status_code == 404


def test_deleting_a_session_removes_rows_and_files(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(GOOD)])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        upload(c, sid, "r.docx", docx_bytes)
        art_id = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Change the title"}))[-1][1]["meta"]["artifact"]["id"]
        with test_database.session() as db:  # prove the rows exist first, so the "== 0" below means something
            assert db.scalar(select(func.count()).select_from(ArtifactVersion).where(ArtifactVersion.artifact_id == art_id)) == 1
            assert db.scalar(select(func.count()).select_from(Message).where(Message.session_id == sid)) == 2
        assert (tmp_path / "store" / sid).exists()
        c.delete(f"/sessions/{sid}")
        assert not (tmp_path / "store" / sid).exists()
        with test_database.session() as db:
            for model in (Message, UploadedFile, Artifact):
                assert db.scalar(select(func.count()).select_from(model).where(model.session_id == sid)) == 0
            assert db.scalar(select(func.count()).select_from(ArtifactVersion).where(ArtifactVersion.artifact_id == art_id)) == 0


def test_pptx_upload_and_slide_edit_end_to_end(test_database, tmp_path):
    p = tmp_path / "d.pptx"
    build_pptx(p)
    llm = FakeLLM(plans=[{"format": "pptx", "summary": "Added agenda.", "ops": [
        {"op": "pptx.add_slide", "layout": "Title and Content", "texts": {"0": "Agenda", "1": "A\nB"}}]}])
    with make_client(test_database, tmp_path, llm=llm) as c:
        sid = new_session(c)
        up = upload(c, sid, "d.pptx", p.read_bytes()).json()
        assert up["analysis"]["counts"]["slides"] == 3 and "Title and Content" in up["analysis"]["layouts"]
        art = sse(c.post(f"/sessions/{sid}/chat", json={"message": "Add an agenda slide"}))[-1][1]["meta"]["artifact"]
        out = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/1/download").content
        names = zipfile.ZipFile(io.BytesIO(out)).namelist()
        assert sum(1 for n in names if n.startswith("ppt/slides/slide")) == 4
