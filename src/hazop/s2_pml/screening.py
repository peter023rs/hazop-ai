"""
screening.py — deviation consequence screening (Fable FR-PML-3/4/5).

FR-PML-3 wants a steady-state simulator (Aspen HYSYS; DWSIM as open-source
fallback) behind the process model; whether that lands in v1 is open issue
OI-2, and no simulator runs in this offline repo. What CAN be real today is
the seam and the discipline around it:

  * `SimulatorInterface` — the swap seam (same move as `EmbedderInterface`
    in kb/embed.py, per AR-5). A HYSYS or DWSIM adapter implements
    `screen()`; returning None means "no validated model for this case",
    which routes the case to the heuristic fallback.
  * FR-PML-4 labeling is ENFORCED by the dispatcher, not trusted from the
    adapter: every simulation result is re-labeled with its case id, model
    id/version and convergence status, and `reliable` is True only when the
    simulation converged. A non-converged screen can be shown, but never
    trusted.
  * `HeuristicScreener` — FR-PML-5: where no simulation model exists, fall
    back to engineering heuristics (the spec's own example: pump deadhead
    pressure ~ 1.2-1.5 x normal discharge, pump curve preferred when
    available) "and SHALL clearly label these as estimates" — every
    heuristic result carries `source="heuristic"`, `reliable=False`, and an
    ESTIMATE label. When the case's `conditions` carry no numbers the rule
    still answers, qualitatively, and says what data would quantify it.

Units: values in `estimate` echo whatever units the caller supplied in
`case.conditions` — this module does no unit conversion.

Deviation vocabulary used by the heuristic rules: "blocked_outlet",
"no_flow", "reverse_flow". Unknown (equipment_type, deviation) pairs return
an honest "no heuristic rule" result rather than a guess (refusal over
fabrication, FR-ARE-9 spirit).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

HEURISTIC_MODEL_ID = "hazop.l2.heuristic-screener"
HEURISTIC_VERSION = "1"

_ESTIMATE_LABEL = ("ESTIMATE — engineering heuristic, no simulation model "
                   "(FR-PML-5)")


@dataclass(frozen=True)
class ScreeningCase:
    """One deviation screening question about one equipment item."""
    case_id: str
    equipment_tag: str
    equipment_type: str          # adapter vocabulary: pump, compressor, ...
    deviation: str               # blocked_outlet | no_flow | reverse_flow
    conditions: dict = field(default_factory=dict)  # known process data


@dataclass(frozen=True)
class ScreeningResult:
    case_id: str
    source: str                  # "simulation" | "heuristic"
    model_id: str
    model_version: str
    converged: bool | None       # None = not applicable (heuristic)
    reliable: bool               # True only for a converged simulation
    label: str                   # always-visible provenance banner
    estimate: dict               # numbers/bounds, caller's units
    basis: str                   # formula + assumptions, plain words
    warnings: tuple = ()


class SimulatorInterface:
    """Seam for FR-PML-3. Adapters (Aspen HYSYS automation API, DWSIM)
    subclass this; `screen()` returns None when the simulator holds no
    validated model for the case, sending it to the heuristic fallback."""
    name = "simulator"
    version = "0"

    def screen(self, case: ScreeningCase) -> ScreeningResult | None:
        raise NotImplementedError


def screen_case(case: ScreeningCase,
                simulator: SimulatorInterface | None = None
                ) -> ScreeningResult:
    """Screen one case: simulation when a simulator is wired in and has a
    model, heuristics otherwise (FR-PML-5). Simulation results come back
    re-labeled per FR-PML-4 regardless of what the adapter set."""
    if simulator is not None:
        result = simulator.screen(case)
        if result is not None:
            return _finalize_simulation(result)
    return HeuristicScreener().screen(case)


def _finalize_simulation(result: ScreeningResult) -> ScreeningResult:
    """Enforce FR-PML-4: case id / model id+version / convergence in the
    label, reliable only when converged, non-converged visibly flagged."""
    converged = result.converged is True
    status = ("converged" if converged
              else "NOT CONVERGED — result unreliable (FR-PML-4)")
    label = (f"simulation case {result.case_id} — model {result.model_id} "
             f"v{result.model_version} — {status}")
    warnings = result.warnings
    if not converged:
        warnings = tuple(warnings) + (
            "non-converged/extrapolated simulation: do not use as a "
            "consequence screen without engineering review",)
    return dataclasses.replace(result, source="simulation", label=label,
                               converged=converged, reliable=converged,
                               warnings=warnings)


# -- heuristic rules (FR-PML-5) ---------------------------------------------
# each rule: conditions -> (estimate, basis, [warnings])

def _pump_blocked_outlet(cond: dict):
    shutoff = cond.get("shutoff_pressure")
    if shutoff is not None:
        return ({"deadhead_pressure": shutoff},
                "shutoff (deadhead) pressure taken from the supplied pump "
                "curve value — preferred over the multiplier rule",
                ["verify the curve is for the installed impeller"])
    p = cond.get("normal_discharge_pressure")
    basis = ("centrifugal pump deadhead ~ 1.2-1.5 x normal discharge "
             "pressure (rule of thumb; use the pump shutoff curve value "
             "when available)")
    warnings = ["positive-displacement pumps exceed this range — "
                "verify pump type"]
    if p is None:
        return ({}, basis,
                warnings + ["no normal_discharge_pressure supplied — "
                            "unquantified; provide it or the pump curve"])
    return ({"deadhead_pressure_low": round(1.2 * p, 3),
             "deadhead_pressure_high": round(1.5 * p, 3)}, basis, warnings)


def _compressor_blocked_outlet(cond: dict):
    relief = cond.get("relief_set_pressure")
    basis = ("blocked compressor discharge: pressure rises toward the "
             "machine's capability (settle-out / surge / stall depends on "
             "machine type); the relief set pressure bounds the system if "
             "the relief path is adequate")
    warnings = ["verify relief sizing covers the blocked-outlet case",
                "positive-displacement machines keep building pressure — "
                "verify machine type"]
    if relief is None:
        return ({}, basis,
                warnings + ["no relief_set_pressure supplied — "
                            "unquantified bound"])
    return ({"bounding_pressure": relief}, basis, warnings)


def _vessel_blocked_outlet(cond: dict):
    p = cond.get("max_source_pressure")
    basis = ("blocked outlet with continued inflow: vessel pressure "
             "approaches the maximum upstream source pressure (upstream "
             "machine deadhead / supply header maximum)")
    warnings = ["compare the bound against vessel MAWP and relief set "
                "pressure"]
    if p is None:
        return ({}, basis,
                warnings + ["no max_source_pressure supplied — "
                            "unquantified bound"])
    return ({"bounding_pressure": p}, basis, warnings)


def _hx_no_flow(cond: dict):
    t = cond.get("heating_medium_temperature")
    basis = ("loss of through-flow on the cold side: trapped fluid "
             "approaches the heating-medium supply temperature (thermal "
             "equilibrium bound); check trapped-volume thermal expansion")
    warnings = ["for cooling service the bound is the hot-side process "
                "inlet temperature instead"]
    if t is None:
        return ({}, basis,
                warnings + ["no heating_medium_temperature supplied — "
                            "unquantified bound"])
    return ({"bounding_temperature": t}, basis, warnings)


def _reverse_flow(cond: dict):
    return ({},
            "reverse flow is credible unless a check valve with VERIFIED "
            "flow direction lies on the path — confirm with the topology "
            "reasoner (TopologyReasoner.check_valves_between, verified "
            "directions only) rather than a heuristic",
            ["direction-unverified check valves must not be credited"])


class HeuristicScreener(SimulatorInterface):
    """FR-PML-5 fallback: deterministic, clearly-labeled estimates. Always
    answers (never returns None); unknown cases get an honest 'no rule'
    result instead of a guess."""
    name = HEURISTIC_MODEL_ID
    version = HEURISTIC_VERSION

    _RULES = {
        ("pump", "blocked_outlet"): _pump_blocked_outlet,
        ("compressor", "blocked_outlet"): _compressor_blocked_outlet,
        ("vessel", "blocked_outlet"): _vessel_blocked_outlet,
        ("tank", "blocked_outlet"): _vessel_blocked_outlet,
        ("heat_exchanger", "no_flow"): _hx_no_flow,
    }

    def screen(self, case: ScreeningCase) -> ScreeningResult:
        if case.deviation == "reverse_flow":
            rule = _reverse_flow
        else:
            rule = self._RULES.get((case.equipment_type, case.deviation))
        if rule is None:
            return ScreeningResult(
                case_id=case.case_id, source="heuristic",
                model_id=self.name, model_version=self.version,
                converged=None, reliable=False,
                label=_ESTIMATE_LABEL,
                estimate={},
                basis=(f"no heuristic rule for ({case.equipment_type}, "
                       f"{case.deviation}) — needs a simulation model or "
                       f"engineering review"),
                warnings=("unscreened case — do not treat as 'no "
                          "consequence'",))
        estimate, basis, warnings = rule(case.conditions)
        return ScreeningResult(
            case_id=case.case_id, source="heuristic",
            model_id=self.name, model_version=self.version,
            converged=None, reliable=False,
            label=_ESTIMATE_LABEL,
            estimate=estimate, basis=basis, warnings=tuple(warnings))
