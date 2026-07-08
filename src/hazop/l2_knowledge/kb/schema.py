"""
schema.py — Data model of the Stage 2 Knowledge Base.

A KB *document* is one curated source (a historical HAZOP study, a standard,
an SDS, an equipment datasheet). A document is split into *chunks*; following
DDR-04, one chunk is one retrievable unit ≈ one worksheet row / one fact —
never a whole document.

Curation (FR-AGM-2): documents enter the corpus with a curation status and
only `approved` documents ever reach the index. Every document carries source,
date, applicability tags and a confidentiality class.

Holdout (DDR-04 / MDL-7): documents marked `holdout=True` belong to the
gold-standard evaluation set and are physically excluded from the index so
retrieval metrics are never contaminated.

The `Evidence` returned by the retriever is shape-compatible with
stage 3's `reasoner.schema.RetrievedEvidence` (source_id, source_type,
snippet, score) — that is the contract seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CurationStatus(str, Enum):
    PENDING = "pending"      # in the review queue — NOT retrievable
    APPROVED = "approved"    # curator-approved — retrievable
    REJECTED = "rejected"    # refused — never retrievable


class SourceType(str, Enum):
    HISTORICAL_HAZOP = "historical_hazop"
    STANDARD = "standard"
    SDS = "sds"
    DATASHEET = "datasheet"
    INCIDENT = "incident"
    KG = "kg"                # facts derived from the plant knowledge graph


@dataclass
class Applicability:
    """Metadata filters (MDL-2): what plants/chemistry a document applies to."""
    unit_types: list[str] = field(default_factory=list)   # e.g. ["air_separation"]
    chemistry: list[str] = field(default_factory=list)    # e.g. ["nitrogen"]
    equipment_classes: list[str] = field(default_factory=list)  # e.g. ["compressor"]


@dataclass
class KBChunk:
    """One retrievable unit. `guidewords`/`parameters` enable structured
    filter boosting from the reasoner's deviation context."""
    chunk_id: str
    text: str
    guidewords: list[str] = field(default_factory=list)   # e.g. ["MORE"]
    parameters: list[str] = field(default_factory=list)   # e.g. ["pressure"]


@dataclass
class KBDocument:
    doc_id: str
    source_type: SourceType
    title: str
    date: str = ""
    curation: CurationStatus = CurationStatus.PENDING
    curator: str = ""
    confidentiality: str = "internal"     # DR-5-style tagging hook
    holdout: bool = False                 # gold-eval set — never indexed
    applicability: Applicability = field(default_factory=Applicability)
    chunks: list[KBChunk] = field(default_factory=list)


@dataclass
class Evidence:
    """Retriever output. Field-compatible with stage 3 RetrievedEvidence."""
    source_id: str          # "<doc_id>#<chunk_id>"
    source_type: str
    snippet: str
    score: float = 0.0


def document_from_dict(d: dict) -> KBDocument:
    app = d.get("applicability", {})
    return KBDocument(
        doc_id=d["doc_id"],
        source_type=SourceType(d["source_type"]),
        title=d.get("title", ""),
        date=d.get("date", ""),
        curation=CurationStatus(d.get("curation", "pending")),
        curator=d.get("curator", ""),
        confidentiality=d.get("confidentiality", "internal"),
        holdout=bool(d.get("holdout", False)),
        applicability=Applicability(
            unit_types=app.get("unit_types", []),
            chemistry=app.get("chemistry", []),
            equipment_classes=app.get("equipment_classes", []),
        ),
        chunks=[KBChunk(
            chunk_id=c["chunk_id"],
            text=c["text"],
            guidewords=[g.upper() for g in c.get("guidewords", [])],
            parameters=[p.lower() for p in c.get("parameters", [])],
        ) for c in d.get("chunks", [])],
    )
