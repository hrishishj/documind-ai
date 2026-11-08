"""Apply an Edit Plan to an existing DOCX *in place* (the original package is edited, never rebuilt).

Formatting is preserved by construction: text goes into a clone of the paragraph's dominant run (so
font/size/colour/bold survive), new paragraphs/rows are deep copies of existing ones, and everything the
plan does not mention (styles, headers, footers, images, section settings, theme) is left untouched.
"""
import copy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.document_engine.common import replace_in_nodes
from app.document_engine.plan import EditPlan, PlanError, RenderResult

RUNLIKE = ("w:r", "w:hyperlink", "w:ins", "w:del", "w:smartTag", "w:fldSimple")
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


# ---------------------------------------------------------------- text helpers
def _t(el) -> str:
    return "".join(x.text or "" for x in el.iter(qn("w:t")))


def _make_t(text: str):
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    return t


def set_paragraph_text(p, text: str) -> None:
    """Replace a paragraph's text, cloning the formatting of its longest text run."""
    runs = [r for r in p.iter(qn("w:r")) if _t(r)]
    if runs:
        donor = copy.deepcopy(max(runs, key=lambda r: len(_t(r))))
    else:  # empty paragraph: fall back to the paragraph-mark formatting
        donor = OxmlElement("w:r")
        mark = p.find(qn("w:pPr") + "/" + qn("w:rPr"))
        if mark is not None:
            donor.append(copy.deepcopy(mark))
    for child in list(donor):
        if child.tag != qn("w:rPr"):
            donor.remove(child)
    for tag in RUNLIKE:
        for el in p.findall(qn(tag)):
            p.remove(el)
    for i, line in enumerate(text.split("\n")):
        if i:
            donor.append(OxmlElement("w:br"))
        donor.append(_make_t(line))
    p.append(donor)


def replace_in_paragraph(p, find: str, repl: str) -> int:
    """Replace `find` even when it spans several runs; the replacement takes the first run's formatting."""
    count, touched = replace_in_nodes(list(p.iter(qn("w:t"))), find, repl)
    for n in touched:
        n.set(XML_SPACE, "preserve")  # keep leading/trailing spaces of the new text
    return count


def _set_cell_text(tc, text: str) -> None:
    ps = tc.findall(qn("w:p"))
    for extra in ps[1:]:
        tc.remove(extra)
    set_paragraph_text(ps[0], text)


# ---------------------------------------------------------------- resolution
class _Ctx:
    def __init__(self, doc):
        self.doc = doc
        self.paras = [p._p for p in doc.paragraphs]
        self.tables = [t._tbl for t in doc.tables]
        self.deleted: set = set()
        self.last_inserted: dict = {}
        self.warnings: list[str] = []

    def para(self, i, errs, what="paragraph"):
        if not 0 <= i < len(self.paras):
            errs.append(f"{what} {i} out of range: document has {len(self.paras)} top-level paragraphs (valid 0..{len(self.paras) - 1})")
            return None
        el = self.paras[i]
        if el in self.deleted:
            errs.append(f"{what} {i} was deleted by an earlier op")
            return None
        return el

    def table(self, i, errs):
        if not 0 <= i < len(self.tables):
            errs.append(f"table {i} out of range: document has {len(self.tables)} tables (valid 0..{len(self.tables) - 1})")
            return None
        return self.tables[i]

    def row(self, t, ti, r, errs, what="row"):
        rows = t.findall(qn("w:tr"))
        if r == -1:
            r = len(rows) - 1
        if not 0 <= r < len(rows):
            errs.append(f"{what} {r} out of range in table {ti}: it has {len(rows)} rows (valid 0..{len(rows) - 1})")
            return None
        if rows[r] in self.deleted:
            errs.append(f"{what} {r} of table {ti} was deleted by an earlier op")
            return None
        return rows[r]


def _resolve(ctx: _Ctx, op, errs: list[str]):
    """Return a zero-arg callable applying `op`, or None (with errors appended)."""
    k = op.op
    if k == "docx.replace_text":
        p = ctx.para(op.paragraph, errs)
        return (lambda: set_paragraph_text(p, op.text)) if p is not None else None
    if k == "docx.delete_paragraph":
        p = ctx.para(op.paragraph, errs)
        if p is None:
            return None
        if p.find(qn("w:pPr") + "/" + qn("w:sectPr")) is not None:
            errs.append(f"paragraph {op.paragraph} holds a section break and cannot be deleted")
            return None
        ctx.deleted.add(p)
        return lambda: p.getparent().remove(p)
    if k == "docx.insert_paragraph":
        anchor = ctx.para(op.after, errs, "after")
        like = ctx.para(op.like if op.like is not None else op.after, errs, "like")
        if anchor is None or like is None:
            return None

        def do():
            new = copy.deepcopy(like)
            for bad in list(new.iter(qn("w:sectPr"), qn("w:bookmarkStart"), qn("w:bookmarkEnd"))):
                bad.getparent().remove(bad)
            set_paragraph_text(new, op.text)
            ctx.last_inserted.get(anchor, anchor).addnext(new)
            ctx.last_inserted[anchor] = new
        return do
    if k == "docx.replace_all":
        def do():
            n = 0
            for p in ctx.doc.element.body.iter(qn("w:p")):
                n += replace_in_paragraph(p, op.find, op.replace)
            for s in ctx.doc.sections:
                for hf in (s.header, s.footer, s.first_page_header, s.first_page_footer, s.even_page_header, s.even_page_footer):
                    if not hf.is_linked_to_previous:
                        for p in hf._element.iter(qn("w:p")):
                            n += replace_in_paragraph(p, op.find, op.replace)
            if n == 0:
                ctx.warnings.append(f"replace_all: '{op.find}' not found")
        return do
    # ---- tables
    t = ctx.table(op.table, errs)
    if t is None:
        return None
    if k in ("docx.set_cell", "docx.delete_row"):
        tr = ctx.row(t, op.table, op.row, errs)
        if tr is None:
            return None
        if k == "docx.delete_row":
            ctx.deleted.add(tr)
            return lambda: tr.getparent().remove(tr)
        tcs = tr.findall(qn("w:tc"))
        if not 0 <= op.col < len(tcs):
            errs.append(f"col {op.col} out of range in table {op.table} row {op.row}: row has {len(tcs)} cells (valid 0..{len(tcs) - 1})")
            return None
        return lambda: _set_cell_text(tcs[op.col], op.text)
    if k == "docx.add_row":
        like = ctx.row(t, op.table, op.like_row, errs, "like_row")
        after = ctx.row(t, op.table, op.after_row, errs, "after_row")
        if like is None or after is None:
            return None

        def do():
            new = copy.deepcopy(like)
            hdr = new.find(qn("w:trPr") + "/" + qn("w:tblHeader"))
            if hdr is not None:  # a cloned header row must not become a repeating header
                hdr.getparent().remove(hdr)
            for i, tc in enumerate(new.findall(qn("w:tc"))):
                _set_cell_text(tc, op.values[i] if i < len(op.values) else "")
            if len(op.values) > len(new.findall(qn("w:tc"))):
                ctx.warnings.append(f"add_row: {len(op.values)} values for {len(new.findall(qn('w:tc')))} cells; extras ignored")
            ctx.last_inserted.get(after, after).addnext(new)  # keep plan order for several rows after one anchor
            ctx.last_inserted[after] = new
        return do
    errs.append(f"unknown op {k}")
    return None


def _prepare(doc, plan: EditPlan):
    ctx, errs, actions = _Ctx(doc), [], []
    for i, op in enumerate(plan.ops):
        local: list[str] = []
        fn = _resolve(ctx, op, local)
        errs += [f"op {i} ({op.op}): {e}" for e in local]
        if fn is not None:
            actions.append((i, op, fn))
    return ctx, errs, actions


def validate_docx_plan(src: str | Path, plan: EditPlan) -> list[str]:
    """Dry run: errors the LLM can act on, nothing is modified."""
    _, errs, _ = _prepare(Document(str(src)), plan)
    return errs


def render_docx(src: str | Path, plan: EditPlan, dst: str | Path) -> RenderResult:
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("refusing to overwrite the source file; write a new version")
    doc = Document(str(src))
    ctx, errs, actions = _prepare(doc, plan)
    if errs:
        raise PlanError(errs)  # all-or-nothing: nothing is saved if any op is invalid
    res = RenderResult()
    for i, op, fn in actions:
        fn()
        res.applied += 1
        res.details.append(f"op {i} {op.op} ok")
    res.warnings = ctx.warnings
    doc.save(str(dst))
    return res
