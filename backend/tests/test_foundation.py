from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.errors import NotFoundError, register_error_handlers
from app.main import app


def test_health():
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def _error_app() -> TestClient:
    a = FastAPI()
    register_error_handlers(a)

    @a.get("/nf")
    def nf():
        raise NotFoundError("nope")

    @a.get("/boom")
    def boom():
        raise RuntimeError("secret internals")

    return TestClient(a, raise_server_exceptions=False)


def test_app_error_is_structured():
    r = _error_app().get("/nf")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "not_found", "message": "nope"}}


def test_unhandled_error_does_not_leak_internals():
    r = _error_app().get("/boom")
    assert r.status_code == 500
    assert "secret internals" not in r.text


def test_storage_path_is_repo_relative_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = Settings(storage_dir=Path("backend/storage"))
    assert s.storage_path == (REPO_ROOT / "backend" / "storage").resolve()


def test_sqlalchemy_url_uses_psycopg3():
    s = Settings(database_url="postgresql://u:p@h:5432/d")
    assert s.sqlalchemy_url.startswith("postgresql+psycopg://")


def test_postgres_reachable():
    engine = create_engine(get_settings().sqlalchemy_url)
    try:
        with engine.connect() as c:
            assert c.execute(text("select 1")).scalar() == 1
    except Exception as e:  # report clearly instead of a vague failure
        pytest.fail(f"Postgres not reachable with .env DATABASE_URL: {type(e).__name__}")
