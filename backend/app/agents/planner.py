from app.agents.prompts import PLANNER_SYSTEM, RETRY_NOTE, SOURCES_BLOCK
from app.document_engine.plan import EditPlan, plan_model_for
from app.llm.provider import LLM


def make_plan(llm: LLM, fmt: str, outline: str, instruction: str, history: list[tuple[str, str]],
              errors: list[str] | None = None, sources: str = "") -> EditPlan:
    """Ask the LLM for an edit plan. `errors` (from the validator) are fed back so it can self-correct."""
    system = PLANNER_SYSTEM.format(fmt=fmt, summary=outline,
                                   sources=SOURCES_BLOCK.format(sources=sources) if sources else "")
    user = instruction
    if errors:
        user += "\n\n" + RETRY_NOTE.format(errors="\n".join(f"- {e}" for e in errors))
    out = llm.structured(plan_model_for(fmt), system, user, history[-6:])
    return EditPlan.model_validate(out.model_dump())  # re-validated with the strict combined schema
