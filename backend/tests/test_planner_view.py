import json

import pytest

from app.core.config import REPO_ROOT
from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.planner_view import build_planner_view
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.document_engine.xlsx.analyzer import analyze_spreadsheet, analyze_xlsx
from app.llm.provider import est_tokens


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("pv")
    build_docx(d / "a.docx"); build_pptx(d / "a.pptx"); build_xlsx(d / "a.xlsx")
    return d


def test_docx_view_shows_indices_styles_and_tables(files):
    v = build_planner_view(analyze_docx(files / "a.docx"))
    assert "P0 [Heading 1] Quarterly Report" in v and "P1 [Normal] Intro paragraph in Georgia. Bold red run." in v
    assert "T0: 3x3 Region | Q1 | Q2" in v and "North | 10 | 20" in v  # table content is visible for targeting
    assert "Georgia" not in v.replace("in Georgia", "")  # formatting facts are not in the view (text mentions are fine)
    assert "1F4E79" not in v and "margin" not in v.lower()


def test_pptx_view_has_layouts_without_unfillable_placeholders_and_shape_names(files):
    v = build_planner_view(analyze_pptx(files / "a.pptx"))
    assert '"Title and Content" (idx0 TITLE, idx1 OBJECT)' in v
    assert "DATE" not in v and "FOOTER" not in v and "SLIDE_NUMBER" not in v
    assert "S2 [Title and Content] title='Highlights'" in v and "idx:0" in v
    assert "table 3x2 header=['Metric', 'Value']" in v and "chart(barChart)" in v


def test_xlsx_view_has_values_ranges_tables_and_last_row(files):
    v = build_planner_view(analyze_xlsx(files / "a.xlsx"))
    assert "Sheet 'Report' range A1:D6 last_data_row=6 tables: Sales A3:D6 merged: A1:D1" in v
    assert "A3=Region | B3=Revenue" in v and "A4=North | B4=1234.5" in v
    assert "state=hidden" in v  # the hidden sheet is flagged


def test_view_is_far_smaller_than_the_json_summary(files):
    for f, fn in (("a.pptx", analyze_pptx), ("a.xlsx", analyze_xlsx)):
        p = fn(files / f)
        assert len(build_planner_view(p)) < len(json.dumps(p))


def test_budget_is_enforced_and_marked(files):
    v = build_planner_view(analyze_docx(files / "a.docx"), budget_chars=120)
    assert len(v) <= 140 and v.endswith("[view truncated]")


SEC = REPO_ROOT / "sample_templates" / "docx" / "sec_filing_sample.docx"


@pytest.mark.skipif(not SEC.exists(), reason="real sample missing")
def test_relevance_pulls_far_away_paragraphs_into_a_huge_document():
    p = analyze_docx(SEC)
    texts = {b["index"]: b["text"] for b in p["body"] if b["type"] == "paragraph" and len(b.get("text", "")) > 40}
    far_idx, far_text = [(i, t) for i, t in texts.items() if i > 1500][0]
    keyword = max(far_text.split(), key=len)  # a distinctive word from deep in the document
    v_plain = build_planner_view(p, "")
    v_asked = build_planner_view(p, f"update the part about {keyword}")
    assert f"P{far_idx} " not in v_plain and f"P{far_idx} " in v_asked
    assert "paragraphs" in v_asked and "omitted" in v_asked  # gaps are marked, not hidden
    assert est_tokens(v_asked) < 2500  # and still small enough for an 8k-token/minute budget


OFFICE = __import__("pathlib").Path(r"C:\Program Files\Microsoft Office\root\Templates\1033\Training.potx")


@pytest.mark.skipif(not OFFICE.exists(), reason="Office templates not installed")
def test_real_deck_view_stays_under_budget():
    p = analyze_pptx(OFFICE)
    assert est_tokens(build_planner_view(p, "change slide 2 title")) < 2000 < est_tokens(json.dumps(p))


XLS = REPO_ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"


@pytest.mark.skipif(not XLS.exists(), reason="real sample missing")
def test_many_sheet_workbook_prioritises_the_named_sheet():
    p = analyze_spreadsheet(XLS)
    v = build_planner_view(p, "update the uncertain tax positions sheet")
    assert "Sheet 'uncertain tax positions'" in v and "more sheets" in v
