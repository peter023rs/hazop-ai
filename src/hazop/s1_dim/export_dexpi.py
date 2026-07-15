"""
DEXPI-aligned plant model export - the frozen Stage 1 -> Stage 2 contract.

Consumes the per-sheet topology graphs (output/topology_page<N>.json) and
emits one plant-level JSON whose object classes mirror pyDEXPI / DEXPI 1.3
naming, so a later native pyDEXPI export is a mechanical mapping:

  Equipment                     tagged vessels/machines (+ untagged circles)
  Nozzle                        connection points on equipment (deduplicated)
  PipingNetworkSegment          pipe run between two connection points,
                                with lineNumber when labelled on the drawing
  PipingComponent               valves (subclass guessed: BallValve/...)
  ProcessInstrumentationFunction  instrument bubbles with full ISA tag
  PipeOffPageConnector          off-page connectors; matched across sheets
                                by shared line numbers

Cleanups applied while exporting:
  - nozzles of the same equipment within 8 pt merge into one (keep label)
  - junction/dead-end chains that remained separate nodes stay as implicit
    segment endpoints ("PipingNode")

Usage:  python3 scripts/export_dexpi.py
Output: output/plant_model_dexpi.json + connectivity statistics
"""
import json
import math
import re
from collections import defaultdict

PROCESS_PAGES = range(4, 13)


def load_topologies():
    topos = {}
    for p in PROCESS_PAGES:
        topos[p] = json.load(open(f"output/topology_page{p}.json"))
    return topos


def merge_nozzles(topo):
    """Same-equipment nozzles within 8pt are one physical nozzle.

    Adjacent strokes (pipe + spec-break fragment) can each cross the
    equipment boundary and spawn their own nozzle node; this dedupes them,
    preferring whichever carries a drawn label (N1, P2, ...).
    """
    nodes = {n["id"]: n for n in topo["nodes"]}
    groups = defaultdict(list)
    for n in topo["nodes"]:
        if n["kind"] == "nozzle":
            groups[n.get("equipment")].append(n)
    remap = {}
    for eq, nzs in groups.items():
        used = set()
        for i, a in enumerate(nzs):
            if a["id"] in used:
                continue
            cluster = [a]
            for b in nzs[i + 1:]:
                if b["id"] in used:
                    continue
                if math.hypot(a["xy"][0] - b["xy"][0],
                              a["xy"][1] - b["xy"][1]) < 8:
                    cluster.append(b)
                    used.add(b["id"])
            keeper = next((c for c in cluster if c.get("nozzle_label")), cluster[0])
            for c in cluster:
                if c["id"] != keeper["id"]:
                    remap[c["id"]] = keeper["id"]
    topo["nodes"] = [n for n in topo["nodes"] if n["id"] not in remap]
    for e in topo["edges"]:
        e["a"] = remap.get(e["a"], e["a"])
        e["b"] = remap.get(e["b"], e["b"])
    topo["edges"] = [e for e in topo["edges"] if e["a"] != e["b"]]
    return topo


VALVE_CLASS = {
    "gate": "GateValve",
    "ball": "BallValve",
    "check": "CheckValve",
    "safety": "SafetyValve",
    "angle": "AngleValve",
}


def valve_subclass(n):
    sub = n.get("subclass")
    if sub in VALVE_CLASS:
        return VALVE_CLASS[sub]
    if n.get("ball"):
        return "BallValve"
    return "GateValve"  # dominant default on these sheets


def instrument_class(tag):
    """ISA function letters -> DEXPI-ish instrumentation subclass."""
    if not tag:
        return "ProcessInstrumentationFunction"
    letters = tag.split()[0]
    if letters.startswith("PSV"):
        return "SafetyValveOrFitting"
    return "ProcessInstrumentationFunction"


def export():
    topos = {p: merge_nozzles(t) for p, t in load_topologies().items()}

    plant = {
        "schema": "hazop-l1/dexpi-aligned/v1",
        "sourceDrawing": "2401单元工艺管道及仪表流程图.pdf",
        "conceptualModel": {
            "Equipment": [], "Nozzle": [], "PipingComponent": [],
            "ProcessInstrumentationFunction": [], "PipingNetworkSegment": [],
            "PipeOffPageConnector": [], "PipingNode": [],
        },
    }
    cm = plant["conceptualModel"]
    uid = lambda p, i: f"p{p}n{i}"

    seg_count_by_node = defaultdict(int)
    for p, topo in topos.items():
        nodes = {n["id"]: n for n in topo["nodes"]}
        for n in topo["nodes"]:
            k = n["kind"]
            base = {"id": uid(p, n["id"]), "sheet": p,
                    "position": n["xy"]}
            if k == "equipment":
                cm["Equipment"].append(base | {
                    "tagName": n.get("tag"),
                    "shape": n.get("shape"),
                    "bbox": n.get("bbox"),
                })
            elif k == "nozzle":
                cm["Nozzle"].append(base | {
                    "equipment": uid(p, n["equipment"]) if isinstance(
                        n.get("equipment"), int) else None,
                    "subTagName": n.get("nozzle_label"),
                })
            elif k == "valve":
                cm["PipingComponent"].append(base | {
                    "componentClass": valve_subclass(n)})
            elif k == "instrument":
                cm["ProcessInstrumentationFunction"].append(base | {
                    "tagName": n.get("full_tag") or n.get("tag"),
                    "componentClass": instrument_class(n.get("tag")),
                })
            elif k == "off-page":
                cm["PipeOffPageConnector"].append(base | {
                    "labels": n.get("labels", [])})
            else:
                cm["PipingNode"].append(base | {"nodeType": k})
        # equipment ref fix: nozzle 'equipment' holds eq-item index, resolve
        eq_nodes = [n for n in topo["nodes"] if n["kind"] == "equipment"]
        for nz in cm["Nozzle"]:
            if nz["sheet"] != p or nz["equipment"] is not None:
                continue
        for e in topo["edges"]:
            if e["style"] == "internal":
                continue
            seg = {
                "id": f"p{p}s{len(cm['PipingNetworkSegment'])}",
                "sheet": p,
                "from": uid(p, e["a"]), "to": uid(p, e["b"]),
                "segmentClass": ("SignalLine" if e["style"] == "dashed"
                                 else "PipingNetworkSegment"),
                "length": e.get("length"),
                "polyline": e.get("points"),
            }
            if e.get("line_numbers"):
                seg["lineNumber"] = e["line_numbers"]
            # flow direction: "from_to" = flow runs from -> to
            if e.get("direction") and not e.get("direction_conflict"):
                seg["flowDirection"] = ("from_to" if e["direction"] == "ab"
                                        else "to_from")
                seg["flowDirectionSource"] = e.get("direction_source")
            cm["PipingNetworkSegment"].append(seg)
            seg_count_by_node[seg["from"]] += 1
            seg_count_by_node[seg["to"]] += 1

    # nozzle -> equipment references (equipment kept its per-sheet item index
    # in the topology; map index -> equipment node id per sheet)
    for p, topo in topos.items():
        eq_by_index = {}
        for n in topo["nodes"]:
            if n["kind"] == "equipment":
                eq_by_index[len(eq_by_index)] = uid(p, n["id"])
        # topology stored nozzle.equipment as eq-item index in drawing order;
        # rebuild from the 'internal' edges instead (nozzle -- equipment)
        internal = [(e["a"], e["b"]) for e in topo["edges"]
                    if e["style"] == "internal"]
        nid_to_eq = {}
        # off-page connectors reclassified from connector circles keep their
        # internal nozzle links too
        eq_ids = {n["id"] for n in topo["nodes"]
                  if n["kind"] in ("equipment", "off-page")}
        for a, b in internal:
            if a in eq_ids:
                nid_to_eq[b] = uid(p, a)
            elif b in eq_ids:
                nid_to_eq[a] = uid(p, b)
        for nz in cm["Nozzle"]:
            if nz["sheet"] == p:
                raw = int(nz["id"].split("n")[1])
                if raw in nid_to_eq:
                    nz["equipment"] = nid_to_eq[raw]

    # cross-sheet links via shared line numbers
    ln_pages = defaultdict(set)
    for seg in cm["PipingNetworkSegment"]:
        for ln in seg.get("lineNumber", []):
            ln_pages[ln].add(seg["sheet"])
    links = []
    for ln, ps in sorted(ln_pages.items()):
        if len(ps) > 1:
            links.append({"lineNumber": ln, "sheets": sorted(ps)})
    plant["crossSheetLinks"] = links

    with open("output/plant_model_dexpi.json", "w", encoding="utf-8") as f:
        json.dump(plant, f, ensure_ascii=False, indent=1)

    # stats
    print("plant model -> output/plant_model_dexpi.json")
    for k, v in cm.items():
        print(f"  {k:<34} {len(v)}")
    print(f"  crossSheetLinks                    {len(links)}")
    from collections import Counter
    vc = Counter(c["componentClass"] for c in cm["PipingComponent"])
    print(f"  valve classes: {dict(vc)}")
    fd = sum(1 for s in cm["PipingNetworkSegment"] if s.get("flowDirection"))
    print(f"  segments with flowDirection: {fd}")
    tagged_eq = [e for e in cm["Equipment"] if e.get("tagName")]
    print(f"\ntagged equipment ({len(tagged_eq)}):")
    for e in tagged_eq:
        print(f"   sheet {e['sheet']}: {e['tagName']}")
    return plant


if __name__ == "__main__":
    export()
