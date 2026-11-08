"""Database operations for sessions, messages, uploads and generated artifacts."""
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.database.models import Artifact, ArtifactVersion, ChatSession, Message, TemplateProfile, UploadedFile
from app.services.storage import LocalStorage
from app.services.uploads import ValidatedUpload
from app.workflows.state import FileRef


def create_session(db: Session, title: str = "New chat") -> ChatSession:
    s = ChatSession(title=title[:200])
    db.add(s)
    db.commit()
    return s


def get_session(db: Session, session_id: str) -> ChatSession:
    s = db.get(ChatSession, session_id)
    if s is None:
        raise NotFoundError("session not found")
    return s


def list_sessions(db: Session, limit: int = 100) -> list[ChatSession]:
    return list(db.scalars(select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)))


def delete_session(db: Session, storage: LocalStorage, session_id: str) -> None:
    s = get_session(db, session_id)
    db.delete(s)  # cascades to messages, files, profiles, artifacts, versions, citations
    db.commit()
    shutil.rmtree(storage.root / session_id, ignore_errors=True)


def add_message(db: Session, session_id: str, role: str, content: str, meta: dict | None = None) -> Message:
    m = Message(session_id=session_id, role=role, content=content, meta=meta or {})
    db.add(m)
    s = db.get(ChatSession, session_id)
    if s is not None and s.title == "New chat" and role == "user":
        s.title = " ".join(content.split())[:60] or "New chat"
    db.commit()
    return m


def recent_history(db: Session, session_id: str, n: int = 10) -> list[tuple[str, str]]:
    rows = db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.created_at.desc()).limit(n))
    return [("human" if m.role == "user" else "ai", m.content) for m in reversed(list(rows))]


def record_upload(db: Session, session_id: str, v: ValidatedUpload, stored_rel: str, sha: str) -> UploadedFile:
    f = UploadedFile(session_id=session_id, original_name=v.display_name, stored_path=stored_rel, kind=v.format,
                     mime_type=v.mime, size_bytes=v.size, sha256=sha)
    db.add(f)
    db.commit()
    return f


def save_profile(db: Session, file_id: str, profile: dict) -> None:
    db.add(TemplateProfile(file_id=file_id, profile=profile))
    db.commit()


def get_file(db: Session, session_id: str, file_id: str) -> UploadedFile:
    f = db.get(UploadedFile, file_id)
    if f is None or f.session_id != session_id:  # never reveal another session's file
        raise NotFoundError("file not found")
    return f


def file_refs(db: Session, session_id: str) -> list[FileRef]:
    """Everything the workflow may refer to: uploads, plus the LATEST version of each generated artifact."""
    refs: list[FileRef] = [
        FileRef(id=f.id, name=f.original_name, path=f.stored_path, format=f.kind, kind="upload")
        for f in db.scalars(select(UploadedFile).where(UploadedFile.session_id == session_id).order_by(UploadedFile.created_at))]
    arts = db.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at))
    for a in arts:
        v = a.versions[-1] if a.versions else None
        if v is not None:
            refs.append(FileRef(id=a.id, name=a.name, path=v.stored_path, format=a.kind, kind="artifact", version=v.version))
    return refs


def record_artifact(db: Session, session_id: str, art: dict, plan: dict | None) -> tuple[Artifact, ArtifactVersion]:
    """Create the artifact on first generation, or append a version to an existing one."""
    a = db.get(Artifact, art["id"])
    if a is None:
        a = Artifact(id=art["id"], session_id=session_id, name=art["name"], kind=art["format"])
        db.add(a)
        db.flush()
    src = art.get("from_file")
    src_is_upload = db.get(UploadedFile, src) is not None if src else False
    v = ArtifactVersion(artifact_id=a.id, version=art["version"], stored_path=art["path"], edit_plan=plan or {},
                        source_file_id=src if src_is_upload else None, summary=(plan or {}).get("summary", ""))
    db.add(v)
    db.commit()
    return a, v


def get_artifact_version(db: Session, session_id: str, artifact_id: str, version: int) -> tuple[Artifact, ArtifactVersion]:
    a = db.get(Artifact, artifact_id)
    if a is None or a.session_id != session_id:
        raise NotFoundError("artifact not found")
    v = next((x for x in a.versions if x.version == version), None)
    if v is None:
        raise NotFoundError(f"version {version} not found")
    return a, v
