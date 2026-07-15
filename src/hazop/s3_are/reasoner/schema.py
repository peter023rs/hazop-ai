"""
schema.py — The data contract between the AI Reasoner (stage 3) and the
upstream Drawing Digitization (stage 1) + Knowledge Graph (stage 2) stages.

This is the SEAM. As long as stages 1-2 produce data in these shapes, the
reasoner can be developed and tested against mocks and later swapped onto
real output with no logic changes.

Nothing here depends on stage 1 or 2 being implemented.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Topology graph — produced by stage 1 (drawing digitization / parsing)
# ---------------------------------------------------------------------------

class EquipmentType(str, Enum):
    VESSEL = "vessel"
    PUMP = "pump"
    COMPRESSOR = "compressor"
    HEAT_EXCHANGER = "heat_exchanger"
    VALVE = "valve"
    CHECK_VALVE = "check_valve"
    CONTROL_VALVE = "control_valve"
    RELIEF_VALVE = "relief_valve"
    INSTRUMENT = "instrument"
    LINE = "line"
    TANK = "tank"
    COLUMN = "column"


class Parameter(str, Enum):
    """Process parameters a guideword is applied to (HAZOP core set)."""
    FLOW = "flow"
    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    LEVEL = "level"
    COMPOSITION = "composition"
    REACTION = "reaction"
    PHASE = "phase"


@dataclass
class EquipmentNode:
    """A single item of equipment or a stream node in the topology graph."""
    tag: str                          # e.g. "P-101", "V-205"  (unique)
    equipment_type: EquipmentType
    name: str = ""
    attributes: dict = field(default_factory=dict)   # design_pressure, MAWP, set_pressure, etc.
    detection_confidence: float = 1.0                # from stage 1 vision model; 1.0 for mock

    def __post_init__(self):
        if not self.name:
            self.name = self.tag


@dataclass
class Connection:
    """A directed edge: process flows from `source` tag to `target` tag."""
    source: str          # equipment tag
    target: str          # equipment tag
    line_tag: str = ""   # e.g. "6\"-P-101-CS"
    attributes: dict = field(default_factory=dict)


@dataclass
class TopologyGraph:
    """
    The structured output of stage 1. A directed graph of equipment + streams.
    The reasoner treats this as read-only input.
    """
    nodes: list[EquipmentNode] = field(default_factory=list)
    edges: list[Connection] = field(default_factory=list)

    def __post_init__(self):
        self._by_tag: dict[str, EquipmentNode] = {n.tag: n for n in self.nodes}

    def node_by_tag(self, tag: str) -> Optional[EquipmentNode]:
        if len(self._by_tag) != len(self.nodes):  # nodes list mutated after init
            self._by_tag = {n.tag: n for n in self.nodes}
        return self._by_tag.get(tag)

    def all_tags(self) -> set[str]:
        return {n.tag for n in self.nodes}


# ---------------------------------------------------------------------------
# HAZOP study node — the unit of analysis (a section of the process)
# ---------------------------------------------------------------------------

@dataclass
class StudyNode:
    """
    A HAZOP node: a bounded section of the process with a design intent and a
    set of parameters to interrogate. Boundaries may be proposed by stage 1/2
    tooling and approved by a facilitator; for the reasoner it's just input.
    """
    node_id: str
    description: str
    equipment_tags: list[str] = field(default_factory=list)  # members of this node
    parameters: list[Parameter] = field(default_factory=list)
    design_intent: str = ""


# ---------------------------------------------------------------------------
# Knowledge Graph / retrieval interface — produced/served by stage 2
# ---------------------------------------------------------------------------

@dataclass
class RetrievedEvidence:
    """A single piece of evidence returned by the KB / RAG retriever (stage 2)."""
    source_id: str          # document id / KG node id
    source_type: str        # "sds" | "historical_hazop" | "standard" | "datasheet" | "kg"
    snippet: str
    score: float = 0.0      # retrieval relevance score


class RetrieverInterface(ABC):
    """
    The contract the reasoner uses to talk to stage 2. Any real KB/RAG service
    must implement `retrieve`. The reasoner never touches Neo4j/FAISS directly —
    it goes through this interface, so a mock can stand in.

    `filters` carries the deviation structurally (e.g. {"guideword": "NO",
    "parameter": "flow"}) so retrievers don't have to re-parse the query text.
    """

    @abstractmethod
    def retrieve(self, query: str, k: int = 5,
                 filters: Optional[dict] = None) -> list[RetrievedEvidence]:
        ...
