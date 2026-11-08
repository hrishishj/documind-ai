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


def summarize_profile(profile: dict) -> dict:
    fmt = profile.get("format")
    if fmt == "docx":
        return summarize_docx(profile)
    if fmt == "xlsx":
        return summarize_xlsx(profile)
    raise ValueError(f"No summarizer for format {fmt!r}")


def summary_json(profile: dict) -> str:
    return json.dumps(summarize_profile(profile), separators=(",", ":"))
