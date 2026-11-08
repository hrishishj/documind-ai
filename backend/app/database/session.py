from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


class Database:
    """Engine + session factory for one database URL (the app owns one; tests can make their own)."""

    def __init__(self, url: str):
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._factory()

    def dispose(self) -> None:
        self.engine.dispose()


@lru_cache
def get_database() -> Database:
    return Database(get_settings().sqlalchemy_url)


def get_engine() -> Engine:  # kept for existing callers/tests
    return get_database().engine


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session (closed after the request)."""
    db = get_database().session()
    try:
        yield db
    finally:
        db.close()
