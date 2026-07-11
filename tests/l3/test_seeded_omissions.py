"""
test_seeded_omissions.py — Tests for the MDL-13 seeded-omission harness.

Verifies the seeding math on synthetic worksheets (so the harness is trusted
independently of the reasoner), then runs the real gate: StubLLM pipeline
output + deterministic critic must detect >= 90% of seeded omissions.
"""

import random
import unittest

from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
from hazop.l4_model.seeded_omissions import (
    BLANKED_CONSEQUENCES, DROPPED_ROW, TARGET_OMISSION_DETECTION,
    OmissionEvalResult, OmissionTrial, SeededOmission,
    detected_omissions, run_seeded_omission_eval, seed_omissions,
)
from hazop.l3_reasoner.reasoner.core import AIReasoner
from hazop.l3_reasoner.reasoner.critic import CritiqueReport
from hazop.l3_reasoner.reasoner.guidewords import deviations_for_parameters
from hazop.l3_reasoner.reasoner.llm import StubLLM
from hazop.l3_reasoner.reasoner.mock_retriever import MockRetriever
from hazop.l3_reasoner.reasoner.schema import Parameter
from hazop.l3_reasoner.reasoner.worksheet import Finding, WorksheetRow


def _complete_rows(parameters=(Parameter.FLOW, Parameter.PRESSURE)):
    """Synthetic complete worksheet: every deviation has a cause + consequence."""
    rows = []
    for d in deviations_for_parameters(list(parameters)):
        rows.append(WorksheetRow(
            node_id="N",
            deviation=d,
            causes=[Finding(text=f"cause of {d.label}", confidence=0.9)],
            consequences=[Finding(text=f"consequence of {d.label}",
                                  confidence=0.9)],
        ))
    return rows


class TestSeeding(unittest.TestCase):
    def test_deterministic_under_fixed_seed(self):
        rows = _complete_rows()
        a = seed_omissions(rows, 3, random.Random(7))
        b = seed_omissions(rows, 3, random.Random(7))
        self.assertEqual(a[1], b[1])
        self.assertEqual([r.deviation.label for r in a[0]],
                         [r.deviation.label for r in b[0]])

    def test_omissions_actually_remove_content(self):
        rows = _complete_rows()
        seeded_rows, omissions = seed_omissions(rows, 4, random.Random(1))
        labels = {r.deviation.label: r for r in seeded_rows}
        for o in omissions:
            if o.kind == DROPPED_ROW:
                self.assertNotIn(o.deviation_label, labels)
            else:
                self.assertEqual(labels[o.deviation_label].consequences, [])

    def test_input_rows_never_mutated(self):
        rows = _complete_rows()
        before = [(r.deviation.label, len(r.consequences)) for r in rows]
        seed_omissions(rows, 5, random.Random(2))
        after = [(r.deviation.label, len(r.consequences)) for r in rows]
        self.assertEqual(before, after)

    def test_one_omission_per_row(self):
        rows = _complete_rows()
        _, omissions = seed_omissions(rows, len(rows), random.Random(3))
        labels = [o.deviation_label for o in omissions]
        self.assertEqual(len(labels), len(set(labels)))

    def test_blanking_only_seeded_where_consequences_exist(self):
        rows = _complete_rows()
        for r in rows[::2]:
            r.consequences = []          # half the rows already blank
        for trial in range(50):
            _, omissions = seed_omissions(rows, 4, random.Random(trial))
            blank_labels = {r.deviation.label for r in rows
                            if not r.consequences}
            for o in omissions:
                if o.kind == BLANKED_CONSEQUENCES:
                    self.assertNotIn(o.deviation_label, blank_labels)

    def test_rejects_more_omissions_than_rows(self):
        rows = _complete_rows(parameters=(Parameter.PRESSURE,))
        with self.assertRaises(ValueError):
            seed_omissions(rows, len(rows) + 1, random.Random(0))


class TestDetectionScoring(unittest.TestCase):
    def _report(self, missing=(), no_conseq=()):
        return CritiqueReport(
            node_id="N",
            missing_deviations=list(missing),
            rows_without_consequences=list(no_conseq),
            low_confidence_rows=[], unsupported_rows=[], rejected_rows=[],
        )

    def test_detection_requires_the_matching_channel(self):
        seeded = [SeededOmission(DROPPED_ROW, "More Pressure"),
                  SeededOmission(BLANKED_CONSEQUENCES, "No/Not Flow")]
        # dropped row reported only as a no-consequence row -> NOT detected
        report = self._report(missing=[], no_conseq=["More Pressure",
                                                     "No/Not Flow"])
        det = detected_omissions(report, seeded)
        self.assertEqual(det, [SeededOmission(BLANKED_CONSEQUENCES,
                                              "No/Not Flow")])

    def test_result_aggregation_and_miss_reporting(self):
        t1 = OmissionTrial(
            seeded=[SeededOmission(DROPPED_ROW, "A"),
                    SeededOmission(BLANKED_CONSEQUENCES, "B")],
            detected=[SeededOmission(DROPPED_ROW, "A")])
        result = OmissionEvalResult(node_id="N", trials=[t1])
        self.assertEqual(result.total_seeded, 2)
        self.assertEqual(result.total_detected, 1)
        self.assertEqual(result.detection_rate, 0.5)
        self.assertFalse(result.passed)
        self.assertEqual(t1.missed,
                         [SeededOmission(BLANKED_CONSEQUENCES, "B")])
        self.assertIn("MISSED", result.summary())

    def test_empty_result_is_vacuous_pass(self):
        result = OmissionEvalResult(node_id="N", trials=[])
        self.assertEqual(result.detection_rate, 1.0)
        self.assertTrue(result.passed)


class TestMdl13Gate(unittest.TestCase):
    def test_critic_detects_seeded_omissions_on_pipeline_output(self):
        # The actual MDL-13 test scenario: complete worksheet from the
        # deterministic pipeline, 20 trials x 3 seeded omissions.
        node = build_study_node()
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM())
        rows = reasoner.analyze_node(node)

        result = run_seeded_omission_eval(node, rows, trials=20,
                                          per_trial=3, seed=0)
        self.assertEqual(result.total_seeded, 60)
        self.assertGreaterEqual(result.detection_rate,
                                TARGET_OMISSION_DETECTION)
        # today's critic is deterministic: anything under 100% is a regression
        self.assertEqual(result.detection_rate, 1.0)
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
