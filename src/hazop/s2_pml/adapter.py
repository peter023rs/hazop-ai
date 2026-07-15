"""
adapter.py — Stage 1 → Stage 3 topology bridge (the plant knowledge graph).

Stage 1 (`hazop_L1`) emits `plant_model_dexpi.json`: a *geometric* graph —
thousands of PipingNodes (junctions, corners, dead-ends) and short
PipingNetworkSegments, plus Equipment / Nozzle / PipingComponent /
ProcessInstrumentationFunction / PipeOffPageConnector records with drawing
coordinates. Stage 3 (`hazop_L3`) consumes an *equipment-level* graph
(`reasoner/schema.py`: EquipmentNode + Connection).

This adapter contracts the geometry away:

  1. Every Equipment / valve / instrument / off-page connector becomes a
     terminal node. Nozzles are folded into their parent equipment, and
     equipment records sharing a tag (multi-view drawings) are merged.
  2. The piping network (junction/corner/dead-end PipingNodes + segments) is
     traversed; whenever two terminals are connected through pass-through
     geometry only, one equipment-level Connection is emitted, carrying the
     line numbers seen along the way.

Flow direction: stage 1 reads on-pipe flow arrows, check-valve markers and
off-page connector text, and propagates along unbranched paths; segments
carry flowDirection ("from_to"/"to_from"). During contraction every
discovered pass-through path is classified by its own votes and paths are
pooled per (terminal pair, direction class), never across classes:

  * all votes with the walk  -> a "known" connection in flow order;
  * two pure classes in opposite directions -> TWO anti-parallel "known"
    connections (real parallel pipes, e.g. a two-stage compressor's
    discharge into its intercooler and the cooled return back);
  * votes flipping sign along one path -> that path crosses a flow
    divergence/convergence (a tee fed by a third pipe), so the pair has no
    through-flow direction: "unknown" plus a direction_note explaining why;
  * no votes anywhere -> "unknown" (drawing order, do not trust).

After contraction a pass-through propagation runs at the *equipment* level:
a valve / check valve / relief valve / off-page connector with exactly two
process-side connections stores no fluid, so once one side is direction-
verified the other is forced ("l2-passthrough"). Stage 1 already propagates
along sheet geometry; this pass adds only what the contracted graph can
see — terminal pairs whose geometry pooled into mixed direction classes,
and continuation across sheets through linked off-page connectors.
Connections that cross a flow divergence (direction_note) are never forced.

"conflict" remains in the vocabulary for genuinely contradictory evidence,
but stage 1 already refuses to emit contradictory flowDirection on a run
(disagreeing seeds set direction_conflict there), so it should stay rare.
Caveat: the BFS visits each geometry node once per start terminal, so path
enumeration relies on pairs being walked from both ends and from every
nozzle — adequate here, not exhaustive in general.

Valve classes: CheckValve -> check_valve, SafetyValve -> relief_valve,
other piping components -> valve.

detection_confidence is 1.0 for tag-text-derived elements (vector text is
exact) and 0.7 for untagged geometry-only detections.

Output is plain dicts (JSON-safe). `to_l3_topology()` converts to real L3
dataclasses when the hazop_L3 folder is importable.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

# tag letter -> stage 3 EquipmentType value (2401 convention: 2401-K-001A)
TYPE_BY_LETTER = {
    "K": "compressor",
    "C": "compressor",
    "V": "vessel",
    "T": "tank",
    "P": "pump",
    "E": "heat_exchanger",
    "PK": "compressor",   # packaged machine train
    "F": "vessel",        # filter — closest available class
    "D": "vessel",        # dryer/drum
}

_TAG_RE = re.compile(r"^\d{4}-([A-Z]+)-\d+")


def equipment_type_for_tag(tag: str) -> str:
    m = _TAG_RE.match(tag or "")
    if m:
        return TYPE_BY_LETTER.get(m.group(1), "vessel")
    return "vessel"


def load_plant_model(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_equipment_graph(model: dict) -> dict:
    """Contract the stage 1 conceptual model into an equipment-level graph.

    Returns {"nodes": [...], "edges": [...], "stats": {...}} where nodes/edges
    mirror stage 3's EquipmentNode/Connection fields.
    """
    cm = model["conceptualModel"]

    # ---- terminals: raw element id -> canonical tag -------------------------
    terminal_of: dict[str, str] = {}
    nodes: dict[str, dict] = {}          # tag -> node dict (merged by tag)

    def add_node(tag: str, etype: str, name: str, sheet, confidence: float,
                 **attrs):
        if tag in nodes:
            nodes[tag]["attributes"].setdefault("sheets", [])
            if sheet not in nodes[tag]["attributes"]["sheets"]:
                nodes[tag]["attributes"]["sheets"].append(sheet)
            return
        nodes[tag] = {
            "tag": tag,
            "equipment_type": etype,
            "name": name or tag,
            "attributes": {"sheets": [sheet], **attrs},
            "detection_confidence": confidence,
        }

    for eq in cm.get("Equipment", []):
        tag = eq.get("tagName") or f"EQ-{eq['id']}"
        conf = 1.0 if eq.get("tagName") else 0.7
        add_node(tag, equipment_type_for_tag(eq.get("tagName") or ""),
                 eq.get("tagName") or f"untagged {eq.get('shape', '?')}",
                 eq.get("sheet"), conf, shape=eq.get("shape"))
        terminal_of[eq["id"]] = tag

    # nozzles belong to their equipment: fold into the parent tag
    for nz in cm.get("Nozzle", []):
        parent = terminal_of.get(nz.get("equipment"))
        if parent:
            terminal_of[nz["id"]] = parent

    PC_TYPE = {"CheckValve": "check_valve", "SafetyValve": "relief_valve"}
    for pc in cm.get("PipingComponent", []):
        cls = pc.get("componentClass", "Valve")
        tag = f"XV-{pc['id']}"
        add_node(tag, PC_TYPE.get(cls, "valve"), cls,
                 pc.get("sheet"), 0.9, component_class=cls)
        terminal_of[pc["id"]] = tag

    for pif in cm.get("ProcessInstrumentationFunction", []):
        tag = pif.get("tagName") or f"I-{pif['id']}"
        conf = 1.0 if pif.get("tagName") else 0.7
        add_node(tag, "instrument", pif.get("tagName") or "instrument",
                 pif.get("sheet"), conf)
        terminal_of[pif["id"]] = tag

    for opc in cm.get("PipeOffPageConnector", []):
        labels = opc.get("labels") or []
        tag = f"OPC-{labels[0]}" if labels else f"OPC-{opc['id']}"
        add_node(tag, "line", " / ".join(labels) or "off-page connector",
                 opc.get("sheet"), 0.9, labels=labels)
        terminal_of[opc["id"]] = tag

    # ---- piping geometry: adjacency over segments ---------------------------
    # each hop carries a flow "vote": +1 if travelling this way follows the
    # segment's flowDirection, -1 against, 0 unknown
    adj: dict[str, list[tuple[str, tuple, int, str]]] = defaultdict(list)
    for seg in cm.get("PipingNetworkSegment", []):
        lines = tuple(seg.get("lineNumber") or [])
        a, b = seg["from"], seg["to"]
        fd = seg.get("flowDirection")
        src = seg.get("flowDirectionSource", "")
        fwd = {"from_to": 1, "to_from": -1}.get(fd, 0)
        adj[a].append((b, lines, fwd, src))
        adj[b].append((a, lines, -fwd, src))

    # ---- contraction: BFS from each terminal through pass-through nodes -----
    # per terminal pair, paths pool into buckets keyed by their direction
    # class: +1 / -1 (all votes one way, relative to sorted-key order),
    # 0 (no votes), "branch" (votes flip sign along the path)
    edges: dict[tuple[str, str], dict] = {}
    for start_id, start_tag in terminal_of.items():
        seen = {start_id}
        frontier: list[tuple[str, tuple, tuple]] = [(start_id, (), ())]
        while frontier:
            cur, lines, votes = frontier.pop()
            for nxt, seg_lines, fwd, src in adj.get(cur, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                acc = lines + tuple(l for l in seg_lines if l not in lines)
                accv = votes + ((fwd, src),) if fwd else votes
                other = terminal_of.get(nxt)
                if other is None:
                    frontier.append((nxt, acc, accv))  # pass-through geometry
                elif other != start_tag:
                    key = tuple(sorted((start_tag, other)))
                    # votes are recorded relative to start_tag -> other
                    signs = {v for v, _ in accv}
                    if not signs:
                        cls = 0
                    elif len(signs) > 1:
                        cls = "branch"
                    else:
                        cls = signs.pop() * (-1 if key[0] != start_tag else 1)
                    bucket = edges.setdefault(key, {}).setdefault(
                        cls, {"lines": set(), "sources": set()})
                    bucket["lines"].update(acc)
                    bucket["sources"].update(s for _, s in accv)

    edge_list = []
    n_anti = n_no_through = 0
    for (a, b), buckets in sorted(edges.items()):
        zero_lines = set().union(*(buckets[c]["lines"]
                                   for c in (0, "branch") if c in buckets),
                                 set())
        directed = [c for c in (1, -1) if c in buckets]
        if directed:
            # undirected twin strokes of the same corridor share their lines
            n_anti += len(directed) == 2
            for cls in directed:
                bucket = buckets[cls]
                lines = bucket["lines"] | zero_lines
                edge_list.append({
                    "source": a if cls == 1 else b,
                    "target": b if cls == 1 else a,
                    "line_tag": sorted(lines)[0] if lines else "",
                    "attributes": {
                        "line_numbers": sorted(lines),
                        "direction": "known",
                        "direction_sources": sorted(bucket["sources"]),
                    },
                })
        else:
            lines = set().union(*(bk["lines"] for bk in buckets.values()))
            attrs = {"line_numbers": sorted(lines), "direction": "unknown"}
            if "branch" in buckets:
                n_no_through += 1
                attrs["direction_note"] = (
                    "no through-flow direction: the connecting path crosses "
                    "a flow divergence/convergence")
            edge_list.append({
                "source": a,
                "target": b,
                "line_tag": sorted(lines)[0] if lines else "",
                "attributes": attrs,
            })

    n_forced = _passthrough_pass(nodes, edge_list)

    connected = {t for e in edge_list for t in (e["source"], e["target"])}
    n_dir = sum(1 for e in edge_list
                if e["attributes"]["direction"] == "known")
    n_conf = sum(1 for e in edge_list
                 if e["attributes"]["direction"] == "conflict")
    return {
        "nodes": sorted(nodes.values(), key=lambda n: n["tag"]),
        "edges": edge_list,
        "stats": {
            "terminals": len(nodes),
            "connections": len(edge_list),
            "directed_connections": n_dir,
            "direction_conflicts": n_conf,
            "anti_parallel_pairs": n_anti,
            "no_through_flow_pairs": n_no_through,
            "l2_passthrough_forced": n_forced,
            "isolated_terminals": len(set(nodes) - connected),
            "raw_piping_nodes": len(cm.get("PipingNode", [])),
            "raw_segments": len(cm.get("PipingNetworkSegment", [])),
        },
    }


# node types through which flow must pass unchanged (no storage, no branching
# once contracted to exactly two process-side connections)
_PASSTHROUGH_TYPES = {"valve", "check_valve", "relief_valve", "line"}


def _passthrough_pass(nodes: dict, edge_list: list) -> int:
    """Equipment-level direction propagation, iterated to a fixpoint.

    For every pass-through node with exactly two process-side connections
    where exactly one carries direction="known": flow in must equal flow
    out, so the unknown side is forced and tagged "l2-passthrough".
    Instrument taps don't count as connections; no-through-flow pairs
    (direction_note) are never forced. Returns the number forced.
    """
    adj = defaultdict(list)
    for e in edge_list:
        adj[e["source"]].append(e)
        adj[e["target"]].append(e)

    def other_end(e, tag):
        return e["target"] if e["source"] == tag else e["source"]

    forced = 0
    changed = True
    while changed:
        changed = False
        for tag, node in nodes.items():
            if node["equipment_type"] not in _PASSTHROUGH_TYPES:
                continue
            proc = [e for e in adj[tag]
                    if nodes[other_end(e, tag)]["equipment_type"]
                    != "instrument"]
            if len(proc) != 2:
                continue
            k = [e for e in proc if e["attributes"]["direction"] == "known"]
            u = [e for e in proc if e["attributes"]["direction"] == "unknown"
                 and "direction_note" not in e["attributes"]]
            if len(k) != 1 or len(u) != 1:
                continue
            e_u = u[0]
            flow_enters = k[0]["target"] == tag
            # flow entering must leave through e_u (and vice versa)
            if flow_enters != (e_u["source"] == tag):
                e_u["source"], e_u["target"] = e_u["target"], e_u["source"]
            e_u["attributes"]["direction"] = "known"
            e_u["attributes"]["direction_sources"] = ["l2-passthrough"]
            forced += 1
            changed = True
    return forced


def to_l3_topology(graph: dict):
    """Convert the plain-dict graph into stage 3 dataclasses.
    Requires the hazop_L3 folder on sys.path."""
    from hazop.s3_are.reasoner.schema import (Connection, EquipmentNode, EquipmentType,
                                 TopologyGraph)
    nodes = [EquipmentNode(
        tag=n["tag"],
        equipment_type=EquipmentType(n["equipment_type"]),
        name=n["name"],
        attributes=n["attributes"],
        detection_confidence=n["detection_confidence"],
    ) for n in graph["nodes"]]
    edges = [Connection(
        source=e["source"],
        target=e["target"],
        line_tag=e["line_tag"],
        attributes=e["attributes"],
    ) for e in graph["edges"]]
    return TopologyGraph(nodes=nodes, edges=edges)
