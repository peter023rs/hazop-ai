"""
hazop.web.app — integrated dashboard over all three HAZOP-AI stages.

    Stage 1  hazop.l1_extraction   P&ID -> topology (sheet overlays, stats)
    Stage 2  hazop.l2_knowledge    plant graph + curated KB retrieval
    Stage 3  hazop.l3_reasoner     guideword reasoning -> HAZOP worksheet

Everything is served live from the three stages — the plant graph is
contracted from stage 1 output at startup, KB queries run the real hybrid
retriever, and the worksheet tab runs the real AIReasoner (offline StubLLM)
on either the mock process or the digitized 2401 compressor train.

Run:    python -m hazop.web.app   (or `hazop-web` after pip install)
Open:   http://127.0.0.1:8780
Env:    HAZOP_HOST / HAZOP_PORT / HAZOP_DATA
"""

import json
import os
import socket
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from hazop.l2_knowledge.kb import KBRetriever, as_l3_retriever
from hazop.l2_knowledge.plant_graph import (build_equipment_graph,
                                            load_plant_model, to_l3_topology)
from hazop.l3_reasoner.mock_data.pump_vessel import (build_study_node,
                                                     build_topology)
from hazop.l3_reasoner.reasoner.core import AIReasoner
from hazop.l3_reasoner.reasoner.critic import critique
from hazop.l3_reasoner.reasoner.llm import StubLLM
from hazop.l3_reasoner.reasoner.mock_retriever import MockRetriever
from hazop.l3_reasoner.reasoner.schema import Parameter, StudyNode
from hazop.l3_reasoner.reasoner.topology import TopologyReasoner
from hazop.l3_reasoner.evaluation import (PUMP_VESSEL_GOLD, evaluate_node,
                                          load_gold)

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


def main():
    host = os.environ.get("HAZOP_HOST", "127.0.0.1")   # Docker sets 0.0.0.0
    port = int(os.environ.get("HAZOP_PORT", "8780"))
    print("warming up: contracting plant graph + indexing KB ...")
    state()
    print(f"ready — open http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
