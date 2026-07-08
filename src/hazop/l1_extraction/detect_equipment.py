"""
Equipment detector: find equipment items on each process sheet by combining
three deterministic signals:

  1. tag text     - spans matching the plant convention 24xx-X(X)-nnn(A/B),
                    e.g. 2401-V-002, 2401-K-001A, 2401-PK-001A/B; the nearby
                    Chinese name and spec lines (设计压力/温度, 容积...) are
                    gathered as attributes
  2. capsule geometry - vessels & compressor stages are drawn as two dished-
                    head arcs (grouped polylines, ~half-circle span) facing
                    each other; paired arcs give the equipment bbox
  3. equipment circles - cooler/filter circles have bubble-like geometry but
                    no tag text inside (the "empty bubbles" of the instrument
                    pass)

Usage:  python3 scripts/detect_equipment.py [page]   (default: all 4-12)
Output: output/equipment_index.json + stdout table
"""
import fitz
import json
import math
import re
import sys

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)
CONTENT = (200, 200, 2200, 1450)

TAG_RE = re.compile(r"^24\d{2}-[A-Z]{1,3}-\d{3,4}[A-Z]?(/[A-Z])?$")
SPEC_RE = re.compile(r"设计压力|设计温度|外形尺寸|公称容积|处理量|入口流量|出口压力|入口压力")


def circle_fit(pts):
    """Kasa algebraic circle fit; returns (cx, cy, r, rms) or None."""
    n = len(pts)
    if n < 3:
        return None
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); syy = sum(p[1] ** 2 for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    sxz = sum(p[0] * (p[0] ** 2 + p[1] ** 2) for p in pts)
    syz = sum(p[1] * (p[0] ** 2 + p[1] ** 2) for p in pts)
    sz = sxx + syy
    # solve [sxx sxy sx; sxy syy sy; sx sy n] [a b c] = [sxz syz sz]
    A = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    B = [sxz, syz, sz]
    # gaussian elimination
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        A[col], A[piv] = A[piv], A[col]
        B[col], B[piv] = B[piv], B[col]
        for r in range(col + 1, 3):
            f = A[r][col] / A[col][col]
            for c in range(col, 3):
                A[r][c] -= f * A[col][c]
            B[r] -= f * B[col]
    x = [0.0, 0.0, 0.0]
    for r in (2, 1, 0):
        x[r] = (B[r] - sum(A[r][c] * x[c] for c in range(r + 1, 3))) / A[r][r]
    a, b, c = x
    cx, cy = a / 2, b / 2
    r2 = c + cx ** 2 + cy ** 2
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    rms = math.sqrt(sum((math.hypot(p[0] - cx, p[1] - cy) - r) ** 2 for p in pts) / n)
    return cx, cy, r, rms


def find_arcs(page):
    """Grouped polylines that fit a circle; classify full circles vs arcs."""
    arcs = []
    x0, y0, x1, y1 = CONTENT
    for d in page.get_drawings():
        items = d["items"]
        if len(items) < 6:
            continue
        pts = []
        for it in items:
            if it[0] == "l":
                pts.append((it[1].x, it[1].y))
            elif it[0] == "c":
                pts.append((it[1].x, it[1].y))
        if items[-1][0] == "l":
            pts.append((items[-1][2].x, items[-1][2].y))
        if len(pts) < 6:
            continue
        fit = circle_fit(pts)
        if fit is None:
            continue
        cx, cy, r, rms = fit
        if not (x0 <= cx <= x1 and y0 <= cy <= y1):
            continue
        if rms > r * 0.04 or r < 6 or r > 120:
            continue
        # angular span distinguishes full circles (~360°, e.g. cooler
        # symbols) from arcs (~180°, vessel dished heads): sort vertex
        # angles and subtract the largest angular gap from the full turn
        angs = sorted(math.atan2(p[1] - cy, p[0] - cx) for p in pts)
        gaps = [angs[i + 1] - angs[i] for i in range(len(angs) - 1)]
        gaps.append(2 * math.pi - (angs[-1] - angs[0]))
        span = 2 * math.pi - max(gaps)
        # opening direction = middle of the largest gap
        gi = gaps.index(max(gaps))
        if gi < len(angs) - 1:
            open_ang = (angs[gi] + angs[gi + 1]) / 2
        else:
            open_ang = (angs[-1] + angs[0] + 2 * math.pi) / 2
        arcs.append({
            "cx": cx, "cy": cy, "r": r,
            "span_deg": math.degrees(span),
            "open_deg": math.degrees(open_ang) % 360,
        })
    return arcs


def pair_capsules(arcs):
    """Two same-radius arcs (~half circles) opening toward each other along
    an axis = capsule body (vessel if vertical, machine stage if horizontal)."""
    heads = [a for a in arcs if 100 <= a["span_deg"] <= 280 and a["r"] >= 12]
    used = set()
    capsules = []
    for i in range(len(heads)):
        if i in used:
            continue
        for j in range(i + 1, len(heads)):
            if j in used:
                continue
            a, b = heads[i], heads[j]
            if abs(a["r"] - b["r"]) > max(2.0, a["r"] * 0.15):
                continue
            dx, dy = b["cx"] - a["cx"], b["cy"] - a["cy"]
            dist = math.hypot(dx, dy)
            if not (a["r"] * 0.5 <= dist <= a["r"] * 12):
                continue
            # each head must OPEN toward the other head: a vessel's top head
            # bulges up (opens down toward the shell), the bottom head the
            # reverse. Without this check any two same-size arcs on the
            # sheet would pair up.
            axis = math.degrees(math.atan2(dy, dx)) % 360
            da = (a["open_deg"] - axis) % 360
            db = (b["open_deg"] - (axis + 180) % 360) % 360
            if min(da, 360 - da) > 55 or min(db, 360 - db) > 55:
                continue
            r = (a["r"] + b["r"]) / 2
            horiz = abs(dx) >= abs(dy)
            # heads bulge outward by r beyond the arc centers
            if horiz:
                bbox = (min(a["cx"], b["cx"]) - r, a["cy"] - r,
                        max(a["cx"], b["cx"]) + r, a["cy"] + r)
            else:
                bbox = (a["cx"] - r, min(a["cy"], b["cy"]) - r,
                        a["cx"] + r, max(a["cy"], b["cy"]) + r)
            capsules.append({
                "bbox": [round(v, 1) for v in bbox],
                "r": round(r, 1),
                "orientation": "H" if horiz else "V",
            })
            used.add(i); used.add(j)
            break
    return capsules


def find_equipment_circles(page, instruments):
    """Bubble-sized circles with no instrument tag = coolers/filters/funnels."""
    inst_centers = [tuple(r["center"]) for r in instruments]
    out = []
    for a in find_arcs(page):
        if a["span_deg"] < 330 or not (7 <= a["r"] <= 20):
            continue
        if any(math.hypot(a["cx"] - ix, a["cy"] - iy) < 5 for ix, iy in inst_centers):
            continue
        out.append({"center": [round(a["cx"], 1), round(a["cy"], 1)],
                    "r": round(a["r"], 1)})
    return out


def find_tags(page):
    """Equipment tag spans + nearby name/spec attribute text."""
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                t = span["text"].strip()
                if t:
                    spans.append({"text": t, "bbox": span["bbox"],
                                  "size": span["size"]})
    tags = []
    for s in spans:
        if not TAG_RE.match(s["text"]):
            continue
        x0, y0, x1, y1 = s["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # instrument loop numbers also exist; equipment tags are larger text
        # and not inside a bubble - filter by size (specs use 17.5pt on these
        # sheets vs 8.75 for in-bubble text)
        if s["size"] < 12:
            continue
        name, specs = None, []
        for o in spans:
            ox = (o["bbox"][0] + o["bbox"][2]) / 2
            oy = (o["bbox"][1] + o["bbox"][3]) / 2
            if abs(ox - cx) > 160 or abs(oy - cy) > 140 or o is s:
                continue
            if SPEC_RE.search(o["text"]):
                specs.append(o["text"])
            elif (re.search(r"[一-鿿]", o["text"])
                  and len(o["text"]) >= 3 and abs(oy - cy) < 60
                  and not re.search(r"[:：]", o["text"])):
                name = o["text"]
        tags.append({"tag": s["text"], "xy": [round(cx, 1), round(cy, 1)],
                     "name": name, "specs": specs})
    return tags


def index_page(doc, page_num, instruments):
    page = doc[page_num - 1]
    tags = find_tags(page)
    capsules = pair_capsules(find_arcs(page))
    circles = find_equipment_circles(page, instruments)

    # associate each capsule with the nearest tag (if within reach)
    for c in capsules:
        bx = (c["bbox"][0] + c["bbox"][2]) / 2
        by = (c["bbox"][1] + c["bbox"][3]) / 2
        best, bestd = None, 1e9
        for t in tags:
            dd = math.hypot(t["xy"][0] - bx, t["xy"][1] - by)
            # bare tag mentions (notes, references) rank below the real
            # labels, which carry a name or spec block
            if not (t["name"] or t["specs"]):
                dd *= 2.5
            if dd < bestd:
                best, bestd = t, dd
        c["tag"] = best["tag"] if best and bestd < 900 else None
        c["page"] = page_num
    for t in tags:
        t["page"] = page_num
    for c in circles:
        c["page"] = page_num
    return tags, capsules, circles


if __name__ == "__main__":
    doc = fitz.open(PDF_PATH)
    instruments_all = json.load(open("output/instrument_index.json"))
    pages = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PROCESS_PAGES)
    result = {"tags": [], "capsules": [], "equipment_circles": []}
    for p in pages:
        inst = [r for r in instruments_all
                if r["page"] == p and r["class"] == "instrument"]
        tags, capsules, circles = index_page(doc, p, inst)
        result["tags"] += tags
        result["capsules"] += capsules
        result["equipment_circles"] += circles
        print(f"page {p}: {len(tags)} tags, {len(capsules)} capsules, "
              f"{len(circles)} equipment circles")
        for t in tags:
            print(f"   TAG {t['tag']:<18} name={t['name'] or '-':<12} "
                  f"specs={len(t['specs'])}")
        for c in capsules:
            print(f"   CAP {c['orientation']} bbox={c['bbox']} r={c['r']} "
                  f"tag={c['tag']}")
    with open("output/equipment_index.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"\ntotal: {len(result['tags'])} tags, {len(result['capsules'])} "
          f"capsules, {len(result['equipment_circles'])} circles "
          f"-> output/equipment_index.json")
