"""Live end-to-end check of web research against a RUNNING server (real Tavily + Groq).

    python tools/web_e2e.py
"""
import io
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.document_engine.samples import build_docx  # noqa: E402

BASE = "http://127.0.0.1:8000"


def chat(c, sid, msg):
    t0, labels, final = time.time(), [], None
    with c.stream("POST", f"/sessions/{sid}/chat", json={"message": msg}) as resp:
        ev = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if ev == "node":
                    labels.append(d["label"])
                if ev in ("final", "error"):
                    final = (ev, d)
    return final, labels, time.time() - t0


def main():
    with httpx.Client(base_url=BASE, timeout=300) as c:
        print("health:", c.get("/health").json())
        sid = c.post("/sessions", json={}).json()["id"]

        print("\n=== A. web question (no documents) ===")
        (kind, d), labels, dt = chat(c, sid, "Search the web: what energy density do solid-state batteries reach and which companies lead the field?")
        print(f"[{dt:.1f}s] steps={labels}\nA: {d.get('content', d)[:600]}")
        for ci in d.get("citations", []):
            print(f"   [{ci['ref']}] ({ci['source_type']}) {ci['title'][:60]} -> {ci['url']}")
        assert kind == "final" and d["citations"] and all(x["source_type"] == "web" and x["url"].startswith("http") for x in d["citations"]), d
        assert "Searching the web" in labels

        print("\n=== B. edit that uses web facts ===")
        tmp = Path(tempfile.mkdtemp()) / "Quarterly Report.docx"
        build_docx(tmp)
        c.post(f"/sessions/{sid}/files", files={"file": (tmp.name, tmp.read_bytes())}).raise_for_status()
        (kind, d), labels, dt = chat(c, sid, "Search the web for the typical energy density of solid-state batteries and add a short paragraph about it after the introduction in the report.")
        print(f"[{dt:.1f}s] steps={labels}\nA: {d.get('content', d)[:900]}")
        art = d["meta"].get("artifact")
        assert kind == "final" and art, d
        out = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/{art['version']}/download").content
        paras = [p.text for p in Document(io.BytesIO(out)).paragraphs]
        print("document start:")
        for p in paras[:5]:
            print("   |", p[:260])
        print("document tail:")
        for p in paras[-4:]:
            print("   |", p[:150])
        assert "Sources" in paras and any(p.startswith("[1] ") and "http" in p for p in paras), paras
        assert len(d["citations"]) >= 1 and all(x["source_type"] == "web" for x in d["citations"])
        c.delete(f"/sessions/{sid}")
    print("\nWEB E2E PASSED")


if __name__ == "__main__":
    main()
