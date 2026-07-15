"""
nodes.py — HAZOP study-node boundary proposal (Fable FR-PML-2).

FR-PML-2: "The system SHALL propose HAZOP node boundaries based on design
intent changes (pressure breaks, phase changes, unit operation boundaries),
subject to facilitator approval and manual redefinition."

Input is the adapter's equipment-level graph (`build_equipment_graph`
output). The design-intent markers available at this layer are type-level —
stage 1 carries no stream conditions or phase data — so the deterministic
rules are:

  Rule G (grouping)       vessels/tanks connected directly through piping
                          and in-line valves share one design intent -> one
                          proposed node. In-line valves and instruments are
                          never members; they ride along in the condensed
                          view of whichever node owns their equipment.
  Rule P (pressure break) pumps and compressors change pressure: they never
                          merge two sections. A machine joins the section on
                          its VERIFIED suction side (its discharge starts a
                          new design intent); with no verified suction-side
                          neighbor it becomes a standalone proposed node,
                          flagged for the facilitator.
  Rule X (phase change)   heat exchangers, same treatment as Rule P.
  Rule M (machine train)  break equipment joined by verified anti-parallel
                          connections (e.g. a compressor stage and its
                          intercooler: discharge out, cooled return back)
                          is one design intent and stays together.
  Rule U (unit boundary)  off-page connectors end a node; they are recorded
                          as boundary elements, never members. Because OPCs
                          never merge sections, proposals naturally stop at
                          sheet edges.

Every proposal carries `status: "proposed"`, the rationale strings for the
rules that fired, and its boundary elements with the design-intent kind
("pressure break" / "phase change" / "unit boundary"). A proposal's
`members` list feeds `condensed_node_view(graph, members)` unchanged.

Facilitator redefinition (the second half of FR-PML-2): `merge_nodes` and
`move_member` return a NEW proposal with the affected nodes re-derived
(boundaries recomputed from the graph), `status: "redefined"`, and an entry
in the proposal-level `redefinitions` log — AI-proposed vs human-modified
stays distinguishable (AR-1).
"""

from __future__ import annotations

import copy
from collections import defaultdict

from .condense import (_INLINE_TYPES, _adjacency, _chain_direction,
                       _walk_to_equipment)

# equipment types that mark a design-intent change (Rules P and X)
_BREAK_KIND = {
    "compressor": "pressure break",
    "pump": "pressure break",
    "heat_exchanger": "phase change",
}
# terminals that mark the edge of a unit / sheet (Rule U)
_UNIT_BOUNDARY_TYPES = {"line"}

_PROPOSAL_NOTE = ("FR-PML-2: node boundaries are PROPOSALS from type-level "
                  "design-intent rules — facilitator approval and manual "
                  "redefinition required before use in a study.")


def _folded_neighbors(graph: dict) -> tuple[dict, dict]:
    """(nodes_by_tag, neighbors) where neighbors[tag][other] is the flow
    relation seen from `tag` after folding in-line components:
    "out" (verified tag->other), "in" (verified other->tag), "both"
    (verified anti-parallel pair), or "unknown"."""
    nodes = {n["tag"]: n for n in graph["nodes"]}
    adj = _adjacency(graph)
    majors = [t for t, n in nodes.items()
              if n["equipment_type"] not in _INLINE_TYPES
              and n["equipment_type"] != "instrument"]
    neighbors: dict[str, dict[str, str]] = {t: {} for t in majors}
    for tag in majors:
        for first_edge in adj[tag]:
            other = (first_edge["target"] if first_edge["source"] == tag
                     else first_edge["source"])
            if nodes[other]["equipment_type"] == "instrument":
                continue
            for chain, end in _walk_to_equipment(tag, first_edge, nodes, adj):
                if end == tag:
                    continue
                if nodes[end]["equipment_type"] in _INLINE_TYPES:
                    continue  # dead-ended valve chain, no terminal behind it
                direction, _ = _chain_direction(chain, tag)
                d = {"forward": "out", "backward": "in"}.get(direction,
                                                             "unknown")
                prev = neighbors[tag].get(end)
                if prev is None or prev == "unknown":
                    neighbors[tag][end] = d
                elif d != "unknown" and d != prev:
                    neighbors[tag][end] = "both"
    return nodes, neighbors


class _DSU:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _boundaries_for(members: set[str], nodes: dict, neighbors: dict) -> list:
    """Boundary elements of a member set: every folded neighbor outside it,
    with the design-intent kind that makes it a boundary."""
    out = []
    for m in sorted(members):
        for other, flow in sorted(neighbors.get(m, {}).items()):
            if other in members:
                continue
            etype = nodes[other]["equipment_type"]
            if etype in _UNIT_BOUNDARY_TYPES:
                kind = "unit boundary"
            else:
                kind = _BREAK_KIND.get(etype, "adjacent section")
            out.append({"tag": other, "equipment_type": etype,
                        "kind": kind, "flow": flow, "at_member": m})
    return out


def _describe(members: set[str], nodes: dict) -> str:
    counts: dict[str, int] = defaultdict(int)
    for m in members:
        counts[nodes[m]["equipment_type"]] += 1
    return " + ".join(f"{c} {t}{'s' if c > 1 else ''}"
                      for t, c in sorted(counts.items()))


def propose_nodes(graph: dict, node_prefix: str = "PN") -> dict:
    """Propose HAZOP study nodes over the equipment-level graph.

    Returns {"nodes": [...], "unassigned": [...], "redefinitions": [],
    "note": ..., "stats": {...}}. Each proposed node:
    {node_id, members, description, rationale, boundaries, status}.
    """
    nodes, neighbors = _folded_neighbors(graph)
    candidates = sorted(t for t in neighbors
                        if nodes[t]["equipment_type"]
                        not in _UNIT_BOUNDARY_TYPES)
    grouping = {t for t in candidates
                if nodes[t]["equipment_type"] not in _BREAK_KIND}
    breaks = {t for t in candidates if t not in grouping}

    unassigned = [{"tag": t,
                   "reason": "no process connections in the contracted "
                             "graph (isolated or instrument-only)"}
                  for t in candidates if not neighbors[t]]
    isolated = {u["tag"] for u in unassigned}
    active = [t for t in candidates if t not in isolated]

    dsu = _DSU(active)
    # Rule G: directly-connected grouping equipment shares design intent
    for a in active:
        if a not in grouping:
            continue
        for b in neighbors[a]:
            if b in grouping and b not in isolated:
                dsu.union(a, b)
    # Rule M: machine trains (verified anti-parallel pairs) stay together
    train_members: set[str] = set()
    for a in sorted(breaks):
        if a in isolated:
            continue
        for b, flow in neighbors[a].items():
            if b in breaks and flow == "both":
                dsu.union(a, b)
                train_members.update((a, b))

    def component_has_grouping(root) -> bool:
        return any(dsu.find(t) == root for t in grouping if t not in isolated)

    # Rules P/X: attach each break cluster on its verified suction side,
    # iterated so serial machines chain onto the section their feed joined
    placement_notes: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for m in sorted(breaks):
            if m in isolated or component_has_grouping(dsu.find(m)):
                continue
            ups = [o for o, flow in sorted(neighbors[m].items())
                   if flow == "in" and o not in isolated
                   and nodes[o]["equipment_type"] not in _UNIT_BOUNDARY_TYPES
                   and dsu.find(o) != dsu.find(m)
                   and component_has_grouping(dsu.find(o))]
            if not ups:
                continue
            kind = _BREAK_KIND[nodes[m]["equipment_type"]]
            note = (f"{m}: {kind} — attached on its verified suction side "
                    f"(fed from {ups[0]}); its discharge starts the next "
                    f"node")
            if len({dsu.find(o) for o in ups}) > 1:
                note += (f"; AMBIGUOUS — multiple verified suction-side "
                         f"sections ({', '.join(ups)}), facilitator must "
                         f"confirm")
            placement_notes[m] = note
            dsu.union(ups[0], m)
            changed = True

    components: dict[str, set[str]] = defaultdict(set)
    for t in active:
        components[dsu.find(t)].add(t)

    proposals = []
    for members in sorted(components.values(), key=min):
        rationale = []
        n_group = sum(1 for m in members if m in grouping)
        if n_group > 1:
            rationale.append(f"{n_group} directly-connected vessels/tanks "
                             f"grouped: shared design intent, no "
                             f"design-intent change between them")
        trains = sorted(m for m in members if m in train_members)
        if trains:
            rationale.append(f"machine train kept together (verified "
                             f"anti-parallel connections): "
                             f"{', '.join(trains)}")
        for m in sorted(members):
            if m in placement_notes:
                rationale.append(placement_notes[m])
            elif m in breaks and n_group == 0:
                kind = _BREAK_KIND[nodes[m]["equipment_type"]]
                rationale.append(
                    f"{m}: {kind} with no verified suction-side neighbor — "
                    f"standalone node; placement needs facilitator "
                    f"confirmation")
        if not rationale:
            rationale.append("single equipment item, no adjacent section "
                             "shares its design intent")
        proposals.append({
            "members": sorted(members),
            "description": _describe(members, nodes),
            "rationale": rationale,
            "boundaries": _boundaries_for(members, nodes, neighbors),
            "status": "proposed",
        })

    for i, p in enumerate(proposals, 1):
        p["node_id"] = f"{node_prefix}-{i:03d}"

    kinds = [b["kind"] for p in proposals for b in p["boundaries"]]
    return {
        "nodes": proposals,
        "unassigned": unassigned,
        "redefinitions": [],
        "note": _PROPOSAL_NOTE,
        "stats": {
            "proposed_nodes": len(proposals),
            "equipment_assigned": sum(len(p["members"]) for p in proposals),
            "unassigned": len(unassigned),
            "standalone_break_nodes": sum(
                1 for p in proposals
                if all(nodes[m]["equipment_type"] in _BREAK_KIND
                       for m in p["members"])),
            "pressure_break_boundaries": kinds.count("pressure break"),
            "phase_change_boundaries": kinds.count("phase change"),
            "unit_boundaries": kinds.count("unit boundary"),
        },
    }


# -- facilitator redefinition (FR-PML-2, second half) -----------------------

def _rebuild_node(node: dict, members: list[str], nodes: dict,
                  neighbors: dict, reason: str) -> None:
    node["members"] = sorted(members)
    node["description"] = _describe(set(members), nodes)
    node["boundaries"] = _boundaries_for(set(members), nodes, neighbors)
    node["status"] = "redefined"
    node["rationale"].append(f"facilitator: {reason}")


def merge_nodes(graph: dict, proposal: dict, node_ids: list[str],
                reason: str = "facilitator merge") -> dict:
    """Merge two or more proposed nodes into the first id given. Returns a
    new proposal; the input is not mutated."""
    if len(node_ids) < 2:
        raise ValueError("merge_nodes needs at least two node ids")
    new = copy.deepcopy(proposal)
    by_id = {n["node_id"]: n for n in new["nodes"]}
    missing = [i for i in node_ids if i not in by_id]
    if missing:
        raise KeyError(f"unknown node ids: {missing}")
    nodes, neighbors = _folded_neighbors(graph)
    keep = by_id[node_ids[0]]
    members = list(keep["members"])
    for nid in node_ids[1:]:
        members += by_id[nid]["members"]
        new["nodes"].remove(by_id[nid])
    _rebuild_node(keep, members, nodes, neighbors, reason)
    new["redefinitions"].append(
        {"op": "merge", "node_ids": list(node_ids), "into": node_ids[0],
         "reason": reason})
    return new


def move_member(graph: dict, proposal: dict, tag: str, to_node_id: str,
                reason: str = "facilitator move") -> dict:
    """Move one equipment tag into another proposed node (drawing the
    boundary elsewhere). Returns a new proposal; empty source nodes are
    dropped."""
    new = copy.deepcopy(proposal)
    by_id = {n["node_id"]: n for n in new["nodes"]}
    if to_node_id not in by_id:
        raise KeyError(f"unknown node id: {to_node_id}")
    source = next((n for n in new["nodes"] if tag in n["members"]), None)
    if source is None:
        raise KeyError(f"tag {tag!r} is not a member of any proposed node")
    if source["node_id"] == to_node_id:
        return new
    nodes, neighbors = _folded_neighbors(graph)
    source_members = [m for m in source["members"] if m != tag]
    if source_members:
        _rebuild_node(source, source_members, nodes, neighbors,
                      f"{reason} ({tag} moved out)")
    else:
        new["nodes"].remove(source)
    target = by_id[to_node_id]
    _rebuild_node(target, target["members"] + [tag], nodes, neighbors,
                  f"{reason} ({tag} moved in)")
    new["redefinitions"].append(
        {"op": "move", "tag": tag, "from": source["node_id"],
         "to": to_node_id, "reason": reason})
    return new
