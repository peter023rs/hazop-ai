"""Tests for the stage 1 -> stage 3 plant-graph adapter."""

import unittest
from pathlib import Path

from hazop.s2_pml import (build_equipment_graph, equipment_type_for_tag,
                         load_plant_model)

REPO_ROOT = Path(__file__).resolve().parents[2]
L1_MODEL = REPO_ROOT / "data" / "l1_output" / "plant_model_dexpi.json"


def tiny_model():
    """Two vessels joined via a check valve through junction geometry:
    V-001.nozzle -> j1 -> check valve -> j2 -> V-002.nozzle, plus an
    instrument hanging off j1 and a second drawing view of V-001 (tag
    merge). Segment s1 carries a flow direction (nozzle -> j1); s4 carries
    one drawn the other way round (n2 -> j2 as "to_from")."""
    return {"conceptualModel": {
        "Equipment": [
            {"id": "e1", "sheet": 4, "tagName": "2401-V-001", "shape": "capsule"},
            {"id": "e1b", "sheet": 5, "tagName": "2401-V-001", "shape": "capsule"},
            {"id": "e2", "sheet": 4, "tagName": "2401-V-002", "shape": "capsule"},
        ],
        "Nozzle": [
            {"id": "n1", "sheet": 4, "equipment": "e1"},
            {"id": "n2", "sheet": 4, "equipment": "e2"},
        ],
        "PipingComponent": [
            {"id": "pc1", "sheet": 4, "componentClass": "CheckValve"},
        ],
        "ProcessInstrumentationFunction": [
            {"id": "i1", "sheet": 4, "tagName": "2401-PT-001"},
        ],
        "PipeOffPageConnector": [],
        "PipingNode": [
            {"id": "j1", "sheet": 4, "nodeType": "junction"},
            {"id": "j2", "sheet": 4, "nodeType": "junction"},
        ],
        "PipingNetworkSegment": [
            {"id": "s1", "from": "n1", "to": "j1",
             "lineNumber": ["50-IA-001"],
             "flowDirection": "from_to", "flowDirectionSource": "arrow"},
            {"id": "s2", "from": "j1", "to": "pc1"},
            {"id": "s3", "from": "pc1", "to": "j2"},
            {"id": "s4", "from": "n2", "to": "j2",
             "flowDirection": "to_from", "flowDirectionSource": "propagated"},
            {"id": "s5", "from": "j1", "to": "i1"},
        ],
    }}


class TestAdapter(unittest.TestCase):
    def setUp(self):
        self.graph = build_equipment_graph(tiny_model())
        self.edges = {(e["source"], e["target"]) for e in self.graph["edges"]}

    def test_tag_merge_across_sheets(self):
        v1 = [n for n in self.graph["nodes"] if n["tag"] == "2401-V-001"]
        self.assertEqual(len(v1), 1)
        self.assertEqual(sorted(v1[0]["attributes"]["sheets"]), [4, 5])

    def _find(self, a, b):
        for e in self.graph["edges"]:
            if {e["source"], e["target"]} == {a, b}:
                return e

    def test_contraction_stops_at_terminals(self):
        # vessel connects to the valve, NOT through it to the other vessel
        def has(a, b):
            return (a, b) in self.edges or (b, a) in self.edges
        self.assertTrue(has("2401-V-001", "XV-pc1"))
        self.assertTrue(has("XV-pc1", "2401-V-002"))
        self.assertFalse(has("2401-V-001", "2401-V-002"))

    def test_instrument_attached_and_line_number_carried(self):
        self.assertIsNotNone(self._find("2401-V-001", "2401-PT-001"))
        e = self._find("2401-V-001", "XV-pc1")
        self.assertIn("50-IA-001", e["attributes"]["line_numbers"])

    def test_flow_direction_orients_connections(self):
        # s1 (arrow) directs V-001 -> valve; s4 ("to_from") directs
        # valve -> V-002
        e1 = self._find("2401-V-001", "XV-pc1")
        self.assertEqual(e1["attributes"]["direction"], "known")
        self.assertEqual((e1["source"], e1["target"]),
                         ("2401-V-001", "XV-pc1"))
        self.assertIn("arrow", e1["attributes"]["direction_sources"])
        e2 = self._find("XV-pc1", "2401-V-002")
        self.assertEqual(e2["attributes"]["direction"], "known")
        self.assertEqual((e2["source"], e2["target"]),
                         ("XV-pc1", "2401-V-002"))

    def test_check_valve_class_mapped(self):
        cv = next(n for n in self.graph["nodes"] if n["tag"] == "XV-pc1")
        self.assertEqual(cv["equipment_type"], "check_valve")

    def test_equipment_typing_from_tag(self):
        self.assertEqual(equipment_type_for_tag("2401-K-001A"), "compressor")
        self.assertEqual(equipment_type_for_tag("2401-V-002"), "vessel")
        self.assertEqual(equipment_type_for_tag(""), "vessel")


class TestParallelPathDirections(unittest.TestCase):
    """Distinct parallel routes must not pool their votes into a fake
    conflict (the 2401 compressor/intercooler and tee-fed valve cases)."""

    @staticmethod
    def _model(segments, nodes):
        return {"conceptualModel": {
            "Equipment": [
                {"id": "e1", "sheet": 4, "tagName": "2401-K-001A",
                 "shape": "capsule"},
                {"id": "e2", "sheet": 4, "shape": "circle"},
            ],
            "Nozzle": [
                {"id": "n1", "sheet": 4, "equipment": "e1"},
                {"id": "n2", "sheet": 4, "equipment": "e1"},
            ],
            "PipingComponent": [
                {"id": "pc1", "sheet": 4, "componentClass": "GateValve"},
                {"id": "pc2", "sheet": 4, "componentClass": "GateValve"},
            ],
            "ProcessInstrumentationFunction": [],
            "PipeOffPageConnector": [],
            "PipingNode": nodes,
            "PipingNetworkSegment": segments,
        }}

    def test_anti_parallel_paths_become_two_directed_edges(self):
        # discharge n1 -> cooler e2, return e2 -> n2: same terminal pair,
        # two real pipes with opposite (individually consistent) directions
        graph = build_equipment_graph(self._model(
            segments=[
                {"id": "s1", "from": "n1", "to": "j1",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
                {"id": "s2", "from": "j1", "to": "e2"},
                {"id": "s3", "from": "e2", "to": "j2"},
                {"id": "s4", "from": "j2", "to": "n2",
                 "flowDirection": "from_to",
                 "flowDirectionSource": "propagated"},
            ],
            nodes=[{"id": "j1", "sheet": 4, "nodeType": "junction"},
                   {"id": "j2", "sheet": 4, "nodeType": "junction"}]))
        pair = [e for e in graph["edges"]
                if {e["source"], e["target"]} == {"2401-K-001A", "EQ-e2"}]
        self.assertEqual(len(pair), 2)
        self.assertEqual({(e["source"], e["target"]) for e in pair},
                         {("2401-K-001A", "EQ-e2"),
                          ("EQ-e2", "2401-K-001A")})
        for e in pair:
            self.assertEqual(e["attributes"]["direction"], "known")
        self.assertEqual(graph["stats"]["direction_conflicts"], 0)
        self.assertEqual(graph["stats"]["anti_parallel_pairs"], 1)

    def test_tee_fed_pair_has_no_through_flow(self):
        # flow enters at j1 from a third pipe and feeds both valves; the
        # valve-valve path flips sign at j1 -> no through-flow direction
        graph = build_equipment_graph(self._model(
            segments=[
                {"id": "s1", "from": "j1", "to": "pc1",
                 "flowDirection": "from_to",
                 "flowDirectionSource": "propagated"},
                {"id": "s2", "from": "j1", "to": "pc2",
                 "flowDirection": "from_to",
                 "flowDirectionSource": "propagated"},
                {"id": "s3", "from": "n1", "to": "j1",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
            ],
            nodes=[{"id": "j1", "sheet": 4, "nodeType": "junction"}]))
        e = next(e for e in graph["edges"]
                 if {e["source"], e["target"]} == {"XV-pc1", "XV-pc2"})
        self.assertEqual(e["attributes"]["direction"], "unknown")
        self.assertIn("direction_note", e["attributes"])
        self.assertEqual(graph["stats"]["direction_conflicts"], 0)
        self.assertEqual(graph["stats"]["no_through_flow_pairs"], 1)


class TestPassthroughPropagation(unittest.TestCase):
    """Equipment-level pass-through pass: a 2-connection valve or off-page
    connector forces its unknown side once the other side is verified."""

    @staticmethod
    def _model(components, opcs, instruments, segments):
        return {"conceptualModel": {
            "Equipment": [
                {"id": "e1", "sheet": 4, "tagName": "2401-V-001",
                 "shape": "capsule"},
                {"id": "e2", "sheet": 5, "tagName": "2401-V-002",
                 "shape": "capsule"},
            ],
            "Nozzle": [
                {"id": "n1", "sheet": 4, "equipment": "e1"},
                {"id": "n2", "sheet": 5, "equipment": "e2"},
            ],
            "PipingComponent": components,
            "ProcessInstrumentationFunction": instruments,
            "PipeOffPageConnector": opcs,
            "PipingNode": [
                {"id": "j1", "sheet": 4, "nodeType": "junction"},
                {"id": "j2", "sheet": 4, "nodeType": "junction"},
            ],
            "PipingNetworkSegment": segments,
        }}

    def _find(self, graph, a, b):
        for e in graph["edges"]:
            if {e["source"], e["target"]} == {a, b}:
                return e

    def test_valve_second_side_forced(self):
        # arrow directs V-001 -> valve; the valve's other side has no votes
        # anywhere, but a 2-connection valve stores no fluid
        graph = build_equipment_graph(self._model(
            components=[{"id": "pc1", "sheet": 4,
                         "componentClass": "GateValve"}],
            opcs=[], instruments=[],
            segments=[
                {"id": "s1", "from": "n1", "to": "j1",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
                {"id": "s2", "from": "j1", "to": "pc1"},
                {"id": "s3", "from": "pc1", "to": "j2"},
                {"id": "s4", "from": "j2", "to": "n2"},
            ]))
        e = self._find(graph, "XV-pc1", "2401-V-002")
        self.assertEqual(e["attributes"]["direction"], "known")
        self.assertEqual((e["source"], e["target"]),
                         ("XV-pc1", "2401-V-002"))
        self.assertEqual(e["attributes"]["direction_sources"],
                         ["l2-passthrough"])
        self.assertEqual(graph["stats"]["l2_passthrough_forced"], 1)

    def test_instrument_tap_does_not_block_forcing(self):
        # a pressure tap hanging off the valve's downstream leg must not
        # count as a third connection
        graph = build_equipment_graph(self._model(
            components=[{"id": "pc1", "sheet": 4,
                         "componentClass": "GateValve"}],
            opcs=[],
            instruments=[{"id": "i1", "sheet": 4, "tagName": "2401-PT-001"}],
            segments=[
                {"id": "s1", "from": "n1", "to": "j1",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
                {"id": "s2", "from": "j1", "to": "pc1"},
                {"id": "s3", "from": "pc1", "to": "j2"},
                {"id": "s4", "from": "j2", "to": "n2"},
                {"id": "s5", "from": "j2", "to": "i1"},
            ]))
        e = self._find(graph, "XV-pc1", "2401-V-002")
        self.assertEqual(e["attributes"]["direction"], "known")
        self.assertEqual((e["source"], e["target"]),
                         ("XV-pc1", "2401-V-002"))

    def test_offpage_connector_bridges_sheets(self):
        # sheet 4 flows into the connector; the sheet 5 side inherits it
        graph = build_equipment_graph(self._model(
            components=[],
            opcs=[{"id": "opc1", "sheet": 4, "labels": ["A1"]}],
            instruments=[],
            segments=[
                {"id": "s1", "from": "n1", "to": "opc1",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
                {"id": "s2", "from": "opc1", "to": "n2"},
            ]))
        e = self._find(graph, "OPC-A1", "2401-V-002")
        self.assertEqual(e["attributes"]["direction"], "known")
        self.assertEqual((e["source"], e["target"]),
                         ("OPC-A1", "2401-V-002"))
        self.assertEqual(e["attributes"]["direction_sources"],
                         ["l2-passthrough"])

    def test_vessel_never_forced(self):
        # vessels may have unextracted connections: 2-connection vessels
        # are NOT pass-through, so nothing may be forced through EQ nodes
        graph = build_equipment_graph(self._model(
            components=[], opcs=[], instruments=[],
            segments=[
                {"id": "s1", "from": "n1", "to": "n2",
                 "flowDirection": "from_to", "flowDirectionSource": "arrow"},
            ]))
        self.assertEqual(graph["stats"]["l2_passthrough_forced"], 0)


@unittest.skipUnless(L1_MODEL.exists(), "stage 1 output not present")
class TestRealPlantModel(unittest.TestCase):
    def test_real_model_contracts_cleanly(self):
        graph = build_equipment_graph(load_plant_model(L1_MODEL))
        self.assertGreater(graph["stats"]["terminals"], 300)
        self.assertGreater(graph["stats"]["connections"], 400)
        tags = [n["tag"] for n in graph["nodes"]]
        self.assertEqual(len(tags), len(set(tags)), "tags must be unique")
        self.assertIn("2401-K-001A", tags)
        node_tags = set(tags)
        for e in graph["edges"]:
            self.assertIn(e["source"], node_tags)
            self.assertIn(e["target"], node_tags)

    def test_real_model_has_directions_and_valve_classes(self):
        graph = build_equipment_graph(load_plant_model(L1_MODEL))
        types = {n["equipment_type"] for n in graph["nodes"]}
        self.assertIn("relief_valve", types)
        self.assertIn("check_valve", types)
        self.assertGreater(graph["stats"]["directed_connections"], 50)
        # conflicts must be flagged, never silently oriented
        for e in graph["edges"]:
            self.assertIn(e["attributes"]["direction"],
                          ("known", "unknown", "conflict"))


if __name__ == "__main__":
    unittest.main()
