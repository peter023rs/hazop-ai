"""
metrics.py — Score reasoner output against a gold standard (README step 2).

Two thresholded metrics from the specs:

  - deviation coverage >= 0.85   (V1 AC-03 / Fable MDL-7)
  - cause recall       >= 0.80   (Fable MDL-9)

Consequence recall, safeguard recall and the grounding-gate hallucination
rate (MDL-10) are reported alongside as informational metrics.

Matching is deterministic keyword matching (see gold.py) scoped to the row
with the gold item's deviation label — a "blocked outlet" cause found under
the wrong deviation does not count.
"""

from __future__ import annotations

from dataclasses import dataclass

from hazop.l3_reasoner.reasoner.worksheet import WorksheetRow

from .gold import GoldItem, GoldNode

TARGET_DEVIATION_COVERAGE = 0.85   # V1 AC-03 / Fable MDL-7
TARGET_CAUSE_RECALL = 0.80         # Fable MDL-9


def _item_matched(item: GoldItem, texts: list[str]) -> bool:
    """True if any keyword group has all its keywords in one finding text."""
    lowered = [t.lower() for t in texts]
    for group in item.match_any:
        kws = [k.lower() for k in group]
        if any(all(k in t for k in kws) for t in lowered):
            return True
    return False


@dataclass
class SlotRecall:
    slot: str                    # "causes" | "consequences" | "safeguards"
    matched: list[str]           # gold item descriptions that were found
    missed: list[str]            # gold item descriptions not found

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.missed)

    @property
    def recall(self) -> float:
        return len(self.matched) / self.total if self.total else 1.0


@dataclass
class EvalResult:
    node_id: str
    deviation_coverage: float
    missing_deviations: list[str]
    causes: SlotRecall
    consequences: SlotRecall
    safeguards: SlotRecall
    hallucination_rate: float    # rejected / (rejected + accepted) findings

    @property
    def passed(self) -> bool:
        return (self.deviation_coverage >= TARGET_DEVIATION_COVERAGE
                and self.causes.recall >= TARGET_CAUSE_RECALL)

    def summary(self) -> str:
        def pct(x: float) -> str:
            return f"{100 * x:.1f}%"

        def mark(ok: bool) -> str:
            return "PASS" if ok else "FAIL"

        lines = [
            f"Gold-standard evaluation — {self.node_id}: "
            f"{mark(self.passed)}",
            f"  Deviation coverage: {pct(self.deviation_coverage)} "
            f"(target >= {pct(TARGET_DEVIATION_COVERAGE)}) "
            f"[{mark(self.deviation_coverage >= TARGET_DEVIATION_COVERAGE)}]",
            f"  Cause recall:       {pct(self.causes.recall)} "
            f"({len(self.causes.matched)}/{self.causes.total}, "
            f"target >= {pct(TARGET_CAUSE_RECALL)}) "
            f"[{mark(self.causes.recall >= TARGET_CAUSE_RECALL)}]",
            f"  Consequence recall: {pct(self.consequences.recall)} "
            f"({len(self.consequences.matched)}/{self.consequences.total}, "
            f"informational)",
            f"  Safeguard recall:   {pct(self.safeguards.recall)} "
            f"({len(self.safeguards.matched)}/{self.safeguards.total}, "
            f"informational)",
            f"  Hallucination rate: {pct(self.hallucination_rate)} "
            f"(grounding-gate rejections, MDL-10)",
        ]
        if self.missing_deviations:
            lines.append("  Missing deviations: "
                         + ", ".join(self.missing_deviations))
        for slot in (self.causes, self.consequences, self.safeguards):
            for miss in slot.missed:
                lines.append(f"  MISSED {slot.slot[:-1]}: {miss}")
        return "\n".join(lines)


def _slot_recall(rows_by_label: dict[str, WorksheetRow], gold: GoldNode,
                 slot: str) -> SlotRecall:
    matched, missed = [], []
    for label, item in gold.all_items(slot):
        row = rows_by_label.get(label)
        texts = [f.text for f in getattr(row, slot)] if row else []
        (matched if _item_matched(item, texts) else missed).append(
            f"[{label}] {item.description}")
    return SlotRecall(slot=slot, matched=matched, missed=missed)


def evaluate_node(rows: list[WorksheetRow], gold: GoldNode) -> EvalResult:
    rows_by_label = {r.deviation.label: r for r in rows}

    expected = set(gold.expected_deviations)
    covered = expected & set(rows_by_label)
    coverage = len(covered) / len(expected) if expected else 1.0

    accepted = sum(len(r.causes) + len(r.consequences) + len(r.safeguards)
                   for r in rows)
    rejected = sum(len(r.rejected_findings) for r in rows)
    hallucination_rate = rejected / (rejected + accepted) if (rejected + accepted) else 0.0

    return EvalResult(
        node_id=gold.node_id,
        deviation_coverage=coverage,
        missing_deviations=sorted(expected - covered),
        causes=_slot_recall(rows_by_label, gold, "causes"),
        consequences=_slot_recall(rows_by_label, gold, "consequences"),
        safeguards=_slot_recall(rows_by_label, gold, "safeguards"),
        hallucination_rate=hallucination_rate,
    )
