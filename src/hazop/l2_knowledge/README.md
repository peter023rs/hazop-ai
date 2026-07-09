# Stage 2 — Knowledge Layer (`hazop.l2_knowledge`)

The layer between **Stage 1** (drawing digitization, `hazop.l1_extraction`)
and **Stage 3** (AI reasoner, `hazop.l3_reasoner`). Offline, deterministic,
no API key — the expensive parts (real embedding model, live Neo4j, vector
DB) sit behind swap seams; the logic, gates and tests are real.

## What Stage 2 is: two bridges

Stage 3 defines Stage 2 as the *knowledge graph / KB*. In practice that is
two bridges, and both live here:

```
Stage 1 output                        Stage 3 input contracts (l3_reasoner/reasoner/schema.py)
──────────────                        ────────────────────────
data/l1_output/            ──►  plant_graph/adapter.py  ──►  TopologyGraph
plant_model_dexpi.json          (contract 3,089 geometric     (EquipmentNode +
                                 segments to equipment         Connection)
                                 level, merge tags,
                                 fold nozzles)

data/corpus/*.json         ──►  kb/ (ingest → hybrid     ──►  RetrieverInterface
(curated safety docs)           index → retriever)            (replaces MockRetriever)
```

## Components (suggested reading order)

| module | what it does | requirement hooks |
|---|---|---|
| `kb/schema.py` | KB document/chunk model: curation status, applicability tags, confidentiality, holdout flag; `Evidence` is field-compatible with L3 `RetrievedEvidence` | FR-AGM-2, DR-5 |
| `kb/index.py` | ingestion with the **curation gate** (only `approved` docs index; pending/rejected reported, never searchable) and **gold-set holdout exclusion**; hybrid search — the heart of the KB | FR-AGM-2, DDR-04/MDL-7 |
| `kb/retriever.py` | `KBRetriever` — the drop-in replacement for L3's `MockRetriever`; `as_l3_retriever()` wraps it as a true `RetrieverInterface` | FR-ARE-2 |
| `kb/bm25.py`, `kb/embed.py` | pure-Python Okapi BM25 (lexical half); `EmbedderInterface` + `HashingEmbedder` stub (dense half — swap a real embedding model in here) | MDL-2, AR-3 |
| `plant_graph/adapter.py` | Stage 1 DEXPI JSON → equipment-level graph: BFS contraction through junction geometry, nozzle folding, tag merging across sheets, tag-letter equipment typing, direction votes | MDL-3 input, MDL-10 grounding basis |
| `plant_graph/neo4j_store.py` | plant graph → Neo4j: `to_cypher()` offline idempotent script, `load()` batched live loader behind the optional `neo4j` extra. **`FLOWS_TO` = verified flow direction, `CONNECTED_TO` = drawing order only** — so `-[:FLOWS_TO*]->` is always a trusted traversal | DDR-01 graph substrate |
| `demo.py` | A: ingest+retrieve · B: contract the real 2401 drawing · C: run L3's real `AIReasoner` with this retriever, on the mock node **and** a real study node | |

Hybrid fusion is Reciprocal Rank Fusion of the BM25 and dense rank lists,
plus a structured boost when a chunk's guideword/parameter tags match the
deviation filters the reasoner passes, plus hard applicability filters
(unit_type / equipment_class / chemistry).

The seed corpus in `data/corpus/` (IEC 61882 tables, a historical
air-station HAZOP, N₂ SDS, compressor datasheet) includes one `pending` and
one `holdout` document that exist purely to prove the exclusion gates work —
see `tests/l2/test_kb.py::TestCurationAndHoldout`.

## Run it

```bash
python -m hazop.l2_knowledge.demo          # A/B/C end-to-end (reads data/)
pytest tests/l2                            # gates, ranking, contraction, Neo4j store

# Neo4j
python -m hazop.l2_knowledge.load_neo4j    # -> outputs/plant_graph_2401.cypher (no deps)
python -m hazop.l2_knowledge.load_neo4j --load --password <pw>   # push to a live server
                                           # (needs the `neo4j` extra + running Neo4j)
```

Data resolves to the repo's `data/` directory (override with `HAZOP_DATA`).

## Current numbers on the real drawing

3,080 piping nodes / 3,089 segments contract to **427 equipment-level nodes
and 701 connections** (8 compressors, 61 vessels, 204 valves + 5 relief +
2 check, 131 instruments, 16 off-page connectors), **159 connections with
known flow direction and zero conflicts**. The two compressor/intercooler
pairs resolve into anti-parallel directed connections (discharge in, cooled
return out — both arrow-backed); tee-fed valve pairs carry a
`direction_note` explaining there is no through-flow direction between them.

## Honest limitations (flagged in the data, not hidden)

1. **Flow direction is partial** — 159/701 (~22%) of equipment-level
   connections carry `direction="known"` with evidence sources; the rest
   stay `"unknown"` in drawing order. The L3 reasoner is direction-aware:
   results crossing unknown edges are reported as unverified.
2. **Valve classes**: `check_valve` and `relief_valve` map through to L3's
   types, but gate/globe/butterfly remain lumped as `valve`.
3. `HashingEmbedder` is a stub — token-overlap in vector clothing. Real
   semantic retrieval needs a real model behind `EmbedderInterface`.
4. The corpus is 6 hand-written seed documents. The real build-out is
   ingestion of actual historical HAZOPs (one worksheet row = one chunk)
   with a curator review queue as a UI.
5. Untagged Stage 1 equipment (circles = coolers/filters/motors) get
   generated `EQ-*` tags with `detection_confidence=0.7`.

## Next steps

1. Extend direction coverage past ~22% (equipment-semantics seeds:
   compressor/pump discharge, PSV outlet).
2. Swap `HashingEmbedder` for a real model; re-run the same tests.
3. Ingest a real historical HAZOP worksheet (XLSX → one chunk per row).
4. Node-scoped condensed context views over the Neo4j graph for the
   generation layer (per the handoff doc).
