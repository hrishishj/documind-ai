import hashlib
import zipfile

import pytest
from lxml import etree
from pptx import Presentation

from app.document_engine.plan import EditPlan, PlanError
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.pptx.renderer import render_pptx as _render_pptx, validate_pptx_plan
from app.document_engine.samples import build_pptx

P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "src.pptx"
    build_pptx(p)
    return p


def plan(*ops):
    return EditPlan.model_validate({"format": "pptx", "ops": list(ops)})


def assert_package_sane(path):
    """No duplicate part names and every internal relationship target exists (what PowerPoint 'repairs')."""
    import posixpath
    import re
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert len(names) == len(set(names)), f"duplicate parts: {sorted(n for n in set(names) if names.count(n) > 1)}"
        have = set(names)
        for n in names:
            if not n.endswith(".rels"):
                continue
            base = posixpath.dirname(posixpath.dirname(n))  # folder of the source part
            for m in re.finditer(r'<Relationship [^>]*>', z.read(n).decode("utf8")):
                tag = m.group(0)
                if 'TargetMode="External"' in tag:
                    continue
                target = re.search(r'Target="([^"]+)"', tag).group(1)
                full = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
                assert full in have, f"{n} -> missing target {full}"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def names(path, slide):
    return [s.name for s in Presentation(str(path)).slides[slide - 1].shapes]


def textbox_name(path):
    return next(n for n in names(path, 3) if n.startswith("TextBox"))


def titles(path):
    return [(s.slide_layout.name, s.shapes.title.text if s.shapes.title is not None else None) for s in Presentation(str(path)).slides]


def render_pptx(src, plan_, dst):
    """Every render in this module is also checked for package integrity."""
    res = _render_pptx(src, plan_, dst)
    assert_package_sane(dst)
    return res


def test_source_untouched_and_overwrite_refused(src, tmp_path):
    before = sha(src)
    render_pptx(src, plan({"op": "pptx.replace_all", "find": "Annual", "replace": "Yearly"}), tmp_path / "o.pptx")
    assert sha(src) == before
    with pytest.raises(ValueError):
        render_pptx(src, plan(), src)


def test_set_text_keeps_run_formatting(src, tmp_path):
    out = tmp_path / "o.pptx"
    tb = textbox_name(src)
    render_pptx(src, plan({"op": "pptx.set_text", "slide": 3, "shape": tb, "text": "Line one\nLine two"}), out)
    shape = next(s for s in Presentation(str(out)).slides[2].shapes if s.name == tb)
    paras = shape.text_frame.paragraphs
    assert [p.text for p in paras] == ["Line one", "Line two"]
    for p in paras:  # BOTH lines (incl. the cloned one) keep Arial 24 bold red
        r = p.runs[0]
        assert (r.font.name, r.font.size.pt, r.font.bold, str(r.font.color.rgb)) == ("Arial", 24.0, True, "C00000")


def test_set_text_on_placeholder_by_idx_and_title(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.set_text", "slide": 1, "shape": "idx:1", "text": "FY2025"},
                          {"op": "pptx.set_text", "slide": 2, "shape": "idx:0", "text": "Key results"}), out)
    prs = Presentation(str(out))
    assert prs.slides[0].placeholders[1].text == "FY2025" and prs.slides[1].shapes.title.text == "Key results"


def test_set_cell_keeps_table_structure(src, tmp_path):
    out = tmp_path / "o.pptx"
    tbl = next(n for n in names(src, 2) if n.startswith("Table"))
    render_pptx(src, plan({"op": "pptx.set_cell", "slide": 2, "shape": tbl, "row": 1, "col": 1, "text": "42"}), out)
    t = next(s for s in Presentation(str(out)).slides[1].shapes if s.has_table).table
    assert t.cell(1, 1).text == "42" and t.cell(0, 0).text == "Metric"
    a, b = analyze_pptx(src)["slides"][1], analyze_pptx(out)["slides"][1]
    ta = next(s for s in a["shapes"] if "table" in s)
    tb = next(s for s in b["shapes"] if "table" in s)
    assert (ta["x_in"], ta["y_in"], ta["w_in"], ta["h_in"]) == (tb["x_in"], tb["y_in"], tb["w_in"], tb["h_in"])


def test_add_slide_uses_template_layout_and_drops_unfilled_placeholders(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.add_slide", "layout": "Title and Content",
                           "texts": {"0": "Roadmap", "1": "Q1 ship\nQ2 scale"}, "position": 2}), out)
    assert titles(out) == [("Title Slide", "Annual Review"), ("Title and Content", "Roadmap"),
                           ("Title and Content", "Highlights"), ("Title Only", "Chart")]
    new = Presentation(str(out)).slides[1]
    assert [p.text for p in new.placeholders[1].text_frame.paragraphs] == ["Q1 ship", "Q2 scale"]
    # master/layout logos are inherited, so the profile still shows them and masters are unchanged
    assert analyze_pptx(out)["masters"][0]["decor"] == analyze_pptx(src)["masters"][0]["decor"]


def test_footer_and_date_placeholders_are_rejected_not_silently_dropped(src, tmp_path):
    with pytest.raises(PlanError, match="no fillable placeholder idx"):
        render_pptx(src, plan({"op": "pptx.add_slide", "layout": "Title Only", "texts": {"0": "T", "11": "Footer text"}}),
                    tmp_path / "o.pptx")


def test_add_slide_leaves_no_empty_placeholder_shells(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.add_slide", "layout": "Title and Content", "texts": {"0": "Only title"}}), out)
    last = Presentation(str(out)).slides[3]
    assert [s.name for s in last.placeholders] and all(s.placeholder_format.idx == 0 for s in last.placeholders)


def test_duplicate_slide_copies_picture_and_table(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.duplicate_slide", "slide": 2}), out)
    prs = Presentation(str(out))
    assert len(prs.slides) == 4
    a, b = analyze_pptx(out)["slides"][1], analyze_pptx(out)["slides"][2]
    assert a["layout"] == b["layout"] and a["title"] == b["title"]
    pa = [s for s in a["shapes"] if s["kind"] == "picture"][0]
    pb = [s for s in b["shapes"] if s["kind"] == "picture"][0]
    assert pa["image"]["sha256"] == pb["image"]["sha256"]  # image rel was remapped, not dropped
    assert (pa["x_in"], pa["y_in"], pa["w_in"], pa["h_in"]) == (pb["x_in"], pb["y_in"], pb["w_in"], pb["h_in"])
    assert any("table" in s for s in b["shapes"])


def test_duplicate_slide_with_chart_is_refused(src, tmp_path):
    with pytest.raises(PlanError, match="cannot be duplicated safely"):
        render_pptx(src, plan({"op": "pptx.duplicate_slide", "slide": 3}), tmp_path / "o.pptx")


def test_delete_slide_drops_the_part_from_the_package(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.delete_slide", "slide": 2}), out)
    assert [t for _, t in titles(out)] == ["Annual Review", "Chart"]
    slide_parts = [n for n in zipfile.ZipFile(out).namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
    assert len(slide_parts) == 2  # no orphaned slide left behind


def test_move_slide(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.move_slide", "slide": 3, "to": 1}), out)
    assert [t for _, t in titles(out)] == ["Chart", "Annual Review", "Highlights"]


def test_combined_structural_ops_are_consistent(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan(
        {"op": "pptx.delete_slide", "slide": 1},
        {"op": "pptx.add_slide", "layout": "Title Slide", "texts": {"0": "New opener", "1": "sub"}, "position": 1},
        {"op": "pptx.duplicate_slide", "slide": 2}), out)
    assert [t for _, t in titles(out)] == ["New opener", "Highlights", "Highlights", "Chart"]


def test_replace_all_across_runs(src, tmp_path):
    out = tmp_path / "o.pptx"
    prs = Presentation(str(src))
    p = prs.slides[0].placeholders[1].text_frame.paragraphs[0]
    p.runs[0].text = "FY20"
    r2 = p.add_run()
    r2.text = "24 plan"
    prs.save(str(tmp_path / "split.pptx"))
    render_pptx(tmp_path / "split.pptx", plan({"op": "pptx.replace_all", "find": "FY2024", "replace": "FY2030"}), out)
    assert Presentation(str(out)).slides[0].placeholders[1].text == "FY2030 plan"


def test_invalid_plan_errors_are_actionable_and_nothing_is_saved(src, tmp_path):
    out = tmp_path / "o.pptx"
    p = plan({"op": "pptx.set_text", "slide": 9, "shape": "x", "text": "t"},
             {"op": "pptx.set_text", "slide": 3, "shape": "Nope", "text": "t"},
             {"op": "pptx.add_slide", "layout": "Imaginary", "texts": {}},
             {"op": "pptx.add_slide", "layout": "Title Only", "texts": {"7": "x"}},
             {"op": "pptx.move_slide", "slide": 1, "to": 50})
    with pytest.raises(PlanError) as e:
        render_pptx(src, p, out)
    errs = e.value.errors
    assert len(errs) == 5 and not out.exists()
    assert "valid 1..3" in errs[0] and "shapes:" in errs[1] and "Title Only" in errs[2] and "available idx" in errs[3]
    assert validate_pptx_plan(src, p) == errs


def test_reference_to_deleted_slide_is_rejected(src, tmp_path):
    with pytest.raises(PlanError, match="deleted by an earlier op"):
        render_pptx(src, plan({"op": "pptx.delete_slide", "slide": 2},
                              {"op": "pptx.set_text", "slide": 2, "shape": "idx:0", "text": "x"}), tmp_path / "o.pptx")


def _with_sections(path, out):
    """Give the deck a PowerPoint 2010 section list covering all slides (as real decks have)."""
    prs = Presentation(str(path))
    ids = [e.get("id") for e in prs.slides._sldIdLst]
    xml = (f'<p:extLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:ext uri="{{521415D9-36F7-43E2-AB2F-B90AF26B5E84}}">'
           f'<p14:sectionLst xmlns:p14="{P14}"><p14:section name="All" id="{{11111111-1111-1111-1111-111111111111}}"><p14:sldIdLst>'
           + "".join(f'<p14:sldId id="{i}"/>' for i in ids) + "</p14:sldIdLst></p14:section></p14:sectionLst></p:ext></p:extLst>")
    prs.part._element.append(etree.fromstring(xml))
    prs.save(str(out))


def test_section_lists_stay_consistent_with_the_slide_list(src, tmp_path):
    sec = tmp_path / "sec.pptx"
    _with_sections(src, sec)
    out = tmp_path / "o.pptx"
    render_pptx(sec, plan({"op": "pptx.delete_slide", "slide": 2},
                          {"op": "pptx.add_slide", "layout": "Title Only", "texts": {"0": "Added"}},
                          {"op": "pptx.duplicate_slide", "slide": 1}), out)
    root = Presentation(str(out)).part._element
    in_deck = sorted(e.get("id") for e in root.find("{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst"))
    in_sections = sorted(e.get("id") for e in root.iter(f"{{{P14}}}sldId"))
    assert in_deck == in_sections and len(in_deck) == 4  # a mismatch is what triggers PowerPoint's repair dialog


def test_untouched_slides_and_template_are_identical_after_edit(src, tmp_path):
    out = tmp_path / "o.pptx"
    render_pptx(src, plan({"op": "pptx.set_text", "slide": 1, "shape": "idx:0", "text": "Changed"}), out)
    a, b = analyze_pptx(src), analyze_pptx(out)
    assert a["masters"] == b["masters"] and a["slide_size"] == b["slide_size"]
    assert a["slides"][1:] == b["slides"][1:]  # slides 2 and 3: every shape, position and format identical
    assert [{k: v for k, v in s.items() if k not in ("text", "title")} for s in a["slides"][0]["shapes"]] == \
           [{k: v for k, v in s.items() if k not in ("text", "title")} for s in b["slides"][0]["shapes"]]
