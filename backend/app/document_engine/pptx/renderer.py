"""Apply an Edit Plan to an existing PPTX in place.

New slides are instantiated from the template's own layouts (so master, theme, placeholders and logos
come along), duplicates are deep copies of an existing slide, and text edits clone the dominant run
formatting. Slide numbers in ops refer to the ORIGINAL deck; positions refer to the deck at that step.
"""
import copy
from pathlib import Path

from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

from app.document_engine.common import replace_in_nodes
from app.document_engine.plan import EditPlan, PlanError, RenderResult
from app.document_engine.pptx.analyzer import open_presentation

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P14_SLDID = "{http://schemas.microsoft.com/office/powerpoint/2010/main}sldId"
SHAPE_TAGS = ("p:sp", "p:pic", "p:graphicFrame", "p:grpSp", "p:cxnSp")
from pptx.enum.shapes import PP_PLACEHOLDER  # noqa: E402

SKIPPED_PLACEHOLDERS = {PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}
COPYABLE_RELS = {RT.IMAGE, RT.HYPERLINK}
SKIP_RELS = {RT.SLIDE_LAYOUT, RT.NOTES_SLIDE}


# ---------------------------------------------------------------- text helpers
def _para_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(qn("a:t")))


def set_para_text(p, text: str) -> None:
    """Replace one a:p's text using a clone of its longest run (keeps font/size/colour/bold)."""
    runs = [r for r in p.findall(qn("a:r")) if _para_text(r)]
    end = p.find(qn("a:endParaRPr"))
    if runs:
        donor = copy.deepcopy(max(runs, key=lambda r: len(_para_text(r))))
    else:
        donor = p.makeelement(qn("a:r"), {})
        if end is not None:  # empty paragraph: its end-of-paragraph props describe the intended format
            rpr = copy.deepcopy(end)
            rpr.tag = qn("a:rPr")
            donor.append(rpr)
    for c in list(donor):
        if c.tag != qn("a:rPr"):
            donor.remove(c)
    t = donor.makeelement(qn("a:t"), {})
    t.text = text
    donor.append(t)
    for tag in ("a:r", "a:br", "a:fld"):
        for el in p.findall(qn(tag)):
            p.remove(el)
    if end is not None:
        end.addprevious(donor)
    else:
        p.append(donor)


def set_body_text(txbody, text: str) -> None:
    """One paragraph per line; extra lines clone the last paragraph, surplus paragraphs are removed."""
    lines = text.split("\n")
    paras = txbody.findall(qn("a:p"))
    while len(paras) < len(lines):
        clone = copy.deepcopy(paras[-1])
        paras[-1].addnext(clone)
        paras.append(clone)
    for extra in paras[len(lines):]:
        txbody.remove(extra)
    for p, line in zip(paras, lines):
        set_para_text(p, line)


# ---------------------------------------------------------------- resolution
class _Ctx:
    def __init__(self, prs):
        self.prs = prs
        self.orig = list(prs.slides)
        self.order = list(self.orig)  # live deck order (slide objects)
        self.n = len(self.orig)  # simulated slide count for position validation
        self.deleted: set = set()
        self.warnings: list[str] = []
        self._sid: dict[int, int] = {}
        for s in self.orig:
            self.track(s)
        self.layouts = {l.name: l for m in prs.slide_masters for l in m.slide_layouts}

    def sid(self, s) -> int:
        """Slide id recorded when the slide entered the deck. python-pptx recomputes it from the live slide
        list, which fails for a slide that was already removed, so it is never asked twice."""
        return self._sid[id(s)]

    def track(self, s) -> None:
        self._sid[id(s)] = s.slide_id

    def pos(self, s) -> int:
        """Current index of a slide in the live deck order."""
        return next(i for i, x in enumerate(self.order) if self.sid(x) == self.sid(s))

    def slide(self, n, errs):
        if not 1 <= n <= len(self.orig):
            errs.append(f"slide {n} out of range: the original deck has {len(self.orig)} slides (valid 1..{len(self.orig)})")
            return None
        s = self.orig[n - 1]
        if s.slide_id in self.deleted:  # keyed by id: python-pptx proxies are unhashable
            errs.append(f"slide {n} was deleted by an earlier op")
            return None
        return s

    def shape(self, slide, n, key, errs):
        shapes = list(slide.shapes)
        if key.startswith("idx:"):
            try:
                idx = int(key[4:])
            except ValueError:
                errs.append(f"bad shape key '{key}'")
                return None
            for s in shapes:
                if s.is_placeholder and s.placeholder_format.idx == idx:
                    return s
            avail = [f"idx:{s.placeholder_format.idx}" for s in shapes if s.is_placeholder]
            errs.append(f"slide {n} has no placeholder '{key}'; placeholders: {avail}")
            return None
        hits = [s for s in shapes if s.name == key]
        if not hits:
            errs.append(f"slide {n} has no shape named '{key}'; shapes: {[s.name for s in shapes]}")
            return None
        if len(hits) > 1:
            self.warnings.append(f"slide {n}: {len(hits)} shapes named '{key}'; using the first")
        return hits[0]


def _all_text_paragraphs(shape):
    """Every a:p inside a shape (text frames, table cells, groups)."""
    return list(shape._element.iter(qn("a:p")))


def _renumber(prs) -> None:
    """Give slide parts contiguous names in current list order. python-pptx only does this once (lazily);
    after a deletion, count-based naming of the next new slide would collide with a surviving slide
    and produce a package with two parts of the same name."""
    prs.part.rename_slide_parts([e.rId for e in prs.slides._sldIdLst])


def _section_lists(prs):
    ns = "{http://schemas.microsoft.com/office/powerpoint/2010/main}"
    return prs.part._element.findall(f".//{ns}section/{ns}sldIdLst")


def _register_in_section(prs, new_id: int, near_id: int | None) -> None:
    """If the deck uses sections, a new slide must belong to one (else PowerPoint may 'repair' the file)."""
    lists = _section_lists(prs)
    if not lists:
        return
    target = lists[-1]
    if near_id is not None:
        for lst in lists:
            if any(e.get("id") == str(near_id) for e in lst):
                target = lst
                break
    el = target.makeelement(P14_SLDID, {"id": str(new_id)})
    near = next((e for e in target if e.get("id") == str(near_id)), None)
    (near.addnext(el) if near is not None else target.append(el))


def _unregister_from_sections(prs, slide_id: int) -> None:
    for lst in _section_lists(prs):
        for e in list(lst):
            if e.get("id") == str(slide_id):
                lst.remove(e)


def _duplicate(prs, src):
    new = prs.slides.add_slide(src.slide_layout)
    tree = new.shapes._spTree
    for el in list(tree):
        if el.tag in [qn(t) for t in SHAPE_TAGS]:
            tree.remove(el)
    rid_map = {}
    for rid, rel in src.part.rels.items():
        if rel.reltype in SKIP_RELS:
            continue
        if rel.reltype == RT.IMAGE:
            rid_map[rid] = new.part.relate_to(rel.target_part, rel.reltype)
        elif rel.reltype == RT.HYPERLINK and rel.is_external:
            rid_map[rid] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
    src_tree = src.shapes._spTree
    for el in src_tree:
        if el.tag in [qn(t) for t in SHAPE_TAGS]:
            clone = copy.deepcopy(el)
            for node in clone.iter():
                for k, v in list(node.attrib.items()):
                    if k.startswith("{" + R_NS + "}") and v in rid_map:
                        node.set(k, rid_map[v])
            tree.append(clone)
    bg = src._element.find(qn("p:cSld") + "/" + qn("p:bg"))
    if bg is not None:
        new._element.find(qn("p:cSld")).insert(0, copy.deepcopy(bg))
    return new


def _unsupported_for_duplicate(src) -> list[str]:
    """Relationships a duplicate cannot safely share (charts, embedded objects, media would alias the original)."""
    xml_rids = {v for n in src.shapes._spTree.iter() for k, v in n.attrib.items() if k.startswith("{" + R_NS + "}")}
    bad = []
    for rid, rel in src.part.rels.items():
        if rid in xml_rids and rel.reltype not in COPYABLE_RELS | SKIP_RELS:
            bad.append(rel.reltype.rsplit("/", 1)[-1])
    return bad


def _resolve(ctx: _Ctx, op, errs: list[str]):
    k = op.op
    if k == "pptx.replace_all":
        def do():
            n = 0
            for s in ctx.order:
                for p in s.shapes._spTree.iter(qn("a:p")):
                    c, _ = replace_in_nodes(list(p.iter(qn("a:t"))), op.find, op.replace)
                    n += c
            if n == 0:
                ctx.warnings.append(f"replace_all: '{op.find}' not found")
        return do
    if k == "pptx.add_slide":
        layout = ctx.layouts.get(op.layout)
        if layout is None:
            errs.append(f"layout '{op.layout}' not found; available: {sorted(ctx.layouts)}")
            return None
        # python-pptx does not instantiate date/footer/slide-number placeholders on new slides, so text for them
        # would be silently dropped; only offer (and accept) the placeholders that really get created.
        idxs = {str(p.placeholder_format.idx): p.placeholder_format.type for p in layout.placeholders
                if p.placeholder_format.type not in SKIPPED_PLACEHOLDERS}
        bad = [i for i in op.texts if i not in idxs]
        if bad:
            errs.append(f"layout '{op.layout}' has no fillable placeholder idx {bad}; available idx: {sorted(idxs)}")
            return None
        if op.position is not None and not 1 <= op.position <= ctx.n + 1:
            errs.append(f"position {op.position} invalid: deck will have {ctx.n} slides (valid 1..{ctx.n + 1})")
            return None
        pos = op.position
        ctx.n += 1

        def do():
            _renumber(ctx.prs)
            s = ctx.prs.slides.add_slide(layout)
            ctx.track(s)
            for ph in list(s.placeholders):
                key = str(ph.placeholder_format.idx)
                if key in op.texts:
                    set_body_text(ph.text_frame._txBody, op.texts[key])
                else:
                    ph._element.getparent().remove(ph._element)  # drop unfilled placeholders (no "Click to add…")
            at = (pos - 1) if pos else len(ctx.order)
            ctx.order.insert(at, s)
            near = ctx.sid(ctx.order[at - 1]) if at > 0 else None
            _register_in_section(ctx.prs, ctx.sid(s), near)
        return do
    # ---- everything below targets an existing (original) slide
    s = ctx.slide(op.slide, errs)
    if s is None:
        return None
    if k == "pptx.delete_slide":
        ctx.deleted.add(s.slide_id)
        ctx.n -= 1

        def do():
            lst = ctx.prs.slides._sldIdLst
            el = next(e for e in lst if int(e.get("id")) == ctx.sid(s))
            ctx.prs.part.drop_rel(el.rId)
            lst.remove(el)
            _unregister_from_sections(ctx.prs, ctx.sid(s))
            del ctx.order[ctx.pos(s)]
        return do
    if k == "pptx.duplicate_slide":
        bad = _unsupported_for_duplicate(s)
        if bad:
            errs.append(f"slide {op.slide} contains {sorted(set(bad))} which cannot be duplicated safely (the copy would alias the original)")
            return None
        if op.position is not None and not 1 <= op.position <= ctx.n + 1:
            errs.append(f"position {op.position} invalid: deck will have {ctx.n} slides (valid 1..{ctx.n + 1})")
            return None
        pos = op.position
        ctx.n += 1

        def do():
            _renumber(ctx.prs)
            new = _duplicate(ctx.prs, s)
            ctx.track(new)
            at = (pos - 1) if pos else ctx.pos(s) + 1
            ctx.order.insert(at, new)
            _register_in_section(ctx.prs, ctx.sid(new), ctx.sid(s))
        return do
    if k == "pptx.move_slide":
        if not 1 <= op.to <= ctx.n:
            errs.append(f"move target {op.to} invalid: deck will have {ctx.n} slides (valid 1..{ctx.n})")
            return None

        def do():
            del ctx.order[ctx.pos(s)]
            ctx.order.insert(op.to - 1, s)
        return do
    shape = ctx.shape(s, op.slide, op.shape, errs)
    if shape is None:
        return None
    if k == "pptx.set_text":
        if getattr(shape, "has_table", False) and shape.has_table:
            errs.append(f"shape '{op.shape}' on slide {op.slide} is a table; use pptx.set_cell")
            return None
        if not getattr(shape, "has_text_frame", False) or not shape.has_text_frame:
            errs.append(f"shape '{op.shape}' on slide {op.slide} has no text frame (kind: {shape.shape_type})")
            return None
        return lambda: set_body_text(shape.text_frame._txBody, op.text)
    if k == "pptx.set_cell":
        if not (getattr(shape, "has_table", False) and shape.has_table):
            errs.append(f"shape '{op.shape}' on slide {op.slide} is not a table")
            return None
        t = shape.table
        if not (0 <= op.row < len(t.rows) and 0 <= op.col < len(t.columns)):
            errs.append(f"cell ({op.row},{op.col}) out of range: table is {len(t.rows)}x{len(t.columns)}")
            return None
        return lambda: set_body_text(t.cell(op.row, op.col).text_frame._txBody, op.text)
    errs.append(f"unknown op {k}")
    return None


def _prepare(prs, plan: EditPlan):
    ctx, errs, actions = _Ctx(prs), [], []
    for i, op in enumerate(plan.ops):
        local: list[str] = []
        fn = _resolve(ctx, op, local)
        errs += [f"op {i} ({op.op}): {e}" for e in local]
        if fn is not None:
            actions.append((i, op, fn))
    return ctx, errs, actions


def validate_pptx_plan(src: str | Path, plan: EditPlan) -> list[str]:
    _, errs, _ = _prepare(open_presentation(Path(src)), plan)
    return errs


def render_pptx(src: str | Path, plan: EditPlan, dst: str | Path) -> RenderResult:
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("refusing to overwrite the source file; write a new version")
    prs = open_presentation(Path(src))
    ctx, errs, actions = _prepare(prs, plan)
    if errs:
        raise PlanError(errs)
    res = RenderResult()
    for i, op, fn in actions:
        fn()
        res.applied += 1
        res.details.append(f"op {i} {op.op} ok")
    # write the final slide order into presentation.xml
    lst = prs.slides._sldIdLst
    order_ids = [ctx.sid(s) for s in ctx.order]
    by_id = {int(e.get("id")): e for e in lst}
    for e in list(lst):
        lst.remove(e)
    for sid in order_ids:
        lst.append(by_id[sid])
    _renumber(prs)  # final part names follow the final slide order
    res.warnings = ctx.warnings
    prs.save(str(dst))
    return res
