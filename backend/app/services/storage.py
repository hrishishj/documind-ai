"""Session-scoped file storage. Names on disk are generated; user-supplied names never reach the filesystem."""
import hashlib
import re
import uuid
from pathlib import Path

from app.core.errors import UnsafeFileError

SAFE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- path safety
    def _dir(self, session_id: str, *parts: str) -> Path:
        for p in (session_id, *parts):
            if not SAFE_ID.match(p):
                raise UnsafeFileError(f"invalid path component {p!r}")
        d = (self.root / session_id / Path(*parts)) if parts else (self.root / session_id)
        d = d.resolve()
        if self.root not in d.parents and d != self.root:  # defence in depth against traversal
            raise UnsafeFileError("path escapes storage root")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolve(self, stored_path: str) -> Path:
        """Turn a stored (relative) path back into an absolute one, refusing anything outside the root."""
        p = (self.root / stored_path).resolve()
        if self.root not in p.parents:
            raise UnsafeFileError("path escapes storage root")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    # ---- uploads
    def save_upload(self, session_id: str, data: bytes, ext: str) -> tuple[Path, str]:
        ext = ext.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
            raise UnsafeFileError("bad extension")
        path = self._dir(session_id, "uploads") / f"{uuid.uuid4().hex}.{ext}"
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    # ---- artifact versions
    def artifact_path(self, session_id: str, artifact_id: str, version: int, ext: str) -> Path:
        ext = ext.lower().lstrip(".")
        if ext not in {"docx", "pptx", "xlsx"}:
            raise UnsafeFileError(f"cannot store artifact of type {ext!r}")
        return self._dir(session_id, "artifacts", artifact_id) / f"v{int(version)}.{ext}"
