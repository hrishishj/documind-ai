"""Turn uploaded documents into small, citable passages ("chunks") with exact locators.

Every chunk says WHERE it came from (paragraph range, table, slide, sheet rows, PDF page) so an answer can point the
user back to the source. Chunks are ~900 characters: small enough to embed precisely, big enough to carry context.
"""
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.document_engine.render import detect_format

TARGET = 900          # preferred chunk size in characters
HARD_MAX = 1400       # never exceed this
MAX_CHUNKS = 4000     # per file: bounds embedding time and vector storage


@dataclass
class Chunk:
    text: str
    locator: str                  # human-readable, shown in citations: "¶12-18", "Table 3", "Slide 4", "Sheet 'x' rows 5-19", "p.3"
    title: str = ""               # nearest heading / slide title / sheet name
    order: int = 0                # position within the file


def _clean(s: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", s).strip()


def _pack(parts: list[tuple[str, str]], locator_fn, title: str = "") -> list[Chunk]:
    """Greedily pack (marker, text) pieces into chunks of ~TARGET chars. locator_fn(first_marker, last_marker)."""
    out: list[Chunk] = []
    buf: list[str] = []
    first = last = ""
    size = 0
    for marker, text in parts:
        text = _clean(text)
        if not text:
            continue
        while len(text) > HARD_MAX:  # a single huge paragraph: split on sentence-ish boundaries
            cut = max(text.rfind(". ", 0, TARGET + 200), text.rfind("; ", 0, TARGET + 200), text.rfind(" ", 0, TARGET))
            cut = cut + 1 if cut > 200 else TARGET
            if buf:
                out.append(Chunk("\n".join(buf), locator_fn(first, last), title))
                buf, size = [], 0
            out.append(Chunk(text[:cut].strip(), locator_fn(marker, marker), title))
            text = text[cut:].strip()
        if buf and size + len(text) > TARGET:
            out.append(Chunk("\n".join(buf), locator_fn(first, last), title))
            buf, size = [], 0
        if not buf:
            first = marker
        buf.append(text)
        size += len(text) + 1
        last = marker
    if buf:
        out.append(Chunk("\n".join(buf), locator_fn(first, last), title))
    return out


# ------------------------------------------------------------------ DOCX
def _docx(path: Path) -> list[Chunk]:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(path))
    W = qn
    txt = lambda el: "".join(t.text or "" for t in el.iter(W("w:t")))  # noqa: E731
    chunks: list[Chunk] = []
    section: list[tuple[str, str]] = []
    heading = ""
    p_i = t_i = 0

    def flush():
        nonlocal section
        if section:
            chunks.extend(_pack(section, lambda a, b: f"¶{a}" if a == b else f"¶{a}-{b}", heading))
            section = []

    for child in doc.element.body.iterchildren():
        if child.tag == W("w:p"):
            ppr = child.find(W("w:pPr"))
            ps = ppr.find(W("w:pStyle")) if ppr is not None else None
            style = (ps.get(W("w:val")) if ps is not None else "") or ""
            text = txt(child)
            if style.lower().startswith(("heading", "title")) and text.strip():
                flush()
                heading = _clean(text)
            section.append((str(p_i), text))
            p_i += 1
        elif child.tag == W("w:tbl"):
            flush()
            rows = []
            for tr in child.findall(W("w:tr")):
                cells = [_clean(txt(tc)) for tc in tr.findall(W("w:tc"))]
                cells = [c for c in cells if c]
                if cells:
                    rows.append(" | ".join(dict.fromkeys(cells)))  # merged cells repeat; keep each value once
            if rows:
                chunks.extend(_pack([(str(i), r) for i, r in enumerate(rows)], lambda a, b, n=t_i: f"Table {n}", heading))
            t_i += 1
    flush()
    return chunks


# ------------------------------------------------------------------ PPTX
def _pptx(path: Path) -> list[Chunk]:
    from app.document_engine.pptx.analyzer import open_presentation

    prs = open_presentation(path)
    chunks: list[Chunk] = []
    for n, slide in enumerate(prs.slides, 1):
        title = _clean(slide.shapes.title.text) if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        pieces: list[tuple[str, str]] = []
        for sh in slide.shapes:
            try:
                if getattr(sh, "has_text_frame", False) and sh.has_text_frame and sh.text_frame.text.strip():
                    pieces.append((sh.name, sh.text_frame.text))
                elif getattr(sh, "has_table", False) and sh.has_table:
                    for r in sh.table.rows:
                        cells = [_clean(c.text) for c in r.cells if _clean(c.text)]
                        if cells:
                            pieces.append((sh.name, " | ".join(cells)))
            except Exception:  # noqa: BLE001 - one odd shape must not lose the whole slide
                continue
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            pieces.append(("notes", "Speaker notes: " + slide.notes_slide.notes_text_frame.text))
        for c in _pack(pieces, lambda a, b, n=n: f"Slide {n}", title):
            c.text = (f"[Slide {n}: {title}] " if title else f"[Slide {n}] ") + c.text
            chunks.append(c)
    return chunks


# ------------------------------------------------------------------ XLSX / XLS
def _num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return v.isoformat(sep=" ") if hasattr(v, "isoformat") else str(v)


def _xlsx(path: Path) -> list[Chunk]:
    from openpyxl import load_workbook

    from app.document_engine.xlsx.convert_xls import convert_xls_to_xlsx

    with tempfile.TemporaryDirectory() as td:
        src = path
        if path.suffix.lower() == ".xls":
            src = Path(td) / "c.xlsx"
            convert_xls_to_xlsx(path, src)
        wb = load_workbook(src, read_only=True, data_only=True)
        chunks: list[Chunk] = []
        for ws in wb.worksheets:
            rows: list[tuple[str, str]] = []
            for r_i, row in enumerate(ws.iter_rows(values_only=True), 1):
                cells = [_num(v).strip() for v in row if v is not None and str(v).strip() != ""]
                if cells:
                    rows.append((str(r_i), " | ".join(cells)))
            for c in _pack(rows, lambda a, b, s=ws.title: f"Sheet '{s}' row {a}" if a == b else f"Sheet '{s}' rows {a}-{b}", ws.title):
                c.text = f"[Sheet {ws.title}] " + c.text
                chunks.append(c)
        wb.close()
    return chunks


# ------------------------------------------------------------------ PDF / text
def _pdf(path: Path) -> list[Chunk]:
    import pymupdf

    chunks: list[Chunk] = []
    with pymupdf.open(str(path)) as doc:
        for n, page in enumerate(doc, 1):
            text = page.get_text("text")
            lines = [_clean(l) for l in text.splitlines()]
            paras = [(str(n), " ".join(l for l in block.split("\n") if l)) for block in re.split(r"\n\s*\n", "\n".join(lines))]
            chunks.extend(_pack(paras, lambda a, b, n=n: f"p.{n}"))
    return chunks


def _text(path: Path) -> list[Chunk]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    paras, start, buf = [], 1, []
    for i, line in enumerate(lines + [""], 1):
        if line.strip():
            if not buf:
                start = i
            buf.append(line)
        elif buf:
            paras.append((str(start), " ".join(buf)))
            buf = []
    return _pack(paras, lambda a, b: f"line {a}")


_EXTRACTORS = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx, "pdf": _pdf, "txt": _text}


def extract_chunks(path: str | Path) -> list[Chunk]:
    """All citable passages of a file, in document order. Raises on unreadable files."""
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    fmt = "pdf" if ext == "pdf" else "txt" if ext in ("txt", "md") else detect_format(path)
    chunks = _EXTRACTORS[fmt](path)
    chunks = [c for c in chunks if len(c.text.strip()) >= 3][:MAX_CHUNKS]
    for i, c in enumerate(chunks):
        c.order = i
    return chunks
