"""
grounding.py — MDL-10 audit: tag grounding of released worksheet output.

MDL-10: >= 98% of equipment/instrument tags referenced in AI output SHALL
exist in the topology graph (hard gate; failures block release).

The pipeline already enforces this per finding (Stage A gate in core._grounded
validates `referenced_tags`). This module is the independent measurement on
the OTHER side of the gate: it scans the text of the worksheet as released —
finding texts and action entries — re-extracts every tag-like reference, and
checks each against the topology. That closes two holes a gate-side count
cannot see:

  * a generator that mentions a fabricated tag in its text without declaring
    it in `referenced_tags` slips past the gate; the audit catches it;
  * the MDL-10 number reported upward is measured on released output, not
    inferred from rejection counts.

Tag extraction is deterministic, two-pass:

  1. pattern pass — tokens shaped like plant tags (letters-hyphen-digits,
     e.g. P-101, PSV-201, LAH-2001A), excluding standards-body references
     (IEC-61882, ISO-15926, ...) which are citations, not tag references;
  2. literal pass — exact occurrences of known topology tags the digit
     pattern cannot see (e.g. a mock tag like "V-SUCT").

Anything the pattern pass finds that is not a known tag is a grounding
violation. The extractor deliberately over-triggers rather than under-
triggers: a false flag costs a reviewer seconds; a missed fabricated tag is
exactly the failure MDL-10 exists to block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hazop.l3_reasoner.reasoner.schema import TopologyGraph
from hazop.l3_reasoner.reasoner.worksheet import WorksheetRow

TARGET_GROUNDING_PRECISION = 0.98    # Fable MDL-10

# letters-hyphen-(optional letters)digits(optional suffix): P-101, PSV-201,
# TK-100, LAH-2001A, V-201B
_TAG_PATTERN = re.compile(r"\b[A-Z]{1,4}-[A-Z]{0,3}\d{1,5}[A-Z]{0,2}\b")

# standards bodies whose clause references look like tags (IEC-61882,
# ISO-15926, API-521 ...): citations, not equipment references.
_STANDARDS_PREFIXES = frozenset({
    "IEC", "ISO", "ISA", "API", "ANSI", "OSHA", "NFPA", "CFR", "ASME",
    "IEEE", "EN", "DIN", "DN", "PN", "UL", "ATEX",
})


def known_tags(topology: TopologyGraph) -> set[str]:
    """Every name a worksheet may legitimately reference: equipment tags
    plus line tags carried on the edges."""
    tags = set(topology.all_tags())
    tags.update(e.line_tag for e in topology.edges if e.line_tag)
    return tags


def extract_tag_references(text: str, known: set[str]) -> set[str]:
    """All tag references in `text`: pattern-shaped candidates (standards
    citations excluded) plus literal occurrences of known tags."""
    found = {m.group(0) for m in _TAG_PATTERN.finditer(text)
             if m.group(0).split("-", 1)[0] not in _STANDARDS_PREFIXES}
    for tag in known:
        if tag not in found and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(tag)}(?![A-Za-z0-9])", text):
            found.add(tag)
    return found


@dataclass(frozen=True)
class TagReference:
    """One (worksheet location, tag) reference and its grounding verdict."""
    deviation_label: str
    kind: str              # "cause" | "consequence" | "safeguard" | "action"
    tag: str
    grounded: bool
    text: str              # the finding/action text the tag appears in


@dataclass
class GroundingAudit:
    node_id: str
    references: list[TagReference]

    @property
    def total(self) -> int:
        return len(self.references)

    @property
    def grounded_count(self) -> int:
        return sum(1 for r in self.references if r.grounded)

    @property
    def violations(self) -> list[TagReference]:
        return [r for r in self.references if not r.grounded]

    @property
    def precision(self) -> float:
        """Share of tag references that exist in the topology (the MDL-10
        number). Vacuously 1.0 on a worksheet that references no tags."""
        return self.grounded_count / self.total if self.total else 1.0

    @property
    def passed(self) -> bool:
        return self.precision >= TARGET_GROUNDING_PRECISION

    def summary(self) -> str:
        lines = [
            f"Output tag-grounding audit (MDL-10) — {self.node_id}: "
            f"{'PASS' if self.passed else 'FAIL'}",
            f"  Grounding precision: {100 * self.precision:.1f}% "
            f"({self.grounded_count}/{self.total} tag references, "
            f"target >= {100 * TARGET_GROUNDING_PRECISION:.0f}%)",
        ]
        for v in self.violations:
            lines.append(f"  UNGROUNDED: {v.tag} in {v.kind} of "
                         f"[{v.deviation_label}]: {v.text[:80]}")
        return "\n".join(lines)


def audit_grounding(rows: list[WorksheetRow],
                    topology: TopologyGraph) -> GroundingAudit:
    """Scan released worksheet text (findings + actions; rejected findings
    are NOT released and not scanned) and verdict every tag reference."""
    known = known_tags(topology)
    node_id = rows[0].node_id if rows else "(empty)"

    refs: list[TagReference] = []

    def scan(label: str, kind: str, text: str):
        for tag in sorted(extract_tag_references(text, known)):
            refs.append(TagReference(
                deviation_label=label, kind=kind, tag=tag,
                grounded=tag in known, text=text))

    for row in rows:
        for kind, findings in (("cause", row.causes),
                               ("consequence", row.consequences),
                               ("safeguard", row.safeguards)):
            for f in findings:
                scan(row.deviation.label, kind, f.text)
        for action in row.actions:
            scan(row.deviation.label, "action", action)

    return GroundingAudit(node_id=node_id, references=refs)
