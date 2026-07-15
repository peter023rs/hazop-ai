"""
Graph assembly: fuse the symbol indexes (instruments, valves, equipment) with
the traced line graph into one typed topology graph per sheet.

Steps
  1. sheet tag prefix from the notes ("...位号以"2401-"为前缀")
  2. equipment nodes claim their internal strokes (vessel shells) and expose
     nozzle nodes where pipes cross the equipment boundary
  3. corner chains collapse into single pipe-run edges (polylines)
  4. pipe line-number labels (e.g. 50-IA-00501-M11E-N) attach to the nearest
     run; instrument tags get the sheet prefix
  5. off-page connectors from directional text (…至…/…自…) or drawing refs
  6. flow direction: seed runs from on-pipe arrows (arrow_index), check-valve
     markers and 至/自 connector text, then propagate through pass-through
     nodes (valves and 2-way junctions); conflicts flagged, never guessed
  7. angle valves with a PSV bubble nearby upgrade to subclass "safety"
Output: output/topology_page<N>.json, overlay PNG, cross-sheet stitch report

Usage:  python3 scripts/assemble_graph.py [page]    (default: all 4-12)
"""
import fitz
import json
import math
import re
import sys
from collections import defaultdict

PDF_PATH = "2401单元工艺管道及仪表流程图.pdf"
PROCESS_PAGES = range(4, 13)

LINE_NO_RE = re.compile(r"^\d{2,3}-[A-Z]{1,4}-\d{4,5}-[A-Z0-9]+(-[A-Z0-9]+)?$")
DWG_REF_RE = re.compile(r"^[A-Z]{1,3}\d{2}-\d{3,4}$")
PREFIX_RE = re.compile(r"以[“\"]?(24\d{2})-?[”\"]?为前缀")


def get_spans(page):
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                t = span["text"].strip()
                if t:
                    x0, y0, x1, y1 = span["bbox"]
                    spans.append({"text": t, "bbox": span["bbox"],
                                  "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2})
    return spans


def pt_seg_dist(p, a, b):
    ax, ay = a; bx, by = b
    L2 = (bx - ax) ** 2 + (by - ay) ** 2
    if L2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0, min(1, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2))
    return math.hypot(p[0] - (ax + t * (bx - ax)), p[1] - (ay + t * (by - ay)))


def seg_dir_at(points, p):
    """Local direction of a polyline where it passes closest to point p."""
    best, bestd = 0, 1e9
    for k in range(len(points) - 1):
        d = pt_seg_dist(p, points[k], points[k + 1])
        if d < bestd:
            best, bestd = k, d
    ax, ay = points[best]; bx, by = points[best + 1]
    L = math.hypot(bx - ax, by - ay) or 1.0
    return ((bx - ax) / L, (by - ay) / L), bestd


def orient_runs(nodes, runs, arrows, connectors):
    """Assign flow direction to pipe runs.

    Seeds (never overwritten):
      - on-pipe flow arrows: nearest parallel run within 6 pt
      - check-valve flow markers (flow_dir on the valve node): the runs
        incident to the valve
      - off-page connector text: 至/去 (to) = flow toward the connector,
        自/来 (from) = flow away from it
      - safety-valve orientation (equipment semantics): PSVs are angle
        valves drawn spring-up in this convention — the process inlet rises
        into the valve from below, the outlet leaves horizontally; flow is
        always inlet -> valve -> outlet
    Propagation: flow passes straight through any node with exactly two
    solid-pipe runs (valves, spec breaks, leftover pass-throughs). At a
    branching junction directions are only inferred by conservation — a
    junction stores no fluid, so when every directed branch points the same
    way (all in, or all out) and exactly one branch is undirected, that
    branch must carry the balancing flow the other way; anything weaker is
    never guessed. A run that two seeds disagree about gets
    direction=None + direction_conflict=True.
    direction is "ab" (points a->b along the stored polyline) or "ba".
    """
    node_by_id = {n["id"]: n for n in nodes}
    pipe_runs = [r for r in runs if r["style"] == "solid"]
    incident = defaultdict(list)
    for r in pipe_runs:
        incident[r["a"]].append(r)
        incident[r["b"]].append(r)

    def set_dir(r, direction, source):
        cur = r.get("direction")
        if cur is None:
            r["direction"] = direction
            r["direction_source"] = source
            return True
        if cur != direction:
            r["direction_conflict"] = True
        return False

    # --- seeds: arrows ------------------------------------------------------
    for a in arrows:
        ux, uy = a["dir"]
        best, bestd, bestflip = None, 6.0, False
        for r in pipe_runs:
            (dx, dy), d = seg_dir_at(r["points"], a["center"])
            if d >= bestd:
                continue
            dot = dx * ux + dy * uy
            if abs(dot) < 0.85:
                continue  # arrow not parallel to this run
            best, bestd, bestflip = r, d, dot < 0
        if best is not None:
            set_dir(best, "ba" if bestflip else "ab", "arrow")

    # --- seeds: check-valve flow markers ------------------------------------
    for n in nodes:
        if n.get("flow_dir") and n["kind"] == "valve":
            ux, uy = n["flow_dir"]
            for r in incident[n["id"]]:
                (dx, dy), _ = seg_dir_at(r["points"], n["xy"])
                dot = dx * ux + dy * uy
                if abs(dot) >= 0.7:
                    set_dir(r, "ab" if dot > 0 else "ba", "check-valve")

    # --- seeds: connector text ----------------------------------------------
    for c in connectors:
        label = c["label"]
        to_conn = bool(re.search(r"[至去]", label))
        from_conn = bool(re.search(r"[自来]", label))
        if to_conn == from_conn:
            continue  # ambiguous or absent
        nid = c["node"]
        for r in incident[nid]:
            # direction so that flow moves toward (至) / away from (自) node
            if r["a"] == nid:
                set_dir(r, "ba" if to_conn else "ab", "connector")
            elif r["b"] == nid:
                set_dir(r, "ab" if to_conn else "ba", "connector")

    # --- seeds: safety-valve orientation --------------------------------------
    # every PSV on this drawing follows one template (verified pages
    # 5/7/9/10/12): angle valve, spring ticks up, inlet run rising into the
    # valve from BELOW, outlet run leaving horizontally. Flow through a
    # relief valve is one-way by construction, so both runs get a seed;
    # bonnet/vent stubs (approach from above) and PSV-bubble leaders
    # (far node = instrument) are not process pipes and are skipped.
    for n in nodes:
        if n["kind"] != "valve" or n.get("subclass") != "safety":
            continue
        for r in incident[n["id"]]:
            if r["a"] == r["b"]:
                continue  # spring-tick self loop
            far = node_by_id.get(r["b"] if r["a"] == n["id"] else r["a"])
            if far is None or far["kind"] == "instrument":
                continue
            pts = r["points"]
            if math.dist(pts[0], n["xy"]) <= math.dist(pts[-1], n["xy"]):
                near, nxt = pts[0], pts[1]
            else:
                near, nxt = pts[-1], pts[-2]
            dx, dy = nxt[0] - near[0], nxt[1] - near[1]
            if dy > abs(dx):
                into_valve = True            # attaches from below: inlet
            elif abs(dx) > abs(dy):
                into_valve = False           # horizontal: outlet
            else:
                continue                     # rises above: bonnet/vent stub
            if (r["a"] == n["id"]) == into_valve:
                set_dir(r, "ba", "psv")
            else:
                set_dir(r, "ab", "psv")

    # --- propagation through pass-through nodes -----------------------------
    # A junction passes flow straight through when it has exactly two REAL
    # pipe continuations; instrument-tap leaders (run ending at a bubble)
    # and micro-stubs (dead-end, < 8 pt) hang off main lines everywhere and
    # do not count as branches.
    def ignorable(q, at_node):
        far = q["b"] if q["a"] == at_node else q["a"]
        fn = node_by_id.get(far)
        if fn is None:
            return False
        return (fn["kind"] == "instrument"
                or (fn["kind"] == "dead-end" and q["length"] < 8))

    def propagate(frontier):
        while frontier:
            r = frontier.pop()
            d = r.get("direction")
            if d is None:
                continue
            # flow exits at 'downstream end', enters at 'upstream end'
            down_node = r["b"] if d == "ab" else r["a"]
            up_node = r["a"] if d == "ab" else r["b"]
            for node, incoming in ((down_node, True), (up_node, False)):
                n = node_by_id.get(node)
                if n is None or n["kind"] not in ("valve", "junction",
                                                  "dead-end"):
                    continue
                others = [q for q in incident[node] if q is not r
                          and not ignorable(q, node)]
                # duplicate parallel strokes produce twin runs to the same far
                # node; they are one physical branch
                by_far = defaultdict(list)
                for q in others:
                    by_far[q["b"] if q["a"] == node else q["a"]].append(q)
                if len(by_far) != 1:
                    continue  # branching junction: conservation's job
                for q in next(iter(by_far.values())):
                    if q.get("direction"):
                        continue
                    # incoming=True: flow continues out of `node` through q
                    # incoming=False: flow arrives into `node` through q
                    if q["a"] == node:
                        nd = "ab" if incoming else "ba"
                    else:
                        nd = "ba" if incoming else "ab"
                    if set_dir(q, nd, "propagated"):
                        frontier.append(q)

    def flow_into(q, node):
        """True if q's flow enters `node`, False if it leaves, None unknown."""
        d = q.get("direction")
        if d is None:
            return None
        return node == (q["b"] if d == "ab" else q["a"])

    propagate([r for r in pipe_runs if r.get("direction")])

    # --- conservation at branching junctions ---------------------------------
    # a junction stores no fluid: it must have both inflow and outflow, so
    # if every directed branch points the same way and exactly ONE branch
    # group is undirected, that branch carries the balance the other way.
    # Interleaved with pass-through propagation to a fixpoint.
    while True:
        newly = []
        for node, qs in incident.items():
            n = node_by_id.get(node)
            if n is None or n["kind"] not in ("junction", "valve"):
                continue
            by_far = defaultdict(list)
            for q in qs:
                if not ignorable(q, node):
                    by_far[q["b"] if q["a"] == node else q["a"]].append(q)
            if len(by_far) < 3:
                continue  # pass-through territory, nothing to conserve
            statuses, unknown = [], []
            for group in by_far.values():
                dirs = {flow_into(q, node) for q in group} - {None}
                if len(dirs) > 1:
                    break  # twin strokes disagree: leave to conflict flags
                (statuses if dirs else unknown).append(
                    dirs.pop() if dirs else group)
            else:
                if len(unknown) != 1 or len(set(statuses)) != 1:
                    continue
                forced_in = not statuses[0]
                for q in unknown[0]:
                    if forced_in:
                        nd = "ab" if q["b"] == node else "ba"
                    else:
                        nd = "ab" if q["a"] == node else "ba"
                    if set_dir(q, nd, "conservation"):
                        newly.append(q)
        if not newly:
            break
        propagate(newly)

    n_dir = sum(1 for r in pipe_runs if r.get("direction"))
    n_conf = sum(1 for r in pipe_runs if r.get("direction_conflict"))
    return len(pipe_runs), n_dir, n_conf


def assemble(page_num, doc, equipment, arrows=()):
    page = doc[page_num - 1]
    g = json.load(open(f"output/graph_page{page_num}.json"))
    nodes, edges = g["nodes"], g["edges"]
    spans = get_spans(page)

    # -- 1. sheet prefix ---------------------------------------------------
    prefix = None
    for s in spans:
        m = PREFIX_RE.search(s["text"])
        if m:
            prefix = m.group(1) + "-"
            break

    # -- 2. equipment claim + nozzles ---------------------------------------
    eq_items = []
    for c in equipment["capsules"]:
        if c["page"] == page_num:
            eq_items.append({"bbox": c["bbox"], "tag": c.get("tag"),
                             "shape": "capsule", "orientation": c["orientation"]})
    for c in equipment["equipment_circles"]:
        if c["page"] == page_num:
            r = c["r"]
            eq_items.append({"bbox": [c["center"][0] - r, c["center"][1] - r,
                                      c["center"][0] + r, c["center"][1] + r],
                             "tag": None, "shape": "circle"})

    def owner(xy):
        for k, e in enumerate(eq_items):
            b = e["bbox"]
            if b[0] - 2 <= xy[0] <= b[2] + 2 and b[1] - 2 <= xy[1] <= b[3] + 2:
                return k
        return None

    # Equipment "claims" its geometry: an edge with BOTH endpoints inside an
    # equipment bbox is internal furniture (vessel shell, compressor zigzag)
    # and is dropped from the pipe network; an edge crossing the boundary is
    # a real pipe connection - its inside endpoint becomes a nozzle node.
    node_owner = [owner(n["xy"]) for n in nodes]
    eq_node_ids = {}
    new_edges = []
    for e in edges:
        oa, ob = node_owner[e["a"]], node_owner[e["b"]]
        if oa is not None and ob is not None:
            continue  # internal stroke (shell/zigzag) - claimed by equipment
        if oa is None and ob is None:
            new_edges.append(e)
            continue
        # boundary crossing: inside endpoint becomes a nozzle
        k = oa if oa is not None else ob
        nid = e["a"] if oa is not None else e["b"]
        nodes[nid]["kind"] = "nozzle"
        nodes[nid]["equipment"] = k
        # nearby nozzle label (N1, P2, L1-1 ...)
        for s in spans:
            if (re.match(r"^[NPL]\d(-\d)?$", s["text"])
                    and math.hypot(s["cx"] - nodes[nid]["xy"][0],
                                   s["cy"] - nodes[nid]["xy"][1]) < 22):
                nodes[nid]["nozzle_label"] = s["text"]
                break
        new_edges.append(e)
    edges = new_edges

    # add one node per equipment item, linked to its nozzles
    for k, e in enumerate(eq_items):
        b = e["bbox"]
        nid = len(nodes)
        nodes.append({"id": nid, "xy": ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2),
                      "kind": "equipment", "tag": e["tag"], "shape": e["shape"],
                      "bbox": e["bbox"]})
        eq_node_ids[k] = nid
    for n in nodes:
        if n["kind"] == "nozzle":
            edges.append({"a": n["id"], "b": eq_node_ids[n["equipment"]],
                          "style": "internal"})

    # -- 3. collapse corner chains ------------------------------------------
    # The traced graph is thousands of micro-segments; for the topology we
    # only care about "anchor" nodes (valves, instruments, equipment,
    # nozzles, junctions, dead-ends). Any 2-degree pass-through node is
    # collapsible: chains of them merge into ONE pipe-run edge that keeps
    # the full polyline for rendering and length measurement.
    adj = defaultdict(list)
    for i, e in enumerate(edges):
        adj[e["a"]].append(i)
        adj[e["b"]].append(i)
    keep_kinds = {"valve", "instrument", "equipment", "nozzle", "junction",
                  "dead-end"}
    collapsible = {
        n["id"] for n in nodes
        if n["kind"] not in keep_kinds and len(adj[n["id"]]) == 2
        and len({edges[i]["style"] for i in adj[n["id"]]} - {"bridge"}) <= 1
    }
    runs = []
    visited_e = set()

    def walk(start_edge, start_node):
        """Follow a chain of collapsible nodes from start_node across
        start_edge until an anchor (or a dead trail) is reached."""
        chain = [start_edge]
        visited_e.add(start_edge)
        e0 = edges[start_edge]
        cur = e0["b"] if e0["a"] == start_node else e0["a"]
        while cur in collapsible:
            nxt = [j for j in adj[cur] if j not in visited_e]
            if not nxt:
                break
            j = nxt[0]
            visited_e.add(j)
            chain.append(j)
            cur = edges[j]["b"] if edges[j]["a"] == cur else edges[j]["a"]
        return chain, cur

    def emit(chain, start, end):
        pts = [nodes[start]["xy"]]
        p = start
        for j in chain:
            p = edges[j]["b"] if edges[j]["a"] == p else edges[j]["a"]
            pts.append(nodes[p]["xy"])
        style = next((edges[j]["style"] for j in chain
                      if edges[j]["style"] != "bridge"), "solid")
        runs.append({"a": start, "b": end, "style": style, "points": pts,
                     "length": round(sum(
                         math.hypot(pts[k + 1][0] - pts[k][0],
                                    pts[k + 1][1] - pts[k][1])
                         for k in range(len(pts) - 1)), 1)})

    # pass 1: chains anchored at non-collapsible nodes
    for i, e in enumerate(edges):
        if i in visited_e:
            continue
        for start in (e["a"], e["b"]):
            if start in collapsible or i in visited_e:
                continue
            chain, end = walk(i, start)
            emit(chain, start, end)
    # pass 2: leftovers - loops or chains living entirely on collapsible
    # nodes (dashed vendor boxes, isolated signal lines)
    for i, e in enumerate(edges):
        if i in visited_e:
            continue
        back, tail_start = walk(i, e["b"])   # walk one way from a
        # restart walking the other way from the far end
        visited_e.difference_update(back)
        chain, end = walk(i, tail_start)
        emit(chain, tail_start, end)

    # -- 4. labels: line numbers to runs, prefix to instruments -------------
    # A line-number text span (e.g. 50-IA-00501-M11E-N) is written alongside
    # the pipe it names: attach it to the run with the smallest perpendicular
    # distance. These labels later drive CROSS-SHEET stitching - the same
    # line number on two sheets means the same physical pipe.
    for s in spans:
        if not LINE_NO_RE.match(s["text"]):
            continue
        best, bestd = None, 30.0
        for r in runs:
            if r["style"] == "internal":
                continue
            d = min(pt_seg_dist((s["cx"], s["cy"]), r["points"][k], r["points"][k + 1])
                    for k in range(len(r["points"]) - 1))
            if d < bestd:
                best, bestd = r, d
        if best is not None:
            best.setdefault("line_numbers", []).append(s["text"])
    for n in nodes:
        if n["kind"] == "instrument" and n.get("tag") and prefix:
            parts = n["tag"].split()
            if len(parts) >= 2:
                n["full_tag"] = f"{prefix}{parts[0]}-{parts[-1]}"

    # -- 5. off-page connectors ---------------------------------------------
    used_nodes = {r["a"] for r in runs} | {r["b"] for r in runs}
    deg = defaultdict(int)
    for r in runs:
        deg[r["a"]] += 1
        deg[r["b"]] += 1
    connectors = []
    for s in spans:
        is_dir = (re.search(r"[至自来]", s["text"]) and len(s["text"]) >= 4
                  and not SPECISH.search(s["text"]))
        is_ref = bool(DWG_REF_RE.match(s["text"]))
        if not (is_dir or is_ref):
            continue
        # prefer a connector-circle "equipment" node (hatched-arrow circles
        # are untagged circles that landed in the equipment pass)
        best, bestd, best_kind = None, 70.0, None
        for n in nodes:
            if n["kind"] == "equipment" and n.get("shape") == "circle":
                d = math.hypot(s["cx"] - n["xy"][0], s["cy"] - n["xy"][1])
                if d < bestd:
                    best, bestd, best_kind = n["id"], d, "equipment-circle"
        for nid in used_nodes:
            if nodes[nid]["kind"] != "dead-end" or deg[nid] != 1:
                continue
            d = math.hypot(s["cx"] - nodes[nid]["xy"][0],
                           s["cy"] - nodes[nid]["xy"][1])
            if d < bestd:
                best, bestd, best_kind = nid, d, "dead-end"
        if best is not None:
            nodes[best]["kind"] = "off-page"
            nodes[best].setdefault("labels", []).append(s["text"])
            connectors.append({"node": best, "label": s["text"]})

    # -- 6. flow direction ---------------------------------------------------
    n_pipe, n_dir, n_conf = orient_runs(nodes, runs, arrows, connectors)

    # -- 7. angle valve + PSV bubble nearby = safety valve --------------------
    psv_bubbles = [n for n in nodes if n["kind"] == "instrument"
                   and str(n.get("tag", "")).startswith("PSV")]
    for n in nodes:
        if n["kind"] == "valve" and n.get("subclass") == "angle":
            if any(math.hypot(n["xy"][0] - b["xy"][0],
                              n["xy"][1] - b["xy"][1]) < 60
                   for b in psv_bubbles):
                n["subclass"] = "safety"

    # -- emit -----------------------------------------------------------------
    out_nodes = []
    for n in nodes:
        if n["id"] in used_nodes or n["kind"] in ("equipment",):
            m = {"id": n["id"], "kind": n["kind"],
                 "xy": [round(n["xy"][0], 1), round(n["xy"][1], 1)]}
            for k in ("tag", "full_tag", "ball", "subclass", "flow_dir",
                      "nozzle_label", "labels", "shape", "bbox", "equipment"):
                if n.get(k) is not None:
                    m[k] = n[k]
            out_nodes.append(m)
    topo = {"page": page_num, "sheet_prefix": prefix,
            "nodes": out_nodes,
            "edges": [{k: v for k, v in r.items() if k != "points"} | {
                "points": [[round(x, 1), round(y, 1)] for x, y in r["points"]]}
                for r in runs],
            "connectors": connectors,
            "direction_stats": {"pipe_runs": n_pipe, "directed": n_dir,
                                "conflicts": n_conf}}
    with open(f"output/topology_page{page_num}.json", "w", encoding="utf-8") as f:
        json.dump(topo, f, ensure_ascii=False, indent=1)
    return topo


SPECISH = re.compile(r"[:：]|设计|入口|出口|外形|容积|处理量")


def render(doc, topo, out_png):
    page = doc[topo["page"] - 1]
    nd = {n["id"]: n for n in topo["nodes"]}
    shape = page.new_shape()
    for e in topo["edges"]:
        col = {"solid": (0.9, 0.1, 0.1), "dashed": (0.1, 0.5, 0.9),
               "internal": (0.6, 0.6, 0.6)}.get(e["style"], (0.9, 0.1, 0.1))
        pts = e["points"]
        for k in range(len(pts) - 1):
            shape.draw_line(fitz.Point(*pts[k]), fitz.Point(*pts[k + 1]))
        shape.finish(color=col, width=2.2)
    for n in topo["nodes"]:
        col = {"valve": (1, 0, 1), "instrument": (0, 0, 1),
               "equipment": (0, 0.6, 0), "nozzle": (1, 0.6, 0),
               "junction": (0.6, 0.4, 0), "off-page": (0.8, 0, 0),
               }.get(n["kind"])
        if col is None:
            continue
        if n["kind"] == "equipment" and n.get("bbox"):
            shape.draw_rect(fitz.Rect(*n["bbox"]) + (-3, -3, 3, 3))
        else:
            shape.draw_circle(fitz.Point(*n["xy"]), 7)
        shape.finish(color=col, width=2.0)
    shape.commit()
    pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
    pix.save(out_png)


if __name__ == "__main__":
    doc = fitz.open(PDF_PATH)
    equipment = json.load(open("output/equipment_index.json"))
    try:
        arrow_index = json.load(open("output/arrow_index.json"))
    except FileNotFoundError:
        arrow_index = []
    pages = [int(sys.argv[1])] if len(sys.argv) > 1 else list(PROCESS_PAGES)
    from collections import Counter
    all_line_nos = defaultdict(list)
    for p in pages:
        topo = assemble(p, doc, equipment,
                        [a for a in arrow_index if a["page"] == p])
        kinds = Counter(n["kind"] for n in topo["nodes"])
        styles = Counter(e["style"] for e in topo["edges"])
        labeled = sum(1 for e in topo["edges"] if e.get("line_numbers"))
        ds = topo["direction_stats"]
        print(f"page {p}: prefix={topo['sheet_prefix']} "
              f"nodes={dict(kinds)} edges={dict(styles)} "
              f"runs-with-line-no={labeled} connectors={len(topo['connectors'])} "
              f"directed={ds['directed']}/{ds['pipe_runs']}"
              f"{' CONFLICTS=' + str(ds['conflicts']) if ds['conflicts'] else ''}")
        for e in topo["edges"]:
            for ln in e.get("line_numbers", []):
                all_line_nos[ln].append(p)
        render(doc, topo, f"output/topology_page{p}_overlay.png")
    # cross-sheet stitch candidates: same line number on multiple sheets
    stitch = {ln: ps for ln, ps in all_line_nos.items() if len(set(ps)) > 1}
    if len(pages) > 1:
        print(f"\ncross-sheet line numbers ({len(stitch)}):")
        for ln, ps in sorted(stitch.items()):
            print(f"   {ln}: pages {sorted(set(ps))}")
