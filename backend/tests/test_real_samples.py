"""Tests against real-world files supplied in sample_templates/ (skipped if absent)."""

import pytest

from app.core.config import REPO_ROOT
from app.document_engine.docx.analyzer import analyze_docx

SEC = REPO_ROOT / "sample_templates" / "docx" / "sec_filing_sample.docx"


@pytest.fixture(scope="module")
def sec():
    if not SEC.exists():
        pytest.skip("real sample not present")
    return analyze_docx(SEC)


def test_real_docx_counts(sec):
    assert sec["counts"]["tables"] == 93
    assert sec["counts"]["paragraphs"] == 2109


def test_real_docx_census_reflects_direct_formatting_not_style(sec):
    # style font is Liberation Serif, but the text is overwhelmingly Arial 10pt
    assert sec["defaults"]["name"] == "Liberation Serif"
    top = sec["formatting_census"]["fonts"][0]
    assert top["value"] == "Arial" and top["share"] > 0.8
    assert sec["formatting_census"]["sizes_pt"][0]["value"] == 10.0


def test_real_docx_cell_borders_and_shading_found(sec):
    big = max(sec["tables"], key=lambda t: t["rows"] * t["cols"])
    assert big["cell_borders"]["bottom"]["cells"] > 0
    assert big["shading_summary"].get("E5E5E5", 0) > 0
    # large per-cell lists must be summarised, not dumped
    assert "cell_shading" not in big or len(big["cell_shading"]) <= 40


def test_real_docx_no_header_footer_reported_as_absent(sec):
    assert sec["sections"][0]["header"] == {"present": False}
