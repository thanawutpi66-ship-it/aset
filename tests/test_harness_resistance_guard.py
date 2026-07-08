"""Phase D2 regression: defense-in-depth for BatteryConfig.harness_resistance_ohm.

Two independent layers, per the approved Phase D decision:
  1. Config-entry validation (ConfigManager.validate_config) rejects an implausible
     harness_resistance_ohm (> HARNESS_RESISTANCE_MAX_OHM, or negative) outright.
  2. A runtime warn-and-skip guard (aset_batt.acquisition.analysis.
     _correct_for_harness_r) refuses to apply a correction that would remove more
     than 50% of a specific raw ohmic reading, even if the configured value passed
     entry validation — because a harness value calibrated against one specimen can
     still be disproportionate relative to a DIFFERENT (e.g. genuinely degraded)
     pack's true resistance, and applying it blindly would floor that pack's
     DCIR/R0 near zero and grade it "A" with no indication anything was wrong.
"""
import unittest

import numpy as np

from aset_batt.core.config import ConfigManager, HARNESS_RESISTANCE_MAX_OHM
from aset_batt.acquisition.analysis import (
    _correct_for_harness_r, _HARNESS_MAX_REMOVAL_FRACTION, analyze_series,
)
from aset_batt.acquisition.models import BatteryProfile


class TestConfigEntryValidation(unittest.TestCase):
    def test_default_zero_is_valid(self):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = 0.0
        self.assertTrue(cfg.validate_config())

    def test_realistic_calibrated_value_is_valid(self):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = 0.065  # the one value calibrated on this rig
        self.assertTrue(cfg.validate_config())

    def test_value_at_ceiling_is_valid(self):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = HARNESS_RESISTANCE_MAX_OHM
        self.assertTrue(cfg.validate_config())

    def test_value_above_ceiling_rejected(self):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = HARNESS_RESISTANCE_MAX_OHM + 0.01
        self.assertFalse(cfg.validate_config())

    def test_decimal_typo_rejected(self):
        """The exact scenario D2 exists for: a 0.65 vs 0.065 typo."""
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = 0.65
        self.assertFalse(cfg.validate_config())

    def test_negative_value_rejected(self):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = -0.01
        self.assertFalse(cfg.validate_config())


class TestRuntimeWarnAndSkipGuard(unittest.TestCase):
    def test_correction_applied_when_under_threshold(self):
        # harness = 30% of raw -> comfortably under the 50% cap
        corrected, warnings = _correct_for_harness_r(0.100, 0.030, "DCIR", [])
        self.assertAlmostEqual(corrected, 0.070, places=6)
        self.assertEqual(warnings, [])

    def test_correction_skipped_when_over_threshold(self):
        # harness = 65% of raw -> must be refused
        corrected, warnings = _correct_for_harness_r(0.100, 0.065, "DCIR", [])
        self.assertAlmostEqual(corrected, 0.100, places=6)  # unchanged, NOT floored
        self.assertEqual(len(warnings), 1)
        self.assertIn("harness_resistance_ohm", warnings[0])
        self.assertIn("DCIR", warnings[0])

    def test_boundary_at_exactly_50_percent_is_skipped(self):
        # harness == 50% of raw: ">=" per _HARNESS_MAX_REMOVAL_FRACTION, so the
        # boundary itself is treated as "too large," not "just barely fine."
        corrected, warnings = _correct_for_harness_r(0.100, 0.050, "DCIR", [])
        self.assertAlmostEqual(corrected, 0.100, places=6)
        self.assertEqual(len(warnings), 1)

    def test_zero_harness_is_a_no_op(self):
        corrected, warnings = _correct_for_harness_r(0.100, 0.0, "DCIR", [])
        self.assertAlmostEqual(corrected, 0.100, places=6)
        self.assertEqual(warnings, [])

    def test_zero_raw_reading_is_a_no_op(self):
        corrected, warnings = _correct_for_harness_r(0.0, 0.03, "DCIR", [])
        self.assertAlmostEqual(corrected, 0.0, places=6)
        self.assertEqual(warnings, [])

    def test_warnings_list_not_mutated_in_place(self):
        """Callers rely on the returned list, not in-place mutation of the input —
        guards against a future refactor accidentally aliasing state across calls."""
        original = []
        corrected, warnings = _correct_for_harness_r(0.100, 0.065, "DCIR", original)
        self.assertEqual(original, [])
        self.assertEqual(len(warnings), 1)

    def test_threshold_constant_is_fifty_percent(self):
        self.assertAlmostEqual(_HARNESS_MAX_REMOVAL_FRACTION, 0.5)


def _profile(harness_r_ohm):
    return BatteryProfile(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
        harness_r_ohm=harness_r_ohm,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=0.03,
    )


class TestRuntimeGuardPreventsFalseGradeAAtIntegrationLevel(unittest.TestCase):
    """The scenario the plan explicitly calls out: a would-be REJECT->A flip via an
    oversized harness correction must be BLOCKED, not silently applied."""

    def test_oversized_harness_correction_is_skipped_grade_stays_reject(self):
        # A genuinely degraded pack: DCIR steps read 0.08 ohm raw (ratio 2.67x the
        # 0.03 ohm baseline -> REJECT). A harness value that would remove more than
        # half of that (0.065/0.08 = 81%) must be refused — the pack must NOT be
        # graded as if its true DCIR were ~0.015.
        # time_s uses a realistic ~5 Hz rig dt (0.2 s) so the post-edge sample lands
        # well inside identify_dcir's staleness gate (_DCIR_MAX_STEP_DT); a 1 s gap
        # would (correctly, but not what this test is about) get dropped as stale.
        ia = np.array([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])
        raw_r = 0.08
        v_rest = 12.0
        va = np.array([v_rest, v_rest, v_rest, v_rest - 5.0 * raw_r,
                        v_rest - 5.0 * raw_r, v_rest - 5.0 * raw_r])
        temp = np.full(6, 25.0)
        cap = np.zeros(6)
        time_s = np.arange(6, dtype=float) * 0.2

        profile = _profile(harness_r_ohm=0.065)
        res = analyze_series(time_s, ia, va, temp, cap, profile, is_hppc=False)

        self.assertTrue(res["dcir_measured"])
        self.assertAlmostEqual(res["dcir_mohm"] / 1000.0, raw_r, places=3)
        self.assertEqual(res["grade"], "REJECT")
        self.assertTrue(any("harness_resistance_ohm" in w for w in res["quality_warnings"]))


if __name__ == "__main__":
    unittest.main()
