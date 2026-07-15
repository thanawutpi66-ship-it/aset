"""
Tests for the accuracy upgrades:
- SoH-adjusted (effective) capacity in coulomb counting
- live SoH from a full→empty capacity sweep
- current-offset tare
- 2-state EKF basic stability + sign
"""
import unittest
import numpy as np

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.soc_ekf import SoCEKF


class TestEffectiveCapacity(unittest.TestCase):
    def test_soh_shrinks_capacity(self):
        e = StateEstimator(10.0, BatteryModel("LiFePO4"))
        self.assertAlmostEqual(e.effective_capacity(), 10.0, places=3)
        e.set_soh(80.0)
        self.assertAlmostEqual(e.effective_capacity(), 8.0, places=3)

    def test_aged_battery_soc_drops_faster(self):
        # Same Ah removed → an aged cell (lower SoH) loses a larger SoC fraction.
        e_new = StateEstimator(10.0, BatteryModel("LiFePO4"))
        e_new.use_ekf = False               # isolate coulomb counting
        e_new._reset_to_soc(100.0)
        e_aged = StateEstimator(10.0, BatteryModel("LiFePO4"))
        e_aged.use_ekf = False
        e_aged.set_soh(80.0)
        e_aged._reset_to_soc(100.0)
        # discharge 1A for 1h = 1 Ah on a steep-ish voltage (avoid OCV correction noise)
        e_new.update(3.30, 1.0, dt=3600)
        e_aged.update(3.30, 1.0, dt=3600)
        # new: 1/10 = 10% drop → ~90 ; aged: 1/8 = 12.5% drop → ~87.5
        self.assertGreater(e_new.soc, e_aged.soc)


class TestCurrentTare(unittest.TestCase):
    def test_manual_offset_applied(self):
        e = StateEstimator(10.0, BatteryModel("LiFePO4"))
        e.use_ekf = False
        e.current_offset = 0.5           # sensor reads 0.5 A high
        e._reset_to_soc(50.0)
        # true current 0 (reads 0.5); offset removes it → near-zero coulomb movement
        e.update(3.28, 0.5, dt=3600)
        self.assertAlmostEqual(e.ah_accumulated, 0.0, delta=0.05)


class TestLiveSoH(unittest.TestCase):
    def test_soh_from_full_to_empty_sweep(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        e = StateEstimator(rated_capacity=5.3, battery_model=m)
        e._reset_to_soc(100.0)
        e._cap_counting = True               # simulate a 100% anchor just fired
        e._cap_counter_ah = 4.0              # ~4.0 Ah delivered over the full sweep
        # A real full sweep has plenty of time (and, since the DCIR pre-edge fix, an
        # edge right at REST->DISCHARGE) to get a real R0 fit well before nearing
        # empty — simulate that here, since the loaded zero-anchor now refuses to
        # trust its IR-compensated estimate on an uncalibrated rin (a real Quick
        # Scan run hard-reset SoC 24.25%->0.00% off exactly that uncalibrated
        # guess — see test_zero_anchor_requires_calibration.py).
        e._r0_calibrated = True
        # now force the empty anchor by feeding a low voltage at small current —
        # sustained for a few samples (the anchor now requires _anchor_min_sustain_s
        # of continuous below-threshold readings, not a single sample, to avoid
        # firing on one noisy reading — see the real-hardware bug this gates against)
        v_empty = m.get_ocv_from_soc(0.0)
        for _ in range(4):
            e.update(v_empty * 0.99, 0.2, dt=1.0)
        self.assertGreater(e.soh, 50.0)
        self.assertLess(e.soh, 100.0)
        self.assertAlmostEqual(e.measured_capacity_ah, e.soh / 100.0 * 5.3, delta=0.1)


class TestAblationFlags(unittest.TestCase):
    def test_peukert_flag_gates(self):
        e = StateEstimator(5.3, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.use_peukert = True
        self.assertGreater(e._peukert_dah(5.3, 1.0), 1.0)
        e.use_peukert = False
        self.assertEqual(e._peukert_dah(5.3, 1.0), 1.0)

    def test_eta_flag_gates(self):
        e = StateEstimator(5.3, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.use_eta = True
        self.assertLess(e._coulomb_eta(95.0, -1.0), 1.0)   # gassing loss near full
        e.use_eta = False
        self.assertEqual(e._coulomb_eta(95.0, -1.0), 1.0)

    def test_trapezoidal_integration(self):
        # ramped current: trapezoid ≠ rectangular. First step seeds with no history.
        e = StateEstimator(10.0, BatteryModel("LiFePO4"))
        e.use_ekf = False
        e._reset_to_soc(50.0)
        e.update(3.30, 0.0, dt=10.0)     # seed _last_current = 0
        before = e.ah_accumulated
        e.update(3.30, 2.0, dt=3600.0)   # trapezoid: (0+2)/2*1h = 1 Ah, not 2
        self.assertAlmostEqual(e.ah_accumulated - before, 1.0, places=3)


class TestEKF(unittest.TestCase):
    def test_ekf_discharge_decreases_soc_and_stable(self):
        ekf = SoCEKF(soc0=80.0, r0=0.03, r1=0.02, c1=1500.0)
        # soc_delta per step: 2 A · 1 s on 50 Ah → 0.0011 %/step (caller-computed)
        soc_delta = 2.0 * 1.0 / 3600.0 / 50.0 * 100.0
        for _ in range(200):
            ekf.predict(current=2.0, dt=1.0, soc_delta_pct=soc_delta)
            ekf.update(v_meas=12.4, current=2.0, ocv_pack=12.6,
                       docv_dsoc_pack=0.01, r0=0.03)
        self.assertLess(ekf.soc, 80.0)
        self.assertTrue(np.all(np.isfinite(ekf.P)))
        self.assertTrue(0.0 <= ekf.soc <= 100.0)

    def test_peukert_affects_ekf_output(self):
        # Regression: Peukert must change the EKF SoC (previously the EKF predict
        # recomputed coulomb itself and ignored Peukert entirely).
        def run(use_peukert):
            m = BatteryModel("LeadAcid", 2.0, 6, 1)
            e = StateEstimator(5.3, m)
            e.use_peukert = use_peukert
            e.use_ocv = False           # isolate the coulomb/Peukert path in the EKF
            e._reset_to_soc(90.0)
            for _ in range(60):         # high-rate discharge where Peukert bites
                e.update(12.0, 5.3, dt=10.0, temp=25.0)
            return e.soc
        self.assertLess(run(True), run(False) - 0.5)   # Peukert depletes faster

    def test_ekf_set_soc_resets(self):
        ekf = SoCEKF(soc0=50.0, r0=0.03, r1=0.02, c1=1500.0)
        ekf.set_soc(100.0)
        self.assertAlmostEqual(ekf.soc, 100.0, places=6)
        self.assertAlmostEqual(ekf.v_rc, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
