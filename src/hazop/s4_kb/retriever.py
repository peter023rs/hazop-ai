"""
retriever.py — The Stage 2 retriever that replaces stage 3's MockRetriever.

`KBRetriever.retrieve(query, k, filters)` has exactly the signature of
stage 3's `RetrieverInterface.retrieve`, and returns `Evidence` objects that
are field-compatible with stage 3's `RetrievedEvidence`. Duck-typing already
works; `as_l3_retriever()` additionally wraps it in a genuine subclass of the
L3 abstract interface (so isinstance checks pass) when the L3 repo is on the
path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .index import HybridIndex, load_corpus
from .schema import Evidence


class KBRetriever:
    """Stage 2 KB service facade: corpus dir in, RetrieverInterface out."""

    def __init__(self, corpus_dir: str | Path,
                 index: Optional[HybridIndex] = None):
        self.index = index or HybridIndex()
        self.report = self.index.ingest(load_corpus(corpus_dir))

    def retrieve(self, query: str, k: int = 5,
                 filters: Optional[dict] = None) -> list[Evidence]:
        return self.index.search(query, k=k, filters=filters)


def as_l3_retriever(kb: KBRetriever):
    """Wrap a KBRetriever as a real subclass of the stage 3
    RetrieverInterface, re-emitting evidence as L3 RetrievedEvidence.
    Requires the hazop_L3 folder on sys.path."""
    from hazop.s3_are.reasoner.schema import RetrieverInterface, RetrievedEvidence  # L3

    class _L3KBRetriever(RetrieverInterface):
        def retrieve(self, query, k=5, filters=None):
            return [RetrievedEvidence(
                source_id=e.source_id,
                source_type=e.source_type,
                snippet=e.snippet,
                score=e.score,
            ) for e in kb.retrieve(query, k=k, filters=filters)]

    return _L3KBRetriever()
