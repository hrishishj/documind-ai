"""Apply an Edit Plan to an existing XLSX (legacy .xls is converted first).

Values are written into existing cells, so number formats, fonts, fills and borders stay. New rows copy the
style of a reference row. openpyxl cannot round-trip some content (charts, shapes, sparklines, pivots, macros);
the renderer detects that up front and refuses unless the plan says `allow_lossy`, instead of silently
dropping it.
"""
import re
import tempfile
import zipfile
from copy import copy
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.cell import coordinate_from_string

from app.document_engine.plan import EditPlan, PlanError, RenderResult
from app.document_engine.xlsx.convert_xls import convert_xls_to_xlsx

BAD_SHEET_CHARS = set('[]:*?/\\')


def assess_content(path: str | Path) -> dict[str, list[str]]:
    """What an openpyxl load/save does to the package. Every entry was verified against Excel itself
    (tests/fixtures/excel_rich.xlsx and excel_chart.xlsx, counted via COM before and after a round trip):

    * blocking - really lost: drawing shapes/text boxes, sparklines (openpyxl warns), slicers, VBA
    * degraded - kept, with small fidelity loss: charts (chart-style preset, effects and the
      "square corners" flag are lost, so the border renders rounded); Excel-2010 rule extensions
    * pivot tables are preserved and are neither listed nor warned about
    """
    blocking, degraded = [], []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        read = lambda p: z.read(p).decode("utf8", "ignore")  # noqa: E731
        sheets = [x for x in names if re.match(r"xl/worksheets/sheet\d+\.xml$", x)]
        shapes = sum(len(re.findall(r"<xdr:(?:sp|cxnSp|grpSp)[ >]", read(x)))
                     for x in names if x.startswith("xl/drawings/drawing") and x.endswith(".xml"))
        if shapes:
            blocking.append(f"{shapes} drawing shape(s)/text box(es)")
        if any("sparklineGroup" in read(x) for x in sheets):
            blocking.append("sparklines")
        if any(x.startswith("xl/slicers/") for x in names):
            blocking.append("slicers")
        if "xl/vbaProject.bin" in names:
            blocking.append("VBA macros")
        charts = sum(1 for x in names if re.match(r"xl/charts/chart\d+\.xml$", x))
        if charts:
            degraded.append(f"{charts} chart(s) are re-serialized: chart style preset, effects and square corners may change")
        if any(re.search(r"<x14:(conditionalFormatting|dataValidation)", read(x)) for x in sheets):
            degraded.append("Excel-2010 conditional-format/validation extensions are dropped (basic rules are kept)")
    return {"blocking": blocking, "degraded": degraded}


def detect_lossy_content(path: str | Path) -> list[str]:
    """Blocking items only (kept for callers that just need the refusal list)."""
    return assess_content(path)["blocking"]


def _last_data_row(ws) -> int:
    for r in range(ws.max_row, 0, -1):
        if any(c.value is not None for c in ws[r]):
            return r
    return 0


class _Ctx:
    def __init__(self, wb):
        self.wb = wb
        self.warnings: list[str] = []
        self.new_names: set[str] = set()  # sheet names claimed by earlier copy_sheet ops in this plan

    def sheet(self, name, errs):
        if name not in self.wb.sheetnames:
            errs.append(f"sheet '{name}' not found; sheets: {self.wb.sheetnames}")
            return None
        return self.wb[name]

    def cell_ok(self, ws, addr, errs):
        try:
            col, row = coordinate_from_string(addr.upper())
            range_boundaries(f"{col}{row}")
        except Exception:
            errs.append(f"'{addr}' is not a valid cell address")
            return None
        c = ws[addr.upper()]
        if isinstance(c, MergedCell):
            anchor = next((str(r).split(":")[0] for r in ws.merged_cells.ranges if addr.upper() in r), "?")
            errs.append(f"{addr} lies inside a merged range on '{ws.title}'; write to its top-left cell {anchor}")
            return None
        return c


def _coerce(cell, v):
    """ISO date strings become real dates when the target cell is date-formatted."""
    if isinstance(v, str) and cell.is_date:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


def _resolve(ctx: _Ctx, op, errs: list[str]):
    ws = ctx.sheet(op.sheet, errs)
    if ws is None:
        return None
    k = op.op
    if k == "xlsx.set_cell":
        c = ctx.cell_ok(ws, op.cell, errs)
        if c is None:
            return None

        def do():
            c.value = _coerce(c, op.value)
        return do
    if k == "xlsx.set_range":
        try:
            col, row = coordinate_from_string(op.top_left.upper())
            c0 = range_boundaries(f"{col}{row}")[0]
        except Exception:
            errs.append(f"'{op.top_left}' is not a valid cell address")
            return None
        cells = []
        for dr, rowvals in enumerate(op.values):
            for dc, v in enumerate(rowvals):
                addr = f"{get_column_letter(c0 + dc)}{row + dr}"
                c = ctx.cell_ok(ws, addr, errs)
                if c is None:
                    return None
                cells.append((c, v))

        def do():
            for c, v in cells:
                c.value = _coerce(c, v)
        return do
    if k == "xlsx.append_row":
        if op.like_row is not None and not 1 <= op.like_row <= max(ws.max_row, 1):
            errs.append(f"like_row {op.like_row} out of range on '{ws.title}' (rows 1..{ws.max_row})")
            return None

        def do():
            last = _last_data_row(ws)
            like = op.like_row or last
            new = last + 1
            first_col = 1
            if like >= 1:
                filled = [c.column for c in ws[like] if c.value is not None]
                first_col = min(filled) if filled else 1
                for c in ws[like]:
                    if c.has_style:
                        ws.cell(row=new, column=c.column)._style = copy(c._style)
                if ws.row_dimensions[like].height:
                    ws.row_dimensions[new].height = ws.row_dimensions[like].height
                if any(c.data_type == "f" for c in ws[like]):
                    ctx.warnings.append(f"'{ws.title}' row {like} contains formulas; they were not copied to row {new}")
            for i, v in enumerate(op.values):
                cell = ws.cell(row=new, column=first_col + i)
                if not isinstance(cell, MergedCell):
                    cell.value = _coerce(cell, v)
            for t in ws.tables.values():  # grow an Excel table that ended at the previous last row
                c1, r1, c2, r2 = range_boundaries(t.ref)
                if r2 == last and c1 <= first_col <= c2:
                    t.ref = f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{new}"
                    if t.autoFilter is not None:
                        t.autoFilter.ref = t.ref
            if len(ws.conditional_formatting):
                ctx.warnings.append(f"'{ws.title}': conditional-formatting ranges were not extended to row {new}")
        return do
    if k == "xlsx.copy_sheet":
        if any(ch in BAD_SHEET_CHARS for ch in op.new_name):
            errs.append(f"new_name '{op.new_name}' contains one of []:*?/\\")
            return None
        taken = {n.lower() for n in ctx.wb.sheetnames} | ctx.new_names
        if op.new_name.lower() in taken:
            errs.append(f"a sheet named '{op.new_name}' already exists")
            return None
        if getattr(ws, "_images", None):
            ctx.warnings.append(f"copy_sheet: images on '{ws.title}' are not copied to '{op.new_name}'")
        ctx.new_names.add(op.new_name.lower())

        def do():
            cp = ctx.wb.copy_worksheet(ws)
            cp.title = op.new_name
        return do
    errs.append(f"unknown op {k}")
    return None


def _prepare(wb, plan):
    ctx, errs, actions = _Ctx(wb), [], []
    for i, op in enumerate(plan.ops):
        local: list[str] = []
        fn = _resolve(ctx, op, local)
        errs += [f"op {i} ({op.op}): {e}" for e in local]
        if fn is not None:
            actions.append((i, op, fn))
    return ctx, errs, actions


def validate_xlsx_plan(src: str | Path, plan: EditPlan) -> list[str]:
    src = Path(src)
    if src.suffix.lower() == ".xls":
        with tempfile.TemporaryDirectory() as td:
            convert_xls_to_xlsx(src, Path(td) / "c.xlsx")
            return _prepare(load_workbook(Path(td) / "c.xlsx"), plan)[1]
    return _prepare(load_workbook(src), plan)[1]


def render_xlsx(src: str | Path, plan: EditPlan, dst: str | Path) -> RenderResult:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("refusing to overwrite the source file; write a new version")
    if dst.suffix.lower() != ".xlsx":
        raise ValueError("output must be .xlsx")
    notes: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        work = src
        if src.suffix.lower() == ".xls":
            work = Path(td) / "converted.xlsx"
            convert_xls_to_xlsx(src, work)
            notes.append("converted from legacy .xls; formulas (cached values only), charts, images, conditional "
                         "formatting and data validation were not carried over")
        risk = assess_content(work)
        lossy = risk["blocking"]
        notes += risk["degraded"]
        if lossy and not plan.allow_lossy:
            raise PlanError([f"this workbook contains {', '.join(lossy)}, which cannot be preserved when editing with this "
                             "engine; the edit was refused so nothing is silently lost. Set allow_lossy=true to proceed anyway."])
        wb = load_workbook(work)
        ctx, errs, actions = _prepare(wb, plan)
        if errs:
            raise PlanError(errs)
        res = RenderResult()
        for i, op, fn in actions:
            fn()
            res.applied += 1
            res.details.append(f"op {i} {op.op} ok")
        res.warnings = notes + ctx.warnings + ([f"LOSSY: dropped {', '.join(lossy)}"] if lossy else [])
        wb.save(str(dst))
    return res
