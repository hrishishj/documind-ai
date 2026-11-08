"""Tests against genuine Microsoft-authored templates (not redistributed; skipped if Office is absent).

Expected numbers were cross-checked against PowerPoint itself via COM (slides, layouts and every
shape's geometry matched: 5 templates, 175 shapes, 0 mismatches).
"""
import json
from pathlib import Path

import pytest

from app.document_engine.pptx.analyzer import analyze_pptx

TPL = Path(r"C:\Program Files\Microsoft Office\root\Templates\1033")
pytestmark = pytest.mark.skipif(not TPL.exists(), reason="Microsoft Office templates not installed")


def load(name):
    return analyze_pptx(TPL / name)


def all_shapes(prof):
    for s in prof["slides"]:
        yield from s["shapes"]


@pytest.mark.parametrize("name,slides,layouts", [
    ("Training.potx", 19, 13), ("Pitchbook.potx", 6, 16), ("QuizShow.potx", 8, 9),
    ("WidescreenPresentation.potx", 8, 9), ("ClassicPhotoAlbum.potx", 7, 22)])
def test_real_template_structure(name, slides, layouts):
    p = load(name)
    assert p["counts"]["slides"] == slides and p["counts"]["layouts"] == layouts
    assert not [s for s in all_shapes(p) if s["kind"] == "unreadable"]
    json.dumps(p)


def test_potx_is_opened_without_modifying_the_file():
    f = TPL / "Training.potx"
    before = f.stat().st_mtime_ns
    load("Training.potx")
    assert f.stat().st_mtime_ns == before


def test_unsupported_chart_type_does_not_abort_analysis():
    # Pitchbook has a 3-D pie, which python-pptx cannot classify; the XML fallback must report it
    p = load("Pitchbook.potx")
    # charts often live inside content placeholders, so select by the "chart" key, not kind
    charts = [s["chart"] for s in all_shapes(p) if "chart" in s]
    assert len(charts) == p["package_parts"]["charts"] == 5  # every chart part is accounted for
    pie = [c for c in charts if "pie3DChart" in c["plot_types"]]
    assert len(pie) == 1 and pie[0]["type"] is None and pie[0]["series"] == 4


def test_widescreen_template_geometry():
    p = load("WidescreenPresentation.potx")
    assert (p["slide_size"]["width_in"], p["slide_size"]["height_in"]) == (10.0, 5.625)


