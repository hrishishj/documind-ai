"""Deterministic 'Sources' section for documents that were written using web research.

The model is never trusted to remember to cite: after it has produced its plan, these operations are appended so the
generated file itself says where its facts came from. They only use operations the renderer already validates, so the
section inherits the document's own formatting (cloned paragraph / the template's own layout).
"""
from app.research.search import clean_text, domain_of

BODY_TYPES = ("OBJECT", "BODY")
TITLE_TYPES = ("TITLE", "CENTER_TITLE")


def _line(i: int, s: dict, with_url: bool) -> str:
    title = clean_text(s.get("title", ""), 90)
    return f"[{i}] {title} — {s['url']}" if with_url else f"{title} — {domain_of(s['url'])}"


def docx_ops(profile: dict, sources: list[dict], deleted: set[int]) -> tuple[list[dict], str]:
    """Append at the END of the document (after any trailing table). The title clones the last heading, the entries clone
    the last ordinary body paragraph, so the section looks like the rest of the document."""
    paras = [b for b in profile["body"] if b["type"] == "paragraph" and b.get("text", "").strip() and b["index"] not in deleted]
    if not paras:
        return [], "no paragraph to take formatting from for a Sources section"
    is_heading = lambda b: b.get("style", "").lower().startswith(("heading", "title"))  # noqa: E731
    body = next((b for b in reversed(paras) if not is_heading(b) and not b.get("list_item")), None) \
        or next((b for b in reversed(paras) if not is_heading(b)), paras[-1])
    heading = next((b for b in reversed(paras) if is_heading(b)), body)
    ops = [{"op": "docx.append_paragraph", "like": heading["index"], "text": "Sources"}]
    ops += [{"op": "docx.append_paragraph", "like": body["index"], "text": _line(i, s, True)} for i, s in enumerate(sources, 1)]
    return ops, ""


def pptx_ops(profile: dict, sources: list[dict]) -> tuple[list[dict], str]:
    def usable(layout):
        types = {p["placeholder"]["type"]: str(p["placeholder"]["idx"]) for p in layout["placeholders"]}
        title = next((types[t] for t in TITLE_TYPES if t in types), None)
        body = next((types[t] for t in BODY_TYPES if t in types), None)
        return (title, body) if title and body else None

    layouts = [l for m in profile["masters"] for l in m["layouts"]]
    # prefer the template's ordinary "Title and Content" layout, else any layout that has a title and a body
    ordered = sorted(layouts, key=lambda l: (l["name"] != "Title and Content", l["name"] not in ("Title and Content",)))
    for l in ordered:
        found = usable(l)
        if found:
            title_idx, body_idx = found
            body = "\n".join(_line(i, s, False) for i, s in enumerate(sources, 1))
            return [{"op": "pptx.add_slide", "layout": l["name"], "texts": {title_idx: "Sources", body_idx: body}}], ""
    return [], "the template has no layout with a title and a body to hold a Sources slide"


def sources_ops(fmt: str, profile: dict, sources: list[dict], plan_ops: list[dict]) -> tuple[list[dict], str]:
    """(operations to append to the plan, note explaining why nothing was added — empty when something was)."""
    if not sources:
        return [], ""
    if fmt == "docx":
        deleted = {o["paragraph"] for o in plan_ops if o.get("op") == "docx.delete_paragraph"}
        return docx_ops(profile, sources, deleted)
    if fmt == "pptx":
        return pptx_ops(profile, sources)
    return [], "Excel workbooks do not get a Sources section; the sources are listed in the chat reply"
