"""Natural-language + Cypher query layer over the plant graph."""

import unittest

from hazop.s2_pml import (GraphQuery, Intent, QueryError, parse_question,
                          run_cypher)
from hazop.s2_pml.query import examples, find_tags, to_cypher


def _node(tag, etype, name=None):
    return {"tag": tag, "equipment_type": etype, "name": name or tag,
            "attributes": {"sheets": [4]}, "detection_confidence": 1.0}


def _edge(source, target, direction="unknown", lines=()):
    attrs = {"line_numbers": list(lines), "direction": direction}
    if direction == "known":
        attrs["direction_sources"] = ["arrow"]
    return {"source": source, "target": target,
            "line_tag": lines[0] if lines else "", "attributes": attrs}


def _graph():
    """K-001 -> V-001 -> PSV-001 (all verified), V-001 -? V-002
    (drawing order only), plus an off-train instrument."""
    return {
        "nodes": [_node("2401-K-001", "compressor"),
                  _node("2401-V-001", "vessel"),
                  _node("2401-V-002", "vessel"),
                  _node("2401-PSV-001", "relief_valve"),
                  _node("2401-FT-001", "instrument")],
        "edges": [_edge("2401-K-001", "2401-V-001", "known", ["50-GA-1"]),
                  _edge("2401-V-001", "2401-PSV-001", "known"),
                  _edge("2401-V-001", "2401-V-002"),
                  _edge("2401-FT-001", "2401-V-002")],
        "stats": {},
    }


class TestTagGrounding(unittest.TestCase):
    TAGS = ["2401-K-001", "2401-V-001", "2401-V-002"]

    def test_exact_match_case_insensitive(self):
        self.assertEqual(find_tags("trace 2401-k-001 please", self.TAGS),
                         ["2401-K-001"])

    def test_partial_resolves_by_suffix(self):
        self.assertEqual(find_tags("downstream of K-001", self.TAGS),
                         ["2401-K-001"])

    def test_ambiguous_partial_raises_with_candidates(self):
        with self.assertRaises(QueryError) as ctx:
            find_tags("what about 001", self.TAGS)
        self.assertIn("2401-K-001", str(ctx.exception))

    def test_plain_words_are_not_tags(self):
        self.assertEqual(find_tags("list all relief valves", self.TAGS), [])


class TestParseQuestion(unittest.TestCase):
    def setUp(self):
        self.g = _graph()

    def parse(self, q):
        intent = parse_question(q, self.g)
        self.assertIsNotNone(intent, f"no parse for: {q}")
        return intent

    def test_downstream_phrasings(self):
        for q in ("What is downstream of 2401-K-001?",
                  "where does K-001 go",
                  "what does 2401-K-001 feed"):
            intent = self.parse(q)
            self.assertEqual((intent.kind, intent.tag),
                             ("downstream", "2401-K-001"), q)

    def test_downstream_with_type_filter(self):
        intent = self.parse("which vessels are downstream of 2401-K-001?")
        self.assertEqual(intent.kind, "downstream")
        self.assertEqual(intent.equipment_type, "vessel")

    def test_upstream_phrasings(self):
        for q in ("what is upstream of 2401-V-001",
                  "what feeds V-001?"):
            intent = self.parse(q)
            self.assertEqual((intent.kind, intent.tag),
                             ("upstream", "2401-V-001"), q)

    def test_path(self):
        intent = self.parse("show the path from K-001 to V-001")
        self.assertEqual((intent.kind, intent.source, intent.target),
                         ("path", "2401-K-001", "2401-V-001"))

    def test_list_synonyms(self):
        for q, etype in (("list all relief valves", "relief_valve"),
                         ("show every PSV", "relief_valve"),
                         ("which check valves are there", "check_valve"),
                         ("compressors", "compressor")):
            intent = self.parse(q)
            self.assertEqual((intent.kind, intent.equipment_type),
                             ("list", etype), q)

    def test_count(self):
        intent = self.parse("how many of each equipment type?")
        self.assertEqual(intent.kind, "count")
        intent = self.parse("how many vessels are there?")
        self.assertEqual((intent.kind, intent.equipment_type),
                         ("count", "vessel"))

    def test_relief(self):
        for q in ("does 2401-V-001 have a relief path?",
                  "is V-001 protected against overpressure"):
            intent = self.parse(q)
            self.assertEqual((intent.kind, intent.tag),
                             ("relief", "2401-V-001"), q)

    def test_neighbours(self):
        intent = self.parse("what is connected to V-001?")
        self.assertEqual((intent.kind, intent.tag),
                         ("neighbours", "2401-V-001"))

    def test_info_and_bare_tag(self):
        self.assertEqual(self.parse("tell me about 2401-V-001").kind, "info")
        self.assertEqual(self.parse("2401-V-001").kind, "info")

    def test_tagless_trace_raises_with_hint(self):
        with self.assertRaises(QueryError):
            parse_question("what is downstream of the big compressor",
                           self.g)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_question(
            "tell me a joke about pipes", self.g))


class TestToCypher(unittest.TestCase):
    def test_downstream_uses_flows_to(self):
        c = to_cypher(Intent("downstream", tag="2401-K-001"))
        self.assertIn("-[:FLOWS_TO*1..", c)
        self.assertIn('{tag: "2401-K-001"}', c)

    def test_list_uses_specific_label(self):
        c = to_cypher(Intent("list", equipment_type="relief_valve"))
        self.assertIn("(n:ReliefValve)", c)

    def test_traversal_filter_adds_label(self):
        c = to_cypher(Intent("downstream", tag="2401-K-001",
                             equipment_type="vessel"))
        self.assertIn("(x:Vessel)", c)

    def test_neighbours_matches_both_rel_types(self):
        c = to_cypher(Intent("neighbours", tag="2401-V-001"))
        self.assertIn("FLOWS_TO|CONNECTED_TO", c)


class TestGraphQueryExecution(unittest.TestCase):
    def setUp(self):
        self.gq = GraphQuery(_graph())

    def test_downstream_splits_verified_and_unverified(self):
        r = self.gq.ask("what is downstream of 2401-K-001?")
        by_tag = {row["tag"]: row["flow"] for row in r.rows}
        self.assertEqual(by_tag["2401-V-001"], "verified")
        self.assertEqual(by_tag["2401-PSV-001"], "verified")
        self.assertEqual(by_tag["2401-V-002"], "unverified")
        self.assertIn("FLOWS_TO", r.cypher)
        self.assertTrue(any(n["tag"] == "2401-K-001" for n in r.nodes))

    def test_downstream_type_filter(self):
        r = self.gq.ask("which vessels are downstream of K-001?")
        self.assertEqual({row["tag"] for row in r.rows},
                         {"2401-V-001", "2401-V-002"})

    def test_upstream(self):
        r = self.gq.ask("what feeds 2401-V-001?")
        self.assertIn("2401-K-001", {row["tag"] for row in r.rows})

    def test_path_orders_shortest_first(self):
        r = self.gq.ask("path from 2401-K-001 to 2401-PSV-001")
        self.assertEqual(r.rows[0]["hops"], 2)
        self.assertEqual(r.rows[0]["flow"], "verified")

    def test_no_path_answer(self):
        r = self.gq.ask("path from 2401-PSV-001 to 2401-K-001")
        self.assertEqual(r.rows, [])
        self.assertIn("No flow path", r.answer)

    def test_list_relief_valves(self):
        r = self.gq.ask("list all relief valves")
        self.assertEqual([row["tag"] for row in r.rows], ["2401-PSV-001"])

    def test_count(self):
        r = self.gq.ask("how many of each equipment type?")
        counts = {row["equipment_type"]: row["count"] for row in r.rows}
        self.assertEqual(counts["vessel"], 2)

    def test_relief_protected(self):
        r = self.gq.ask("does 2401-V-001 have a relief path?")
        self.assertTrue(r.answer.startswith("Yes"))
        self.assertEqual(r.rows[0]["relief_valve"], "2401-PSV-001")

    def test_relief_drawn_but_unverified(self):
        # V-002 reaches the PSV only across the unverified V-001 edge
        r = self.gq.ask("does 2401-V-002 have a relief path?")
        self.assertIn("Drawn but not verified", r.answer)

    def test_relief_missing_flags_for_team(self):
        # nothing is downstream of the PSV itself, and a relief valve is
        # not its own overpressure protection
        r = self.gq.ask("does 2401-PSV-001 have a relief path?")
        self.assertIn("flag for the HAZOP team", r.answer)

    def test_relief_behind_closable_valve_not_credited(self):
        # V-009 -> XV-9 (closable) -> PSV-002 and no other route: the PSV
        # is reachable but fails the blocked-outlet case
        g = {"nodes": [_node("2401-V-009", "vessel"),
                       _node("2401-XV-9", "valve"),
                       _node("2401-PSV-002", "relief_valve")],
             "edges": [_edge("2401-V-009", "2401-XV-9", "known"),
                       _edge("2401-XV-9", "2401-PSV-002", "known")],
             "stats": {}}
        r = GraphQuery(g).ask("does 2401-V-009 have a relief path?")
        self.assertIn("Not credited", r.answer)
        self.assertIn("2401-PSV-002",
                      {row["relief_valve"] for row in r.rows})

    def test_neighbours_reports_rel_type(self):
        r = self.gq.ask("what is connected to 2401-V-001?")
        rels = {row["tag"]: row["relationship"] for row in r.rows}
        self.assertEqual(rels["2401-PSV-001"], "FLOWS_TO (out)")
        self.assertEqual(rels["2401-K-001"], "FLOWS_TO (in)")
        self.assertIn("CONNECTED_TO", rels["2401-V-002"])

    def test_info_lists_properties(self):
        r = self.gq.ask("tell me about 2401-V-001")
        props = {row["property"]: row["value"] for row in r.rows}
        self.assertEqual(props["equipment_type"], "vessel")

    def test_unknown_tag_fails_closed(self):
        with self.assertRaises(QueryError):
            self.gq.run(Intent("downstream", tag="2401-X-999"))

    def test_unparseable_without_translator_raises_hint(self):
        with self.assertRaises(QueryError) as ctx:
            self.gq.ask("sing me a song")
        self.assertIn("examples", str(ctx.exception))

    def test_translator_fallback_is_regrounded(self):
        class FakeTranslator:
            def translate(self, question):
                return Intent("downstream", tag="K-001")  # partial tag

        gq = GraphQuery(_graph(), translator=FakeTranslator())
        r = gq.ask("trace the flow from that first machine")
        self.assertEqual(r.intent, "downstream")
        self.assertIn("2401-V-001", {row["tag"] for row in r.rows})

    def test_result_serializes(self):
        d = self.gq.ask("list all vessels").to_dict()
        self.assertEqual(sorted(d), ["answer", "cypher", "edges", "intent",
                                     "nodes", "question", "rows"])


class TestExamples(unittest.TestCase):
    def test_examples_parse_against_own_graph(self):
        ex = examples(_graph())
        self.assertGreaterEqual(len(ex), 5)
        g = _graph()
        for e in ex:
            self.assertIsNotNone(parse_question(e["question"], g),
                                 e["question"])
            self.assertTrue(e["cypher"].startswith("MATCH"), e["question"])


class _FakeNode(dict):
    def __init__(self, tag, labels):
        super().__init__(tag=tag, name=tag, equipment_type=labels[-1].lower())
        self.labels = labels


class _FakeRel:
    def __init__(self, start, end, rtype):
        self.start_node, self.end_node, self.type = start, end, rtype

    def keys(self):
        return ()

    def __iter__(self):
        return iter(())


class _FakeResult:
    def __init__(self, columns, records):
        self._columns, self._records = columns, records

    def keys(self):
        return self._columns

    def __iter__(self):
        return iter(self._records)


class _FakeSession:
    def __init__(self, result):
        self._result = result

    def run(self, cypher):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeDriver:
    def __init__(self, result):
        self._result = result
        self.closed = False

    def session(self, database=None):
        return _FakeSession(self._result)

    def close(self):
        self.closed = True


class TestRunCypher(unittest.TestCase):
    def test_write_clauses_rejected_before_touching_server(self):
        for bad in ("MATCH (n) DETACH DELETE n",
                    "MERGE (n:PlantItem {tag:'x'})",
                    "MATCH (n) SET n.tag = 'y'"):
            with self.assertRaises(QueryError):
                run_cypher(bad, driver=object())  # driver never used

    def test_read_query_returns_rows_and_graph(self):
        a = _FakeNode("2401-K-001", ["PlantItem", "Compressor"])
        b = _FakeNode("2401-V-001", ["PlantItem", "Vessel"])
        rel = _FakeRel(a, b, "FLOWS_TO")
        result = _FakeResult(["n", "r"], [{"n": a, "r": rel}])
        out = run_cypher("MATCH (n)-[r]->(m) RETURN n, r",
                         driver=_FakeDriver(result))
        self.assertEqual(out["columns"], ["n", "r"])
        tags = {n["tag"] for n in out["nodes"]}
        self.assertEqual(tags, {"2401-K-001", "2401-V-001"})
        self.assertEqual(out["edges"][0]["rel"], "FLOWS_TO")
        self.assertEqual(out["nodes"][0]["label"] in
                         ("Compressor", "Vessel"), True)


if __name__ == "__main__":
    unittest.main()
