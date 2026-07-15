"""
condense.py — node-scoped condensed context views (the generation-layer
feed, per the handoff doc and the Delft "Talking like P&IDs" condensing:
their pruning cut a full drawing from ~67k to ~9k tokens; same move here).

The deterministic reasoner gets the FULL equipment-level graph. The LLM
never should: for one study node it needs the members, their immediate
process neighborhood, and what stands in-line between them — nothing else.
This module builds exactly that from the adapter's equipment-level graph
(`build_equipment_graph` output; the Neo4j graph holds the same data, so a
Cypher variant of this view is a straight translation).

Condensing moves, in order:

  1. keep the member equipment + process neighbors within `hops`
     equipment-to-equipment steps;
  2. fold in-line components (valves / check valves / relief valves)
     into the connection that carries them — a chain of five gate valves
     between two vessels becomes one connection with `via=[...]`;
  3. attach instrument taps to the equipment they hang off as a name
     list, not as graph nodes;
  4. drop everything else (coordinates, sheets, detection metadata).

Direction honesty is preserved per connection: "known" only when every
folded hop carries a verified direction that agrees with the walk (the
connection is then emitted in flow order); anything less reports
verified_hops out of total so the generation layer can say "unverified"
instead of guessing — same discipline as the L3 topology reasoner.
"""

from __future__ import annotations

import json
from collections import defaultdict

# in-line components folded into connections (view step 2)
_INLINE_TYPES = {"valve", "check_valve", "relief_valve"}
# terminals that mark the edge of the drawing, not equipment
_BOUNDARY_TYPES = {"line"}

_INLINE_LABEL = {"valve": "valve", "check_valve": "check valve",
                 "relief_valve": "relief valve"}


def _adjacency(graph: dict) -> dict[str, list[dict]]:
    adj = defaultdict(list)
    for e in graph["edges"]:
        adj[e["source"]].append(e)
        adj[e["target"]].append(e)
    return adj


def _walk_to_equipment(start_tag: str, first_edge: dict, nodes: dict,
                       adj: dict) -> list[tuple[list[dict], str]]:
    """From `start_tag` across `first_edge`, walk through in-line
    component nodes until hitting an equipment/boundary terminal (or a
    dead end). Returns [(edge_chain, end_tag)] — one per branch, since
    valve trees may fan out."""
    results: list[tuple[list[dict], str]] = []
    stack = [(first_edge, start_tag, [first_edge])]
    seen_edges = {id(first_edge)}
    while stack:
        edge, came_from, chain = stack.pop()
        here = edge["target"] if edge["source"] == came_from else edge["source"]
        if nodes[here]["equipment_type"] not in _INLINE_TYPES:
            results.append((chain, here))
            continue
        onward = [e for e in adj[here] if id(e) not in seen_edges
                  and nodes[e["target"] if e["source"] == here
                            else e["source"]]["equipment_type"]
                  != "instrument"]
        if not onward:
            results.append((chain, here))  # chain ends on the component
            continue
        for e in onward:
            seen_edges.add(id(e))
            stack.append((e, here, chain + [e]))
    return results


def _chain_direction(chain: list[dict], start_tag: str) -> tuple[str, int]:
    """Classify a folded chain's flow direction relative to walking
    start->end. Returns (one of "forward"/"backward"/"unknown",
    verified_hop_count). "forward"/"backward" require every hop known and
    agreeing; anything mixed or partial stays "unknown"."""
    here = start_tag
    votes: set[str] = set()
    verified = 0
    for e in chain:
        nxt = e["target"] if e["source"] == here else e["source"]
        if e["attributes"]["direction"] == "known":
            verified += 1
            votes.add("forward" if e["source"] == here else "backward")
        here = nxt
    if verified == len(chain) and len(votes) == 1:
        return votes.pop(), verified
    return "unknown", verified


def condensed_node_view(graph: dict, member_tags: list[str],
                        hops: int = 1) -> dict:
    """Build the condensed view for one study node.

    graph: `build_equipment_graph` output. member_tags: the node's
    equipment. hops: how many equipment-to-equipment steps to include
    beyond the members (1 = immediate process neighbors).
    """
    nodes = {n["tag"]: n for n in graph["nodes"]}
    adj = _adjacency(graph)
    missing = [t for t in member_tags if t not in nodes]
    if missing:
        raise KeyError(f"member tags not in graph: {missing}")

    view_equipment: dict[str, dict] = {}
    connections: list[dict] = []
    seen_conn: set[tuple] = set()

    def add_equipment(tag: str, role: str):
        if tag in view_equipment:
            # members never get demoted to neighbors
            if role == "member":
                view_equipment[tag]["role"] = role
            return
        n = nodes[tag]
        instruments = sorted(
            nodes[other]["tag"]
            for e in adj[tag]
            for other in [e["target"] if e["source"] == tag else e["source"]]
            if nodes[other]["equipment_type"] == "instrument")
        view_equipment[tag] = {
            "tag": tag,
            "type": n["equipment_type"],
            "name": n["name"],
            "role": role,
            "instruments": instruments,
        }

    frontier = list(dict.fromkeys(member_tags))
    for tag in frontier:
        add_equipment(tag, "member")

    for _ in range(hops):
        next_frontier: list[str] = []
        for tag in frontier:
            for first_edge in adj[tag]:
                other = (first_edge["target"]
                         if first_edge["source"] == tag
                         else first_edge["source"])
                if nodes[other]["equipment_type"] == "instrument":
                    continue
                for chain, end in _walk_to_equipment(tag, first_edge,
                                                     nodes, adj):
                    if end == tag:
                        continue
                    inline = []
                    here = tag
                    for e in chain:
                        nxt = (e["target"] if e["source"] == here
                               else e["source"])
                        if (nxt != end and nodes[nxt]["equipment_type"]
                                in _INLINE_TYPES):
                            inline.append(nxt)
                        here = nxt

                    direction, verified = _chain_direction(chain, tag)
                    src, dst = ((tag, end) if direction != "backward"
                                else (end, tag))
                    # directed connections dedupe per flow order, so a
                    # verified anti-parallel pair (discharge out + cooled
                    # return back) stays two connections; undirected ones
                    # dedupe pair-wise
                    key = (((src, dst) if direction != "unknown"
                            else (min(tag, end), max(tag, end))),
                           tuple(sorted(inline)))
                    if key in seen_conn:
                        continue
                    seen_conn.add(key)
                    lines = sorted({ln for e in chain
                                    for ln in e["attributes"]
                                    .get("line_numbers", [])})
                    connections.append({
                        "from": src,
                        "to": dst,
                        "via": [{"tag": v,
                                 "type": nodes[v]["equipment_type"]}
                                for v in inline],
                        "direction": ("known"
                                      if direction != "unknown"
                                      else "unknown"),
                        "verified_hops": f"{verified}/{len(chain)}",
                        "lines": lines,
                    })

                    end_type = nodes[end]["equipment_type"]
                    role = ("boundary" if end_type in _BOUNDARY_TYPES
                            or end_type in _INLINE_TYPES else "neighbor")
                    add_equipment(end, role)
                    if role == "neighbor" and end not in next_frontier:
                        next_frontier.append(end)
        frontier = [t for t in next_frontier
                    if view_equipment[t]["role"] == "neighbor"]

    return {
        "members": [v for v in view_equipment.values()
                    if v["role"] == "member"],
        "neighbors": [v for v in view_equipment.values()
                      if v["role"] == "neighbor"],
        "boundary": [v for v in view_equipment.values()
                     if v["role"] == "boundary"],
        "connections": connections,
    }


def render_text(view: dict) -> str:
    """Compact prompt block — the only topology text the generation layer
    should ever see for a node."""
    out = []
    for section, label in (("members", "STUDY NODE MEMBERS"),
                           ("neighbors", "PROCESS NEIGHBORS"),
                           ("boundary", "VIEW BOUNDARY")):
        items = view[section]
        if not items:
            continue
        out.append(f"{label}:")
        for v in items:
            inst = (f"  [instruments: {', '.join(v['instruments'])}]"
                    if v["instruments"] else "")
            out.append(f"  {v['tag']} ({v['type']}){inst}")
    out.append("CONNECTIONS (direction verified only where stated):")
    for c in view["connections"]:
        via = (" via " + ", ".join(f"{_INLINE_LABEL[v['type']]} {v['tag']}"
                                   for v in c["via"]) if c["via"] else "")
        if c["direction"] == "known":
            arrow, note = "->", "direction verified"
        else:
            arrow, note = "--", (f"direction NOT verified "
                                 f"({c['verified_hops']} hops)")
        lines = f"; line {', '.join(c['lines'])}" if c["lines"] else ""
        out.append(f"  {c['from']} {arrow} {c['to']}{via} ({note}{lines})")
    return "\n".join(out)


def token_estimate(obj) -> int:
    """Crude ~4-chars-per-token estimate, good enough to show the
    condensing ratio."""
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return max(1, len(text) // 4)
