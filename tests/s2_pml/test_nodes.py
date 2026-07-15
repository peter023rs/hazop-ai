"""HAZOP node boundary proposal (FR-PML-2)."""

import unittest
from pathlib import Path

from hazop.s2_pml import (build_equipment_graph,
                                            load_plant_model, merge_nodes,
                                            move_member, propose_nodes)

REPO_ROOT = Path(__file__).resolve().parents[2]
L1_MODEL = REPO_ROOT / "data" / "l1_output" / "plant_model_dexpi.json"


def _node(tag, etype, name=None):
    return {"tag": tag, "equipment_type": etype, "name": name or tag,
            "attributes": {"sheets": [4]}, "detection_confidence": 1.0}


def _edge(source, target, direction="unknown", lines=()):
    attrs = {"line_numbers": list(lines), "direction": direction}
    if direction == "known":
        attrs["direction_sources"] = ["arrow"]
    return {"source": source, "target": target,
            "line_tag": lines[0] if lines else "", "attributes": attrs}


def _graph(nodes, edges):
    return {"nodes": nodes, "edges": edges, "stats": {}}


def _members(proposal):
    return {p["node_id"]: p["members"] for p in proposal["nodes"]}


def _node_of(proposal, tag):
    return next(p for p in proposal["nodes"] if tag in p["members"])


class TestProposeNodes(unittest.TestCase):
    def test_connected_vessels_group_through_valves(self):
        # Rule G: V-001 -valve- V-002 share one design intent
        g = _graph(
            [_node("V-001", "vessel"), _node("XV-1", "valve"),
             _node("V-002", "vessel")],
            [_edge("V-001", "XV-1"), _edge("XV-1", "V-002")])
        proposal = propose_nodes(g)
        self.assertEqual(len(proposal["nodes"]), 1)
        self.assertEqual(proposal["nodes"][0]["members"],
                         ["V-001", "V-002"])
        self.assertEqual(proposal["nodes"][0]["status"], "proposed")
        # valves are never members
        self.assertNotIn("XV-1", proposal["nodes"][0]["members"])

    def test_pressure_break_splits_and_machine_joins_suction_side(self):
        # Rules G + P: V-001 -> K-001 -> V-002 with verified directions;
        # the compressor joins its suction side, discharge starts a new node
        g = _graph(
            [_node("V-001", "vessel"), _node("K-001", "compressor"),
             _node("V-002", "vessel")],
            [_edge("V-001", "K-001", "known"),
             _edge("K-001", "V-002", "known")])
        proposal = propose_nodes(g)
        self.assertEqual(len(proposal["nodes"]), 2)
        suction = _node_of(proposal, "V-001")
        self.assertEqual(suction["members"], ["K-001", "V-001"])
        self.assertEqual(_node_of(proposal, "V-002")["members"], ["V-002"])
        self.assertTrue(any("pressure break" in r
                            for r in suction["rationale"]))
        # the discharge node sees the machine as a pressure-break boundary
        kinds = {b["kind"] for b in _node_of(proposal, "V-002")["boundaries"]}
        self.assertIn("pressure break", kinds)

    def test_unverified_machine_is_standalone_and_flagged(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("P-001", "pump"),
             _node("V-002", "vessel")],
            [_edge("V-001", "P-001"), _edge("P-001", "V-002")])
        proposal = propose_nodes(g)
        pump_node = _node_of(proposal, "P-001")
        self.assertEqual(pump_node["members"], ["P-001"])
        self.assertTrue(any("facilitator" in r
                            for r in pump_node["rationale"]))

    def test_machine_train_stays_together(self):
        # Rule M: compressor + intercooler joined by a verified
        # anti-parallel pair, fed from a suction drum
        g = _graph(
            [_node("V-001", "vessel"), _node("K-001", "compressor"),
             _node("E-001", "heat_exchanger")],
            [_edge("V-001", "K-001", "known"),
             _edge("K-001", "E-001", "known"),
             _edge("E-001", "K-001", "known")])
        proposal = propose_nodes(g)
        self.assertEqual(len(proposal["nodes"]), 1)
        self.assertEqual(proposal["nodes"][0]["members"],
                         ["E-001", "K-001", "V-001"])
        self.assertTrue(any("machine train" in r
                            for r in proposal["nodes"][0]["rationale"]))

    def test_offpage_connector_is_unit_boundary_not_member(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("OPC-A1", "line"),
             _node("V-002", "vessel")],
            [_edge("V-001", "OPC-A1"), _edge("OPC-A1", "V-002")])
        proposal = propose_nodes(g)
        # the OPC never merges the two sides: two nodes, boundary entries
        self.assertEqual(len(proposal["nodes"]), 2)
        for p in proposal["nodes"]:
            self.assertNotIn("OPC-A1", p["members"])
            self.assertEqual(p["boundaries"][0]["tag"], "OPC-A1")
            self.assertEqual(p["boundaries"][0]["kind"], "unit boundary")

    def test_instruments_and_isolated_equipment_never_members(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("2401-PT-1", "instrument"),
             _node("V-999", "vessel")],
            [_edge("V-001", "2401-PT-1")])
        proposal = propose_nodes(g)
        all_members = [m for p in proposal["nodes"] for m in p["members"]]
        self.assertNotIn("2401-PT-1", all_members)
        # V-001 has only an instrument tap; V-999 nothing at all
        self.assertEqual(sorted(u["tag"] for u in proposal["unassigned"]),
                         ["V-001", "V-999"])

    def test_every_major_equipment_assigned_exactly_once(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("K-001", "compressor"),
             _node("V-002", "vessel"), _node("XV-1", "valve"),
             _node("OPC-A1", "line")],
            [_edge("V-001", "K-001", "known"),
             _edge("K-001", "XV-1", "known"),
             _edge("XV-1", "V-002", "known"),
             _edge("V-002", "OPC-A1")])
        proposal = propose_nodes(g)
        assigned = [m for p in proposal["nodes"] for m in p["members"]]
        self.assertEqual(sorted(assigned), ["K-001", "V-001", "V-002"])
        self.assertEqual(len(assigned), len(set(assigned)))
        s = proposal["stats"]
        self.assertEqual(s["equipment_assigned"], 3)
        self.assertEqual(s["unit_boundaries"], 1)


class TestFacilitatorRedefinition(unittest.TestCase):
    def setUp(self):
        self.g = _graph(
            [_node("V-001", "vessel"), _node("K-001", "compressor"),
             _node("V-002", "vessel")],
            [_edge("V-001", "K-001", "known"),
             _edge("K-001", "V-002", "known")])
        self.proposal = propose_nodes(self.g)

    def test_merge_nodes(self):
        ids = [p["node_id"] for p in self.proposal["nodes"]]
        merged = merge_nodes(self.g, self.proposal, ids,
                             reason="one compression node")
        self.assertEqual(len(merged["nodes"]), 1)
        self.assertEqual(merged["nodes"][0]["members"],
                         ["K-001", "V-001", "V-002"])
        self.assertEqual(merged["nodes"][0]["status"], "redefined")
        self.assertEqual(merged["nodes"][0]["boundaries"], [])
        self.assertEqual(merged["redefinitions"][0]["op"], "merge")
        # input proposal untouched (AR-1: proposed vs modified separable)
        self.assertEqual(len(self.proposal["nodes"]), 2)
        self.assertEqual(self.proposal["nodes"][0]["status"], "proposed")

    def test_move_member(self):
        target = _node_of(self.proposal, "V-002")["node_id"]
        moved = move_member(self.g, self.proposal, "K-001", target,
                            reason="machine belongs with discharge")
        self.assertEqual(_node_of(moved, "K-001")["node_id"], target)
        self.assertEqual(_node_of(moved, "K-001")["members"],
                         ["K-001", "V-002"])
        self.assertEqual(moved["redefinitions"][0]["op"], "move")

    def test_move_last_member_drops_source_node(self):
        target = _node_of(self.proposal, "V-002")["node_id"]
        step1 = move_member(self.g, self.proposal, "K-001", target)
        step2 = move_member(self.g, step1, "V-001", target)
        self.assertEqual(len(step2["nodes"]), 1)
        self.assertEqual(step2["nodes"][0]["members"],
                         ["K-001", "V-001", "V-002"])

    def test_unknown_ids_raise(self):
        with self.assertRaises(KeyError):
            merge_nodes(self.g, self.proposal, ["PN-001", "PN-999"])
        with self.assertRaises(KeyError):
            move_member(self.g, self.proposal, "GHOST-1", "PN-001")


@unittest.skipUnless(L1_MODEL.exists(), "stage 1 output not present")
class TestRealDrawing(unittest.TestCase):
    def test_proposal_covers_the_2401_graph(self):
        graph = build_equipment_graph(load_plant_model(L1_MODEL))
        proposal = propose_nodes(graph)
        majors = [n for n in graph["nodes"]
                  if n["equipment_type"] not in
                  ("valve", "check_valve", "relief_valve", "instrument",
                   "line")]
        assigned = [m for p in proposal["nodes"] for m in p["members"]]
        unassigned = [u["tag"] for u in proposal["unassigned"]]
        # exact partition: every major item exactly once
        self.assertEqual(sorted(assigned + unassigned),
                         sorted(n["tag"] for n in majors))
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertGreater(len(proposal["nodes"]), 1)
        # members feed the condensed view unchanged
        from hazop.s2_pml import condensed_node_view
        biggest = max(proposal["nodes"], key=lambda p: len(p["members"]))
        view = condensed_node_view(graph, biggest["members"])
        self.assertEqual(len(view["members"]), len(biggest["members"]))


if __name__ == "__main__":
    unittest.main()
