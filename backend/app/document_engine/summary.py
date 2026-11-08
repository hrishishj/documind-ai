"""Bounded-size views of a Template Profile for LLM prompts.

The full profile is stored in the DB and used by deterministic code; the LLM only ever
sees this summary, so prompt size stays predictable no matter how large the document is.
"""
import json

MAX_OUTLINE = 40
MAX_TABLES = 15
MAX_BODY = 30
MAX_IMAGES = 20


def summarize_docx(p: dict) -> dict:
    sections = []
    for s in p["sections"]:
        sections.append({k: s[k] for k in ("index", "orientation", "page_width_in", "page_height_in", "margins_in",
                                            "columns", "different_first_page", "header", "footer") if k in s})
    tables = []
    for t in p["tables"][:MAX_TABLES]:
        tables.append({k: t[k] for k in ("index", "rows", "cols", "style", "borders", "cell_borders",
                                         "shading_summary", "merged_count", "header_row_text") if k in t})
    return {
        "format": "docx",
        "counts": p["counts"],
        "theme_fonts": p["theme"].get("fonts"),
        "defaults": p["defaults"],
        "sections": sections,
        "styles_used": p["styles_used"],
        "formatting_census": p["formatting_census"],
        "outline": p["outline"][:MAX_OUTLINE],
        "outline_truncated": len(p["outline"]) > MAX_OUTLINE,
        "tables": tables,
        "tables_truncated": len(p["tables"]) > MAX_TABLES,
        "images": p["images"][:MAX_IMAGES],
        "body_sample": p["body"][:MAX_BODY],
        "body_total_items": len(p["body"]),
    }


MAX_SHEETS_DETAIL = 6


def summarize_xlsx(p: dict) -> dict:
    keep = ("name", "state", "dimensions", "tab_color", "freeze_panes", "merged_count", "merged_ranges",
            "column_widths", "header_row", "conditional_formatting", "data_validations", "images", "tables",
            "page_setup", "style_census")
    detailed = [{k: s[k] for k in keep if k in s} for s in p["sheets"][:MAX_SHEETS_DETAIL]]
    for s in detailed:  # trim long lists again for prompt size
        s["merged_ranges"] = s.get("merged_ranges", [])[:10]
        if "header_row" in s:
            s["header_row"] = {**s["header_row"], "cells": dict(list(s["header_row"]["cells"].items())[:6])}
    return {
        "format": "xlsx", "source_format": p.get("source_format"), "counts": p["counts"],
        "sheet_names": p["sheet_names"][:100], "package_parts": p["package_parts"], "warnings": p["warnings"],
        "sheets_detailed": detailed, "sheets_omitted": max(0, len(p["sheets"]) - MAX_SHEETS_DETAIL),
    }


MAX_SLIDES_DETAIL = 25
MAX_LAYOUTS = 20


def _brief_shape(s: dict) -> dict:
    keep = ("name", "kind", "placeholder", "x_in", "y_in", "w_in", "h_in", "run_format")
    out = {k: s[k] for k in keep if k in s}
    if "text" in s:
        out["text"] = s["text"][:60]
    if "table" in s:
        out["table"] = {k: s["table"][k] for k in ("rows", "cols", "header_row_text", "first_row_flag") if k in s["table"]}
    if "chart" in s:
        out["chart"] = s["chart"]
    if "image" in s:
        out["image"] = {"content_type": s["image"].get("content_type"), "sha256": s["image"].get("sha256", "")[:12]}
    return out


def summarize_pptx(p: dict) -> dict:
    masters = []
    for m in p["masters"]:
        masters.append({
            "theme": m["theme"], "text_styles": m["text_styles"], "background": m.get("background"),
            "placeholders": [_brief_shape(s) for s in m["placeholders"]],
            "decor": [_brief_shape(s) for s in m["decor"]],
            "layouts": [{"name": l["name"], "used_by_slides": l["used_by_slides"],
                         "placeholders": [{"type": s["placeholder"]["type"], "idx": s["placeholder"]["idx"],
                                           **{k: s[k] for k in ("x_in", "y_in", "w_in", "h_in") if k in s}}
                                          for s in l["placeholders"]],
                         "decor": [_brief_shape(s) for s in l["decor"]]} for l in m["layouts"][:MAX_LAYOUTS]],
            "layouts_total": len(m["layouts"])})
    slides = [{"index": s["index"], "layout": s["layout"], "title": s.get("title"),
               "background_override": s.get("background_override"), "has_notes": s.get("has_notes"),
               "shapes": [_brief_shape(x) for x in s["shapes"][:12]], "shape_count": s["shape_count"]}
              for s in p["slides"][:MAX_SLIDES_DETAIL]]
    return {"format": "pptx", "slide_size": p["slide_size"], "counts": p["counts"], "masters": masters,
            "slides": slides, "slides_omitted": max(0, len(p["slides"]) - MAX_SLIDES_DETAIL),
            "formatting_census": p["formatting_census"], "package_parts": p["package_parts"], "warnings": p["warnings"]}


def summarize_profile(profile: dict) -> dict:
    fmt = profile.get("format")
    if fmt == "pptx":
        return summarize_pptx(profile)
    if fmt == "docx":
        return summarize_docx(profile)
    if fmt == "xlsx":
        return summarize_xlsx(profile)
    raise ValueError(f"No summarizer for format {fmt!r}")


def summary_json(profile: dict) -> str:
    return json.dumps(summarize_profile(profile), separators=(",", ":"))
