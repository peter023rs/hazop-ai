"""
mdl_scorecard.py — One-pass runner for the Section 4.3 model-performance
gates (Fable MDL-9..13) over a single reasoner configuration.

    python -m hazop.mdl.mdl_scorecard
    python -m hazop.mdl.mdl_scorecard --llm anthropic --workers 4
    python -m hazop.mdl.mdl_scorecard --audit-dir outputs

One worksheet run feeds every gate, so all numbers describe the same output:

  MDL-7/9   gold-standard eval    deviation coverage + cause recall
  MDL-10    grounding audit       tag references in released text vs topology
  MDL-11    fabrication proxies   + expert audit sheet (CSV/JSON) to --audit-dir
  MDL-12    latency               serial per-deviation wall time, nearest-rank P95
  MDL-13    seeded omissions      critic detection rate over seeded gaps

Exit code 0 when every AUTOMATABLE gate passes. MDL-11 never affects the
exit code: its verdict belongs to the expert audit of the generated sheet.

Honesty note (VV-1): run with --llm stub, the scorecard validates the
HARNESS — deterministic pipeline, gates, metrics plumbing. Model performance
claims require a real generator AND the held-out gold set authored by
qualified facilitators (MDL-7), independently reviewed.
"""

import argparse
import sys
from pathlib import Path

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.evidence_critic import (AnthropicEvidenceCritic,
                                                        LexicalEvidenceCritic,
                                                        LocalEvidenceCritic)
from hazop.s3_are.reasoner.llm import AnthropicLLM, LocalLLM, StubLLM
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.mdl import load_gold, evaluate_node, PUMP_VESSEL_GOLD
from hazop.mdl.fabrication import (build_fabrication_report,
                                                      write_audit_sheet_csv,
                                                      write_audit_sheet_json)
from hazop.mdl.grounding import audit_grounding
from hazop.mdl.latency import measure_latency
from hazop.mdl.seeded_omissions import run_seeded_omission_eval


def _line(gate: str, metric: str, value: str, status: str) -> str:
    return f"  {gate:<8} {metric:<28} {value:>10}   {status}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", choices=("stub", "anthropic", "local"),
                        default="stub")
    parser.add_argument("--evidence-critic",
                        choices=("lexical", "anthropic", "local", "none"),
                        default="lexical")
    parser.add_argument("--workers", type=int, default=1,
                        help="ignored for MDL-12 timing (always serial); "
                             "reserved for future split runs")
    parser.add_argument("--audit-dir", default=None,
                        help="where to write the MDL-11 expert audit sheet "
                             "(CSV+JSON); omit to skip writing")
    parser.add_argument("--sample-size", type=int, default=20,
                        help="MDL-11 audit sample size")
    parser.add_argument("--trials", type=int, default=20,
                        help="MDL-13 seeding trials")
    args = parser.parse_args(argv)

    critic = {"lexical": LexicalEvidenceCritic,
              "anthropic": AnthropicEvidenceCritic,
              "local": LocalEvidenceCritic,
              "none": lambda: None}[args.evidence_critic]()
    llm = {"stub": StubLLM, "anthropic": AnthropicLLM,
           "local": LocalLLM}[args.llm]()

    topology = build_topology()
    node = build_study_node()
    reasoner = AIReasoner(topology=topology, retriever=MockRetriever(),
                          llm=llm, grounding_required=True,
                          evidence_critic=critic)

    # One measured run feeds every gate (MDL-12 needs the serial timing).
    rows, latency = measure_latency(reasoner, node)

    gold_result = evaluate_node(rows, load_gold(PUMP_VESSEL_GOLD))
    grounding = audit_grounding(rows, topology)
    fabrication = build_fabrication_report(rows, sample_size=args.sample_size)
    omissions = run_seeded_omission_eval(node, rows, trials=args.trials)

    if args.audit_dir:
        audit_dir = Path(args.audit_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)
        write_audit_sheet_csv(fabrication, audit_dir / "mdl11_audit_sheet.csv")
        write_audit_sheet_json(fabrication, audit_dir / "mdl11_audit_sheet.json")

    print(f"Section 4.3 scorecard — node {node.node_id}, "
          f"llm={args.llm}, evidence_critic={args.evidence_critic}")
    if args.llm == "stub":
        print("(StubLLM run: numbers validate the harness, not a model — "
              "VV-1)")
    print()
    for block in (gold_result, grounding, latency, omissions, fabrication):
        print(block.summary())
        print()

    def mark(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    gates = [
        ("MDL-7", "deviation coverage",
         f"{100 * gold_result.deviation_coverage:.1f}%",
         mark(gold_result.deviation_coverage >= 0.85),
         gold_result.deviation_coverage >= 0.85),
        ("MDL-9", "cause recall",
         f"{100 * gold_result.causes.recall:.1f}%",
         mark(gold_result.causes.recall >= 0.80),
         gold_result.causes.recall >= 0.80),
        ("MDL-10", "grounding precision",
         f"{100 * grounding.precision:.1f}%",
         mark(grounding.passed), grounding.passed),
        ("MDL-11", "fabrication rate",
         f"{100 * fabrication.released_unverified_rate:.1f}%*",
         "HUMAN AUDIT", True),
        ("MDL-12", "latency P95",
         f"{latency.p95:.2f} s",
         mark(latency.passed), latency.passed),
        ("MDL-13", "omission detection",
         f"{100 * omissions.detection_rate:.1f}%",
         mark(omissions.passed), omissions.passed),
    ]
    print("Scorecard:")
    for gate, metric, value, status, _ in gates:
        print(_line(gate, metric, value, status))
    print("  * released-unverified-citation proxy; the MDL-11 verdict "
          "comes from the expert audit sheet")

    return 0 if all(ok for *_, ok in gates) else 1


if __name__ == "__main__":
    sys.exit(main())
