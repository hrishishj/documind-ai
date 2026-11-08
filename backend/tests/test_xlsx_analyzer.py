import json

import pytest

from app.core.config import REPO_ROOT
from app.document_engine.samples import build_xlsx
from app.document_engine.xlsx.analyzer import analyze_spreadsheet, analyze_xlsx


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    p = tmp_path_factory.mktemp("xlsx") / "t.xlsx"
    truth = build_xlsx(p)
    return analyze_xlsx(p), truth


def sheet(prof, name="Report"):
    return next(s for s in prof["sheets"] if s["name"] == name)


def test_json_serialisable(built):
    json.dumps(built[0])


def test_sheets_and_hidden_state(built):
    prof, _ = built
    assert prof["sheet_names"] == ["Report", "Hidden"]
    assert sheet(prof, "Hidden")["state"] == "hidden"
    assert sheet(prof)["tab_color"] == "FF6600"


def test_title_merge_and_font(built):
    prof, t = built
    s = sheet(prof)
    assert s["merged_ranges"] == [t["merged"]]
    a1 = s["header_row"]  # first row with >=2 non-empty cells is row 3 (title is a single merged cell)
    assert a1["row"] == 3


def test_header_row_style(built):
    prof, t = built
    cells = sheet(prof)["header_row"]["cells"]
    a3 = cells["A3"]
    assert a3["fill"] == t["header_fill"]
    assert a3["font"]["name"] == "Arial" and a3["font"]["size_pt"] == 11.0 and a3["font"]["bold"] is True
    assert a3["font"]["color"] == "FFFFFF"
    assert a3["alignment"]["h"] == "center" and a3["alignment"]["wrap"] is True
    assert a3["border"] == {"bottom": "medium"}


def test_dimensions_freeze_widths(built):
    prof, t = built
    s = sheet(prof)
    assert s["freeze_panes"] == t["freeze"]
    assert s["column_widths"]["A"] == 24.0 and s["column_widths"]["B"] == 14.0
    assert s["row_heights"][3] == 28.0


def test_number_formats_found(built):
    prof, t = built
    assert set(sheet(prof)["style_census"]["number_formats"]) == t["formats"]


def test_conditional_validation_table_image(built):
    prof, t = built
    s = sheet(prof)
    assert s["conditional_formatting"][0]["range"] == "B4:B6"
    assert s["conditional_formatting"][0]["rules"][0]["type"] == "cellIs"
    assert s["data_validations"][0]["type"] == "list" and s["data_validations"][0]["range"] == "A4:A6"
    assert s["tables"] == [{"name": t["table"][0], "ref": t["table"][1], "style": "TableStyleMedium2"}]
    assert s["images"][0]["from_cell"] == t["image_from"]


def test_page_setup_and_footer(built):
    s = sheet(built[0])
    assert s["page_setup"]["orientation"] == "landscape"
    assert s["header_footer"]["odd_footer"] == "Confidential"


def test_package_parts_inventory(built):
    parts = built[0]["package_parts"]
    assert parts["media_files"] == 1 and parts["has_vba"] is False and parts["charts"] == 0


def test_charts_are_detected_and_warned(tmp_path):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    wb = Workbook()
    ws = wb.active
    for r in range(1, 5):
        ws.append([r, r * 2])
    ch = BarChart()
    ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=4))
    ws.add_chart(ch, "D2")
    p = tmp_path / "c.xlsx"
    wb.save(p)
    prof = analyze_xlsx(p)
    assert prof["package_parts"]["charts"] == 1
    assert any("chart" in w for w in prof["warnings"])


# ---------- real legacy .xls ----------
XLS = REPO_ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"


@pytest.mark.skipif(not XLS.exists(), reason="real sample not present")
def test_real_xls_profile():
    prof = analyze_spreadsheet(XLS)
    assert prof["source_format"] == "xls" and prof["counts"]["sheets"] == 77
    assert prof["conversion"]["merged"] > 100
    assert any("legacy .xls" in w for w in prof["warnings"])
    inc = next(s for s in prof["sheets"] if s["name"] == "ome statements")
    assert inc["merged_count"] == 19
    assert inc["style_census"]["non_empty_cells"] > 50
    assert len(json.dumps(prof)) < 3_000_000
