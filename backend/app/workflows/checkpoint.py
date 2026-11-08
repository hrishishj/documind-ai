"""PostgreSQL-backed LangGraph checkpointer: per-session workflow state that survives restarts."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import get_settings


def make_pool(max_size: int = 5) -> ConnectionPool:
    # autocommit + dict_row + no prepared statements are what PostgresSaver requires
    return ConnectionPool(get_settings().database_url, min_size=1, max_size=max_size, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})


def make_checkpointer(pool: ConnectionPool | None = None):
    """Create the saver and its tables (idempotent). Caller owns the pool and closes it on shutdown."""
    from langgraph.checkpoint.postgres import PostgresSaver

    saver = PostgresSaver(pool or make_pool())
    saver.setup()
    return saver
