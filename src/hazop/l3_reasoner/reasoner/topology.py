"""
topology.py — Deterministic graph reasoning over the process topology.

Implements the "topology reasoner" (Fable MDL-3): flow tracing, isolation
boundary identification, flow-path enumeration, and consequence-propagation
paths. These have exact algorithmic solutions, so they are pure graph
algorithms (NetworkX) — no ML, per MDL-3.

Also provides the tag-grounding check used for hallucination control
(FR-ARE-9 / MDL-10): any tag the LLM references must exist in the topology
graph, else the output is rejected.

Direction confidence: stage 1 only verifies flow direction where the drawing
carries evidence (arrows, check valves, connector text, conservation at
junctions); the stage 2 adapter marks every connection with
attributes["direction"] = "known" / "unknown" / "conflict". Edges without
the attribute (hand-authored graphs) count as known. An unverified edge is
traversable in BOTH directions here — its drawing order is arbitrary — but
anything reached across one is reported as unverified, and the safeguard
detections (relief path, check valve) only make their high-confidence claim
over fully verified paths.

Depends only on the schema.TopologyGraph contract, so it runs entirely on
mock graphs today.
"""

from __future__ import annotations

import warnings

import networkx as nx

from .schema import TopologyGraph, EquipmentType

# Equipment that can block a relief flow path: anything closable, plus
# machines a relief stream cannot be credited through.
_RELIEF_BLOCKING = {
    EquipmentType.PUMP,
    EquipmentType.COMPRESSOR,
    EquipmentType.VALVE,
    EquipmentType.CONTROL_VALVE,
}


class TopologyReasoner:
    def __init__(self, topology: TopologyGraph):
        self.topology = topology
        self.g = self._build_graph(topology)

    @staticmethod
    def _build_graph(topology: TopologyGraph) -> nx.DiGraph:
        g = nx.DiGraph()
        for node in topology.nodes:
            g.add_node(node.tag, data=node)
        known = {n.tag for n in topology.nodes}
        dangling = sorted(
            {t for e in topology.edges for t in (e.source, e.target)} - known)
        if dangling:
            # Real stage-1 output will contain these (OCR errors, missed
            # symbols). Keep the edges — input is read-only — but surface it.
            warnings.warn(
                f"Topology edges reference tags with no equipment node: "
                f"{dangling}", stacklevel=3)
        for edge in topology.edges:
            verified = (edge.attributes or {}).get("direction",
                                                   "known") == "known"
            g.add_edge(edge.source, edge.target, data=edge, verified=verified)
            if not verified and not g.has_edge(edge.target, edge.source):
                # drawing order is arbitrary on an unverified edge: the
                # physical flow could run either way
                g.add_edge(edge.target, edge.source, data=edge,
                           verified=False)
        return g

    # -- basic tracing -----------------------------------------------------

    def _reach(self, tag: str, forward: bool) -> dict[str, bool]:
        """Reachability with direction confidence: tag -> True when some
        fully verified path reaches it, False when every path crosses at
        least one unverified edge."""
        if tag not in self.g:
            return {}
        step = self.g.successors if forward else self.g.predecessors

        def edge_verified(a, b):
            return self.g.edges[(a, b) if forward else (b, a)]["verified"]

        status: dict[str, bool] = {tag: True}
        frontier = [tag]
        while frontier:
            cur = frontier.pop()
            for nxt in step(cur):
                ok = status[cur] and edge_verified(cur, nxt)
                # first visit, or upgrade from unverified to verified
                if nxt != tag and status.get(nxt) is not True and (
                        nxt not in status or ok):
                    status[nxt] = ok
                    frontier.append(nxt)
        status.pop(tag, None)
        return status

    def downstream(self, tag: str, verified_only: bool = False) -> list[str]:
        """All equipment reachable by following flow from `tag`."""
        return sorted(t for t, ok in self._reach(tag, True).items()
                      if ok or not verified_only)

    def upstream(self, tag: str, verified_only: bool = False) -> list[str]:
        """All equipment that can flow into `tag`."""
        return sorted(t for t, ok in self._reach(tag, False).items()
                      if ok or not verified_only)

    def downstream_detail(self, tag: str) -> dict[str, bool]:
        """Downstream tags -> True (verified path) / False (unverified)."""
        return self._reach(tag, True)

    def upstream_detail(self, tag: str) -> dict[str, bool]:
        """Upstream tags -> True (verified path) / False (unverified)."""
        return self._reach(tag, False)

    def immediate_downstream(self, tag: str) -> list[str]:
        return list(self.g.successors(tag)) if tag in self.g else []

    def immediate_upstream(self, tag: str) -> list[str]:
        return list(self.g.predecessors(tag)) if tag in self.g else []

    # -- flow-path enumeration --------------------------------------------

    def flow_paths(self, source: str, target: str, cutoff: int = 12,
                   verified_only: bool = False) -> list[list[str]]:
        """All simple flow paths from source to target."""
        if source not in self.g or target not in self.g:
            return []
        paths = nx.all_simple_paths(self.g, source, target, cutoff=cutoff)
        if not verified_only:
            return list(paths)
        return [p for p in paths
                if all(self.g.edges[a, b]["verified"]
                       for a, b in zip(p, p[1:]))]

    # -- relief / protection path detection -------------------------------

    def has_relief_path(self, tag: str, verified_only: bool = True) -> bool:
        """
        True if a relief valve is reachable strictly downstream of `tag`
        WITHOUT passing through equipment that can block relief flow
        (closable valves, pumps, compressors). A PSV on the far side of a
        closable valve is not credited: the blocked-outlet case is exactly
        the scenario the relief path must survive. The node itself is
        excluded — a relief valve is not its own over-pressure protection.
        With verified_only (the default) the path must consist of verified
        flow directions — a relief claim over guessed directions is not a
        relief claim; pass False to ask whether one is merely drawn.
        """
        if tag not in self.g:
            return False
        seen = {tag}
        frontier = [t for t in self.g.successors(tag)
                    if not verified_only or self.g.edges[tag, t]["verified"]]
        while frontier:
            t = frontier.pop()
            if t in seen:
                continue
            seen.add(t)
            node = self.topology.node_by_tag(t)
            if node is None:
                continue
            if node.equipment_type == EquipmentType.RELIEF_VALVE:
                return True
            if node.equipment_type in _RELIEF_BLOCKING:
                continue
            frontier.extend(
                nxt for nxt in self.g.successors(t)
                if not verified_only or self.g.edges[t, nxt]["verified"])
        return False

    def check_valves_between(self, source: str, target: str,
                             verified_only: bool = True) -> list[str]:
        """Check valves lying on any flow path source->target (reverse-flow
        safeguards). verified_only (default) credits only paths whose flow
        directions are all verified."""
        found: set[str] = set()
        for path in self.flow_paths(source, target,
                                    verified_only=verified_only):
            for tag in path:
                node = self.topology.node_by_tag(tag)
                if node and node.equipment_type == EquipmentType.CHECK_VALVE:
                    found.add(tag)
        return sorted(found)

    # -- isolation boundary -----------------------------------------------

    def isolation_boundary(self, tag: str) -> dict[str, list[str]]:
        """
        Nearest isolating elements (valves) upstream and downstream of `tag`.
        Approximates the section that can be isolated around a piece of equipment.

        Check valves are NOT isolating: they cannot be closed on demand, they
        only block reverse flow. Positive isolation needs a closable valve.
        """
        isolating = {EquipmentType.VALVE, EquipmentType.CONTROL_VALVE}

        def nearest(direction_fn) -> list[str]:
            boundary, seen, frontier = [], set(), direction_fn(tag)
            while frontier:
                nxt = frontier.pop()
                if nxt in seen:
                    continue
                seen.add(nxt)
                node = self.topology.node_by_tag(nxt)
                if node and node.equipment_type in isolating:
                    boundary.append(nxt)
                else:
                    frontier.extend(direction_fn(nxt))
            return boundary

        return {
            "upstream_isolation": nearest(self.immediate_upstream),
            "downstream_isolation": nearest(self.immediate_downstream),
        }

    # -- grounding check (hallucination control) --------------------------

    def validate_tags(self, tags: list[str]) -> tuple[list[str], list[str]]:
        """
        Split referenced tags into (valid, invalid) against the topology.
        Used to gate LLM output: any invalid tag => that finding is rejected
        (FR-ARE-9 / MDL-10 hard gate).
        """
        known = self.topology.all_tags()
        valid = [t for t in tags if t in known]
        invalid = [t for t in tags if t not in known]
        return valid, invalid
