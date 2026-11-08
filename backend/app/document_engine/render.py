"""Single entry point used by the workflow layer: validate -> render -> re-analyze -> preservation check."""
from pathlib import Path

from pydantic import BaseModel, Field

from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.docx.renderer import render_docx, validate_docx_plan
from app.document_engine.plan import EditPlan, PlanError, RenderResult
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.pptx.renderer import render_pptx, validate_pptx_plan
from app.document_engine.verify import check_preservation
from app.document_engine.xlsx.analyzer import analyze_spreadsheet
from app.document_engine.xlsx.renderer import render_xlsx, validate_xlsx_plan

_ANALYZE = {"docx": analyze_docx, "pptx": analyze_pptx, "xlsx": analyze_spreadsheet}
_RENDER = {"docx": render_docx, "pptx": render_pptx, "xlsx": render_xlsx}
_VALIDATE = {"docx": validate_docx_plan, "pptx": validate_pptx_plan, "xlsx": validate_xlsx_plan}
_EXT = {".docx": "docx", ".pptx": "pptx", ".potx": "pptx", ".xlsx": "xlsx", ".xls": "xlsx"}


class ApplyOutcome(BaseModel):
    result: RenderResult
    preserved: bool
    violations: list[str] = Field(default_factory=list)


def detect_format(path: str | Path) -> str:
    try:
        return _EXT[Path(path).suffix.lower()]
    except KeyError:
        raise PlanError([f"unsupported file type '{Path(path).suffix}'; supported: {sorted(_EXT)}"])


def validate_plan(src: str | Path, plan: EditPlan) -> list[str]:
    """Errors phrased for the LLM to fix its plan; empty list = plan is applicable."""
    fmt = detect_format(src)
    if plan.format != fmt:
        return [f"plan format '{plan.format}' does not match the source file ({fmt})"]
    return _VALIDATE[fmt](src, plan)


def apply_plan(src: str | Path, plan: EditPlan, dst: str | Path) -> ApplyOutcome:
    """Render the plan into a NEW file and verify the template's design survived."""
    fmt = detect_format(src)
    if plan.format != fmt:
        raise PlanError([f"plan format '{plan.format}' does not match the source file ({fmt})"])
    before = _ANALYZE[fmt](src)
    result = _RENDER[fmt](src, plan, dst)
    report = check_preservation(before, _ANALYZE[fmt](dst))
    return ApplyOutcome(result=result, preserved=report["ok"], violations=report["violations"])
