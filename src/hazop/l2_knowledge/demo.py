"""
demo.py — Stage 2 end-to-end, wired to the REAL neighbors on both sides.

    python3 demo.py

Part A  KB ingestion + hybrid retrieval (curation gate & holdout shown live)
Part B  Stage 1 plant model -> equipment-level topology (the L1->L3 bridge)
Part C  Stage 3's real AIReasoner running with THIS retriever instead of its
        MockRetriever — first on L3's mock pump/vessel node, then on a study
        node built from the real 2401 drawing.

Offline, deterministic, no API key — same philosophy as the L3 scaffold.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
L1_MODEL = ROOT / "hazop_L1(Extraction)" / "output" / "plant_model_dexpi.json"
L3_DIR = ROOT / "hazop_L3(AI reasoner)"

from hazop.l2_knowledge.kb import KBRetriever, as_l3_retriever
from hazop.l2_knowledge.plant_graph import build_equipment_graph, load_plant_model, to_l3_topology


def part_a() -> KBRetriever:
    print("=" * 78)
    print("A. KNOWLEDGE BASE — ingest corpus, hybrid retrieval")
    print("=" * 78)
    kb = KBRetriever(HERE / "corpus")
    print(kb.report.summary())

    for query, filters in [
        ("more pressure", {"guideword": "MORE", "parameter": "pressure"}),
        ("no flow", {"guideword": "NO", "parameter": "flow"}),
        ("reverse flow", {"guideword": "REVERSE", "parameter": "flow",
                          "unit_type": "air_station"}),
    ]:
        print(f"\nquery: {query!r}  filters: {filters}")
        for ev in kb.retrieve(query, k=3, filters=filters):
            print(f"  [{ev.score:.3f}] {ev.source_id} ({ev.source_type})")
            print(f"          {ev.snippet[:100]}...")
    return kb


def part_b() -> dict:
    print("\n" + "=" * 78)
    print("B. PLANT GRAPH — stage 1 geometry -> equipment-level topology")
    print("=" * 78)
    if not L1_MODEL.exists():
        print(f"stage 1 output not found at {L1_MODEL} — skipping")
        return {}
    graph = build_equipment_graph(load_plant_model(L1_MODEL))
    s = graph["stats"]
    print(f"contracted {s['raw_piping_nodes']} piping nodes / "
          f"{s['raw_segments']} segments")
    print(f"  -> {s['terminals']} equipment-level nodes, "
          f"{s['connections']} connections "
          f"({s['isolated_terminals']} isolated)")
    print(f"  flow direction: {s['directed_connections']} connections known "
          f"(from arrows/check valves/connector text + propagation), "
          f"{s['direction_conflicts']} conflicts flagged")

    by_type: dict[str, int] = {}
    for n in graph["nodes"]:
        by_type[n["equipment_type"]] = by_type.get(n["equipment_type"], 0) + 1
    print("  node types:", ", ".join(f"{t}={c}" for t, c in sorted(by_type.items())))

    out = HERE / "output"
    out.mkdir(exist_ok=True)
    with open(out / "topology_equipment_level.json", "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)
    print(f"  written to {out / 'topology_equipment_level.json'}")

    tagged = [n for n in graph["nodes"]
              if n["equipment_type"] in ("compressor", "vessel", "tank", "pump")
              and not n["tag"].startswith("EQ-")]
    print("  sample tagged equipment:",
          ", ".join(n["tag"] for n in tagged[:8]))
    return graph


def part_c(kb: KBRetriever, graph: dict) -> None:
    print("\n" + "=" * 78)
    print("C. INTEGRATION — stage 3 reasoner running on stage 2 components")
    print("=" * 78)
    sys.path.insert(0, str(L3_DIR))
    from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
    from hazop.l3_reasoner.reasoner.core import AIReasoner
    from hazop.l3_reasoner.reasoner.critic import critique
    from hazop.l3_reasoner.reasoner.llm import StubLLM
    from hazop.l3_reasoner.reasoner.schema import Parameter, StudyNode

    retriever = as_l3_retriever(kb)

    # C1 — L3's own mock process, MockRetriever swapped for the real KB
    print("\nC1. mock pump/vessel node with the stage 2 retriever")
    reasoner = AIReasoner(topology=build_topology(), retriever=retriever,
                          llm=StubLLM(), grounding_required=True)
    rows = reasoner.analyze_node(build_study_node())
    with_ev = sum(1 for r in rows for f in r.causes + r.consequences + r.safeguards
                  if f.evidence)
    print(f"  {len(rows)} deviations analyzed; "
          f"{with_ev} findings carry KB evidence")
    sample = next(r for r in rows
                  if r.deviation.guideword.name == "MORE"
                  and r.deviation.parameter.name == "PRESSURE")
    print("  sample row [More Pressure] causes:")
    for c in sample.causes:
        ev = ", ".join(e.source_id for e in c.evidence) or "none"
        print(f"    - {c.text}  [evidence: {ev}]")

    # C2 — a study node on the REAL 2401 drawing via the plant-graph adapter
    if not graph:
        return
    print("\nC2. real 2401 unit: compressor train study node")
    topo = to_l3_topology(graph)
    members = [n.tag for n in topo.nodes
               if n.equipment_type.value in ("compressor", "vessel")
               and not n.tag.startswith("EQ-")][:5]
    node = StudyNode(
        node_id="2401-NODE-1",
        description="Air compression train (from digitized P&ID, unit 2401)",
        equipment_tags=members,
        parameters=[Parameter.FLOW, Parameter.PRESSURE, Parameter.TEMPERATURE],
        design_intent="Compress atmospheric air and deliver dry compressed "
                      "air to the plant/instrument air headers.",
    )
    print(f"  node members: {members}")
    reasoner = AIReasoner(topology=topo, retriever=retriever,
                          llm=StubLLM(), grounding_required=True)
    rows = reasoner.analyze_node(node)
    report = critique(node, rows)
    print(f"  {len(rows)} deviations analyzed on the real topology")
    print("  critic: " + report.summary().splitlines()[0])
    n_known = sum(1 for e in graph["edges"]
                  if e["attributes"]["direction"] == "known")
    print(f"  NOTE: {n_known}/{len(graph['edges'])} connections carry real "
          "flow direction (stage 1 arrows/check valves/connector text + "
          "conservation); the L3 topology reasoner treats the rest as "
          "unverified — traversable both ways, flagged, and never behind a "
          "high-confidence safeguard claim.")


if __name__ == "__main__":
    kb = part_a()
    graph = part_b()
    part_c(kb, graph)
