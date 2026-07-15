# Stage 4 — Knowledge Base (`hazop.s4_kb`)

The requirements doc's **Knowledge Base** subsystem (hazop_ai Fable draft
§2.1 #4): a curated corpus of historical studies, standards tables and
equipment data behind a hybrid retriever that plugs straight into the AI
reasoner (`hazop.s3_are`). Offline, deterministic, no API key — the real
embedding model sits behind a swap seam; the gates, ranking and tests are
real.

```
data/corpus/*.json         ──►  ingest → hybrid index   ──►  RetrieverInterface
(curated safety docs)           → retriever                  (replaces the ARE's
                                                              MockRetriever)
```

The other stage-2-era bridge — stage 1 geometry → equipment graph — is the
Process Model Layer, `hazop.s2_pml`.

## Components (suggested reading order)

| module | what it does | requirement hooks |
|---|---|---|
| `schema.py` | KB document/chunk model: curation status, applicability tags, confidentiality, holdout flag; `Evidence` is field-compatible with ARE `RetrievedEvidence` | FR-AGM-2, DR-5 |
| `index.py` | ingestion with the **curation gate** (only `approved` docs index; pending/rejected reported, never searchable) and **gold-set holdout exclusion**; hybrid search — the heart of the KB | FR-AGM-2, DDR-04/MDL-7 |
| `retriever.py` | `KBRetriever` — the drop-in replacement for ARE's `MockRetriever`; `as_l3_retriever()` wraps it as a true `RetrieverInterface` | FR-ARE-2 |
| `bm25.py`, `embed.py` | pure-Python Okapi BM25 (lexical half); `EmbedderInterface` + `HashingEmbedder` stub and `SentenceTransformerEmbedder` — the real on-prem dense model behind the same seam (`pip install -e ".[embed]"`) | MDL-2, AR-3 |
| `ingest_xlsx.py` | historical HAZOP worksheet (XLSX) → KB document, **one worksheet row = one chunk**; stdlib zip/XML reader, header-alias column mapping, guideword/parameter parsed from the deviation cell (never guessed), documents enter `pending` for the curator gate | DDR-04, FR-AGM-2 |

Hybrid fusion is Reciprocal Rank Fusion of the BM25 and dense rank lists,
plus a structured boost when a chunk's guideword/parameter tags match the
deviation filters the reasoner passes, plus hard applicability filters
(unit_type / equipment_class / chemistry).

The seed corpus in `data/corpus/` (IEC 61882 tables, a historical
air-station HAZOP, N₂ SDS, compressor datasheet) includes one `pending` and
one `holdout` document that exist purely to prove the exclusion gates work —
see `tests/s4_kb/test_kb.py::TestCurationAndHoldout`.

## Run it

```bash
python -m hazop.s2_pml.demo            # part A ingests this corpus and retrieves
pytest tests/s4_kb                     # curation/holdout gates, ranking, XLSX ingest
```

Data resolves to the repo's `data/` directory (override with `HAZOP_DATA`).

## Honest limitations (flagged in the data, not hidden)

1. The tests and demo still run on `HashingEmbedder` (token-overlap in
   vector clothing). `SentenceTransformerEmbedder` is built behind the same
   seam but has not been benchmarked on this corpus yet.
2. The corpus is 6 hand-written seed documents. XLSX worksheet ingestion
   exists (`ingest_xlsx.py`), but no *real* historical study has been run
   through it, and the curator review queue is still ingest-time status
   flags, not a UI (that workflow belongs to `hazop.s7_agm`, FR-AGM-2).

## Next steps

1. Run `SentenceTransformerEmbedder` on the corpus (`pip install -e
   ".[embed]"`), re-run the retrieval tests, compare rankings.
2. Ingest a real historical HAZOP workbook through `worksheet_to_document`
   and take it through curator approval.
