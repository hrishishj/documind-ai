"""Manual live check: run the real graph with real Groq on generated sample documents.

    python tools/live_check.py            (needs GROQ_API_KEY in .env; consumes a few thousand tokens)
"""
import json
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.document_engine.samples import build_docx, build_pptx, build_xlsx  # noqa: E402
from app.llm.provider import GroqLLM  # noqa: E402
from app.services.storage import LocalStorage  # noqa: E402
from app.workflows.graph import Deps, build_graph  # noqa: E402

CASES = [
    ("docx", build_docx, "Change the report title to 'FY2025 Annual Report' and add a table row for West with 5 and 6."),
    ("pptx", build_pptx, "Add a slide titled 'Agenda' with bullets Intro, Results, Next steps right after the first slide, and change slide 3's title to 'Chart Review'."),
    ("xlsx", build_xlsx, "Add a row for West with revenue 77.5, margin 0.2 and date 2024-04-01."),
    ("docx", build_docx, "What is the title of the document and how many tables does it have?"),
]


def main():
    storage = LocalStorage(Path(tempfile.mkdtemp()) / "store")
    llm = GroqLLM()
    deps = Deps(llm, storage)
    graph = build_graph(deps)
    print(f"model={get_settings().groq_model}")
    chosen = [int(a) for a in sys.argv[1:]]  # optional: run only these case numbers (0-based)
    for n, (fmt, builder, msg) in enumerate(CASES):
        if chosen and n not in chosen:
            continue
        tmp = Path(tempfile.mkdtemp()) / f"x.{fmt}"
        builder(tmp)
        path, _ = storage.save_upload("live", tmp.read_bytes(), fmt)
        files = [{"id": "f1", "name": f"sample.{fmt}", "path": storage.rel(path), "format": fmt, "kind": "upload"}]
        t = time.time()
        out = graph.invoke({"session_id": "live", "user_message": msg, "history": [], "files": files})
        dt = time.time() - t
        print(f"\n[{fmt}] {msg}\n  -> intent={out.get('intent')} attempts={out.get('attempts')} {dt:.1f}s")
        for o in (out.get("plan") or {}).get("ops", []):
            print("     op:", json.dumps({k: v for k, v in o.items() if v is not None}, ensure_ascii=False)[:150])
        if out.get("plan_errors"):
            print("     errors:", out["plan_errors"][:2])
        print("  response:", out["response"].replace("\n", " ")[:260])
        if out.get("render"):
            print("  preserved:", out["render"]["preserved"], out["render"]["violations"])


if __name__ == "__main__":
    main()
