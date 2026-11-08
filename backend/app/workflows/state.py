"""Shared state for the chat workflow (a plain TypedDict so LangGraph can checkpoint it)."""
from typing import TypedDict


class FileRef(TypedDict, total=False):
    id: str            # upload id or artifact id
    name: str          # display name
    path: str          # path relative to storage root
    format: str        # docx | pptx | xlsx | pdf | txt ...
    kind: str          # "upload" | "artifact"
    version: int       # artifacts only
    index_status: str  # uploads: pending | indexing | ready | failed | skipped


class ChatState(TypedDict, total=False):
    # inputs
    session_id: str
    user_message: str
    history: list[tuple[str, str]]      # [("human"|"ai", text)] most recent last
    files: list[FileRef]                # everything the user can refer to in this session
    # routing
    intent: str                         # chat | edit | clarify
    target: FileRef | None
    route_reason: str
    # web research
    needs_web: bool
    web_sources: list[dict]             # [{title, url, domain, text, score}] from the search engine
    web_note: str                       # why web research was unavailable, if it was requested but failed
    sources_ops: int                    # how many plan operations were appended automatically for the Sources section
    # editing loop
    profile_summary: str
    plan: dict | None
    plan_errors: list[str]
    attempts: int
    declined: bool                      # the model returned no operations and explained why (response holds the text)
    # outputs
    artifact: dict | None               # {id, name, format, version, path, from_file}
    render: dict | None                 # {applied, warnings, preserved, violations}
    response: str
    citations: list[dict]
    error: str | None
