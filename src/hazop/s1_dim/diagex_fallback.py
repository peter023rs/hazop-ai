"""
LLM-vision fallback for documents the deterministic pipeline refuses.

When compat_check returns "unsupported" (raster/scanned pages, missing text
layer, foreign stroke-width or tag conventions) the geometric detectors
cannot run. Instead of rejecting the upload, pipeline.run() routes the
document here: the DiagEx vision-language agent (ABB research package,
Apache-2.0) reads the page rasters with an LLM and returns a reconciled
entity/connection graph, which this module converts into the SAME per-page
topology_page<N>.json contract the deterministic path emits — so the
overlay viewer, export_dexpi and Stage 2 consume it unchanged.

What is preserved from the deterministic path's honesty rules:
  - every topology carries engine="diagex" provenance, and nodes keep the
    agent's own per-entity confidence (high/medium/low);
  - flow direction is never guessed: seeds come only from off-page-connector
    direction attributes the agent read off the drawing (arrow + 至/自
    wording), then run through the SAME orient_runs() propagation and
    junction-conservation logic as the deterministic path;
  - the 2401-convention-specific PSV orientation seed is disabled — by
    definition a document that reached this fallback does not follow the
    reference convention, so that equipment-semantics shortcut cannot be
    trusted here.

Requirements (both checked by availability() before any spend):
  - the diagex package importable:  pip install -e <DiagEx-Repro checkout>
  - a provider key: ANTHROPIC_API_KEY (default), or DIAGEX_LLM_PROVIDER=
    openrouter/kimi with the matching key, as documented by DiagEx.

Tuning: HAZOP_DIAGEX_EFFORT = low|medium|high|xhigh (default medium).

All functions that touch disk assume cwd == run_dir (pipeline.run() chdirs
there) and write under output/, like every other stage module.
"""
import json
import math
import os

import fitz

from . import assemble_graph as AG

# diagex valve_type -> the subclass vocabulary export_dexpi understands.
# Everything else keeps its raw valve_type for provenance and lets
# export_dexpi's default (GateValve) apply.
VALVE_SUBCLASS = {
    "gate": "gate",
    "ball": "ball",
    "check": "check",
    "safety_relief": "safety",
}

_PROVIDER_KEY = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "kimi": "KIMI_API_KEY",
}


def availability():
    """(ok, reason-if-not). Checked before any LLM spend."""
    try:
        import diagex  # noqa: F401  (deferred: optional dependency)
    except ImportError as exc:
        return False, (f"diagex package not installed ({exc}); "
                       f"pip install -e <DiagEx-Repro checkout> to enable "
                       f"the LLM-vision fallback")
    provider = os.environ.get("DIAGEX_LLM_PROVIDER", "anthropic").lower()
    key_var = _PROVIDER_KEY.get(provider, "ANTHROPIC_API_KEY")
    if not os.environ.get(key_var):
        return False, (f"{key_var} not set "
                       f"(DIAGEX_LLM_PROVIDER={provider})")
    return True, ""


def _extract(diagram_path, effort):
    """Run the DiagEx agent on diagram_path (relative to cwd == run_dir).

    Returns (graph_dict, pages_meta, diagex_summary). Kept separate so tests
    can monkeypatch it and exercise the conversion without an API key.
    Full agent artefacts (transcripts, pid.dexpi.json, confidence report)
    persist under diagex_runs/ inside the run directory for auditing.
    """
    from pathlib import Path

    from diagex.config import load_config
    from diagex.extractors.pid import run_pid_extract

    cfg = load_config()
    cfg.runs_dir = Path("diagex_runs")
    result = run_pid_extract(
        diagram=Path(diagram_path),
        effort=effort,
        config=cfg,
        persist=True,
    )
    graph = result.graph.model_dump(mode="json")
    pages_meta = []
    if result.run_dir is not None:
        pages_file = Path(result.run_dir) / "pages.json"
        if pages_file.exists():
            pages_meta = json.loads(pages_file.read_text(encoding="utf-8"))
    summary = {
        "model": result.model,
        "effort": result.effort,
        "run_dir": str(result.run_dir) if result.run_dir else None,
        "dexpi_json": (str(result.dexpi_json_path)
                       if result.dexpi_json_path else None),
        "stats": result.dexpi_stats,
        "cost": result.cost_summary,
    }
    return graph, pages_meta, summary


def _bbox_center(bbox):
    return (bbox["x"] + bbox["w"] / 2.0, bbox["y"] + bbox["h"] / 2.0)


def _polyline_pt(edge, scale):
    sx, sy = scale
    return [[round(x * sx, 1), round(y * sy, 1)]
            for x, y in edge.get("polyline_global") or []]


def _length(points):
    return round(sum(math.hypot(points[k + 1][0] - points[k][0],
                                points[k + 1][1] - points[k][1])
                     for k in range(len(points) - 1)), 1)


def convert_graph(graph, pages_meta, page_map, page_sizes_pt):
    """DiagEx ReconciledGraph (as dict) -> hazop per-page topology dicts.

    graph          ReconciledGraph.model_dump() output
    pages_meta     diagex pages.json records (pixel frame per page_index)
    page_map       {diagex page_index -> original 1-based PDF page number}
    page_sizes_pt  {original page number -> (width_pt, height_pt)}

    Returns (topos, report): topos maps original page number -> topology
    dict in assemble_graph's output schema; report counts what was dropped
    or synthesized so nothing disappears silently.
    """
    px_frame = {m["page_index"]: (m["width"], m["height"])
                for m in pages_meta}

    def scale_for(page_index):
        orig = page_map[page_index]
        w_pt, h_pt = page_sizes_pt[orig]
        w_px, h_px = px_frame.get(page_index, (w_pt, h_pt))
        return (w_pt / w_px if w_px else 1.0,
                h_pt / h_px if h_px else 1.0)

    report = {"nodes_dropped": 0, "edges_cross_sheet": 0,
              "edges_dangling": 0, "nozzles_synthesized": 0}
    cross_sheet = []

    # ---- nodes, grouped per original page --------------------------------
    per_page = {}   # orig page -> {"nodes":[], "runs":[], "connectors":[],
                    #               "idmap":{}, "safety_ids":set()}
    for orig in sorted(set(page_map.values())):
        per_page[orig] = {"nodes": [], "runs": [], "connectors": [],
                          "idmap": {}, "safety_ids": set()}

    node_page = {}
    for n in graph.get("nodes", []):
        pidx = n["page_index"]
        if pidx not in page_map:
            continue
        orig = page_map[pidx]
        P = per_page[orig]
        kind = n["kind"]
        if kind in ("text", "note"):
            report["nodes_dropped"] += 1
            continue
        sx, sy = scale_for(pidx)
        cx, cy = _bbox_center(n["bbox_global"])
        nid = len(P["nodes"])
        m = {"id": nid, "xy": [round(cx * sx, 1), round(cy * sy, 1)],
             "confidence": n.get("confidence")}
        attrs = n.get("attributes") or {}
        if kind == "equipment" and attrs.get("valve_type") is not None:
            vt = str(attrs["valve_type"])
            m["kind"] = "valve"
            m["valve_type"] = vt
            if n.get("label"):
                m["tag"] = n["label"]
            if VALVE_SUBCLASS.get(vt) == "safety":
                # subclass is applied AFTER orientation, so orient_runs'
                # convention-specific PSV seed never fires on this path
                P["safety_ids"].add(nid)
            elif vt in VALVE_SUBCLASS:
                m["subclass"] = VALVE_SUBCLASS[vt]
        elif kind == "equipment":
            b = n["bbox_global"]
            m["kind"] = "equipment"
            m["tag"] = n.get("label") or None
            m["bbox"] = [round(b["x"] * sx, 1), round(b["y"] * sy, 1),
                         round((b["x"] + b["w"]) * sx, 1),
                         round((b["y"] + b["h"]) * sy, 1)]
            if attrs.get("equipment_class"):
                m["equipment_class"] = attrs["equipment_class"]
        elif kind == "instrument":
            m["kind"] = "instrument"
            m["tag"] = n.get("label") or None
        elif kind == "opc":
            m["kind"] = "off-page"
            m["labels"] = [t for t in (n.get("source_quote"), n.get("label"))
                           if t][:1] or [n.get("label") or ""]
            direction = attrs.get("direction")
            if direction in ("in", "out"):
                # orient_runs seeds from 至 (to connector) / 自 (from);
                # out = flow toward the connector, in = flow away from it
                P["connectors"].append(
                    {"node": nid,
                     "label": ("至 " if direction == "out" else "自 ")
                              + (n.get("label") or ""),
                     "direction": direction})
        else:   # unexpected kind: keep the topology connected
            m["kind"] = "junction"
        P["nodes"].append(m)
        P["idmap"][n["id"]] = nid
        node_page[n["id"]] = pidx

    # ---- edges -> pipe runs ----------------------------------------------
    for e in graph.get("edges", []):
        pa, pb = node_page.get(e["from_node"]), node_page.get(e["to_node"])
        if pa is None and pb is None:
            report["edges_dangling"] += 1
            continue
        pidx = pa if pa is not None else pb
        if e.get("cross_sheet") or (pb is not None and pa is not None
                                    and pa != pb):
            report["edges_cross_sheet"] += 1
            cross_sheet.append({"from": e["from_node"], "to": e["to_node"],
                                "pages": sorted({page_map[p] for p in
                                                 (pa, pb) if p is not None}),
                                "line_type": e.get("line_type")})
            continue
        orig = page_map[pidx]
        P = per_page[orig]
        sxy = scale_for(pidx)
        points = _polyline_pt(e, sxy)

        def endpoint(diagex_id, own_end_pt):
            nid = P["idmap"].get(diagex_id)
            if nid is None:     # endpoint node was dropped (text/note)
                nid = len(P["nodes"])
                P["nodes"].append({"id": nid, "kind": "dead-end",
                                   "xy": own_end_pt})
                report["edges_dangling"] += 1
            return nid

        a_pt = points[0] if points else None
        b_pt = points[-1] if points else None
        a = endpoint(e["from_node"], a_pt or [0.0, 0.0])
        b = endpoint(e["to_node"], b_pt or [0.0, 0.0])
        if not points:
            points = [P["nodes"][a]["xy"], P["nodes"][b]["xy"]]
        style = ("solid" if e.get("line_type") in (None, "process")
                 else "dashed")
        run = {"a": a, "b": b, "style": style, "points": points,
               "length": _length(points)}
        attrs = e.get("attributes") or {}
        if attrs.get("line_id"):
            run["line_numbers"] = [attrs["line_id"]]
        if e.get("confidence"):
            run["confidence"] = e["confidence"]
        P["runs"].append(run)

    # ---- nozzle synthesis -------------------------------------------------
    # DiagEx pipes attach directly to equipment nodes; the hazop contract
    # expects a nozzle node at the boundary plus an "internal" edge to the
    # equipment (export_dexpi merges nozzles within 8 pt afterwards).
    for orig, P in per_page.items():
        nodes, runs = P["nodes"], P["runs"]
        for r in list(runs):    # snapshot: internal edges append to runs
            if r["style"] != "solid":
                continue
            for end in ("a", "b"):
                n = nodes[r[end]]
                if n["kind"] != "equipment":
                    continue
                pts = r["points"]
                near = (pts[0] if math.dist(pts[0], n["xy"])
                        <= math.dist(pts[-1], n["xy"]) else pts[-1])
                nz = {"id": len(nodes), "kind": "nozzle",
                      "xy": [near[0], near[1]], "equipment": n["id"]}
                nodes.append(nz)
                r[end] = nz["id"]
                runs.append({"a": nz["id"], "b": n["id"],
                             "style": "internal",
                             "points": [nz["xy"], n["xy"]],
                             "length": _length([nz["xy"], n["xy"]])})
                report["nozzles_synthesized"] += 1

    # ---- flow direction + emit -------------------------------------------
    topos = {}
    for orig, P in per_page.items():
        n_pipe, n_dir, n_conf = AG.orient_runs(
            P["nodes"], P["runs"], arrows=(),
            connectors=[{"node": c["node"], "label": c["label"]}
                        for c in P["connectors"]])
        for nid in P["safety_ids"]:
            P["nodes"][nid]["subclass"] = "safety"
        topos[orig] = {
            "page": orig, "sheet_prefix": None, "engine": "diagex",
            "nodes": P["nodes"], "edges": P["runs"],
            "connectors": [{"node": c["node"],
                            "label": c["label"].split(" ", 1)[-1],
                            "direction": c["direction"]}
                           for c in P["connectors"]],
            "direction_stats": {"pipe_runs": n_pipe, "directed": n_dir,
                                "conflicts": n_conf},
        }
    return topos, report | {"cross_sheet_edges": cross_sheet}


def run_fallback(pdf_path, doc, pages):
    """Full fallback: DiagEx extraction -> topology_page<N>.json files.

    pdf_path  the PDF the viewer will display (possibly normalized.pdf) —
              the SAME file is fed to DiagEx so coordinates line up
    doc       open fitz document for pdf_path
    pages     1-based page numbers to extract (compat gate already ran)

    Writes output/topology_page<N>.json (+ overlay PNGs, best-effort) and
    output/diagex_report.json; returns a summary dict for pipeline.run().
    """
    effort = os.environ.get("HAZOP_DIAGEX_EFFORT", "medium")

    # feed DiagEx only the requested pages: page subsetting isn't part of
    # its extract API, and skipping legend/title sheets saves real money
    if list(pages) != list(range(1, len(doc) + 1)):
        subset = fitz.open()
        for p in pages:
            subset.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
        subset.save("diagex_input.pdf")
        subset.close()
        input_pdf = "diagex_input.pdf"
    else:
        input_pdf = pdf_path
    page_map = {i: p for i, p in enumerate(pages)}
    page_sizes_pt = {p: (doc[p - 1].rect.width, doc[p - 1].rect.height)
                     for p in pages}

    graph, pages_meta, dg = _extract(input_pdf, effort)
    topos, report = convert_graph(graph, pages_meta, page_map, page_sizes_pt)

    counts = {"instruments": 0, "valves": 0, "equipment": 0, "off_page": 0}
    for p in pages:
        topo = topos.get(p) or {
            "page": p, "sheet_prefix": None, "engine": "diagex",
            "nodes": [], "edges": [], "connectors": [],
            "direction_stats": {"pipe_runs": 0, "directed": 0,
                                "conflicts": 0}}
        with open(f"output/topology_page{p}.json", "w",
                  encoding="utf-8") as f:
            json.dump(topo, f, ensure_ascii=False, indent=1)
        for n in topo["nodes"]:
            k = n["kind"]
            if k == "instrument":
                counts["instruments"] += 1
            elif k == "valve":
                counts["valves"] += 1
            elif k == "equipment":
                counts["equipment"] += 1
            elif k == "off-page":
                counts["off_page"] += 1
        try:
            AG.render(doc, topo, f"output/topology_page{p}_overlay.png")
        except Exception:
            pass    # overlays are a convenience, never worth failing a run

    with open("output/diagex_report.json", "w", encoding="utf-8") as f:
        json.dump({"diagex": dg, "conversion": report}, f,
                  ensure_ascii=False, indent=1)

    return {"pages": list(pages), "engine": "diagex", "counts": counts,
            "diagex": dg, "conversion": report}
