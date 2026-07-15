# Stage 1 — P&ID Topology Extraction (`hazop.s1_dim`)

Deterministic, rule-based extraction of a plant topology graph from vector
P&ID PDFs (ZWCAD exports, unit-2401 drawing convention). **No ML, no OCR** —
every detection is auditable geometry: if the pipeline claims a valve is
there, you can point at the exact drawing primitives that say so. Full design
rationale and a file-by-file code guide live in `hazop_L1(Extraction)/DESIGN.md`
(rev 4) in the research folder alongside this repo.

## How it works (module by module, in pipeline order)

| step | module | what it does |
|---|---|---|
| 0 | `normalize_pdf.py`, `compat_check.py` | scale-normalize any input PDF to the reference drawing convention and gate it (`compatible` verdict) before anything runs |
| 1 | `detect_instruments.py` | instrument bubbles (circle + tag text) |
| 2 | `detect_valves.py` | bowtie valves + subclasses: inline-vs-angle form, PSV spring ticks, and a *separate* check-valve pattern (end bars + flap + filled triangle — not a bowtie in this convention) |
| 3 | `detect_equipment.py` | equipment capsules, merged with nearby tag/spec text |
| 4 | `detect_arrows.py` | on-pipe flow arrows in the drawing's two encodings (closed slender triangles; filled-rect + taper strokes); apex = downstream |
| 5 | `trace_lines.py` | pipe-run tracing with valve/arrow bounding boxes masked out; junction vs crossing handling |
| 6 | `assemble_graph.py` | typed topology graph; `orient_runs()` seeds flow direction from arrows, check valves, **PSV orientation** (equipment semantics: angle valve drawn spring-up, inlet below, outlet horizontal — flow is always inlet→valve→outlet) and 至/自 connector text, propagates conservatively, applies flow conservation at junctions — and **never guesses** across a real branch (disagreements become `direction_conflict`, not overwrites) |
| 7 | `export_dexpi.py`, `export_pydexpi_native.py` | DEXPI-aligned plant model — the Stage 1 → Stage 2 contract (`plant_model_dexpi.json`: per-segment `flowDirection` + source, real valve `componentClass`) |
| — | `pipeline.py` | orchestrates 0–7 on any PDF (`hazop-l1` CLI) |
| — | `app.py`, `build_viewer.py` | validation website: upload → pipeline → interactive accept/reject overlay viewer |
| — | `extract_page_spike.py` | early exploration spike, kept for reference |

## Current results on the reference drawing (9 process sheets)

| what | count | how validated |
|---|---|---|
| instrument bubbles | 139, 100% tagged | overlay render + viewer labels |
| valves | 226 — 209 gate, 8 ball, 5 **safety (PSV)**, 2 **check** (+2 `angle` artifacts, see residuals) | overlay render; PSVs match all 5 PSV bubbles; check valves confirmed by crop render |
| equipment | 31 capsules + tags/specs, 80 total | overlay render |
| flow arrows | 111 (2 drawing encodings) | overlay render, direction ticks |
| directed pipe segments | 192, **zero conflicts** (92 arrow / 71 propagated / 10 conservation / 9 PSV orientation / 6 connector / 4 check-valve) | seed cross-agreement |
| plant model | `data/l1_output/plant_model_dexpi.json` | consumed by stage 2 (`hazop.s2_pml`) |

## Run

```bash
# full pipeline on any PDF (compat gate + scale normalization included)
hazop-l1 <pid.pdf> <run_dir> "4-12"
# or: python -m hazop.s1_dim.pipeline <pid.pdf> <run_dir> "4-12"

# validation website (upload -> pipeline -> accept/reject viewer)
python -m hazop.s1_dim.app        # http://127.0.0.1:8777
```

The pre-digitized 2401 outputs (plant model + per-page topology/overlays)
ship with the repo under `data/l1_output/` — the dashboard and Stage 2 read
from there, so you only need to run the pipeline for *new* drawings.

## Known residuals (flagged, not hidden)

- Direction covers the arrowed main paths, what conservation at branching
  junctions can force, and (since this rev) the five PSVs' inlet/outlet
  runs via symbol-orientation semantics. Junctions with agreeing flows but
  2+ unknown branches remain genuinely underdetermined per-edge — the next
  seed is compressor/pump discharge semantics (which nozzle is which needs
  capsule-level convention work that PSVs didn't).
- Two page-6 dryer-heater zigzag arrowheads remain `angle`-subclass entries
  in the valve index (they drop out of the topology; no pipes attach).
- Gate/globe/butterfly valves are still one `gate` class.
