"""Tests for the KB: curation gate, holdout, hybrid retrieval, contract shape."""

import unittest
from pathlib import Path

from hazop.l2_knowledge.kb import HybridIndex, KBRetriever, load_corpus
from hazop.l2_knowledge.kb.bm25 import BM25, tokenize
from hazop.l2_knowledge.kb.schema import (Applicability, CurationStatus, KBChunk, KBDocument,
                       SourceType)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "data" / "corpus"


def make_doc(doc_id, status=CurationStatus.APPROVED, holdout=False,
             text="blocked outlet causes more pressure", **app):
    return KBDocument(
        doc_id=doc_id, source_type=SourceType.STANDARD, title=doc_id,
        curation=status, holdout=holdout,
        applicability=Applicability(**app),
        chunks=[KBChunk(chunk_id="c1", text=text,
                        guidewords=["MORE"], parameters=["pressure"])],
    )


class TestCurationAndHoldout(unittest.TestCase):
    def test_pending_documents_never_retrievable(self):
        idx = HybridIndex()
        idx.ingest([make_doc("OK"), make_doc("PENDING",
                                             status=CurationStatus.PENDING)])
        ids = {e.source_id for e in idx.search("more pressure", k=10)}
        self.assertTrue(any(i.startswith("OK#") for i in ids))
        self.assertFalse(any(i.startswith("PENDING#") for i in ids))
        self.assertIn("PENDING", idx.report.excluded)

    def test_rejected_documents_never_retrievable(self):
        idx = HybridIndex()
        idx.ingest([make_doc("BAD", status=CurationStatus.REJECTED)])
        self.assertEqual(idx.search("more pressure", k=10), [])

    def test_holdout_excluded_even_when_approved(self):
        idx = HybridIndex()
        idx.ingest([make_doc("GOLD", holdout=True), make_doc("OK")])
        ids = {e.source_id for e in idx.search("more pressure", k=10)}
        self.assertFalse(any(i.startswith("GOLD#") for i in ids))


class TestRetrieval(unittest.TestCase):
    def setUp(self):
        self.kb = KBRetriever(CORPUS)

    def test_returns_relevant_evidence_first(self):
        evs = self.kb.retrieve("more pressure", k=3,
                               filters={"guideword": "MORE",
                                        "parameter": "pressure"})
        self.assertTrue(evs)
        self.assertIn("pressure", evs[0].snippet.lower())

    def test_contract_shape_matches_l3_retrieved_evidence(self):
        ev = self.kb.retrieve("no flow", k=1)[0]
        for f in ("source_id", "source_type", "snippet", "score"):
            self.assertTrue(hasattr(ev, f))
        self.assertIn("#", ev.source_id)  # doc#chunk provenance

    def test_filter_boost_prefers_tagged_chunk(self):
        idx = HybridIndex()
        idx.ingest([
            make_doc("TAGGED", text="valve stuck causes trouble"),
            KBDocument(doc_id="UNTAGGED", source_type=SourceType.STANDARD,
                       title="", curation=CurationStatus.APPROVED,
                       chunks=[KBChunk(chunk_id="c1",
                                       text="valve stuck causes trouble")]),
        ])
        evs = idx.search("valve stuck", k=2,
                         filters={"guideword": "MORE", "parameter": "pressure"})
        self.assertTrue(evs[0].source_id.startswith("TAGGED#"))

    def test_hard_applicability_filter_drops_document(self):
        idx = HybridIndex()
        idx.ingest([make_doc("AIR", unit_types=["air_station"]),
                    make_doc("GENERIC")])
        ids = {e.source_id for e in idx.search(
            "more pressure", k=10, filters={"unit_type": "refinery"})}
        self.assertFalse(any(i.startswith("AIR#") for i in ids))
        self.assertTrue(any(i.startswith("GENERIC#") for i in ids))

    def test_pending_and_holdout_corpus_docs_excluded(self):
        report = self.kb.report
        self.assertIn("VENDOR-BULLETIN-77", report.excluded)
        self.assertIn("GOLD-EVAL-NODE1", report.excluded)


class TestBM25(unittest.TestCase):
    def test_scores_rank_matching_doc_higher(self):
        bm = BM25()
        bm.fit([tokenize("pump trip causes no flow"),
                tokenize("relief valve sizing basis")])
        s = bm.scores(tokenize("pump trip"))
        self.assertGreater(s[0], s[1])


if __name__ == "__main__":
    unittest.main()
