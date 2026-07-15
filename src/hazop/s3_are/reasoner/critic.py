"""
critic.py — Completeness critic pass (FR-ARE-8 Fable / FR-03-08 V1 gap detection).

At node close-out, compares covered deviations against the applicable
guideword x parameter matrix and reports gaps. Also surfaces low-confidence and
unsupported rows for reviewer attention. Deterministic — runs today.
"""

from __future__ import annotations

from dataclasses import dataclass

from .guidewords import deviations_for_parameters
from .schema import StudyNode
from .worksheet import WorksheetRow


@dataclass
class CritiqueReport:
    node_id: str
    missing_deviations: list[str]
    rows_without_consequences: list[str]
    low_confidence_rows: list[str]      # rows with a finding under threshold
    unsupported_rows: list[str]         # rows with an unsupported finding
    rejected_rows: list[str]            # rows where the grounding gate fired
    rejected_finding_count: int = 0     # total findings rejected (MDL-10 audit)

    @property
    def is_complete(self) -> bool:
        return not self.missing_deviations

    def summary(self) -> str:
        parts = [f"Node {self.node_id}: "
                 f"{'COMPLETE' if self.is_complete else 'GAPS FOUND'}"]
        if self.missing_deviations:
            parts.append(f"  Missing deviations ({len(self.missing_deviations)}): "
                         + ", ".join(self.missing_deviations))
        if self.rows_without_consequences:
            parts.append(f"  Rows with no consequence: "
                         + ", ".join(self.rows_without_consequences))
        if self.low_confidence_rows:
            parts.append(f"  Low-confidence rows: "
                         + ", ".join(self.low_confidence_rows))
        if self.unsupported_rows:
            parts.append(f"  Rows with unsupported findings: "
                         + ", ".join(self.unsupported_rows))
        if self.rejected_rows:
            parts.append(f"  Findings rejected by grounding gate "
                         f"({self.rejected_finding_count}) in rows: "
                         + ", ".join(self.rejected_rows))
        return "\n".join(parts)


def critique(node: StudyNode, rows: list[WorksheetRow],
             confidence_threshold: float = 0.4) -> CritiqueReport:
    expected = {d.label for d in deviations_for_parameters(node.parameters)}
    covered = {r.deviation.label for r in rows}
    missing = sorted(expected - covered)

    no_conseq, low_conf, unsupported, rejected = [], [], [], []
    rejected_count = 0
    for r in rows:
        if not r.consequences:
            no_conseq.append(r.deviation.label)
        if r.min_confidence() < confidence_threshold:
            low_conf.append(r.deviation.label)
        all_findings = r.causes + r.consequences + r.safeguards
        if any(not f.is_supported for f in all_findings):
            unsupported.append(r.deviation.label)
        if r.rejected_findings:
            rejected.append(r.deviation.label)
            rejected_count += len(r.rejected_findings)

    return CritiqueReport(
        node_id=node.node_id,
        missing_deviations=missing,
        rows_without_consequences=no_conseq,
        low_confidence_rows=low_conf,
        unsupported_rows=unsupported,
        rejected_rows=rejected,
        rejected_finding_count=rejected_count,
    )
