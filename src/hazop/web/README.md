# Integrated Dashboard (`hazop.web`)

One website over all three stages, served live from their code (nothing is
pre-baked except the Stage 1 drawing outputs shipped in `data/l1_output/`):

| tab | what it shows | backed by |
|---|---|---|
| Overview | pipeline stats L1 → L2 → L3, KB ingestion report, Neo4j link | all three stages |
| L1 · Extraction | per-sheet topology overlays + node/edge/direction stats | `data/l1_output/` |
| L2 · Plant Graph | interactive 427-node equipment graph (cytoscape.js); solid blue = verified flow, dashed = drawing order; click a node for a direction-aware upstream/downstream trace + relief-path status | `plant_graph` adapter + L3 `TopologyReasoner` |
| L2 · Knowledge Base | live hybrid retrieval (BM25+dense, guideword filters) and the corpus curation gates | `kb.KBRetriever` |
| L3 · HAZOP Worksheet | runs the real `AIReasoner` on the mock process or the digitized 2401 compressor train: findings with confidence + evidence, grounding-gate audit, critic report, gold-eval baseline-vs-integrated comparison | `reasoner` + `evaluation` |

## Run

```bash
hazop-web                     # or: python -m hazop.web.app
# open http://127.0.0.1:8780
```

Configure via env: `HAZOP_HOST`, `HAZOP_PORT`, `HAZOP_DATA` (defaults to the
repo's `data/`). Needs internet for the cytoscape.js CDN.

Notes:
- The Stage 1 accept/reject validator is a separate app
  (`python -m hazop.l1_extraction.app`, port 8777).
- The Neo4j Browser (port 7474) is linked from the Overview tab when a
  server is running.
- The gold-eval tab intentionally shows the integrated Stage-2 KB *failing*
  cause recall on the pump-process gold set — the corpus is curated for the
  2401 air station, so pump-domain precedents are a known gap that closes
  with real historical-HAZOP ingestion.
