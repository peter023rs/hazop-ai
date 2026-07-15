"""
Flow-arrow detector: find solid flow-direction arrows on pipes and read the
direction they point.

The 2401 sheets draw on-pipe arrows in two encodings (both confirmed by
geometry dump + render):

  Style T ("triangle") - one closed 3-line path (fill and/or zero-width
      stroke) forming a slender triangle. Pages 6/7/10/11/12.
      Direction: apex = vertex opposite the shortest side (the base).

  Style R ("rect+taper") - a filled slender axis-aligned rectangle plus two
      0.71 pt taper strokes converging on the midpoint of one short edge
      (the apex), their far ends on the opposite corners. Pages 4/5/8/9.
      Direction: rect center -> apex.

Not flow arrows (excluded): the big hatched off-page connector arrows
(handled by the assembler via 至/自 text), instrument leader dots, and
stroked-CJK text glyphs (SHX fonts export as short strokes - excluded by
the closed-triangle / rect+taper structural requirements).

Usage:
    python3 scripts/detect_arrows.py [page]      # default: all sheets 4-12
Output: output/arrow_index.json + stdout table
"""
import fitz
import json
import math
import sys

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)
CONTENT = (200, 200, 2200, 1450)

TRI_MAX_BBOX = 26        # flow arrowheads are small
TRI_MIN_HEIGHT = 4.0     # apex-to-base distance
TRI_MIN_BASE = 2.5
TRI_MIN_ASPECT = 1.1     # height / base; flow arrows are slender
RECT_SHORT = (4.0, 10.0)   # style R rectangle short side
RECT_LONG = (10.0, 26.0)   # style R rectangle long side
RECT_MIN_ASPECT = 1.7
TAPER_SNAP = 2.5         # taper endpoint -> rect landmark tolerance


def _close(p, q, tol):
    return math.hypot(p[0] - q[0], p[1] - q[1]) <= tol


def _axis(dir_vec):
    ux, uy = dir_vec
    ang = math.degrees(math.atan2(uy, ux)) % 180
    if ang < 15 or ang > 165:
        return "H"
    if 75 < ang < 105:
        return "V"
    return "D"


def _in_content(cx, cy):
    x0, y0, x1, y1 = CONTENT
    return x0 <= cx <= x1 and y0 <= cy <= y1


def _triangle_arrow(pts):
    """pts = 3 vertices. Return (center, dir, size) if slender arrow-shaped."""
    a, b, c = pts
    sides = [  # (length, base_p, base_q, apex)
        (math.hypot(b[0] - c[0], b[1] - c[1]), b, c, a),
        (math.hypot(c[0] - a[0], c[1] - a[1]), c, a, b),
        (math.hypot(a[0] - b[0], a[1] - b[1]), a, b, c),
    ]
    sides.sort(key=lambda s: s[0])
    base_len, bp, bq, apex = sides[0]
    if base_len < TRI_MIN_BASE:
        return None
    bmid = ((bp[0] + bq[0]) / 2, (bp[1] + bq[1]) / 2)
    height = math.hypot(apex[0] - bmid[0], apex[1] - bmid[1])
    if height < TRI_MIN_HEIGHT or height / base_len < TRI_MIN_ASPECT:
        return None
    ux, uy = (apex[0] - bmid[0]) / height, (apex[1] - bmid[1]) / height
    cx = (a[0] + b[0] + c[0]) / 3
    cy = (a[1] + b[1] + c[1]) / 3
    return (cx, cy), (ux, uy), height


def _closed_triangle_pts(items):
    """3 line items closing on themselves -> the 3 vertices, else None."""
    if len(items) != 3 or any(it[0] != "l" for it in items):
        return None
    pts = [(items[0][1].x, items[0][1].y)]
    cur = items[0][1]
    for it in items:
        if not _close((it[1].x, it[1].y), (cur.x, cur.y), 0.6):
            return None
        cur = it[2]
        pts.append((cur.x, cur.y))
    if not _close(pts[0], pts[-1], 0.6):
        return None
    return pts[:3]


def _rect_landmarks(items):
    """4 line items forming an axis-aligned slender rectangle ->
    (center, [short-edge midpoints], [corner pairs per short edge])."""
    if len(items) != 4 or any(it[0] != "l" for it in items):
        return None
    xs, ys = set(), set()
    corners = []
    for it in items:
        for p in (it[1], it[2]):
            xs.add(round(p.x, 1)); ys.add(round(p.y, 1))
            corners.append((p.x, p.y))
    if len(xs) != 2 or len(ys) != 2:
        return None
    (x0, x1), (y0, y1) = sorted(xs), sorted(ys)
    w, h = x1 - x0, y1 - y0
    short, long_ = min(w, h), max(w, h)
    if not (RECT_SHORT[0] <= short <= RECT_SHORT[1]
            and RECT_LONG[0] <= long_ <= RECT_LONG[1]
            and long_ / short >= RECT_MIN_ASPECT):
        return None
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if w < h:   # vertical arrow: short edges at top/bottom
        mids = [((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1)]
        edge_corners = [[(x0, y0), (x1, y0)], [(x0, y1), (x1, y1)]]
    else:       # horizontal arrow: short edges at left/right
        mids = [(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)]
        edge_corners = [[(x0, y0), (x0, y1)], [(x1, y0), (x1, y1)]]
    return (cx, cy), mids, edge_corners


def find_arrows(page):
    arrows = []

    # pool of stroke segments for style R tapers
    strokes = []
    for d in page.get_drawings():
        if d["type"] == "f":
            continue
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = (it[1].x, it[1].y), (it[2].x, it[2].y)
            L = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if RECT_LONG[0] * 0.7 <= L <= RECT_LONG[1] * 1.3:
                strokes.append((p1, p2))

    for d in page.get_drawings():
        items = d["items"]

        # ---- style T: closed slender triangle (fill or zero-width stroke)
        pts = _closed_triangle_pts(items)
        if pts is not None:
            r = d["rect"]
            if max(r.width, r.height) <= TRI_MAX_BBOX:
                tri = _triangle_arrow(pts)
                if tri:
                    (cx, cy), (ux, uy), size = tri
                    if _in_content(cx, cy):
                        arrows.append({
                            "center": (round(cx, 1), round(cy, 1)),
                            "dir": (round(ux, 3), round(uy, 3)),
                            "size": round(size, 1),
                            "style": "T",
                            "bbox": [round(r.x0, 1), round(r.y0, 1),
                                     round(r.x1, 1), round(r.y1, 1)],
                        })
            continue

        # ---- style R: filled slender rect + 2 converging taper strokes
        if d["type"] != "f":
            continue
        lm = _rect_landmarks(items)
        if lm is None:
            continue
        (cx, cy), mids, edge_corners = lm
        if not _in_content(cx, cy):
            continue
        for apex_i in (0, 1):
            apex = mids[apex_i]
            far_corners = edge_corners[1 - apex_i]
            n_taper = 0
            for p1, p2 in strokes:
                for a, b in ((p1, p2), (p2, p1)):
                    if _close(a, apex, TAPER_SNAP) and any(
                            _close(b, fc, TAPER_SNAP) for fc in far_corners):
                        n_taper += 1
                        break
            if n_taper >= 2:
                h = math.hypot(apex[0] - cx, apex[1] - cy) * 2
                ux, uy = (apex[0] - cx) / (h / 2), (apex[1] - cy) / (h / 2)
                r = d["rect"]
                arrows.append({
                    "center": (round(cx, 1), round(cy, 1)),
                    "dir": (round(ux, 3), round(uy, 3)),
                    "size": round(h, 1),
                    "style": "R",
                    "bbox": [round(r.x0, 1), round(r.y0, 1),
                             round(r.x1, 1), round(r.y1, 1)],
                })
                break

    # dedupe fill+stroke duplicates (same triangle drawn twice)
    arrows.sort(key=lambda a: -a["size"])
    kept = []
    for a in arrows:
        if all(math.hypot(a["center"][0] - k["center"][0],
                          a["center"][1] - k["center"][1]) >= 3 for k in kept):
            kept.append(a)
    for a in kept:
        a["axis"] = _axis(a["dir"])
    return kept


if __name__ == "__main__":
    doc = fitz.open(PDF_PATH)
    pages = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PROCESS_PAGES)
    all_arrows = []
    for p in pages:
        arrows = find_arrows(doc[p - 1])
        for a in arrows:
            a["page"] = p
        arrows.sort(key=lambda a: (a["center"][1], a["center"][0]))
        all_arrows.extend(arrows)
        byax = {ax: sum(1 for a in arrows if a["axis"] == ax) for ax in "HVD"}
        print(f"page {p}: {len(arrows)} arrows "
              f"(H={byax['H']} V={byax['V']} D={byax['D']}, "
              f"T={sum(1 for a in arrows if a['style']=='T')} "
              f"R={sum(1 for a in arrows if a['style']=='R')})")
    with open("output/arrow_index.json", "w") as f:
        json.dump(all_arrows, f, indent=2)
    print(f"\ntotal: {len(all_arrows)} arrows -> output/arrow_index.json")
