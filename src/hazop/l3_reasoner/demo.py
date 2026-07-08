"""
demo.py — Run the AI Reasoner end-to-end on the mock pump/vessel process.

    python demo.py

No API key, no stage 1/2 pipeline required. Everything runs on mock data +
stub LLM, exercising the real guideword / topology / grounding / critic logic.
"""

import json

from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
from hazop.l3_reasoner.reasoner.core import AIReasoner
from hazop.l3_reasoner.reasoner.critic import critique
from hazop.l3_reasoner.reasoner.mock_retriever import MockRetriever
from hazop.l3_reasoner.reasoner.llm import StubLLM


def main():
    topology = build_topology()
    node = build_study_node()

    reasoner = AIReasoner(
        topology=topology,
        retriever=MockRetriever(),
        llm=StubLLM(),
        grounding_required=True,
    )

    rows = reasoner.analyze_node(node)

    print("=" * 78)
    print(f"HAZOP WORKSHEET — {node.node_id}: {node.description}")
    print(f"Design intent: {node.design_intent}")
    print("=" * 78)

    def fmt(f: dict) -> str:
        src = ", ".join(f["evidence"]) if f["evidence"] else (
            "topology" if f["topology_grounded"] else "none")
        return f"{f['text']}  [conf {f['confidence']}, evidence: {src}]"

    for r in rows:
        d = r.to_dict()
        print(f"\n[{d['deviation']}]  (min_conf={d['min_confidence']}, "
              f"provenance={d['provenance']})")
        print(f"  Causes:")
        for c in d["causes"]:
            print(f"    - {fmt(c)}")
        print(f"  Consequences:")
        for c in d["consequences"]:
            print(f"    - {fmt(c)}")
        print(f"  Safeguards:")
        for s in d["safeguards"]:
            print(f"    - {fmt(s)}")
        for rej in d["rejected_findings"]:
            print(f"  REJECTED ({rej['kind']}, invalid tags "
                  f"{rej['invalid_tags']}): {rej['text']}")
        print(f"  Risk rating: severity={d['severity']} likelihood={d['likelihood']} "
              f"(human-only field, intentionally blank)")

    print("\n" + "=" * 78)
    print("COMPLETENESS CRITIC")
    print("=" * 78)
    report = critique(node, rows)
    print(report.summary())

    print(f"\nTotal deviations analyzed: {len(rows)}")

    # also show the JSON export shape (for stage 5 report generation)
    with open("worksheet_output.json", "w") as f:
        json.dump([r.to_dict() for r in rows], f, indent=2)
    print("Full worksheet written to worksheet_output.json")


if __name__ == "__main__":
    main()
