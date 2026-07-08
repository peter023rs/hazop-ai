"""
embed.py — The dense half of hybrid retrieval (MDL-2), behind a swap seam.

`EmbedderInterface` is the contract; `HashingEmbedder` is the offline stand-in
(feature-hashing bag of words → unit vector). It is deterministic, needs no
model download and no API key, so the whole Stage 2 pipeline runs today —
exactly the StubLLM trick from stage 3. Swap in a real embedding model
(sentence-transformers on-prem, or a cloud API where AR-3 permits) by
implementing `embed()`; nothing else in the index changes.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

from .bm25 import tokenize


class EmbedderInterface(ABC):
    dim: int

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a unit-length vector for `text`."""


class HashingEmbedder(EmbedderInterface):
    """Feature hashing: each token maps to a (bucket, sign) via its digest.
    Captures token overlap like BM25 but in vector form — enough to exercise
    the hybrid fusion machinery end-to-end."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in tokenize(text):
            h = hashlib.sha1(tok.encode()).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] % 2 else -1.0
            v[idx] += sign
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
