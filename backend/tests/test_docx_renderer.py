import copy
import hashlib

import pytest
from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.docx.renderer import render_docx, replace_in_paragraph, validate_docx_plan
from app.document_engine.plan import EditPlan, PlanError
from app.document_engine.samples import build_docx


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "src.docx"
    build_docx(p)
    return p


def plan(*ops):
    return EditPlan.model_validate({"format": "docx", "ops": list(ops)})


def body_xml(path):
    return [etree.tostring(c) for c in Document(str(path)).element.body.iterchildren()]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_source_is_never_modified_and_overwrite_refused(src, tmp_path):
    before = sha(src)
    render_docx(src, plan({"op": "docx.replace_text", "paragraph": 1, "text": "x"}), tmp_path / "o.docx")
    assert sha(src) == before
    with pytest.raises(ValueError):
        render_docx(src, plan(), src)


@pytest.fixture()
def styled(tmp_path):
    """Paragraph 1: long Arial-14-bold-blue run + short italic run, centred. Paragraph 0 is a plain heading."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor
    d = Document()
    d.add_paragraph("Heading line")
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    a = p.add_run("This is the long dominant run.")
    a.font.name, a.font.size, a.font.bold, a.font.color.rgb = "Arial", Pt(14), True, RGBColor(0, 0, 0xFF)
    p.add_run(" short").italic = True
    d.add_paragraph("Tail")
    f = tmp_path / "styled.docx"
    d.save(f)
    return f


def test_replace_text_keeps_dominant_run_formatting_and_touches_nothing_else(styled, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(styled, plan({"op": "docx.replace_text", "paragraph": 1, "text": "Brand new text"}), out)
    a, b = body_xml(styled), body_xml(out)
    assert len(a) == len(b)
    assert [i for i, (x, y) in enumerate(zip(a, b)) if x != y] == [1]  # exactly one body element differs
    p = Document(str(out)).paragraphs[1]
    assert p.text == "Brand new text" and len(p.runs) == 1
    src_run = Document(str(styled)).paragraphs[1].runs[0]  # the dominant (longer) run, not the italic one
    assert etree.tostring(p.runs[0]._r.rPr) == etree.tostring(src_run._r.rPr)
    r = p.runs[0]
    assert (r.font.name, r.font.size.pt, r.font.bold, str(r.font.color.rgb)) == ("Arial", 14.0, True, "0000FF")
    assert not r.italic  # the minor run's formatting must not leak in
    assert etree.tostring(p._p.pPr) == etree.tostring(Document(str(styled)).paragraphs[1]._p.pPr)  # alignment kept


def test_replace_text_multiline_makes_breaks(src, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(src, plan({"op": "docx.replace_text", "paragraph": 1, "text": "a\nb"}), out)
    assert len(Document(str(out)).paragraphs[1]._p.findall(".//" + qn("w:br"))) == 1


def test_insert_paragraphs_keep_plan_order_and_clone_style(styled, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(styled, plan(
        {"op": "docx.insert_paragraph", "after": 0, "text": "first new", "like": 1},
        {"op": "docx.insert_paragraph", "after": 0, "text": "second new", "like": 1}), out)
    d = Document(str(out))
    assert [p.text for p in d.paragraphs] == ["Heading line", "first new", "second new", "This is the long dominant run. short", "Tail"]
    like = Document(str(styled)).paragraphs[1]
    for i in (1, 2):  # both clones carry the centred alignment and the Arial-14-bold-blue run
        assert etree.tostring(d.paragraphs[i]._p.pPr) == etree.tostring(like._p.pPr)
        r = d.paragraphs[i].runs[0]
        assert (r.font.name, r.font.size.pt, r.font.bold) == ("Arial", 14.0, True)


def test_delete_and_header_style_preserved(src, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(src, plan({"op": "docx.delete_paragraph", "paragraph": 2}), out)
    assert "Details" not in [p.text for p in Document(str(out)).paragraphs]
    # nothing structural around it changed
    assert analyze_docx(out)["sections"] == analyze_docx(src)["sections"]


def test_table_cell_and_row_ops_keep_borders_and_shading(src, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(src, plan(
        {"op": "docx.set_cell", "table": 0, "row": 1, "col": 1, "text": "999"},
        {"op": "docx.add_row", "table": 0, "values": ["West", "7", "8"], "like_row": 1},
        {"op": "docx.add_row", "table": 0, "values": ["Far", "1", "2"], "like_row": 1}), out)
    t = Document(str(out)).tables[0]
    assert [c.text for c in t.rows[1].cells] == ["North", "999", "20"]
    assert t.rows[2].cells[0].text == "Merged total"  # original last row stays where it was
    assert [t.rows[i].cells[0].text for i in (3, 4)] == ["West", "Far"]  # new rows follow it, in plan order
    prof_a, prof_b = analyze_docx(src)["tables"][0], analyze_docx(out)["tables"][0]
    assert prof_b["rows"] == prof_a["rows"] + 2
    assert prof_b["borders"] == prof_a["borders"] and prof_b["cell_shading"] == prof_a["cell_shading"]


def test_add_row_never_silently_drops_values_when_the_template_row_is_merged(src, tmp_path):
    # the fixture's last row is "Merged total" (2 cells in a 3-column table): cloning it for 3 values would lose one
    with pytest.raises(PlanError) as e:
        render_docx(src, plan({"op": "docx.add_row", "table": 0, "values": ["West", "5", "6"], "like_row": -1}), tmp_path / "o.docx")
    msg = e.value.errors[0]
    assert "only 2 cells" in msg and "3 values" in msg and "[1, 0]" in msg  # latest-first candidates
    assert "not the header row" in msg
    res = render_docx(src, plan({"op": "docx.add_row", "table": 0, "values": ["H", "1", "2"], "like_row": 0}), tmp_path / "hdr.docx")
    assert any("usually the header" in w for w in res.warnings)  # cloning the header row is allowed but flagged
    # and the corrected plan succeeds with all three values present
    out = tmp_path / "ok.docx"
    render_docx(src, plan({"op": "docx.add_row", "table": 0, "values": ["West", "5", "6"], "like_row": 1}), out)
    assert [c.text for c in Document(str(out)).tables[0].rows[-1].cells] == ["West", "5", "6"]


def test_delete_row(src, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(src, plan({"op": "docx.delete_row", "table": 0, "row": 1}), out)
    assert len(Document(str(out)).tables[0].rows) == 2


def test_replace_all_across_run_boundaries(tmp_path):
    d = Document()
    p = d.add_paragraph()
    for chunk in ("Hel", "lo Wor", "ld and Hel", "lo"):
        p.add_run(chunk)
    p.runs[1].bold = True
    assert replace_in_paragraph(p._p, "Hello", "Bye") == 2
    assert p.text == "Bye World and Bye"
    assert p.runs[1].bold is True  # untouched middle run keeps its formatting


def test_replace_all_reaches_tables_headers_and_footers(src, tmp_path):
    out = tmp_path / "o.docx"
    res = render_docx(src, plan({"op": "docx.replace_all", "find": "ACME", "replace": "Globex"},
                                {"op": "docx.replace_all", "find": "North", "replace": "Nord"}), out)
    d = Document(str(out))
    assert "Globex Corp" in d.sections[0].header.paragraphs[0].text
    assert d.tables[0].rows[1].cells[0].text == "Nord"
    assert not res.warnings


def test_replace_all_missing_text_warns(src, tmp_path):
    res = render_docx(src, plan({"op": "docx.replace_all", "find": "zzz-not-there", "replace": "x"}), tmp_path / "o.docx")
    assert any("not found" in w for w in res.warnings)


def test_invalid_plan_collects_all_errors_and_saves_nothing(src, tmp_path):
    out = tmp_path / "o.docx"
    p = plan({"op": "docx.replace_text", "paragraph": 99, "text": "x"},
             {"op": "docx.set_cell", "table": 5, "row": 0, "col": 0, "text": "x"},
             {"op": "docx.set_cell", "table": 0, "row": 0, "col": 9, "text": "x"},
             {"op": "docx.replace_text", "paragraph": 1, "text": "fine"})
    with pytest.raises(PlanError) as e:
        render_docx(src, p, out)
    assert len(e.value.errors) == 3 and not out.exists()
    assert "valid 0.." in e.value.errors[0] and "has 1 tables" in e.value.errors[1]
    assert validate_docx_plan(src, p) == e.value.errors


def test_reference_to_deleted_paragraph_is_rejected(src, tmp_path):
    p = plan({"op": "docx.delete_paragraph", "paragraph": 1}, {"op": "docx.replace_text", "paragraph": 1, "text": "x"})
    with pytest.raises(PlanError, match="deleted by an earlier op"):
        render_docx(src, p, tmp_path / "o.docx")


def test_plan_schema_rejects_garbage():
    with pytest.raises(Exception):
        plan({"op": "docx.replace_text", "paragraph": -1, "text": "x"})
    with pytest.raises(Exception):
        plan({"op": "docx.replace_text", "paragraph": 0, "text": "x", "surprise": 1})
    with pytest.raises(Exception):
        EditPlan.model_validate({"format": "docx", "ops": [{"op": "pptx.delete_slide", "slide": 1}]})


def test_output_reanalyzes_cleanly(src, tmp_path):
    out = tmp_path / "o.docx"
    render_docx(src, plan({"op": "docx.insert_paragraph", "after": 1, "text": "added"}), out)
    a, b = analyze_docx(src), analyze_docx(out)
    assert b["counts"]["paragraphs"] == a["counts"]["paragraphs"] + 1
    strip = lambda imgs: [{k: v for k, v in i.items() if k != "paragraph_index"} for i in imgs]  # noqa: E731
    assert b["theme"] == a["theme"] and strip(b["images"]) == strip(a["images"]) and b["sections"] == a["sections"]
    # the inserted paragraph shifts later paragraph indices by exactly one
    pos = lambda prof: [i["paragraph_index"] for i in prof["images"] if "paragraph_index" in i]  # noqa: E731
    assert [x + 1 for x in pos(a)] == pos(b)
