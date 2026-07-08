"""Regression test: IEC capacity calculators no longer crash on empty data.

aset_batt/app/auto_controller.py's _run_capacity_test populates voltage_data/
current_data/time_data inside a `while self.is_profile_running:` loop with no
pre-loop guard — aborting a profile test before the first sample leaves these
lists empty, which used to reach these calculators and raise
ZeroDivisionError during the abort-results step instead of failing gracefully.
See tests/test_auto_controller_profile.py for the integration-level abort test
that exercises the real trigger path through AutoController.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.iec61960_standard import IEC61960Standard


class TestBatteryModelCapacityEmptyData(unittest.TestCase):
    def test_empty_arrays_return_zeroed_result_not_raise(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        res = m.calculate_iec61960_capacity([], [], [], discharge_rate=1.0)
        self.assertEqual(res["capacity_ah"], 0.0)
        self.assertEqual(res["average_voltage_v"], 0.0)
        self.assertEqual(res["discharge_time_hours"], 0)

    def test_zero_discharge_rate_does_not_raise(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        res = m.calculate_iec61960_capacity([12.0, 12.0], [1.0, 1.0], [0.0, 1.0],
                                            discharge_rate=0.0)
        self.assertEqual(res["expected_time_hours"], 0.0)

    def test_mismatched_length_still_raises_valueerror(self):
        """The pre-existing length-mismatch guard must still work — only the
        empty-but-consistent case should be newly tolerated."""
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        with self.assertRaises(ValueError):
            m.calculate_iec61960_capacity([12.0], [1.0, 1.0], [0.0, 1.0], discharge_rate=1.0)

    def test_normal_data_unaffected(self):
        """The guard must not change the numeric result when data is present.
        Note: average_voltage_v sums per-segment midpoints then divides by the
        sample count (not the segment count) — that's the pre-existing
        behavior of this function, unrelated to and unchanged by the B1 guard,
        so this test pins the real value rather than the naive mean."""
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        res = m.calculate_iec61960_capacity(
            [12.6, 12.4, 12.2], [1.0, 1.0, 1.0], [0.0, 1800.0, 3600.0], discharge_rate=1.0)
        self.assertAlmostEqual(res["average_voltage_v"], (12.5 + 12.3) / 3)
        self.assertGreater(res["capacity_ah"], 0.0)


class TestIec61960StandardCapacityEmptyData(unittest.TestCase):
    def test_empty_arrays_return_zeroed_result_not_raise(self):
        std = IEC61960Standard(battery_capacity_ah=7.0, battery_type="LeadAcid",
                               nominal_voltage=12.0)
        res = std.calculate_capacity([], [], [])
        self.assertEqual(res["capacity_ah"], 0.0)
        self.assertEqual(res["average_voltage"], 0.0)
        self.assertEqual(res["discharge_time_hours"], 0)

    def test_mismatched_length_still_raises_valueerror(self):
        std = IEC61960Standard(battery_capacity_ah=7.0, battery_type="LeadAcid",
                               nominal_voltage=12.0)
        with self.assertRaises(ValueError):
            std.calculate_capacity([12.0], [1.0, 1.0], [0.0, 1.0])

    def test_normal_data_unaffected(self):
        std = IEC61960Standard(battery_capacity_ah=7.0, battery_type="LeadAcid",
                               nominal_voltage=12.0)
        res = std.calculate_capacity([12.6, 12.4], [1.0, 1.0], [0.0, 3600.0])
        self.assertAlmostEqual(res["average_voltage"], 12.5)
        self.assertGreater(res["capacity_ah"], 0.0)


if __name__ == "__main__":
    unittest.main()
