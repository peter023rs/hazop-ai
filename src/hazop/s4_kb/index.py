"""
index.py — Corpus loading (with the curation gate) and the hybrid index.

Ingestion rules enforced here, not in the retriever, so nothing unapproved
can ever be retrieved:
  * curation gate (FR-AGM-2): only CurationStatus.APPROVED documents index;
    pending/rejected are reported but never searchable.
  * holdout exclusion (DDR-04/MDL-7): documents flagged `holdout` (the
    gold-standard eval set) are physically excluded from the index.

Search = hybrid BM25 + dense (MDL-2) fused with Reciprocal Rank Fusion (no
score normalization needed), then a structured-metadata boost: chunks whose
guideword/parameter tags match the reasoner's deviation filters rank up, and
hard applicability filters (unit_type / equipment_class) drop non-applicable
documents entirely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .bm25 import BM25, tokenize
from .embed import EmbedderInterface, HashingEmbedder, cosine
from .schema import CurationStatus, Evidence, KBChunk, KBDocument, document_from_dict


@dataclass
class IngestReport:
    indexed: list[str] = field(default_factory=list)     # doc_ids in the index
    excluded: dict = field(default_factory=dict)         # doc_id -> reason

    def summary(self) -> str:
        lines = [f"indexed {len(self.indexed)} document(s): "
                 + ", ".join(self.indexed)]
        for doc_id, reason in self.excluded.items():
            lines.append(f"excluded {doc_id}: {reason}")
        return "\n".join(lines)


def load_corpus(corpus_dir: str | Path) -> list[KBDocument]:
    docs = []
    for p in sorted(Path(corpus_dir).glob("*.json")):
        with open(p, encoding="utf-8") as f:
            docs.append(document_from_dict(json.load(f)))
    return docs


@dataclass
class _Entry:
    doc: KBDocument
    chunk: KBChunk


class HybridIndex:
    RRF_K = 60          # standard reciprocal-rank-fusion constant
    FILTER_BOOST = 0.5  # rank-score multiplier per matching structured tag

    def __init__(self, embedder: Optional[EmbedderInterface] = None):
        self.embedder = embedder or HashingEmbedder()
        self._entries: list[_Entry] = []
        self._bm25 = BM25()
        self._vecs: list[list[float]] = []
        self.report = IngestReport()

    # ---- ingestion --------------------------------------------------------

    def ingest(self, docs: list[KBDocument]) -> IngestReport:
        for doc in docs:
            if doc.curation is not CurationStatus.APPROVED:
                self.report.excluded[doc.doc_id] = (
                    f"curation gate (FR-AGM-2): status={doc.curation.value}")
                continue
            if doc.holdout:
                self.report.excluded[doc.doc_id] = (
                    "gold-standard holdout (DDR-04): never indexed")
                continue
            for chunk in doc.chunks:
                self._entries.append(_Entry(doc, chunk))
            self.report.indexed.append(doc.doc_id)
        self._bm25.fit([tokenize(e.chunk.text) for e in self._entries])
        self._vecs = [self.embedder.embed(e.chunk.text) for e in self._entries]
        return self.report

    # ---- search -----------------------------------------------------------

    def search(self, query: str, k: int = 5,
               filters: Optional[dict] = None) -> list[Evidence]:
        if not self._entries:
            return []
        filters = filters or {}

        # hard applicability filters drop whole documents
        eligible = [i for i, e in enumerate(self._entries)
                    if self._applicable(e.doc, filters)]
        if not eligible:
            return []

        lex = self._bm25.scores(tokenize(query))
        qv = self.embedder.embed(query)
        den = [cosine(qv, self._vecs[i]) for i in range(len(self._entries))]

        fused = {i: 0.0 for i in eligible}
        for scores in (lex, den):
            ranked = sorted(eligible, key=lambda i: scores[i], reverse=True)
            for rank, i in enumerate(ranked):
                if scores[i] > 0:
                    fused[i] += 1.0 / (self.RRF_K + rank + 1)

        # structured deviation boost (guideword/parameter from the reasoner)
        gw = str(filters.get("guideword", "")).upper()
        pm = str(filters.get("parameter", "")).lower()
        for i in eligible:
            c = self._entries[i].chunk
            boost = 1.0
            if gw and gw in c.guidewords:
                boost += self.FILTER_BOOST
            if pm and pm in c.parameters:
                boost += self.FILTER_BOOST
            fused[i] *= boost

        top = sorted(eligible, key=lambda i: fused[i], reverse=True)[:k]
        best = fused[top[0]] if top and fused[top[0]] > 0 else 1.0
        out = []
        for i in top:
            if fused[i] <= 0:
                continue
            e = self._entries[i]
            out.append(Evidence(
                source_id=f"{e.doc.doc_id}#{e.chunk.chunk_id}",
                source_type=e.doc.source_type.value,
                snippet=e.chunk.text,
                score=round(fused[i] / best, 3),
            ))
        return out

    @staticmethod
    def _applicable(doc: KBDocument, filters: dict) -> bool:
        """A document with an empty applicability list is generic (applies
        everywhere); a non-empty list must contain the requested value."""
        for key, values in (("unit_type", doc.applicability.unit_types),
                            ("equipment_class", doc.applicability.equipment_classes),
                            ("chemistry", doc.applicability.chemistry)):
            want = filters.get(key)
            if want and values and want not in values:
                return False
        return True
