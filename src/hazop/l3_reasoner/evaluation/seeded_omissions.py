"""
seeded_omissions.py — MDL-13 harness: seeded-omission detection by the critic.

MDL-13: the critic pass (FR-ARE-8) SHALL demonstrate detection of >= 90% of
deliberately seeded worksheet omissions in test scenarios.

This module is the measurement, not the critic: it takes a COMPLETE worksheet
(any generator — StubLLM or a real model — the harness does not care), deletes
known content from it, runs `critic.critique`, and scores how many of the
seeded omissions the critique report surfaces.

Seeded omission kinds map to the critic's detection channels:

  * dropped_row            — an entire deviation row is removed; must appear
                             in CritiqueReport.missing_deviations
  * blanked_consequences   — a row's consequences are emptied; must appear
                             in CritiqueReport.rows_without_consequences

Seeding is deterministic under a fixed seed (no LLM in the loop), so the gate
is reproducible in CI. Today's critic is deterministic and should detect 100%;
the harness exists so that any future critic change (LLM-assisted completeness,
precedent comparison, refactors) is measured against the MDL-13 gate instead
of trusted.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass

from ..reasoner.critic import CritiqueReport, critique
from ..reasoner.schema import StudyNode
from ..reasoner.worksheet import WorksheetRow

TARGET_OMISSION_DETECTION = 0.90     # Fable MDL-13

DROPPED_ROW = "dropped_row"
BLANKED_CONSEQUENCES = "blanked_consequences"


@dataclass(frozen=True)
class SeededOmission:
    """One deliberately introduced worksheet gap."""
    kind: str                 # DROPPED_ROW | BLANKED_CONSEQUENCES
    deviation_label: str      # the row the omission was seeded into


def seed_omissions(rows: list[WorksheetRow], n: int,
                   rng: random.Random) -> tuple[list[WorksheetRow],
                                                list[SeededOmission]]:
    """
    Return (seeded_rows, omissions): a deep copy of `rows` with `n` omissions
    introduced, and the ground-truth record of what was removed.

    At most one omission per row (dropping a row and blanking its
    consequences would be the same single gap to a reviewer).
    blanked_consequences is only seeded on rows that HAVE consequences —
    blanking an already-empty row removes nothing and would make the
    ground truth unscoreable.
    """
    if n > len(rows):
        raise ValueError(f"cannot seed {n} omissions into {len(rows)} rows")
    seeded_rows = copy.deepcopy(rows)
    picked = rng.sample(range(len(seeded_rows)), n)

    omissions: list[SeededOmission] = []
    dropped: set[int] = set()
    for i in picked:
        row = seeded_rows[i]
        kinds = [DROPPED_ROW]
        if row.consequences:
            kinds.append(BLANKED_CONSEQUENCES)
        kind = rng.choice(kinds)
        omissions.append(SeededOmission(kind, row.deviation.label))
        if kind == DROPPED_ROW:
            dropped.add(i)
        else:
            row.consequences = []
    return ([r for i, r in enumerate(seeded_rows) if i not in dropped],
            omissions)


def detected_omissions(report: CritiqueReport,
                       seeded: list[SeededOmission]) -> list[SeededOmission]:
    """The subset of `seeded` that the critique report surfaces, each through
    the channel its kind maps to (a dropped row flagged only as low-confidence
    would NOT count — the reviewer must be pointed at the actual gap)."""
    channel = {
        DROPPED_ROW: set(report.missing_deviations),
        BLANKED_CONSEQUENCES: set(report.rows_without_consequences),
    }
    return [o for o in seeded if o.deviation_label in channel[o.kind]]


@dataclass
class OmissionTrial:
    seeded: list[SeededOmission]
    detected: list[SeededOmission]

    @property
    def missed(self) -> list[SeededOmission]:
        found = set(self.detected)
        return [o for o in self.seeded if o not in found]


@dataclass
class OmissionEvalResult:
    node_id: str
    trials: list[OmissionTrial]

    @property
    def total_seeded(self) -> int:
        return sum(len(t.seeded) for t in self.trials)

    @property
    def total_detected(self) -> int:
        return sum(len(t.detected) for t in self.trials)

    @property
    def detection_rate(self) -> float:
        return (self.total_detected / self.total_seeded
                if self.total_seeded else 1.0)

    @property
    def passed(self) -> bool:
        return self.detection_rate >= TARGET_OMISSION_DETECTION

    def rate_by_kind(self) -> dict[str, tuple[int, int]]:
        """kind -> (detected, seeded) counts across all trials."""
        out: dict[str, list[int]] = {}
        for t in self.trials:
            for o in t.seeded:
                out.setdefault(o.kind, [0, 0])[1] += 1
            for o in t.detected:
                out.setdefault(o.kind, [0, 0])[0] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    def summary(self) -> str:
        lines = [
            f"Seeded-omission detection (MDL-13) — {self.node_id}: "
            f"{'PASS' if self.passed else 'FAIL'}",
            f"  Detection rate: {100 * self.detection_rate:.1f}% "
            f"({self.total_detected}/{self.total_seeded} across "
            f"{len(self.trials)} trials, target >= "
            f"{100 * TARGET_OMISSION_DETECTION:.0f}%)",
        ]
        for kind, (det, tot) in sorted(self.rate_by_kind().items()):
            lines.append(f"    {kind}: {det}/{tot}")
        for t in self.trials:
            for o in t.missed:
                lines.append(f"  MISSED: {o.kind} at [{o.deviation_label}]")
        return "\n".join(lines)


def run_seeded_omission_eval(node: StudyNode, rows: list[WorksheetRow],
                             trials: int = 20, per_trial: int = 3,
                             seed: int = 0) -> OmissionEvalResult:
    """Seed `per_trial` omissions into a complete worksheet `trials` times
    (fresh copy each trial), run the critic, and aggregate detection."""
    rng = random.Random(seed)
    results: list[OmissionTrial] = []
    for _ in range(trials):
        seeded_rows, seeded = seed_omissions(rows, per_trial, rng)
        report = critique(node, seeded_rows)
        results.append(OmissionTrial(
            seeded=seeded, detected=detected_omissions(report, seeded)))
    return OmissionEvalResult(node_id=node.node_id, trials=results)
