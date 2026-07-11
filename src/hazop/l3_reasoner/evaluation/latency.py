"""
latency.py — MDL-12 harness: per-deviation suggestion latency.

MDL-12: suggestion generation <= 10 s per deviation at P95 in live-session
mode (facilitation pace constraint).

The measured unit is one deviation through the FULL pipeline — retrieval,
generation, Stage A grounding gate, Stage B evidence critic, topology
safeguards — because that is what a scribe waits for in a live session.
Deviations are timed serially: the P95 target is about the pace of the
conversation, and parallel fan-out (DDR-11) hides queueing, it does not
change per-item latency.

P95 is nearest-rank (a real observed value, never an interpolation between
two runs — with the small n of a per-node run, interpolating invents times
nobody measured).

The clock is injectable so the harness itself is testable with scripted
durations; production use just calls measure_latency(reasoner, node).
Numbers measured over StubLLM validate the harness, not the model — real
MDL-12 evidence must come from runs with a real generator configured.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from ..reasoner.core import AIReasoner
from ..reasoner.guidewords import deviations_for_parameters
from ..reasoner.schema import StudyNode
from ..reasoner.worksheet import WorksheetRow

TARGET_P95_SECONDS = 10.0        # Fable MDL-12


def nearest_rank_percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest observed value such that at
    least q% of observations are <= it. `values` need not be sorted."""
    if not values:
        raise ValueError("no values to take a percentile of")
    if not 0 < q <= 100:
        raise ValueError(f"q must be in (0, 100], got {q}")
    ordered = sorted(values)
    rank = math.ceil(q / 100 * len(ordered))
    return ordered[rank - 1]


@dataclass
class LatencyResult:
    node_id: str
    labels: list[str]            # deviation labels, in run order
    timings_s: list[float]       # wall seconds per deviation, same order

    @property
    def p50(self) -> float:
        return nearest_rank_percentile(self.timings_s, 50)

    @property
    def p95(self) -> float:
        return nearest_rank_percentile(self.timings_s, 95)

    @property
    def worst(self) -> tuple[str, float]:
        i = max(range(len(self.timings_s)), key=self.timings_s.__getitem__)
        return self.labels[i], self.timings_s[i]

    @property
    def passed(self) -> bool:
        return self.p95 <= TARGET_P95_SECONDS

    def summary(self) -> str:
        worst_label, worst_t = self.worst
        lines = [
            f"Per-deviation latency (MDL-12) — {self.node_id}: "
            f"{'PASS' if self.passed else 'FAIL'}",
            f"  P95: {self.p95:.2f} s over {len(self.timings_s)} deviations "
            f"(target <= {TARGET_P95_SECONDS:.0f} s)   "
            f"P50: {self.p50:.2f} s   "
            f"worst: {worst_t:.2f} s [{worst_label}]",
        ]
        over = [(l, t) for l, t in zip(self.labels, self.timings_s)
                if t > TARGET_P95_SECONDS]
        for label, t in over:
            lines.append(f"  OVER TARGET: {t:.2f} s [{label}]")
        return "\n".join(lines)


def measure_latency(
    reasoner: AIReasoner,
    node: StudyNode,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[WorksheetRow], LatencyResult]:
    """Run every deviation of `node` serially, timing each through the full
    pipeline. Returns the worksheet rows (the run is a real run, not a dry
    one) alongside the latency result."""
    labels: list[str] = []
    timings: list[float] = []
    rows: list[WorksheetRow] = []
    for deviation in deviations_for_parameters(node.parameters):
        start = clock()
        rows.append(reasoner.analyze_deviation(node, deviation))
        timings.append(clock() - start)
        labels.append(deviation.label)
    return rows, LatencyResult(node_id=node.node_id, labels=labels,
                               timings_s=timings)
