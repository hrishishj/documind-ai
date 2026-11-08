"""Regenerate every file in sample_templates/ (our own demo templates) and sample_outputs/.

Fully deterministic: no LLM, no network. Each edit plan below is exactly the kind of JSON the planner
agent produces; applying it goes through the same `render.apply_plan` path the chatbot uses, including
the preservation check. Run:  python -X utf8 -I backend/tools/make_samples.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import TypeAdapter  # noqa: E402

from app.document_engine import samples  # noqa: E402
from app.document_engine.plan import plan_model_for  # noqa: E402
from app.document_engine.planner_view import build_planner_view  # noqa: E402
from app.document_engine.render import _ANALYZE, apply_plan  # noqa: E402

CASES = {
    "docx": ("acme_quarterly_report.docx", samples.build_docx, {
        "format": "docx", "summary": "Retitle, add an intro sentence, fix a cell, add a table row",
        "ops": [
            {"op": "docx.replace_text", "paragraph": 0, "text": "Quarterly Report - Q3 FY2024"},
            {"op": "docx.insert_paragraph", "after": 1, "like": 1, "text": "Revenue grew in every region this quarter."},
            {"op": "docx.set_cell", "table": 0, "row": 1, "col": 1, "text": "12"},
            {"op": "docx.add_row", "table": 0, "values": ["South", "15", "25"], "like_row": 1, "after_row": 1},
        ]}),
    "pptx": ("acme_product_deck.pptx", samples.build_pptx, {
        "format": "pptx", "summary": "Update the title slide, add a Next-steps slide, duplicate Highlights",
        "ops": [
            {"op": "pptx.set_text", "slide": 1, "shape": "Title 1", "text": "Annual Review FY2025"},
            {"op": "pptx.replace_all", "find": "FY2024", "replace": "FY2025"},
            {"op": "pptx.add_slide", "layout": "Title and Content",
             "texts": {"0": "Next steps", "1": "Launch in Q2"}},
            {"op": "pptx.duplicate_slide", "slide": 2},
        ]}),
    "xlsx": ("acme_budget.xlsx", samples.build_xlsx, {
        "format": "xlsx", "summary": "Add a West row (style copied), correct North revenue, copy the sheet for Q2",
        "ops": [
            {"op": "xlsx.append_row", "sheet": "Report", "values": ["West", 410.75, 0.21, "2024-04-02"]},
            {"op": "xlsx.set_cell", "sheet": "Report", "cell": "B4", "value": 1500},
            {"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "Report Q2"},
        ]}),
}


def main() -> int:
    failures = 0
    for fmt, (name, build, plan_json) in CASES.items():
        tpl = ROOT / "sample_templates" / fmt / name
        out_dir = ROOT / "sample_outputs" / fmt
        tpl.parent.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        build(tpl)
        stem = tpl.stem
        (out_dir / f"{stem}.analysis.txt").write_text(build_planner_view(_ANALYZE[fmt](tpl)) + "\n", encoding="utf-8")
        plan = TypeAdapter(plan_model_for(fmt)).validate_python(plan_json)
        (out_dir / f"{stem}.plan.json").write_text(json.dumps(plan_json, indent=2) + "\n", encoding="utf-8")
        dst = out_dir / f"{stem}.edited{tpl.suffix}"
        outcome = apply_plan(tpl, plan, dst)
        ok = outcome.preserved and not outcome.violations
        failures += not ok
        (out_dir / f"{stem}.edited.analysis.txt").write_text(
            build_planner_view(_ANALYZE[fmt](dst)) + "\n", encoding="utf-8")
        print(f"{fmt}: {name} -> {dst.name}  preserved={outcome.preserved} violations={outcome.violations}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
