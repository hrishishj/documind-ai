"""Post-render preservation check: did the edit leave the template's design intact?

Re-analyzes the output and compares template-level invariants with the source profile. Content (text, rows,
slides) is allowed to change; the design system is not.
"""


def _strip(d: dict, drop: tuple) -> dict:
    return {k: v for k, v in d.items() if k not in drop}


def _docx(a: dict, b: dict) -> list[str]:
    v = []
    for key in ("theme", "defaults"):
        if a[key] != b[key]:
            v.append(f"{key} changed")
    for name, st in a["styles_used"].items():
        if name in b["styles_used"] and b["styles_used"][name] != st:
            v.append(f"style '{name}' changed: {st} -> {b['styles_used'][name]}")
    if len(a["sections"]) != len(b["sections"]):
        v.append("section count changed")
    else:
        for sa, sb in zip(a["sections"], b["sections"]):
            for k in ("orientation", "page_width_in", "page_height_in", "margins_in", "columns", "different_first_page"):
                if sa.get(k) != sb.get(k):
                    v.append(f"section {sa['index']} {k}: {sa.get(k)} -> {sb.get(k)}")
            for part in ("header", "footer"):  # text may legitimately change; layout and images may not
                ha, hb = sa[part], sb[part]
                if _strip(ha, ("text",)) != _strip(hb, ("text",)):
                    v.append(f"section {sa['index']} {part} structure changed")
    lost = {i["sha256"] for i in a["images"] if "sha256" in i} - {i["sha256"] for i in b["images"] if "sha256" in i}
    if lost:
        v.append(f"{len(lost)} source image(s) no longer present")
    for ta in a["tables"]:
        tb = next((t for t in b["tables"] if t["index"] == ta["index"]), None)
        if tb is not None and (ta.get("borders") != tb.get("borders")):
            v.append(f"table {ta['index']} borders changed")
    fa, fb = a["formatting_census"], b["formatting_census"]
    if fa["fonts"] and fb["fonts"] and fa["fonts"][0]["value"] != fb["fonts"][0]["value"]:
        v.append(f"dominant font changed: {fa['fonts'][0]['value']} -> {fb['fonts'][0]['value']}")
    return v


def _pptx(a: dict, b: dict) -> list[str]:
    v = []
    if a["slide_size"] != b["slide_size"]:
        v.append("slide size changed")

    def design(masters):  # everything about masters/layouts except how many slides use them
        out = []
        for m in masters:
            m = dict(m)
            m["layouts"] = [_strip(l, ("used_by_slides",)) for l in m["layouts"]]
            out.append(m)
        return out
    da, db = design(a["masters"]), design(b["masters"])
    if len(da) != len(db):
        v.append("master count changed")
    else:
        for i, (x, y) in enumerate(zip(da, db)):
            for k in ("theme", "text_styles", "background", "placeholders", "decor"):
                if x.get(k) != y.get(k):
                    v.append(f"master {i} {k} changed")
            if x["layouts"] != y["layouts"]:
                names = [l["name"] for l, m in zip(x["layouts"], y["layouts"]) if l != m]
                v.append(f"master {i} layouts changed: {names or 'count differs'}")
    return v


def _xlsx(a: dict, b: dict) -> list[str]:
    v = []
    missing = [n for n in a["sheet_names"] if n not in b["sheet_names"]]
    if missing:
        v.append(f"sheets missing: {missing}")
    sb = {s["name"]: s for s in b["sheets"]}
    for s in a["sheets"]:
        t = sb.get(s["name"])
        if t is None:
            continue
        for k in ("merged_ranges", "column_widths", "freeze_panes", "tab_color", "state", "page_setup", "gridlines"):
            if s.get(k) != t.get(k):
                v.append(f"sheet '{s['name']}' {k} changed")
        ta = [x["name"] for x in (s.get("tables") or [])]
        tb = [x["name"] for x in (t.get("tables") or [])]
        if ta != tb:
            v.append(f"sheet '{s['name']}' tables changed: {ta} -> {tb}")
    return v


_CHECKS = {"docx": _docx, "pptx": _pptx, "xlsx": _xlsx}


def check_preservation(before: dict, after: dict) -> dict:
    """Return {"ok": bool, "violations": [...]} comparing two profiles of the same format."""
    if before["format"] != after["format"]:
        return {"ok": False, "violations": [f"format changed: {before['format']} -> {after['format']}"]}
    v = _CHECKS[before["format"]](before, after)
    return {"ok": not v, "violations": v}
