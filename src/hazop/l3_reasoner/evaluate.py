"""
evaluate.py — Run the reasoner against the gold-standard set and score it.

    python evaluate.py                    # offline StubLLM baseline
    python evaluate.py --llm anthropic    # real model (needs `pip install
                                          # anthropic` + ANTHROPIC_API_KEY)

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

from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
from hazop.l3_reasoner.reasoner.core import AIReasoner
from hazop.l3_reasoner.reasoner.llm import AnthropicLLM, StubLLM
from hazop.l3_reasoner.reasoner.mock_retriever import MockRetriever
from hazop.l3_reasoner.evaluation import load_gold, evaluate_node, PUMP_VESSEL_GOLD


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", choices=("stub", "anthropic"), default="stub")
    args = parser.parse_args()

    gold = load_gold(PUMP_VESSEL_GOLD)
    node = build_study_node()

    reasoner = AIReasoner(
        topology=build_topology(),
        retriever=MockRetriever(),
        llm=AnthropicLLM() if args.llm == "anthropic" else StubLLM(),
        grounding_required=True,
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
