import json
import logging
import re
from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ExternalServiceError, NotFoundError
from app.database.models import Artifact, UploadedFile
from app.document_engine.render import detect_format
from app.schemas.api import (ArtifactOut, ChatIn, FileOut, MessageOut, SessionCreate, SessionDetail, SessionOut, UploadOut)
from app.services import chat_store as store
from app.services.uploads import validate_upload

log = logging.getLogger(__name__)
router = APIRouter()

NODE_LABELS = {
    "route": "Understanding your request", "understand": "Reading the document", "plan": "Planning the edit",
    "validate": "Checking the plan", "render": "Applying changes to a copy of your file", "respond": "Writing the summary",
    "chat": "Thinking", "give_up": "Wrapping up",
}


def db_dep(request: Request) -> Iterator[Session]:
    db = request.app.state.db.session()
    try:
        yield db
    finally:
        db.close()


def _analysis_summary(profile: dict) -> dict:
    out = {"format": profile["format"], "counts": profile.get("counts", {}), "warnings": profile.get("warnings", [])}
    if profile["format"] == "docx":
        out["page"] = {k: profile["sections"][0][k] for k in ("page_width_in", "page_height_in", "orientation", "margins_in")}
        out["fonts"] = profile["formatting_census"]["fonts"][:3]
        out["outline"] = profile["outline"][:12]
    elif profile["format"] == "pptx":
        out["slide_size"] = profile["slide_size"]
        out["layouts"] = [l["name"] for m in profile["masters"] for l in m["layouts"]]
        out["theme_fonts"] = profile["masters"][0]["theme"]["fonts"]
    elif profile["format"] == "xlsx":
        out["sheets"] = profile["sheet_names"][:30]
    return out


# ------------------------------------------------------------------ sessions
@router.post("/sessions", response_model=SessionOut, status_code=201)
def create_session(body: SessionCreate | None = None, db: Session = Depends(db_dep)):
    return store.create_session(db, (body.title if body else "New chat"))


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(db: Session = Depends(db_dep)):
    return store.list_sessions(db)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: str, db: Session = Depends(db_dep)):
    s = store.get_session(db, session_id)
    return SessionDetail(id=s.id, title=s.title, created_at=s.created_at, updated_at=s.updated_at,
                         messages=[MessageOut.model_validate(m) for m in s.messages],
                         files=[FileOut.model_validate(f) for f in s.files],
                         artifacts=[ArtifactOut.model_validate(a) for a in s.artifacts])


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request, db: Session = Depends(db_dep)):
    store.delete_session(db, request.app.state.storage, session_id)


# ------------------------------------------------------------------ files
@router.post("/sessions/{session_id}/files", response_model=UploadOut, status_code=201)
def upload_file(session_id: str, request: Request, file: UploadFile = File(...), db: Session = Depends(db_dep)):
    st, settings = request.app.state, request.app.state.settings
    store.get_session(db, session_id)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)  # never read more than limit+1 bytes into memory
    v = validate_upload(file.filename or "", data, max_bytes)
    path, sha = st.storage.save_upload(session_id, data, v.ext)
    rec = store.record_upload(db, session_id, v, st.storage.rel(path), sha)
    analysis = None
    if v.format in ("docx", "pptx", "xlsx"):
        try:
            profile = st.analyzers[v.format](path)
            store.save_profile(db, rec.id, profile)
            analysis = _analysis_summary(profile)
        except Exception as e:  # noqa: BLE001 - a file that validated but cannot be analysed must not poison the session
            log.exception("analysis failed for %s", rec.id)
            db.delete(rec)
            db.commit()
            path.unlink(missing_ok=True)
            raise AppError(f"the file could not be read as a {v.format.upper()} document: {type(e).__name__}")
    return UploadOut(file=FileOut.model_validate(rec), analysis=analysis)


@router.get("/sessions/{session_id}/files/{file_id}/profile")
def file_profile(session_id: str, file_id: str, db: Session = Depends(db_dep)):
    f = store.get_file(db, session_id, file_id)
    if f.profile is None:
        raise NotFoundError("this file has no template analysis (only DOCX, PPTX and XLSX are analysed)")
    return f.profile.profile


# ------------------------------------------------------------------ chat
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/sessions/{session_id}/chat")
def chat(session_id: str, body: ChatIn, request: Request):
    """Server-sent events: `node` progress events, then one `final` (or `error`)."""
    st = request.app.state
    if st.graph is None:
        raise ExternalServiceError("the language model is not configured (set GROQ_API_KEY)")
    with st.db.session() as db:
        store.get_session(db, session_id)  # 404 before streaming starts
    if not st.gate.acquire(session_id):
        raise AppError("another request for this conversation is still running")
    return StreamingResponse(_chat_events(st, session_id, body.message), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _chat_events(st, session_id: str, message: str) -> Iterator[str]:
    try:
        with st.db.session() as db:
            history = store.recent_history(db, session_id)
            store.add_message(db, session_id, "user", message)
            files = store.file_refs(db, session_id)
        yield _sse("start", {"session_id": session_id})
        state: dict = {}
        cfg = {"configurable": {"thread_id": session_id}}
        for chunk in st.graph.stream({"session_id": session_id, "user_message": message, "history": history, "files": files},
                                     cfg, stream_mode="updates"):
            for node, upd in chunk.items():
                state.update(upd or {})
                yield _sse("node", {"node": node, "label": NODE_LABELS.get(node, node)})
        response = state.get("response") or "Done."
        meta: dict = {}
        art = state.get("artifact")
        with st.db.session() as db:
            if art:
                a, v = store.record_artifact(db, session_id, art, state.get("plan"))
                meta = {"artifact": {"id": a.id, "name": a.name, "format": a.kind, "version": v.version},
                        "render": state.get("render")}
            msg = store.add_message(db, session_id, "assistant", response, meta)
            msg_id = msg.id
        yield _sse("final", {"message_id": msg_id, "content": response, "meta": meta, "intent": state.get("intent")})
    except AppError as e:
        yield _sse("error", {"code": e.code, "message": e.message})
    except Exception:  # noqa: BLE001
        log.exception("chat failed")
        yield _sse("error", {"code": "internal_error", "message": "Something went wrong while processing your message."})
    finally:
        st.gate.release(session_id)


# ------------------------------------------------------------------ downloads
def _download(path, name: str):
    safe = re.sub(r'[^A-Za-z0-9._ ()-]', "_", name) or "download"
    return FileResponse(path, filename=safe)


@router.get("/sessions/{session_id}/artifacts/{artifact_id}/versions/{version}/download")
def download_artifact(session_id: str, artifact_id: str, version: int, request: Request, db: Session = Depends(db_dep)):
    a, v = store.get_artifact_version(db, session_id, artifact_id, version)
    p = request.app.state.storage.resolve(v.stored_path)
    if not p.exists():
        raise NotFoundError("the file is no longer available")
    return _download(p, f"{a.name.rsplit('.', 1)[0]}_v{v.version}.{a.kind}")


@router.get("/sessions/{session_id}/files/{file_id}/download")
def download_upload(session_id: str, file_id: str, request: Request, db: Session = Depends(db_dep)):
    f = store.get_file(db, session_id, file_id)
    p = request.app.state.storage.resolve(f.stored_path)
    if not p.exists():
        raise NotFoundError("the file is no longer available")
    return _download(p, f.original_name)
