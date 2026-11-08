import hashlib
from datetime import datetime

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference

from app.core.config import REPO_ROOT
from app.document_engine.plan import EditPlan, PlanError
from app.document_engine.samples import build_xlsx
from app.document_engine.xlsx.analyzer import analyze_spreadsheet, analyze_xlsx
from app.document_engine.xlsx.renderer import assess_content, render_xlsx, validate_xlsx_plan


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "src.xlsx"
    build_xlsx(p)
    return p


def plan(*ops, **kw):
    return EditPlan.model_validate({"format": "xlsx", "ops": list(ops), **kw})


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def style_sig(c):
    return (c.number_format, c.font.name, c.font.sz, c.font.b, c.fill.fgColor.rgb if c.fill.fill_type else None,
            c.border.bottom.style if c.border.bottom else None, c.alignment.horizontal)


def test_source_untouched_overwrite_and_extension_guards(src, tmp_path):
    before = sha(src)
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "A4", "value": "x"}), tmp_path / "o.xlsx")
    assert sha(src) == before
    with pytest.raises(ValueError):
        render_xlsx(src, plan(), src)
    with pytest.raises(ValueError):
        render_xlsx(src, plan(), tmp_path / "o.xls")


def test_set_cell_keeps_number_format_and_style(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "B4", "value": 5000.5},
                          {"op": "xlsx.set_cell", "sheet": "Report", "cell": "A3", "value": "Area"}), out)
    a, b = load_workbook(src)["Report"], load_workbook(out)["Report"]
    assert b["B4"].value == 5000.5 and b["B4"].number_format == "#,##0.00"
    assert style_sig(a["B4"]) == style_sig(b["B4"]) and style_sig(a["A3"]) == style_sig(b["A3"])  # header style survives
    assert b["A3"].value == "Area" and b["A3"].fill.fgColor.rgb.endswith("1F4E79")


def test_iso_date_string_becomes_a_real_date_only_in_date_cells(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "D4", "value": "2025-06-30"},
                          {"op": "xlsx.set_cell", "sheet": "Report", "cell": "A4", "value": "2025-06-30"}), out)
    ws = load_workbook(out)["Report"]
    assert ws["D4"].value == datetime(2025, 6, 30) and ws["D4"].number_format == "yyyy-mm-dd"
    assert ws["A4"].value == "2025-06-30"  # a text cell stays text


def test_formula_is_stored_as_formula(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "B7", "value": "=SUM(B4:B6)"}), out)
    c = load_workbook(out)["Report"]["B7"]
    assert c.value == "=SUM(B4:B6)" and c.data_type == "f"


def test_set_range(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_range", "sheet": "Report", "top_left": "A4", "values": [["N", 1, 0.1], ["S", 2, 0.2]]}), out)
    ws = load_workbook(out)["Report"]
    assert [ws["A4"].value, ws["B5"].value, ws["C5"].number_format] == ["N", 2, "0.0%"]


def test_append_rows_copy_style_extend_table_and_keep_order(src, tmp_path):
    out = tmp_path / "o.xlsx"
    res = render_xlsx(src, plan({"op": "xlsx.append_row", "sheet": "Report", "values": ["West", 77.5, 0.2, "2024-04-01"]},
                                {"op": "xlsx.append_row", "sheet": "Report", "values": ["Far", 1, 0.5, "2024-05-01"]}), out)
    a, b = load_workbook(src)["Report"], load_workbook(out)["Report"]
    assert [b.cell(7, 1).value, b.cell(8, 1).value] == ["West", "Far"]
    for col in (1, 2, 3, 4):  # new rows look exactly like the previous last row
        assert style_sig(b.cell(7, col)) == style_sig(a.cell(6, col)) == style_sig(b.cell(8, col))
    assert b["D7"].value == datetime(2024, 4, 1)
    assert b.tables["Sales"].ref == "A3:D8"  # the Excel table grew with the data
    assert any("conditional-formatting" in w for w in res.warnings)  # honest about what was NOT extended


def test_append_to_empty_sheet(tmp_path):
    p = tmp_path / "e.xlsx"
    wb = Workbook()
    wb.active.title = "S"
    wb.save(p)
    render_xlsx(p, plan({"op": "xlsx.append_row", "sheet": "S", "values": ["a", 1]}), tmp_path / "o.xlsx")
    ws = load_workbook(tmp_path / "o.xlsx")["S"]
    assert [ws["A1"].value, ws["B1"].value] == ["a", 1]


def test_copy_sheet(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "Report Q2"}), out)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Report", "Hidden", "Report Q2"]
    a, b = wb["Report"], wb["Report Q2"]
    assert [str(m) for m in b.merged_cells.ranges] == [str(m) for m in a.merged_cells.ranges]
    assert b.column_dimensions["A"].width == a.column_dimensions["A"].width and b["B4"].value == a["B4"].value
    assert style_sig(b["A3"]) == style_sig(a["A3"])


def test_image_survives_round_trip(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "A4", "value": "x"}), out)
    a, b = analyze_xlsx(src), analyze_xlsx(out)
    assert b["package_parts"]["media_files"] == 1
    assert b["sheets"][0]["images"][0]["from_cell"] == a["sheets"][0]["images"][0]["from_cell"] == "F2"


def test_untouched_sheet_profile_and_layout_identical(src, tmp_path):
    out = tmp_path / "o.xlsx"
    render_xlsx(src, plan({"op": "xlsx.set_cell", "sheet": "Report", "cell": "B4", "value": 1}), out)
    a, b = analyze_xlsx(src), analyze_xlsx(out)
    assert a["sheets"][1] == b["sheets"][1]  # the untouched sheet is identical
    keep = ("merged_ranges", "column_widths", "row_heights", "freeze_panes", "tab_color", "tables", "images",
            "page_setup", "conditional_formatting", "data_validations", "header_row")
    assert {k: a["sheets"][0].get(k) for k in keep} == {k: b["sheets"][0].get(k) for k in keep}


def test_invalid_plan_collects_every_error(src, tmp_path):
    out = tmp_path / "o.xlsx"
    p = plan({"op": "xlsx.set_cell", "sheet": "Nope", "cell": "A1", "value": 1},
             {"op": "xlsx.set_cell", "sheet": "Report", "cell": "B1", "value": 1},          # inside merged A1:D1
             {"op": "xlsx.set_cell", "sheet": "Report", "cell": "ZZZZ", "value": 1},
             {"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "hidden"},            # case-insensitive clash
             {"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "a/b"},
             {"op": "xlsx.append_row", "sheet": "Report", "values": [1], "like_row": 999})
    with pytest.raises(PlanError) as e:
        render_xlsx(src, p, out)
    errs = e.value.errors
    assert len(errs) == 6 and not out.exists()
    assert "sheets: ['Report', 'Hidden']" in errs[0] and "top-left cell A1" in errs[1] and "valid cell" in errs[2]
    assert validate_xlsx_plan(src, p) == errs


def test_two_copies_with_the_same_name_in_one_plan_are_rejected(src, tmp_path):
    with pytest.raises(PlanError, match="already exists"):
        render_xlsx(src, plan({"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "X"},
                              {"op": "xlsx.copy_sheet", "sheet": "Report", "new_name": "x"}), tmp_path / "o.xlsx")


# ------------------------------------------------------------ content-risk guard
# Fixtures were authored by Excel itself; "survives?" claims below were measured with Excel COM before/after
# an openpyxl round trip (shapes 1->0, sparklines 1->0, pivots 1->1, data bars 1->1, chart kept w/ rounded corners).
FIX = REPO_ROOT / "backend" / "tests" / "fixtures"
RICH, CHART = FIX / "excel_rich.xlsx", FIX / "excel_chart.xlsx"


def test_assessment_matches_what_excel_authored(src):
    risk = assess_content(RICH)
    assert risk["blocking"] == ["1 drawing shape(s)/text box(es)", "sparklines"]  # pivot + data bar are NOT blocking
    assert assess_content(CHART)["blocking"] == []
    assert any("chart" in d for d in assess_content(CHART)["degraded"])
    assert assess_content(src) == {"blocking": [], "degraded": []}


def test_workbook_with_shapes_or_sparklines_is_refused_by_default(tmp_path):
    out = tmp_path / "o.xlsx"
    with pytest.raises(PlanError, match="text box.*sparklines|cannot be preserved"):
        render_xlsx(RICH, plan({"op": "xlsx.set_cell", "sheet": "Data", "cell": "A1", "value": "x"}), out)
    assert not out.exists()


def test_allow_lossy_proceeds_and_the_drop_is_real_and_reported(tmp_path):
    out = tmp_path / "o.xlsx"
    res = render_xlsx(RICH, plan({"op": "xlsx.set_cell", "sheet": "Data", "cell": "A1", "value": "Edited"}, allow_lossy=True), out)
    assert any(w.startswith("LOSSY") and "sparklines" in w and "text box" in w for w in res.warnings)
    assert assess_content(out)["blocking"] == []  # honesty: they really are gone, and the result said so
    assert analyze_xlsx(out)["package_parts"]["pivot_tables"] == 1  # pivot tables are preserved


def test_charts_are_kept_with_a_degradation_warning(tmp_path):
    out = tmp_path / "o.xlsx"
    res = render_xlsx(CHART, plan({"op": "xlsx.set_cell", "sheet": "Data", "cell": "B2", "value": 999}), out)
    assert analyze_xlsx(out)["package_parts"]["charts"] == 1
    assert any("chart" in w and "may change" in w for w in res.warnings)
    assert load_workbook(out)["Data"]["B2"].value == 999
    assert not any(w.startswith("LOSSY") for w in res.warnings)


def test_plain_workbooks_are_not_flagged(src):
    assert assess_content(src)["blocking"] == []

# ------------------------------------------------------------ real legacy .xls
XLS = REPO_ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"


@pytest.mark.skipif(not XLS.exists(), reason="real sample not present")
def test_real_xls_edit_preserves_every_other_sheet(tmp_path):
    out = tmp_path / "o.xlsx"
    res = render_xlsx(XLS, plan({"op": "xlsx.set_cell", "sheet": "ome statements", "cell": "A1", "value": "EDITED TITLE"},
                                {"op": "xlsx.append_row", "sheet": "ome statements", "values": ["Total", 1, 2, 3]}), out)
    assert any("legacy .xls" in w for w in res.warnings)
    base, after = analyze_spreadsheet(XLS), analyze_xlsx(out)
    assert len(after["sheets"]) == 77 == len(base["sheets"])
    changed = [s["name"] for s, t in zip(base["sheets"], after["sheets"]) if s != t]
    assert changed == ["ome statements"]  # 76 other sheets are profile-identical
    keep = ("merged_ranges", "column_widths", "freeze_panes", "header_row")
    s0 = next(s for s in base["sheets"] if s["name"] == "ome statements")
    s1 = next(s for s in after["sheets"] if s["name"] == "ome statements")
    assert {k: s0.get(k) for k in keep if k != "header_row"} == {k: s1.get(k) for k in keep if k != "header_row"}
    assert load_workbook(out)["ome statements"]["A1"].value == "EDITED TITLE"
