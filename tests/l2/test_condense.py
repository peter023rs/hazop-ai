"""Node-scoped condensed context views (generation-layer feed)."""

import unittest

from hazop.l2_knowledge.plant_graph import condensed_node_view, render_text


def _node(tag, etype, name=None):
    return {"tag": tag, "equipment_type": etype, "name": name or tag,
            "attributes": {"sheets": [4]}, "detection_confidence": 1.0}


def _edge(source, target, direction="unknown", lines=(), note=None):
    attrs = {"line_numbers": list(lines), "direction": direction}
    if direction == "known":
        attrs["direction_sources"] = ["arrow"]
    if note:
        attrs["direction_note"] = note
    return {"source": source, "target": target,
            "line_tag": lines[0] if lines else "", "attributes": attrs}


def _graph(nodes, edges):
    return {"nodes": nodes, "edges": edges, "stats": {}}


class TestCondensedNodeView(unittest.TestCase):
    def test_valve_chain_folds_into_one_connection(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("XV-1", "valve"),
             _node("XV-2", "valve"), _node("V-002", "vessel")],
            [_edge("V-001", "XV-1", "known", ["50-GA-1"]),
             _edge("XV-1", "XV-2", "known"),
             _edge("XV-2", "V-002", "known", ["50-GA-2"])])
        view = condensed_node_view(g, ["V-001"])
        self.assertEqual(len(view["connections"]), 1)
        c = view["connections"][0]
        self.assertEqual((c["from"], c["to"]), ("V-001", "V-002"))
        self.assertEqual([v["tag"] for v in c["via"]], ["XV-1", "XV-2"])
        self.assertEqual(c["direction"], "known")
        self.assertEqual(c["verified_hops"], "3/3")
        self.assertEqual(c["lines"], ["50-GA-1", "50-GA-2"])

    def test_backward_chain_emitted_in_flow_order(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("XV-1", "valve"),
             _node("V-002", "vessel")],
            [_edge("XV-1", "V-001", "known"),      # flow toward V-001
             _edge("V-002", "XV-1", "known")])     # flow from V-002
        view = condensed_node_view(g, ["V-001"])
        c = view["connections"][0]
        self.assertEqual((c["from"], c["to"]), ("V-002", "V-001"))
        self.assertEqual(c["direction"], "known")

    def test_partial_direction_stays_unknown_with_hop_count(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("XV-1", "valve"),
             _node("V-002", "vessel")],
            [_edge("V-001", "XV-1", "known"),
             _edge("XV-1", "V-002")])              # second hop unverified
        view = condensed_node_view(g, ["V-001"])
        c = view["connections"][0]
        self.assertEqual(c["direction"], "unknown")
        self.assertEqual(c["verified_hops"], "1/2")

    def test_instruments_fold_into_owner_not_nodes(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("2401-PT-1", "instrument"),
             _node("V-002", "vessel")],
            [_edge("V-001", "2401-PT-1"),
             _edge("V-001", "V-002")])
        view = condensed_node_view(g, ["V-001"])
        member = view["members"][0]
        self.assertEqual(member["instruments"], ["2401-PT-1"])
        all_tags = [v["tag"] for v in
                    view["members"] + view["neighbors"] + view["boundary"]]
        self.assertNotIn("2401-PT-1", all_tags)

    def test_offpage_connector_is_boundary(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("OPC-A1", "line")],
            [_edge("V-001", "OPC-A1")])
        view = condensed_node_view(g, ["V-001"])
        self.assertEqual([v["tag"] for v in view["boundary"]], ["OPC-A1"])
        self.assertEqual(view["neighbors"], [])

    def test_anti_parallel_pair_stays_two_connections(self):
        g = _graph(
            [_node("K-001", "compressor"), _node("E-001", "vessel")],
            [_edge("K-001", "E-001", "known"),     # discharge out
             _edge("E-001", "K-001", "known")])    # cooled return
        view = condensed_node_view(g, ["K-001"])
        self.assertEqual(len(view["connections"]), 2)
        self.assertEqual({(c["from"], c["to"]) for c in view["connections"]},
                         {("K-001", "E-001"), ("E-001", "K-001")})

    def test_hops_bound_the_view(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("V-002", "vessel"),
             _node("V-003", "vessel")],
            [_edge("V-001", "V-002"), _edge("V-002", "V-003")])
        near = condensed_node_view(g, ["V-001"], hops=1)
        self.assertEqual([v["tag"] for v in near["neighbors"]], ["V-002"])
        far = condensed_node_view(g, ["V-001"], hops=2)
        self.assertEqual(sorted(v["tag"] for v in far["neighbors"]),
                         ["V-002", "V-003"])

    def test_missing_member_raises(self):
        g = _graph([_node("V-001", "vessel")], [])
        with self.assertRaises(KeyError):
            condensed_node_view(g, ["GHOST-1"])

    def test_render_text_states_direction_honestly(self):
        g = _graph(
            [_node("V-001", "vessel"), _node("XV-1", "check_valve"),
             _node("V-002", "vessel"), _node("V-003", "vessel")],
            [_edge("V-001", "XV-1", "known"),
             _edge("XV-1", "V-002", "known"),
             _edge("V-001", "V-003")])
        text = render_text(condensed_node_view(g, ["V-001"]))
        self.assertIn("V-001 -> V-002 via check valve XV-1 "
                      "(direction verified", text)
        self.assertIn("direction NOT verified", text)
        self.assertIn("STUDY NODE MEMBERS", text)


if __name__ == "__main__":
    unittest.main()
