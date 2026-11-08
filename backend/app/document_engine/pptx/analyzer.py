"""PPTX template analyzer: slide masters, layouts, placeholders, shapes, images, theme -> profile dict."""
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.dml import MSO_FILL
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from app.document_engine.common import clean, emu_to_in, parse_theme_xml, sha256

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
MAX_SHAPES_PER_SLIDE = 60
MAX_TABLE_CELLS = 200


def _rgb(color) -> str | None:
    """Colour of a python-pptx ColorFormat: hex for RGB, 'theme:NAME' for theme colours."""
    try:
        if color.type is None:
            return None
        if color.type == 1:  # MSO_COLOR_TYPE.RGB
            return str(color.rgb)
        return f"theme:{color.theme_color.name}" if color.theme_color is not None else None
    except Exception:
        return None


def _box(sh) -> dict:
    try:
        vals = {"x_in": sh.left, "y_in": sh.top, "w_in": sh.width, "h_in": sh.height}
    except Exception:
        return {}
    return {k: emu_to_in(v) for k, v in vals.items() if v is not None}


def _kind(sh) -> str:
    if sh.is_placeholder:
        return "placeholder"
    try:
        t = sh.shape_type
    except Exception:
        return "unknown"
    return {MSO_SHAPE_TYPE.PICTURE: "picture", MSO_SHAPE_TYPE.TABLE: "table", MSO_SHAPE_TYPE.CHART: "chart",
            MSO_SHAPE_TYPE.GROUP: "group", MSO_SHAPE_TYPE.LINE: "line", MSO_SHAPE_TYPE.TEXT_BOX: "text_box",
            MSO_SHAPE_TYPE.AUTO_SHAPE: "auto_shape", MSO_SHAPE_TYPE.FREEFORM: "freeform"}.get(t, str(t).split(".")[-1].lower())


def _image(sh) -> dict | None:
    try:
        img = sh.image
    except Exception:
        return None
    return clean({"content_type": img.content_type, "sha256": sha256(img.blob), "bytes": len(img.blob),
                  "px": list(img.size) if img.size else None})


def _text_runs(sh, census: Counter | None) -> dict:
    """First-run formatting for display, plus text-weighted census of explicit run formatting."""
    first = None
    for p in sh.text_frame.paragraphs:
        for r in p.runs:
            f = r.font
            n = len(r.text or "")
            if census is not None and n:
                census["fonts"][f.name or "(inherited)"] += n
                census["sizes"][f.size.pt if f.size else "(inherited)"] += n
                census["colors"][_rgb(f.color) or "(inherited)"] += n
                census["total"] += n
            if first is None and n:
                first = clean({"font": f.name, "size_pt": f.size.pt if f.size else None, "bold": f.bold,
                               "italic": f.italic, "color": _rgb(f.color)})
    return first or {}


def _chart(ch) -> dict:
    """python-pptx can't classify some chart types (e.g. 3-D pie); fall back to the chart XML."""
    cns = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
    xml_types = [etree.QName(e).localname for e in ch._chartSpace.xpath(".//c:plotArea/*") if etree.QName(e).localname.endswith("Chart")]
    out = {"has_title": ch.has_title, "plot_types": xml_types, "series": len(ch._chartSpace.findall(".//c:ser", cns))}
    try:
        out["type"] = ch.chart_type.name if ch.chart_type is not None else None
    except Exception:
        out["type"] = None  # unsupported by python-pptx; plot_types above still tells the story
    return out


def _shape(sh, census: Counter | None, depth: int = 0) -> dict:
    try:
        return _shape_unsafe(sh, census, depth)
    except Exception as e:  # one odd shape must never abort analysis of the whole deck
        return {"id": getattr(sh, "shape_id", None), "name": getattr(sh, "name", "?"), "kind": "unreadable",
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _shape_unsafe(sh, census: Counter | None, depth: int = 0) -> dict:
    kind = _kind(sh)
    info = {"id": sh.shape_id, "name": sh.name, "kind": kind, **_box(sh)}
    if sh.is_placeholder:
        pf = sh.placeholder_format
        info["placeholder"] = {"type": pf.type.name if pf.type is not None else None, "idx": pf.idx}
    if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
        txt = sh.text_frame.text
        if txt.strip():
            info["text"] = txt[:100]
            info["run_format"] = _text_runs(sh, census)
    if kind in ("picture",) or (kind == "placeholder" and _image(sh)):
        info["image"] = _image(sh)
    if getattr(sh, "has_table", False) and sh.has_table:
        t = sh.table
        fills = Counter()
        for ri, row in enumerate(t.rows):
            for ci, cell in enumerate(row.cells):
                if ri * len(t.columns) + ci >= MAX_TABLE_CELLS:
                    break
                if cell.fill.type == MSO_FILL.SOLID:
                    fills[_rgb(cell.fill.fore_color) or "?"] += 1
        tblpr = t._tbl.tblPr
        style = tblpr.find("a:tableStyleId", NS)
        info["table"] = clean({
            "rows": len(t.rows), "cols": len(t.columns),
            "header_row_text": [c.text for c in t.rows[0].cells][:8] if len(t.rows) else [],
            "first_row_flag": tblpr.get("firstRow") == "1", "banded_rows": tblpr.get("bandRow") == "1",
            "style_id": style.text if style is not None else None, "cell_fills": dict(fills) or None})
    if getattr(sh, "has_chart", False) and sh.has_chart:
        info["chart"] = _chart(sh.chart)
    if kind == "group" and depth < 2:
        info["children"] = [_shape(c, census, depth + 1) for c in sh.shapes][:20]
    return clean(info)


def _background(obj) -> dict | None:
    try:
        fill = obj.background.fill
        if fill.type == MSO_FILL.SOLID:
            return {"type": "solid", "color": _rgb(fill.fore_color)}
        return {"type": fill.type.name} if fill.type is not None else None
    except Exception:
        return None


def _tx_styles(master) -> dict:
    out = {}
    for name in ("titleStyle", "bodyStyle", "otherStyle"):
        d = master._element.find(f".//p:txStyles/p:{name}/a:lvl1pPr/a:defRPr", NS)
        if d is None:
            continue
        latin = d.find("a:latin", NS)
        fill = d.find("a:solidFill/*", NS)
        out[name] = clean({
            "size_pt": int(d.get("sz")) / 100 if d.get("sz") else None, "bold": True if d.get("b") == "1" else None,
            "font": latin.get("typeface") if latin is not None else None,
            "color": fill.get("val") if fill is not None else None})
    return out


def _package_parts(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    n = lambda pre, suf=".xml": sum(1 for x in names if x.startswith(pre) and x.endswith(suf))  # noqa: E731
    return {"charts": n("ppt/charts/chart"), "media_files": sum(1 for x in names if x.startswith("ppt/media/")),
            "embeddings": sum(1 for x in names if x.startswith("ppt/embeddings/")), "notes_slides": n("ppt/notesSlides/notesSlide"),
            "slide_masters": n("ppt/slideMasters/slideMaster"), "slide_layouts": n("ppt/slideLayouts/slideLayout"),
            "has_vba": "ppt/vbaProject.bin" in names}


TEMPLATE_CT = b"application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
PRESENTATION_CT = b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def open_presentation(path: Path):
    """Open .pptx, or a .potx template (python-pptx rejects its content type; patched in memory only)."""
    if path.suffix.lower() != ".potx":
        return Presentation(str(path))
    import io

    src = io.BytesIO()
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(TEMPLATE_CT, PRESENTATION_CT)
            zout.writestr(item, data)
    src.seek(0)
    return Presentation(src)


def analyze_pptx(path: str | Path) -> dict:
    path = Path(path)
    prs = open_presentation(path)
    census = {"fonts": Counter(), "sizes": Counter(), "colors": Counter(), "total": 0}
    used = Counter(s.slide_layout.name for s in prs.slides)
    images, masters = [], []

    for mi, m in enumerate(prs.slide_masters):
        try:
            theme = parse_theme_xml(m.part.part_related_by(RT.THEME).blob)
        except Exception:
            theme = {"fonts": {}, "colors": {}}
        m_shapes = [_shape(s, None) for s in m.shapes]
        for s in m_shapes:
            if s.get("image"):
                images.append({**s, "location": f"master{mi}"})
        layouts = []
        for li, lay in enumerate(m.slide_layouts):
            shapes = [_shape(s, None) for s in lay.shapes]
            for s in shapes:
                if s.get("image"):
                    images.append({**s, "location": f"layout:{lay.name}"})
            layouts.append({
                "index": li, "name": lay.name, "used_by_slides": used.get(lay.name, 0),
                "placeholders": [s for s in shapes if s["kind"] == "placeholder"],
                "decor": [s for s in shapes if s["kind"] != "placeholder"],
                "background": _background(lay) if lay._element.find(".//p:bg", NS) is not None else None})
        masters.append({"index": mi, "name": m.name or None, "theme": theme, "text_styles": _tx_styles(m),
                        "background": _background(m), "placeholders": [s for s in m_shapes if s["kind"] == "placeholder"],
                        "decor": [s for s in m_shapes if s["kind"] != "placeholder"], "layouts": layouts})

    slides = []
    for i, sl in enumerate(prs.slides):
        shapes = [_shape(s, census) for s in sl.shapes]
        for s in shapes:
            if s.get("image"):
                images.append({**s, "location": f"slide{i + 1}"})
        title = sl.shapes.title.text if sl.shapes.title is not None else None
        slides.append(clean({
            "index": i + 1, "layout": sl.slide_layout.name, "title": title,
            "background_override": None if sl.follow_master_background else _background(sl),
            "has_notes": sl.has_notes_slide and bool(sl.notes_slide.notes_text_frame.text.strip()),
            "shapes": shapes[:MAX_SHAPES_PER_SLIDE], "shape_count": len(shapes)}))

    total = census["total"]
    top = lambda c: [{"value": k, "share": round(v / total, 3)} for k, v in c.most_common(6)] if total else []  # noqa: E731
    parts = _package_parts(path)
    prof = {
        "format": "pptx",
        "slide_size": {"width_in": emu_to_in(prs.slide_width), "height_in": emu_to_in(prs.slide_height),
                       "aspect": round(prs.slide_width / prs.slide_height, 3)},
        "masters": masters, "slides": slides, "images": images,
        "formatting_census": {"text_chars": total, "explicit_fonts": top(census["fonts"]),
                              "explicit_sizes_pt": top(census["sizes"]), "explicit_colors": top(census["colors"])},
        "package_parts": parts,
        "counts": {"slides": len(slides), "masters": len(masters), "layouts": sum(len(m["layouts"]) for m in masters),
                   "images": len(images)},
        "warnings": [],
    }
    if parts["has_vba"]:
        prof["warnings"].append("VBA macros present and not supported; .pptm files are rejected at upload.")
    return prof
