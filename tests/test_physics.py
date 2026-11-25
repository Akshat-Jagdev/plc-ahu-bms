"""Unit tests for the plant model in sim/physics.py: no PLC and no Modbus needed.

Run from the project root:

    .venv/bin/python -m unittest tests.test_physics -v
"""
import math
import unittest

from sim.physics import AhuInputs, AhuPlant

SAT_SETPOINT_C = 13.0


def run(seconds, damper_open=False, valve_pct=0.0, occupancy=0, co2_start_ppm=None):
    """Advance a fresh plant with constant inputs and return it."""
    plant = AhuPlant(occupancy=occupancy)
    if co2_start_ppm is not None:
        plant.s.co2_ppm = plant.s._co2_lag_ppm = co2_start_ppm
    inputs = AhuInputs(sf_cmd=True, rf_cmd=True, exd_open=damper_open, fad_open=damper_open,
                       chwv_pct=valve_pct, chw_pump_cmd=True)
    for _ in range(seconds):
        plant.step(inputs, 1.0)
    return plant


def all_finite(state):
    return all(math.isfinite(v) for v in (state.t_space_c, state.t_mix_c, state.t_sat_c,
                                          state.t_ra_c, state.co2_ppm, state.dp_filter_pa,
                                          state.damper_pos))


class CoilAndZone(unittest.TestCase):
    # the design cooling load is the occupied one (21 kW), so these run with people present
    def test_half_open_valve_holds_13c_with_the_dampers_closed(self):
        state = run(3600, valve_pct=52.5, occupancy=10).s
        self.assertLess(abs(state.t_sat_c - SAT_SETPOINT_C), 0.3)
        self.assertLess(abs(state.t_space_c - 24.0), 0.5)
        self.assertTrue(all_finite(state))

    def test_open_dampers_need_most_of_the_valve_at_35c_outdoor(self):
        state = run(3600, damper_open=True, valve_pct=87.0, occupancy=10).s
        self.assertLess(abs(state.t_sat_c - SAT_SETPOINT_C), 0.3)
        half_open = run(3600, damper_open=True, valve_pct=52.5, occupancy=10).s.t_sat_c
        self.assertGreater(half_open, 20.0)


class Ventilation(unittest.TestCase):
    def test_co2_steady_state_with_the_dampers_open(self):
        state = run(3600, damper_open=True, valve_pct=87.0, occupancy=100, co2_start_ppm=420.0).s
        self.assertAlmostEqual(state.co2_ppm, 636.7, delta=1.0)

    def test_co2_steady_state_with_the_dampers_closed(self):
        state = run(20000, valve_pct=52.5, occupancy=100, co2_start_ppm=420.0).s
        self.assertAlmostEqual(state.co2_ppm, 1720.0, delta=2.0)

    def test_closed_damper_time_constant_is_2500s(self):
        # 420 + 1300 * (1 - exp(-600 / 2500)) after ten minutes at 100 occupants
        state = run(600, valve_pct=52.5, occupancy=100, co2_start_ppm=420.0).s
        self.assertAlmostEqual(state.co2_ppm, 697.0, delta=3.0)


class WithoutAirflow(unittest.TestCase):
    def test_only_the_zone_temperature_moves(self):
        plant = AhuPlant()
        for _ in range(600):
            plant.step(AhuInputs(), 1.0)
        state = plant.s
        self.assertEqual(state.co2_ppm, 450.0)
        self.assertEqual(state.dp_filter_pa, 0.0)
        self.assertEqual(state.damper_pos, 0.0)
        self.assertGreater(state.t_space_c, 24.0)
        self.assertTrue(all_finite(state))


class Filter(unittest.TestCase):
    def test_clog_accumulates_only_while_the_fan_runs(self):
        plant = AhuPlant()
        plant.clog_filter(1.0)
        for _ in range(300):
            plant.step(AhuInputs(sf_cmd=True), 1.0)
        # airflow starts on the second step: 80 Pa base + 299 Pa of clog
        self.assertAlmostEqual(plant.s.dp_filter_pa, 379.0, delta=1.0)
        plant.step(AhuInputs(), 1.0)
        self.assertEqual(plant.s.dp_filter_pa, 0.0)


class Dampers(unittest.TestCase):
    def test_travel_takes_ten_seconds_each_way(self):
        plant = AhuPlant()
        for _ in range(10):
            plant.step(AhuInputs(sf_cmd=True, fad_open=True), 1.0)
        self.assertAlmostEqual(plant.s.damper_pos, 1.0, places=6)
        for _ in range(10):
            plant.step(AhuInputs(sf_cmd=True, fad_open=False), 1.0)
        self.assertAlmostEqual(plant.s.damper_pos, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
