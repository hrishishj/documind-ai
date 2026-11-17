"""Grading and aggregation for the edit eval. Pure functions, unit-tested, no network."""
import math
from pathlib import Path


def extract_text(path: Path) -> tuple[list[str], list[str]]:
    """(ordered text chunks, sheet names). Reads the produced FILE, so a claim in the chat reply cannot fake a pass."""
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        from docx import Document
        d = Document(str(path))
        out = [p.text for p in d.paragraphs]
        for t in d.tables:
            out += [c.text for r in t.rows for c in r.cells]
        for s in d.sections:
            out += [p.text for p in s.header.paragraphs] + [p.text for p in s.footer.paragraphs]
        return [x for x in out if x], []
    if ext == ".pptx":
        from pptx import Presentation
        out = []
        for slide in Presentation(str(path)).slides:
            for sh in slide.shapes:
                if sh.has_text_frame:
                    out.append(sh.text_frame.text)
                if getattr(sh, "has_table", False) and sh.has_table:
                    out += [c.text for r in sh.table.rows for c in r.cells]
        return [x for x in out if x], []
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(str(path))
        out = [str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
        return out, wb.sheetnames
    raise ValueError(f"unsupported file: {ext}")


def grade_file(case: dict, path: Path) -> dict:
    """Content checks for one produced document. Returns {check: bool} for every check the case defines."""
    chunks, sheets = extract_text(path)
    blob = "\n".join(chunks)
    res: dict[str, bool] = {}
    if "contains" in case:
        res["contains"] = all(t in blob for t in case["contains"])
    if "absent" in case:
        res["absent"] = all(t not in blob for t in case["absent"])
    if "keeps" in case:
        res["keeps"] = all(t in blob for t in case["keeps"])
    if "count" in case:
        res["count"] = all(blob.count(t) == n for t, n in case["count"].items())
    if "sheets" in case:
        res["sheets"] = all(s in sheets for s in case["sheets"])
    if "order" in case:
        pos = [blob.find(t) for t in case["order"]]
        res["order"] = all(p >= 0 for p in pos) and pos == sorted(pos)
    return res


def grade_case(case: dict, intent: str, produced: Path | None, status: str) -> dict:
    """One row of results. `status` is the run status from the trace (ok | error | gave_up)."""
    expect = case["expect"]
    if expect == "edit":
        intent_ok = intent == "edit"
    elif expect == "clarify_or_edit":
        intent_ok = intent in ("clarify", "edit")  # either is a defensible reading of an ambiguous request
    elif expect == "no_edit":
        intent_ok = True
    else:
        intent_ok = intent == expect
    checks = grade_file(case, produced) if produced else {}
    if expect == "edit":
        success = produced is not None and bool(checks) and all(checks.values())
    elif expect == "no_edit":
        success = produced is None  # refusing / declining is correct; fabricating an edit is not
    elif expect == "clarify_or_edit":
        success = intent_ok and (produced is None or (bool(checks) and all(checks.values())))
    else:
        success = intent_ok and produced is None
    return {"id": case["id"], "expect": expect, "intent": intent, "intent_ok": intent_ok, "produced": produced is not None,
            "checks": checks, "success": success, "status": status}


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return s[k]


def summarize(rows: list[dict]) -> dict:
    """Aggregate metrics. Each row = grade_case() output + trace fields (duration_ms, tokens, cost, attempts, preserved)."""
    def rate(sel, f):
        sel = list(sel)
        k = sum(1 for r in sel if f(r))
        return {"n": len(sel), "pass": k, "rate": round(k / len(sel), 3) if sel else None}

    edits = [r for r in rows if r["expect"] == "edit"]
    produced = [r for r in edits if r["produced"]]
    lat = [r["duration_ms"] / 1000 for r in rows]

    def stats(sel):
        v = [r["duration_ms"] / 1000 for r in sel]
        return {"n": len(v), "p50_s": round(percentile(v, 50), 2), "p95_s": round(percentile(v, 95), 2),
                "mean_s": round(sum(v) / len(v), 2) if v else 0.0}
    by_exp: dict[str, list] = {}
    for r in rows:
        by_exp.setdefault(r["expect"], []).append(r)
    n = len(rows) or 1
    return {
        "cases": len(rows),
        "task_success": rate(rows, lambda r: r["success"]),
        "edit_task_success": rate(edits, lambda r: r["success"]),
        "routing_accuracy": rate(rows, lambda r: r["intent_ok"]),
        "edit_first_try_valid": rate(edits, lambda r: r["produced"] and r["attempts"] <= 1),
        "edit_eventually_valid": rate(edits, lambda r: r["produced"]),
        "edit_preservation": rate(produced, lambda r: r["preserved"]),
        "edit_content_correct": rate(produced, lambda r: all(r["checks"].values())),
        "mean_attempts_when_edited": round(sum(r["attempts"] for r in produced) / len(produced), 2) if produced else None,
        "latency_all": {**stats(rows), "max_s": round(max(lat), 2) if lat else 0.0},
        "latency_by_expectation": {k: stats(v) for k, v in sorted(by_exp.items())},
        "tokens": {"mean_input": round(sum(r["input_tokens"] for r in rows) / n), "mean_output": round(sum(r["output_tokens"] for r in rows) / n),
                   "total_input": sum(r["input_tokens"] for r in rows), "total_output": sum(r["output_tokens"] for r in rows),
                   "mean_llm_calls": round(sum(r["llm_calls"] for r in rows) / n, 2)},
        "cost_usd": {"total": round(sum(r["cost_usd"] for r in rows), 5), "mean_per_request": round(sum(r["cost_usd"] for r in rows) / n, 5),
                     "mean_per_edit": round(sum(r["cost_usd"] for r in edits) / len(edits), 5) if edits else None},
        "llm_failures": sum(r["llm_failures"] for r in rows), "llm_retries": sum(r["llm_retries"] for r in rows),
    }


# ---------------------------------------------------------------- document Q&A grading (used by tools/eval_qa.py)
import re  # noqa: E402

ABSTAIN = re.compile(r"could(n't| not) find|not (found|mentioned|stated|provided|available|included|contain)|no (information|mention|data)|"
                     r"don't (see|have)|doesn't (say|contain|mention|state)|unable to (find|locate)|cannot (find|determine)|do(es)? not provide", re.I)
_NUM = re.compile(r"\d[\d,.]{2,}")


def _norm(s: str) -> str:
    return re.sub(r"[,\s ]+", "", s).lower()


def fact_in(text: str, needle: str) -> bool:
    """Is the expected fact in the text? Thousands separators and spacing are ignored ('217778' == '217,778')."""
    return _norm(needle) in _norm(text)


def new_numbers(response: str, question: str) -> list[str]:
    """Figures in the response that were not already in the question (a year echoed back is not an invented figure)."""
    seen = {n.rstrip(".,") for n in _NUM.findall(question)}
    return [n for n in _NUM.findall(response) if n.rstrip(".,") not in seen]


def grade_qa_answerable(response: str, citations: list[dict], needle: str, label_file: str, holders: set[str]) -> dict:
    """correct: fact in the answer. cited: a verified citation exists. right_file: a citation points at a file whose text
    really contains the fact. label_file: a citation points at the file the question was written from (stricter).
    supported: the snippet shown to the user contains the fact."""
    cited_files = {c.get("file_id") for c in citations}
    r = {"correct": fact_in(response, needle), "cited": bool(citations), "right_file": bool(cited_files & holders),
         "label_file": label_file in cited_files, "supported": any(fact_in(c.get("snippet", ""), needle) for c in citations)}
    r["pass"] = r["correct"] and r["cited"] and r["right_file"]
    return r


def grade_qa_abstain(kind: str, response: str, question: str, citations: list[dict]) -> dict:
    """Unanswerable: no citation, says it could not find it, states no new figure. Off-topic: no citation (it is just chat)."""
    ok = not citations and (kind == "off_topic" or bool(ABSTAIN.search(response))) and not (kind == "unanswerable" and new_numbers(response, question))
    return {"abstained": ok, "pass": ok}
