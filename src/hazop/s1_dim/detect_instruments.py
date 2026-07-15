"""
Instrument-bubble detector: find circle-shaped vector paths on each process
sheet and associate the text spans inside them (function letters + loop
number), producing a per-page instrument index.

Detection rule (validated on page 7):
  - a "drawing" path whose vertices are equidistant from their centroid
    (radius std/mean < 3%) with >= 8 segments is a circle
  - instrument bubbles on these sheets have radius ~8-20 pt
  - text spans whose center falls inside (or within pad of) the circle bbox
    belong to the bubble: top line = function letters (PT, FIC...),
    bottom line = loop number (00701...)

Usage:
    python3 scripts/detect_instruments.py            # all process sheets (4-12)
    python3 scripts/detect_instruments.py 7          # single page

Output: output/instrument_index.json + table on stdout
"""
import fitz
import json
import math
import sys

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)  # legend sheets 1-3 excluded


def find_circles(page):
    """Find circle-shaped vector paths.

    Key property of this ZWCAD export: straight geometry is flattened into
    loose single-segment paths, but CURVES survive as grouped multi-segment
    polylines. So any grouped path (>=8 items) whose vertices are all the
    same distance from their centroid is a drawn circle.
    """
    circles = []
    for d in page.get_drawings():
        items = d["items"]
        if len(items) < 8:
            continue  # straight geometry / small fragments - not a circle
        # collect the polyline vertices ("l" = line item, "c" = bezier item)
        pts = []
        for it in items:
            op = it[0]
            if op == "l":
                pts.append(it[1]); pts.append(it[2])
            elif op == "c":
                pts.append(it[1]); pts.append(it[4])
        if len(pts) < 8:
            continue
        # circle test: distance from centroid is constant (spread < 3%)
        cx = sum(p.x for p in pts) / len(pts)
        cy = sum(p.y for p in pts) / len(pts)
        radii = [math.hypot(p.x - cx, p.y - cy) for p in pts]
        rmean = sum(radii) / len(radii)
        if rmean == 0:
            continue
        rstd = math.sqrt(sum((r - rmean) ** 2 for r in radii) / len(radii))
        if rstd / rmean < 0.03:
            circles.append({"cx": cx, "cy": cy, "r": rmean, "width": d.get("width")})
    return circles


def get_spans(page):
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                x0, y0, x1, y1 = span["bbox"]
                spans.append({
                    "text": span["text"].strip(),
                    "cx": (x0 + x1) / 2,
                    "cy": (y0 + y1) / 2,
                    "bbox": span["bbox"],
                })
    return [s for s in spans if s["text"]]


def merge_concentric(circles, tol=3.0):
    """Doubled bubbles (e.g. shared-display) are two concentric circles;
    keep one record per center, remembering multiplicity."""
    merged = []
    for c in sorted(circles, key=lambda c: -c["r"]):
        for m in merged:
            if math.hypot(c["cx"] - m["cx"], c["cy"] - m["cy"]) < tol:
                m["n_rings"] += 1
                break
        else:
            c = dict(c, n_rings=1)
            merged.append(c)
    return merged


def index_page(doc, page_num):
    """Detect every circle on the page and attach the text drawn inside it.

    For instrument bubbles the text is two stacked lines: ISA function
    letters on top (PT, LICA, ...) and the loop number below (00502) -
    sorting spans by y reconstructs that reading order.
    """
    page = doc[page_num - 1]
    circles = merge_concentric(find_circles(page))
    spans = get_spans(page)
    records = []
    for c in circles:
        # a span belongs to the bubble if its center falls inside the circle
        inside = [
            s for s in spans
            if math.hypot(s["cx"] - c["cx"], s["cy"] - c["cy"]) <= c["r"] + 2
        ]
        inside.sort(key=lambda s: s["cy"])  # top line first
        # doubled circles carry doubled text spans; drop exact duplicates
        seen = set()
        deduped = []
        for s in inside:
            key = (s["text"], round(s["cx"]), round(s["cy"]))
            if key not in seen:
                seen.add(key)
                deduped.append(s)
        inside = deduped
        records.append({
            "page": page_num,
            "center": [round(c["cx"], 1), round(c["cy"], 1)],
            "radius": round(c["r"], 1),
            "n_rings": c["n_rings"],
            "class": classify(c["r"]),
            "texts": [s["text"] for s in inside],
            "tag": " ".join(s["text"] for s in inside),
        })
    return records


def classify(r):
    """Radius stratification observed across all 9 process sheets."""
    if r >= 15.5:
        return "instrument"      # ISA bubble with function letters + loop no.
    if 5.5 <= r < 12:
        return "small-circle"    # purge points (P), motor (M), package-unit marks
    if 3.0 <= r < 5.5:
        return "connector-dot"   # line junction / leader dots
    return "tiny-dot"            # leader-line endpoints, title-block dots


if __name__ == "__main__":
    doc = fitz.open(PDF_PATH)
    pages = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PROCESS_PAGES)
    all_records = []
    for p in pages:
        recs = index_page(doc, p)
        all_records.extend(recs)
        tagged = sum(1 for r in recs if r["texts"])
        print(f"page {p}: {len(recs)} bubbles, {tagged} with text")
        for r in recs:
            print(f"   ({r['center'][0]:7.1f},{r['center'][1]:7.1f}) r={r['radius']:5.1f} "
                  f"rings={r['n_rings']}  {r['tag'] or '<empty>'}")
    with open("output/instrument_index.json", "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    print(f"\ntotal: {len(all_records)} bubbles -> output/instrument_index.json")
