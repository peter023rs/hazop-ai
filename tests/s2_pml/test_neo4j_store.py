"""Tests for the Neo4j plant-graph store — all offline: Cypher generation
is pure strings, the live loader is exercised with a fake driver."""

import unittest

from hazop.s2_pml.neo4j_store import load, to_cypher


def tiny_graph():
    """One known (arrow-backed) connection, one unknown with a note,
    a tag needing string escaping, and a CJK off-page connector label."""
    return {
        "nodes": [
            {"tag": "2401-K-001A", "equipment_type": "compressor",
             "name": "2401-K-001A", "detection_confidence": 1.0,
             "attributes": {"sheets": [4, 8], "shape": "capsule"}},
            {"tag": "EQ-p4n771", "equipment_type": "vessel",
             "name": "untagged circle", "detection_confidence": 0.7,
             "attributes": {"sheets": [4], "shape": "circle"}},
            {"tag": "XV-quote'd", "equipment_type": "check_valve",
             "name": "CheckValve", "detection_confidence": 0.9,
             "attributes": {"component_class": "CheckValve"}},
            {"tag": "OPC-1", "equipment_type": "line",
             "name": "至管网", "detection_confidence": 0.9,
             "attributes": {"labels": ["至管网"]}},
        ],
        "edges": [
            {"source": "2401-K-001A", "target": "EQ-p4n771",
             "line_tag": "50-IA-001",
             "attributes": {"direction": "known",
                            "direction_sources": ["arrow"],
                            "line_numbers": ["50-IA-001"]}},
            {"source": "XV-quote'd", "target": "OPC-1", "line_tag": "",
             "attributes": {"direction": "unknown",
                            "direction_note": "no through-flow direction",
                            "line_numbers": []}},
        ],
        "stats": {},
    }


class TestToCypher(unittest.TestCase):
    def setUp(self):
        self.script = to_cypher(tiny_graph())

    def test_constraint_and_idempotent_merges(self):
        self.assertIn("CREATE CONSTRAINT plant_item_tag IF NOT EXISTS",
                      self.script)
        # every node/edge statement is a MERGE, never a bare CREATE
        creates = [l for l in self.script.splitlines()
                   if l.startswith("CREATE") and "CONSTRAINT" not in l]
        self.assertEqual(creates, [])

    def test_direction_split(self):
        self.assertIn('MERGE (a)-[r:FLOWS_TO]->(b)', self.script)
        self.assertIn('MERGE (a)-[r:CONNECTED_TO]->(b)', self.script)
        # FLOWS_TO carries the evidence; CONNECTED_TO carries the caveat
        flows = next(l for l in self.script.splitlines()
                     if "[r:FLOWS_TO]" in l)
        self.assertIn('direction_sources: ["arrow"]', flows)
        conn = next(l for l in self.script.splitlines()
                    if "[r:CONNECTED_TO]" in l)
        self.assertIn('direction: "unknown"', conn)
        self.assertIn("no through-flow direction", conn)

    def test_labels_and_props(self):
        self.assertIn("SET n:Compressor", self.script)
        self.assertIn("SET n:CheckValve", self.script)
        self.assertIn("SET n:Line", self.script)
        self.assertIn("sheets: [4, 8]", self.script)
        # OPC 'labels' attribute renamed so it can't be confused with
        # Neo4j node labels (no bare `labels` property key survives)
        self.assertIn("opc_labels", self.script)
        self.assertNotIn(", labels:", self.script)
        self.assertNotIn("{labels:", self.script)

    def test_string_escaping_and_unicode(self):
        # quoted tag survives via JSON string literals
        self.assertIn('"XV-quote\'d"', self.script)
        # CJK stays readable (no \\u escapes)
        self.assertIn("至管网", self.script)


class FakeSession:
    def __init__(self, log):
        self.log = log

    def run(self, query, **params):
        self.log.append((query, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriver:
    def __init__(self):
        self.queries = []
        self.closed = False

    def session(self, database=None):
        return FakeSession(self.queries)

    def close(self):
        self.closed = True


class TestLoad(unittest.TestCase):
    def test_batched_load_counts_and_batching(self):
        driver = FakeDriver()
        counts = load(tiny_graph(), driver=driver, batch_size=2)
        self.assertEqual(counts, {"nodes": 4, "relationships": 2})
        # constraint first, then UNWIND batches
        self.assertIn("CREATE CONSTRAINT", driver.queries[0][0])
        unwinds = [q for q, _ in driver.queries if q.startswith("UNWIND")]
        self.assertTrue(unwinds)
        # injected driver is NOT closed (caller owns it)
        self.assertFalse(driver.closed)

    def test_rows_carry_flat_props(self):
        driver = FakeDriver()
        load(tiny_graph(), driver=driver)
        node_rows = [p["rows"] for q, p in driver.queries
                     if q.startswith("UNWIND") and "MERGE (n:PlantItem" in q]
        all_rows = [r for rows in node_rows for r in rows]
        self.assertEqual(len(all_rows), 4)
        for row in all_rows:
            for value in row.values():
                self.assertIsInstance(value, (str, int, float, bool, list))


if __name__ == "__main__":
    unittest.main()
