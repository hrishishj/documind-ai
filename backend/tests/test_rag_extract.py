import pytest
from docx import Document

from app.core.config import REPO_ROOT
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.rag.extract import HARD_MAX, extract_chunks


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("rag")
    build_docx(d / "a.docx"); build_pptx(d / "a.pptx"); build_xlsx(d / "a.xlsx")
    (d / "n.md").write_text("First paragraph here.\n\nSecond paragraph\ncontinues here.\n\n\nThird.", encoding="utf-8")
    return d


def by_loc(chunks):
    return {c.locator: c for c in chunks}


def test_docx_chunks_have_paragraph_and_table_locators_and_headings(files):
    ch = extract_chunks(files / "a.docx")
    loc = by_loc(ch)
    assert any("Quarterly Report" in c.title for c in ch)
    body = next(c for c in ch if "Intro paragraph in Georgia." in c.text)
    assert body.locator.startswith("¶") and body.title in ("Quarterly Report", "Details")
    t = loc["Table 0"]
    assert "Region | Q1 | Q2" in t.text and "North | 10 | 20" in t.text
    assert "Merged total | 30" in t.text  # the merged cell appears once, not twice
    assert [c.order for c in ch] == list(range(len(ch)))


def test_pptx_chunks_name_the_slide_and_include_tables_and_notes(files):
    ch = extract_chunks(files / "a.pptx")
    loc = by_loc(ch)
    assert "Slide 1" in loc and "Annual Review" in loc["Slide 1"].text
    assert "Metric | Value" in loc["Slide 2"].text and "Speaker notes here" in loc["Slide 2"].text
    assert all(c.text.startswith("[Slide") for c in ch)


def test_xlsx_chunks_carry_sheet_and_row_ranges(files):
    ch = extract_chunks(files / "a.xlsx")
    c = next(x for x in ch if "North" in x.text)
    assert c.locator.startswith("Sheet 'Report' rows") and "North | 1234.5" in c.text and c.title == "Report"


def test_text_file_paragraphs_get_line_numbers(files):
    ch = extract_chunks(files / "n.md")
    assert ch[0].locator == "line 1"
    assert "Second paragraph continues here." in " ".join(c.text for c in ch)


def test_no_chunk_exceeds_the_hard_limit_even_for_a_single_giant_paragraph(tmp_path):
    d = Document()
    d.add_paragraph("Sentence number one is here. " * 400)  # ~11 000 chars with no paragraph break
    p = tmp_path / "big.docx"
    d.save(p)
    ch = extract_chunks(p)
    assert len(ch) > 5 and max(len(c.text) for c in ch) <= HARD_MAX
    assert sum(len(c.text) for c in ch) > 9000  # nothing was dropped


def test_empty_document_yields_no_chunks(tmp_path):
    p = tmp_path / "e.docx"
    Document().save(p)
    assert extract_chunks(p) == []


SAMPLES = REPO_ROOT / "sample_templates"


@pytest.mark.skipif(not (SAMPLES / "pdf" / "ael_investors_presentation.pdf").exists(), reason="sample missing")
def test_real_pdf_pages_are_located():
    ch = extract_chunks(SAMPLES / "pdf" / "ael_investors_presentation.pdf")
    assert len(ch) > 10 and all(c.locator.startswith("p.") for c in ch)
    assert max(len(c.text) for c in ch) <= HARD_MAX


@pytest.mark.skipif(not (SAMPLES / "xlsx" / "sec_filing_sample.xls").exists(), reason="sample missing")
def test_real_xls_numbers_are_findable_as_text():
    ch = extract_chunks(SAMPLES / "xlsx" / "sec_filing_sample.xls")
    # the figure appears on several sheets of the filing; the income-statement sheet must be one of them, correctly located
    hits = [c for c in ch if "331839" in c.text.replace(",", "")]
    assert len(hits) >= 2
    inc = [c for c in hits if c.locator.startswith("Sheet 'ome statements'")]
    assert inc and "Total revenue" in inc[0].text and inc[0].title == "ome statements"
    assert all(c.text.startswith(f"[Sheet {c.title}]") for c in ch)  # text prefix and title always agree


@pytest.mark.skipif(not (SAMPLES / "docx" / "sec_filing_sample.docx").exists(), reason="sample missing")
def test_real_docx_extraction_is_bounded_and_fast():
    import time
    t = time.time()
    ch = extract_chunks(SAMPLES / "docx" / "sec_filing_sample.docx")
    assert 100 < len(ch) <= 4000 and time.time() - t < 20
