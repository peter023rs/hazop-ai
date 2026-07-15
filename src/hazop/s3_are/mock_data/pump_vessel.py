"""
pump_vessel.py — A small hand-authored mock process for development & tests.

Layout (flow direction ->):
    TK-100  ->  V-SUCT  ->  P-101  ->  CV-101  ->  V-201  ->  PSV-201
    (tank)     (suction   (pump)     (check     (vessel)   (relief valve)
               valve)                 valve)

This is exactly the kind of representative structured data stage 1 would emit,
authored by hand so the reasoner can run today with no digitization pipeline.
"""

from hazop.s3_are.reasoner.schema import (
    TopologyGraph, EquipmentNode, Connection, StudyNode,
    EquipmentType, Parameter,
)


def build_topology() -> TopologyGraph:
    nodes = [
        EquipmentNode("TK-100", EquipmentType.TANK, "Feed Tank",
                      attributes={"design_pressure_barg": 0.5}),
        EquipmentNode("V-SUCT", EquipmentType.VALVE, "Pump Suction Valve"),
        EquipmentNode("P-101", EquipmentType.PUMP, "Feed Pump",
                      attributes={"normal_discharge_barg": 8.0}),
        EquipmentNode("CV-101", EquipmentType.CHECK_VALVE, "Discharge Check Valve"),
        EquipmentNode("V-201", EquipmentType.VESSEL, "Feed Surge Drum",
                      attributes={"design_pressure_barg": 10.0, "MAWP_barg": 10.0}),
        EquipmentNode("PSV-201", EquipmentType.RELIEF_VALVE, "Surge Drum Relief Valve",
                      attributes={"set_pressure_barg": 9.5}),
    ]
    edges = [
        Connection("TK-100", "V-SUCT", line_tag="6\"-TK-100"),
        Connection("V-SUCT", "P-101", line_tag="6\"-SUCT"),
        Connection("P-101", "CV-101", line_tag="4\"-DISCH"),
        Connection("CV-101", "V-201", line_tag="4\"-DISCH"),
        Connection("V-201", "PSV-201", line_tag="3\"-RELIEF"),
    ]
    return TopologyGraph(nodes=nodes, edges=edges)


def build_study_node() -> StudyNode:
    return StudyNode(
        node_id="NODE-1",
        description="Pump discharge to feed surge drum",
        equipment_tags=["P-101", "CV-101", "V-201"],
        parameters=[Parameter.FLOW, Parameter.PRESSURE, Parameter.TEMPERATURE,
                    Parameter.LEVEL],
        design_intent="Transfer feed from TK-100 to V-201 at 8 barg, maintaining "
                      "level control in V-201.",
    )
