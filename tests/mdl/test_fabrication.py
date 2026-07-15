"""
test_fabrication.py — Tests for the MDL-11 fabrication-audit harness.

Proxy counting is verified on synthetic findings with hand-built Stage B
verdicts (AI-shaped bad data); sheet generation is round-tripped through
CSV/JSON; and the pipeline integration checks the central invariant:
with Stage B on, nothing released carries an unverified citation.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.mdl.fabrication import (
    build_fabrication_report, write_audit_sheet_csv, write_audit_sheet_json,
)
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.evidence_critic import LexicalEvidenceCritic
from hazop.s3_are.reasoner.guidewords import deviations_for_parameters
from hazop.s3_are.reasoner.llm import StubLLM
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.s3_are.reasoner.schema import Parameter, RetrievedEvidence
from hazop.s3_are.reasoner.worksheet import (Finding, RejectedFinding,
                                                  WorksheetRow)


def _ev(eid: str) -> RetrievedEvidence:
    return RetrievedEvidence(source_id=eid, source_type="historical_hazop",
                             snippet=f"snippet of {eid}", score=0.8)


def _check(eid: str, verdict: str) -> dict:
    return {"evidence_id": eid, "verdict": verdict, "rationale": "r"}


def _synthetic_rows() -> list[WorksheetRow]:
    devs = deviations_for_parameters([Parameter.PRESSURE])
    more_p = next(d for d in devs if d.label == "More Pressure")
    return [WorksheetRow(
        node_id="N", deviation=more_p,
        causes=[
            # A: cited, checked, all supported, released
            Finding(text="A", confidence=0.7, evidence=[_ev("E1")],
                    evidence_checks=[_check("E1", "supported")]),
            # B: cited, checked, citation stripped as insufficient
            Finding(text="B", confidence=0.3, evidence=[],
                    evidence_checks=[_check("E2", "insufficient")]),
            # C: cited, never Stage-B-checked, released unverified
            Finding(text="C", confidence=0.7, evidence=[_ev("E3")]),
            # D: never cited anything -> outside the MDL-11 denominator
            Finding(text="D", confidence=0.3),
        ],
        rejected_findings=[
            RejectedFinding(kind="cause", text="R", confidence=0.6,
                            reason="evidence_contradicted",
                            failed_evidence=["E4"]),
            # grounding-gate rejection: not a citation failure (MDL-10 land)
            RejectedFinding(kind="cause", text="G", invalid_tags=["V-999"]),
        ],
    )]


class TestProxyCounting(unittest.TestCase):
    def setUp(self):
        self.report = build_fabrication_report(_synthetic_rows(), seed=0)

    def test_denominator_is_originally_cited_suggestions(self):
        # A, B, C + the contradicted rejection; D and the grounding
        # rejection are out
        self.assertEqual(self.report.citation_bearing, 4)

    def test_generator_failure_rate_over_stage_b_checked(self):
        # checked: A, B, refused R; failures: B (stripped) + R (refused)
        self.assertEqual(self.report.stage_b_checked, 3)
        self.assertEqual(self.report.generator_citation_failures, 2)
        self.assertAlmostEqual(
            self.report.generator_citation_failure_rate, 2 / 3)
        self.assertEqual(self.report.refused_findings, 1)

    def test_released_unverified_counts_unchecked_citations(self):
        # released with citations: A (verified), C (unchecked)
        self.assertEqual(self.report.released_citation_bearing, 2)
        self.assertEqual(self.report.released_unverified, 1)
        self.assertAlmostEqual(self.report.released_unverified_rate, 0.5)

    def test_sample_covers_released_citation_bearing_only(self):
        claims = {i.claim for i in self.report.sample}
        self.assertEqual(claims, {"A", "C"})
        self.assertTrue(all(i.sample_id.startswith("N-AUD-")
                            for i in self.report.sample))

    def test_sampling_is_deterministic(self):
        rows = _synthetic_rows()
        a = build_fabrication_report(rows, sample_size=1, seed=42)
        b = build_fabrication_report(rows, sample_size=1, seed=42)
        self.assertEqual([i.claim for i in a.sample],
                         [i.claim for i in b.sample])

    def test_empty_rows(self):
        report = build_fabrication_report([])
        self.assertEqual(report.citation_bearing, 0)
        self.assertEqual(report.generator_citation_failure_rate, 0.0)
        self.assertEqual(report.released_unverified_rate, 0.0)


class TestSheetExport(unittest.TestCase):
    def test_csv_one_row_per_citation_with_blank_human_columns(self):
        report = build_fabrication_report(_synthetic_rows(), seed=0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.csv"
            write_audit_sheet_csv(report, path)
            with open(path) as f:
                rows = list(csv.DictReader(f))
        total_citations = sum(len(i.citations) for i in report.sample)
        self.assertEqual(len(rows), total_citations)
        self.assertTrue(all(r["human_verdict"] == "" for r in rows))
        verdicts = {r["stage_b_verdict"] for r in rows}
        self.assertEqual(verdicts, {"supported", "unchecked"})

    def test_json_round_trip_carries_proxies_and_sample(self):
        report = build_fabrication_report(_synthetic_rows(), seed=0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            write_audit_sheet_json(report, path)
            with open(path) as f:
                data = json.load(f)
        self.assertEqual(data["proxies"]["citation_bearing"], 4)
        self.assertEqual(len(data["sample"]), len(report.sample))
        self.assertIn("citations", data["sample"][0])


class TestPipelineIntegration(unittest.TestCase):
    def test_stage_b_on_releases_no_unverified_citations(self):
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM(),
                              evidence_critic=LexicalEvidenceCritic())
        rows = reasoner.analyze_node(build_study_node())
        report = build_fabrication_report(rows, sample_size=10, seed=0)
        self.assertGreater(report.released_citation_bearing, 0)
        self.assertEqual(report.released_unverified, 0)
        self.assertEqual(len(report.sample), 10)

    def test_stage_b_off_flags_everything_released_as_unverified(self):
        reasoner = AIReasoner(build_topology(), MockRetriever(), StubLLM(),
                              evidence_critic=None)
        rows = reasoner.analyze_node(build_study_node())
        report = build_fabrication_report(rows, seed=0)
        self.assertEqual(report.released_unverified_rate, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
