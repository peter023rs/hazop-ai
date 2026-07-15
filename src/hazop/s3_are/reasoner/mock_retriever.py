"""
mock_retriever.py — A stand-in for the stage 2 KB / RAG service.

Implements RetrieverInterface so the reasoner can be developed and tested with
zero dependency on Neo4j / FAISS / the real ingestion pipeline. Returns canned
evidence keyed by simple keyword matching. When stage 2 is ready, swap this for
the real retriever — the reasoner code does not change.
"""

from __future__ import annotations

import re

from .schema import RetrieverInterface, RetrievedEvidence


# A tiny hand-authored "knowledge base": (keywords) -> evidence snippets.
# Represents the kind of thing stage 2 would return from SDS / historical
# HAZOPs / standards.
_CANNED: list[tuple[set[str], RetrievedEvidence]] = [
    ({"more", "pressure"}, RetrievedEvidence(
        source_id="HIST-HAZOP-012#N4",
        source_type="historical_hazop",
        snippet="Blocked outlet with pump running led to overpressure of the "
                "downstream vessel; PSV lifted. Cause: downstream manual valve "
                "closed in error.",
        score=0.91)),
    ({"more", "pressure"}, RetrievedEvidence(
        source_id="STD-IEC61882#tbl3",
        source_type="standard",
        snippet="More Pressure: common causes include blocked/restricted outlet, "
                "excess heat input, thermal expansion of blocked-in liquid.",
        score=0.72)),
    ({"no", "flow"}, RetrievedEvidence(
        source_id="HIST-HAZOP-007#N2",
        source_type="historical_hazop",
        snippet="No flow due to pump trip / suction valve closed; consequence "
                "was loss of level control in downstream vessel.",
        score=0.88)),
    ({"reverse", "flow"}, RetrievedEvidence(
        source_id="STD-API14C#rev",
        source_type="standard",
        snippet="Reverse flow can occur on pump trip when downstream pressure "
                "exceeds discharge; check valve is the typical safeguard.",
        score=0.80)),
    ({"less", "flow"}, RetrievedEvidence(
        source_id="DS-P101#curve",
        source_type="datasheet",
        snippet="Reduced flow may arise from partial blockage, fouling, or "
                "cavitation at low NPSH.",
        score=0.65)),
    ({"more", "temperature"}, RetrievedEvidence(
        source_id="SDS-FEED#thermal",
        source_type="sds",
        snippet="Product decomposes exothermically above 180 C; runaway risk on "
                "loss of cooling.",
        score=0.83)),
    ({"more", "level"}, RetrievedEvidence(
        source_id="STD-IEC61882#tbl3-level",
        source_type="standard",
        snippet="More Level: level control failure (LIC fails) or blocked "
                "outlet while inflow continues; risk of overfill and liquid "
                "carryover downstream.",
        score=0.78)),
    ({"no", "less", "level"}, RetrievedEvidence(
        source_id="HIST-HAZOP-009#N7",
        source_type="historical_hazop",
        snippet="No level in vessel from loss of inflow (upstream tank empty, "
                "drain valve passing); pump ran dry and gas blew by to the "
                "downstream system.",
        score=0.84)),
]


class MockRetriever(RetrieverInterface):
    def retrieve(self, query: str, k: int = 5, filters=None) -> list[RetrievedEvidence]:
        # Split on "/" too so compound guidewords tokenize ("No/Not" -> no, not).
        q = {w.strip(".,") for w in re.split(r"[/\s]+", query.lower()) if w}
        if filters:
            # Structured deviation from the reasoner — more reliable than
            # re-parsing the query text.
            q |= {w for w in re.split(r"[_\s]+",
                                      str(filters.get("guideword", "")).lower()) if w}
            if filters.get("parameter"):
                q.add(str(filters["parameter"]).lower())
        scored: list[tuple[float, RetrievedEvidence]] = []
        for keywords, ev in _CANNED:
            overlap = len(keywords & q)
            if overlap:
                # simple relevance: keyword overlap * stored score
                scored.append((overlap * ev.score, ev))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ev for _, ev in scored[:k]]
