"""Cross-client isolation and abuse limits."""
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import Settings
from app.main import create_app
from app.services.gate import SessionGate, SlotLimiter
from app.workflows.graph import LRUCache
from tests.test_api import GOOD, FakeLLM, docx_plan, new_session, sse, upload


def app_for(test_database, tmp_path, llm=None, indexer=None, **over):
    return create_app(Settings(storage_dir=tmp_path / "store", groq_api_key="", **over), llm=llm,
                      checkpointer=MemorySaver(), database=test_database, indexer=indexer)


@pytest.fixture()
def docx_bytes(tmp_path):
    from app.document_engine.samples import build_docx
    p = tmp_path / "r.docx"
    build_docx(p)
    return p.read_bytes()


def test_another_browser_cannot_see_touch_or_delete_a_conversation(test_database, tmp_path, docx_bytes):
    llm = FakeLLM(plans=[docx_plan(GOOD)])
    app = app_for(test_database, tmp_path, llm=llm)
    with TestClient(app) as alice, TestClient(app) as mallory:   # two cookie jars = two browsers
        sid = new_session(alice)
        fid = upload(alice, sid, "secret.docx", docx_bytes).json()["file"]["id"]
        art = sse(alice.post(f"/sessions/{sid}/chat", json={"message": "Change the title"}))[-1][1]["meta"]["artifact"]

        # mallory has her own identity and an empty list, even though alice's session exists in the same database
        assert mallory.get("/sessions").json() == []
        probes = [
            mallory.get(f"/sessions/{sid}"),
            mallory.delete(f"/sessions/{sid}"),
            upload(mallory, sid, "x.docx", docx_bytes),
            mallory.post(f"/sessions/{sid}/chat", json={"message": "Change the title"}),
            mallory.get(f"/sessions/{sid}/files/{fid}/profile"),
            mallory.get(f"/sessions/{sid}/files/{fid}/download"),
            mallory.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/1/download"),
        ]
        assert [r.status_code for r in probes] == [404] * 7
        assert all(r.json()["error"]["code"] == "not_found" for r in probes)  # same answer as "does not exist"
        # ... and alice is unaffected
        assert alice.get(f"/sessions/{sid}").status_code == 200
        assert alice.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/1/download").status_code == 200


def test_requests_without_a_cookie_get_no_access_and_a_fresh_identity(test_database, tmp_path):
    app = app_for(test_database, tmp_path)
    with TestClient(app) as owner, TestClient(app) as anon:
        sid = new_session(owner)
        r = anon.get("/sessions")
        assert r.json() == [] and "dm_client" in r.cookies
        assert anon.get(f"/sessions/{sid}").status_code == 404


def test_cookie_is_httponly_samesite_and_unguessable(test_database, tmp_path):
    app = app_for(test_database, tmp_path)
    with TestClient(app) as c:
        r = c.get("/sessions")
        header = r.headers["set-cookie"]
        assert "HttpOnly" in header and "SameSite=lax" in header and "Max-Age=31536000" in header
        assert len(r.cookies["dm_client"]) == 32


def test_forged_or_malformed_cookies_cannot_impersonate(test_database, tmp_path):
    app = app_for(test_database, tmp_path)
    with TestClient(app) as alice:
        sid = new_session(alice)
        real = alice.cookies["dm_client"]
        for forged in ("", "../../etc/passwd", "A" * 32, real.upper(), real + "0", "0" * 32):
            with TestClient(app) as m:
                m.cookies.set("dm_client", forged)
                assert m.get(f"/sessions/{sid}").status_code == 404, forged
                assert m.get("/sessions").json() == []


def test_production_cookies_are_marked_secure(test_database, tmp_path):
    with TestClient(app_for(test_database, tmp_path, app_env="production")) as c:
        assert "Secure" in c.get("/sessions").headers["set-cookie"]


def test_per_conversation_upload_limits(test_database, tmp_path, docx_bytes):
    with TestClient(app_for(test_database, tmp_path, max_files_per_session=2)) as c:
        sid = new_session(c)
        assert upload(c, sid, "a.docx", docx_bytes).status_code == 201
        assert upload(c, sid, "b.docx", docx_bytes).status_code == 201
        r = upload(c, sid, "c.docx", docx_bytes)
        assert r.status_code == 400 and "already has 2 files" in r.json()["error"]["message"]
        assert len(c.get(f"/sessions/{sid}").json()["files"]) == 2
    with TestClient(app_for(test_database, tmp_path, max_session_mb=0)) as c:
        sid = new_session(c)
        r = upload(c, sid, "a.docx", docx_bytes)
        assert r.status_code == 400 and "storage limit" in r.json()["error"]["message"]


def test_global_chat_concurrency_cap_returns_busy_and_does_not_leak(test_database, tmp_path):
    llm = FakeLLM(texts=["ok"])
    app = app_for(test_database, tmp_path, llm=llm, max_concurrent_chats=1)
    with TestClient(app) as c:
        sid = new_session(c)
        token = c.app.state.slots.acquire()            # another request is using the only slot
        r = c.post(f"/sessions/{sid}/chat", json={"message": "Hello"})
        assert r.status_code == 400 and "busy" in r.json()["error"]["message"]
        assert c.app.state.gate.acquire(sid)           # the session gate was released again (no leak) ...
        c.app.state.gate.release(sid)
        c.app.state.slots.release(token)
        assert sse(c.post(f"/sessions/{sid}/chat", json={"message": "Hello"}))[-1][0] == "final"   # ... and it recovers
        assert c.app.state.slots.acquire() is not None  # the slot was returned after the stream finished


# ---------------------------------------------------------------- units
class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_slot_limiter_caps_releases_and_expires_abandoned_holds():
    clk = Clock()
    s = SlotLimiter(2, ttl=100, clock=clk)
    a, b = s.acquire(), s.acquire()
    assert a and b and s.acquire() is None
    s.release(a)
    c = s.acquire()
    assert c is not None and s.acquire() is None
    clk.t = 101                                         # holders that never released (client vanished) expire
    assert s.acquire() is not None and s.acquire() is not None
    s.release(None)                                     # releasing "nothing" is harmless


def test_session_gate_expires_abandoned_holds():
    clk = Clock()
    g = SessionGate(ttl=50, clock=clk)
    assert g.acquire("s") and not g.acquire("s") and g.acquire("other")
    clk.t = 51
    assert g.acquire("s")


def test_lru_cache_is_bounded_and_evicts_least_recently_used():
    c = LRUCache(maxsize=2)
    c["a"], c["b"] = 1, 2
    assert c["a"] == 1          # touch a => b is now the oldest
    c["c"] = 3
    assert "b" not in c and "a" in c and "c" in c and len(c) == 2
    for i in range(100):
        c[f"k{i}"] = i
    assert len(c) == 2
