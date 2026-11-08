"""End-to-end: apply_plan on every format, plus mutation tests proving the preservation check can fail."""
import copy
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Inches

from app.core.config import REPO_ROOT
from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.plan import EditPlan, PlanError
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.render import apply_plan, detect_format, validate_plan
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.document_engine.verify import check_preservation
from app.document_engine.xlsx.analyzer import analyze_xlsx


def P(fmt, *ops, **kw):
    return EditPlan.model_validate({"format": fmt, "ops": list(ops), **kw})


@pytest.fixture()
def docx_src(tmp_path):
    p = tmp_path / "a.docx"; build_docx(p); return p


@pytest.fixture()
def pptx_src(tmp_path):
    p = tmp_path / "a.pptx"; build_pptx(p); return p


@pytest.fixture()
def xlsx_src(tmp_path):
    p = tmp_path / "a.xlsx"; build_xlsx(p); return p


def test_docx_end_to_end_preserved(docx_src, tmp_path):
    out = apply_plan(docx_src, P("docx", {"op": "docx.replace_text", "paragraph": 1, "text": "New"},
                                 {"op": "docx.add_row", "table": 0, "values": ["W", "1", "2"], "like_row": 1},
                                 {"op": "docx.replace_all", "find": "ACME", "replace": "Globex"}), tmp_path / "o.docx")
    assert out.preserved, out.violations
    assert out.result.applied == 3  # (previously folded into the assert message, so it never ran)


def test_pptx_end_to_end_preserved(pptx_src, tmp_path):
    out = apply_plan(pptx_src, P("pptx", {"op": "pptx.add_slide", "layout": "Title and Content", "texts": {"0": "T", "1": "B"}},
                                 {"op": "pptx.delete_slide", "slide": 2},
                                 {"op": "pptx.set_text", "slide": 1, "shape": "idx:0", "text": "Hello"}), tmp_path / "o.pptx")
    assert out.preserved, out.violations


def test_xlsx_end_to_end_preserved(xlsx_src, tmp_path):
    out = apply_plan(xlsx_src, P("xlsx", {"op": "xlsx.append_row", "sheet": "Report", "values": ["W", 1, 0.5, "2024-04-01"]},
                                 {"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "Copy"}), tmp_path / "o.xlsx")
    assert out.preserved, out.violations


def test_format_mismatch_and_unknown_extension(docx_src, tmp_path):
    assert "does not match" in validate_plan(docx_src, P("pptx"))[0]
    with pytest.raises(PlanError, match="does not match"):
        apply_plan(docx_src, P("xlsx"), tmp_path / "o.xlsx")
    with pytest.raises(PlanError, match="unsupported"):
        detect_format("x.pdf")
    assert validate_plan(docx_src, P("docx", {"op": "docx.replace_text", "paragraph": 99, "text": "x"}))


# ---- mutation tests: the check must FAIL when the design is damaged
def test_check_detects_changed_margins(docx_src, tmp_path):
    bad = tmp_path / "bad.docx"
    d = Document(str(docx_src)); d.sections[0].left_margin = Inches(2.5); d.save(bad)
    rep = check_preservation(analyze_docx(docx_src), analyze_docx(bad))
    assert not rep["ok"] and any("margins_in" in v for v in rep["violations"])


def test_check_detects_theme_font_and_style_changes(pptx_src, tmp_path):
    a = analyze_pptx(pptx_src)
    b = copy.deepcopy(a)
    b["masters"][0]["theme"]["fonts"]["major"] = "Comic Sans MS"
    b["masters"][0]["layouts"][0]["placeholders"][0]["x_in"] += 1
    rep = check_preservation(a, b)
    assert not rep["ok"] and any("theme" in v for v in rep["violations"]) and any("layouts changed" in v for v in rep["violations"])


def test_check_detects_removed_logo_and_lost_images(docx_src, pptx_src):
    a = analyze_docx(docx_src)
    b = copy.deepcopy(a); b["images"] = []
    assert any("image" in v for v in check_preservation(a, b)["violations"])
    pa = analyze_pptx(pptx_src); pb = copy.deepcopy(pa)
    pb["masters"][0]["decor"] = []
    assert any("decor" in v for v in check_preservation(pa, pb)["violations"])


def test_check_detects_xlsx_layout_damage(xlsx_src):
    a = analyze_xlsx(xlsx_src)
    b = copy.deepcopy(a)
    b["sheets"][0]["column_widths"]["A"] = 5.0
    b["sheets"][0]["merged_ranges"] = []
    b["sheet_names"] = ["Hidden"]
    v = check_preservation(a, b)["violations"]
    assert any("column_widths" in x for x in v) and any("merged_ranges" in x for x in v) and any("missing" in x for x in v)


# ---- real files through the full pipeline
SEC = REPO_ROOT / "sample_templates" / "docx" / "sec_filing_sample.docx"
OFFICE = Path(r"C:\Program Files\Microsoft Office\root\Templates\1033\Training.potx")  # skipped where Office is absent


@pytest.mark.skipif(not SEC.exists(), reason="real sample missing")
def test_real_docx_through_pipeline(tmp_path):
    out = apply_plan(SEC, P("docx", {"op": "docx.replace_text", "paragraph": 4, "text": "X"}), tmp_path / "o.docx")
    assert out.preserved, out.violations


@pytest.mark.skipif(not OFFICE.exists(), reason="Office templates not installed")
def test_real_potx_through_pipeline_and_output_is_a_pptx(tmp_path):
    out = apply_plan(OFFICE, P("pptx", {"op": "pptx.add_slide", "layout": "Title and Content", "texts": {"0": "A", "1": "B"}},
                               {"op": "pptx.delete_slide", "slide": 19}), tmp_path / "o.pptx")
    assert out.preserved, out.violations
    ct = zipfile.ZipFile(tmp_path / "o.pptx").read("[Content_Types].xml").decode()
    assert "presentation.main+xml" in ct and "template.main+xml" not in ct  # a normal deck, not a template


XLS = REPO_ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"


@pytest.mark.skipif(not XLS.exists(), reason="real sample missing")
def test_real_xls_through_pipeline(tmp_path):
    out = apply_plan(XLS, P("xlsx", {"op": "xlsx.set_cell", "sheet": "sheets", "cell": "A1", "value": "Edited"}), tmp_path / "o.xlsx")
    assert out.preserved, out.violations
