import json

import pytest

from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.samples import build_pptx


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    p = tmp_path_factory.mktemp("pptx") / "t.pptx"
    truth = build_pptx(p)
    return analyze_pptx(p), truth


def slide(prof, i):
    return prof["slides"][i - 1]


def by_name(shapes, startswith):
    return next(s for s in shapes if s["name"].startswith(startswith))


def test_json_serialisable(built):
    json.dumps(built[0])


def test_slide_size_and_counts(built):
    prof, t = built
    assert (prof["slide_size"]["width_in"], prof["slide_size"]["height_in"]) == t["size_in"]
    assert prof["slide_size"]["aspect"] == 1.778
    assert prof["counts"]["slides"] == t["n_slides"]


def test_theme_fonts(built):
    prof, t = built
    f = prof["masters"][0]["theme"]["fonts"]
    assert (f["major"], f["minor"]) == (t["major"], t["minor"])
    import re
    assert all(re.fullmatch(r"[0-9A-Fa-f]{6}", v or "") for v in prof["masters"][0]["theme"]["colors"].values())


def test_master_logo_position(built):
    prof, t = built
    pics = [d for d in prof["masters"][0]["decor"] if d["kind"] == "picture"]
    assert len(pics) == 1
    p, tr = pics[0], t["master_logo"]
    assert (p["x_in"], p["y_in"], p["w_in"]) == (tr["x"], tr["y"], tr["w"])
    assert p["image"]["content_type"] == "image/png" and len(p["image"]["sha256"]) == 64


def test_layout_logo_and_usage(built):
    prof, t = built
    lay = next(l for l in prof["masters"][0]["layouts"] if l["name"] == t["layout_logo"]["layout"])
    logo = [d for d in lay["decor"] if d["kind"] == "picture"][0]
    tr = t["layout_logo"]
    assert (logo["x_in"], logo["y_in"], logo["w_in"], logo["h_in"]) == (tr["x"], tr["y"], tr["w"], tr["h"])
    assert lay["used_by_slides"] == 1
    assert any(p["placeholder"]["type"] == "TITLE" for p in lay["placeholders"])


def test_slide_layout_assignment_and_titles(built):
    prof, _ = built
    assert [(s["layout"], s["title"]) for s in prof["slides"]] == [
        ("Title Slide", "Annual Review"), ("Title and Content", "Highlights"), ("Title Only", "Chart")]


def test_placeholder_inherits_position(built):
    # title placeholders carry no own offset; the analyzer must report the inherited geometry
    t1 = by_name(slide(built[0], 1)["shapes"], "Title")
    assert t1["kind"] == "placeholder" and t1["w_in"] > 0 and t1["h_in"] > 0


def test_slide_picture_and_table(built):
    prof, t = built
    sh = slide(prof, 2)["shapes"]
    pic = next(s for s in sh if s["kind"] == "picture")
    assert (pic["x_in"], pic["y_in"], pic["w_in"], pic["h_in"]) == tuple(t["s2_picture"][k] for k in "xywh")
    tb = next(s for s in sh if s["kind"] == "table")
    tr = t["s2_table"]
    assert (tb["x_in"], tb["y_in"], tb["w_in"]) == (tr["x"], tr["y"], tr["w"])
    assert (tb["table"]["rows"], tb["table"]["cols"]) == (tr["rows"], tr["cols"])
    assert tb["table"]["header_row_text"] == ["Metric", "Value"] and tb["table"]["first_row_flag"] is True


def test_text_format_chart_background_notes(built):
    prof, t = built
    s3 = slide(prof, 3)
    tx = by_name(s3["shapes"], "TextBox")["run_format"]
    tr = t["s3_text"]
    assert (tx["font"], tx["size_pt"], tx["bold"], tx["color"]) == (tr["font"], tr["size"], True, tr["color"])
    ch = next(s for s in s3["shapes"] if s["kind"] == "chart")
    assert ch["chart"]["type"] == "COLUMN_CLUSTERED" and ch["chart"]["series"] == 1
    assert ch["chart"]["plot_types"] == ["barChart"]
    assert (ch["x_in"], ch["y_in"], ch["w_in"], ch["h_in"]) == tuple(t["s3_chart"][k] for k in "xywh")
    assert s3["background_override"] == {"type": "solid", "color": t["s3_bg"]}
    assert slide(prof, 2)["has_notes"] is True and slide(prof, 1)["has_notes"] is False


def test_image_inventory_has_locations(built):
    locs = {i["location"] for i in built[0]["images"]}
    assert {"master0", "layout:Title Only", "slide2"} <= locs


def test_summary_bounded_and_keeps_key_facts(built):
    from app.document_engine.summary import summary_json
    s = json.loads(summary_json(built[0]))
    assert s["format"] == "pptx" and s["slide_size"]["aspect"] == 1.778
    assert [x["layout"] for x in s["slides"]] == ["Title Slide", "Title and Content", "Title Only"]
    assert s["masters"][0]["theme"]["fonts"]["major"] == "Georgia"
    assert any(d["kind"] == "picture" for d in s["masters"][0]["decor"])


def test_census_and_parts(built):
    prof, _ = built
    fonts = {f["value"]: f["share"] for f in prof["formatting_census"]["explicit_fonts"]}
    # "Styled text" (11 chars) is explicit Arial; the other 40-odd characters inherit from the theme
    assert fonts["Arial"] == pytest.approx(11 / prof["formatting_census"]["text_chars"], abs=0.001)
    assert "(inherited)" in fonts
    assert prof["package_parts"]["charts"] == 1 and prof["package_parts"]["notes_slides"] == 1
    assert prof["package_parts"]["has_vba"] is False
