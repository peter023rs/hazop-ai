"""
test_grounding.py — Tests for the MDL-10 output tag-grounding audit.

Extraction rules are verified on synthetic text; the audit itself is
exercised with a fabricating LLM double (AI-generated bad data) to show:
grounding gate ON -> released worksheet stays >= 98% grounded; gate OFF ->
the audit catches the fabricated tag the gate would have blocked.
"""

import unittest

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.mdl.grounding import (
    TARGET_GROUNDING_PRECISION, audit_grounding, extract_tag_references,
    known_tags,
)
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.guidewords import deviations_for_parameters
from hazop.s3_are.reasoner.llm import (GeneratedFinding, GeneratedTriple,
                                            LLMInterface, StubLLM)
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.s3_are.reasoner.schema import Parameter
from hazop.s3_are.reasoner.worksheet import Finding, WorksheetRow


KNOWN = known_tags(build_topology())


class TestExtraction(unittest.TestCase):
    def test_pattern_tags_extracted(self):
        text = "Pump P-101 trips; PSV-201 lifts; LAH-2001A alarms."
        self.assertEqual(extract_tag_references(text, set()),
                         {"P-101", "PSV-201", "LAH-2001A"})

    def test_standards_citations_are_not_tags(self):
        text = "Per IEC-61882 and API-521, review relief sizing."
        self.assertEqual(extract_tag_references(text, set()), set())

    def test_hyphenated_prose_is_not_a_tag(self):
        text = "NON-RETURN valve provides SHUT-OFF on reverse flow."
        self.assertEqual(extract_tag_references(text, set()), set())

    def test_known_tag_without_digits_found_by_literal_pass(self):
        text = "Close V-SUCT before maintenance."
        self.assertEqual(extract_tag_references(text, KNOWN), {"V-SUCT"})

    def test_literal_pass_respects_token_boundaries(self):
        # "XP-101" is not a reference to P-101; "P-101B" pattern-extracts
        # as its own (ungrounded) tag rather than matching P-101 literally.
        self.assertEqual(extract_tag_references("tag XP-101x here", KNOWN),
                         set())
        self.assertEqual(extract_tag_references("vessel P-101B", KNOWN),
                         {"P-101B"})


class TestAuditScoring(unittest.TestCase):
    def _rows(self):
        devs = deviations_for_parameters([Parameter.PRESSURE])
        more_p = next(d for d in devs if d.label == "More Pressure")
        no_p = next(d for d in devs if d.label == "No/Not Pressure")
        return [
            WorksheetRow(
                node_id="NODE-1", deviation=more_p,
                causes=[Finding(text="Blocked outlet at V-201.",
                                confidence=0.9)],
                consequences=[Finding(text="Overpressure of V-201; "
                                           "PSV-201 lifts.",
                                      confidence=0.9)],
                actions=["Confirm PSV-201 set pressure."],
            ),
            WorksheetRow(
                node_id="NODE-1", deviation=no_p,
                # planted fabricated tag: V-999 is not in the topology
                causes=[Finding(text="Loss of blanket gas from V-999.",
                                confidence=0.5)],
            ),
        ]

    def test_planted_invalid_tag_is_a_violation(self):
        audit = audit_grounding(self._rows(), build_topology())
        self.assertEqual(audit.total, 5)          # V-201, V-201+PSV-201, PSV-201, V-999
        self.assertEqual(audit.grounded_count, 4)
        self.assertEqual(audit.precision, 0.8)
        self.assertFalse(audit.passed)
        [violation] = audit.violations
        self.assertEqual(violation.tag, "V-999")
        self.assertEqual(violation.kind, "cause")
        self.assertIn("V-999", audit.summary())

    def test_empty_worksheet_is_vacuous_pass(self):
        audit = audit_grounding([], build_topology())
        self.assertEqual(audit.precision, 1.0)
        self.assertTrue(audit.passed)


class _FabricatingLLM(LLMInterface):
    """AI double that behaves like a hallucinating generator: every
    deviation gets one honest finding and one citing a fabricated vessel,
    with the fabricated tag both declared AND embedded in the text."""

    def __init__(self):
        self._stub = StubLLM()

    def generate_findings(self, deviation, node_context, evidence):
        triple = self._stub.generate_findings(deviation, node_context,
                                              evidence)
        triple.causes.append(GeneratedFinding(
            text="Carryover from flash drum V-999 enters the node.",
            referenced_tags=["V-999"],
            confidence=0.8,
            evidence_ids=[],
        ))
        return triple


class TestMdl10Gate(unittest.TestCase):
    def test_gate_on_keeps_released_output_grounded(self):
        reasoner = AIReasoner(build_topology(), MockRetriever(),
                              _FabricatingLLM(), grounding_required=True)
        rows = reasoner.analyze_node(build_study_node())
        audit = audit_grounding(rows, build_topology())
        self.assertEqual(audit.precision, 1.0)
        self.assertTrue(audit.passed)
        # the fabrications were rejected into the audit trail, not released
        self.assertTrue(all(len(r.rejected_findings) >= 1 for r in rows))

    def test_gate_off_audit_catches_fabricated_tags(self):
        reasoner = AIReasoner(build_topology(), MockRetriever(),
                              _FabricatingLLM(), grounding_required=False)
        rows = reasoner.analyze_node(build_study_node())
        audit = audit_grounding(rows, build_topology())
        self.assertLess(audit.precision, TARGET_GROUNDING_PRECISION)
        self.assertFalse(audit.passed)
        self.assertIn("V-999", {v.tag for v in audit.violations})


if __name__ == "__main__":
    unittest.main(verbosity=2)
