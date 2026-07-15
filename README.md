# HAZOP-AI

End-to-end pipeline that turns a P&ID drawing into an AI-assisted HAZOP worksheet,
organized as a single installable Python package (`hazop`) with one subpackage per
subsystem of the requirements doc's §2.1 architecture ("stages 1–7").

| stage | subsystem | package | guide |
|---|---|---|---|
| 1 | DIM — Document Intelligence (P&ID → topology graph; deterministic, no ML/OCR) | `hazop.s1_dim` | [src/hazop/s1_dim/README.md](src/hazop/s1_dim/README.md) |
| 2 | PML — Process Model Layer (plant graph, node proposal, deviation screening) | `hazop.s2_pml` | [src/hazop/s2_pml/README.md](src/hazop/s2_pml/README.md) |
| 3 | ARE — AI Reasoning Engine (guideword reasoning → HAZOP worksheet) | `hazop.s3_are` | [src/hazop/s3_are/README.md](src/hazop/s3_are/README.md) |
| 4 | KB — Knowledge Base (curated corpus, hybrid retrieval) | `hazop.s4_kb` | [src/hazop/s4_kb/README.md](src/hazop/s4_kb/README.md) |
| 5 | SW — Study Workspace (integrated dashboard on :8780) | `hazop.s5_sw` | [src/hazop/s5_sw/README.md](src/hazop/s5_sw/README.md) |
| 6 | RCM — Reporting & Compliance (today: the requirements traceability matrix) | `hazop.s6_rcm` | — |
| 7 | AGM — Administration & Governance (placeholder; curation gate lives in s4_kb) | `hazop.s7_agm` | — |

`hazop.mdl` is not a subsystem: it is the §4 model-development harness
(gold-set eval, grounding audit, fabrication report, latency, seeded
omissions, telemetry) that measures the ARE.

```
hazop-ai/
├── pyproject.toml            # single project: deps, optional extras, entry points
├── src/hazop/
│   ├── s1_dim/               # stage 1 DIM: P&ID PDF -> topology graph
│   │   ├── pipeline.py       #   orchestrator (was scripts/run_pipeline.py)
│   │   ├── app.py            #   Flask upload/validation viewer
│   │   ├── detect_*.py       #   arrows / equipment / instruments / valves
│   │   ├── trace_lines.py, assemble_graph.py, build_viewer.py
│   │   ├── normalize_pdf.py, compat_check.py, extract_page_spike.py
│   │   └── export_dexpi.py, export_pydexpi_native.py
│   ├── s2_pml/               # stage 2 PML: plant graph + node proposal + screening
│   │   ├── adapter.py        #   DIM output -> equipment-level graph
│   │   ├── condense.py       #   node-scoped condensed context views
│   │   ├── nodes.py          #   HAZOP node proposal (FR-PML-2)
│   │   ├── screening.py      #   deviation screening seam/heuristics (FR-PML-3/4/5)
│   │   ├── neo4j_store.py, demo.py, load_neo4j.py
│   ├── s3_are/               # stage 3 ARE: guideword reasoning -> HAZOP worksheet
│   │   ├── reasoner/         #   core, critic, evidence_critic, guidewords, topology, worksheet, llm
│   │   ├── mock_data/        #   pump/vessel fixture
│   │   ├── demo.py, evaluate.py
│   ├── s4_kb/                # stage 4 KB: hybrid BM25 + embedding retriever, XLSX ingest
│   ├── s5_sw/                # stage 5 SW: integrated dashboard tying it all together
│   │   ├── app.py            #   Flask site on :8780
│   │   └── templates/
│   ├── s6_rcm/               # stage 6 RCM: requirements traceability matrix (rtm.py)
│   ├── s7_agm/               # stage 7 AGM: placeholder (no implementation yet)
│   └── mdl/                  # §4 model-development harness (MDL-7..14 gates)
└── tests/                    # tests/s2_pml/, tests/s3_are/, tests/s4_kb/, tests/mdl/, tests/s6_rcm/
```

## Install

Requires **Python 3.10+** (developed on 3.14; macOS's system `/usr/bin/python3`
is 3.9 and its bundled pip fails on this project's editable install — use a
newer Python, e.g. `brew install python@3.14`, or skip straight to Docker below).

```bash
cd hazop-ai
python3.14 -m venv .venv && source .venv/bin/activate
pip install -e .            # core (flask, networkx, pymupdf)
pip install -e ".[neo4j,llm,dexpi,dev]"   # optional extras + pytest
```

The `src/` layout means imports are always absolute and namespaced —
`from hazop.s3_are.reasoner.core import AIReasoner` — with no `sys.path`
juggling. Run modules with `python -m hazop.<subsystem>.<module>`.

## Run

```bash
python -m hazop.s1_dim.pipeline  <pid.pdf> <run_dir> [pages]   # or: hazop-l1
python -m hazop.s2_pml.demo
python -m hazop.s3_are.demo
python -m hazop.s5_sw.app                                               # or: hazop-web  (http://127.0.0.1:8780)
pytest                                                                # run tests
```

## Docker

```bash
docker compose up --build        # builds the image and serves http://localhost:8780
# or without compose:
docker build -t hazop-ai . && docker run -p 8780:8780 hazop-ai
```

The image installs the package, copies `data/`, and runs `hazop-web` bound to
`0.0.0.0:8780`. Configure via env: `HAZOP_HOST`, `HAZOP_PORT`, `HAZOP_DATA`.

## Data & artifacts

The repo is self-contained: `data/l1_output/` holds the digitized 2401-unit
plant model + per-page topology/overlays produced by stage 1 (the directory
name predates the s1_dim rename and is kept for artifact compatibility), and
`data/corpus/` holds the curated stage-4 KB documents. The dashboard and tests
read from `data/` (override with the `HAZOP_DATA` env var). Fresh extraction
runs (`hazop-l1 <pid.pdf> <run_dir>`) write wherever you point them; keep
generated artifacts out of git (see `.gitignore`).
