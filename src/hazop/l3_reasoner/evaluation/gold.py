"""
gold.py — Gold-standard evaluation set: schema + loader (README next-step 2).

A gold set is a hand-authored record of what a competent HAZOP team would
produce for a study node:

  - the deviations that MUST be covered           (V1 AC-03 / Fable MDL-7)
  - the causes that MUST be identified             (Fable MDL-9)
  - expected consequences / safeguards             (informational metrics)

Gold sets are JSON files so process-safety engineers can author and extend
them without touching Python. Each expected item carries `match_any`: a list
of keyword groups. A generated finding matches the item if ANY group has ALL
of its keywords appearing (case-insensitive) in that finding's text. This
keeps scoring deterministic — no LLM-as-judge in the loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GoldItem:
    """One expected finding (a cause, consequence, or safeguard)."""
    description: str                  # human-readable, e.g. "Pump P-101 trips"
    match_any: list[list[str]]        # [[kw AND kw], OR [kw AND kw], ...]


@dataclass
class GoldDeviation:
    """Expected findings for one deviation row."""
    causes: list[GoldItem] = field(default_factory=list)
    consequences: list[GoldItem] = field(default_factory=list)
    safeguards: list[GoldItem] = field(default_factory=list)


@dataclass
class GoldNode:
    """The full gold standard for one study node."""
    node_id: str
    description: str
    expected_deviations: list[str]              # deviation labels that must appear
    deviations: dict[str, GoldDeviation]        # label -> expected findings

    def all_items(self, slot: str) -> list[tuple[str, GoldItem]]:
        """(deviation_label, item) pairs for one slot across all deviations."""
        out = []
        for label, gd in self.deviations.items():
            for item in getattr(gd, slot):
                out.append((label, item))
        return out


def _items(raw: list[dict]) -> list[GoldItem]:
    return [GoldItem(description=r["description"], match_any=r["match_any"])
            for r in raw]


def load_gold(path: str | Path) -> GoldNode:
    with open(path) as f:
        raw = json.load(f)
    deviations = {
        label: GoldDeviation(
            causes=_items(d.get("causes", [])),
            consequences=_items(d.get("consequences", [])),
            safeguards=_items(d.get("safeguards", [])),
        )
        for label, d in raw.get("deviations", {}).items()
    }
    return GoldNode(
        node_id=raw["node_id"],
        description=raw.get("description", ""),
        expected_deviations=raw["expected_deviations"],
        deviations=deviations,
    )


# The gold set shipped for the mock pump/vessel process.
PUMP_VESSEL_GOLD = Path(__file__).parent / "gold_pump_vessel.json"
