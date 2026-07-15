# Stage 2 — Process Model Layer (`hazop.s2_pml`)

The bridge between **stage 1** (drawing digitization, `hazop.s1_dim`) and
**stage 3** (AI reasoner, `hazop.s3_are`). Offline, deterministic, no API
key — the expensive parts (live Neo4j, steady-state simulator) sit behind
swap seams; the logic, gates and tests are real.

This package is the requirements doc's **Process Model Layer** (hazop_ai
Fable draft §3.2, FR-PML-1..5): it builds and serves the directed process
graph (FR-PML-1's substrate — the tracing / isolation / flow-path queries
themselves are ARE's deterministic `reasoner/topology.py`, per MDL-3),
`nodes.py` proposes HAZOP node boundaries (FR-PML-2), and `screening.py`
carries the simulator seam and the labeled heuristic fallback (FR-PML-3/4/5).

```
Stage 1 output                       Stage 3 input contract (s3_are/reasoner/schema.py)
──────────────                       ───────────────────────
data/l1_output/           ──►  adapter.py               ──►  TopologyGraph
plant_model_dexpi.json         (contract 3,089 geometric      (EquipmentNode +
                                segments to equipment          Connection)
                                level, merge tags,
                                fold nozzles)
```

The other stage-2 bridge — curated documents → retriever — is the Knowledge
Base subsystem, `hazop.s4_kb`.

## Components (suggested reading order)

| module | what it does | requirement hooks |
|---|---|---|
| `adapter.py` | Stage 1 DEXPI JSON → equipment-level graph: BFS contraction through junction geometry, nozzle folding, tag merging across sheets, tag-letter equipment typing, direction votes + **equipment-level pass-through propagation** (2-connection valves/OPCs force their unknown side, incl. across sheets) | FR-PML-1 substrate, MDL-3 input, MDL-10 grounding basis |
| `condense.py` | node-scoped condensed context views for the generation layer (Delft-style): members + neighbors within N hops, in-line valves folded into connections, instruments folded into owners, per-connection direction honesty (`verified_hops`); ~42k tokens (full 2401 graph) → ~2.4k (one node view) | DDR-11 node-open cache, MDL-3 |
| `nodes.py` | **HAZOP node boundary proposal**: deterministic partition of the equipment-level graph at design-intent changes — pressure breaks (pumps/compressors join their *verified suction side*; discharge starts the next node), phase changes (heat exchangers, same rule), unit boundaries (off-page connectors end nodes, so proposals stop at sheet edges), machine trains (verified anti-parallel pairs stay together). Every node carries `status="proposed"`, rule-by-rule rationale, and its boundary elements; ambiguous or direction-unverified placements are flagged for the facilitator, and `merge_nodes` / `move_member` implement manual redefinition with a `redefinitions` log (proposed vs redefined stays distinguishable, AR-1). Members feed `condensed_node_view` unchanged | FR-PML-2 |
| `screening.py` | **deviation screening**: `SimulatorInterface` is the FR-PML-3 seam (HYSYS/DWSIM adapters plug in later — OI-2); the dispatcher *enforces* FR-PML-4 labeling (case id, model id+version, convergence status; non-converged = never `reliable`) rather than trusting the adapter; `HeuristicScreener` is the FR-PML-5 fallback — pump deadhead ≈ 1.2–1.5 × normal discharge (pump curve preferred), blocked-outlet pressure bounds, exchanger no-flow temperature bound — every result labeled `ESTIMATE`, qualitative when no numbers are supplied, honest "no rule" instead of a guess | FR-PML-3/4/5 |
| `neo4j_store.py` | plant graph → Neo4j: `to_cypher()` offline idempotent script, `load()` batched live loader behind the optional `neo4j` extra. **`FLOWS_TO` = verified flow direction, `CONNECTED_TO` = drawing order only** — so `-[:FLOWS_TO*]->` is always a trusted traversal | DDR-01 graph substrate |
| `query.py` | **query layer** (IYP-style): `parse_question` grounds a plain-English question into a typed `Intent` (tags resolved against the real graph, partials like `K-001A` accepted, ambiguity fails closed); `GraphQuery` executes it via ARE's direction-aware `TopologyReasoner` and returns answer + rows + subgraph **+ the equivalent Cypher**; `run_cypher` is a read-only passthrough to live Neo4j; `AnthropicTranslator` is the optional LLM seam (fills an Intent, never writes Cypher) | MDL-3 traversals, DDR-06 seam |
| `demo.py` | A: ingest+retrieve (via `hazop.s4_kb`) · B: contract the real 2401 drawing · C: run ARE's real `AIReasoner` with this retriever, on the mock node **and** a real study node · D: propose HAZOP nodes on the real drawing + screen deviations through the heuristic fallback | |
| `load_neo4j.py` | CLI: contract stage-1 output and write `outputs/plant_graph_2401.cypher`; `--load` pushes to a live server | |

## Run it

```bash
python -m hazop.s2_pml.demo             # A/B/C/D end-to-end (reads data/)
pytest tests/s2_pml                     # contraction, condensed views, Neo4j store,
                                        # node proposal, screening

# Neo4j
python -m hazop.s2_pml.load_neo4j       # -> outputs/plant_graph_2401.cypher (no deps)
python -m hazop.s2_pml.load_neo4j --load --password <pw>   # push to a live server
                                        # (needs the `neo4j` extra + running Neo4j)
```

Data resolves to the repo's `data/` directory (override with `HAZOP_DATA`).

## Query the database

Three ways in, most convenient first:

1. **Graph Explorer** (dashboard, `hazop-web` → *L2 · Graph Explorer*):
   ask in plain English — every answer shows the equivalent Cypher, the
   result graph (Neo4j-Browser-style) and a table. No server needed; NL
   questions run in-process against the equipment graph.
2. **Python**:

   ```python
   from hazop.s2_pml import GraphQuery, build_equipment_graph, load_plant_model
   gq = GraphQuery(build_equipment_graph(load_plant_model(path)))
   r = gq.ask("which vessels are downstream of 2401-K-001A?")
   r.answer, r.rows, r.cypher   # + r.nodes / r.edges for drawing
   ```

3. **Cypher on live Neo4j** (after `load_neo4j --load`), e.g.:

   ```cypher
   // verified flow downstream of the first-stage compressor
   MATCH (s:PlantItem {tag:'2401-K-001A'})-[:FLOWS_TO*1..10]->(x)
   RETURN DISTINCT x.tag, x.equipment_type ORDER BY x.tag;

   // every relief valve and which sheet it lives on
   MATCH (psv:ReliefValve) RETURN psv.tag, psv.sheets;
   ```

   The dashboard's query bar passes raw Cypher through to the live server
   read-only; the explorer's example gallery shows more.

Supported question shapes: downstream / upstream of a tag (optionally
filtered, "which **vessels** are downstream of …"), direct neighbours,
path between two tags, list / count by equipment type, relief-path check,
and tag info. Unknown or ambiguous tags fail closed with a hint — nothing
is guessed.

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
   stay `"unknown"` in drawing order. The ARE reasoner is direction-aware:
   results crossing unknown edges are reported as unverified.
2. **Valve classes**: `check_valve` and `relief_valve` map through to ARE's
   types, but gate/globe/butterfly remain lumped as `valve`.
3. Untagged stage-1 equipment (circles = coolers/filters/motors) get
   generated `EQ-*` tags with `detection_confidence=0.7`.
4. **No live simulator** (FR-PML-3 / open issue OI-2): `SimulatorInterface`
   is the seam, but no HYSYS/DWSIM adapter exists — all screening today
   comes from `HeuristicScreener` and is labeled `ESTIMATE` accordingly.
5. **Node proposal markers are type-level.** Stage 1 carries no stream
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
2. Wire `condensed_node_view` output into the generation prompts (today the
   demo prints it; the LLM context should be built from it), and feed
   `propose_nodes` members into StudyNode construction (today demo part C2
   picks members by hand).
3. A DWSIM adapter behind `SimulatorInterface` (open-source path of
   FR-PML-3), keeping `HeuristicScreener` as the no-model fallback.
