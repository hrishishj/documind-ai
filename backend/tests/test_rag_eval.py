"""Retrieval quality on the REAL bundled documents with the REAL embedding model (slow: indexes ~820 passages).

Run with:  pytest --runslow tests/test_rag_eval.py -s

QUESTIONS were used to choose LEX_WEIGHT / MIN_RELEVANCE; HELD_OUT was written afterwards and evaluated once, so it is
the honest estimate. Thresholds below leave a little slack for embedding-model drift but fail on real regressions.
"""
import pytest

from app.core.config import REPO_ROOT
from tests.rag_eval_data import HELD_OUT, OFF_TOPIC, QUESTIONS, contains

pytestmark = pytest.mark.slow
R = REPO_ROOT / "sample_templates"
FILES = [("docx", "sec_filing.docx", R / "docx/sec_filing_sample.docx"), ("xls", "sec_filing.xls", R / "xlsx/sec_filing_sample.xls"),
         ("pdf", "adani_presentation.pdf", R / "pdf/ael_investors_presentation.pdf")]


@pytest.fixture(scope="module")
def idx():
    if not all(p.exists() for _, _, p in FILES):
        pytest.skip("sample documents missing")
    from app.rag.embeddings import get_embedder
    from app.rag.retrieve import Indexer
    from app.rag.store import MemoryStore
    ix = Indexer(get_embedder(), MemoryStore())
    for fid, name, p in FILES:
        ix.index_file("ns", fid, name, p)
    return ix


def rank(ix, q, needle, k=12):
    for i, s in enumerate(ix.search("ns", q, k=k), 1):
        if contains(s.text, needle):
            return i
    return None


def recall(ix, qs, k):
    return sum(1 for q, needle, _ in qs if (r := rank(ix, q, needle)) is not None and r <= k)


def test_tuning_set_recall(idx):
    n = len(QUESTIONS)
    r1, r3, r8 = (recall(idx, QUESTIONS, k) for k in (1, 3, 8))
    print(f"\ntuning set  recall@1={r1}/{n} @3={r3}/{n} @8={r8}/{n}")
    assert r8 >= n - 1 and r3 >= n - 3 and r1 >= n // 2


def test_held_out_recall(idx):
    n = len(HELD_OUT)
    r1, r3, r8 = (recall(idx, HELD_OUT, k) for k in (1, 3, 8))
    print(f"\nheld-out    recall@1={r1}/{n} @3={r3}/{n} @8={r8}/{n}")
    assert r8 >= n - 1 and r1 >= n // 2


def test_relevance_cutoff_separates_on_topic_from_off_topic(idx):
    from app.rag.answer import MIN_RELEVANCE
    on = [idx.search("ns", q, k=1)[0].score for q, _, _ in QUESTIONS + HELD_OUT]
    off = [idx.search("ns", m, k=1)[0].score for m in OFF_TOPIC]
    print(f"\non-topic min={min(on):.3f}  off-topic max={max(off):.3f}  cutoff={MIN_RELEVANCE}")
    assert min(on) >= MIN_RELEVANCE, "a real question would be treated as unrelated"
    assert max(off) < MIN_RELEVANCE, "small talk would be answered 'from the documents'"


def test_answers_come_from_the_right_file_type(idx):
    # the spreadsheet figure must be citable to its sheet, the filing field to a paragraph/table, the deck fact to a page
    top = {q: idx.search("ns", q, k=3) for q, _, _ in QUESTIONS}
    pdf_q = "What was India's real GDP growth?"
    assert any(s.locator.startswith("p.") and contains(s.text, "7.1%") for s in top[pdf_q])
    xls_q = "How much did the company spend on research and development in 2026?"
    assert any(s.locator.startswith("Sheet '") and contains(s.text, "35562") for s in top[xls_q])
