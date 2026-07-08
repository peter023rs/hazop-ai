"""
Valve detector: find bowtie symbols (two triangles tip-to-tip) in the vector
geometry of each process sheet.

The sheets use three drawing styles for the bowtie (all confirmed on page 7):
  A. four short triangle sides meeting at the pinch point
  B. two full-length diagonals crossing at their midpoints, plus two end bars
  C. ball valve: triangle sides stop at a small circle, so the sides only
     meet the pinch when extended ~30-40% beyond their endpoints

Detection:
  1. explode all stroked paths into line segments
  2. keep short diagonals (5-25 pt, slope well away from H/V)
  3. compute pairwise extended-line intersections of opposite-slope pairs
     (allowing t,u in [-0.4, 1.4] to cover style C)
  4. cluster intersection points within 4 pt -> candidate pinch
  5. accept a pinch if it has >=2 crossing pairs (styles A/C), or a single
     midpoint-crossing pair flanked by perpendicular end bars (style B)
  6. reject hatch fills (dense solid arrows etc.): >8 diagonals in cluster
  7. flag ball valves via a tiny circle (r < 5) at the pinch

Classification (per legend sheet 1 + confirmed process-sheet geometry):
  - inline vs angle: outward directions of the two triangles from the pinch
    are ~180 deg apart for an inline bowtie, ~90 deg for an angle valve
    (safety valves are drawn as angle valves). Style B (midpoint crossing)
    is always inline.
  - safety valve: angle form + spring signature (>=2 short parallel ticks
    stacked on the stem near the pinch); the assembler additionally
    upgrades angle valves with a PSV instrument bubble nearby.
  - check valve: inline bowtie + small filled axis-aligned triangle at the
    pinch (legend 止回阀); the triangle apex also gives flow direction.
  - ball valve: tiny circle or filled dot at the pinch.

Usage:
    python3 scripts/detect_valves.py [page]      # default: all sheets 4-12
Output: output/valve_index.json + stdout table
"""
import fitz
import json
import math
import sys

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)
CONTENT = (200, 200, 2200, 1450)  # exclude frame ruler & title block


def explode_segments(page):
    segs = []
    for d in page.get_drawings():
        if d["type"] == "f":
            continue
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = it[1], it[2]
            L = math.hypot(p2.x - p1.x, p2.y - p1.y)
            if L < 1:
                continue
            ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x)) % 180
            segs.append({
                "p1": (p1.x, p1.y), "p2": (p2.x, p2.y),
                "L": L, "ang": ang,
                "mid": ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2),
                "n_items_src": len(d["items"]),
            })
    return segs


def line_intersection(s1, s2, tmin=-0.4, tmax=1.4):
    """Extended intersection; returns (point, t, u) or None."""
    x1, y1 = s1["p1"]; x2, y2 = s1["p2"]
    x3, y3 = s2["p1"]; x4, y4 = s2["p2"]
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / den
    if tmin <= t <= tmax and tmin <= u <= tmax:
        return ((x1 + t * (x2 - x1), y1 + t * (y2 - y1)), t, u)
    return None


def find_circles(page, rmin=0.0, rmax=5.0):
    """Circle-shaped grouped paths with radius in [rmin, rmax]."""
    out = []
    for d in page.get_drawings():
        items = d["items"]
        if len(items) < 8:
            continue
        pts = []
        for it in items:
            if it[0] == "l":
                pts.append(it[1]); pts.append(it[2])
            elif it[0] == "c":
                pts.append(it[1]); pts.append(it[4])
        if len(pts) < 8:
            continue
        cx = sum(p.x for p in pts) / len(pts)
        cy = sum(p.y for p in pts) / len(pts)
        radii = [math.hypot(p.x - cx, p.y - cy) for p in pts]
        rm = sum(radii) / len(radii)
        if rm == 0 or not (rmin <= rm <= rmax):
            continue
        rs = math.sqrt(sum((r - rm) ** 2 for r in radii) / len(radii))
        if rs / rm < 0.08:
            out.append((cx, cy, rm))
    return out


def find_tiny_circles(page, rmax=5.0):
    """Small circles (ball-valve balls, junction dots)."""
    return find_circles(page, 0.0, rmax)


def find_dot_fills(page, rmax=5.0):
    """Small filled squares/dots (ball-valve balls drawn as solid dots)."""
    out = []
    for d in page.get_drawings():
        if d["type"] != "f" or not (3 <= len(d["items"]) <= 6):
            continue
        r = d["rect"]
        if max(r.width, r.height) <= rmax:
            out.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return out


def find_cv_triangles(page):
    """Small filled axis-aligned triangles (check-valve flow markers).
    Returns (center, dir unit vector) per triangle."""
    out = []
    for d in page.get_drawings():
        if d["type"] != "f" or len(d["items"]) != 3:
            continue
        if any(it[0] != "l" for it in d["items"]):
            continue
        r = d["rect"]
        if not (5 <= max(r.width, r.height) <= 14):
            continue
        pts = [(it[1].x, it[1].y) for it in d["items"]]
        a, b, c = pts
        sides = [
            (math.hypot(b[0] - c[0], b[1] - c[1]), b, c, a),
            (math.hypot(c[0] - a[0], c[1] - a[1]), c, a, b),
            (math.hypot(a[0] - b[0], a[1] - b[1]), a, b, c),
        ]
        sides.sort(key=lambda s: s[0])
        _, bp, bq, apex = sides[0]
        bmid = ((bp[0] + bq[0]) / 2, (bp[1] + bq[1]) / 2)
        h = math.hypot(apex[0] - bmid[0], apex[1] - bmid[1])
        if h < 3:
            continue
        ux, uy = (apex[0] - bmid[0]) / h, (apex[1] - bmid[1]) / h
        ang = math.degrees(math.atan2(uy, ux)) % 180
        if not (ang < 10 or ang > 170 or 80 < ang < 100):
            continue  # check markers are axis-aligned; heater arrows are not
        cx = sum(p[0] for p in pts) / 3
        cy = sum(p[1] for p in pts) / 3
        out.append(((cx, cy), (ux, uy)))
    return out


def find_check_valves(page, segs):
    """Check valves (legend 止回阀) are NOT bowties: two end bars, one flap
    diagonal across the pipe, and a small filled triangle whose apex touches
    the diagonal's downstream end. Detected as their own pattern:
      filled triangle + diagonal stroke ending at its apex + end bars.
    Flow direction = apex end of the flap, projected on the pipe axis."""
    x0, y0, x1, y1 = CONTENT
    # candidate flap diagonals
    flaps = [s for s in segs
             if 14 <= s["L"] <= 26
             and (25 <= s["ang"] <= 65 or 115 <= s["ang"] <= 155)
             and s["n_items_src"] <= 2]
    bars = [s for s in segs
            if 6 <= s["L"] <= 16
            and (s["ang"] < 10 or s["ang"] > 170 or 80 < s["ang"] < 100)]
    out = []
    for d in page.get_drawings():
        if d["type"] != "f" or len(d["items"]) != 3:
            continue
        if any(it[0] != "l" for it in d["items"]):
            continue
        r = d["rect"]
        if not (5 <= max(r.width, r.height) <= 14):
            continue
        pts = [(it[1].x, it[1].y) for it in d["items"]]
        a, b, c = pts
        sides = [
            (math.hypot(b[0] - c[0], b[1] - c[1]), a),
            (math.hypot(c[0] - a[0], c[1] - a[1]), b),
            (math.hypot(a[0] - b[0], a[1] - b[1]), c),
        ]
        sides.sort(key=lambda s: s[0])
        apex = sides[0][1]
        for f in flaps:
            # flap endpoint at the triangle apex
            ends = (f["p1"], f["p2"])
            near = min(ends, key=lambda e: math.hypot(e[0] - apex[0],
                                                      e[1] - apex[1]))
            if math.hypot(near[0] - apex[0], near[1] - apex[1]) > 3:
                continue
            other = ends[1] if near is ends[0] else ends[0]
            # end bars near both flap endpoints (perpendicular to the pipe)
            nbar = sum(
                1 for bar in bars
                if min(math.hypot(bar["mid"][0] - e[0], bar["mid"][1] - e[1])
                       for e in ends) < 7
            )
            if nbar < 2:
                continue
            cx, cy = f["mid"]
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                continue
            dx, dy = near[0] - other[0], near[1] - other[1]
            horiz = abs(dx) >= abs(dy)
            out.append({
                "center": (round(cx, 1), round(cy, 1)),
                "bbox": [round(min(f["p1"][0], f["p2"][0]) - 2, 1),
                         round(min(f["p1"][1], f["p2"][1]) - 2, 1),
                         round(max(f["p1"][0], f["p2"][0]) + 2, 1),
                         round(max(f["p1"][1], f["p2"][1]) + 2, 1)],
                "n_segs": 1,
                "orientation": "H" if horiz else "V",
                "ball": False,
                "subclass": "check",
                "flow_dir": ((1.0 if dx > 0 else -1.0, 0.0) if horiz
                             else (0.0, 1.0 if dy > 0 else -1.0)),
            })
            break
    # dedupe (fill+stroke duplicates)
    kept = []
    for v in out:
        if all(math.hypot(v["center"][0] - k["center"][0],
                          v["center"][1] - k["center"][1]) >= 6 for k in kept):
            kept.append(v)
    return kept


def _classify_form(pinch, members):
    """'inline' (triangles back-to-back, ~180 deg) or 'angle' (~90 deg).

    Uses the outward direction from the pinch to each member's far endpoint.
    Members whose pinch sits mid-segment (style B full diagonals) make the
    outward direction meaningless -> classified inline.
    """
    vecs = []
    for s in members:
        d1 = math.hypot(s["p1"][0] - pinch[0], s["p1"][1] - pinch[1])
        d2 = math.hypot(s["p2"][0] - pinch[0], s["p2"][1] - pinch[1])
        if min(d1, d2) > 0.35 * s["L"]:
            return "inline"  # midpoint crossing = style B bowtie
        far = s["p1"] if d1 > d2 else s["p2"]
        L = max(d1, d2)
        if L < 1:
            continue
        vecs.append(((far[0] - pinch[0]) / L, (far[1] - pinch[1]) / L))
    if len(vecs) < 3:
        return "inline"
    # two clusters seeded by the most-separated pair
    best, seeds = -2.0, (0, 1)
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            sep = -(vecs[i][0] * vecs[j][0] + vecs[i][1] * vecs[j][1])
            if sep > best:
                best, seeds = sep, (i, j)
    ca, cb = [], []
    for v in vecs:
        da = v[0] * vecs[seeds[0]][0] + v[1] * vecs[seeds[0]][1]
        db = v[0] * vecs[seeds[1]][0] + v[1] * vecs[seeds[1]][1]
        (ca if da >= db else cb).append(v)
    if not ca or not cb:
        return "inline"
    ma = (sum(v[0] for v in ca), sum(v[1] for v in ca))
    mb = (sum(v[0] for v in cb), sum(v[1] for v in cb))
    na = math.hypot(*ma); nb = math.hypot(*mb)
    if na == 0 or nb == 0:
        return "inline"
    cosang = (ma[0] * mb[0] + ma[1] * mb[1]) / (na * nb)
    ang = math.degrees(math.acos(max(-1, min(1, cosang))))
    return "angle" if 55 <= ang <= 125 else "inline"


def _has_spring(pinch, shorts):
    """Spring signature near an angle-valve pinch: >=2 short parallel ticks
    stacked within ~4.5 pt of each other, 6-24 pt away from the pinch."""
    ticks = []
    for s in shorts:
        if not (3 <= s["L"] <= 9):
            continue
        d = math.hypot(s["mid"][0] - pinch[0], s["mid"][1] - pinch[1])
        if 6 <= d <= 24:
            ticks.append(s)
    for i in range(len(ticks)):
        for j in range(i + 1, len(ticks)):
            a, b = ticks[i], ticks[j]
            dang = abs(a["ang"] - b["ang"])
            if min(dang, 180 - dang) > 12:
                continue
            if math.hypot(a["mid"][0] - b["mid"][0],
                          a["mid"][1] - b["mid"][1]) <= 4.5:
                return True
    return False


def find_valves(page):
    # Candidate pool: valve triangle sides are SHORT DIAGONALS. Pipes are
    # long and axis-aligned, so this filter alone removes ~95% of geometry.
    # n_items_src <= 6 excludes segments that belong to circles/arcs
    # (grouped polylines), which would otherwise fake diagonal crossings.
    segs = explode_segments(page)
    x0, y0, x1, y1 = CONTENT
    diags = [
        s for s in segs
        if 5 <= s["L"] <= 25
        and (15 <= s["ang"] <= 75 or 105 <= s["ang"] <= 165)
        and s["n_items_src"] <= 6
        and x0 <= s["mid"][0] <= x1 and y0 <= s["mid"][1] <= y1
    ]
    shorts = [  # potential end bars: short axis-aligned segments
        s for s in segs
        if 4 <= s["L"] <= 20
        and (s["ang"] < 15 or s["ang"] > 165 or 75 < s["ang"] < 105)
        and x0 <= s["mid"][0] <= x1 and y0 <= s["mid"][1] <= y1
    ]

    # Pairwise extended intersections of opposite-slope diagonal pairs.
    # "Extended" (t,u up to 1.4) is what catches ball valves (style C):
    # their triangle sides stop at the ball circle and only meet the pinch
    # point when prolonged beyond their endpoints.
    crossings = []
    for i in range(len(diags)):
        for j in range(i + 1, len(diags)):
            a, b = diags[i], diags[j]
            dang = abs(a["ang"] - b["ang"])
            if dang < 30 or dang > 150:
                continue
            hit = line_intersection(a, b)
            if hit is None:
                continue
            pt, t, u = hit
            # pinch must be close to both segments (within one length)
            if (math.hypot(pt[0] - a["mid"][0], pt[1] - a["mid"][1]) < a["L"]
                    and math.hypot(pt[0] - b["mid"][0], pt[1] - b["mid"][1]) < b["L"]):
                crossings.append({"pt": pt, "i": i, "j": j, "t": t, "u": u})

    # cluster crossings within 4pt
    used = [False] * len(crossings)
    candidates = []
    for i, c in enumerate(crossings):
        if used[i]:
            continue
        group = [c]
        used[i] = True
        for j in range(i + 1, len(crossings)):
            if used[j]:
                continue
            q = crossings[j]
            if math.hypot(c["pt"][0] - q["pt"][0], c["pt"][1] - q["pt"][1]) < 4:
                group.append(q)
                used[j] = True
        seg_ids = set()
        for g in group:
            seg_ids.add(g["i"]); seg_ids.add(g["j"])
        candidates.append({"group": group, "seg_ids": seg_ids})

    # Acceptance rules, one per drawing style found on these sheets:
    #   style A: 4 short triangle sides -> >=2 crossings at one pinch point
    #   style C: ball valve, same as A but via extended intersections
    #   style B: 2 full diagonals + 2 end bars -> only ONE crossing, which
    #            alone looks like two pipes crossing; require corroborating
    #            perpendicular end bars at the diagonal endpoints
    # Hatch guard: solid arrows are drawn as dense diagonal hatching that
    # produces dozens of crossings - any cluster with >8 segments is a fill.
    valves = []
    for cand in candidates:
        group, seg_ids = cand["group"], cand["seg_ids"]
        members = [diags[k] for k in seg_ids]
        if len(members) > 8:
            continue  # hatch fill (solid arrows), not a valve
        px = sum(g["pt"][0] for g in group) / len(group)
        py = sum(g["pt"][1] for g in group) / len(group)

        ok = False
        if len(group) >= 2 and len(seg_ids) >= 3:
            ok = True  # styles A and C
        elif len(group) == 1 and len(seg_ids) == 2:
            # style B: single X crossing near both midpoints + end bars
            g = group[0]
            if 0.3 <= g["t"] <= 0.7 and 0.3 <= g["u"] <= 0.7:
                ends = [members[0]["p1"], members[0]["p2"],
                        members[1]["p1"], members[1]["p2"]]
                nbar = 0
                for bar in shorts:
                    hits = sum(
                        1 for e in ends
                        for bp in (bar["p1"], bar["p2"])
                        if math.hypot(e[0] - bp[0], e[1] - bp[1]) < 2
                    )
                    if hits >= 2:
                        nbar += 1
                if nbar >= 1:
                    ok = True
        if not ok:
            continue

        xs = [c for s in members for c in (s["p1"][0], s["p2"][0])]
        ys = [c for s in members for c in (s["p1"][1], s["p2"][1])]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        valves.append({
            "center": (round(px, 1), round(py, 1)),
            "bbox": [round(min(xs), 1), round(min(ys), 1),
                     round(max(xs), 1), round(max(ys), 1)],
            "n_segs": len(members),
            "orientation": "H" if bw >= bh else "V",
            "_members": members,
        })

    # dedupe: one valve per pinch within 6pt (keep the one with more segments)
    valves.sort(key=lambda v: -v["n_segs"])
    kept = []
    for v in valves:
        if all(math.hypot(v["center"][0] - k["center"][0],
                          v["center"][1] - k["center"][1]) >= 6 for k in kept):
            kept.append(v)

    # symbol-internal arrows (air-intake circles, motor circles etc.) fake a
    # diagonal crossing; a pinch inside a medium circle is symbol artwork,
    # not a valve. Radius range stays below instrument bubbles (>=15.5).
    symbol_circles = find_circles(page, 5.5, 12.0)
    kept = [v for v in kept if not any(
        math.hypot(v["center"][0] - cx, v["center"][1] - cy) < r + 2
        for cx, cy, r in symbol_circles)]

    balls = find_tiny_circles(page)
    dots = find_dot_fills(page)
    cv_tris = find_cv_triangles(page)
    for v in kept:
        v["ball"] = any(
            math.hypot(v["center"][0] - cx, v["center"][1] - cy) < 4
            for cx, cy, r in balls
        ) or any(
            math.hypot(v["center"][0] - cx, v["center"][1] - cy) < 4
            for cx, cy in dots
        )
        form = _classify_form(v["center"], v["_members"])
        spring = form == "angle" and _has_spring(v["center"], shorts)
        # check valve: filled axis-aligned triangle at the pinch, pointing
        # along the valve axis (its apex = flow direction)
        check_dir = None
        for (cx, cy), (ux, uy) in cv_tris:
            if math.hypot(v["center"][0] - cx, v["center"][1] - cy) > 8:
                continue
            tri_axis = "H" if abs(ux) >= abs(uy) else "V"
            if tri_axis == v["orientation"]:
                check_dir = (round(ux, 3), round(uy, 3))
                break
        if check_dir is not None:
            v["subclass"] = "check"
            v["flow_dir"] = check_dir
        elif spring:
            v["subclass"] = "safety"
        elif form == "angle":
            v["subclass"] = "angle"
        elif v["ball"]:
            v["subclass"] = "ball"
        else:
            v["subclass"] = "gate"
        del v["_members"]

    # flap-style check valves are a separate pattern, not bowties
    for cv in find_check_valves(page, segs):
        if all(math.hypot(cv["center"][0] - k["center"][0],
                          cv["center"][1] - k["center"][1]) >= 6
               for k in kept):
            kept.append(cv)
    return kept


if __name__ == "__main__":
    doc = fitz.open(PDF_PATH)
    pages = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PROCESS_PAGES)
    all_valves = []
    for p in pages:
        valves = find_valves(doc[p - 1])
        for v in valves:
            v["page"] = p
        valves.sort(key=lambda v: (v["center"][1], v["center"][0]))
        all_valves.extend(valves)
        sub = {s: sum(1 for v in valves if v["subclass"] == s)
               for s in ("gate", "ball", "angle", "safety", "check")}
        print(f"page {p}: {len(valves)} valves "
              f"({sum(1 for v in valves if v['orientation']=='H')} H, "
              f"{sum(1 for v in valves if v['orientation']=='V')} V) "
              f"subclass: {', '.join(f'{k}={n}' for k, n in sub.items() if n)}")
        for v in valves:
            extra = f" flow_dir={v['flow_dir']}" if v.get("flow_dir") else ""
            print(f"   ({v['center'][0]:7.1f},{v['center'][1]:7.1f}) "
                  f"{v['orientation']} segs={v['n_segs']} "
                  f"{v['subclass']}{extra}")
    with open("output/valve_index.json", "w") as f:
        json.dump(all_valves, f, indent=2)
    print(f"\ntotal: {len(all_valves)} valves -> output/valve_index.json")
