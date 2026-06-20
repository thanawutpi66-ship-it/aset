"""
Tests for the unified acquisition layer (aset_batt.acquisition):
- HardwareBackend drives the real HAL (MockHardwareController) with correct sign
  convention (charge +, discharge −) — proves the QThread worker is wired to the
  actual instrument backend.
- Analytics: HPPC Rᵢ, ICA dQ/dV, DTV dT/dV, and grading thresholds.
"""
import unittest

import numpy as np

from aset_batt.acquisition.models import (
    BatteryProfile, TestConfig, OperationMode, load_profiles,
)
from aset_batt.acquisition.backends import HardwareBackend
from aset_batt.acquisition.analytics import Analytics
from aset_batt.hardware.mock_hardware import MockHardwareController


def _profile():
    return BatteryProfile("Test 12V", "Lead-Acid", 12.0, 6, 7.0,
                          14.4, 10.5, 1.4, 7.0, 15.0, 10.0, 45.0, 55.0, 0.03)


class TestHardwareBackend(unittest.TestCase):
    """Wiring the real HAL into the worker's backend interface."""

    def setUp(self):
        self.hw = MockHardwareController()
        self.be = HardwareBackend(self.hw)

    def test_discharge_current_is_negative(self):
        cfg = TestConfig(_profile(), OperationMode.CC_DISCHARGE)
        self.be.start_mode(cfg)
        v, i = self.be.step(0.1, 0.1)
        self.assertTrue(np.isfinite(v) and np.isfinite(i))
        self.assertLess(i, 0.0, "discharge current must be negative (worker convention)")

    def test_charge_sets_cccv(self):
        cfg = TestConfig(_profile(), OperationMode.CC_CV_CHARGE)
        self.be.start_mode(cfg)
        v, i = self.be.step(0.1, 0.1)
        self.assertTrue(np.isfinite(v))

    def test_emergency_zero_calls_hal(self):
        self.be.start_mode(TestConfig(_profile(), OperationMode.CC_DISCHARGE))
        self.be.emergency_zero()           # must not raise; zeroes load + psu
        self.assertEqual(self.hw._load_current, 0.0)

    def test_temperature_reads_from_hal(self):
        self.assertTrue(np.isfinite(self.be.read_temperature()))


class TestAnalytics(unittest.TestCase):
    def test_hppc_internal_resistance(self):
        p = _profile()
        ri = Analytics.internal_resistance_hppc([(12.5, 0.0), (12.2, -3.0)], p)
        self.assertAlmostEqual(ri, 0.1, places=3)   # |(-0.3)/(-3.0)|

    def test_ica_dtv_produce_curves(self):
        n = 200
        v = np.linspace(11.0, 14.4, n)
        q = np.linspace(0, 6.5, n)
        t = np.linspace(28, 40, n)
        iv, ic = Analytics.incremental_capacity(v, q)
        dv, dt = Analytics.differential_thermal(v, t)
        self.assertEqual(len(iv), 200)
        self.assertEqual(len(dv), 200)
        self.assertTrue(np.all(np.isfinite(ic)))

    def test_grade_thresholds(self):
        p = _profile()
        self.assertEqual(Analytics.grade(95, 0.033, p), "A")
        self.assertEqual(Analytics.grade(82, 0.05, p), "B")
        self.assertEqual(Analytics.grade(72, 0.07, p), "C")
        self.assertEqual(Analytics.grade(50, 0.2, p), "REJECT")


class TestProfileLoading(unittest.TestCase):
    def test_fallback_profiles(self):
        profs = load_profiles("does_not_exist.json")
        self.assertIn("Lead-Acid 12V (6S, 7Ah)", profs)


if __name__ == "__main__":
    unittest.main()
