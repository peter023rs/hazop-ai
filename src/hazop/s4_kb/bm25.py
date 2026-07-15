"""
bm25.py — Pure-Python Okapi BM25 (the lexical half of hybrid retrieval, MDL-2).

No dependencies, deterministic, ~50 lines. For production scale this swaps for
Elasticsearch/OpenSearch behind the same `scores()` shape; at prototype scale
(hundreds of chunks) this is exact and instant.
"""

from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: list[list[str]] = []
        self._df: dict[str, int] = {}
        self._avgdl = 0.0

    def fit(self, docs: list[list[str]]) -> None:
        self._docs = docs
        self._df = {}
        for toks in docs:
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1
        self._avgdl = (sum(len(d) for d in docs) / len(docs)) if docs else 0.0

    def _idf(self, term: str) -> float:
        n, df = len(self._docs), self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def scores(self, query_tokens: list[str]) -> list[float]:
        out = []
        for toks in self._docs:
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for q in query_tokens:
                f = tf.get(q, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * len(toks) / self._avgdl)
                s += self._idf(q) * f * (self.k1 + 1) / denom
            out.append(s)
        return out
