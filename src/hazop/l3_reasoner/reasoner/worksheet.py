"""
worksheet.py — HAZOP worksheet output schema.

The reasoner emits rows in this structure (FR-05-01 V1 / FR-RCM-1 Fable):
Node, Parameter, Guideword, Deviation, Causes, Consequences, Safeguards,
Risk Rating (human-only), Actions.

Two cross-cutting requirements are baked in here:

1. Provenance (Fable AR-1): every generated record carries a provenance state
   so AI-generated and human-validated content are separable at the data level.

2. Evidence + confidence (V1 NFR-S-02 / FR-03-06, Fable FR-ARE-5): every AI
   finding carries a confidence score and traceable source evidence. Findings
   without evidence are explicitly labelled unsupported rather than dropped
   (V1 NFR-S-01: zero suppression).

Note: risk ranking is intentionally NOT auto-populated (Fable FR-ARE-6 makes
severity/likelihood a human-only field). The reasoner leaves it None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .guidewords import Deviation
from .schema import RetrievedEvidence


class Provenance(str, Enum):
    AI_GENERATED = "ai_generated"
    AI_GENERATED_HUMAN_APPROVED = "ai_generated_human_approved"
    AI_GENERATED_HUMAN_MODIFIED = "ai_generated_human_modified"
    HUMAN_AUTHORED = "human_authored"


@dataclass
class Finding:
    """A single cause/consequence/safeguard item with its evidence + confidence."""
    text: str
    confidence: float                                   # 0.0 - 1.0
    evidence: list[RetrievedEvidence] = field(default_factory=list)
    provenance: Provenance = Provenance.AI_GENERATED
    # True when grounded in the topology graph itself (a deterministic fact,
    # MDL-3) rather than retrieved document evidence. Such findings count as
    # supported even with no document evidence attached.
    topology_grounded: bool = False

    @property
    def is_supported(self) -> bool:
        return len(self.evidence) > 0 or self.topology_grounded

    @property
    def label(self) -> str:
        # NFR-S-01: never drop an unsupported finding — flag it instead.
        return self.text if self.is_supported else f"[UNSUPPORTED INFERENCE] {self.text}"

    def to_dict(self) -> dict:
        """Structured export keeping evidence + confidence traceable
        (NFR-S-02 / FR-ARE-5) instead of flattening to a label string."""
        return {
            "text": self.label,
            "confidence": round(self.confidence, 3),
            "evidence": [e.source_id for e in self.evidence],
            "topology_grounded": self.topology_grounded,
            "supported": self.is_supported,
            "provenance": self.provenance.value,
        }


@dataclass
class RejectedFinding:
    """
    Audit record for a finding rejected by the tag-grounding gate
    (FR-ARE-9 / MDL-10). Rejected content is excluded from the worksheet
    body but never silently lost (NFR-S-01 auditability) — it is what the
    hallucination-rate evaluation counts.
    """
    kind: str                 # "cause" | "consequence" | "safeguard"
    text: str
    invalid_tags: list[str]   # the tags that do not exist in the topology
    confidence: float

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "invalid_tags": self.invalid_tags,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class WorksheetRow:
    node_id: str
    deviation: Deviation
    causes: list[Finding] = field(default_factory=list)
    consequences: list[Finding] = field(default_factory=list)
    safeguards: list[Finding] = field(default_factory=list)
    # Risk ranking left as None on purpose — human-only field (FR-ARE-6).
    severity: Optional[int] = None
    likelihood: Optional[int] = None
    actions: list[str] = field(default_factory=list)
    provenance: Provenance = Provenance.AI_GENERATED
    # Findings rejected by the grounding gate — kept for audit (FR-ARE-9).
    rejected_findings: list[RejectedFinding] = field(default_factory=list)

    @property
    def parameter(self):
        return self.deviation.parameter

    @property
    def guideword(self):
        return self.deviation.guideword

    def min_confidence(self) -> float:
        """Lowest confidence across all findings — a crude row-level flag."""
        vals = [f.confidence for f in
                (self.causes + self.consequences + self.safeguards)]
        return min(vals) if vals else 0.0

    def to_dict(self) -> dict:
        """Dict suitable for export to xlsx/docx/json (FR-05-02). Findings
        stay structured so evidence + confidence survive export (NFR-S-02)."""
        return {
            "node": self.node_id,
            "parameter": self.parameter.value,
            "guideword": self.guideword.value,
            "deviation": self.deviation.label,
            "causes": [f.to_dict() for f in self.causes],
            "consequences": [f.to_dict() for f in self.consequences],
            "safeguards": [f.to_dict() for f in self.safeguards],
            "severity": self.severity,      # None until a human fills it
            "likelihood": self.likelihood,  # None until a human fills it
            "actions": self.actions,
            "provenance": self.provenance.value,
            "min_confidence": round(self.min_confidence(), 3),
            "rejected_findings": [r.to_dict() for r in self.rejected_findings],
        }
