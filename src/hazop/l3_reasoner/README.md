# Stage 3 — AI HAZOP Reasoner (`hazop.l3_reasoner`)

The reasoning engine: guideword × parameter deviations → retrieval-grounded
LLM findings → tag-grounding gate → auditable worksheet rows. Built against
*data contracts* rather than the upstream pipelines, so it runs end-to-end on
mock data with no API key — and identically on the real Stage 1/2 outputs.

## The three seams, and what fills them

The reasoner depends on the *shape* of upstream data, defined in
`reasoner/schema.py`. Everything is built against that contract:

| seam | mock (default, offline) | real component (in this package) |
|---|---|---|
| Stage 1 topology graph | `mock_data/pump_vessel.py` | `hazop.l2_knowledge.plant_graph` adapter over `data/l1_output/plant_model_dexpi.json` — real 2401 unit: 427 nodes / 701 connections, ~22% verified flow direction |
| Stage 2 KB / RAG retriever | `reasoner/mock_retriever.py` | `hazop.l2_knowledge.kb` — curated corpus + hybrid retrieval; wrap with `as_l3_retriever()` |
| generative LLM | `reasoner/llm.py::StubLLM` | `reasoner/llm.py::AnthropicLLM` — Claude behind the same `LLMInterface`; structured outputs pin the finding shape, confidence composed from cited evidence (not model self-report). Needs the `llm` extra + `ANTHROPIC_API_KEY` |

## The per-deviation pipeline (`reasoner/core.py`)

1. full guideword × parameter deviation matrix (FR-ARE-1)
2. retrieve evidence from the KB, filtered by guideword/parameter (FR-ARE-2)
3. generate candidate causes / consequences / safeguards (FR-ARE-3/4)
4. **grounding gate**: every tag cited in generated text is validated
   against the topology graph — invalid tag ⇒ finding excluded from the
   worksheet body but **kept as an audit record** (FR-ARE-9 / MDL-10)
5. enrich with deterministic topology-derived safeguards — relief path /
   check valve at 0.99 only on direction-verified paths, 0.60 "confirm
   before crediting" otherwise (MDL-3)
6. attach confidence + evidence citations, set provenance (FR-ARE-5, AR-1)
7. emit worksheet rows; **risk ranking left blank — human-only** (FR-ARE-6)

## Modules

| module | what it does |
|---|---|
| `reasoner/guidewords.py` | IEC 61882 guideword × parameter matrix, physical-applicability filtering |
| `reasoner/topology.py` | deterministic topology reasoner: tracing, flow paths, isolation boundaries, relief/check-valve detection, direction-aware verification, tag validation |
| `reasoner/core.py` | the orchestration above |
| `reasoner/worksheet.py` | output schema: provenance enum, `Finding` (confidence + evidence), `RejectedFinding` (grounding-gate audit trail) |
| `reasoner/critic.py` | completeness critic at node close-out: matrix-coverage gaps + rejection counts (FR-ARE-8) |
| `reasoner/llm.py` | `LLMInterface` + `StubLLM` + `AnthropicLLM` |
| `evaluation/` | gold-set schema/loader + deterministic scoring: deviation coverage, cause/consequence/safeguard recall, hallucination rate |
| `mock_data/pump_vessel.py` | offline pump/vessel fixture |

## Run it

```bash
python -m hazop.l3_reasoner.demo        # full pipeline on the mock process
python -m hazop.l3_reasoner.evaluate    # score against the gold set (StubLLM)
pytest tests/l3                         # no network, no API key

# real model (optional): pip install -e ".[llm]" + export ANTHROPIC_API_KEY
python -m hazop.l3_reasoner.evaluate --llm anthropic
```

`hazop.l2_knowledge.demo` part C runs THIS reasoner on the real plant graph
and real KB end-to-end — no reasoner logic changes; the seams hold.

## What's still missing (the later 20%)

- **Prompt tuning at scale** — the `AnthropicLLM` client is built; what's
  left is eval-driven prompt iteration on real nodes and parallel fan-out
  for the ≤10 s/deviation latency target (MDL-12).
- **Stage B evidence critic** (DDR-02) — the gate validates *tags*; a
  second LLM pass checking each claim against its cited evidence (<1%
  fabrication, MDL-11) is not built.
- Real retrieval-quality tuning once the KB holds actual historical HAZOPs
  instead of the 6-document seed corpus.
- The gold set is one mock node vs the ≥20 expert-authored nodes MDL-7
  requires.
