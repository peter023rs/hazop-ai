# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

End-to-end pipeline turning a P&ID drawing into an AI-assisted HAZOP worksheet, as one installable Python package (`hazop`, src/ layout) with one subpackage per subsystem ("stage") of the requirements doc's §2.1 architecture. Most stages have their own README — see Architecture below.

## Commands

```bash
# setup — requires Python 3.10+ (developed on 3.14; macOS system python3 is too old)
source .venv/bin/activate                  # venv already exists in the repo
pip install -e ".[dev]"                    # core + pytest
pip install -e ".[neo4j,llm,embed,dexpi]"  # optional extras, each gates one real component

# tests (offline, no API key, no network — this is a design invariant)
pytest                                     # all (testpaths = tests/)
pytest tests/s2_pml                        # one stage
pytest tests/s4_kb/test_kb.py::TestCurationAndHoldout   # one test
pytest -q                                  # what CI runs (python 3.14)

# run
hazop-web                                  # integrated dashboard, http://127.0.0.1:8780
hazop-l1 <pid.pdf> <run_dir> [pages]       # stage-1 extraction on a new PDF
python -m hazop.s1_dim.app                 # stage-1 upload/validation viewer, :8777
python -m hazop.s2_pml.demo                # end-to-end demo incl. real reasoner on real graph
python -m hazop.s3_are.demo | evaluate     # reasoner on mock data / gold-set scoring
python -m hazop.s5_sw.llm_lab --device <d> --model <m> --benchmarks capability,graph_accuracy
python -m hazop.mdl.mdl_scorecard          # §4 MDL gate scorecard
./share.sh [sw|dim]                        # expose dashboard/validator via Cloudflare quick tunnel, password-gated

# docker (CI also smoke-tests /api/overview on the built image)
docker compose up --build                  # serves :8780
```

Run modules with `python -m hazop.<subsystem>.<module>`; imports are always absolute (`from hazop.s3_are.reasoner.core import AIReasoner`).

## Architecture

See `README.md` for the full software design and the stage-by-stage pipeline.

Per-stage design rationale, current numbers, and honest limitations — read the
relevant one before changing a stage:

- `src/hazop/s1_dim/README.md` — stage 1, deterministic rule-based topology extraction from vector P&ID PDFs (no ML, no OCR; every detection traceable to drawing primitives)
- `src/hazop/s2_pml/README.md` — stage 2, process model layer: directed process graph, HAZOP node proposal, screening; Neo4j and simulator behind seams
- `src/hazop/s3_are/README.md` — stage 3, AI reasoner: guideword x parameter deviations, retrieval-grounded LLM findings, tag-grounding gate, worksheet rows
- `src/hazop/s4_kb/README.md` — stage 4, knowledge base: curated corpus, hybrid BM25+dense retrieval, curation gates
- `src/hazop/s5_sw/README.md` — stage 5, integrated dashboard over all stages, plus LLM Lab multi-device benchmarking

`s6_rcm` (requirements traceability, `rtm.py`) and `s7_agm` (stub) have no README yet.

### The seam pattern (load-bearing)

Every expensive or external dependency sits behind an interface with an offline deterministic default, so the whole pipeline and all tests run with no API key, no network, no Neo4j:

| seam | offline default | real component (optional extra / env) |
|---|---|---|
| generative LLM (`s3_are/reasoner/llm.py`) | `StubLLM` | `AnthropicLLM` (`[llm]` + `ANTHROPIC_API_KEY`) or `LocalLLM` (any OpenAI-compatible server; `HAZOP_LLM_URL`, `HAZOP_LLM_MODEL`) |
| evidence critic (`evidence_critic.py`) | `LexicalEvidenceCritic` | `Anthropic`/`LocalEvidenceCritic` (`HAZOP_CRITIC_MODEL` — deliberately a *different* model than the generator) |
| dense embeddings (`s4_kb/embed.py`) | `HashingEmbedder` | `SentenceTransformerEmbedder` (`[embed]`) |
| graph store (`s2_pml/neo4j_store.py`) | offline `to_cypher()` script | live loader (`[neo4j]`) |
| process simulator (`s2_pml/screening.py`) | `HeuristicScreener` (labels everything `ESTIMATE`) | `SimulatorInterface` seam, no adapter yet |

When adding functionality, preserve this: the stub path must stay the working default and tests must not require the real component.

### The honesty principle (project-wide, not per-stage)

Uncertainty is surfaced, never papered over — this shapes APIs and data formats everywhere: flow direction is only marked `known` with an evidence source (disagreements become `direction_conflict`, never overwrites; ~26% coverage is reported, not hidden); ambiguous NL-query tags fail closed with a hint; findings that fail the grounding/evidence gates are excluded from the worksheet but **kept as audit records**; confidence is composed from cited evidence and can only fall; risk ranking is left blank (human-only); LLM Lab failures (OOMs, crashes) are recorded as results. Follow the same discipline in new code, and keep the stage READMEs' "honest limitations" sections accurate when you change behavior.

## Data & environment

- `data/` ships the digitized 2401-unit outputs (`l1_output/` — legacy name kept for artifact compatibility) and the seed KB corpus; dashboard and tests read from it. Override root with `HAZOP_DATA`. Keep freshly generated run artifacts out of git.
- Web config: `HAZOP_HOST`, `HAZOP_PORT`; `HAZOP_WEB_PASSWORD` arms the optional Basic-auth gate (`hazop/authgate.py`), used by `share.sh` when tunneling the dashboard publicly. `HAZOP_DIM_RUNS` points the stage-1 validator at an alternate runs directory (share.sh defaults it to the legacy pre-consolidation tree if present).
- LLM Lab: `data/llm_lab/llm_lab.yaml` is the single source of truth for the device×model matrix (the UI renders it, never edits it); `runs.jsonl` is gitignored and device-local.


