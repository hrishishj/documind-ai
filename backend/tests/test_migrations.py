"""Schema/migration hygiene: models and migrations must agree, and Alembic must never touch tables it doesn't own."""
import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError

BACKEND = Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def test_models_and_migrations_agree_on_the_real_database():
    """Fails when someone changes a model without writing a migration (run `alembic upgrade head` first)."""
    from sqlalchemy import text

    from app.database.session import get_engine
    try:
        with get_engine().connect() as c:
            c.execute(text("select 1"))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable: {type(e).__name__}")
    try:
        command.check(_cfg())
    except CommandError as e:
        pytest.fail(f"schema drift: {str(e)[:400]}")


def test_alembic_ignores_foreign_tables_like_the_langgraph_checkpointer():
    spec = importlib.util.spec_from_file_location("env_probe", BACKEND / "alembic" / "env.py")
    src = (BACKEND / "alembic" / "env.py").read_text()
    ns: dict = {}
    # exec only the filter function (importing env.py would run migrations)
    start = src.index("def include_object")
    end = src.index("\n\n\n", start) if "\n\n\n" in src[start:] else len(src)
    exec(src[start:start + (src[start:].index("return True") + len("return True"))], ns)
    include = ns["include_object"]
    # a reflected table with no counterpart in our models => must be ignored (never dropped)
    assert include(object(), "checkpoints", "table", True, None) is False
    assert include(object(), "checkpoint_writes", "table", True, None) is False
    # our own tables and their columns/indexes are still managed
    assert include(object(), "chat_sessions", "table", True, object()) is True
    assert include(object(), "owner", "column", True, None) is True
    assert spec is not None and end > start


def test_no_migration_drops_the_checkpointer_tables():
    for f in (BACKEND / "alembic" / "versions").glob("*.py"):
        assert "checkpoint" not in f.read_text().lower(), f"{f.name} touches the LangGraph checkpointer tables"
