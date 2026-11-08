import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import ROUTER_SYSTEM
from app.llm.provider import LLM
from app.workflows.state import FileRef

EDITABLE = {"docx", "pptx", "xlsx"}


class RouteDecision(BaseModel):
    intent: Literal["chat", "edit", "clarify"]
    target_file_id: str | None = Field(default=None, description="id of the file to edit (edit only)")
    clarification: str | None = Field(default=None, description="question for the user (clarify only)")
    reason: str = ""


def describe_files(files: list[FileRef]) -> str:
    if not files:
        return "(none)"
    return "\n".join(f"- id={f['id']} name={f['name']!r} format={f['format']} kind={f.get('kind', 'upload')}"
                     + (f" version={f['version']}" if f.get("version") else "") for f in files)


EDIT_VERB = re.compile(
    r"\b(change|edit|update|replace|rewrite|rename|add|insert|append|remove|delete|drop|duplicate|copy|move|reorder|"
    r"fill|translate|shorten|lengthen|reword|fix|correct|set|swap|merge|split|create|generate|make|turn|convert|apply|"
    r"re-?title|re-?phrase|re-?write|re-?format|re-?order|re-?arrange|re-?style|revise|expand|condense|tweak|polish|proofread|"
    r"edit|modify|adjust|amend|extend|trim|cut|bold|highlight|insert|put|include|append|"
    r"should (be|say|read|show)|instead of|needs? to (be|say|read)|must (be|say|read)|ought to be)\b", re.I)
QUESTION = re.compile(r"^\s*(what|why|how|when|where|who|which|can you explain|could you explain|explain|summari[sz]e|describe|"
                      r"tell me|list|show me|is |are |does |do )", re.I)


def quick_route(message: str, files: list[FileRef]) -> tuple[RouteDecision, FileRef | None] | None:
    """Cheap, deterministic routing for clear-cut messages; None = ask the LLM (ambiguous).

    Saves a model call (and ~1-2k of Groq's 8k tokens/minute) on most turns.
    """
    wants_edit = bool(EDIT_VERB.search(message))
    if not wants_edit:
        return RouteDecision(intent="chat", reason="no edit verb"), None
    if QUESTION.search(message) and "?" in message and not re.search(r"\b(can|could|would) you\b", message, re.I):
        return None  # "how do I change X?" vs "change X": let the model decide
    editable = [f for f in files if f["format"] in EDITABLE]
    if not editable:
        return RouteDecision(intent="clarify", reason="no editable file",
                             clarification="Please attach a DOCX, PPTX or XLSX file for me to work on."), None
    named = [f for f in editable if Path(f["name"]).stem.lower() in message.lower()]
    if len(named) == 1:
        return RouteDecision(intent="edit", target_file_id=named[0]["id"], reason="file named in message"), named[0]
    artifacts = [f for f in editable if f.get("kind") == "artifact"]
    if artifacts:  # follow-up edits continue the most recent generated document
        t = artifacts[-1]
        return RouteDecision(intent="edit", target_file_id=t["id"], reason="latest artifact"), t
    if len(editable) == 1:
        return RouteDecision(intent="edit", target_file_id=editable[0]["id"], reason="only editable file"), editable[0]
    return None


def route(llm: LLM, message: str, history: list[tuple[str, str]], files: list[FileRef]) -> tuple[RouteDecision, FileRef | None]:
    """Returns the decision plus the validated target (the model's file id is checked, never trusted)."""
    quick = quick_route(message, files)
    if quick is not None:
        return quick
    d = llm.structured(RouteDecision, ROUTER_SYSTEM.format(files=describe_files(files)), message, history[-6:], max_tokens=200)
    if d.intent != "edit":
        return d, None
    by_id = {f["id"]: f for f in files}
    target = by_id.get(d.target_file_id or "")
    if target is None or target["format"] not in EDITABLE:
        editable = [f for f in files if f["format"] in EDITABLE]
        if len(editable) == 1:  # unambiguous fallback instead of bouncing the user
            return d.model_copy(update={"target_file_id": editable[0]["id"]}), editable[0]
        names = ", ".join(f["name"] for f in editable) or "no editable files"
        return RouteDecision(intent="clarify", reason="invalid or missing target",
                             clarification=f"Which document should I change? Available: {names}."
                             if editable else "Please attach a DOCX, PPTX or XLSX file for me to work on."), None
    return d, target
