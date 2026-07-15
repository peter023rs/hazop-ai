"""
test_reasoner.py — Unit tests for the deterministic reasoner components.

Run:  python -m pytest tests/ -v      (or)   python -m unittest discover tests
Everything here runs on mocks; no stage 1/2, no API key.
"""

import unittest

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.critic import critique
from hazop.s3_are.reasoner.guidewords import (
    Guideword, deviations_for_parameter, deviations_for_parameters,
)
from hazop.s3_are.reasoner.llm import StubLLM, LLMInterface, GeneratedTriple, GeneratedFinding
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.s3_are.reasoner.schema import Parameter
from hazop.s3_are.reasoner.topology import TopologyReasoner


class TestGuidewords(unittest.TestCase):
    def test_pressure_has_no_reverse(self):
        devs = deviations_for_parameter(Parameter.PRESSURE)
        gws = {d.guideword for d in devs}
        self.assertNotIn(Guideword.REVERSE, gws)   # reverse pressure is nonsensical
        self.assertIn(Guideword.MORE, gws)
        self.assertIn(Guideword.LESS, gws)

    def test_flow_includes_reverse(self):
        devs = deviations_for_parameter(Parameter.FLOW)
        gws = {d.guideword for d in devs}
        self.assertIn(Guideword.REVERSE, gws)      # flow can reverse (FR-ARE-1)

    def test_full_matrix_is_nonempty_and_ordered(self):
        devs = deviations_for_parameters(
            [Parameter.FLOW, Parameter.PRESSURE])
        self.assertGreater(len(devs), 0)
        labels = [d.label for d in devs]
        self.assertIn("More Pressure", labels)
        self.assertIn("Reverse Flow", labels)


class TestTopology(unittest.TestCase):
    def setUp(self):
        self.tr = TopologyReasoner(build_topology())

    def test_downstream_tracing(self):
        ds = self.tr.downstream("P-101")
        self.assertIn("V-201", ds)
        self.assertIn("PSV-201", ds)

    def test_upstream_tracing(self):
        us = self.tr.upstream("V-201")
        self.assertIn("P-101", us)
        self.assertIn("TK-100", us)

    def test_relief_path_detection(self):
        self.assertTrue(self.tr.has_relief_path("P-101"))   # PSV-201 downstream
        self.assertFalse(self.tr.has_relief_path("PSV-201"))  # nothing downstream

    def test_relief_path_not_credited_through_blocking_equipment(self):
        # TK-100's only route to PSV-201 passes a closable valve (V-SUCT)
        # and a pump — that is not a credible relief path.
        self.assertFalse(self.tr.has_relief_path("TK-100"))

    def test_check_valve_on_path(self):
        cvs = self.tr.check_valves_between("P-101", "V-201")
        self.assertIn("CV-101", cvs)

    def test_flow_paths_enumeration(self):
        paths = self.tr.flow_paths("TK-100", "V-201")
        self.assertEqual(paths,
                         [["TK-100", "V-SUCT", "P-101", "CV-101", "V-201"]])
        self.assertEqual(self.tr.flow_paths("V-201", "TK-100"), [])

    def test_isolation_boundary_excludes_check_valves(self):
        # A check valve cannot be closed on demand, so CV-101 must not count
        # as downstream isolation for P-101.
        boundary = self.tr.isolation_boundary("P-101")
        self.assertEqual(boundary["upstream_isolation"], ["V-SUCT"])
        self.assertNotIn("CV-101", boundary["downstream_isolation"])

    def test_grounding_valid_and_invalid(self):
        valid, invalid = self.tr.validate_tags(["P-101", "GHOST-999"])
        self.assertEqual(valid, ["P-101"])
        self.assertEqual(invalid, ["GHOST-999"])


class TestDirectionAwareness(unittest.TestCase):
    """Real stage-1 graphs verify direction on only part of the edges
    (attributes["direction"]); results crossing unverified edges must be
    reported as unverified and never carry the 0.99 safeguard claim."""

    @staticmethod
    def _topology(mid_direction: str):
        from hazop.s3_are.reasoner.schema import (Connection, EquipmentNode,
                                     EquipmentType, TopologyGraph)
        nodes = [
            EquipmentNode("T-1", EquipmentType.TANK),
            EquipmentNode("P-1", EquipmentType.PUMP),
            EquipmentNode("V-1", EquipmentType.VESSEL),
            EquipmentNode("PSV-1", EquipmentType.RELIEF_VALVE),
        ]
        edges = [
            Connection("T-1", "P-1", attributes={"direction": "known"}),
            Connection("P-1", "V-1", attributes={"direction": mid_direction}),
            Connection("V-1", "PSV-1", attributes={"direction": "known"}),
        ]
        return TopologyGraph(nodes=nodes, edges=edges)

    def test_verified_chain_behaves_classically(self):
        tr = TopologyReasoner(self._topology("known"))
        self.assertEqual(tr.downstream("T-1"), ["P-1", "PSV-1", "V-1"])
        self.assertEqual(tr.downstream("T-1", verified_only=True),
                         ["P-1", "PSV-1", "V-1"])
        self.assertTrue(tr.has_relief_path("P-1"))

    def test_unverified_edge_reachable_but_flagged(self):
        tr = TopologyReasoner(self._topology("unknown"))
        detail = tr.downstream_detail("T-1")
        self.assertTrue(detail["P-1"])          # verified path
        self.assertFalse(detail["V-1"])         # crosses the unknown edge
        self.assertFalse(detail["PSV-1"])
        self.assertEqual(tr.downstream("T-1", verified_only=True), ["P-1"])
        # drawing order is arbitrary on unknown edges: reverse reachability
        self.assertIn("P-1", tr.upstream_detail("V-1"))
        self.assertIn("V-1", tr.downstream_detail("P-1"))

    def test_relief_claim_needs_verified_directions(self):
        tr = TopologyReasoner(self._topology("unknown"))
        # PSV-1 sits behind the unknown edge from P-1's viewpoint
        self.assertFalse(tr.has_relief_path("P-1"))
        self.assertTrue(tr.has_relief_path("P-1", verified_only=False))
        # from V-1 the relief edge itself is verified
        self.assertTrue(tr.has_relief_path("V-1"))

    def test_unverified_relief_flagged_not_credited_in_worksheet(self):
        from hazop.s3_are.reasoner.schema import StudyNode, Parameter as P
        reasoner = AIReasoner(self._topology("unknown"), MockRetriever(),
                              StubLLM())
        node = StudyNode(node_id="N", description="pressure node",
                         equipment_tags=["P-1"], design_intent="transfer",
                         parameters=[P.PRESSURE])
        rows = reasoner.analyze_node(node)
        row = next(r for r in rows if r.deviation.label == "More Pressure")
        relief = [f for f in row.safeguards if "elief path" in f.text]
        self.assertEqual(len(relief), 1)
        self.assertIn("not verified", relief[0].text)
        self.assertLess(relief[0].confidence, 0.9)


class TestAnthropicLLM(unittest.TestCase):
    """AnthropicLLM against a fake transport — no network, no `anthropic`
    package. Confidence must be composed from cited evidence (DDR-05),
    never taken from the model."""

    @staticmethod
    def _fake_client(payload, stop_reason="end_turn"):
        import json
        from types import SimpleNamespace

        class FakeMessages:
            def __init__(self):
                self.last_request = None

            def create(self, **kwargs):
                self.last_request = kwargs
                return SimpleNamespace(
                    stop_reason=stop_reason,
                    content=[SimpleNamespace(type="text",
                                             text=json.dumps(payload))],
                )

        return SimpleNamespace(messages=FakeMessages())

    @staticmethod
    def _evidence():
        from hazop.s3_are.reasoner.schema import RetrievedEvidence
        return [RetrievedEvidence(source_id="DOC-1#a", source_type="standard",
                                  snippet="Blocked outlet causes overpressure.",
                                  score=0.9)]

    def _deviation(self):
        from hazop.s3_are.reasoner.guidewords import deviations_for_parameter
        return next(d for d in deviations_for_parameter(Parameter.PRESSURE)
                    if d.label == "More Pressure")

    def test_parses_triple_and_composes_confidence(self):
        from hazop.s3_are.reasoner.llm import AnthropicLLM
        payload = {
            "causes": [{"text": "Blocked outlet with pump running.",
                        "referenced_tags": ["P-101"],
                        "evidence_ids": ["DOC-1#a", "GHOST-DOC"]}],
            "consequences": [{"text": "Overpressure of V-201.",
                              "referenced_tags": ["V-201"],
                              "evidence_ids": []}],
            "safeguards": [],
        }
        client = self._fake_client(payload)
        llm = AnthropicLLM(client=client)
        triple = llm.generate_findings(
            self._deviation(),
            {"node_id": "N", "equipment_tags": ["P-101"],
             "design_intent": "transfer"},
            self._evidence())
        cause = triple.causes[0]
        # unknown evidence ids are dropped; known ones kept
        self.assertEqual(cause.evidence_ids, ["DOC-1#a"])
        # composed confidence: evidence-backed above the unsupported floor
        self.assertGreater(cause.confidence, 0.5)
        self.assertEqual(triple.consequences[0].confidence, 0.30)
        self.assertEqual(triple.safeguards, [])
        # request used structured outputs and never asked for self-reported
        # confidence
        req = client.messages.last_request
        self.assertIn("output_config", req)
        self.assertNotIn("confidence",
                         str(req["output_config"]["format"]["schema"]))

    def test_refusal_raises(self):
        from hazop.s3_are.reasoner.llm import AnthropicLLM
        client = self._fake_client(
            {"causes": [], "consequences": [], "safeguards": []},
            stop_reason="refusal")
        llm = AnthropicLLM(client=client)
        with self.assertRaises(RuntimeError):
            llm.generate_findings(
                self._deviation(),
                {"node_id": "N", "equipment_tags": [],
                 "design_intent": ""},
                self._evidence())


class HallucinatingLLM(LLMInterface):
    """Returns a finding referencing a tag that isn't in the topology."""
    def generate_findings(self, deviation, node_context, evidence):
        bad = GeneratedFinding(
            text="Fault at GHOST-999 causes deviation.",
            referenced_tags=["GHOST-999"],
            confidence=0.9, evidence_ids=[],
        )
        return GeneratedTriple(causes=[bad], consequences=[], safeguards=[])


class TestReasoner(unittest.TestCase):
    def setUp(self):
        self.topology = build_topology()
        self.node = build_study_node()

    def test_full_matrix_analyzed(self):
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        rows = r.analyze_node(self.node)
        expected = len(deviations_for_parameters(self.node.parameters))
        self.assertEqual(len(rows), expected)

    def test_risk_ranking_is_never_autopopulated(self):
        # FR-ARE-6: severity/likelihood must remain human-only.
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        for row in r.analyze_node(self.node):
            self.assertIsNone(row.severity)
            self.assertIsNone(row.likelihood)

    def test_grounding_gate_rejects_hallucinated_tags(self):
        # FR-ARE-9 / MDL-10: findings citing unknown tags are dropped.
        r = AIReasoner(self.topology, MockRetriever(), HallucinatingLLM(),
                       grounding_required=True)
        rows = r.analyze_node(self.node)
        for row in rows:
            for f in row.causes:
                self.assertNotIn("GHOST-999", f.text)

    def test_grounding_rejections_are_audited(self):
        # NFR-S-01 auditability: a rejected finding leaves a trace naming the
        # invalid tags, and the critic reports the rejection count.
        r = AIReasoner(self.topology, MockRetriever(), HallucinatingLLM(),
                       grounding_required=True)
        rows = r.analyze_node(self.node)
        for row in rows:
            self.assertEqual(len(row.rejected_findings), 1)
            rej = row.rejected_findings[0]
            self.assertEqual(rej.kind, "cause")
            self.assertEqual(rej.invalid_tags, ["GHOST-999"])
            self.assertIn("rejected_findings", row.to_dict())
        report = critique(self.node, rows)
        self.assertEqual(report.rejected_finding_count, len(rows))
        self.assertIn("grounding gate", report.summary())

    def test_export_preserves_evidence_and_confidence(self):
        # NFR-S-02 / FR-ARE-5: evidence ids + confidence must survive export.
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        rows = r.analyze_node(self.node)
        pressure_more = next(x for x in rows
                             if x.deviation.label == "More Pressure")
        d = pressure_more.to_dict()
        evidence_backed = [c for c in d["causes"] if c["evidence"]]
        self.assertTrue(evidence_backed)
        for c in d["causes"]:
            self.assertIn("confidence", c)
            self.assertIn("supported", c)
            self.assertIn("provenance", c)

    def test_reverse_flow_safeguard_is_tag_order_independent(self):
        # equipment_tags has no ordering contract; CV-101 must be found even
        # when the list is in reverse flow order.
        node = build_study_node()
        node.equipment_tags = list(reversed(node.equipment_tags))
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        rows = r.analyze_node(node)
        rev = next(x for x in rows if x.deviation.label == "Reverse Flow")
        cv_findings = [f for f in rev.safeguards if "CV-101" in f.text]
        self.assertEqual(len(cv_findings), 1)

    def test_topology_safeguard_is_supported(self):
        # A topology-confirmed safeguard must not be flagged unsupported.
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        rows = r.analyze_node(self.node)
        pressure_more = next(x for x in rows if x.deviation.label == "More Pressure")
        topo = [f for f in pressure_more.safeguards if f.topology_grounded]
        self.assertTrue(topo)
        self.assertTrue(all(f.is_supported for f in topo))
        self.assertTrue(all("UNSUPPORTED" not in f.label for f in topo))

    def test_no_duplicate_check_valve_safeguard(self):
        r = AIReasoner(self.topology, MockRetriever(), StubLLM())
        rows = r.analyze_node(self.node)
        rev = next(x for x in rows if x.deviation.label == "Reverse Flow")
        cv_findings = [f for f in rev.safeguards if "CV-101" in f.text]
        self.assertEqual(len(cv_findings), 1)


class TestMockRetriever(unittest.TestCase):
    def test_compound_guideword_tokenizes(self):
        # "No/Not flow" must match the {"no", "flow"} entry with full overlap,
        # ranking it above entries that only share "flow".
        results = MockRetriever().retrieve("No/Not flow")
        self.assertTrue(results)
        self.assertEqual(results[0].source_id, "HIST-HAZOP-007#N2")

    def test_structured_filters_used(self):
        results = MockRetriever().retrieve(
            "anything", filters={"guideword": "MORE", "parameter": "pressure"})
        self.assertTrue(results)
        self.assertEqual(results[0].source_id, "HIST-HAZOP-012#N4")


class TestCritic(unittest.TestCase):
    def test_complete_when_all_deviations_covered(self):
        node = build_study_node()
        r = AIReasoner(build_topology(), MockRetriever(), StubLLM())
        rows = r.analyze_node(node)
        report = critique(node, rows)
        self.assertTrue(report.is_complete)
        self.assertEqual(report.missing_deviations, [])

    def test_gap_detected_when_deviation_missing(self):
        node = build_study_node()
        r = AIReasoner(build_topology(), MockRetriever(), StubLLM())
        rows = r.analyze_node(node)
        report = critique(node, rows[:-3])   # drop a few rows
        self.assertFalse(report.is_complete)
        self.assertTrue(report.missing_deviations)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestParallelFanOut(unittest.TestCase):
    """DDR-11 / MDL-12: deviations fan out across workers with results
    identical to (and ordered like) the serial run."""

    class _SlowLLM(StubLLM):
        def __init__(self):
            import threading
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def generate_findings(self, deviation, node_context, evidence):
            import time
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            try:
                return super().generate_findings(deviation, node_context,
                                                 evidence)
            finally:
                with self.lock:
                    self.active -= 1

    def _reasoner(self, llm, workers):
        from hazop.s3_are.reasoner.mock_retriever import MockRetriever
        return AIReasoner(topology=build_topology(),
                          retriever=MockRetriever(), llm=llm,
                          max_workers=workers)

    def test_parallel_matches_serial(self):
        node = build_study_node()
        serial = self._reasoner(StubLLM(), 1).analyze_node(node)
        parallel = self._reasoner(StubLLM(), 8).analyze_node(node)
        self.assertEqual([r.deviation.label for r in serial],
                         [r.deviation.label for r in parallel])
        self.assertEqual([r.to_dict() for r in serial],
                         [r.to_dict() for r in parallel])

    def test_deviations_actually_overlap(self):
        llm = self._SlowLLM()
        self._reasoner(llm, 8).analyze_node(build_study_node())
        self.assertGreater(llm.max_active, 1)

    def test_serial_never_overlaps(self):
        llm = self._SlowLLM()
        self._reasoner(llm, 1).analyze_node(build_study_node())
        self.assertEqual(llm.max_active, 1)


class TestLocalLLM(unittest.TestCase):
    """LocalLLM / OpenAICompatClient against a fake transport — the
    on-prem branch of DDR-06. No server, no network."""

    @staticmethod
    def _client(responses):
        """Fake transport returning canned OpenAI-style responses."""
        from hazop.s3_are.reasoner.llm import OpenAICompatClient
        import json as _json
        calls = []

        def transport(url, payload):
            calls.append((url, payload))
            body, finish = responses[min(len(calls), len(responses)) - 1]
            text = body if isinstance(body, str) else _json.dumps(body)
            return {"choices": [{"finish_reason": finish,
                                 "message": {"content": text}}]}

        client = OpenAICompatClient(transport=transport)
        return client, calls

    @staticmethod
    def _payload():
        return {"causes": [{"text": "Blocked outlet.",
                            "referenced_tags": ["P-101"],
                            "evidence_ids": ["DOC-1#a", "GHOST"]}],
                "consequences": [{"text": "Overpressure.",
                                  "referenced_tags": [],
                                  "evidence_ids": []}],
                "safeguards": []}

    def _generate(self, client):
        from hazop.s3_are.reasoner.llm import LocalLLM
        from hazop.s3_are.reasoner.guidewords import deviations_for_parameter
        from hazop.s3_are.reasoner.schema import RetrievedEvidence
        dev = next(d for d in deviations_for_parameter(Parameter.PRESSURE)
                   if d.label == "More Pressure")
        ev = [RetrievedEvidence(source_id="DOC-1#a", source_type="standard",
                                snippet="Blocked outlet causes overpressure.",
                                score=0.9)]
        return LocalLLM(client=client).generate_findings(
            dev, {"node_id": "N", "equipment_tags": ["P-101"],
                  "design_intent": "x"}, ev)

    def test_parses_and_composes_like_the_cloud_client(self):
        client, calls = self._client([(self._payload(), "stop")])
        triple = self._generate(client)
        self.assertEqual(triple.causes[0].evidence_ids, ["DOC-1#a"])  # GHOST dropped
        self.assertEqual(triple.causes[0].confidence, 0.55)   # composed
        self.assertEqual(triple.consequences[0].confidence, 0.30)
        # request used OpenAI-style structured output, local URL only
        url, payload = calls[0]
        self.assertIn("localhost", url)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertNotIn("confidence",
                         str(payload["response_format"]["json_schema"]))

    def test_invalid_json_retried_once_then_refused(self):
        client, calls = self._client([("not json {", "stop"),
                                      (self._payload(), "stop")])
        triple = self._generate(client)                       # retry succeeds
        self.assertEqual(len(calls), 2)
        self.assertEqual(triple.causes[0].text, "Blocked outlet.")
        # feedback loop carried the parse error back to the model
        self.assertIn("not valid JSON", calls[1][1]["messages"][-1]["content"])

        client, calls = self._client([("nope", "stop"), ("nope", "stop")])
        with self.assertRaises(RuntimeError):                 # then refuse
            self._generate(client)
        self.assertEqual(len(calls), 2)

    def test_truncation_refused_not_repaired(self):
        client, _ = self._client([(self._payload(), "length")])
        with self.assertRaises(RuntimeError):
            self._generate(client)


class TestLocalEvidenceCritic(unittest.TestCase):
    def _critic(self, responses):
        from hazop.s3_are.reasoner.evidence_critic import LocalEvidenceCritic
        import json as _json

        def transport(url, payload):
            body, finish = responses.pop(0)
            text = body if isinstance(body, str) else _json.dumps(body)
            return {"choices": [{"finish_reason": finish,
                                 "message": {"content": text}}]}

        from hazop.s3_are.reasoner.llm import OpenAICompatClient
        return LocalEvidenceCritic(client=OpenAICompatClient(
            transport=transport))

    @staticmethod
    def _evidence():
        from hazop.s3_are.reasoner.schema import RetrievedEvidence
        return [RetrievedEvidence(source_id="A#1", source_type="standard",
                                  snippet="text", score=0.5)]

    def test_verdicts_parsed(self):
        from hazop.s3_are.reasoner.evidence_critic import EvidenceVerdict
        critic = self._critic([({"checks": [
            {"evidence_id": "A#1", "verdict": "contradicted",
             "rationale": "says the opposite"}]}, "stop")])
        checks = critic.check_claim("Claim.", self._evidence())
        self.assertEqual(checks[0].verdict, EvidenceVerdict.CONTRADICTED)

    def test_critic_failure_abstains_never_supports(self):
        from hazop.s3_are.reasoner.evidence_critic import EvidenceVerdict
        critic = self._critic([("bad", "stop"), ("bad", "stop")])
        checks = critic.check_claim("Claim.", self._evidence())
        self.assertEqual([c.verdict for c in checks],
                         [EvidenceVerdict.INSUFFICIENT])
