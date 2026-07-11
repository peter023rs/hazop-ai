# Stage 2 — Knowledge Layer (`hazop.l2_knowledge`)

The layer between **Stage 1** (drawing digitization, `hazop.l1_extraction`)
and **Stage 3** (AI reasoner, `hazop.l3_reasoner`). Offline, deterministic,
no API key — the expensive parts (real embedding model, live Neo4j, vector
DB, steady-state simulator) sit behind swap seams; the logic, gates and
tests are real.

This package is also where the requirements doc's **Process Model Layer**
(hazop_ai Fable draft §3.2, FR-PML-1..5) lives: `plant_graph/` builds and
serves the directed process graph (FR-PML-1's substrate — the tracing /
isolation / flow-path queries themselves are L3's deterministic
`reasoner/topology.py`, per MDL-3), `plant_graph/nodes.py` proposes HAZOP
node boundaries (FR-PML-2), and `plant_graph/screening.py` carries the
simulator seam and the labeled heuristic fallback (FR-PML-3/4/5).

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
| `kb/bm25.py`, `kb/embed.py` | pure-Python Okapi BM25 (lexical half); `EmbedderInterface` + `HashingEmbedder` stub and `SentenceTransformerEmbedder` — the real on-prem dense model behind the same seam (`pip install -e ".[embed]"`) | MDL-2, AR-3 |
| `kb/ingest_xlsx.py` | historical HAZOP worksheet (XLSX) → KB document, **one worksheet row = one chunk**; stdlib zip/XML reader, header-alias column mapping, guideword/parameter parsed from the deviation cell (never guessed), documents enter `pending` for the curator gate | DDR-04, FR-AGM-2 |
| `plant_graph/adapter.py` | Stage 1 DEXPI JSON → equipment-level graph: BFS contraction through junction geometry, nozzle folding, tag merging across sheets, tag-letter equipment typing, direction votes + **equipment-level pass-through propagation** (2-connection valves/OPCs force their unknown side, incl. across sheets) | FR-PML-1 substrate, MDL-3 input, MDL-10 grounding basis |
| `plant_graph/condense.py` | node-scoped condensed context views for the generation layer (Delft-style): members + neighbors within N hops, in-line valves folded into connections, instruments folded into owners, per-connection direction honesty (`verified_hops`); ~42k tokens (full 2401 graph) → ~2.4k (one node view) | DDR-11 node-open cache, MDL-3 |
| `plant_graph/nodes.py` | **HAZOP node boundary proposal**: deterministic partition of the equipment-level graph at design-intent changes — pressure breaks (pumps/compressors join their *verified suction side*; discharge starts the next node), phase changes (heat exchangers, same rule), unit boundaries (off-page connectors end nodes, so proposals stop at sheet edges), machine trains (verified anti-parallel pairs stay together). Every node carries `status="proposed"`, rule-by-rule rationale, and its boundary elements; ambiguous or direction-unverified placements are flagged for the facilitator, and `merge_nodes` / `move_member` implement manual redefinition with a `redefinitions` log (proposed vs redefined stays distinguishable, AR-1). Members feed `condensed_node_view` unchanged | FR-PML-2 |
| `plant_graph/screening.py` | **deviation screening**: `SimulatorInterface` is the FR-PML-3 seam (HYSYS/DWSIM adapters plug in later — OI-2); the dispatcher *enforces* FR-PML-4 labeling (case id, model id+version, convergence status; non-converged = never `reliable`) rather than trusting the adapter; `HeuristicScreener` is the FR-PML-5 fallback — pump deadhead ≈ 1.2–1.5 × normal discharge (pump curve preferred), blocked-outlet pressure bounds, exchanger no-flow temperature bound — every result labeled `ESTIMATE`, qualitative when no numbers are supplied, honest "no rule" instead of a guess | FR-PML-3/4/5 |
| `plant_graph/neo4j_store.py` | plant graph → Neo4j: `to_cypher()` offline idempotent script, `load()` batched live loader behind the optional `neo4j` extra. **`FLOWS_TO` = verified flow direction, `CONNECTED_TO` = drawing order only** — so `-[:FLOWS_TO*]->` is always a trusted traversal | DDR-01 graph substrate |
| `demo.py` | A: ingest+retrieve · B: contract the real 2401 drawing · C: run L3's real `AIReasoner` with this retriever, on the mock node **and** a real study node · D: propose HAZOP nodes on the real drawing + screen deviations through the heuristic fallback | |

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
python -m hazop.l2_knowledge.demo          # A/B/C/D end-to-end (reads data/)
pytest tests/l2                            # gates, ranking, contraction, Neo4j store,
                                           # node proposal, screening

# Neo4j
python -m hazop.l2_knowledge.load_neo4j    # -> outputs/plant_graph_2401.cypher (no deps)
python -m hazop.l2_knowledge.load_neo4j --load --password <pw>   # push to a live server
                                           # (needs the `neo4j` extra + running Neo4j)
```

Data resolves to the repo's `data/` directory (override with `HAZOP_DATA`).

## Current numbers on the real drawing

3,052 piping nodes / 3,027 segments contract to **428 equipment-level nodes
and 705 connections** (8 compressors, 61 vessels, 205 valves + 5 relief +
2 check, 131 instruments, 16 off-page connectors), **186 connections with
known flow direction (~26%) and zero conflicts** — stage 1 seeds (arrows,
check valves, PSV orientation, connector text, conservation) plus 11 forced
by the equipment-level pass-through pass. The two compressor/intercooler
pairs resolve into anti-parallel directed connections (discharge in, cooled
return out — both arrow-backed); tee-fed valve pairs carry a
`direction_note` explaining there is no through-flow direction between them.

Node proposal on the same graph: **19 HAZOP nodes proposed covering 31
equipment items** (19 pressure-break boundaries, 9 unit boundaries); the
other 38 major items have no traced process connections (isolated or
instrument-only detections) and are reported `unassigned` for the
facilitator rather than forced into nodes. Compressors whose verified flow
evidence offers more than one suction-side section are attached
deterministically and flagged `AMBIGUOUS — facilitator must confirm`.

## Honest limitations (flagged in the data, not hidden)

1. **Flow direction is partial** — 186/705 (~26%) of equipment-level
   connections carry `direction="known"` with evidence sources; the rest
   stay `"unknown"` in drawing order. The L3 reasoner is direction-aware:
   results crossing unknown edges are reported as unverified.
2. **Valve classes**: `check_valve` and `relief_valve` map through to L3's
   types, but gate/globe/butterfly remain lumped as `valve`.
3. The tests and demo still run on `HashingEmbedder` (token-overlap in
   vector clothing). `SentenceTransformerEmbedder` is built behind the same
   seam but has not been benchmarked on this corpus yet.
4. The corpus is 6 hand-written seed documents. XLSX worksheet ingestion
   exists (`kb/ingest_xlsx.py`), but no *real* historical study has been
   run through it, and the curator review queue is still ingest-time
   status flags, not a UI.
5. Untagged Stage 1 equipment (circles = coolers/filters/motors) get
   generated `EQ-*` tags with `detection_confidence=0.7`.
6. **No live simulator** (FR-PML-3 / open issue OI-2): `SimulatorInterface`
   is the seam, but no HYSYS/DWSIM adapter exists — all screening today
   comes from `HeuristicScreener` and is labeled `ESTIMATE` accordingly.
7. **Node proposal markers are type-level.** Stage 1 carries no stream
   conditions or phase data, so "pressure break" = pump/compressor and
   "phase change" = heat exchanger by equipment type; the 2401 drawing tags
   no pumps or exchangers (intercoolers are untagged circles typed vessel),
   so its proposals split on compressors and off-page connectors only.
   Vessel chains within a sheet can group large — proposals, not verdicts;
   `merge_nodes`/`move_member` exist exactly for that.

## Next steps

1. Compressor/pump discharge direction seeds (the PSV-orientation trick
   needs capsule-level convention work to identify which nozzle is which) —
   this also collapses the `AMBIGUOUS` suction-side placements in the node
   proposals.
2. Run `SentenceTransformerEmbedder` on the corpus (`pip install -e
   ".[embed]"`), re-run the retrieval tests, compare rankings.
3. Ingest a real historical HAZOP workbook through `worksheet_to_document`
   and take it through curator approval.
4. Wire `condensed_node_view` output into the generation prompts (today the
   demo prints it; the LLM context should be built from it), and feed
   `propose_nodes` members into StudyNode construction (today demo part C2
   picks members by hand).
5. A DWSIM adapter behind `SimulatorInterface` (open-source path of
   FR-PML-3), keeping `HeuristicScreener` as the no-model fallback.
