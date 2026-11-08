"""Cell-by-cell fidelity audit of the .xls -> .xlsx converter on the real sample."""
import pytest
import xlrd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.core.config import REPO_ROOT
from app.document_engine.xlsx.convert_xls import (BORDER_STYLES, H_ALIGN, WIDTH_UNIT, convert_xls_to_xlsx, rgb_of)

SRC = REPO_ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"
pytestmark = pytest.mark.skipif(not SRC.exists(), reason="real sample not present")


@pytest.fixture(scope="module")
def dst_path(tmp_path_factory):
    dst = tmp_path_factory.mktemp("xls") / "out.xlsx"
    convert_xls_to_xlsx(SRC, dst)
    return dst


@pytest.fixture(scope="module")
def converted(dst_path):
    return None, load_workbook(dst_path), xlrd.open_workbook(str(SRC), formatting_info=True)


def _non_anchor(sh) -> set:
    """0-based (r, c) of merged cells that are not the top-left anchor."""
    out = set()
    for rlo, rhi, clo, chi in sh.merged_cells:
        out |= {(r, c) for r in range(rlo, rhi) for c in range(clo, chi)} - {(rlo, clo)}
    return out


def test_sheet_names_and_order_preserved(converted):
    _, wb, book = converted
    assert wb.sheetnames == book.sheet_names()
    assert len(wb.sheetnames) == 77


def test_every_cell_value_and_style_matches(converted):
    _, wb, book = converted
    problems = []
    checked = 0
    for sh in book.sheets():
        ws = wb[sh.name]
        skip = _non_anchor(sh)  # openpyxl cannot read these back; verified from raw XML below
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                if (r, c) in skip:
                    continue
                ctype = sh.cell_type(r, c)
                xf = book.xf_list[sh.cell_xf_index(r, c)]
                cell = ws.cell(row=r + 1, column=c + 1)
                where = f"{sh.name}!{get_column_letter(c + 1)}{r + 1}"
                v = sh.cell_value(r, c)
                if ctype == xlrd.XL_CELL_NUMBER:
                    if cell.value != v:
                        problems.append((where, "value", v, cell.value))
                elif ctype == xlrd.XL_CELL_TEXT:
                    if cell.value != v:
                        problems.append((where, "value", v, cell.value))
                if sh.cell_xf_index(r, c) == 0 and ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    continue
                checked += 1
                f = book.font_list[xf.font_index]
                if cell.font.name != f.name or abs(cell.font.size - f.height / 20.0) > 0.01:
                    problems.append((where, "font", (f.name, f.height / 20), (cell.font.name, cell.font.size)))
                # bold is defined by font weight (what Excel uses), NOT xlrd's legacy `bold` bit
                if bool(cell.font.bold) != (f.weight >= 700) or bool(cell.font.italic) != bool(f.italic):
                    problems.append((where, "bold/italic", (f.weight, f.italic), (cell.font.bold, cell.font.italic)))
                want_fill = rgb_of(book, xf.background.pattern_colour_index) if xf.background.fill_pattern == 1 else None
                got_fill = cell.fill.fgColor.rgb[-6:] if cell.fill.fill_type == "solid" else None
                if want_fill != got_fill:
                    problems.append((where, "fill", want_fill, got_fill))
                for side, attr in (("left", "left_line_style"), ("right", "right_line_style"),
                                   ("top", "top_line_style"), ("bottom", "bottom_line_style")):
                    want = BORDER_STYLES.get(getattr(xf.border, attr))
                    got = getattr(cell.border, side).style
                    if want != got:
                        problems.append((where, f"border.{side}", want, got))
                want_fmt = book.format_map[xf.format_key].format_str
                if cell.number_format != want_fmt:
                    problems.append((where, "numfmt", want_fmt, cell.number_format))
                if cell.alignment.horizontal != H_ALIGN.get(xf.alignment.hor_align):
                    problems.append((where, "halign", xf.alignment.hor_align, cell.alignment.horizontal))
    assert checked > 1000, "audit did not examine enough cells"
    assert not problems, f"{len(problems)} mismatches, first 10: {problems[:10]}"


def test_bold_matches_what_excel_itself_reports(converted):
    """Regression: xlrd's legacy `bold` bit is unset in real files; Excel (COM) reported these as bold."""
    _, wb, _ = converted
    for sheet, addr in (("ome statements", "A5"), ("ome statements", "C5"), ("sheets", "A6"),
                        ("uncertain tax positions", "D6")):
        assert wb[sheet][addr].font.b is True, (sheet, addr)
    assert wb["ome statements"]["A1"].font.b is False  # and plain cells stay plain


def test_non_anchor_merged_cell_styles_survive_in_file(dst_path, converted):
    """Edge borders/alignment on non-anchor merged cells must be present in the saved XML."""
    import zipfile
    from lxml import etree
    _, wb, book = converted
    zf = zipfile.ZipFile(dst_path)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    problems, checked = [], 0
    for idx, sh in enumerate(book.sheets(), 1):
        root = etree.fromstring(zf.read(f"xl/worksheets/sheet{idx}.xml"))
        styles = {c.get("r"): int(c.get("s", 0)) for c in root.iter(f"{ns}c")}
        for r, c in _non_anchor(sh):
            xf = book.xf_list[sh.cell_xf_index(r, c)]
            if sh.cell_xf_index(r, c) == 0:
                continue
            coord = f"{get_column_letter(c + 1)}{r + 1}"
            s = styles.get(coord)
            if s is None:
                problems.append((sh.name, coord, "cell missing from XML"))
                continue
            checked += 1
            sa = wb._cell_styles[s]
            border, align = wb._borders[sa.borderId], wb._alignments[sa.alignmentId]
            for side, attr in (("left", "left_line_style"), ("right", "right_line_style"),
                               ("top", "top_line_style"), ("bottom", "bottom_line_style")):
                if getattr(border, side).style != BORDER_STYLES.get(getattr(xf.border, attr)):
                    problems.append((sh.name, coord, side))
            if align.horizontal != H_ALIGN.get(xf.alignment.hor_align):
                problems.append((sh.name, coord, "halign"))
    assert checked > 100, f"only {checked} non-anchor styled cells examined"
    assert not problems, f"{len(problems)} problems, first: {problems[:8]}"


def test_merged_ranges_preserved(converted):
    _, wb, book = converted
    for sh in book.sheets():
        want = {(rlo + 1, rhi, clo + 1, chi) for rlo, rhi, clo, chi in sh.merged_cells}
        got = {(m.min_row, m.max_row, m.min_col, m.max_col) for m in wb[sh.name].merged_cells.ranges}
        assert want == got, sh.name


def test_column_widths_preserved(converted):
    _, wb, book = converted
    bad = []
    for sh in book.sheets():
        for c, info in sh.colinfo_map.items():
            if c < 16384 and info.width and not info.hidden:
                got = wb[sh.name].column_dimensions[get_column_letter(c + 1)].width
                if abs(got - info.width / WIDTH_UNIT) > 0.01:
                    bad.append((sh.name, c, info.width / WIDTH_UNIT, got))
    assert not bad, bad[:5]
