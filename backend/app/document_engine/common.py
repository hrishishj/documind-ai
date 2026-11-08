"""Shared helpers for the OOXML analyzers."""
import hashlib

EMU_PER_INCH = 914400
EMU_PER_PT = 12700


def emu_to_in(v: int | None) -> float | None:
    return None if v is None else round(int(v) / EMU_PER_INCH, 3)


def twips_to_in(v: int | None) -> float | None:
    return None if v is None else round(int(v) / 1440, 3)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_theme_xml(blob: bytes) -> dict:
    """Theme fonts + colours from a DrawingML theme part (shared by DOCX and PPTX)."""
    from lxml import etree

    a = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    root = etree.fromstring(blob)
    fonts = {}
    for kind in ("major", "minor"):
        el = root.find(f".//a:{kind}Font/a:latin", a)
        fonts[kind] = el.get("typeface") if el is not None else None
    colors = {}
    scheme = root.find(".//a:clrScheme", a)
    if scheme is not None:
        for c in scheme:
            child = c[0] if len(c) else None
            if child is not None:
                # srgbClr@val is hex; sysClr@val is a name ("window") and lastClr holds the hex.
                kind = etree.QName(child).localname
                colors[etree.QName(c).localname] = child.get("val") if kind == "srgbClr" else child.get("lastClr")
    return {"fonts": fonts, "colors": colors}


def replace_in_nodes(nodes: list, find: str, repl: str) -> tuple[int, set]:
    """Find/replace over consecutive text nodes (any element with `.text`), even when `find` spans nodes.

    The replacement lands in the node where the match starts, so it inherits that run's formatting.
    Returns (replacements, set of nodes whose text changed).
    """
    count, start, touched = 0, 0, set()
    texts = [n.text or "" for n in nodes]
    while True:
        full = "".join(texts)
        i = full.find(find, start)
        if i == -1:
            return count, touched
        j = i + len(find)
        pos, first, last = 0, None, None
        for k, s in enumerate(texts):
            a, b = pos, pos + len(s)
            if first is None and i < b:
                first = (k, i - a)
            if j <= b:
                last = (k, j - a)
                break
            pos = b
        (k1, o1), (k2, o2) = first, last
        if k1 == k2:
            texts[k1] = texts[k1][:o1] + repl + texts[k1][o2:]
        else:
            texts[k1] = texts[k1][:o1] + repl
            for k in range(k1 + 1, k2):
                texts[k] = ""
            texts[k2] = texts[k2][o2:]
        for k in range(k1, k2 + 1):
            nodes[k].text = texts[k]
            touched.add(nodes[k])
        count += 1
        start = i + len(repl)


def clean(d: dict) -> dict:
    """Drop None values so profiles stay compact for LLM prompts."""
    return {k: v for k, v in d.items() if v is not None}
