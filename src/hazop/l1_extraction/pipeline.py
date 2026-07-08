"""
End-to-end Stage 1 pipeline runner, parametrized for the upload server.

run(pdf_path, run_dir, pages_spec) executes the full chain in run_dir:
  instruments -> valves -> equipment -> line graphs -> topology -> viewer
producing run_dir/output/{*_index.json, graph_page*.json, topology_page*.json,
viewer.html}.

pages_spec: "all" (default) or e.g. "4-12" / "2,5,7-9". Legend sheets add
noise, so exclude them when you know which pages they are.
"""
import json
import os
import sys

import fitz

from . import detect_instruments as DI
from . import detect_valves as DV
from . import detect_equipment as DE
from . import detect_arrows as DA
from . import trace_lines as TL
from . import assemble_graph as AG
from . import build_viewer as BV
from . import normalize_pdf as NP
from . import compat_check as CC


def parse_pages(spec, npages):
    if not spec or spec.strip().lower() == "all":
        return list(range(1, npages + 1))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted({p for p in out if 1 <= p <= npages})


def run(pdf_path, run_dir, pages_spec="all"):
    pdf_path = os.path.abspath(pdf_path)
    os.makedirs(os.path.join(run_dir, "output"), exist_ok=True)
    # the stage scripts read/write relative "output/..." paths, so run the
    # whole chain from inside the run directory (restored in finally)
    cwd = os.getcwd()
    os.chdir(run_dir)
    try:
        # scale normalization front door: bring foreign exports of the same
        # convention to reference scale so all detector constants apply
        norm_report = NP.normalize(pdf_path, "normalized.pdf")
        with open("output/calibration.json", "w") as f:
            json.dump(norm_report, f, indent=1)
        if norm_report["normalized"]:
            pdf_path = os.path.abspath("normalized.pdf")

        doc = fitz.open(pdf_path)
        pages = parse_pages(pages_spec, len(doc))

        # compatibility gate: refuse documents that violate the pipeline's
        # hard requirements instead of emitting silently wrong topology
        compat = CC.check(doc, norm_report)
        with open("output/compat_report.json", "w") as f:
            json.dump(compat, f, indent=1)
        if compat["verdict"] == "unsupported":
            fails = "; ".join(c["detail"] for c in compat["checks"]
                              if c["level"] == "fail")
            raise ValueError(f"document incompatible with this pipeline: {fails}")

        instruments = []
        for p in pages:
            instruments += DI.index_page(doc, p)
        with open("output/instrument_index.json", "w", encoding="utf-8") as f:
            json.dump(instruments, f, ensure_ascii=False, indent=1)

        valves = []
        for p in pages:
            page = doc[p - 1]
            vs = DV.find_valves(page)
            for v in vs:
                v["page"] = p
            valves += vs
        with open("output/valve_index.json", "w") as f:
            json.dump(valves, f, indent=1)

        arrows = []
        for p in pages:
            ars = DA.find_arrows(doc[p - 1])
            for a in ars:
                a["page"] = p
            arrows += ars
        with open("output/arrow_index.json", "w") as f:
            json.dump(arrows, f, indent=1)

        equipment = {"tags": [], "capsules": [], "equipment_circles": []}
        for p in pages:
            pin = [r for r in instruments
                   if r["page"] == p and r["class"] == "instrument"]
            tags, capsules, circles = DE.index_page(doc, p, pin)
            equipment["tags"] += tags
            equipment["capsules"] += capsules
            equipment["equipment_circles"] += circles
        with open("output/equipment_index.json", "w", encoding="utf-8") as f:
            json.dump(equipment, f, ensure_ascii=False, indent=1)

        for p in pages:
            pv = [v for v in valves if v["page"] == p]
            pi = [r for r in instruments
                  if r["page"] == p and r["class"] == "instrument"]
            pa = [a for a in arrows if a["page"] == p]
            nodes, edges = TL.build_graph(p, doc, pv, pi, pa)
            with open(f"output/graph_page{p}.json", "w", encoding="utf-8") as f:
                json.dump({"nodes": nodes, "edges": edges}, f, ensure_ascii=False)

        for p in pages:
            AG.assemble(p, doc, equipment,
                        [a for a in arrows if a["page"] == p])

        BV.PDF_PATH = pdf_path
        BV.PROCESS_PAGES = pages
        BV.build()

        return {
            "pages": pages,
            "instruments": sum(1 for r in instruments
                               if r["class"] == "instrument"),
            "valves": len(valves),
            "arrows": len(arrows),
            "equipment_capsules": len(equipment["capsules"]),
            "equipment_tags": len(equipment["tags"]),
            "calibration": {k: norm_report[k] for k in
                            ("scale", "method", "normalized")},
            "compatibility": compat["verdict"],
        }
    finally:
        os.chdir(cwd)


def _cli():
    pdf = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "run_out"
    spec = sys.argv[3] if len(sys.argv) > 3 else "all"
    print(json.dumps(run(pdf, out, spec), indent=1))


if __name__ == "__main__":
    _cli()
