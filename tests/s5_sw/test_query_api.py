"""
test_query_api.py — Graph Explorer endpoints (/api/query, /api/query/meta,
/api/cypher) on the dashboard.

Uses the Flask test client against the real dashboard state (the shipped
2401 plant graph), like the rest of the s5 surface. /api/cypher is only
exercised for its guard paths — no live Neo4j in CI.
"""

import unittest
import warnings

import hazop.s5_sw.app as webapp


class TestQueryAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        warnings.filterwarnings(
            "ignore", message="Topology edges reference tags")
        cls.client = webapp.app.test_client()

    def test_meta_lists_labels_relationships_examples(self):
        r = self.client.get("/api/query/meta")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        labels = {l["label"] for l in d["labels"]}
        self.assertIn("ReliefValve", labels)
        self.assertEqual({r_["type"] for r_ in d["relationships"]},
                         {"FLOWS_TO", "CONNECTED_TO"})
        self.assertGreaterEqual(len(d["examples"]), 5)
        for e in d["examples"]:
            self.assertIn("question", e)
            self.assertIn("cypher", e)

    def test_query_answers_with_cypher_rows_and_subgraph(self):
        r = self.client.post("/api/query",
                             json={"question": "list all relief valves"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["intent"], "list")
        self.assertIn("ReliefValve", d["cypher"])
        self.assertEqual(len(d["rows"]), 5)          # the 2401 unit's PSVs
        self.assertEqual(len(d["nodes"]), 5)

    def test_query_traversal_on_real_graph(self):
        r = self.client.post(
            "/api/query",
            json={"question": "what is downstream of 2401-K-001A?"})
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(d["intent"], "downstream")
        self.assertTrue(any(row["flow"] == "verified" for row in d["rows"]))

    def test_bad_question_returns_hint_not_500(self):
        r = self.client.post("/api/query",
                             json={"question": "sing me a song"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("examples", r.get_json()["error"])

    def test_empty_question_rejected(self):
        r = self.client.post("/api/query", json={})
        self.assertEqual(r.status_code, 400)

    def test_cypher_write_clause_rejected(self):
        r = self.client.post("/api/cypher",
                             json={"query": "MATCH (n) DETACH DELETE n"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("read-only", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
