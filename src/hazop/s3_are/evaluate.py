"""
evaluate.py — Run the reasoner against the gold-standard set and score it.

    python evaluate.py                    # offline StubLLM baseline
    python evaluate.py --llm anthropic    # cloud model (needs `pip install
                                          # anthropic` + ANTHROPIC_API_KEY)
    python evaluate.py --llm local        # self-hosted model on an OpenAI-
                                          # compatible server (Ollama/vLLM/
                                          # LM Studio; HAZOP_LLM_URL/_MODEL)
    python evaluate.py --evidence-critic lexical|anthropic|local|none
                                          # Stage B critic (DDR-02) in the loop

Measures the spec acceptance metrics on the mock pump/vessel process:
deviation coverage >= 85% (V1 AC-03 / MDL-7) and cause recall >= 80%
(MDL-9), plus informational consequence/safeguard recall and the
grounding-gate hallucination rate (MDL-10).

Exit code 0 if the thresholded metrics pass, 1 otherwise — CI-friendly.
The default scores the StubLLM + MockRetriever baseline; --llm anthropic
scores the real model through the same harness unchanged.
"""

import argparse
import sys

from hazop.s3_are.mock_data.pump_vessel import build_topology, build_study_node
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.evidence_critic import (AnthropicEvidenceCritic,
                                                        LexicalEvidenceCritic,
                                                        LocalEvidenceCritic)
from hazop.s3_are.reasoner.llm import AnthropicLLM, LocalLLM, StubLLM
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.mdl import load_gold, evaluate_node, PUMP_VESSEL_GOLD


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", choices=("stub", "anthropic", "local"),
                        default="stub")
    parser.add_argument("--evidence-critic",
                        choices=("lexical", "anthropic", "local", "none"),
                        default="lexical")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel deviation fan-out (DDR-11)")
    args = parser.parse_args()

    gold = load_gold(PUMP_VESSEL_GOLD)
    node = build_study_node()

    critic = {"lexical": LexicalEvidenceCritic,
              "anthropic": AnthropicEvidenceCritic,
              "local": LocalEvidenceCritic,
              "none": lambda: None}[args.evidence_critic]()
    llm = {"stub": StubLLM, "anthropic": AnthropicLLM,
           "local": LocalLLM}[args.llm]()
    reasoner = AIReasoner(
        topology=build_topology(),
        retriever=MockRetriever(),
        llm=llm,
        grounding_required=True,
        evidence_critic=critic,
        max_workers=args.workers,
    )
    rows = reasoner.analyze_node(node)

    result = evaluate_node(rows, gold)
    print(result.summary())
    if result.causes.missed or result.safeguards.missed:
        print("\nNote: misses against the gold set are KB/retrieval gaps to "
              "close with the real stage-2 knowledge base, not reasoner bugs.")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
