import pytest
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.database.models import Base
from app.database.session import Database

TEST_DB = "documind_ai_test"


@pytest.fixture(scope="session")
def test_database():
    """An isolated database on the same server (created on demand, schema built from the models)."""
    url = make_url(get_settings().sqlalchemy_url)
    try:
        admin = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password,
                                dbname="postgres", autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    with admin:
        if not admin.execute("select 1 from pg_database where datname=%s", (TEST_DB,)).fetchone():
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(TEST_DB)))
    db = Database(url.set(database=TEST_DB).render_as_string(hide_password=False))
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()
