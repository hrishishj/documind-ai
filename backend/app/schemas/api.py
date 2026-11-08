from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SessionOut(_Out):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    title: str = Field(default="New chat", max_length=200)


class FileOut(_Out):
    id: str
    original_name: str
    kind: str
    size_bytes: int
    index_status: str
    chunk_count: int
    index_error: str
    created_at: datetime


class CitationOut(_Out):
    ref: int
    source_type: str
    title: str          # file name
    locator: str | None
    snippet: str
    file_id: str | None
    url: str | None


class VersionOut(_Out):
    version: int
    summary: str
    created_at: datetime


class ArtifactOut(_Out):
    id: str
    name: str
    kind: str
    versions: list[VersionOut]


class MessageOut(_Out):
    id: str
    role: str
    content: str
    meta: dict
    citations: list[CitationOut] = []
    created_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]
    files: list[FileOut]
    artifacts: list[ArtifactOut]


class UploadOut(BaseModel):
    file: FileOut
    analysis: dict | None = None  # compact summary of what the analyzer found (None for pdf/txt)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
