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
from aset_batt.acquisition.analysis import analyze_series
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

    def test_cc_discharge_respects_c_rate(self):
        # _profile(): capacity 7.0 Ah, max_discharge_a 7.0 A
        p = _profile()
        p.discharge_c_rate = 0.2
        cfg = TestConfig(p, OperationMode.CC_DISCHARGE)
        self.be.start_mode(cfg)
        self.assertAlmostEqual(self.hw._load_current, 1.4, places=2)

        p.discharge_c_rate = 0.5
        cfg = TestConfig(p, OperationMode.CC_DISCHARGE)
        self.be.start_mode(cfg)
        self.assertAlmostEqual(self.hw._load_current, 3.5, places=2)

        # Clamped by max_discharge_a (7.0 A)
        p.discharge_c_rate = 2.0
        cfg = TestConfig(p, OperationMode.CC_DISCHARGE)
        self.be.start_mode(cfg)
        self.assertAlmostEqual(self.hw._load_current, 7.0, places=2)

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


class TestMixedPolarityHPPC(unittest.TestCase):
    def test_regen_heavy_hppc_is_not_misclassified_as_a_charge(self):
        """HPPC may spend longer in regen than discharge; it still contains
        discharge-pulse evidence and must not have its electrical grade blocked
        merely because the median current is negative."""
        t = np.arange(101, dtype=float)
        i = np.where(t < 75.0, -1.0, 1.0)  # 3x more regen throughput than discharge
        result = analyze_series(
            t, i, np.full_like(t, 12.6), np.full_like(t, 25.0),
            np.zeros_like(t), _profile(), is_hppc=True, fit_ecm=False,
        )
        self.assertFalse(any("test was a CHARGE" in w for w in result["quality_warnings"]))

    def test_charge_only_record_remains_flagged(self):
        t = np.arange(101, dtype=float)
        i = np.full_like(t, -1.0)
        result = analyze_series(
            t, i, np.full_like(t, 12.6), np.full_like(t, 25.0),
            np.zeros_like(t), _profile(), is_hppc=False, fit_ecm=False,
        )
        self.assertTrue(any("test was a CHARGE" in w for w in result["quality_warnings"]))


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


class TestWorkerEcmAndDcirWiring(unittest.TestCase):
    """HPPC post-processing fits the 1-RC ECM (R1/C1 are resolvable at 5 Hz; R0 by
    extrapolation) AND reports the single-step DCIR@~250 ms as a cross-check."""

    def test_post_process_fits_ecm_and_reports_dcir(self):
        from aset_batt.acquisition.worker import AcquisitionWorker
        # voc 12.80 (was 13.2): a rested OCV must sit WITHIN the chemistry's
        # calibrated curve — 13.2 V is above the LeadAcid 100% point (12.888 V),
        # i.e. synthetic surface charge, which the pipeline now correctly clamps
        # out of the sag/CCA arithmetic (making sag legitimately 0 for that input).
        r0, r1, c1, cur, voc = 0.012, 0.018, 1000.0, 8.0, 12.80   # τ=18 s
        tau = r1 * c1
        dt = 0.1
        t_rest = np.arange(0, 10, dt); t_pulse = np.arange(0, 40, dt)
        v = np.concatenate([np.full_like(t_rest, voc),
                            voc - cur * (r0 + r1 * (1 - np.exp(-t_pulse / tau)))])
        # worker convention (post-normalization): current reaching _post_process is
        # discharge-POSITIVE — the sign is flipped once at the backend boundary in run().
        i = np.concatenate([np.zeros_like(t_rest), np.full_like(t_pulse, cur)])
        tt = np.arange(len(v)) * dt
        q = np.cumsum(np.abs(i)) * dt / 3600.0
        temp = np.full_like(v, 30.0)

        w = AcquisitionWorker(backend=None,
                              cfg=TestConfig(_profile(), OperationMode.HPPC),
                              csv_path="unused.csv")
        res = w._post_process(list(tt), list(i), list(v), list(q), list(temp), _profile())
        # clean 1-RC data → the fit recovers R0/R1; the DCIR cross-check is also reported.
        self.assertTrue(res["ecm_identified"])
        self.assertGreaterEqual(res["ecm_r2"], 0.9)
        self.assertAlmostEqual(res["r0_mohm"], 12.0, delta=3.0)
        self.assertAlmostEqual(res["r1_mohm"], 18.0, delta=5.0)
        self.assertGreater(res["dcir_mohm"], 0.0)        # single-step DCIR cross-check
        self.assertGreater(res["voltage_sag_v"], 0.0)    # load metric is populated
        # HPPC has valid electrical evidence but is not a verified C10 capacity
        # test, so the evidence gate withholds the *overall* grade while keeping
        # the resistance grade available for diagnosis.
        self.assertEqual(res["grade"], "REVIEW")
        self.assertIn(res["electrical_grade"], ("A", "B", "C", "REJECT"))


class TestProfileLoading(unittest.TestCase):
    def test_fallback_profiles(self):
        profs = load_profiles("does_not_exist.json")
        self.assertIn("Lead-Acid 12V (6S, 7Ah)", profs)


if __name__ == "__main__":
    unittest.main()
