"""DOCX template analyzer: file -> structured Template Profile (JSON-serialisable dict).

Everything here is deterministic (no LLM). The profile records what the file *actually*
contains so downstream edit planning can't hallucinate layout.
"""
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Length

from app.document_engine.common import clean, emu_to_in, sha256, twips_to_in

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
BORDER_SIDES = ("top", "left", "bottom", "right", "insideH", "insideV")
WRAP_TAGS = ("wrapNone", "wrapSquare", "wrapTight", "wrapThrough", "wrapTopAndBottom")


# ---------- theme ----------
def _theme(doc) -> dict:
    for part in doc.part.package.iter_parts():
        if str(part.partname).startswith("/word/theme/") and str(part.partname).endswith(".xml"):
            from lxml import etree

            root = etree.fromstring(part.blob)
            fonts = {}
            for kind in ("major", "minor"):
                el = root.find(f".//a:{kind}Font/a:latin", NS)
                fonts[kind] = el.get("typeface") if el is not None else None
            colors = {}
            scheme = root.find(".//a:clrScheme", NS)
            if scheme is not None:
                for c in scheme:
                    name = etree.QName(c).localname
                    child = c[0] if len(c) else None
                    if child is not None:
                        # srgbClr@val is hex; sysClr@val is a name ("window") and lastClr holds the hex.
                        kind = etree.QName(child).localname
                        colors[name] = child.get("val") if kind == "srgbClr" else child.get("lastClr")
            return {"fonts": fonts, "colors": colors}
    return {"fonts": {}, "colors": {}}


# ---------- fonts / styles ----------
def _color(font) -> str | None:
    c = font.color
    if c is None or c.type is None:
        return None
    if c.rgb is not None:
        return str(c.rgb)
    return f"theme:{c.theme_color}" if c.theme_color is not None else None


def _style_chain(style):
    while style is not None:
        yield style
        style = style.base_style


def _doc_defaults(doc, theme) -> dict:
    rpr = doc.styles.element.find("w:docDefaults/w:rPrDefault/w:rPr", NS)
    out = {}
    if rpr is None:
        return out
    rf = rpr.find("w:rFonts", NS)
    if rf is not None:
        name = rf.get(qn("w:ascii"))
        theme_ref = rf.get(qn("w:asciiTheme"))
        if not name and theme_ref:
            name = theme["fonts"].get("major" if theme_ref.startswith("major") else "minor")
        out["name"] = name
    sz = rpr.find("w:sz", NS)
    if sz is not None:
        out["size_pt"] = int(sz.get(qn("w:val"))) / 2
    return out


def _effective_font(style, defaults) -> dict:
    def first(getter):
        for s in _style_chain(style):
            v = getter(s.font)
            if v is not None:
                return v
        return None

    size = first(lambda f: f.size)
    return clean({
        "name": first(lambda f: f.name) or defaults.get("name"),
        "size_pt": size.pt if size is not None else defaults.get("size_pt"),
        "bold": first(lambda f: f.bold),
        "italic": first(lambda f: f.italic),
        "color": first(_color),
    })


def _pfmt(style) -> dict:
    def first(attr):
        for s in _style_chain(style):
            v = getattr(s.paragraph_format, attr, None)
            if v is not None:
                return v
        return None

    def pt(v):
        return round(v.pt, 1) if isinstance(v, Length) else v

    al = first("alignment")
    return clean({
        "alignment": al.name if isinstance(al, WD_ALIGN_PARAGRAPH) else (str(al) if al is not None else None),
        "space_before_pt": pt(first("space_before")),
        "space_after_pt": pt(first("space_after")),
        "line_spacing": pt(first("line_spacing")),
    })


def _run_fmt(run) -> dict:
    f = run.font
    return clean({
        "name": f.name, "size_pt": f.size.pt if f.size else None, "bold": f.bold,
        "italic": f.italic, "underline": f.underline if f.underline else None, "color": _color(f),
    })


# ---------- images ----------
def _images_in(element, part) -> list[dict]:
    out = []
    for d in element.iter(qn("w:drawing")):
        box = d[0] if len(d) else None
        if box is None:
            continue
        kind = "floating" if box.tag == qn("wp:anchor") else "inline"
        ext = box.find("wp:extent", NS)
        blip = box.find(".//a:blip", NS)
        docpr = box.find("wp:docPr", NS)
        info = {"placement": kind, "name": docpr.get("name") if docpr is not None else None,
                "alt_text": docpr.get("descr") if docpr is not None else None}
        if ext is not None:
            info["width_in"], info["height_in"] = emu_to_in(ext.get("cx")), emu_to_in(ext.get("cy"))
        if blip is not None:
            rid = blip.get(qn("r:embed"))
            try:
                ip = part.related_parts[rid]
                info.update(content_type=ip.content_type, sha256=sha256(ip.blob), bytes=len(ip.blob),
                            file=str(ip.partname).lstrip("/"))
            except KeyError:
                info["error"] = f"unresolved relationship {rid}"
        if kind == "floating":
            for axis in ("H", "V"):
                pos = box.find(f"wp:position{axis}", NS)
                if pos is not None:
                    off, al = pos.find("wp:posOffset", NS), pos.find("wp:align", NS)
                    info[f"pos_{axis.lower()}"] = clean({
                        "relative_from": pos.get("relativeFrom"),
                        "offset_in": emu_to_in(off.text) if off is not None else None,
                        "align": al.text if al is not None else None,
                    })
            info["wrap"] = next((t for t in WRAP_TAGS if box.find(f"wp:{t}", NS) is not None), None)
            info["behind_text"] = box.get("behindDoc") in ("1", "true")
        out.append(clean(info))
    return out


# ---------- tables ----------
def _borders(tbl_el) -> dict:
    node = tbl_el.find("w:tblPr/w:tblBorders", NS)
    out = {}
    if node is None:
        return out
    for side in BORDER_SIDES:
        b = node.find(f"w:{side}", NS)
        if b is not None:
            sz = b.get(qn("w:sz"))
            color = b.get(qn("w:color"))
            out[side] = clean({"style": b.get(qn("w:val")), "size_eighths_pt": int(sz) if sz else None,
                               "color": None if color in (None, "auto") else color})
    return out


def _table(tbl, index: int) -> dict:
    el = tbl._tbl
    grid = [twips_to_in(g.get(qn("w:w"))) for g in el.findall("w:tblGrid/w:gridCol", NS)]
    merged = []
    shading = {}
    for ri, row in enumerate(tbl.rows):
        for ci, tc in enumerate(row._tr.findall("w:tc", NS)):
            span = tc.find("w:tcPr/w:gridSpan", NS)
            vm = tc.find("w:tcPr/w:vMerge", NS)
            if span is not None or vm is not None:
                merged.append(clean({"row": ri, "cell": ci, "col_span": int(span.get(qn("w:val"))) if span is not None else None,
                                     "v_merge": (vm.get(qn("w:val")) or "continue") if vm is not None else None}))
            shd = tc.find("w:tcPr/w:shd", NS)
            if shd is not None and shd.get(qn("w:fill")) not in (None, "auto"):
                shading[f"{ri},{ci}"] = shd.get(qn("w:fill"))
    first_row = [c.text for c in tbl.rows[0].cells] if tbl.rows else []
    return clean({
        "index": index, "style": tbl.style.name if tbl.style is not None else None,
        "rows": len(tbl.rows), "cols": len(tbl.columns), "column_widths_in": grid,
        "borders": _borders(el), "cell_shading": shading, "merged_cells": merged,
        "header_row_text": first_row,
    })


# ---------- headers / footers ----------
def _hf(hf) -> dict:
    if hf.is_linked_to_previous:
        return {"linked_to_previous": True}
    el = hf._element
    instr = " ".join((t.text or "") for t in el.iter(qn("w:instrText")))
    simple = " ".join(f.get(qn("w:instr")) or "" for f in el.iter(qn("w:fldSimple")))
    fields = (instr + " " + simple).upper()
    return {
        "text": [p.text for p in hf.paragraphs if p.text.strip()],
        "alignment": [p.alignment.name if p.alignment is not None else None for p in hf.paragraphs][:3],
        "images": _images_in(el, hf.part),
        "tables": len(hf.tables),
        "has_page_number": "PAGE" in fields,
    }


def _section(i: int, s) -> dict:
    cols = s._sectPr.find("w:cols", NS)
    return {
        "index": i, "orientation": s.orientation.name, "start_type": s.start_type.name,
        "page_width_in": emu_to_in(s.page_width), "page_height_in": emu_to_in(s.page_height),
        "margins_in": {k: emu_to_in(getattr(s, f"{k}_margin")) for k in ("top", "bottom", "left", "right")}
        | {"header": emu_to_in(s.header_distance), "footer": emu_to_in(s.footer_distance), "gutter": emu_to_in(s.gutter)},
        "columns": int(cols.get(qn("w:num"))) if cols is not None and cols.get(qn("w:num")) else 1,
        "different_first_page": bool(s.different_first_page_header_footer),
        "header": _hf(s.header), "footer": _hf(s.footer),
        "first_page_header": _hf(s.first_page_header) if s.different_first_page_header_footer else None,
        "first_page_footer": _hf(s.first_page_footer) if s.different_first_page_header_footer else None,
    }


# ---------- main entry ----------
def analyze_docx(path: str | Path) -> dict:
    doc = Document(str(path))
    theme = _theme(doc)
    defaults = _doc_defaults(doc, theme)

    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body, outline, images, tables = [], [], [], []
    styles_used: dict[str, dict] = {}
    p_i = t_i = 0
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            st = p.style
            sname = st.name if st is not None else "Normal"
            if st is not None and sname not in styles_used:
                styles_used[sname] = {"type": "paragraph", "based_on": st.base_style.name if st.base_style else None,
                                      "font": _effective_font(st, defaults), "paragraph": _pfmt(st)}
            imgs = _images_in(child, doc.part)
            for im in imgs:
                images.append({**im, "paragraph_index": p_i})
            level = None
            if sname.lower().startswith("heading "):
                try:
                    level = int(sname.split()[-1])
                except ValueError:
                    pass
            if sname == "Title":
                level = 0
            if level is not None:
                outline.append({"level": level, "text": p.text, "paragraph_index": p_i})
            num = child.find("w:pPr/w:numPr", NS) is not None
            body.append(clean({
                "type": "paragraph", "index": p_i, "style": sname, "text": p.text[:120],
                "alignment": p.alignment.name if p.alignment is not None else None,
                "list_item": num or None, "images": len(imgs) or None,
                "direct_runs": [_run_fmt(r) for r in p.runs if _run_fmt(r)][:4] or None,
            }))
            p_i += 1
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, doc)
            prof = _table(tbl, t_i)
            tables.append(prof)
            body.append({"type": "table", "index": t_i, "rows": prof["rows"], "cols": prof["cols"]})
            if tbl.style is not None and tbl.style.name not in styles_used:
                styles_used[tbl.style.name] = {"type": "table"}
            t_i += 1

    sections = [_section(i, s) for i, s in enumerate(doc.sections)]
    for s in sections:  # surface header/footer images in the global inventory too
        for where in ("header", "footer"):
            for im in (s[where].get("images") or []):
                images.append({**im, "location": f"section{s['index']}.{where}"})

    cp = doc.core_properties
    return {
        "format": "docx",
        "metadata": clean({"title": cp.title or None, "author": cp.author or None}),
        "theme": theme,
        "defaults": defaults,
        "sections": sections,
        "styles_used": styles_used,
        "outline": outline,
        "body": body,
        "tables": tables,
        "images": images,
        "counts": {"paragraphs": p_i, "tables": t_i, "images": len(images), "sections": len(sections)},
    }
