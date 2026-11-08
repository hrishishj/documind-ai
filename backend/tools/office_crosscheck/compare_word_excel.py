import json, sys, tempfile
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[3]  # repo root
sys.path.insert(0, str(ROOT / "backend"))
from pathlib import Path
from docx import Document
from docx.oxml.ns import qn
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from app.document_engine.xlsx.convert_xls import convert_xls_to_xlsx

t = json.load(open(sys.argv[1], encoding="utf-8-sig"))

# ---- Word: is 23849 == all <w:p> in the body (incl. table cells)? ----
d = Document(str(ROOT / "sample_templates" / "docx" / "sec_filing_sample.docx"))
all_p = sum(1 for _ in d.element.body.iter(qn("w:p")))
print(f"WORD  paragraphs: word={t['word']['paragraphs']} all <w:p> in body={all_p} top-level={len(d.paragraphs)}")

# ---- Excel: compare against converted workbook ----
dst = Path(tempfile.mkdtemp()) / "c.xlsx"
convert_xls_to_xlsx(str(ROOT / "sample_templates" / "xlsx" / "sec_filing_sample.xls"), dst)
wb = load_workbook(dst)
ex = t["excel"]
print(f"EXCEL sheets: excel={ex['sheet_count']} mine={len(wb.sheetnames)}")
bad = 0
checked = 0
for s in ex["sheets"]:
    ws = wb[s["name"]]
    for col, key in ((1, "colA_width"), (3, "colC_width")):
        mine = ws.column_dimensions[get_column_letter(col)].width
        # Excel reports width in char units that include a small padding delta vs xls raw; allow 1 char
        flag = "" if mine and abs(mine - s[key]) <= 1.0 else "  <-- DIFF"
        if flag: bad += 1
        print(f"  {s['name']:<26} col{get_column_letter(col)} width excel={s[key]:.2f} mine={mine}{flag}")
    norm = lambda f: f.replace("\\", "").lower()  # noqa: E731  (escaping/case only; same format)
    for c in s["cells"]:
        cell = ws[c["addr"]]
        rng = next((r for r in ws.merged_cells.ranges if cell.coordinate in r), None)
        if rng is not None:  # Excel reads a merged area through its top-left (anchor) cell
            cell = ws.cell(row=rng.min_row, column=rng.min_col)
        mine = {"font": cell.font.name, "size": float(cell.font.sz), "bold": bool(cell.font.b), "fmt": norm(cell.number_format),
                "merged": rng is not None}
        want = {"font": c["font"], "size": float(c["size"]), "bold": c["bold"], "fmt": norm(c["fmt"]), "merged": c["merged"]}
        checked += 1
        if mine != want:
            bad += 1
            print("  DIFF", s["name"], c["addr"], "excel:", want, "mine:", mine)
print(f"excel cells compared: {checked}; mismatches: {bad}")
