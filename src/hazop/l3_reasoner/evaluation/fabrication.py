"""
fabrication.py — MDL-11 harness: fabrication-rate proxies + human audit sheet.

MDL-11: fabrication rate < 1% of suggestions citing evidence that does not
support the claim, measured by EXPERT AUDIT of sampled output.

The definitive MDL-11 number is human judgment — this harness cannot and
does not claim it. What it automates is everything around the human:

  * proxy metrics from Stage B verdicts already attached to the worksheet
    (evidence_checks on findings, evidence_contradicted rejections) —
    generator citation-failure tendency, and whether anything reached the
    released worksheet with citations no critic verified;
  * a deterministic sample sheet (CSV/JSON) of released citation-bearing
    suggestions for the expert audit: claim, every cited excerpt in full,
    the Stage B verdict + rationale for each, and blank human-verdict
    columns. Fixed seed -> the same sample is drawn on re-runs, so an audit
    can be interrupted and resumed against the same sheet.

Proxy semantics (worth being precise about):

  * citation_bearing        — suggestions that ORIGINALLY cited evidence
                              (evidence_checks survives stripping, and
                              refused findings are counted from the audit
                              trail), i.e. the MDL-11 denominator;
  * generator_citation_failures — of those, how many had >= 1 citation
                              Stage B judged insufficient/contradicted:
                              the raw model tendency the pilot must watch;
  * released_unverified     — released suggestions still carrying a
                              citation with no SUPPORTED verdict (Stage B
                              off or bypassed). With Stage B on this must
                              be 0: unsupporting citations are stripped
                              before release. Nonzero says the released
                              worksheet contains exactly the exposure
                              MDL-11 measures — audit everything, not a
                              sample.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from ..reasoner.worksheet import Finding, WorksheetRow

TARGET_FABRICATION_RATE = 0.01     # Fable MDL-11 — judged by human audit

_SUPPORTED = "supported"


@dataclass
class AuditCitation:
    evidence_id: str
    snippet: str
    stage_b_verdict: str        # supported | contradicted | insufficient | unchecked
    stage_b_rationale: str


@dataclass
class AuditItem:
    """One sampled suggestion for the expert sheet."""
    sample_id: str
    deviation_label: str
    kind: str                   # cause | consequence | safeguard
    claim: str
    confidence: float
    citations: list[AuditCitation] = field(default_factory=list)
    # human columns — blank on generation, filled by the auditor
    human_verdict: str = ""
    human_notes: str = ""


@dataclass
class FabricationReport:
    node_id: str
    citation_bearing: int
    stage_b_checked: int
    generator_citation_failures: int
    released_citation_bearing: int
    released_unverified: int
    refused_findings: int
    sample: list[AuditItem]

    @property
    def generator_citation_failure_rate(self) -> float:
        """Share of Stage-B-checked suggestions with >= 1 failed citation.
        Model tendency (informational) — NOT the MDL-11 released rate."""
        return (self.generator_citation_failures / self.stage_b_checked
                if self.stage_b_checked else 0.0)

    @property
    def released_unverified_rate(self) -> float:
        """Share of released citation-bearing suggestions whose citations
        no critic verified. The automated ceiling on MDL-11 exposure."""
        return (self.released_unverified / self.released_citation_bearing
                if self.released_citation_bearing else 0.0)

    def summary(self) -> str:
        def pct(x: float) -> str:
            return f"{100 * x:.1f}%"

        lines = [
            f"Fabrication-rate audit prep (MDL-11) — {self.node_id}: "
            f"HUMAN AUDIT REQUIRED (target < "
            f"{pct(TARGET_FABRICATION_RATE)} on released output)",
            f"  Citation-bearing suggestions: {self.citation_bearing} "
            f"({self.stage_b_checked} Stage-B-checked, "
            f"{self.refused_findings} refused as contradicted)",
            f"  Generator citation-failure rate: "
            f"{pct(self.generator_citation_failure_rate)} "
            f"({self.generator_citation_failures}/{self.stage_b_checked}, "
            f"informational)",
            f"  Released with unverified citations: "
            f"{pct(self.released_unverified_rate)} "
            f"({self.released_unverified}/{self.released_citation_bearing}"
            f", must be 0% when Stage B is on)",
            f"  Audit sample drawn: {len(self.sample)} suggestions",
        ]
        return "\n".join(lines)


def _originally_cited(f: Finding) -> bool:
    return bool(f.evidence_checks) or bool(f.evidence)


def _has_failed_check(f: Finding) -> bool:
    return any(c["verdict"] != _SUPPORTED for c in f.evidence_checks)


def _citations(f: Finding) -> list[AuditCitation]:
    checks = {c["evidence_id"]: c for c in f.evidence_checks}
    out = []
    for e in f.evidence:
        c = checks.get(e.source_id)
        out.append(AuditCitation(
            evidence_id=e.source_id,
            snippet=e.snippet,
            stage_b_verdict=c["verdict"] if c else "unchecked",
            stage_b_rationale=c["rationale"] if c else "",
        ))
    return out


def build_fabrication_report(rows: list[WorksheetRow], sample_size: int = 20,
                             seed: int = 0) -> FabricationReport:
    node_id = rows[0].node_id if rows else "(empty)"

    citation_bearing = 0
    stage_b_checked = 0
    generator_failures = 0
    released_bearing = 0
    released_unverified = 0
    refused = 0
    candidates: list[AuditItem] = []

    for row in rows:
        for kind, findings in (("cause", row.causes),
                               ("consequence", row.consequences),
                               ("safeguard", row.safeguards)):
            for f in findings:
                if not _originally_cited(f):
                    continue
                citation_bearing += 1
                if f.evidence_checks:
                    stage_b_checked += 1
                    if _has_failed_check(f):
                        generator_failures += 1
                if f.evidence:
                    released_bearing += 1
                    cits = _citations(f)
                    if any(c.stage_b_verdict != _SUPPORTED for c in cits):
                        released_unverified += 1
                    candidates.append(AuditItem(
                        sample_id="",           # assigned after sampling
                        deviation_label=row.deviation.label,
                        kind=kind,
                        claim=f.text,
                        confidence=f.confidence,
                        citations=cits,
                    ))
        for r in row.rejected_findings:
            if r.reason == "evidence_contradicted":
                citation_bearing += 1
                stage_b_checked += 1
                generator_failures += 1
                refused += 1

    rng = random.Random(seed)
    sample = (candidates if len(candidates) <= sample_size
              else rng.sample(candidates, sample_size))
    for i, item in enumerate(sample, start=1):
        item.sample_id = f"{node_id}-AUD-{i:03d}"

    return FabricationReport(
        node_id=node_id,
        citation_bearing=citation_bearing,
        stage_b_checked=stage_b_checked,
        generator_citation_failures=generator_failures,
        released_citation_bearing=released_bearing,
        released_unverified=released_unverified,
        refused_findings=refused,
        sample=sample,
    )


_CSV_COLUMNS = ["sample_id", "deviation", "kind", "claim", "confidence",
                "evidence_id", "evidence_snippet", "stage_b_verdict",
                "stage_b_rationale", "human_verdict", "human_notes"]


def write_audit_sheet_csv(report: FabricationReport, path: str | Path) -> None:
    """One row per (suggestion, citation) pair; the human fills the last
    two columns per suggestion (on its first row)."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for item in report.sample:
            for j, c in enumerate(item.citations):
                writer.writerow({
                    "sample_id": item.sample_id,
                    "deviation": item.deviation_label if j == 0 else "",
                    "kind": item.kind if j == 0 else "",
                    "claim": item.claim if j == 0 else "",
                    "confidence": item.confidence if j == 0 else "",
                    "evidence_id": c.evidence_id,
                    "evidence_snippet": c.snippet,
                    "stage_b_verdict": c.stage_b_verdict,
                    "stage_b_rationale": c.stage_b_rationale,
                    "human_verdict": item.human_verdict if j == 0 else "",
                    "human_notes": item.human_notes if j == 0 else "",
                })


def write_audit_sheet_json(report: FabricationReport, path: str | Path) -> None:
    payload = {
        "node_id": report.node_id,
        "target_fabrication_rate": TARGET_FABRICATION_RATE,
        "proxies": {
            "citation_bearing": report.citation_bearing,
            "stage_b_checked": report.stage_b_checked,
            "generator_citation_failures": report.generator_citation_failures,
            "generator_citation_failure_rate":
                round(report.generator_citation_failure_rate, 4),
            "released_citation_bearing": report.released_citation_bearing,
            "released_unverified": report.released_unverified,
            "released_unverified_rate":
                round(report.released_unverified_rate, 4),
            "refused_findings": report.refused_findings,
        },
        "sample": [{
            "sample_id": i.sample_id,
            "deviation": i.deviation_label,
            "kind": i.kind,
            "claim": i.claim,
            "confidence": i.confidence,
            "citations": [vars(c) for c in i.citations],
            "human_verdict": i.human_verdict,
            "human_notes": i.human_notes,
        } for i in report.sample],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
