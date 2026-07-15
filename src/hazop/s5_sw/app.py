"""
hazop.s5_sw.app — integrated dashboard over the HAZOP-AI subsystems (SW, stage 5).

    stage 1  hazop.s1_dim    P&ID -> topology (sheet overlays, stats)
    stage 2  hazop.s2_pml    plant graph, node proposal, screening
    stage 3  hazop.s3_are    guideword reasoning -> HAZOP worksheet
    stage 4  hazop.s4_kb     curated KB hybrid retrieval

Everything is served live from those subsystems — the plant graph is
contracted from DIM output at startup, KB queries run the real hybrid
retriever, and the worksheet tab runs the real AIReasoner (offline StubLLM)
on either the mock process or the digitized 2401 compressor train.

Run:    python -m hazop.s5_sw.app   (or `hazop-web` after pip install)
Open:   http://127.0.0.1:8780
Env:    HAZOP_HOST / HAZOP_PORT / HAZOP_DATA
"""

import json
import os
import socket
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from hazop.s4_kb import KBRetriever, as_l3_retriever
from hazop.s2_pml import (GraphQuery, QueryError, build_equipment_graph,
                          load_plant_model, query_examples, run_cypher,
                          to_l3_topology)
from hazop.s2_pml.neo4j_store import _label as _cypher_label
from hazop.s3_are.mock_data.pump_vessel import (build_study_node,
                                                     build_topology)
from hazop.s3_are.reasoner.core import AIReasoner
from hazop.s3_are.reasoner.critic import critique
from hazop.s3_are.reasoner.evidence_critic import LexicalEvidenceCritic
from hazop.s3_are.reasoner.llm import StubLLM
from hazop.s3_are.reasoner.mock_retriever import MockRetriever
from hazop.s3_are.reasoner.schema import Parameter, StudyNode
from hazop.s3_are.reasoner.topology import TopologyReasoner
from hazop.mdl import (PUMP_VESSEL_GOLD, evaluate_node,
                                          load_gold)
from hazop.mdl.fabrication import build_fabrication_report
from hazop.mdl.grounding import audit_grounding
from hazop.mdl.latency import (TARGET_P95_SECONDS,
                                                  measure_latency)
from hazop.mdl.seeded_omissions import (
    TARGET_OMISSION_DETECTION, run_seeded_omission_eval)
from hazop.s6_rcm.rtm import rtm_view, update_requirement
from hazop.s6_rcm.rtm import RTM_PATH as _DEFAULT_RTM_PATH
from hazop.mdl.telemetry import SuggestionEvent, TelemetryLog
from hazop.s5_sw import llm_lab
from hazop.s5_sw.llm_lab import BENCHMARKS, LabError, RunManager

# Runtime data ships with the repo under data/ (override with HAZOP_DATA,
# e.g. the Docker image sets HAZOP_DATA=/app/data).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]               # .../hazop-ai
DATA_DIR = Path(os.environ.get("HAZOP_DATA", REPO_ROOT / "data"))
L1_OUT = DATA_DIR / "l1_output"
CORPUS_DIR = DATA_DIR / "corpus"

app = Flask(__name__)

PAGES = list(range(4, 13))          # process sheets of the 2401 drawing


# --------------------------------------------------------------------------
# shared state, built once on first use
# --------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.plant_model = load_plant_model(L1_OUT / "plant_model_dexpi.json")
        self.graph = build_equipment_graph(self.plant_model)
        self.topology = to_l3_topology(self.graph)
        self.topo_reasoner = TopologyReasoner(self.topology)
        self.kb = KBRetriever(CORPUS_DIR)
        self.retriever = as_l3_retriever(self.kb)
        self.node_by_tag = {n["tag"]: n for n in self.graph["nodes"]}
        self.graph_query = GraphQuery(self.graph)
        self.query_examples = query_examples(self.graph)


_state = None


def state() -> _State:
    global _state
    if _state is None:
        _state = _State()
    return _state


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _finding(f) -> dict:
    return {
        "text": f.text,
        "confidence": round(f.confidence, 3),
        "supported": f.is_supported,
        "topology_grounded": f.topology_grounded,
        "evidence": [{"source_id": e.source_id, "source_type": e.source_type,
                      "score": round(e.score, 3), "snippet": e.snippet}
                     for e in f.evidence],
    }


def _rows(rows) -> list:
    return [{
        "deviation": r.deviation.label,
        "guideword": r.deviation.guideword.name,
        "parameter": r.deviation.parameter.value,
        "causes": [_finding(f) for f in r.causes],
        "consequences": [_finding(f) for f in r.consequences],
        "safeguards": [_finding(f) for f in r.safeguards],
        "rejected": [x.to_dict() for x in r.rejected_findings],
    } for r in rows]


def _real_2401_node() -> StudyNode:
    st = state()
    members = [n.tag for n in st.topology.nodes
               if n.equipment_type.value in ("compressor", "vessel")
               and not n.tag.startswith("EQ-")][:5]
    return StudyNode(
        node_id="2401-NODE-1",
        description="Air compression train (from digitized P&ID, unit 2401)",
        equipment_tags=members,
        parameters=[Parameter.FLOW, Parameter.PRESSURE, Parameter.TEMPERATURE],
        design_intent="Compress atmospheric air and deliver dry compressed "
                      "air to the plant/instrument air headers.",
    )


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# APIs
# --------------------------------------------------------------------------

@app.route("/api/overview")
def api_overview():
    st = state()
    cm = st.plant_model["conceptualModel"]
    segs = cm["PipingNetworkSegment"]
    directed = sum(1 for s in segs if s.get("flowDirection"))
    src_counts = {}
    for s in segs:
        source = s.get("flowDirectionSource")
        if source:
            src_counts[source] = src_counts.get(source, 0) + 1
    valve_classes = {}
    for p in cm["PipingComponent"]:
        valve_classes[p["componentClass"]] = \
            valve_classes.get(p["componentClass"], 0) + 1
    gstats = st.graph["stats"]
    types = {}
    for n in st.graph["nodes"]:
        types[n["equipment_type"]] = types.get(n["equipment_type"], 0) + 1

    return jsonify({
        "l1": {
            "instruments": len(cm["ProcessInstrumentationFunction"]),
            "piping_components": len(cm["PipingComponent"]),
            "valve_classes": valve_classes,
            "equipment": len(cm["Equipment"]),
            "segments": len(segs),
            "directed_segments": directed,
            "direction_sources": src_counts,
            "pages": PAGES,
        },
        "l2": {
            "stats": gstats,
            "node_types": types,
            "kb_report": st.kb.report.summary(),
            "neo4j_up": _port_open(7474),
        },
        "l3": {
            "deviation_matrix": "IEC 61882 guideword x parameter",
            "llm": "StubLLM (offline, deterministic) — AnthropicLLM available "
                   "behind the same seam",
        },
    })


@app.route("/api/l1/pages")
def api_l1_pages():
    out = []
    for page in PAGES:
        path = L1_OUT / f"topology_page{page}.json"
        if not path.exists():
            continue
        topo = json.loads(path.read_text())
        kinds = {}
        for n in topo["nodes"]:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        ds = topo.get("direction_stats", {})
        out.append({
            "page": page,
            "sheet_prefix": topo.get("sheet_prefix", ""),
            "nodes": len(topo["nodes"]),
            "kinds": kinds,
            "edges": len(topo["edges"]),
            "connectors": len(topo.get("connectors", [])),
            "directed": ds.get("directed", 0),
            "pipe_runs": ds.get("pipe_runs", 0),
        })
    return jsonify(out)


@app.route("/l1/overlay/<int:page>")
def l1_overlay(page):
    path = L1_OUT / f"topology_page{page}_overlay.png"
    if not path.exists():
        abort(404)
    return send_file(path)


@app.route("/api/graph")
def api_graph():
    st = state()
    nodes = [{"data": {
        "id": n["tag"],
        "type": n["equipment_type"],
        "name": n["name"],
        "confidence": n["detection_confidence"],
        "sheets": n["attributes"].get("sheets", []),
    }} for n in st.graph["nodes"]]
    edges = [{"data": {
        "id": f"e{i}",
        "source": e["source"],
        "target": e["target"],
        "direction": e["attributes"]["direction"],
        "sources": e["attributes"].get("direction_sources", []),
        "lines": e["attributes"].get("line_numbers", []),
        "note": e["attributes"].get("direction_note", ""),
    }} for i, e in enumerate(st.graph["edges"])]
    return jsonify({"nodes": nodes, "edges": edges,
                    "stats": st.graph["stats"]})


# Graph Explorer (IYP-style query experience over the plant graph):
# natural-language questions run in-process against the equipment graph;
# raw Cypher is passed through read-only to a live Neo4j when one is up.
NEO4J_URI = os.environ.get("HAZOP_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HAZOP_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HAZOP_NEO4J_PASSWORD", "hazop2401")
NEO4J_DATABASE = os.environ.get("HAZOP_NEO4J_DATABASE", "neo4j")


@app.route("/api/query/meta")
def api_query_meta():
    st = state()
    by_type: dict[str, int] = {}
    for n in st.graph["nodes"]:
        by_type[n["equipment_type"]] = by_type.get(n["equipment_type"], 0) + 1
    stats = st.graph["stats"]
    flows = stats.get("directed_connections", 0)
    return jsonify({
        "labels": [{"label": _cypher_label(t), "type": t, "count": c}
                   for t, c in sorted(by_type.items(),
                                      key=lambda kv: -kv[1])],
        "relationships": [
            {"type": "FLOWS_TO", "count": flows,
             "note": "verified flow direction"},
            {"type": "CONNECTED_TO",
             "count": stats.get("connections", 0) - flows,
             "note": "drawing order only"}],
        "examples": st.query_examples,
        "neo4j_up": _port_open(7474),
        "neo4j_browser": "http://localhost:7474",
    })


@app.route("/api/query", methods=["POST"])
def api_query():
    payload = request.get_json(force=True, silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    try:
        return jsonify(state().graph_query.ask(question).to_dict())
    except QueryError as err:
        return jsonify({"error": err.hint}), 400


@app.route("/api/cypher", methods=["POST"])
def api_cypher():
    payload = request.get_json(force=True, silent=True) or {}
    cypher = (payload.get("query") or "").strip()
    if not cypher:
        return jsonify({"error": "empty query"}), 400
    try:
        out = run_cypher(cypher, uri=NEO4J_URI, user=NEO4J_USER,
                         password=NEO4J_PASSWORD, database=NEO4J_DATABASE)
    except QueryError as err:
        return jsonify({"error": err.hint}), 400
    except Exception as err:      # driver missing / server down / bad auth
        return jsonify({
            "error": f"live Neo4j unavailable ({err}). Start the server "
                     f"and load the graph with: python -m "
                     f"hazop.s2_pml.load_neo4j --load"}), 503
    return jsonify(out)


@app.route("/api/trace")
def api_trace():
    st = state()
    tag = request.args.get("tag", "")
    node = st.node_by_tag.get(tag)
    if node is None:
        abort(404)

    def split(detail):
        verified = sorted(t for t, ok in detail.items() if ok)
        unverified = sorted(t for t, ok in detail.items() if not ok)
        return {"verified": verified, "unverified": unverified}

    return jsonify({
        "node": node,
        "downstream": split(st.topo_reasoner.downstream_detail(tag)),
        "upstream": split(st.topo_reasoner.upstream_detail(tag)),
        "has_relief_path": st.topo_reasoner.has_relief_path(tag),
        "has_relief_path_drawn": st.topo_reasoner.has_relief_path(
            tag, verified_only=False),
    })


@app.route("/api/kb/search")
def api_kb_search():
    st = state()
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    filters = {}
    for key in ("guideword", "parameter"):
        value = request.args.get(key, "").strip()
        if value:
            filters[key] = value
    results = st.kb.retrieve(query, k=8, filters=filters or None)
    return jsonify([{
        "source_id": e.source_id,
        "source_type": e.source_type,
        "score": round(e.score, 3),
        "snippet": e.snippet,
    } for e in results])


@app.route("/api/kb/corpus")
def api_kb_corpus():
    docs = []
    for path in sorted((CORPUS_DIR).glob("*.json")):
        doc = json.loads(path.read_text())
        docs.append({
            "file": path.name,
            "doc_id": doc.get("doc_id", "?"),
            "title": doc.get("title", ""),
            "source_type": doc.get("source_type", ""),
            "curation": doc.get("curation", ""),
            "holdout": bool(doc.get("holdout")),
            "chunks": len(doc.get("chunks", [])),
        })
    return jsonify(docs)


@app.route("/api/worksheet")
def api_worksheet():
    st = state()
    which = request.args.get("node", "mock")
    if which == "real2401":
        topology, node = st.topology, _real_2401_node()
    else:
        topology, node = build_topology(), build_study_node()
    reasoner = AIReasoner(topology=topology, retriever=st.retriever,
                          llm=StubLLM(), grounding_required=True)
    rows = reasoner.analyze_node(node)
    report = critique(node, rows)
    n_findings = sum(len(r.causes) + len(r.consequences) + len(r.safeguards)
                     for r in rows)
    return jsonify({
        "node": {
            "node_id": node.node_id,
            "description": node.description,
            "equipment_tags": node.equipment_tags,
            "design_intent": node.design_intent,
            "parameters": [p.value for p in node.parameters],
        },
        "rows": _rows(rows),
        "critic": report.summary(),
        "totals": {"deviations": len(rows), "findings": n_findings},
    })


def _run_eval(retriever) -> dict:
    gold = load_gold(PUMP_VESSEL_GOLD)
    reasoner = AIReasoner(topology=build_topology(), retriever=retriever,
                          llm=StubLLM(), grounding_required=True)
    rows = reasoner.analyze_node(build_study_node())
    result = evaluate_node(rows, gold)
    return {
        "passed": result.passed,
        "deviation_coverage": round(result.deviation_coverage, 3),
        "cause_recall": round(result.causes.recall, 3),
        "consequence_recall": round(result.consequences.recall, 3),
        "safeguard_recall": round(result.safeguards.recall, 3),
        "hallucination_rate": round(result.hallucination_rate, 3),
        "missed": (result.causes.missed + result.consequences.missed
                   + result.safeguards.missed),
    }


@app.route("/api/eval")
def api_eval():
    return jsonify({
        "baseline": _run_eval(MockRetriever()),
        "integrated": _run_eval(state().retriever),
    })


@app.route("/api/scorecard")
def api_scorecard():
    """Section 4.3 model-performance gates (MDL-7..13) in one measured run —
    the web view of hazop.mdl.mdl_scorecard. `retriever=mock` scores
    the L3 baseline; `retriever=kb` scores the integrated Stage-2 KB."""
    which = request.args.get("retriever", "mock")
    retriever = MockRetriever() if which == "mock" else state().retriever
    topology, node = build_topology(), build_study_node()
    reasoner = AIReasoner(topology=topology, retriever=retriever,
                          llm=StubLLM(), grounding_required=True,
                          evidence_critic=LexicalEvidenceCritic())

    rows, latency = measure_latency(reasoner, node)
    gold_result = evaluate_node(rows, load_gold(PUMP_VESSEL_GOLD))
    grounding = audit_grounding(rows, topology)
    fabrication = build_fabrication_report(rows, sample_size=8)
    omissions = run_seeded_omission_eval(node, rows, trials=20)

    def gate(gid, name, display, status, target, detail=None):
        return {"id": gid, "name": name, "display": display,
                "status": status, "target": target, "detail": detail or []}

    def mark(ok):
        return "pass" if ok else "fail"

    pct = lambda x: f"{100 * x:.1f}%"
    by_kind = omissions.rate_by_kind()
    gates = [
        gate("MDL-7", "deviation coverage",
             pct(gold_result.deviation_coverage),
             mark(gold_result.deviation_coverage >= 0.85), ">= 85%",
             [f"missing: {m}" for m in gold_result.missing_deviations]),
        gate("MDL-9", "cause recall",
             pct(gold_result.causes.recall),
             mark(gold_result.causes.recall >= 0.80), ">= 80%",
             [f"missed: {m}" for m in gold_result.causes.missed]),
        gate("MDL-10", "grounding precision",
             pct(grounding.precision), mark(grounding.passed), ">= 98%",
             [f"ungrounded {v.tag} in {v.kind} of [{v.deviation_label}]"
              for v in grounding.violations]),
        gate("MDL-11", "fabrication rate",
             pct(fabrication.released_unverified_rate) + " proxy",
             "human_audit", "< 1% by expert audit",
             [f"citation-bearing: {fabrication.citation_bearing}",
              f"generator citation-failure rate: "
              f"{pct(fabrication.generator_citation_failure_rate)} "
              f"({fabrication.generator_citation_failures}"
              f"/{fabrication.stage_b_checked} Stage-B-checked)",
              f"refused as contradicted: {fabrication.refused_findings}",
              f"released with unverified citations: "
              f"{fabrication.released_unverified}"
              f"/{fabrication.released_citation_bearing}"]),
        gate("MDL-12", "latency P95",
             f"{latency.p95:.2f} s", mark(latency.passed),
             f"<= {TARGET_P95_SECONDS:.0f} s",
             [f"P50 {latency.p50:.3f} s",
              f"worst {latency.worst[1]:.3f} s [{latency.worst[0]}]"]),
        gate("MDL-13", "omission detection",
             pct(omissions.detection_rate), mark(omissions.passed),
             f">= {100 * TARGET_OMISSION_DETECTION:.0f}%",
             [f"{k}: {det}/{tot}" for k, (det, tot)
              in sorted(by_kind.items())]),
    ]

    return jsonify({
        "meta": {
            "node_id": node.node_id,
            "retriever": which,
            "llm": "StubLLM (deterministic)",
            "evidence_critic": "lexical",
            "note": "StubLLM run: numbers validate the measurement harness, "
                    "not a model (VV-1). Same gates rerun unchanged against "
                    "a real generator.",
        },
        "gates": gates,
        "latency": {"labels": latency.labels,
                    "timings_ms": [round(1000 * t, 2)
                                   for t in latency.timings_s]},
        "audit_sample": [{
            "sample_id": i.sample_id,
            "deviation": i.deviation_label,
            "kind": i.kind,
            "claim": i.claim,
            "confidence": i.confidence,
            "citations": [{"evidence_id": c.evidence_id,
                           "snippet": c.snippet,
                           "verdict": c.stage_b_verdict,
                           "rationale": c.stage_b_rationale}
                          for c in i.citations],
        } for i in fabrication.sample],
    })


# Accept/edit/reject telemetry (MDL-14 / FR-SW-2). Module-level so tests
# can point it at a temp file; one JSONL under the data dir by default.
TELEMETRY_PATH = DATA_DIR / "telemetry" / "suggestion_events.jsonl"


@app.route("/api/telemetry", methods=["POST"])
def api_telemetry_record():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        event = SuggestionEvent(**payload)
    except (TypeError, ValueError) as err:
        # schema violations are the caller's bug; never half-record
        return jsonify({"error": str(err)}), 400
    TelemetryLog(TELEMETRY_PATH).record(event)
    return jsonify(event.to_dict()), 201


@app.route("/api/telemetry", methods=["GET"])
def api_telemetry_summary():
    s = TelemetryLog(TELEMETRY_PATH).summarize()
    return jsonify({"total": s.total, "by_action": s.by_action,
                    "by_kind": s.by_kind})


# LLM Lab (multi-device local-model benchmarking). Module-level paths so
# tests can repoint them; the YAML matrix is the single source of truth.
LAB_CONFIG_PATH = DATA_DIR / "llm_lab" / "llm_lab.yaml"
LAB_RUNS_PATH = DATA_DIR / "llm_lab" / "runs.jsonl"
lab_runs = RunManager(LAB_RUNS_PATH)


@app.route("/api/lab/config")
def api_lab_config():
    try:
        config = llm_lab.load_config(LAB_CONFIG_PATH)
    except LabError as err:
        return jsonify({"error": err.args[0]}), 500
    busy = set(lab_runs.busy_devices())
    devices = []
    for device in config.devices:
        status = llm_lab.device_status(device)
        installed = {m["name"] for m in status["installed"]}
        devices.append({
            "name": device.name, "base_url": device.base_url,
            "hardware": device.hardware, "busy": device.name in busy,
            **status,
            "candidates": [{
                "name": m.name, "role": m.role,
                "installed": m.name in installed
                or m.name.split(":")[0] in {i.split(":")[0]
                                            for i in installed},
            } for m in config.candidates(device.name)],
        })
    return jsonify({"devices": devices,
                    "benchmarks": list(BENCHMARKS),
                    "defaults": config.defaults,
                    "critic_model": config.critic_model(),
                    "config_path": str(LAB_CONFIG_PATH)})


@app.route("/api/lab/run", methods=["POST"])
def api_lab_run():
    payload = request.get_json(force=True, silent=True) or {}
    device_name = (payload.get("device") or "").strip()
    model = (payload.get("model") or "").strip()
    wanted = payload.get("benchmarks") or []
    if not device_name or not model or not wanted:
        return jsonify({"error": "need device, model and benchmarks"}), 400
    bad = [b for b in wanted if b not in BENCHMARKS]
    if bad:
        return jsonify({"error": f"unknown benchmark(s): {bad}"}), 400
    try:
        config = llm_lab.load_config(LAB_CONFIG_PATH)
        config.device(device_name)                 # validates the name
    except LabError as err:
        return jsonify({"error": err.args[0]}), 404
    graph_query = state().graph_query

    def job(progress):
        results = {}
        for benchmark in wanted:
            progress(f"{benchmark}: starting …")
            results[benchmark] = llm_lab.run_benchmark(
                benchmark, config, device_name, model,
                graph_query=graph_query,
                progress=lambda t, b=benchmark: progress(f"{b}: {t}"))
        return results

    try:
        run_id = lab_runs.start("benchmark", device_name, model, job,
                                meta={"benchmarks": wanted,
                                      "defaults": config.defaults})
    except LabError as err:
        return jsonify({"error": err.args[0]}), 409
    return jsonify({"run_id": run_id}), 202


@app.route("/api/lab/pull", methods=["POST"])
def api_lab_pull():
    payload = request.get_json(force=True, silent=True) or {}
    device_name = (payload.get("device") or "").strip()
    model = (payload.get("model") or "").strip()
    if not device_name or not model:
        return jsonify({"error": "need device and model"}), 400
    try:
        device = llm_lab.load_config(LAB_CONFIG_PATH).device(device_name)
    except LabError as err:
        return jsonify({"error": err.args[0]}), 404

    def job(progress):
        return llm_lab.pull_model(device.base_url, model, progress=progress)

    try:
        run_id = lab_runs.start("pull", device_name, model, job)
    except LabError as err:
        return jsonify({"error": err.args[0]}), 409
    return jsonify({"run_id": run_id}), 202


@app.route("/api/lab/run/<run_id>")
def api_lab_run_status(run_id):
    status = lab_runs.status(run_id)
    if status is None:
        return jsonify({"error": "unknown run id"}), 404
    return jsonify(status)


@app.route("/api/lab/runs")
def api_lab_runs():
    return jsonify(llm_lab.read_runs(LAB_RUNS_PATH))


# Requirements Traceability Matrix (Fable section 9). Module-level path so
# tests can point at a copy; the JSON file is the controlled deliverable.
RTM_PATH = _DEFAULT_RTM_PATH


@app.route("/api/rtm")
def api_rtm():
    return jsonify(rtm_view(RTM_PATH))


@app.route("/api/rtm/<req_id>", methods=["POST"])
def api_rtm_update(req_id):
    payload = request.get_json(force=True, silent=True) or {}
    try:
        entry = update_requirement(req_id, status=payload.get("status"),
                                   notes=payload.get("notes"), path=RTM_PATH)
    except KeyError as err:
        return jsonify({"error": str(err)}), 404
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    return jsonify(entry)


def main():
    host = os.environ.get("HAZOP_HOST", "127.0.0.1")   # Docker sets 0.0.0.0
    port = int(os.environ.get("HAZOP_PORT", "8780"))
    print("warming up: contracting plant graph + indexing KB ...")
    state()
    print(f"ready — open http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
