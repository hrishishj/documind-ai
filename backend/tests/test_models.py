import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    Artifact, ArtifactVersion, ChatSession, Citation, Message, TemplateProfile, UploadedFile,
)
from app.database.session import get_engine


@pytest.fixture
def db():
    """Real Postgres session; everything is rolled back after each test."""
    conn = get_engine().connect()
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


def test_jsonb_columns_are_real_jsonb():
    cols = {c["name"]: str(c["type"]) for c in inspect(get_engine()).get_columns("template_profiles")}
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
    assert db.scalar(select(TemplateProfile.profile)) == payload


def test_cascade_delete_session_removes_children(db):
    cs = ChatSession()
    m = Message(role="user", content="hi")
    m.citations.append(Citation(source_type="web", url="https://example.com"))
    cs.messages.append(m)
    a = Artifact(name="r", kind="pptx")
    a.versions.append(ArtifactVersion(version=1, stored_path="p"))
    cs.artifacts.append(a)
    db.add(cs)
    db.flush()
    db.delete(cs)
    db.flush()
    for model in (Message, Citation, Artifact, ArtifactVersion):
        assert db.scalar(select(func.count()).select_from(model)) == 0


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
