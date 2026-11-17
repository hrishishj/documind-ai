"""The eval is only worth reporting if the grader itself is right: test it on files with known content."""
from pathlib import Path

from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from tests.eval.edit_cases import CASES
from tests.eval.grade import extract_text, grade_case, grade_file, percentile, summarize


def test_case_ids_are_unique_and_every_edit_case_has_a_check():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)) >= 25
    for c in CASES:
        if c["expect"] == "edit":
            assert any(k in c for k in ("contains", "absent", "count", "sheets", "order")), c["id"]


def test_the_keeps_and_absent_strings_are_consistent_with_the_real_templates():
    """`keeps` strings must exist in the untouched template (else a perfect edit would be graded as damage), and
    `absent` strings must exist too (else the check passes without the edit doing anything)."""
    root = Path(__file__).resolve().parents[2] / "sample_templates"
    for c in CASES:
        if not c["files"]:
            continue
        blob = "\n".join("\n".join(extract_text(root / f)[0]) for f in c["files"])
        for t in c.get("keeps", []):
            assert t in blob, f"{c['id']}: keeps {t!r} is not in the template"
        for t in c.get("absent", []):
            assert t in blob, f"{c['id']}: absent {t!r} is not in the template, so the check is vacuous"


def test_docx_grading_detects_content_and_collateral_damage(tmp_path):
    p = tmp_path / "a.docx"
    build_docx(p)
    case = dict(contains=["Quarterly Report"], absent=["nonexistent"], keeps=["Details"], count={"Details": 1})
    assert grade_file(case, p) == {"contains": True, "absent": True, "keeps": True, "count": True}
    assert grade_file(dict(contains=["Annual"]), p) == {"contains": False}
    assert grade_file(dict(absent=["Details"]), p) == {"absent": False}


def test_pptx_order_and_xlsx_sheets(tmp_path):
    p, x = tmp_path / "a.pptx", tmp_path / "a.xlsx"
    build_pptx(p)
    build_xlsx(x)
    assert grade_file(dict(order=["Annual Review", "Highlights", "Styled text"]), p) == {"order": True}
    assert grade_file(dict(order=["Styled text", "Annual Review"]), p) == {"order": False}
    assert grade_file(dict(sheets=["Report"]), x) == {"sheets": True} and grade_file(dict(sheets=["Report Q2"]), x) == {"sheets": False}


def test_success_requires_the_right_behaviour_for_each_kind_of_case(tmp_path):
    p = tmp_path / "a.docx"
    build_docx(p)
    edit = dict(id="e", expect="edit", contains=["Quarterly Report"])
    assert grade_case(edit, "edit", p, "ok")["success"] is True
    assert grade_case(edit, "edit", None, "gave_up")["success"] is False            # no file produced
    assert grade_case(dict(edit, contains=["zzz"]), "edit", p, "ok")["success"] is False  # file produced but wrong
    assert grade_case(dict(id="c", expect="chat"), "chat", None, "ok")["success"] is True
    assert grade_case(dict(id="c", expect="chat"), "edit", p, "ok")["success"] is False
    assert grade_case(dict(id="n", expect="no_edit"), "edit", None, "gave_up")["success"] is True
    assert grade_case(dict(id="n", expect="no_edit"), "edit", p, "ok")["success"] is False  # fabricated an edit
    amb = dict(id="a", expect="clarify_or_edit", contains=["Quarterly Report"])
    assert grade_case(amb, "clarify", None, "ok")["success"] is True
    assert grade_case(amb, "edit", p, "ok")["success"] is True
    assert grade_case(dict(amb, contains=["zzz"]), "edit", p, "ok")["success"] is False


def test_percentile_and_summary():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(xs, 50) == 5 and percentile(xs, 95) == 10
    base = dict(expect="edit", intent_ok=True, produced=True, checks={"contains": True}, success=True, attempts=1, preserved=True,
                duration_ms=2000, input_tokens=1000, output_tokens=100, cost_usd=0.001, llm_calls=1, llm_failures=0, llm_retries=0)
    rows = [base, {**base, "attempts": 2, "duration_ms": 6000}, {**base, "produced": False, "success": False, "checks": {}}]
    s = summarize(rows)
    assert s["edit_task_success"] == {"n": 3, "pass": 2, "rate": 0.667}
    assert s["edit_first_try_valid"]["pass"] == 1 and s["edit_eventually_valid"]["pass"] == 2
    assert s["mean_attempts_when_edited"] == 1.5 and s["latency_all"]["max_s"] == 6.0
    assert s["cost_usd"]["total"] == 0.003 and s["tokens"]["total_input"] == 3000


# ---------------------------------------------------------------- Q&A grading
def test_qa_answerable_grading_distinguishes_wrong_file_from_a_second_valid_file():
    from tests.eval.grade import grade_qa_answerable
    cites = [{"file_id": "docx", "snippet": "Operating income 155,237"}]
    r = grade_qa_answerable("Operating income was $155,237 million [1].", cites, "155237", "xls", {"xls", "docx"})
    assert r["pass"] and r["right_file"] and not r["label_file"] and r["supported"]      # valid evidence, not the labelled file
    bad = grade_qa_answerable("Operating income was $155,237 million [1].", cites, "155237", "xls", {"xls"})
    assert not bad["right_file"] and not bad["pass"]                                      # cited a file that lacks the fact
    assert not grade_qa_answerable("It was $1 million [1].", cites, "155237", "xls", {"docx"})["pass"]  # wrong answer
    assert not grade_qa_answerable("Operating income was 155,237.", [], "155237", "xls", {"xls"})["pass"]  # uncited


def test_qa_abstention_grading():
    from tests.eval.grade import grade_qa_abstain, new_numbers
    q = "What was the company's total revenue in fiscal 2031?"
    assert grade_qa_abstain("unanswerable", "I could not find revenue for fiscal 2031 in the documents.", q, [])["pass"]   # year echo is fine
    assert not grade_qa_abstain("unanswerable", "Revenue in fiscal 2031 will be $412,000 million.", q, [])["pass"]         # invented figure
    assert not grade_qa_abstain("unanswerable", "I could not find it.", q, [{"file_id": "x"}])["pass"]                      # citation attached
    assert not grade_qa_abstain("unanswerable", "Revenue is large.", q, [])["pass"]                                         # did not say it was missing
    assert grade_qa_abstain("off_topic", "Hello! How can I help?", "hello there", [])["pass"]
    assert new_numbers("2031 and 412,000", q) == ["412,000"]
