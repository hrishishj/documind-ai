"""Answer-quality eval for document Q&A (RAG) with the REAL embedding model and the REAL LLM.

    cd backend
    python -X utf8 -I tools/eval_qa.py            # -> docs/eval_qa_results.json + docs/EVAL_QA_RESULTS.md

Goes beyond retrieval recall (tests/test_rag_eval.py): it asks each ground-truth question through the full workflow and
grades the ANSWER. Per answerable question:
  correct        the expected fact appears in the answer text (numbers compared ignoring thousands separators)
  cited          the answer carries at least one [n] citation that survived verification
  right_file     one citation points at a file whose text really contains the fact (computed from the extracted
                 passages: the same figure appears in both the XLS statements and the 10-K text, so either is a valid citation)
  label_file     stricter: a citation points at the specific file the question was written from
  supported      the cited passages themselves contain the expected fact (citation is real evidence, not decoration)
Unanswerable / off-topic questions pass when the system abstains: no citations and a plain "could not find"/chat reply
that states no figure that was not already in the question.

Grader note (disclosed because it changed after the first run): the first version scored `right_file` against the single file
a question was written from, and counted years echoed from the question ("2031") as invented figures. Both were grader bugs,
found by reading the failures; they are fixed here and `label_file` keeps the strict number visible.
Questions: tests/rag_eval_data.py (tuning + held-out; written from facts read out of the files) plus the UNANSWERABLE list below.
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.llm.provider import GroqLLM  # noqa: E402
from app.observability.trace import RunTrace, use_trace  # noqa: E402
from app.rag.embeddings import get_embedder  # noqa: E402
from app.rag.extract import extract_chunks  # noqa: E402
from app.rag.retrieve import Indexer  # noqa: E402
from app.rag.store import MemoryStore  # noqa: E402
from app.services.storage import LocalStorage  # noqa: E402
from app.workflows.graph import Deps, build_graph  # noqa: E402
from tests.eval.grade import fact_in, grade_qa_abstain, grade_qa_answerable, percentile  # noqa: E402
from tests.rag_eval_data import HELD_OUT, OFF_TOPIC, QUESTIONS  # noqa: E402

OUT_JSON, OUT_MD = ROOT / "docs" / "eval_qa_results.json", ROOT / "docs" / "EVAL_QA_RESULTS.md"
R = ROOT / "sample_templates"
FILES = [("docx", "sec_filing.docx", R / "docx/sec_filing_sample.docx"), ("xls", "sec_filing.xls", R / "xlsx/sec_filing_sample.xls"),
         ("pdf", "investor_presentation.pdf", R / "pdf/ael_investors_presentation.pdf")]
# On-topic in style but NOT answerable from these documents: a faithful system must say so instead of inventing a figure.
UNANSWERABLE = [
    "What was the company's total revenue in fiscal 2031?",
    "Who is the chief executive of the investor-presentation company's main competitor?",
    "How many employees work in the Antarctic office?",
    "What dividend per share did the company pay in 2019?",
]


def ask(graph, files, q, sid):
    trace, t0 = RunTrace(sid), time.perf_counter()
    err = ""
    out: dict = {}
    try:
        with use_trace(trace):
            out = graph.invoke({"session_id": sid, "user_message": q, "history": [], "files": files},
                               {"configurable": {"thread_id": f"{sid}-{abs(hash(q))}"}})
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:160]}"
    return out, trace, (time.perf_counter() - t0) * 1000, err


def main() -> int:
    emb = get_embedder()
    ix = Indexer(emb, MemoryStore())
    for fid, name, p in FILES:
        ix.index_file("qa", fid, name, p)
    holders_text = {fid: " ".join(c.text for c in extract_chunks(p)) for fid, _, p in FILES}
    files = [{"id": fid, "name": name, "path": "-", "format": fid, "kind": "upload", "index_status": "ready"} for fid, name, _ in FILES]
    llm = GroqLLM()
    graph = build_graph(Deps(llm, LocalStorage(Path("/nonexistent-eval")), retriever=ix), checkpointer=MemorySaver())
    rows = []
    items = [("tuning", q, n, w) for q, n, w in QUESTIONS] + [("held_out", q, n, w) for q, n, w in HELD_OUT] + \
            [("unanswerable", q, None, None) for q in UNANSWERABLE] + [("off_topic", q, None, None) for q in OFF_TOPIC]
    for i, (kind, q, needle, which) in enumerate(items, 1):
        out, trace, ms, err = ask(graph, files, q, "qa")
        resp, cites = out.get("response", ""), out.get("citations") or []
        tot = trace.totals()
        row = {"set": kind, "q": q, "expected": needle, "file": which, "response": resp[:300], "n_cites": len(cites), "crash": err,
               "duration_ms": round(ms, 1), "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"],
               "cost_usd": tot["cost_usd"], "llm_calls": tot["llm_calls"], "llm_retries": tot["llm_retries"]}
        if needle:
            holders = {fid for fid, txt in holders_text.items() if fact_in(txt, needle)}
            row.update(cited_files=sorted({c.get("file_id") for c in cites}), holders=sorted(holders),
                       **grade_qa_answerable(resp, cites, needle, which, holders))
        else:
            row.update(grade_qa_abstain(kind, resp, q, cites))
        rows.append(row)
        print(f"[{i}/{len(items)}] {kind:<12} {'PASS' if row['pass'] else 'FAIL'} {ms / 1000:5.1f}s  {q[:60]}", flush=True)

    def rate(sel, f):
        sel = list(sel)
        k = sum(1 for r in sel if f(r))
        return {"n": len(sel), "pass": k, "rate": round(k / len(sel), 3) if sel else None}
    ans = [r for r in rows if r["expected"]]
    held = [r for r in rows if r["set"] == "held_out"]
    abst = [r for r in rows if not r["expected"]]
    lat = [r["duration_ms"] / 1000 for r in rows]
    n = len(rows)
    summary = {"answerable_pass": rate(ans, lambda r: r["pass"]), "answer_correct": rate(ans, lambda r: r["correct"]),
               "has_citation": rate(ans, lambda r: r["cited"]), "citation_right_file": rate(ans, lambda r: r["right_file"]),
               "citation_label_file": rate(ans, lambda r: r["label_file"]),
               "citation_supported_by_snippet": rate(ans, lambda r: r["supported"]),
               "held_out_pass": rate(held, lambda r: r["pass"]),
               "abstain_unanswerable": rate([r for r in rows if r["set"] == "unanswerable"], lambda r: r["pass"]),
               "abstain_off_topic": rate([r for r in rows if r["set"] == "off_topic"], lambda r: r["pass"]),
               "abstain_all": rate(abst, lambda r: r["pass"]),
               "latency": {"p50_s": round(percentile(lat, 50), 2), "p95_s": round(percentile(lat, 95), 2), "max_s": round(max(lat), 2)},
               "tokens_mean_in": round(sum(r["input_tokens"] for r in rows) / n), "tokens_mean_out": round(sum(r["output_tokens"] for r in rows) / n),
               "cost_total": round(sum(r["cost_usd"] for r in rows), 5), "cost_mean": round(sum(r["cost_usd"] for r in rows) / n, 5),
               "crashes": sum(1 for r in rows if r["crash"])}
    st = get_settings()
    data = {"meta": {"date": dt.date.today().isoformat(), "model": st.groq_model, "embedding": st.embedding_model,
                     "price_in": st.price_input_per_mtok, "price_out": st.price_output_per_mtok}, "summary": summary, "rows": rows}
    OUT_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    OUT_MD.write_text(render_md(data), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


def pct(r):
    return f"{r['pass']}/{r['n']} ({'n/a' if r['rate'] is None else str(round(r['rate'] * 100)) + '%'})"


def render_md(d: dict) -> str:
    s, m = d["summary"], d["meta"]
    out = ["# Document Q&A answer evaluation (generated; do not edit by hand)", "",
           f"Generated by `backend/tools/eval_qa.py` on {m['date']} with **{m['model']}** and local `{m['embedding']}` embeddings over the three real "
           "sample documents (SEC filing as DOCX, its financial statements as XLS, an investor deck as PDF). Ground truth was read out of the "
           "files (`backend/tests/rag_eval_data.py`). The *held-out* questions were written after the retrieval settings were frozen. "
           "Single run, every question listed; LLM output is non-deterministic.", "",
           "## Headline", "", "| Metric | Result |", "|---|---|",
           f"| Answerable questions answered correctly **and** cited to the right file | {pct(s['answerable_pass'])} |",
           f"| &nbsp;&nbsp;expected fact present in the answer | {pct(s['answer_correct'])} |",
           f"| &nbsp;&nbsp;answer carries a verified citation | {pct(s['has_citation'])} |",
           f"| &nbsp;&nbsp;a citation points at a file that really contains the fact | {pct(s['citation_right_file'])} |",
           f"| &nbsp;&nbsp;(stricter) a citation points at the specific file the question was written from | {pct(s['citation_label_file'])} |",
           f"| &nbsp;&nbsp;the cited snippet itself contains the fact | {pct(s['citation_supported_by_snippet'])} |",
           f"| Held-out questions only | {pct(s['held_out_pass'])} |",
           f"| Abstains on unanswerable questions (no invented figure, no citation) | {pct(s['abstain_unanswerable'])} |",
           f"| Treats off-topic messages as chat (no citation) | {pct(s['abstain_off_topic'])} |",
           f"| Latency p50 / p95 / max | {s['latency']['p50_s']} s / {s['latency']['p95_s']} s / {s['latency']['max_s']} s |",
           f"| Mean tokens per question (in / out) | {s['tokens_mean_in']} / {s['tokens_mean_out']} |",
           f"| Cost per question (mean) / total | ${s['cost_mean']:.5f} / ${s['cost_total']:.5f} |",
           f"| Crashed turns | {s['crashes']} |", "",
           f"Cost at ${m['price_in']}/1M input and ${m['price_out']}/1M output tokens. Latency includes the rate-limit governor's pacing "
           "(Groq free tier, 8,000 tokens/min). \"supported\" uses the snippet shown to the user, which is a short excerpt, so it is "
           "stricter than \"the whole cited passage supports it\".", "",
           "## Every question", "", "| Set | Question | Pass | Correct | Cited | File | Snippet | Latency |", "|---|---|---|---|---|---|---|---|"]
    yn = lambda v: "-" if v is None else ("yes" if v else "no")  # noqa: E731
    for r in d["rows"]:
        out.append(f"| {r['set']} | {r['q']} | {'PASS' if r['pass'] else '**FAIL**'} | {yn(r.get('correct'))} | {yn(r.get('cited')) if r['expected'] else r['n_cites']} | "
                   f"{yn(r.get('right_file'))} | {yn(r.get('supported'))} | {r['duration_ms'] / 1000:.1f} s |")
    fails = [r for r in d["rows"] if not r["pass"]]
    out += ["", "## Failures", ""]
    if not fails:
        out.append("None in this run.")
    for r in fails:
        out += [f"- **{r['q']}** ({r['set']}; expected `{r['expected']}`): {r['response']!r}" + (f" crash: {r['crash']}" if r["crash"] else "")]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
