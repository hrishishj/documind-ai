"""XLSX (and legacy XLS via conversion) template analyzer -> JSON-serialisable profile."""
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.document_engine.common import clean, emu_to_in
from app.document_engine.xlsx.convert_xls import convert_xls_to_xlsx

MAX_DETAIL = 40
MAX_CELLS_CENSUS = 50_000
XLS_LIMITS = ("formulas (cached values only)", "charts", "images", "conditional formatting", "data validation")


def _color(c) -> str | None:
    if c is None:
        return None
    t = getattr(c, "type", None)
    if t == "rgb" and isinstance(c.rgb, str):
        return c.rgb[-6:]
    if t == "theme":
        return f"theme:{c.theme}" + (f"{c.tint:+.2f}" if c.tint else "")
    if t == "indexed":
        return f"indexed:{c.indexed}"
    return None


SIDES = ("left", "right", "top", "bottom")


def _edges(border) -> dict:
    """Visible border edges. openpyxl leaves unset sides as None, not an empty Side."""
    out = {}
    for s in SIDES:
        side = getattr(border, s, None)
        if side is not None and side.style:
            out[s] = side.style
    return out


def _cell_style(cell) -> dict:
    f, fl, b, a = cell.font, cell.fill, cell.border, cell.alignment
    return clean({
        "font": clean({"name": f.name, "size_pt": f.sz, "bold": bool(f.b) or None, "italic": bool(f.i) or None,
                       "underline": f.u, "color": _color(f.color)}),
        "fill": _color(fl.fgColor) if fl.fill_type == "solid" else None,
        "border": _edges(b) or None,
        "number_format": cell.number_format if cell.number_format != "General" else None,
        "alignment": clean({"h": a.horizontal, "v": a.vertical, "wrap": bool(a.wrap_text) or None, "indent": a.indent or None}) or None,
    })


def _image_info(img) -> dict:
    a = img.anchor
    info = {"anchor_type": type(a).__name__}
    fr = getattr(a, "_from", None)
    if fr is not None:
        info["from_cell"] = f"{get_column_letter(fr.col + 1)}{fr.row + 1}"
    to = getattr(a, "to", None)
    if to is not None:
        info["to_cell"] = f"{get_column_letter(to.col + 1)}{to.row + 1}"
    ext = getattr(a, "ext", None)
    if ext is not None:
        info["width_in"], info["height_in"] = emu_to_in(ext.width), emu_to_in(ext.height)
    else:
        info["width_px"], info["height_px"] = img.width, img.height
    return info


def _sheet(ws) -> dict:
    cells = Counter(); fills = Counter(); fmts = Counter(); fonts = Counter(); bordered = 0
    header = None
    n = 0
    for row in ws.iter_rows():
        non_empty = [c for c in row if c.value is not None]
        if header is None and len(non_empty) >= 2:
            header = {"row": non_empty[0].row, "cells": {c.coordinate: _cell_style(c) for c in non_empty[:12]}}
        for c in non_empty:
            n += 1
            if n > MAX_CELLS_CENSUS:
                break
            fonts[(c.font.name, c.font.sz, bool(c.font.b))] += 1
            if c.fill.fill_type == "solid":
                fills[_color(c.fill.fgColor)] += 1
            if c.number_format != "General":
                fmts[c.number_format] += 1
            if _edges(c.border):
                bordered += 1
        if n > MAX_CELLS_CENSUS:
            break

    widths = {k: round(v.width, 2) for k, v in ws.column_dimensions.items() if v.width and not v.hidden}
    heights = {r: round(d.height, 1) for r, d in ws.row_dimensions.items() if d.height}
    cf = []
    for rng, rules in ws.conditional_formatting._cf_rules.items():
        cf.append({"range": str(rng.sqref), "rules": [clean({"type": r.type, "operator": r.operator,
                                                              "formula": list(r.formula) if r.formula else None}) for r in rules]})
    merged = [str(m) for m in ws.merged_cells.ranges]
    ps, pm = ws.page_setup, ws.page_margins
    return clean({
        "name": ws.title, "state": ws.sheet_state,
        "dimensions": ws.dimensions, "max_row": ws.max_row, "max_col": ws.max_column,
        "tab_color": _color(ws.sheet_properties.tabColor), "gridlines": ws.sheet_view.showGridLines,
        "freeze_panes": ws.freeze_panes, "zoom": ws.sheet_view.zoomScale,
        "merged_count": len(merged), "merged_ranges": merged[:MAX_DETAIL],
        "column_widths": dict(list(widths.items())[:MAX_DETAIL]), "column_widths_count": len(widths),
        "row_heights": dict(list(heights.items())[:MAX_DETAIL]), "row_heights_count": len(heights),
        "header_row": header,
        "conditional_formatting": cf[:MAX_DETAIL] or None,
        "data_validations": [{"type": d.type, "range": str(d.sqref), "formula1": d.formula1}
                             for d in ws.data_validations.dataValidation][:MAX_DETAIL] or None,
        "images": [_image_info(i) for i in ws._images][:MAX_DETAIL] or None,
        "tables": [{"name": t.name, "ref": t.ref, "style": t.tableStyleInfo.name if t.tableStyleInfo else None}
                   for t in ws.tables.values()] or None,
        "page_setup": clean({"orientation": ps.orientation, "paper_size": ps.paperSize, "fit_to_width": ps.fitToWidth,
                             "margins_in": {"left": pm.left, "right": pm.right, "top": pm.top, "bottom": pm.bottom}}),
        "header_footer": clean({"odd_header": ws.oddHeader.center.text or ws.oddHeader.left.text or ws.oddHeader.right.text,
                                "odd_footer": ws.oddFooter.center.text or ws.oddFooter.left.text or ws.oddFooter.right.text}) or None,
        "style_census": {
            "non_empty_cells": min(n, MAX_CELLS_CENSUS),
            "fonts": [{"name": k[0], "size_pt": k[1], "bold": k[2], "cells": v} for k, v in fonts.most_common(5)],
            "fills": dict(fills.most_common(5)), "number_formats": dict(fmts.most_common(6)),
            "cells_with_borders": bordered,
        },
    })


def _package_parts(path: Path) -> dict:
    """Things openpyxl silently drops on load/save (charts, pivots, macros) are inventoried from the zip."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    count = lambda pre, suf=".xml": sum(1 for n in names if n.startswith(pre) and n.endswith(suf))  # noqa: E731
    return {"charts": count("xl/charts/chart"), "drawings": count("xl/drawings/drawing"),
            "media_files": sum(1 for n in names if n.startswith("xl/media/")),
            "pivot_tables": count("xl/pivotTables/pivotTable"), "has_vba": "xl/vbaProject.bin" in names,
            "comments": count("xl/comments")}


def analyze_xlsx(path: str | Path, _source_format: str = "xlsx") -> dict:
    path = Path(path)
    wb = load_workbook(path)  # not read-only: styles, images and merges are needed
    parts = _package_parts(path)
    prof = {
        "format": "xlsx", "source_format": _source_format,
        "sheet_names": wb.sheetnames, "defined_names": list(wb.defined_names.keys())[:MAX_DETAIL],
        "package_parts": parts,
        "sheets": [_sheet(ws) for ws in wb.worksheets],
        "counts": {"sheets": len(wb.sheetnames)},
        "warnings": [],
    }
    if parts["charts"]:
        prof["warnings"].append(f"{parts['charts']} chart(s) present: openpyxl drops charts on save; edits must patch XML.")
    if parts["has_vba"]:
        prof["warnings"].append("VBA macros present and not supported; .xlsm files are rejected at upload.")
    return prof


def analyze_spreadsheet(path: str | Path) -> dict:
    """Entry point for .xlsx and legacy .xls (converted in a temp dir first)."""
    path = Path(path)
    if path.suffix.lower() == ".xls":
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "converted.xlsx"
            stats = convert_xls_to_xlsx(path, dst)
            prof = analyze_xlsx(dst, _source_format="xls")
        prof["conversion"] = {**stats, "not_carried_over": list(XLS_LIMITS)}
        prof["warnings"].append("Converted from legacy .xls; " + ", ".join(XLS_LIMITS) + " were not carried over.")
        return prof
    return analyze_xlsx(path)
