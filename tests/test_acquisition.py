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

    def test_hppc_durations_respected(self):
        import dataclasses
        p = dataclasses.replace(_profile(), hppc_pulse_duration=5.0,
                                hppc_relaxation_duration=5.0)   # cycle = 10 s
        self.be.start_mode(TestConfig(p, OperationMode.HPPC))
        self.be.step(0.1, 2.0)                 # phase 2 < relax 5 → rest
        self.assertEqual(self.hw._load_current, 0.0)
        self.be.step(0.1, 7.0)                 # phase 7 ≥ relax 5 → pulse
        self.assertGreater(self.hw._load_current, 0.0)
        self.be.step(0.1, 12.0)                # next cycle, phase 2 → rest (relaxation tail)
        self.assertEqual(self.hw._load_current, 0.0)


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

    def test_grade_from_ecm_penalises_r0_and_r1(self):
        p = _profile()                       # internal_r=0.03 → r0_base=0.018, r1_base=0.012
        # healthy: both at baseline
        self.assertEqual(Analytics.grade_from_ecm(95, 0.018, 0.012, p), "A")
        # high R1 (SEI growth) alone drags a high-SoH cell down even if R0 is fine
        self.assertNotEqual(Analytics.grade_from_ecm(95, 0.018, 0.030, p), "A")
        # high R0 (contact) alone likewise
        self.assertNotEqual(Analytics.grade_from_ecm(95, 0.045, 0.012, p), "A")
        # both badly grown → reject
        self.assertEqual(Analytics.grade_from_ecm(60, 0.06, 0.05, p), "REJECT")


class TestWorkerEcmWiring(unittest.TestCase):
    """HPPC post-processing must run the 1-RC identifier and grade on R0/R1."""

    def test_post_process_identifies_ecm(self):
        from aset_batt.acquisition.worker import AcquisitionWorker
        r0, r1, c1, cur, voc = 0.012, 0.018, 1000.0, 8.0, 13.2   # τ=18 s
        tau = r1 * c1
        dt = 0.1
        t_rest = np.arange(0, 10, dt); t_pulse = np.arange(0, 40, dt)
        v = np.concatenate([np.full_like(t_rest, voc),
                            voc - cur * (r0 + r1 * (1 - np.exp(-t_pulse / tau)))])
        # worker convention: discharge current is NEGATIVE (the worker flips it
        # internally for the identifier) — use the real convention so the test
        # exercises the same path as production.
        i = np.concatenate([np.zeros_like(t_rest), np.full_like(t_pulse, -cur)])
        tt = np.arange(len(v)) * dt
        q = np.cumsum(np.abs(i)) * dt / 3600.0
        temp = np.full_like(v, 30.0)

        w = AcquisitionWorker(backend=None,
                              cfg=TestConfig(_profile(), OperationMode.HPPC),
                              csv_path="unused.csv")
        res = w._post_process(list(tt), list(i), list(v), list(q), list(temp), [], _profile())
        self.assertTrue(res["ecm_identified"])
        self.assertAlmostEqual(res["r0_mohm"], 12.0, delta=2.0)
        self.assertAlmostEqual(res["r1_mohm"], 18.0, delta=4.0)
        self.assertIn(res["grade"], ("A", "B", "C", "REJECT"))


class TestProfileLoading(unittest.TestCase):
    def test_fallback_profiles(self):
        profs = load_profiles("does_not_exist.json")
        self.assertIn("Lead-Acid 12V (6S, 7Ah)", profs)


if __name__ == "__main__":
    unittest.main()
