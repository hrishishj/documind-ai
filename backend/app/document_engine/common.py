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


def clean(d: dict) -> dict:
    """Drop None values so profiles stay compact for LLM prompts."""
    return {k: v for k, v in d.items() if v is not None}
