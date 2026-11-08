import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    Artifact, ArtifactVersion, ChatSession, Citation, Message, TemplateProfile, UploadedFile,
)


@pytest.fixture
def db(test_database):
    """Session on the ISOLATED test database (never the dev one: these tests assert global row counts, which a running
    app would otherwise break); everything is rolled back after each test."""
    conn = test_database.engine.connect()
    trans = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint")
    yield s
    s.close()
    trans.rollback()
    conn.close()


def _file(session_id):
    return UploadedFile(
        session_id=session_id, original_name="a.docx", stored_path="x/y.docx",
        kind="docx", mime_type="application/x", size_bytes=1, sha256="0" * 64,
    )


def test_jsonb_columns_are_real_jsonb(test_database):
    cols = {c["name"]: str(c["type"]) for c in inspect(test_database.engine).get_columns("template_profiles")}
    assert cols["profile"] == "JSONB"


def test_profile_json_round_trip(db):
    cs = ChatSession()
    f = _file(None)
    cs.files.append(f)
    db.add(cs)
    db.flush()
    payload = {"fonts": ["Calibri"], "margins": {"top": 1.0}, "nested": [{"a": 1}]}
    db.add(TemplateProfile(file_id=f.id, profile=payload))
    db.flush()
    db.expire_all()
    # scoped to THIS file: other tests commit rows to the same database, so "the first row" would be arbitrary
    assert db.scalar(select(TemplateProfile.profile).where(TemplateProfile.file_id == f.id)) == payload


def test_cascade_delete_session_removes_children(db):
    cs = ChatSession(owner="o")
    m = Message(role="user", content="hi")
    m.citations.append(Citation(source_type="web", url="https://example.com"))
    cs.messages.append(m)
    a = Artifact(name="r", kind="pptx")
    a.versions.append(ArtifactVersion(version=1, stored_path="p"))
    cs.artifacts.append(a)
    db.add(cs)
    db.flush()
    mine = {Message: m.id, Citation: m.citations[0].id, Artifact: a.id, ArtifactVersion: a.versions[0].id}
    count = lambda model: db.scalar(select(func.count()).select_from(model).where(model.id == mine[model]))  # noqa: E731
    assert all(count(model) == 1 for model in mine)   # they exist first, so the zero below is meaningful
    db.delete(cs)
    db.flush()
    assert all(count(model) == 0 for model in mine)   # and the cascade removed exactly those rows


def test_duplicate_artifact_version_rejected(db):
    cs = ChatSession()
    a = Artifact(name="r", kind="docx")
    a.versions.append(ArtifactVersion(version=1, stored_path="p"))
    cs.artifacts.append(a)
    db.add(cs)
    db.flush()
    db.add(ArtifactVersion(artifact_id=a.id, version=1, stored_path="q"))
    with pytest.raises(IntegrityError):
        db.flush()
