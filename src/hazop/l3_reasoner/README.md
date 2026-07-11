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
| Stage 1 topology graph | `mock_data/pump_vessel.py` | `hazop.l2_knowledge.plant_graph` adapter over `data/l1_output/plant_model_dexpi.json` — real 2401 unit: 428 nodes / 705 connections, ~26% verified flow direction |
| Stage 2 KB / RAG retriever | `reasoner/mock_retriever.py` | `hazop.l2_knowledge.kb` — curated corpus + hybrid retrieval; wrap with `as_l3_retriever()` |
| generative LLM | `reasoner/llm.py::StubLLM` | the two branches of DDR-06's swappable inference service: `AnthropicLLM` — Claude via the cloud API (`llm` extra + `ANTHROPIC_API_KEY`; per AR-3 sends node context to a third party — explicit configuration/legal-review territory); `LocalLLM` — a **self-hosted** model on any OpenAI-compatible server (Ollama / vLLM / LM Studio / llama.cpp; stdlib HTTP, no extra) so process data never leaves the deployment (AR-3 on-prem, AR-4 air-gap). Both share the same prompt/schema; confidence composed from cited evidence (not model self-report) |

## The per-deviation pipeline (`reasoner/core.py`)

1. full guideword × parameter deviation matrix (FR-ARE-1) — deviations fan
   out across a thread pool when `max_workers > 1` (DDR-11 / MDL-12),
   results identical to and ordered like the serial run
2. retrieve evidence from the KB, filtered by guideword/parameter (FR-ARE-2)
3. generate candidate causes / consequences / safeguards (FR-ARE-3/4)
4. **Stage A critic — grounding gate**: every tag cited in generated text
   is validated against the topology graph — invalid tag ⇒ finding
   excluded from the worksheet body but **kept as an audit record**
   (FR-ARE-9 / MDL-10)
5. **Stage B critic — evidence check** (optional seam): every claim is
   judged against each excerpt it cites (supported / contradicted /
   insufficient). Contradicted ⇒ refused into the audit record;
   insufficient citations are stripped so no suggestion cites evidence
   that does not support it (the MDL-11 fabrication definition), the
   finding relabelled an unsupported inference; confidence re-composed
   from surviving citations and can only fall (DDR-02, DDR-05)
6. enrich with deterministic topology-derived safeguards — relief path /
   check valve at 0.99 only on direction-verified paths, 0.60 "confirm
   before crediting" otherwise (MDL-3)
7. attach confidence + evidence citations, set provenance (FR-ARE-5, AR-1)
8. emit worksheet rows; **risk ranking left blank — human-only** (FR-ARE-6)

## Modules

| module | what it does |
|---|---|
| `reasoner/guidewords.py` | IEC 61882 guideword × parameter matrix, physical-applicability filtering |
| `reasoner/topology.py` | deterministic topology reasoner: tracing, flow paths, isolation boundaries, relief/check-valve detection, direction-aware verification, tag validation |
| `reasoner/core.py` | the orchestration above |
| `reasoner/worksheet.py` | output schema: provenance enum, `Finding` (confidence + evidence), `RejectedFinding` (grounding-gate audit trail) |
| `reasoner/critic.py` | completeness critic at node close-out: matrix-coverage gaps + rejection counts (FR-ARE-8) |
| `reasoner/evidence_critic.py` | Stage B critic (DDR-02): `LexicalEvidenceCritic` (offline, content-word containment, abstains rather than accuses) + `AnthropicEvidenceCritic` / `LocalEvidenceCritic` (real NLI-style pass on a *different* model than the generator to break correlated error — cloud and self-hosted variants; refusal/failure ⇒ abstention on every citation, never fabricated support) |
| `reasoner/llm.py` | `LLMInterface` + `StubLLM` + `AnthropicLLM` (cloud) + `OpenAICompatClient`/`LocalLLM` (self-hosted, OpenAI-compatible protocol, JSON-schema structured output, invalid JSON retried once with the error fed back then refused — DDR-03) |
| `evaluation/` | gold-set schema/loader + deterministic scoring: deviation coverage, cause/consequence/safeguard recall, hallucination rate |
| `mock_data/pump_vessel.py` | offline pump/vessel fixture |

## Run it

```bash
python -m hazop.l3_reasoner.demo        # full pipeline on the mock process
python -m hazop.l3_reasoner.evaluate    # score against the gold set (StubLLM)
pytest tests/l3                         # no network, no API key

# cloud model (optional): pip install -e ".[llm]" + export ANTHROPIC_API_KEY
python -m hazop.l3_reasoner.evaluate --llm anthropic
# Stage B critic in the eval loop: lexical (default) | anthropic | local | none
python -m hazop.l3_reasoner.evaluate --evidence-critic anthropic

# fully local (no data leaves the machine): any OpenAI-compatible server.
# e.g. Ollama: `brew install ollama && ollama serve`,
#      `ollama pull llama3.1:8b && ollama pull llama3.2:3b`
python -m hazop.l3_reasoner.evaluate --llm local --evidence-critic local --workers 4
# defaults: HAZOP_LLM_URL=http://localhost:11434/v1, HAZOP_LLM_MODEL=llama3.1:8b,
#           HAZOP_CRITIC_MODEL=llama3.2:3b (different model than the generator, DDR-02)
```

`hazop.l2_knowledge.demo` part C runs THIS reasoner on the real plant graph
and real KB end-to-end — no reasoner logic changes; the seams hold.

## What's still missing (the later 20%)

- **Prompt tuning at scale** — the `AnthropicLLM` client and the parallel
  fan-out are built; what's left is eval-driven prompt iteration on real
  nodes and measuring the ≤10 s P95/deviation target (MDL-12) against the
  live API rather than stubs.
- **Stage B calibration** — the evidence critic is built (both the offline
  lexical double and the real `AnthropicEvidenceCritic`), but the <1%
  fabrication bar (MDL-11) is a *measured* claim: it needs an
  expert-audited sample of real-model output, which does not exist yet.
- Real retrieval-quality tuning once the KB holds actual historical HAZOPs
  instead of the 6-document seed corpus (the XLSX ingest path in stage 2
  is ready for them).
- The gold set is one mock node vs the ≥20 expert-authored nodes MDL-7
  requires — expert-authored is the point; it cannot be generated here.
