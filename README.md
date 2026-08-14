# HAZOP-AI

End-to-end pipeline that turns a P&ID drawing into an AI-assisted HAZOP worksheet,
organized as a single installable Python package (`hazop`) with one subpackage per
subsystem of the requirements doc's §2.1 architecture ("stages 1–7").

| stage | subsystem | package | guide |
|---|---|---|---|
| 1 | DIM — Document Intelligence (P&ID → topology graph; deterministic geometry, with a DiagEx LLM-vision fallback for documents the compat gate refuses) | `hazop.s1_dim` | [src/hazop/s1_dim/README.md](src/hazop/s1_dim/README.md) |
| 2 | PML — Process Model Layer (plant graph, node proposal, deviation screening, NL + Cypher query layer) | `hazop.s2_pml` | [src/hazop/s2_pml/README.md](src/hazop/s2_pml/README.md) |
| 3 | ARE — AI Reasoning Engine (guideword reasoning → HAZOP worksheet) | `hazop.s3_are` | [src/hazop/s3_are/README.md](src/hazop/s3_are/README.md) |
| 4 | KB — Knowledge Base (curated corpus, hybrid retrieval) | `hazop.s4_kb` | [src/hazop/s4_kb/README.md](src/hazop/s4_kb/README.md) |
| 5 | SW — Study Workspace (integrated dashboard on :8780, incl. the Graph Explorer and the multi-device [LLM Lab](data/llm_lab/README.md)) | `hazop.s5_sw` | [src/hazop/s5_sw/README.md](src/hazop/s5_sw/README.md) |
| 6 | RCM — Reporting & Compliance (today: the requirements traceability matrix) | `hazop.s6_rcm` | — |
| 7 | AGM — Administration & Governance (placeholder; curation gate lives in s4_kb) | `hazop.s7_agm` | — |

`hazop.mdl` is not a subsystem: it is the §4 model-development harness
(gold-set eval, grounding audit, fabrication report, latency, seeded
omissions, telemetry) that measures the ARE.

## Architecture

Stages talk to each other through explicit file/schema contracts, never by
importing each other's internals — so any stage can be re-run, replaced or
inspected on its own:

```
                       compat gate (s1_dim/compat_check.py)
                                │
P&ID PDF ──► normalize_pdf ─────┤
                                │
      compatible / degraded ────┴──── unsupported
                │                          │
   deterministic geometry path      DiagEx LLM-vision fallback
   detect_* → trace_lines           diagex_fallback.py
   → assemble_graph                 (engine="diagex", optional)
                └────────────┬─────────────┘
                             │  same contract from both paths:
                             ▼  output/topology_page<N>.json, then
                    data/l1_output/plant_model_dexpi.json
                    (DEXPI-aligned, per-segment flowDirection + source)
                             │
   s2_pml/adapter.py contracts ~3k geometric segments → 428-node equipment graph
     nodes.py (HAZOP-node proposal) · screening.py (deviation screening)
     condense.py (node-scoped context) · query.py (NL → Intent → Cypher)
                             │
                             ▼  TopologyGraph contract (s3_are/reasoner/schema.py)
data/corpus/ ──s4_kb──► hybrid BM25 + dense retriever ──► s3_are AIReasoner
                                                            │ guideword × parameter,
                                                            │ two-stage critic:
                                                            │ tag-grounding gate,
                                                            │ then evidence check
                                                            ▼
                                             worksheet rows + audit records
                                                            │
   s5_sw dashboard (:8780) serves all of it live;  mdl/ measures the ARE
```

### Stage 1: two extraction paths behind one gate

`compat_check.py` runs before any detector and decides how the document is
processed:

- **compatible / degraded** → the deterministic geometry-only path
  (`detect_*.py` → `trace_lines.py` → `assemble_graph.py`). Every detection is
  auditable drawing geometry; nothing is inferred by a model.
- **unsupported** (scanned/raster pages, no text layer, foreign stroke-width
  or tag conventions) → `diagex_fallback.py`, which routes the pages through
  DiagEx (ABB Research's zero-shot P&ID vision-language agent, Apache-2.0;
  local checkout at `~/Desktop/DiagEx-Repro`) and converts its
  reconciled graph into the *same*
  `topology_page<N>.json` contract — so the overlay viewer, `export_dexpi`
  and Stage 2 consume it unchanged. Runs are stamped `engine: "diagex"`,
  nodes keep the agent's per-entity confidence, flow direction is seeded only
  from off-page-connector attributes read off the drawing (the 2401-specific
  PSV-orientation seed is deliberately disabled on this path), and the agent
  transcripts plus native DEXPI export persist under the run's `diagex_runs/`
  for auditing.

The fallback is optional and checked for availability *before* any spend:

```bash
pip install -e ~/Desktop/DiagEx-Repro    # the diagex package
export ANTHROPIC_API_KEY=...             # or OpenRouter/Kimi per DiagEx docs
export HAZOP_DIAGEX_EFFORT=medium        # low|medium|high|xhigh
```

Without it, unsupported documents are refused with the original compat error
and the deterministic path is unaffected either way.

### The seam pattern

Every expensive or external dependency sits behind an interface with an
offline deterministic default, so the whole pipeline and the entire test
suite run with no API key, no network and no Neo4j:

| seam | offline default | real component (extra / env) |
|---|---|---|
| stage-1 extraction (`s1_dim/pipeline.py`) | deterministic geometry path | `diagex_fallback.py` (`diagex` package + `ANTHROPIC_API_KEY`) |
| generative LLM (`s3_are/reasoner/llm.py`) | `StubLLM` | `AnthropicLLM` (`[llm]` + `ANTHROPIC_API_KEY`) or `LocalLLM` (`HAZOP_LLM_URL`, `HAZOP_LLM_MODEL`) |
| evidence critic (`s3_are/reasoner/evidence_critic.py`) | `LexicalEvidenceCritic` | `Anthropic`/`LocalEvidenceCritic` (`HAZOP_CRITIC_MODEL` — deliberately a *different* model than the generator) |
| dense embeddings (`s4_kb/embed.py`) | `HashingEmbedder` | `SentenceTransformerEmbedder` (`[embed]`) |
| graph store (`s2_pml/neo4j_store.py`) | offline `to_cypher()` script | live loader (`[neo4j]`) |
| process simulator (`s2_pml/screening.py`) | `HeuristicScreener` (labels everything `ESTIMATE`) | `SimulatorInterface` seam, no adapter yet |

### The honesty principle

Uncertainty is surfaced, never papered over, and this shapes the data formats
everywhere: flow direction is only marked `known` with an evidence source
(disagreements become `direction_conflict`, never overwrites, and ~26%
coverage is reported rather than hidden); ambiguous NL-query tags fail closed
with a hint; findings that fail the grounding/evidence gates are excluded from
the worksheet but **kept as audit records**; confidence is composed from cited
evidence and can only fall; risk ranking is left blank (human-only); LLM Lab
failures (OOMs, crashes) are recorded as results.

## Layout

```
hazop-ai/
├── pyproject.toml            # single project: deps, optional extras, entry points
├── src/hazop/
│   ├── authgate.py           # optional Basic-auth gate (HAZOP_WEB_PASSWORD)
│   ├── s1_dim/               # stage 1 DIM: P&ID PDF -> topology graph
│   │   ├── pipeline.py       #   orchestrator: normalize -> compat gate -> path
│   │   ├── normalize_pdf.py, compat_check.py   # scale normalization + the gate
│   │   ├── detect_*.py       #   arrows / equipment / instruments / valves
│   │   ├── trace_lines.py, assemble_graph.py   # pipe runs + orient_runs()
│   │   ├── diagex_fallback.py #  LLM-vision path for "unsupported" documents
│   │   ├── export_dexpi.py, export_pydexpi_native.py  # stage 1 -> 2 contract
│   │   ├── app.py, build_viewer.py  # upload/validation viewer on :8777
│   │   └── extract_page_spike.py    # early exploration spike, kept for reference
│   ├── s2_pml/               # stage 2 PML: plant graph + node proposal + screening
│   │   ├── adapter.py        #   DIM output -> equipment-level graph
│   │   ├── condense.py       #   node-scoped condensed context views
│   │   ├── nodes.py          #   HAZOP node proposal (FR-PML-2)
│   │   ├── screening.py      #   deviation screening seam/heuristics (FR-PML-3/4/5)
│   │   ├── query.py          #   NL -> grounded Intent -> Cypher query layer
│   │   ├── neo4j_store.py, load_neo4j.py, demo.py
│   ├── s3_are/               # stage 3 ARE: guideword reasoning -> HAZOP worksheet
│   │   ├── reasoner/         #   core, critic, evidence_critic, guidewords,
│   │   │                     #   topology, worksheet, schema, llm, mock_retriever
│   │   ├── mock_data/        #   pump/vessel fixture
│   │   ├── demo.py, evaluate.py
│   ├── s4_kb/                # stage 4 KB: hybrid retrieval over the curated corpus
│   │   ├── bm25.py, embed.py, retriever.py   # lexical + dense + fusion
│   │   └── index.py, schema.py, ingest_xlsx.py
│   ├── s5_sw/                # stage 5 SW: integrated dashboard tying it all together
│   │   ├── app.py            #   Flask site on :8780
│   │   ├── llm_lab.py        #   multi-device local-model benchmarking
│   │   └── templates/, static/
│   ├── s6_rcm/               # stage 6 RCM: requirements traceability matrix (rtm.py)
│   ├── s7_agm/               # stage 7 AGM: placeholder (no implementation yet)
│   └── mdl/                  # §4 model-development harness (MDL-7..14 gates)
│       ├── gold.py, metrics.py, grounding.py, fabrication.py
│       └── latency.py, seeded_omissions.py, telemetry.py, mdl_scorecard.py
├── data/                     # l1_output/, corpus/, llm_lab/, rtm/, telemetry/
└── tests/                    # one directory per stage: s1_dim/, s2_pml/, s3_are/,
                              # s4_kb/, s5_sw/, s6_rcm/, mdl/
```

## Install

Requires **Python 3.10+** (developed on 3.14; macOS's system `/usr/bin/python3`
is 3.9 and its bundled pip fails on this project's editable install — use a
newer Python, e.g. `brew install python@3.14`, or skip straight to Docker below).

```bash
cd hazop-ai
python3.14 -m venv .venv && source .venv/bin/activate
pip install -e .            # core (flask, networkx, pymupdf)
pip install -e ".[neo4j,llm,embed,dexpi,dev]"   # optional extras + pytest
```

Each extra gates exactly one real component behind a seam (see the table
above); none of them are needed to run the pipeline, the dashboard or the
tests. The stage-1 DiagEx fallback is installed separately, from its own
checkout: `pip install -e ~/Desktop/DiagEx-Repro`.

The `src/` layout means imports are always absolute and namespaced —
`from hazop.s3_are.reasoner.core import AIReasoner` — with no `sys.path`
juggling. Run modules with `python -m hazop.<subsystem>.<module>`.

## Run

```bash
python -m hazop.s1_dim.pipeline <pid.pdf> <run_dir> [pages]  # or: hazop-l1
python -m hazop.s1_dim.app                                   # or: hazop-dim (:8777)
python -m hazop.s2_pml.demo                                  # end-to-end on the real graph
python -m hazop.s3_are.demo                                  # reasoner on mock data
python -m hazop.s3_are.evaluate                              # gold-set scoring
python -m hazop.s5_sw.app                                    # or: hazop-web  (:8780)
python -m hazop.mdl.mdl_scorecard                            # §4 gate scorecard
python -m hazop.s5_sw.llm_lab --device <d> --model <m> \
       --benchmarks capability,graph_accuracy                # local-model bench
./share.sh [sw|dim]                                          # Cloudflare quick tunnel
pytest                                                       # run tests (offline)
```

The test suite is an offline design invariant: no API key, no network and no
Neo4j, on every stage.

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

The rest of `data/` holds `llm_lab/llm_lab.yaml` (the single source of truth
for the LLM Lab's device × model matrix — the UI renders it and never edits
it; the device-local `runs.jsonl` is gitignored), `rtm/requirements.json` for
the stage-6 traceability matrix, and `telemetry/` for suggestion events.

Environment variables, all optional: `HAZOP_DATA` (data root), `HAZOP_HOST` /
`HAZOP_PORT` (web binding), `HAZOP_WEB_PASSWORD` (arms the Basic-auth gate,
used by `share.sh` when tunnelling publicly), `HAZOP_DIM_RUNS` (points the
stage-1 validator at an alternate runs directory), plus the per-seam variables
listed in the seam table.
