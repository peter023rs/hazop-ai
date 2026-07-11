"""
test_evaluation.py — Tests for the gold-standard evaluation harness.

Covers the matching/metric logic on synthetic data (so the math is verified
independently of the reasoner) plus the deterministic StubLLM baseline run.
"""

import unittest

from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
from hazop.l4_model.gold import GoldItem, GoldDeviation, GoldNode, load_gold, PUMP_VESSEL_GOLD
from hazop.l4_model.metrics import (
    evaluate_node, _item_matched,
    TARGET_CAUSE_RECALL, TARGET_DEVIATION_COVERAGE,
)
from hazop.l3_reasoner.reasoner.core import AIReasoner
from hazop.l3_reasoner.reasoner.guidewords import deviations_for_parameters, Deviation
from hazop.l3_reasoner.reasoner.llm import StubLLM
from hazop.l3_reasoner.reasoner.mock_retriever import MockRetriever
from hazop.l3_reasoner.reasoner.schema import Parameter
from hazop.l3_reasoner.reasoner.worksheet import Finding, WorksheetRow


def _row(deviation: Deviation, causes: list[str]) -> WorksheetRow:
    return WorksheetRow(
        node_id="N", deviation=deviation,
        causes=[Finding(text=t, confidence=0.9) for t in causes],
    )


class TestMatching(unittest.TestCase):
    def test_group_is_all_of_within_any_of_across(self):
        item = GoldItem("x", match_any=[["pump", "trip"], ["cavitation"]])
        # both keywords of a group in ONE text -> match
        self.assertTrue(_item_matched(item, ["the pump may trip on power loss"]))
        # alternative group alone -> match
        self.assertTrue(_item_matched(item, ["Cavitation at low NPSH"]))
        # keywords split across different findings -> no match
        self.assertFalse(_item_matched(item, ["pump running", "unit trip"]))
        self.assertFalse(_item_matched(item, []))


class TestMetrics(unittest.TestCase):
    def setUp(self):
        devs = deviations_for_parameters([Parameter.FLOW])
        self.no_flow = next(d for d in devs if d.label == "No/Not Flow")
        self.rev_flow = next(d for d in devs if d.label == "Reverse Flow")

    def _gold(self) -> GoldNode:
        return GoldNode(
            node_id="N",
            description="",
            expected_deviations=["No/Not Flow", "Reverse Flow"],
            deviations={
                "No/Not Flow": GoldDeviation(causes=[
                    GoldItem("pump trip", [["pump trip"]]),
                    GoldItem("valve closed", [["valve closed"]]),
                ]),
            },
        )

    def test_coverage_and_missing_deviations(self):
        rows = [_row(self.no_flow, ["pump trip on power failure"])]
        result = evaluate_node(rows, self._gold())
        self.assertEqual(result.deviation_coverage, 0.5)
        self.assertEqual(result.missing_deviations, ["Reverse Flow"])

    def test_cause_recall_counts_matches_per_deviation_row(self):
        rows = [
            _row(self.no_flow, ["pump trip on power failure"]),
            # "valve closed" appears under the WRONG deviation -> must not count
            _row(self.rev_flow, ["suction valve closed in error"]),
        ]
        result = evaluate_node(rows, self._gold())
        self.assertEqual(result.causes.recall, 0.5)
        self.assertEqual(len(result.causes.missed), 1)
        self.assertIn("valve closed", result.causes.missed[0])

    def test_empty_gold_slot_is_full_recall(self):
        rows = [_row(self.no_flow, ["pump trip"]),
                _row(self.rev_flow, [])]
        result = evaluate_node(rows, self._gold())
        self.assertEqual(result.safeguards.recall, 1.0)   # no gold safeguards


class TestGoldFile(unittest.TestCase):
    def test_gold_labels_exist_in_generated_matrix(self):
        # Guards against typos: every label in the gold file must be a
        # deviation the guideword engine can actually generate for this node.
        gold = load_gold(PUMP_VESSEL_GOLD)
        node = build_study_node()
        valid = {d.label for d in deviations_for_parameters(node.parameters)}
        for label in gold.expected_deviations:
            self.assertIn(label, valid)
        for label in gold.deviations:
            self.assertIn(label, valid)
        self.assertEqual(gold.node_id, node.node_id)


class TestBaseline(unittest.TestCase):
    def test_stub_baseline_meets_spec_targets(self):
        # Deterministic baseline: StubLLM + MockRetriever must meet the
        # thresholded metrics on the shipped gold set. If this fails after a
        # change, either the pipeline regressed or the gold set changed.
        gold = load_gold(PUMP_VESSEL_GOLD)
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM())
        rows = reasoner.analyze_node(build_study_node())
        result = evaluate_node(rows, gold)

        self.assertGreaterEqual(result.deviation_coverage,
                                TARGET_DEVIATION_COVERAGE)
        self.assertGreaterEqual(result.causes.recall, TARGET_CAUSE_RECALL)
        self.assertTrue(result.passed)
        self.assertEqual(result.hallucination_rate, 0.0)
        # The level-cause KB gaps were closed (level precedents exist in the
        # mock KB and the stage-2 corpus); the baseline must stay at full
        # recall so any regression is a hard failure, not threshold noise.
        self.assertEqual(result.causes.missed, [])
        self.assertEqual(result.safeguards.missed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
