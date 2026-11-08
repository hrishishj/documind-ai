import json, sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[3]  # repo root
sys.path.insert(0, str(ROOT / "backend"))
from app.document_engine.pptx.analyzer import analyze_pptx

truth_file, pptx_file = sys.argv[1], sys.argv[2]
t = json.load(open(truth_file, encoding="utf-8-sig"))
p = analyze_pptx(pptx_file)
TOL = 0.2
bad, shapes_checked = [], 0

if abs(p["slide_size"]["width_in"] * 72 - t["width_pt"]) > TOL or abs(p["slide_size"]["height_in"] * 72 - t["height_pt"]) > TOL:
    bad.append(("slide_size", p["slide_size"], (t["width_pt"], t["height_pt"])))
mine_layouts = [l["name"] for l in p["masters"][0]["layouts"]]
if mine_layouts != t["layouts"]:
    bad.append(("layouts", mine_layouts, t["layouts"]))
if len(p["slides"]) != len(t["slides"]):
    bad.append(("slide_count", len(p["slides"]), len(t["slides"])))

for ms, ts in zip(p["slides"], t["slides"]):
    if ms["layout"] != ts["layout"]:
        bad.append((f"slide{ms['index']}.layout", ms["layout"], ts["layout"]))
    tshapes = ts["shapes"] if isinstance(ts["shapes"], list) else [ts["shapes"]]
    if ms["shape_count"] != len(tshapes):
        bad.append((f"slide{ms['index']}.shape_count", ms["shape_count"], len(tshapes)))
    for a, b in zip(ms["shapes"], tshapes):
        shapes_checked += 1
        if a["name"] != b["name"]:
            bad.append((f"slide{ms['index']}.name", a["name"], b["name"]))
            continue
        for mine_k, truth_k in (("x_in", "left"), ("y_in", "top"), ("w_in", "width"), ("h_in", "height")):
            if mine_k not in a:
                bad.append((f"slide{ms['index']}:{a['name']}", mine_k + " missing", b[truth_k]))
            elif abs(a[mine_k] * 72 - b[truth_k]) > TOL:
                bad.append((f"slide{ms['index']}:{a['name']}", mine_k, round(a[mine_k] * 72, 2), b[truth_k]))
print(f"slides={len(p['slides'])} layouts={len(mine_layouts)} shapes_compared={shapes_checked} mismatches={len(bad)}")
for x in bad[:15]:
    print("  ", x)
