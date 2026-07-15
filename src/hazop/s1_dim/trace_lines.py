"""
Line tracer: build a connectivity graph (nodes + edges) from a process sheet.

Stroke width encodes line role in these ZWCAD exports (content area only):
  0.43 pt            long secondary pipes
  1.13 / 1.70 pt     main process pipes
  0.71 pt            symbol strokes AND auxiliary pipes/instrument leaders -
                     separated structurally: symbols are multi-item paths
                     (circles, heads) or live inside valve/bubble regions
  0.14 pt            dashes of instrument signal lines + arrow hatch fills

Pipeline:
  1. collect solid-line segments (pipe widths), excluding segments that belong
     to detected valve bodies and flow-arrow bodies (arrow_index bboxes)
  2. chain 0.14pt dashes into dashed polylines (signal lines); hatch fills are
     rejected because their dashes have no collinear continuation
  3. snap endpoints, split segments at T-junctions, merge collinear chains
  4. bridge pipe stubs across valve nodes; attach ends to instrument bubbles
  5. classify nodes: valve / instrument / junction / corner / dead-end
Output: output/graph_page<N>.json + colored overlay PNG

Usage:  python3 scripts/trace_lines.py [page]     (default 7)
"""
import fitz
import json
import math
import sys
from collections import defaultdict

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
CONTENT = (200, 200, 2200, 1450)
PIPE_WIDTHS = (0.43, 1.13, 1.70)
AUX_WIDTH = 0.71    # shared by symbol strokes and auxiliary pipes/leaders
DASH_WIDTH = 0.14
SNAP = 1.2          # endpoint coincidence tolerance (pt)
DASH_GAP = 7.0      # max gap between dashes of one signal line
DASH_MAX_LEN = 8.0  # signal dashes are short; hatch strokes run longer
SOLID_GAP = 8.0     # bridge collinear solid runs across spec-break symbols
VALVE_REACH = 14.0  # pipe stub -> valve center bridge distance


def wclass(w):
    """Map a stroke width to its semantic role (see module docstring).

    This is the tracer's core prior: the ZWCAD export encodes what a line
    IS in how thick it is. Widths that match nothing (e.g. the 0.0 hairline
    decorations) return None and are ignored entirely.
    """
    if w is None:
        return None
    for pw in PIPE_WIDTHS:
        if abs(w - pw) < 0.05:
            return "pipe"
    if abs(w - AUX_WIDTH) < 0.05:
        return "aux"
    if abs(w - DASH_WIDTH) < 0.03:
        return "dash"
    return None


TITLE_BLOCK = (1795, 1295, 2350, 1470)  # revision table intrudes into CONTENT


def collect_segments(page, valves, instruments, arrows=()):
    solid, dashes = [], []
    x0, y0, x1, y1 = CONTENT
    tb = fitz.Rect(*TITLE_BLOCK)
    vboxes = [fitz.Rect(*v["bbox"]) + (-2, -2, 2, 2) for v in valves]
    # flow-arrow bodies (taper strokes, base bars) are decoration on the
    # pipe, not pipe: mask them like valve bodies
    aboxes = [fitz.Rect(*a["bbox"]) + (-1.5, -1.5, 1.5, 1.5)
              for a in arrows if a.get("bbox")]
    vboxes += aboxes
    bubbles = [(r["center"][0], r["center"][1], r["radius"]) for r in instruments]
    # text underlines masquerade as pipes; mask them with text bboxes
    tboxes = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                tboxes.append(fitz.Rect(span["bbox"]) + (-1, -1, 1, 3))
    for d in page.get_drawings():
        if d["type"] == "f":
            continue
        cls = wclass(d.get("width"))
        if cls is None:
            continue
        for it in d["items"]:
            if it[0] != "l":
                continue
            p1, p2 = it[1], it[2]
            mx, my = (p1.x + p2.x) / 2, (p1.y + p2.y) / 2
            if not (x0 <= mx <= x1 and y0 <= my <= y1):
                continue
            if tb.contains(fitz.Point(mx, my)):
                continue
            L = math.hypot(p2.x - p1.x, p2.y - p1.y)
            if L < 0.5:
                continue
            seg = {"p1": (p1.x, p1.y), "p2": (p2.x, p2.y), "L": L, "w": cls}
            if cls == "dash":
                if L <= DASH_MAX_LEN:
                    dashes.append(seg)
                continue
            if cls == "aux":
                # 0.71pt is ambiguous: symbols AND auxiliary pipes share it.
                # Resolve structurally - drop everything that can be
                # identified as symbol geometry; what survives is aux piping
                # and instrument leader lines:
                if len(d["items"]) > 4:
                    continue  # circles, vessel heads, arcs
                ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x)) % 180
                axis_aligned = ang < 8 or ang > 172 or 82 < ang < 98
                if not axis_aligned and L < 12:
                    continue  # arrowheads, ticks, bowtie remnants
                if L < 3:
                    continue  # nozzle bars, break marks
                if any(math.hypot(mx - bx, my - by) <= br + 1
                       for bx, by, br in bubbles):
                    continue  # strokes inside instrument bubbles
            # drop strokes inside valve bodies (bowtie sides/end bars)
            if any(vb.contains(fitz.Point(mx, my)) for vb in vboxes):
                continue
            # drop short strokes inside text bboxes (underlines)
            if L < 120 and any(
                b.contains(fitz.Point(p1.x, p1.y)) and b.contains(fitz.Point(p2.x, p2.y))
                for b in tboxes
            ):
                continue
            solid.append(seg)
    return solid, dashes


def chain_dashes(dashes):
    """Merge collinear dashes with small gaps into dashed polylines.
    Hatch fill dashes have no long collinear continuation and are dropped."""
    used = [False] * len(dashes)
    lines = []
    for i, s in enumerate(dashes):
        if used[i]:
            continue
        dx, dy = s["p2"][0] - s["p1"][0], s["p2"][1] - s["p1"][1]
        ux, uy = dx / s["L"], dy / s["L"]
        chain = [i]
        used[i] = True
        grew = True
        while grew:
            grew = False
            pts = [p for k in chain for p in (dashes[k]["p1"], dashes[k]["p2"])]
            ts = [(p[0] * ux + p[1] * uy) for p in pts]
            lo_t, hi_t = min(ts), max(ts)
            lo_p = pts[ts.index(lo_t)]; hi_p = pts[ts.index(hi_t)]
            ref = dashes[chain[0]]["p1"]
            for j, t in enumerate(dashes):
                if used[j]:
                    continue
                # collinear: direction parallel and offset small
                dxj, dyj = t["p2"][0] - t["p1"][0], t["p2"][1] - t["p1"][1]
                cross = abs(ux * dyj - uy * dxj) / t["L"]
                if cross > 0.15:
                    continue
                off = abs((t["p1"][0] - ref[0]) * uy - (t["p1"][1] - ref[1]) * ux)
                if off > 1.5:
                    continue
                dmin = min(
                    math.hypot(a[0] - b[0], a[1] - b[1])
                    for a in (t["p1"], t["p2"]) for b in (lo_p, hi_p)
                )
                if dmin < DASH_GAP:
                    chain.append(j)
                    used[j] = True
                    grew = True
        if len(chain) >= 3:
            pts = [p for k in chain for p in (dashes[k]["p1"], dashes[k]["p2"])]
            ts = [(p[0] * ux + p[1] * uy) for p in pts]
            a = pts[ts.index(min(ts))]; b = pts[ts.index(max(ts))]
            lines.append({"p1": a, "p2": b, "style": "dashed",
                          "L": math.hypot(b[0] - a[0], b[1] - a[1])})
    return lines


class PointPool:
    """Snap nearby endpoints to shared node ids."""
    def __init__(self, tol):
        self.tol = tol
        self.pts = []

    def get(self, p):
        for i, q in enumerate(self.pts):
            if math.hypot(p[0] - q[0], p[1] - q[1]) < self.tol:
                return i
        self.pts.append(list(p))
        return len(self.pts) - 1


def point_on_seg(p, a, b, tol):
    ax, ay = a; bx, by = b
    L2 = (bx - ax) ** 2 + (by - ay) ** 2
    if L2 == 0:
        return None
    t = ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2
    if t < 0.02 or t > 0.98:
        return None
    qx, qy = ax + t * (bx - ax), ay + t * (by - ay)
    if math.hypot(p[0] - qx, p[1] - qy) < tol:
        return t
    return None


def bridge_solid_gaps(solid):
    """Connect collinear solid segments separated by small gaps
    (spec-break marks interrupt pipe strokes)."""
    extra = []
    for i in range(len(solid)):
        for j in range(i + 1, len(solid)):
            a, b = solid[i], solid[j]
            dax, day = a["p2"][0] - a["p1"][0], a["p2"][1] - a["p1"][1]
            dbx, dby = b["p2"][0] - b["p1"][0], b["p2"][1] - b["p1"][1]
            cross = dax * dby - day * dbx
            if abs(cross) / (a["L"] * b["L"]) > 0.05:
                continue
            best = None
            for pa in (a["p1"], a["p2"]):
                for pb in (b["p1"], b["p2"]):
                    dd = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    if dd < (best[0] if best else 1e9):
                        best = (dd, pa, pb)
            dd, pa, pb = best
            if not (SNAP < dd <= SOLID_GAP):
                continue
            # gap direction must continue the line, not run parallel to it
            ux, uy = dax / a["L"], day / a["L"]
            off = abs((pb[0] - pa[0]) * uy - (pb[1] - pa[1]) * ux)
            if off < 1.5:
                extra.append({"p1": pa, "p2": pb, "L": dd, "w": "bridge"})
    return extra


def build_graph(page_num, doc, valves, instruments, arrows=()):
    """Turn raw segments into a connectivity graph for one sheet.

    Order of operations:
      1. collect segments (width-filtered, symbol/title/text masked)
      2. bridge gaps left by spec-break symbols
      3. chain signal-line dashes
      4. split segments where another segment's endpoint touches them
         (T-junctions), so every connection becomes a shared node
      5. snap endpoints into shared node ids (PointPool)
      6. bridge remaining stubs into valve/instrument anchor nodes -
         pipes stop AT a valve's body, so the valve node reconnects them
      7. classify plain nodes by degree (dead-end / corner / junction)
    """
    page = doc[page_num - 1]
    solid, dash_raw = collect_segments(page, valves, instruments, arrows)
    solid = solid + bridge_solid_gaps(solid)
    dashed = chain_dashes(dash_raw)
    segs = ([dict(s, style="solid") for s in solid] + dashed)

    # split segments at T-junctions (an endpoint of one lies on another)
    endpoints = [s["p1"] for s in segs] + [s["p2"] for s in segs]
    out = []
    for s in segs:
        cuts = []
        for p in endpoints:
            t = point_on_seg(p, s["p1"], s["p2"], SNAP)
            if t is not None:
                cuts.append((t, p))
        cuts.sort()
        prev = s["p1"]
        for t, p in cuts:
            out.append(dict(s, p1=prev, p2=p))
            prev = p
        out.append(dict(s, p1=prev, p2=s["p2"]))
    segs = [s for s in out
            if math.hypot(s["p2"][0] - s["p1"][0], s["p2"][1] - s["p1"][1]) > 0.5]

    pool = PointPool(SNAP)
    edges = []
    for s in segs:
        a, b = pool.get(s["p1"]), pool.get(s["p2"])
        if a != b:
            edges.append({"a": a, "b": b, "style": s["style"]})

    # attach nodes to valves / instruments
    nodes = [{"id": i, "xy": tuple(p), "kind": "point"} for i, p in enumerate(pool.pts)]
    for v in valves:
        vid = pool.get(v["center"])
        if vid == len(nodes):
            nodes.append({"id": vid, "xy": tuple(v["center"]), "kind": "valve"})
        else:
            nodes[vid]["kind"] = "valve"
        nodes[vid]["ball"] = v.get("ball", False)
        nodes[vid]["subclass"] = v.get("subclass", "gate")
        if v.get("flow_dir"):
            nodes[vid]["flow_dir"] = v["flow_dir"]
    for r in instruments:
        iid = pool.get(tuple(r["center"]))
        if iid == len(nodes):
            nodes.append({"id": iid, "xy": tuple(r["center"]), "kind": "instrument"})
        else:
            nodes[iid]["kind"] = "instrument"
        nodes[iid]["tag"] = r["tag"]

    # bridge stubs to valve/instrument nodes
    deg = defaultdict(int)
    for e in edges:
        deg[e["a"]] += 1; deg[e["b"]] += 1
    for n in nodes:
        if n["kind"] not in ("valve", "instrument"):
            continue
        reach = VALVE_REACH if n["kind"] == "valve" else 22.0
        for m in nodes:
            if m is n or m["kind"] != "point" or deg[m["id"]] != 1:
                continue
            dd = math.hypot(n["xy"][0] - m["xy"][0], n["xy"][1] - m["xy"][1])
            if dd < reach:
                edges.append({"a": n["id"], "b": m["id"], "style": "bridge"})
                deg[n["id"]] += 1; deg[m["id"]] += 1

    # classify point nodes
    for n in nodes:
        if n["kind"] != "point":
            continue
        d = deg[n["id"]]
        n["kind"] = ("dead-end" if d <= 1 else
                     "corner" if d == 2 else "junction")
    return nodes, edges


def render(page, nodes, edges, out_png):
    import random
    random.seed(7)
    shape = page.new_shape()
    for e in edges:
        a = nodes[e["a"]]["xy"]; b = nodes[e["b"]]["xy"]
        col = ((0.9, 0.1, 0.1) if e["style"] == "solid" else
               (0.1, 0.5, 0.9) if e["style"] == "dashed" else (0.1, 0.7, 0.2))
        shape.draw_line(fitz.Point(*a), fitz.Point(*b))
        shape.finish(color=col, width=2.2)
    for n in nodes:
        col = {"valve": (1, 0, 1), "instrument": (0, 0, 1),
               "junction": (1, 0.6, 0), "dead-end": (0.5, 0, 0)}.get(n["kind"])
        if col:
            shape.draw_circle(fitz.Point(*n["xy"]), 6)
            shape.finish(color=col, width=1.8)
    shape.commit()
    pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
    pix.save(out_png)


if __name__ == "__main__":
    page_num = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    doc = fitz.open(PDF_PATH)
    valves = [v for v in json.load(open("output/valve_index.json"))
              if v["page"] == page_num]
    instruments = [r for r in json.load(open("output/instrument_index.json"))
                   if r["page"] == page_num and r["class"] == "instrument"]
    try:
        arrows = [a for a in json.load(open("output/arrow_index.json"))
                  if a["page"] == page_num]
    except FileNotFoundError:
        arrows = []
    nodes, edges = build_graph(page_num, doc, valves, instruments, arrows)

    from collections import Counter
    print("nodes:", Counter(n["kind"] for n in nodes))
    print("edges:", Counter(e["style"] for e in edges))
    with open(f"output/graph_page{page_num}.json", "w", encoding="utf-8") as f:
        json.dump({"nodes": nodes, "edges": edges}, f, ensure_ascii=False, indent=1)
    render(doc[page_num - 1], nodes, edges, f"output/graph_page{page_num}_overlay.png")
    print(f"-> output/graph_page{page_num}.json + overlay png")
