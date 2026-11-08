import uuid

import pytest

from app.workflows.checkpoint import make_checkpointer, make_pool
from app.workflows.graph import Deps, build_graph
from tests.test_workflow_graph import FakeLLM, docx_plan, GOOD, upload  # reuse the scripted fake + helpers
from app.document_engine.samples import build_docx
from app.services.storage import LocalStorage


@pytest.fixture()
def pool():
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout
    p = make_pool(max_size=3)  # programming errors must fail loudly, never be reported as "skipped"
    try:
        p.wait(timeout=5)
    except (OperationalError, PoolTimeout) as e:  # pragma: no cover - only "server unreachable" skips
        p.close()
        pytest.skip(f"Postgres not available: {type(e).__name__}")
    yield p
    p.close()


def test_state_survives_a_simulated_restart(pool, tmp_path):
    storage = LocalStorage(tmp_path / "store")
    f = upload(storage, build_docx, "docx", "report.docx")
    thread = f"test-{uuid.uuid4().hex}"
    cfg = {"configurable": {"thread_id": thread}}

    saver = make_checkpointer(pool)
    g1 = build_graph(Deps(FakeLLM(plans=[docx_plan(GOOD)]), storage), checkpointer=saver)
    out = g1.invoke({"session_id": "s1", "user_message": "Change the title", "history": [], "files": [f]}, cfg)
    assert out["artifact"]["version"] == 1

    # "restart": brand-new saver and graph objects, same database, same thread id
    g2 = build_graph(Deps(FakeLLM(), storage), checkpointer=make_checkpointer(pool))
    restored = g2.get_state(cfg).values
    assert restored["artifact"]["id"] == out["artifact"]["id"] and restored["response"] == out["response"]
    assert restored["plan"]["ops"][0]["op"] == "docx.replace_text"

    saver.delete_thread(thread)
    assert g2.get_state(cfg).values == {}  # cleaned up: tests leave nothing behind
