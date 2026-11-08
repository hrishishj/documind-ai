import json

import pytest

from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.samples import build_docx


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    p = tmp_path_factory.mktemp("docx") / "t.docx"
    truth = build_docx(p)
    return analyze_docx(p), truth


def test_profile_is_json_serialisable(built):
    json.dumps(built[0])


def test_page_and_margins(built):
    prof, t = built
    s = prof["sections"][0]
    assert (s["page_width_in"], s["page_height_in"]) == t["page"]
    assert s["orientation"] == "PORTRAIT"
    for k, v in t["margins"].items():
        assert s["margins_in"][k] == v, k


def test_fonts_and_colors(built):
    prof, t = built
    assert prof["styles_used"]["Normal"]["font"]["name"] == t["normal_font"]
    h1 = prof["styles_used"]["Heading 1"]["font"]
    assert (h1["name"], h1["size_pt"], h1["bold"], h1["color"]) == (t["h1_font"], t["h1_size"], True, t["h1_color"])


def test_outline_headings(built):
    prof, _ = built
    assert [(o["level"], o["text"]) for o in prof["outline"]] == [(1, "Quarterly Report"), (2, "Details")]


def test_header_footer(built):
    prof, t = built
    s = prof["sections"][0]
    assert t["header_text"] in s["header"]["text"]
    assert len(s["header"]["images"]) == 1 and s["header"]["images"][0]["placement"] == "inline"
    assert s["footer"]["has_page_number"] is True


def test_floating_image_position(built):
    prof, t = built
    fl = [i for i in prof["images"] if i.get("placement") == "floating"]
    assert len(fl) == 1
    f, tr = fl[0], t["floating"]
    assert f["pos_h"] == {"relative_from": "page", "offset_in": tr["x_in"]}
    assert f["pos_v"] == {"relative_from": "page", "offset_in": tr["y_in"]}
    assert (f["width_in"], f["height_in"]) == (tr["w_in"], tr["h_in"])
    assert f["wrap"] == "wrapNone" and f["behind_text"] is True
    assert f["alt_text"] == "Company watermark" and len(f["sha256"]) == 64


def test_image_inventory_counts(built):
    prof, _ = built
    assert prof["counts"]["images"] == 2  # floating body logo + header logo


def test_table_borders_shading_merge(built):
    prof, t = built
    tb = prof["tables"][0]
    assert (tb["rows"], tb["cols"]) == (3, 3)
    b = tb["borders"]
    assert (b["top"]["style"], b["top"]["color"], b["top"]["size_eighths_pt"]) == ("single", "FF0000", 12)
    assert (b["bottom"]["style"], b["bottom"]["color"]) == ("double", "0000FF")
    assert b["insideH"]["style"] == "dashed" and b["insideV"]["style"] == "nil"
    assert tb["cell_shading"]["0,0"] == t["header_fill"]
    assert any(m["row"] == 2 and m["col_span"] == 2 for m in tb["merged_cells"])
    assert tb["header_row_text"] == ["Region", "Q1", "Q2"]


def test_theme_colors_are_all_hex(built):
    # regression: sysClr val="window" (6 chars) was once mistaken for a hex value
    import re
    colors = built[0]["theme"]["colors"]
    assert colors, "theme colors missing"
    assert all(re.fullmatch(r"[0-9A-Fa-f]{6}", v or "") for v in colors.values()), colors


def test_census_reports_direct_formatting(built):
    # the fixture's red bold run is direct formatting inside a Georgia-styled paragraph
    c = built[0]["formatting_census"]
    assert c["bold_share"] > 0
    assert any(x["value"] == "C00000" for x in c["colors"])
    assert {x["value"] for x in c["fonts"]} >= {"Georgia"}


def test_first_section_without_header_is_marked_absent():
    from docx import Document
    import io
    from app.document_engine.docx.analyzer import analyze_docx as an
    buf = io.BytesIO()
    Document().save(buf)
    buf.seek(0)
    s = an(buf)["sections"][0]
    assert s["header"] == {"present": False} and s["footer"] == {"present": False}


def test_summary_is_small_and_json(built):
    from app.document_engine.summary import summary_json
    out = summary_json(built[0])
    assert len(out) < 30_000
    json.loads(out)


def test_body_order_preserved(built):
    prof, _ = built
    kinds = [b["type"] for b in prof["body"]]
    assert kinds[-1] == "table" and kinds[0] == "paragraph"
