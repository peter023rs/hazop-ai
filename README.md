# HAZOP-AI

End-to-end pipeline that turns a P&ID drawing into an AI-assisted HAZOP worksheet,
organized as a single installable Python package (`hazop`) with one subpackage per stage.

```
hazop-ai/
├── pyproject.toml            # single project: deps, optional extras, entry points
├── src/hazop/
│   ├── l1_extraction/        # Stage 1: P&ID PDF -> topology graph
│   │   ├── pipeline.py       #   orchestrator (was scripts/run_pipeline.py)
│   │   ├── app.py            #   Flask upload/validation viewer
│   │   ├── detect_*.py       #   arrows / equipment / instruments / valves
│   │   ├── trace_lines.py, assemble_graph.py, build_viewer.py
│   │   ├── normalize_pdf.py, compat_check.py, extract_page_spike.py
│   │   └── export_dexpi.py, export_pydexpi_native.py
│   ├── l2_knowledge/         # Stage 2: plant graph + curated KB retrieval
│   │   ├── kb/               #   hybrid BM25 + embedding retriever
│   │   ├── plant_graph/      #   L1 adapter + Neo4j store
│   │   ├── demo.py, load_neo4j.py
│   ├── l3_reasoner/          # Stage 3: guideword reasoning -> HAZOP worksheet
│   │   ├── reasoner/         #   core, critic, guidewords, topology, worksheet, llm
│   │   ├── evaluation/       #   gold sets + metrics
│   │   ├── mock_data/        #   pump/vessel fixture
│   │   ├── demo.py, evaluate.py
│   └── web/                  # integrated dashboard tying L1/L2/L3 together
│       ├── app.py            #   Flask site on :8780
│       └── templates/
└── tests/                    # tests/l2/, tests/l3/
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
`from hazop.l3_reasoner.reasoner.core import AIReasoner` — with no `sys.path`
juggling. Run modules with `python -m hazop.<stage>.<module>`.

## Run

```bash
python -m hazop.l1_extraction.pipeline  <pid.pdf> <run_dir> [pages]   # or: hazop-l1
python -m hazop.l2_knowledge.demo
python -m hazop.l3_reasoner.demo
python -m hazop.web.app                                               # or: hazop-web  (http://127.0.0.1:8780)
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
plant model + per-page topology/overlays produced by stage 1, and
`data/corpus/` holds the curated stage-2 KB documents. The dashboard and tests
read from `data/` (override with the `HAZOP_DATA` env var). Fresh extraction
runs (`hazop-l1 <pid.pdf> <run_dir>`) write wherever you point them; keep
generated artifacts out of git (see `.gitignore`).
