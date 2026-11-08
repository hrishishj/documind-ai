"""The Sources section is appended deterministically and must render in the template's own formatting."""
import pytest
from docx import Document

from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.plan import EditPlan
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.render import apply_plan
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.document_engine.xlsx.analyzer import analyze_xlsx
from app.research.sources_ops import sources_ops

SRC = [{"title": "Solid-State Battery Technology for Next-Generation Vehicles", "url": "https://www.mdpi.com/1996-1073/19/11/2659"},
       {"title": "Solid-state batteries 2026-2036", "url": "https://www.idtechex.com/en/research-report/solid-state-batteries/1130"}]


def plan(fmt, ops):
    return EditPlan.model_validate({"format": fmt, "ops": ops})


@pytest.fixture()
def docx_file(tmp_path):
    p = tmp_path / "a.docx"
    build_docx(p)
    return p


def test_docx_sources_are_cloned_from_the_last_paragraph_and_keep_the_original_untouched(docx_file, tmp_path):
    prof = analyze_docx(docx_file)
    ops, note = sources_ops("docx", prof, SRC, [])
    assert note == "" and [o["op"] for o in ops] == ["docx.append_paragraph"] * 3
    out = tmp_path / "o.docx"
    res = apply_plan(docx_file, plan("docx", ops), out)
    assert res.preserved, res.violations
    doc = Document(str(out))
    # the fixture ENDS WITH A TABLE: the section must still come after it, at the very end of the document
    assert doc.element.body[-2].tag.endswith("}p") and doc.element.body[-1].tag.endswith("}sectPr")
    assert list(doc.element.body).index(doc.tables[0]._tbl) < list(doc.element.body).index(doc.paragraphs[-3]._p)
    paras = [p.text for p in doc.paragraphs]
    tail = paras[-3:]
    assert tail[0] == "Sources"
    assert tail[1].startswith("[1] Solid-State Battery") and tail[1].endswith("https://www.mdpi.com/1996-1073/19/11/2659")
    assert tail[2].startswith("[2] Solid-state batteries 2026-2036") and "idtechex.com" in tail[2]  # order preserved
    assert paras[:len(Document(str(docx_file)).paragraphs)] == [p.text for p in Document(str(docx_file)).paragraphs]
    # formatting: the title clones a heading, the entries clone an ordinary paragraph (not a heading)
    styles = [p.style.name for p in doc.paragraphs[-3:]]
    assert styles[0].startswith("Heading") and not styles[1].startswith("Heading") and styles[1] == styles[2]


def test_docx_sources_never_clone_a_paragraph_the_plan_deletes(docx_file):
    prof = analyze_docx(docx_file)
    body_idx = max(b["index"] for b in prof["body"] if b["type"] == "paragraph" and b.get("text", "").strip()
                   and not b["style"].startswith("Heading"))
    ops, _ = sources_ops("docx", prof, SRC, [{"op": "docx.delete_paragraph", "paragraph": body_idx}])
    assert ops and all(o["like"] != body_idx for o in ops)


def test_docx_with_no_text_paragraph_explains_instead_of_failing(tmp_path):
    p = tmp_path / "e.docx"
    Document().save(p)
    ops, note = sources_ops("docx", analyze_docx(p), SRC, [])
    assert ops == [] and "no paragraph" in note


def test_pptx_sources_become_a_slide_on_the_templates_own_layout(tmp_path):
    p = tmp_path / "a.pptx"
    build_pptx(p)
    prof = analyze_pptx(p)
    ops, note = sources_ops("pptx", prof, SRC, [])
    assert note == "" and ops[0]["op"] == "pptx.add_slide" and ops[0]["layout"] == "Title and Content"
    out = tmp_path / "o.pptx"
    res = apply_plan(p, plan("pptx", ops), out)
    assert res.preserved, res.violations
    after = analyze_pptx(out)
    last = after["slides"][-1]
    assert last["layout"] == "Title and Content" and last["title"] == "Sources"
    from pptx import Presentation
    body = Presentation(str(out)).slides[-1].placeholders[1].text_frame.text   # (the analyzer truncates text to 100 chars)
    assert "mdpi.com" in body and "idtechex.com" in body and "https://" not in body   # domains, not full URLs: they must fit a slide
    assert len(after["slides"]) == len(prof["slides"]) + 1


def test_pptx_without_a_suitable_layout_says_so(tmp_path):
    p = tmp_path / "a.pptx"
    build_pptx(p)
    prof = analyze_pptx(p)
    for m in prof["masters"]:
        for l in m["layouts"]:
            l["placeholders"] = [x for x in l["placeholders"] if x["placeholder"]["type"] not in ("OBJECT", "BODY")]
    ops, note = sources_ops("pptx", prof, SRC, [])
    assert ops == [] and "no layout" in note


def test_xlsx_explains_it_does_not_add_a_sources_section(tmp_path):
    p = tmp_path / "a.xlsx"
    build_xlsx(p)
    ops, note = sources_ops("xlsx", analyze_xlsx(p), SRC, [])
    assert ops == [] and "chat reply" in note


def test_no_sources_means_no_ops_and_no_note(docx_file):
    assert sources_ops("docx", analyze_docx(docx_file), [], []) == ([], "")


def test_sources_with_hostile_titles_are_cleaned():
    ops, _ = sources_ops("docx", {"body": [{"type": "paragraph", "index": 0, "text": "x"}]},
                         [{"title": "Bad\x00 title\n\nwith   breaks " + "x" * 300, "url": "https://a.com/"}], [])
    t = ops[1]["text"]
    assert "\x00" not in t and "\n" not in t and len(t) < 160 and t.endswith("https://a.com/")
