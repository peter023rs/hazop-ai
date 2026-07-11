"""
demo.py — Stage 2 end-to-end, wired to the REAL neighbors on both sides.

    python3 demo.py

Part A  KB ingestion + hybrid retrieval (curation gate & holdout shown live)
Part B  Stage 1 plant model -> equipment-level topology (the L1->L3 bridge)
Part C  Stage 3's real AIReasoner running with THIS retriever instead of its
        MockRetriever — first on L3's mock pump/vessel node, then on a study
        node built from the real 2401 drawing.
Part D  Process Model Layer: HAZOP node boundary proposal on the real
        drawing (FR-PML-2) + deviation screening through the simulator
        seam's heuristic fallback (FR-PML-3/4/5).

Offline, deterministic, no API key — same philosophy as the L3 scaffold.
"""

import json
import os
from pathlib import Path

# Runtime data ships with the repo under data/ (override with HAZOP_DATA,
# e.g. the Docker image sets HAZOP_DATA=/app/data).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]               # .../hazop-ai
DATA_DIR = Path(os.environ.get("HAZOP_DATA", REPO_ROOT / "data"))
CORPUS_DIR = DATA_DIR / "corpus"
L1_MODEL = DATA_DIR / "l1_output" / "plant_model_dexpi.json"
OUT_DIR = REPO_ROOT / "outputs"

from hazop.l2_knowledge.kb import KBRetriever, as_l3_retriever
from hazop.l2_knowledge.plant_graph import build_equipment_graph, load_plant_model, to_l3_topology


def part_a() -> KBRetriever:
    print("=" * 78)
    print("A. KNOWLEDGE BASE — ingest corpus, hybrid retrieval")
    print("=" * 78)
    kb = KBRetriever(CORPUS_DIR)
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
          f"(arrows/check valves/PSV orientation/connector text + "
          f"propagation incl. {s['l2_passthrough_forced']} L2 pass-through), "
          f"{s['direction_conflicts']} conflicts flagged")

    by_type: dict[str, int] = {}
    for n in graph["nodes"]:
        by_type[n["equipment_type"]] = by_type.get(n["equipment_type"], 0) + 1
    print("  node types:", ", ".join(f"{t}={c}" for t, c in sorted(by_type.items())))

    OUT_DIR.mkdir(exist_ok=True)
    with open(OUT_DIR / "topology_equipment_level.json", "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)
    print(f"  written to {OUT_DIR / 'topology_equipment_level.json'}")

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
    from hazop.l3_reasoner.mock_data.pump_vessel import build_topology, build_study_node
    from hazop.l3_reasoner.reasoner.core import AIReasoner
    from hazop.l3_reasoner.reasoner.critic import critique
    from hazop.l3_reasoner.reasoner.evidence_critic import LexicalEvidenceCritic
    from hazop.l3_reasoner.reasoner.llm import StubLLM
    from hazop.l3_reasoner.reasoner.schema import Parameter, StudyNode

    retriever = as_l3_retriever(kb)

    # C1 — L3's own mock process, MockRetriever swapped for the real KB
    print("\nC1. mock pump/vessel node with the stage 2 retriever")
    reasoner = AIReasoner(topology=build_topology(), retriever=retriever,
                          llm=StubLLM(), grounding_required=True,
                          evidence_critic=LexicalEvidenceCritic())
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

    # node-scoped condensed context view — the only topology text the
    # generation layer should see (Delft-style condensing)
    from hazop.l2_knowledge.plant_graph import (condensed_node_view, render_text,
                                                token_estimate)
    view = condensed_node_view(graph, members, hops=1)
    text = render_text(view)
    print(f"  condensed node view: {len(view['members'])} members, "
          f"{len(view['neighbors'])} neighbors, "
          f"{len(view['connections'])} connections — "
          f"~{token_estimate(graph)} tokens (full graph) -> "
          f"~{token_estimate(text)} tokens (view)")

    reasoner = AIReasoner(topology=topo, retriever=retriever,
                          llm=StubLLM(), grounding_required=True,
                          evidence_critic=LexicalEvidenceCritic())
    rows = reasoner.analyze_node(node)
    report = critique(node, rows)
    print(f"  {len(rows)} deviations analyzed on the real topology")
    print("  critic: " + report.summary().splitlines()[0])
    n_known = sum(1 for e in graph["edges"]
                  if e["attributes"]["direction"] == "known")
    print(f"  NOTE: {n_known}/{len(graph['edges'])} connections carry real "
          "flow direction (stage 1 arrows/check valves/PSV orientation/"
          "connector text + conservation, plus L2 pass-through); the L3 "
          "topology reasoner treats the rest as unverified — traversable "
          "both ways, flagged, and never behind a high-confidence "
          "safeguard claim.")


def part_d(graph: dict) -> None:
    print("\n" + "=" * 78)
    print("D. PROCESS MODEL LAYER — node proposal + deviation screening")
    print("=" * 78)
    if not graph:
        print("no plant graph — skipping")
        return
    from hazop.l2_knowledge.plant_graph import (ScreeningCase, propose_nodes,
                                                screen_case)

    # D1 — FR-PML-2: propose HAZOP node boundaries at design-intent changes
    proposal = propose_nodes(graph)
    s = proposal["stats"]
    print(f"\nD1. {s['proposed_nodes']} HAZOP nodes proposed covering "
          f"{s['equipment_assigned']} equipment items "
          f"({s['unassigned']} isolated/instrument-only items left for the "
          f"facilitator)")
    print(f"  boundaries: {s['pressure_break_boundaries']} pressure breaks, "
          f"{s['phase_change_boundaries']} phase changes, "
          f"{s['unit_boundaries']} unit (off-page) boundaries")
    for p in proposal["nodes"][:3]:
        print(f"  {p['node_id']} [{p['status']}] {p['description']}: "
              f"{', '.join(p['members'])}")
        print(f"      {p['rationale'][0]}")
    print(f"  ({proposal['note']})")

    # D2 — FR-PML-5 heuristic screening behind the FR-PML-3 simulator seam
    print("\nD2. deviation screening — no simulator wired in, so the "
          "heuristic fallback answers:")
    compressor = next((n["tag"] for n in graph["nodes"]
                       if n["equipment_type"] == "compressor"), None)
    for case in [
        ScreeningCase("D2-PUMP-DEADHEAD", "P-901", "pump", "blocked_outlet",
                      {"normal_discharge_pressure": 8.0}),
        ScreeningCase("D2-K-BLOCKED", compressor or "K-901", "compressor",
                      "blocked_outlet", {}),
    ]:
        r = screen_case(case)
        print(f"  {case.equipment_tag} / {case.deviation}: "
              f"{r.estimate or 'qualitative only'}")
        print(f"      [{r.label}] reliable={r.reliable}")
        print(f"      basis: {r.basis[:100]}...")


if __name__ == "__main__":
    kb = part_a()
    graph = part_b()
    part_c(kb, graph)
    part_d(graph)
