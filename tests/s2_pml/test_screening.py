"""Deviation screening: simulator seam + heuristic fallback
(FR-PML-3/4/5)."""

import unittest

from hazop.s2_pml import (HeuristicScreener, ScreeningCase,
                                            ScreeningResult,
                                            SimulatorInterface, screen_case)


def _case(equipment_type="pump", deviation="blocked_outlet", **conditions):
    return ScreeningCase(case_id="CASE-1", equipment_tag="2401-P-001",
                         equipment_type=equipment_type, deviation=deviation,
                         conditions=conditions)


def _sim_result(converged, **overrides):
    fields = dict(case_id="CASE-1", source="simulation", model_id="hysys",
                  model_version="14.5", converged=converged, reliable=True,
                  label="raw adapter label", estimate={"pressure": 11.0},
                  basis="flowsheet 2401-A blocked-outlet case")
    fields.update(overrides)
    return ScreeningResult(**fields)


class _FakeSimulator(SimulatorInterface):
    name, version = "hysys", "14.5"

    def __init__(self, result):
        self.result = result

    def screen(self, case):
        return self.result


class TestHeuristicScreener(unittest.TestCase):
    def test_pump_deadhead_multiplier_rule(self):
        r = screen_case(_case(normal_discharge_pressure=8.0))
        self.assertEqual(r.source, "heuristic")
        self.assertFalse(r.reliable)
        self.assertIn("ESTIMATE", r.label)
        self.assertAlmostEqual(r.estimate["deadhead_pressure_low"], 9.6)
        self.assertAlmostEqual(r.estimate["deadhead_pressure_high"], 12.0)
        self.assertIn("1.2-1.5", r.basis)

    def test_pump_curve_value_preferred_over_multiplier(self):
        r = screen_case(_case(normal_discharge_pressure=8.0,
                              shutoff_pressure=10.4))
        self.assertEqual(r.estimate, {"deadhead_pressure": 10.4})
        self.assertIn("pump curve", r.basis)

    def test_missing_data_stays_qualitative_with_warning(self):
        r = screen_case(_case())
        self.assertEqual(r.estimate, {})
        self.assertTrue(any("no normal_discharge_pressure" in w
                            for w in r.warnings))

    def test_unknown_case_refuses_instead_of_guessing(self):
        r = screen_case(_case(equipment_type="vessel", deviation="no_flow"))
        self.assertEqual(r.estimate, {})
        self.assertIn("no heuristic rule", r.basis)
        self.assertTrue(any("unscreened" in w for w in r.warnings))

    def test_reverse_flow_defers_to_topology_reasoner(self):
        r = screen_case(_case(deviation="reverse_flow"))
        self.assertIn("check valve", r.basis)
        self.assertIn("VERIFIED", r.basis)

    def test_vessel_and_exchanger_bounds(self):
        r = screen_case(_case(equipment_type="vessel",
                              max_source_pressure=12.0))
        self.assertEqual(r.estimate, {"bounding_pressure": 12.0})
        r = screen_case(_case(equipment_type="heat_exchanger",
                              deviation="no_flow",
                              heating_medium_temperature=180.0))
        self.assertEqual(r.estimate, {"bounding_temperature": 180.0})


class TestSimulatorSeam(unittest.TestCase):
    def test_converged_simulation_is_reliable_and_labeled(self):
        r = screen_case(_case(), simulator=_FakeSimulator(_sim_result(True)))
        self.assertEqual(r.source, "simulation")
        self.assertTrue(r.reliable)
        # FR-PML-4: case id, model id/version, convergence in the label
        self.assertIn("CASE-1", r.label)
        self.assertIn("hysys v14.5", r.label)
        self.assertIn("converged", r.label)

    def test_nonconverged_simulation_flagged_unreliable(self):
        # even when the adapter claims reliable=True
        r = screen_case(_case(), simulator=_FakeSimulator(
            _sim_result(False, reliable=True)))
        self.assertFalse(r.reliable)
        self.assertIn("NOT CONVERGED", r.label)
        self.assertTrue(any("non-converged" in w for w in r.warnings))

    def test_no_model_falls_back_to_heuristics(self):
        r = screen_case(_case(normal_discharge_pressure=8.0),
                        simulator=_FakeSimulator(None))
        self.assertEqual(r.source, "heuristic")
        self.assertIn("ESTIMATE", r.label)


if __name__ == "__main__":
    unittest.main()
