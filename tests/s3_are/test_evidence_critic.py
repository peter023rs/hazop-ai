"""Stage B evidence critic (DDR-02 / MDL-11): claim-vs-citation judgments,
the refuse-on-contradiction path, citation stripping, and the Anthropic
critic against a fake transport."""

import json
import unittest
from types import SimpleNamespace

from hazop.s3_are.mock_data.pump_vessel import build_study_node, build_topology
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.evidence_critic import (
    AnthropicEvidenceCritic,
    ClaimCheck,
    EvidenceCriticInterface,
    EvidenceVerdict,
    LexicalEvidenceCritic,
)
from hazop.s3_are.reasoner.llm import (
    GeneratedFinding,
    GeneratedTriple,
    LLMInterface,
)
from hazop.s3_are.reasoner.schema import RetrievedEvidence


def _ev(source_id="DOC-1#a",
        snippet="Blocked outlet causes overpressure of the vessel."):
    return RetrievedEvidence(source_id=source_id, source_type="standard",
                             snippet=snippet, score=0.9)


class TestLexicalEvidenceCritic(unittest.TestCase):
    def test_claim_drawn_from_excerpt_is_supported(self):
        critic = LexicalEvidenceCritic()
        checks = critic.check_claim(
            "Blocked outlet causes overpressure.", [_ev()])
        self.assertEqual(checks[0].verdict, EvidenceVerdict.SUPPORTED)
        self.assertEqual(checks[0].evidence_id, "DOC-1#a")

    def test_unrelated_claim_is_insufficient_never_contradicted(self):
        critic = LexicalEvidenceCritic()
        checks = critic.check_claim(
            "Bearing failure from lubrication loss on the compressor.",
            [_ev()])
        # token overlap cannot see negation: abstain, don't accuse
        self.assertEqual(checks[0].verdict, EvidenceVerdict.INSUFFICIENT)

    def test_one_check_per_citation_in_order(self):
        critic = LexicalEvidenceCritic()
        checks = critic.check_claim("Blocked outlet causes overpressure.",
                                    [_ev("A#1"), _ev("B#2", "Unrelated text "
                                                     "about calibration.")])
        self.assertEqual([c.evidence_id for c in checks], ["A#1", "B#2"])
        self.assertEqual(checks[0].verdict, EvidenceVerdict.SUPPORTED)
        self.assertEqual(checks[1].verdict, EvidenceVerdict.INSUFFICIENT)


class _OneFindingLLM(LLMInterface):
    """Emits exactly one cause citing whatever evidence was retrieved."""

    def __init__(self, text="Blocked outlet causes overpressure."):
        self.text = text

    def generate_findings(self, deviation, node_context, evidence):
        f = GeneratedFinding(
            text=self.text,
            referenced_tags=[],
            confidence=0.85,
            evidence_ids=[e.source_id for e in evidence],
        )
        return GeneratedTriple(causes=[f], consequences=[], safeguards=[])


class _FixedRetriever:
    def __init__(self, evidence):
        self.evidence = evidence

    def retrieve(self, query, k=5, filters=None):
        return self.evidence


class _VerdictCritic(EvidenceCriticInterface):
    """Returns a fixed verdict for every citation."""

    def __init__(self, verdict):
        self.verdict = verdict

    def check_claim(self, claim_text, evidence):
        return [ClaimCheck(e.source_id, self.verdict, "test") for e in evidence]


class TestReasonerStageB(unittest.TestCase):
    def _rows(self, critic, evidence):
        reasoner = AIReasoner(
            topology=build_topology(),
            retriever=_FixedRetriever(evidence),
            llm=_OneFindingLLM(),
            evidence_critic=critic,
        )
        return reasoner.analyze_node(build_study_node())

    def test_contradicted_citation_refuses_the_finding(self):
        rows = self._rows(_VerdictCritic(EvidenceVerdict.CONTRADICTED),
                          [_ev()])
        for row in rows:
            self.assertEqual(row.causes, [])           # excluded from body
            self.assertEqual(len(row.rejected_findings), 1)  # kept for audit
            rej = row.rejected_findings[0]
            self.assertEqual(rej.reason, "evidence_contradicted")
            self.assertEqual(rej.failed_evidence, ["DOC-1#a"])
            self.assertEqual(rej.invalid_tags, [])

    def test_insufficient_citations_are_stripped_and_flagged(self):
        rows = self._rows(_VerdictCritic(EvidenceVerdict.INSUFFICIENT),
                          [_ev()])
        for row in rows:
            cause = row.causes[0]
            self.assertEqual(cause.evidence, [])       # citation stripped
            self.assertFalse(cause.is_supported)
            self.assertTrue(cause.label.startswith("[UNSUPPORTED INFERENCE]"))
            self.assertLessEqual(cause.confidence, 0.30)
            # the verdicts stay auditable on the finding
            self.assertEqual(cause.evidence_checks[0]["verdict"],
                             "insufficient")
            self.assertEqual(row.rejected_findings, [])  # flagged, not dropped

    def test_supported_citations_survive_untouched(self):
        rows = self._rows(LexicalEvidenceCritic(), [_ev()])
        for row in rows:
            cause = row.causes[0]
            self.assertEqual([e.source_id for e in cause.evidence],
                             ["DOC-1#a"])
            self.assertEqual(cause.confidence, 0.85)   # never raised, not cut
            self.assertTrue(cause.is_supported)

    def test_confidence_recomposed_from_surviving_citations(self):
        good = _ev("GOOD#1")
        bad = _ev("BAD#2", "Text about an unrelated maintenance procedure "
                           "for exchanger cleaning schedules.")
        rows = self._rows(LexicalEvidenceCritic(), [good, bad])
        for row in rows:
            cause = row.causes[0]
            self.assertEqual([e.source_id for e in cause.evidence],
                             ["GOOD#1"])
            # one surviving citation: 0.4 + 0.15 = 0.55 < original 0.85
            self.assertEqual(cause.confidence, 0.55)

    def test_findings_without_citations_pass_through(self):
        rows = self._rows(_VerdictCritic(EvidenceVerdict.CONTRADICTED), [])
        for row in rows:
            self.assertEqual(len(row.causes), 1)       # nothing to contradict
            self.assertEqual(row.rejected_findings, [])

    def test_no_critic_means_no_stage_b(self):
        reasoner = AIReasoner(
            topology=build_topology(),
            retriever=_FixedRetriever([_ev()]),
            llm=_OneFindingLLM(),
        )
        rows = reasoner.analyze_node(build_study_node())
        for row in rows:
            self.assertEqual(row.causes[0].evidence_checks, [])


class TestAnthropicEvidenceCritic(unittest.TestCase):
    """Fake transport — no network, no `anthropic` package."""

    @staticmethod
    def _fake_client(payload, stop_reason="end_turn"):
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

    def test_parses_verdicts_and_abstains_on_missing(self):
        payload = {"checks": [
            {"evidence_id": "DOC-1#a", "verdict": "supported",
             "rationale": "excerpt states the claim"},
            {"evidence_id": "GHOST", "verdict": "supported",
             "rationale": "should be ignored"},
        ]}
        client = self._fake_client(payload)
        critic = AnthropicEvidenceCritic(client=client)
        checks = critic.check_claim(
            "Blocked outlet causes overpressure.",
            [_ev("DOC-1#a"), _ev("DOC-2#b", "Other text.")])
        self.assertEqual(checks[0].verdict, EvidenceVerdict.SUPPORTED)
        # unknown ids ignored; unjudged citations get an abstention
        self.assertEqual(checks[1].evidence_id, "DOC-2#b")
        self.assertEqual(checks[1].verdict, EvidenceVerdict.INSUFFICIENT)
        # the critic runs a different model than the generator (DDR-02)
        from hazop.s3_are.reasoner.llm import AnthropicLLM
        req = client.messages.last_request
        self.assertNotEqual(req["model"], AnthropicLLM.MODEL)
        self.assertIn("output_config", req)

    def test_refusal_abstains_on_every_citation(self):
        client = self._fake_client({"checks": []}, stop_reason="refusal")
        critic = AnthropicEvidenceCritic(client=client)
        checks = critic.check_claim("Claim.", [_ev("A#1"), _ev("B#2")])
        self.assertEqual([c.verdict for c in checks],
                         [EvidenceVerdict.INSUFFICIENT] * 2)

    def test_no_citations_no_call(self):
        client = self._fake_client({"checks": []})
        critic = AnthropicEvidenceCritic(client=client)
        self.assertEqual(critic.check_claim("Claim.", []), [])
        self.assertIsNone(client.messages.last_request)


if __name__ == "__main__":
    unittest.main()
