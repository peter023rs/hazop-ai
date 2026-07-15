"""
guidewords.py — Deterministic guideword x parameter deviation generation.

Implements FR-03-01 (V1) / FR-ARE-1 (Fable): apply the full IEC 61882 guideword
set to each process parameter for every node.

Pure logic — no LLM, no upstream dependency. Fully testable on mock nodes.

Design note on FR-ARE-1: we do NOT suppress deviations we consider physically
implausible (e.g. "Reverse Flow" where a check valve exists). Per the Fable
requirement, reverse flow must still be raised, with the check valve listed as a
*safeguard*, not used to hide the deviation. So this module deliberately
generates the full matrix; plausibility filtering (if any) is a downstream
concern that only re-labels, never deletes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .schema import Parameter


class Guideword(str, Enum):
    NO = "No/Not"
    MORE = "More"
    LESS = "Less"
    AS_WELL_AS = "As Well As"
    PART_OF = "Part Of"
    REVERSE = "Reverse"
    OTHER_THAN = "Other Than"
    EARLY = "Early"
    LATE = "Late"
    BEFORE = "Before"
    AFTER = "After"


# Human-readable meaning, mirrors Appendix A of the V1 spec.
GUIDEWORD_MEANING: dict[Guideword, str] = {
    Guideword.NO: "Complete negation of the design intent",
    Guideword.MORE: "Quantitative increase",
    Guideword.LESS: "Quantitative decrease",
    Guideword.AS_WELL_AS: "Qualitative increase (additional component/phase)",
    Guideword.PART_OF: "Qualitative decrease (missing component)",
    Guideword.REVERSE: "Opposite of the design intent",
    Guideword.OTHER_THAN: "Complete substitution — different material/condition",
    Guideword.EARLY: "Timing deviation — occurs too soon",
    Guideword.LATE: "Timing deviation — occurs too late",
    Guideword.BEFORE: "Sequence error — out of order (before)",
    Guideword.AFTER: "Sequence error — out of order (after)",
}


# Which guidewords are meaningful for which parameters. This is not suppression
# of hazards — it is avoiding nonsensical combinations (e.g. "Reverse Temperature"
# has no HAZOP meaning). Timing/sequence guidewords apply to procedural/batch
# parameters. Continuous parameters use the quantitative set.
# A conservative mapping: when unsure, include the guideword.
_QUANTITATIVE = {Guideword.NO, Guideword.MORE, Guideword.LESS,
                 Guideword.AS_WELL_AS, Guideword.PART_OF, Guideword.OTHER_THAN}
_WITH_REVERSE = _QUANTITATIVE | {Guideword.REVERSE}
_TIMING = {Guideword.EARLY, Guideword.LATE, Guideword.BEFORE, Guideword.AFTER}

PARAMETER_GUIDEWORDS: dict[Parameter, set[Guideword]] = {
    Parameter.FLOW: _WITH_REVERSE | _TIMING,          # flow can reverse; batch timing relevant
    Parameter.PRESSURE: _QUANTITATIVE,
    Parameter.TEMPERATURE: _QUANTITATIVE,
    Parameter.LEVEL: _QUANTITATIVE,
    Parameter.COMPOSITION: {Guideword.PART_OF, Guideword.AS_WELL_AS,
                            Guideword.OTHER_THAN, Guideword.NO},
    Parameter.REACTION: {Guideword.NO, Guideword.MORE, Guideword.LESS,
                         Guideword.REVERSE, Guideword.OTHER_THAN,
                         Guideword.AS_WELL_AS, Guideword.EARLY, Guideword.LATE},
    Parameter.PHASE: {Guideword.AS_WELL_AS, Guideword.PART_OF, Guideword.OTHER_THAN},
}


@dataclass(frozen=True)
class Deviation:
    """A guideword + parameter combination, e.g. 'More Pressure'."""
    parameter: Parameter
    guideword: Guideword

    @property
    def label(self) -> str:
        return f"{self.guideword.value} {self.parameter.value.capitalize()}"

    def __str__(self) -> str:
        return self.label


def deviations_for_parameter(parameter: Parameter) -> list[Deviation]:
    """All meaningful guideword deviations for one parameter, in canonical order."""
    applicable = PARAMETER_GUIDEWORDS.get(parameter, set(Guideword))
    # preserve the canonical guideword order from the enum
    ordered = [gw for gw in Guideword if gw in applicable]
    return [Deviation(parameter=parameter, guideword=gw) for gw in ordered]


def deviations_for_parameters(parameters: list[Parameter]) -> list[Deviation]:
    """Full deviation matrix across a node's parameters."""
    result: list[Deviation] = []
    for p in parameters:
        result.extend(deviations_for_parameter(p))
    return result
