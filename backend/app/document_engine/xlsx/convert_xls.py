"""Pure-Python legacy .xls -> .xlsx converter (no Office/LibreOffice needed).

Carries over: values, fonts, fills, borders, number formats, alignment, merged ranges,
column widths, row heights, gridline flag. NOT carried over (xlrd does not expose them):
formulas (cached values are kept), charts, images, conditional formatting, data validation.
"""
from pathlib import Path

import xlrd
from openpyxl import Workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

BORDER_STYLES = {0: None, 1: "thin", 2: "medium", 3: "dashed", 4: "dotted", 5: "thick", 6: "double", 7: "hair",
                 8: "mediumDashed", 9: "dashDot", 10: "mediumDashDot", 11: "dashDotDot",
                 12: "mediumDashDotDot", 13: "slantDashDot"}
H_ALIGN = {0: None, 1: "left", 2: "center", 3: "right", 4: "fill", 5: "justify", 6: "centerContinuous", 7: "distributed"}
V_ALIGN = {0: "top", 1: "center", 2: "bottom", 3: "justify", 4: "distributed"}
UNDERLINE = {0: None, 1: "single", 2: "double", 33: "singleAccounting", 34: "doubleAccounting"}
AUTO_COLORS = {0x40, 0x41, 0x7FFF, None}
WIDTH_UNIT = 256.0  # xls column width unit = 1/256 of a character


def rgb_of(book: xlrd.Book, idx) -> str | None:
    """xls colour index -> 'RRGGBB' (None for automatic/system colours)."""
    if idx in AUTO_COLORS:
        return None
    rgb = book.colour_map.get(idx)
    return None if rgb is None else "%02X%02X%02X" % rgb


def style_parts(book: xlrd.Book, xf_index: int) -> dict:
    """Translate one xls XF record into openpyxl style objects (also used by the audit test)."""
    xf = book.xf_list[xf_index]
    f = book.font_list[xf.font_index]
    font = Font(name=f.name, size=f.height / 20.0, bold=bool(f.bold), italic=bool(f.italic),
                underline=UNDERLINE.get(f.underline_type), strike=bool(f.struck_out),
                color=rgb_of(book, f.colour_index))
    bg = xf.background
    fill = PatternFill()
    if bg.fill_pattern == 1:  # solid: the foreground ("pattern") colour is the visible fill
        c = rgb_of(book, bg.pattern_colour_index)
        if c:
            fill = PatternFill("solid", fgColor=c, bgColor=c)
    b = xf.border

    def side(style_attr, colour_attr):
        st = BORDER_STYLES.get(getattr(b, style_attr))
        return Side(style=st, color=rgb_of(book, getattr(b, colour_attr))) if st else Side()

    border = Border(left=side("left_line_style", "left_colour_index"), right=side("right_line_style", "right_colour_index"),
                    top=side("top_line_style", "top_colour_index"), bottom=side("bottom_line_style", "bottom_colour_index"))
    a = xf.alignment
    align = Alignment(horizontal=H_ALIGN.get(a.hor_align), vertical=V_ALIGN.get(a.vert_align),
                      wrap_text=bool(a.text_wrapped), indent=a.indent_level)
    fmt = book.format_map[xf.format_key].format_str if xf.format_key in book.format_map else "General"
    return {"font": font, "fill": fill, "border": border, "alignment": align, "number_format": fmt}


def convert_xls_to_xlsx(src: str | Path, dst: str | Path) -> dict:
    """Convert and return simple stats. Raises on unreadable input."""
    book = xlrd.open_workbook(str(src), formatting_info=True)
    wb = Workbook()
    wb.remove(wb.active)
    style_cache: dict[int, dict] = {}
    stats = {"sheets": 0, "cells": 0, "merged": 0}

    for sh in book.sheets():
        ws = wb.create_sheet(title=sh.name)
        stats["sheets"] += 1
        ws.sheet_view.showGridLines = bool(sh.show_grid_lines)
        # Merge FIRST: merge_cells() swaps non-anchor cells for MergedCell objects, which would
        # discard any style set earlier. Styling afterwards keeps per-cell edge borders intact.
        for rlo, rhi, clo, chi in sh.merged_cells:  # xlrd ranges are half-open
            ws.merge_cells(start_row=rlo + 1, end_row=rhi, start_column=clo + 1, end_column=chi)
            stats["merged"] += 1
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                ctype = sh.cell_type(r, c)
                xf_idx = sh.cell_xf_index(r, c)
                if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) and xf_idx == 0:
                    continue  # unformatted empty cell
                cell = ws.cell(row=r + 1, column=c + 1)
                v = sh.cell_value(r, c)
                if isinstance(cell, MergedCell):
                    pass  # value lives in the anchor cell; only the style is applied below
                elif ctype == xlrd.XL_CELL_DATE:
                    try:
                        cell.value = xlrd.xldate.xldate_as_datetime(v, book.datemode)
                    except Exception:
                        cell.value = v
                elif ctype == xlrd.XL_CELL_BOOLEAN:
                    cell.value = bool(v)
                elif ctype == xlrd.XL_CELL_ERROR:
                    cell.value = xlrd.error_text_from_code.get(v, "#ERR")
                elif ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    cell.value = None
                else:
                    cell.value = v
                if xf_idx not in style_cache:
                    style_cache[xf_idx] = style_parts(book, xf_idx)
                st = style_cache[xf_idx]
                cell.font, cell.fill, cell.border = st["font"], st["fill"], st["border"]
                cell.alignment, cell.number_format = st["alignment"], st["number_format"]
                stats["cells"] += 1
        for c, info in sh.colinfo_map.items():
            if c < 16384 and info.width and not info.hidden:
                ws.column_dimensions[get_column_letter(c + 1)].width = round(info.width / WIDTH_UNIT, 2)
            elif info.hidden and c < 16384:
                ws.column_dimensions[get_column_letter(c + 1)].hidden = True
        for r, info in sh.rowinfo_map.items():
            if info.height_mismatch or info.height:
                ws.row_dimensions[r + 1].height = round(info.height / 20.0, 2)
            if info.hidden:
                ws.row_dimensions[r + 1].hidden = True
    wb.save(str(dst))
    return stats
