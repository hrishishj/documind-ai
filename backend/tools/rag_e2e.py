"""Live end-to-end Q&A check against a RUNNING server with real Groq + Pinecone + embeddings.

    python tools/rag_e2e.py            (uploads the three sample documents, waits for indexing, asks questions)
"""
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
R = Path(__file__).resolve().parents[2] / "sample_templates"
FILES = [R / "docx/sec_filing_sample.docx", R / "xlsx/sec_filing_sample.xls", R / "pdf/ael_investors_presentation.pdf"]

# (question, expectation shown in the report, substring that must be in the answer or None)
QUESTIONS = [
    ("What was total revenue in fiscal 2026, and how did it compare with 2025?", "331,839 vs 281,724 (millions)", "331"),
    ("How much did the company spend on research and development in fiscal 2026?", "35,562 (millions)", "35"),
    ("What was India's real GDP growth according to the investor presentation?", "7.1%", "7.1"),
    ("What is the company's SEC commission file number?", "001-37845", "37845"),
    ("What is the CEO's annual salary?", "NOT in the documents -> must say it cannot find it", None),
    ("hello, thanks for the help", "small talk -> plain reply, no citations", None),
]


def main():
    out = []
    with httpx.Client(base_url=BASE, timeout=300) as c:
        print("health:", c.get("/health").json())
        sid = c.post("/sessions", json={}).json()["id"]
        for f in FILES:
            r = c.post(f"/sessions/{sid}/files", files={"file": (f.name, f.read_bytes())})
            r.raise_for_status()
        t = time.time()
        while True:
            files = c.get(f"/sessions/{sid}").json()["files"]
            st = {x["original_name"]: (x["index_status"], x["chunk_count"]) for x in files}
            if all(s in ("ready", "failed") for s, _ in st.values()):
                break
            if time.time() - t > 600:
                sys.exit("indexing timed out: " + str(st))
            time.sleep(4)
        print(f"indexed in {time.time() - t:.0f}s: {st}")
        for q, expect, needle in QUESTIONS:
            t0 = time.time()
            final = None
            with c.stream("POST", f"/sessions/{sid}/chat", json={"message": q}) as resp:
                ev = None
                for line in resp.iter_lines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: ") and ev in ("final", "error"):
                        final = (ev, json.loads(line[6:]))
            kind, d = final
            ok = None if needle is None else (needle in d.get("content", "").replace(",", ""))
            print(f"\nQ: {q}\n   expect: {expect}  [{time.time() - t0:.1f}s]")
            print("   A:", d.get("content", d).replace("\n", " ")[:400])
            for ci in d.get("citations", []):
                print(f"   [{ci['ref']}] {ci['title']} - {ci['locator']}: {ci['snippet'][:110]!r}")
            out.append((q, kind, ok, len(d.get("citations", []))))
        c.delete(f"/sessions/{sid}")
    print("\nSUMMARY:", [(q[:38], k, ok, n) for q, k, ok, n in out])


if __name__ == "__main__":
    main()
