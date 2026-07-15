"""
test_latency.py — Tests for the MDL-12 latency harness.

Percentile math and pass/fail logic are verified with scripted clocks (no
sleeping in tests); the real-clock path runs once over the StubLLM pipeline
as a smoke check that the harness measures an actual run.
"""

import unittest

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.mdl.latency import (
    TARGET_P95_SECONDS, LatencyResult, measure_latency,
    nearest_rank_percentile,
)
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.guidewords import deviations_for_parameters
from hazop.s3_are.reasoner.llm import StubLLM
from hazop.s3_are.reasoner.mock_retriever import MockRetriever


class TestPercentile(unittest.TestCase):
    def test_nearest_rank_is_an_observed_value(self):
        values = list(range(1, 21))          # 1..20
        self.assertEqual(nearest_rank_percentile(values, 95), 19)
        self.assertEqual(nearest_rank_percentile(values, 50), 10)
        self.assertEqual(nearest_rank_percentile(values, 100), 20)

    def test_single_value(self):
        self.assertEqual(nearest_rank_percentile([3.2], 95), 3.2)

    def test_unsorted_input(self):
        self.assertEqual(nearest_rank_percentile([5.0, 1.0, 3.0], 50), 3.0)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            nearest_rank_percentile([], 95)
        with self.assertRaises(ValueError):
            nearest_rank_percentile([1.0], 0)


class TestResult(unittest.TestCase):
    def _result(self, timings):
        return LatencyResult(node_id="N",
                             labels=[f"D{i}" for i in range(len(timings))],
                             timings_s=timings)

    def test_pass_when_p95_within_target(self):
        # one outlier in 20: P95 (nearest rank) is the 19th value = 2.0 s
        result = self._result([2.0] * 19 + [60.0])
        self.assertEqual(result.p95, 2.0)
        self.assertTrue(result.passed)
        self.assertEqual(result.worst, ("D19", 60.0))
        self.assertIn("OVER TARGET", result.summary())

    def test_fail_when_p95_over_target(self):
        result = self._result([TARGET_P95_SECONDS + 1.0] * 10)
        self.assertFalse(result.passed)
        self.assertIn("FAIL", result.summary())


class _ScriptedClock:
    """Returns scripted instants: start/end pairs per deviation."""

    def __init__(self, durations):
        self._times = []
        t = 0.0
        for d in durations:
            self._times.append(t)        # start
            t += d
            self._times.append(t)        # end
        self._i = 0

    def __call__(self):
        t = self._times[self._i]
        self._i += 1
        return t


class TestMeasurement(unittest.TestCase):
    def test_scripted_clock_yields_scripted_timings(self):
        node = build_study_node()
        n_dev = len(deviations_for_parameters(node.parameters))
        durations = [0.5 + 0.1 * i for i in range(n_dev)]
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM())

        rows, result = measure_latency(reasoner, node,
                                       clock=_ScriptedClock(durations))
        self.assertEqual(len(rows), n_dev)
        for got, want in zip(result.timings_s, durations):
            self.assertAlmostEqual(got, want)

    def test_real_clock_smoke_run(self):
        # Harness on the deterministic pipeline with the real clock: the
        # numbers validate the harness (stub latency is not a model claim),
        # and the run must produce the full worksheet.
        node = build_study_node()
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM())
        rows, result = measure_latency(reasoner, node)
        self.assertEqual(len(result.timings_s), len(rows))
        self.assertTrue(all(t >= 0.0 for t in result.timings_s))
        self.assertTrue(result.passed)   # stub pipeline is milliseconds


if __name__ == "__main__":
    unittest.main(verbosity=2)
