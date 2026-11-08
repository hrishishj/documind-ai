"""Compact plain-text view of a document for the planning LLM.

Formatting is deliberately absent: the renderer preserves design by construction, so the model only needs
structure + text to *target* edits. Plain lines cost far fewer tokens than the JSON profile (Groq's free tier
allows only 8,000 tokens/minute), and the view is cut to a character budget with relevance-based selection so
huge documents still fit.
"""
import re

STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "make", "change", "add", "new", "slide",
        "table", "row", "text", "please", "should", "would", "your", "have", "all", "sheet"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", s.lower()) if w not in STOP}


def _pick(items: list[tuple[int, str]], instruction: str, keep_first: int, keep_matches: int, always=lambda t: False):
    """Choose which (index, text) items to show: the first few, the ones matching the request, and flagged ones."""
    want = _words(instruction)
    chosen = {i for i, _ in items[:keep_first]}
    matches = sorted(((len(want & _words(t)), i) for i, t in items if want & _words(t)), reverse=True)[:keep_matches]
    chosen |= {i for _, i in matches}
    chosen |= {i for i, t in items if always(t)}
    return chosen


def _docx(p: dict, instruction: str) -> list[str]:
    out = ["DOCX. P<n> = top-level paragraph n (0-based). T<n> = table n; row/col are 0-based, col = cell position in the row."]
    paras = [(b["index"], b) for b in p["body"] if b["type"] == "paragraph"]
    texty = [(i, b.get("text", "")) for i, b in paras if b.get("text", "").strip()]
    heading = lambda t: False  # noqa: E731
    head_idx = {o["paragraph_index"] for o in p["outline"]}
    chosen = _pick(texty, instruction, keep_first=40, keep_matches=40) | head_idx
    by_idx = dict(paras)
    prev = None
    for i, t in texty:
        if i not in chosen:
            continue
        if prev is not None and i - prev > 1:
            out.append(f"  [... paragraphs {prev + 1}-{i - 1} omitted ...]")
        b = by_idx[i]
        out.append(f"P{i} [{b['style']}] {t[:90]}")
        prev = i
    if texty and prev is not None and prev < texty[-1][0]:
        out.append(f"  [... paragraphs {prev + 1}-{texty[-1][0]} omitted ...]")
    for t in p["tables"][:30]:
        rows = " / ".join(" | ".join(r) for r in t.get("preview", [])[:3] if any(r))
        out.append(f"T{t['index']}: {t['rows']}x{t['cols']} {rows[:200]}")
    if len(p["tables"]) > 30:
        out.append(f"  [... {len(p['tables']) - 30} more tables ...]")
    return out


def _pptx(p: dict, instruction: str) -> list[str]:
    out = [f"PPTX. Slide numbers are 1-based. Shapes are addressed by name, or 'idx:N' for placeholders. "
           f"Size {p['slide_size']['width_in']}x{p['slide_size']['height_in']} in."]
    layouts = []
    for m in p["masters"]:
        for l in m["layouts"]:
            ph = ", ".join(f"idx{x['placeholder']['idx']} {x['placeholder']['type']}" for x in l["placeholders"]
                           if x["placeholder"]["type"] not in ("DATE", "FOOTER", "SLIDE_NUMBER"))  # not fillable on new slides
            layouts.append(f'"{l["name"]}" ({ph})')
    out.append("Layouts: " + "; ".join(layouts))
    slides = [(s["index"], (s.get("title") or "") + " " + " ".join(x.get("text", "") for x in s["shapes"])) for s in p["slides"]]
    chosen = _pick(slides, instruction, keep_first=14, keep_matches=10)
    prev = None
    for s in p["slides"]:
        if s["index"] not in chosen:
            continue
        if prev is not None and s["index"] - prev > 1:
            out.append(f"  [... slides {prev + 1}-{s['index'] - 1} omitted ...]")
        out.append(f"S{s['index']} [{s['layout']}] title={s.get('title')!r}")
        for sh in s["shapes"][:10]:
            ph = f" idx:{sh['placeholder']['idx']}" if "placeholder" in sh else ""
            extra = ""
            if "table" in sh:
                extra = f" table {sh['table']['rows']}x{sh['table']['cols']} header={sh['table']['header_row_text'][:4]}"
            if "chart" in sh:
                extra += f" chart({','.join(sh['chart'].get('plot_types', []))})"
            txt = f": {sh['text'][:60]!r}" if sh.get("text") else ""
            out.append(f"  - {sh['name']!r} {sh['kind']}{ph}{extra}{txt}")
        prev = s["index"]
    if prev is not None and prev < len(p["slides"]):
        out.append(f"  [... slides {prev + 1}-{len(p['slides'])} omitted ...]")
    return out


def _xlsx(p: dict, instruction: str) -> list[str]:
    out = ["XLSX. Address cells like A1. append_row adds below the last data row and copies its style."]
    want = _words(instruction)
    sheets = p["sheets"]
    order = sorted(range(len(sheets)), key=lambda i: (0 if want & _words(sheets[i]["name"]) else 1, i))[:8]
    for i in sorted(order):
        s = sheets[i]
        tbl = ", ".join(f"{t['name']} {t['ref']}" for t in s.get("tables", []) or [])
        merged = ", ".join(s.get("merged_ranges", [])[:6])
        out.append(f"Sheet {s['name']!r} range {s.get('dimensions')} last_data_row={s.get('last_data_row')}"
                   + (f" state={s['state']}" if s.get("state") != "visible" else "")
                   + (f" tables: {tbl}" if tbl else "") + (f" merged: {merged}" if merged else ""))
        for r in s.get("preview", []):
            out.append("  " + " | ".join(f"{k}={v}" for k, v in r["values"].items()))
    if len(sheets) > len(order):
        out.append(f"[... {len(sheets) - len(order)} more sheets: {', '.join(repr(s['name']) for s in sheets if s not in [sheets[i] for i in order])[:300]}]")
    return out


_VIEWS = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx}


def build_planner_view(profile: dict, instruction: str = "", budget_chars: int = 6000) -> str:
    text = "\n".join(_VIEWS[profile["format"]](profile, instruction))
    return text if len(text) <= budget_chars else text[:budget_chars] + "\n[view truncated]"
