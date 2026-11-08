"""LangGraph workflow: route -> understand -> plan -> validate (retry) -> render -> respond.

Only the LLM-facing steps (route, plan, chat) are non-deterministic. Understanding, validation, rendering and
the preservation check are plain code, and the final answer about what changed is generated from facts, not by
the model, so it cannot claim edits that did not happen.
"""
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agents.planner import make_plan
from app.agents.prompts import CHAT_SYSTEM
from app.agents.router import describe_files, route
from app.core.errors import AppError, ExternalServiceError
from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.plan import EditPlan, PlanError
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.render import apply_plan, detect_format, validate_plan
from app.document_engine.planner_view import build_planner_view
from app.document_engine.xlsx.analyzer import analyze_spreadsheet
from app.llm.provider import LLM, StructuredOutputError
from app.services.storage import LocalStorage
from app.workflows.state import ChatState

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
_ANALYZERS = {"docx": analyze_docx, "pptx": analyze_pptx, "xlsx": analyze_spreadsheet}
_OUT_EXT = {"docx": "docx", "pptx": "pptx", "xlsx": "xlsx"}


@dataclass
class Deps:
    llm: LLM
    storage: LocalStorage
    profiles: dict[str, dict] = field(default_factory=dict)  # sha256 of file -> analysis profile (cache)


def _file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_graph(deps: Deps, checkpointer=None):
    # ------------------------------------------------------------------ nodes
    def route_node(state: ChatState) -> dict:
        try:
            decision, target = route(deps.llm, state["user_message"], state.get("history", []), state.get("files", []))
        except ExternalServiceError as e:
            return {"intent": "error", "error": e.message, "plan": None, "artifact": None, "render": None, "plan_errors": [],
                    "declined": False, "response": "I couldn't reach the language model just now. Please try again in a moment."}
        # Reset EVERYTHING per-turn: the checkpointer keeps the previous turn's state under the same thread id, and a
        # stale `response`/`plan` from the last message would otherwise be mistaken for this turn's.
        update = {"intent": decision.intent, "target": target, "route_reason": decision.reason, "attempts": 0,
                  "plan_errors": [], "plan": None, "artifact": None, "render": None, "error": None, "citations": [],
                  "response": "", "declined": False, "profile_summary": ""}
        if decision.intent == "clarify":
            update["response"] = decision.clarification or "Could you tell me which document you mean?"
        return update

    def _profile_for(ref) -> dict:
        path = deps.storage.resolve(ref["path"])
        key = _file_hash(path)
        if key not in deps.profiles:
            deps.profiles[key] = _ANALYZERS[detect_format(path)](path)
        return deps.profiles[key]

    def chat_node(state: ChatState) -> dict:
        files = state.get("files", [])
        editable = [f for f in files if f["format"] in ("docx", "pptx", "xlsx")]
        context = ""
        if editable:  # let the model see the document it is being asked about (latest artifact, else the last upload)
            ref = ([f for f in editable if f.get("kind") == "artifact"] or editable)[-1]
            try:
                view = build_planner_view(_profile_for(ref), state["user_message"], budget_chars=3500)
                context = f"Document outline of {ref['name']!r}:\n{view}"
            except Exception:  # noqa: BLE001 - an unreadable file must not break plain conversation
                log.exception("could not build chat context for %s", ref.get("name"))
        try:
            text = deps.llm.text(CHAT_SYSTEM.format(files=describe_files(files), context=context or "(no document outline available)"),
                                 state["user_message"], state.get("history", [])[-10:])
        except ExternalServiceError as e:
            return {"error": e.message, "response": "I couldn't reach the language model just now. Please try again."}
        return {"response": text}

    def understand_node(state: ChatState) -> dict:
        target = state["target"]
        path = deps.storage.resolve(target["path"])
        key = _file_hash(path)
        if key not in deps.profiles:  # analysis is cached by content hash; the view depends on the request
            deps.profiles[key] = _ANALYZERS[detect_format(path)](path)
        return {"profile_summary": build_planner_view(deps.profiles[key], state["user_message"])}

    def plan_node(state: ChatState) -> dict:
        target = state["target"]
        n = state.get("attempts", 0) + 1
        try:
            plan = make_plan(deps.llm, detect_format(target["path"]), state["profile_summary"], state["user_message"],
                             state.get("history", []), state.get("plan_errors") or None)
        except ValidationError as e:  # the model produced something the strict schema rejects
            msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:6]]
            return {"attempts": n, "plan": None, "plan_errors": ["schema violation: " + "; ".join(msgs)]}
        except StructuredOutputError as e:  # unparseable output: retry with the reason, don't abort
            return {"attempts": n, "plan": None, "plan_errors": ["schema violation: " + e.message]}
        except ExternalServiceError as e:
            return {"attempts": MAX_ATTEMPTS, "plan": None, "error": e.message,
                    "plan_errors": [e.message]}
        return {"attempts": n, "plan": plan.model_dump(mode="json"), "plan_errors": []}

    def validate_node(state: ChatState) -> dict:
        if state.get("error"):
            return {}
        if state.get("plan_errors"):  # schema-level rejection already recorded by plan_node
            return {}
        plan = EditPlan.model_validate(state["plan"])
        if not plan.ops:  # the model declined / needs more info: relay its explanation, do not render
            return {"plan_errors": [], "declined": True,
                    "response": plan.summary or "I couldn't find a way to make that change."}
        errs = validate_plan(deps.storage.resolve(state["target"]["path"]), plan)
        return {"plan_errors": errs}

    def render_node(state: ChatState) -> dict:
        target = state["target"]
        plan = EditPlan.model_validate(state["plan"])
        src = deps.storage.resolve(target["path"])
        fmt = detect_format(src)
        is_artifact = target.get("kind") == "artifact"
        art_id = target["id"] if is_artifact else uuid.uuid4().hex
        version = (target.get("version") or 1) + 1 if is_artifact else 1
        dst = deps.storage.artifact_path(state["session_id"], art_id, version, _OUT_EXT[fmt])
        try:
            out = apply_plan(src, plan, dst)
        except PlanError as e:  # should be rare: validation already passed
            return {"plan_errors": e.errors}
        base = Path(target["name"]).stem
        name = base if is_artifact else f"{base} (edited)"
        return {"artifact": {"id": art_id, "name": f"{name}.{_OUT_EXT[fmt]}", "format": fmt, "version": version,
                             "path": deps.storage.rel(dst), "from_file": target["id"]},
                "render": {"applied": out.result.applied, "warnings": out.result.warnings,
                           "preserved": out.preserved, "violations": out.violations}}

    def respond_node(state: ChatState) -> dict:
        art, rep, plan = state["artifact"], state["render"], state["plan"]
        lines = [f"Done. I created **{art['name']}** (version {art['version']}) from *{state['target']['name']}*."]
        if plan.get("summary"):
            lines.append(plan["summary"])
        lines.append(f"{rep['applied']} change(s) applied.")
        lines.append("The original layout and formatting were preserved." if rep["preserved"] else
                     "**Warning:** my check found differences from the original design: " + "; ".join(rep["violations"]))
        if rep["warnings"]:
            lines.append("Notes: " + " ".join(rep["warnings"]))
        return {"response": "\n\n".join(lines)}

    def give_up_node(state: ChatState) -> dict:
        errs = state.get("plan_errors") or ["unknown problem"]
        if state.get("error"):
            return {"response": "I couldn't reach the language model just now. Please try again in a moment."}
        return {"response": "I wasn't able to produce a valid change for that request after "
                            f"{state.get('attempts', 0)} attempts. Last problem: {errs[0]}. "
                            "Could you rephrase it or point me at the exact slide, table or sheet?"}

    # ------------------------------------------------------------------ routing
    def after_route(state: ChatState) -> str:
        return {"edit": "understand", "chat": "chat"}.get(state.get("intent", ""), END)

    def after_validate(state: ChatState) -> str:
        if state.get("declined"):
            return END  # model declined / explained instead of planning
        if not state.get("plan_errors"):
            return "render"
        if state.get("error") or state.get("attempts", 0) >= MAX_ATTEMPTS:
            return "give_up"
        return "plan"

    def after_render(state: ChatState) -> str:
        return "plan" if state.get("plan_errors") and state.get("attempts", 0) < MAX_ATTEMPTS else (
            "give_up" if state.get("plan_errors") else "respond")

    g = StateGraph(ChatState)
    for name, fn in (("route", route_node), ("chat", chat_node), ("understand", understand_node), ("plan", plan_node),
                     ("validate", validate_node), ("render", render_node), ("respond", respond_node), ("give_up", give_up_node)):
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, {"understand": "understand", "chat": "chat", END: END})
    g.add_edge("chat", END)
    g.add_edge("understand", "plan")
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", after_validate, {"render": "render", "plan": "plan", "give_up": "give_up", END: END})
    g.add_conditional_edges("render", after_render, {"respond": "respond", "plan": "plan", "give_up": "give_up"})
    g.add_edge("respond", END)
    g.add_edge("give_up", END)
    return g.compile(checkpointer=checkpointer)
