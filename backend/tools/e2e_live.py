"""End-to-end check over HTTP against a RUNNING server (python -m uvicorn app.main:app --port 8000).

    python tools/e2e_live.py <path-to-.pptx|.potx|.docx|.xlsx> "<instruction>" [out_dir]
"""
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"


def main():
    src, instruction = Path(sys.argv[1]), sys.argv[2]
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.cwd()
    with httpx.Client(base_url=BASE, timeout=180) as c:
        print("health:", c.get("/health").json())
        sid = c.post("/sessions", json={}).json()["id"]
        r = c.post(f"/sessions/{sid}/files", files={"file": (src.name, src.read_bytes())})
        r.raise_for_status()
        up = r.json()
        print("uploaded:", up["file"]["original_name"], up["file"]["kind"], json.dumps(up["analysis"]["counts"]))
        t = time.time()
        final = None
        with c.stream("POST", f"/sessions/{sid}/chat", json={"message": instruction}) as resp:
            ev = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: "):
                    d = json.loads(line[6:])
                    print(f"  [{time.time() - t:5.1f}s] {ev}: {d.get('label') or d.get('code') or ''}")
                    if ev in ("final", "error"):
                        final = (ev, d)
        ev, d = final
        print("\nRESPONSE:", d.get("content") or d)
        art = (d.get("meta") or {}).get("artifact")
        if art:
            data = c.get(f"/sessions/{sid}/artifacts/{art['id']}/versions/{art['version']}/download").content
            out = out_dir / f"e2e_result_v{art['version']}.{art['format']}"
            out.write_bytes(data)
            print("saved:", out, len(data), "bytes")
        print("session:", sid)


if __name__ == "__main__":
    main()
