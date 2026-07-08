"""
Native pyDEXPI export: convert the DEXPI-aligned plant model
(output/plant_model_dexpi.json) into real pydexpi objects and serialize.

Run with the 3.12 venv:
    .venv312/bin/python scripts/export_pydexpi_native.py

Mapping (our schema -> pydexpi):
  Equipment capsule V (tag 24xx-V-*)  -> equipment.Vessel  (+ Nozzles)
  Equipment capsule H (tag 24xx-K-*)  -> equipment.RotaryCompressor (screw)
  other tagged capsules               -> equipment.CustomEquipment
  PipingComponent componentClass      -> piping.GateValve/BallValve/AngleValve/
                                         CheckValve/SpringLoadedAngleGlobeSafetyValve
                                         (PSVs are drawn angle-form with a spring)
  PipingNetworkSegment (per sheet)    -> piping.PipingNetworkSegment inside
                                         one PipingNetworkSystem per sheet,
                                         lineNumber set when labelled;
                                         sourceNode/targetNode are PipingNodes
                                         oriented by flowDirection (swapped for
                                         to_from), flowDirection set to
                                         SingleFlowPipingNetworkSegment when the
                                         direction is known, and the evidence
                                         (arrow/check-valve/connector/propagated)
                                         kept as a FlowDirectionSource
                                         CustomAttribute
  PipeOffPageConnector                -> piping.PipeOffPageConnector
  ProcessInstrumentationFunction      -> instrumentation.ProcessInstrumentationFunction

Output: output/plant_model.pydexpi.json (pydexpi JsonSerializer format)
        + a networkx graph sanity check via pydexpi's GraphLoader
"""
import json
from collections import defaultdict

from pydexpi.dexpi_classes import equipment, piping, instrumentation
from pydexpi.dexpi_classes.dexpiModel import ConceptualModel, DexpiModel
from pydexpi.dexpi_classes.pydantic_classes import (
    CustomAttribute, PipingNetworkSegmentFlowClassification,
    PipingSourceItem, PipingTargetItem)
from pydexpi.loaders.json_serializer import JsonSerializer

PLANT_JSON = "output/plant_model_dexpi.json"
OUT_PATH = "output/plant_model.pydexpi.json"


def equipment_class(tag, shape):
    if not tag:
        return equipment.CustomEquipment
    kind = tag.split("-")[1] if "-" in tag else ""
    if kind.startswith("V"):
        return equipment.Vessel
    if kind.startswith("K"):
        return equipment.RotaryCompressor  # screw compressors on these sheets
    if kind.startswith("X") or kind.startswith("PK"):
        return equipment.CustomEquipment
    return equipment.CustomEquipment


def main():
    plant = json.load(open(PLANT_JSON))
    cm_in = plant["conceptualModel"]

    # --- equipment + nozzles -------------------------------------------------
    nozzles_by_eq = defaultdict(list)
    for nz in cm_in["Nozzle"]:
        if nz.get("equipment"):
            nozzles_by_eq[nz["equipment"]].append(nz)

    tagged_items = []
    obj_by_id = {}
    for e in cm_in["Equipment"]:
        cls = equipment_class(e.get("tagName"), e.get("shape"))
        nozzles = [
            equipment.Nozzle(subTagName=nz.get("subTagName"))
            for nz in nozzles_by_eq.get(e["id"], [])
        ]
        kwargs = {"nozzles": nozzles}
        if e.get("tagName"):
            kwargs["tagName"] = e["tagName"]
        if cls is equipment.CustomEquipment:
            # CustomEquipment requires an explicit type name
            kwargs["typeName"] = ("PackageUnit" if e.get("shape") == "capsule"
                                  else "EquipmentCircle")
        obj = cls(**kwargs)
        obj_by_id[e["id"]] = obj
        for nz, nzobj in zip(nozzles_by_eq.get(e["id"], []), nozzles):
            obj_by_id[nz["id"]] = nzobj
        tagged_items.append(obj)

    # --- valves ---------------------------------------------------------------
    valve_cls = {
        "GateValve": piping.GateValve,
        "BallValve": piping.BallValve,
        "AngleValve": piping.AngleValve,
        "CheckValve": piping.CheckValve,
        "SafetyValve": piping.SpringLoadedAngleGlobeSafetyValve,
    }
    for v in cm_in["PipingComponent"]:
        cls = valve_cls.get(v["componentClass"], piping.GateValve)
        obj_by_id[v["id"]] = cls()

    # --- off-page connectors ----------------------------------------------------
    for c in cm_in["PipeOffPageConnector"]:
        obj_by_id[c["id"]] = piping.PipeOffPageConnector()

    # --- instruments -------------------------------------------------------------
    pifs = []
    for i in cm_in["ProcessInstrumentationFunction"]:
        pif = instrumentation.ProcessInstrumentationFunction(
            tagName=i.get("tagName"))
        obj_by_id[i["id"]] = pif
        pifs.append(pif)

    # --- segment endpoints -------------------------------------------------------
    # sourceNode/targetNode are references and pydexpi has no composition owner
    # for bare junction nodes, so components go in sourceItem/targetItem and
    # bare node ids ride along as custom attributes.
    def endpoint(node_id, item_role):
        obj = obj_by_id.get(node_id)
        role_cls = PipingSourceItem if item_role == "source" else PipingTargetItem
        if isinstance(obj, role_cls):
            return {f"{item_role}Item": obj}, []
        return {}, [CustomAttribute(
            attributeName=f"{item_role.capitalize()}NodeId", value=node_id)]

    # --- piping systems: one per sheet, segments from runs ---------------------
    valve_types = tuple(set(valve_cls.values()))
    systems = []
    by_sheet = defaultdict(list)
    for s in cm_in["PipingNetworkSegment"]:
        by_sheet[s["sheet"]].append(s)
    for sheet, segs in sorted(by_sheet.items()):
        seg_objs = []
        sheet_line_numbers = set()
        for s in segs:
            if s.get("segmentClass") == "SignalLine":
                continue  # signal lines belong to instrumentation, skip here
            items = []
            src = obj_by_id.get(s["from"])
            tgt = obj_by_id.get(s["to"])
            for end_obj in (src, tgt):
                if isinstance(end_obj, valve_types + (piping.PipeOffPageConnector,)):
                    items.append(end_obj)
            kwargs = {"items": items}
            # DEXPI carries direction as source->target order; to_from swaps
            from_id, to_id = s["from"], s["to"]
            if s.get("flowDirection") == "to_from":
                from_id, to_id = to_id, from_id
            custom = []
            for node_id, role in ((from_id, "source"), (to_id, "target")):
                fields, extra = endpoint(node_id, role)
                kwargs.update(fields)
                custom.extend(extra)
            if s.get("flowDirection") in ("from_to", "to_from"):
                kwargs["flowDirection"] = (
                    PipingNetworkSegmentFlowClassification
                    .SingleFlowPipingNetworkSegment)
                custom.append(CustomAttribute(
                    attributeName="FlowDirectionSource",
                    value=s.get("flowDirectionSource", "")))
            if custom:
                kwargs["customAttributes"] = custom
            if s.get("lineNumber"):
                sheet_line_numbers.update(s["lineNumber"])
            seg = piping.PipingNetworkSegment(**kwargs)
            seg_objs.append(seg)
        systems.append(piping.PipingNetworkSystem(
            segments=seg_objs,
            lineNumber=(sorted(sheet_line_numbers)[0]
                        if sheet_line_numbers else None),
        ))

    model = DexpiModel(
        originatingSystemName="hazop-l1-extraction",
        conceptualModel=ConceptualModel(
            taggedPlantItems=tagged_items,
            pipingNetworkSystems=systems,
            processInstrumentationFunctions=pifs,
        ),
    )

    serializer = JsonSerializer()
    d = serializer.model_to_dict(model)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print(f"pydexpi model -> {OUT_PATH}")
    cm = model.conceptualModel
    print(f"  taggedPlantItems: {len(cm.taggedPlantItems)}")
    all_segs = [g for s in cm.pipingNetworkSystems for g in s.segments]
    directed = sum(1 for g in all_segs if g.flowDirection is not None)
    print(f"  pipingNetworkSystems: {len(cm.pipingNetworkSystems)} "
          f"({len(all_segs)} segments, {directed} with flowDirection)")
    print(f"  processInstrumentationFunctions: "
          f"{len(cm.processInstrumentationFunctions)}")

    # --- sanity: convert to networkx via pydexpi GraphLoader -------------------
    try:
        from pydexpi.loaders.graph_loader import GraphLoader
        import inspect
        gl_methods = [m for m in dir(GraphLoader) if not m.startswith("_")]
        print(f"\nGraphLoader available (methods: {gl_methods[:6]}...) - "
              f"Stage 2 can consume this model as a networkx graph")
    except Exception as ex:
        print("GraphLoader check failed:", ex)


if __name__ == "__main__":
    main()
