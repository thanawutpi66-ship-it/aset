"""EKF accuracy fixes, and the live SoC/Rin display accuracy work:
  #1 the measurement update uses the OHMIC R0 (ekf.R0), not the full DCIR (self.rin),
     so RC polarisation isn't double-counted against the EKF's own V_RC state;
  #3 an OCV anchor taken on a flat plateau is seeded with LARGE SoC uncertainty so the
     filter stays correctable instead of locking onto an unreliable inversion;
  live Rin = (ekf.R0+R1) rescaled by the Arrhenius temperature multiplier — SoC-aware,
     temperature-aware, and stable (not the noisy per-sample (OCV-V)/I); AEKF (adaptive
     R) is on by default so the filter de-weights voltage on model mismatch; soc_std is
     exposed for the live "SoC ±%" display."""
import unittest

from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.characterization import build_ecm_table


def _est():
    e = StateEstimator(rated_capacity=7.0,
                       battery_model=BatteryModel("LiFePO4", 3.2, 4, 1))
    e._reset_to_soc(80.0)
    return e


class TestOhmicR0InUpdate(unittest.TestCase):
    def test_update_uses_ekf_r0_not_dcir(self):
        e = _est()
        e.update(13.0, 5.0, dt=1.0, temp=25.0)      # lazily create the EKF
        e.update_ecm(0.1234, 0.02, 1000.0)           # distinctive OHMIC R0
        captured = {}
        real_update = e._ekf.update
        def spy(v, cur, ocv, docv, r0, r_override=None):
            captured["r0"] = r0
            return real_update(v, cur, ocv, docv, r0, r_override=r_override)
        e._ekf.update = spy
        e.update(13.0, 5.0, dt=1.0, temp=25.0)
        # the R0 fed to the measurement update must be the ohmic ekf.R0, and must NOT be
        # the full DCIR (self.rin), which differs.
        self.assertAlmostEqual(captured["r0"], 0.1234, places=4)
        self.assertNotAlmostEqual(captured["r0"], e.rin, places=3)


class TestPlateauInitUncertainty(unittest.TestCase):
    def test_var_large_on_plateau_small_on_knee(self):
        e = _est()
        # LFP: ~50% sits on the flat plateau (slope < min_ocv_slope) → large variance;
        # ~3% is on the steep knee → small variance.
        self.assertGreater(e._ocv_init_var(50.0, 25.0), 100.0)
        self.assertLess(e._ocv_init_var(3.0, 25.0), 50.0)

    def test_reset_sets_ekf_covariance(self):
        e = _est()
        e.update(13.0, 0.0, dt=1.0, temp=25.0)       # create EKF
        e._reset_to_soc(50.0, soc_var=200.0)         # plateau-style anchor
        self.assertAlmostEqual(float(e._ekf.P[0, 0]), 200.0, places=3)
        e._reset_to_soc(50.0)                         # firm endpoint anchor (default ~1)
        self.assertAlmostEqual(float(e._ekf.P[0, 0]), 1.0, places=3)


class TestLiveRinAccuracy(unittest.TestCase):
    """Live Rin must be temperature-aware AND SoC-aware, and returned alongside soc_std."""

    def test_temperature_raises_live_rin(self):
        model = BatteryModel("LiFePO4", series_cells=8)
        e_cold = StateEstimator(50.0, model); e_cold._reset_to_soc(50.0)
        e_warm = StateEstimator(50.0, model); e_warm._reset_to_soc(50.0)
        r_cold = e_cold.update(25.6, 5.0, dt=1.0, temp=-10.0)["rin"]
        r_warm = e_warm.update(25.6, 5.0, dt=1.0, temp=40.0)["rin"]
        self.assertGreater(r_cold, r_warm)



    def test_soc_std_and_adaptive_r_exposed(self):
        e = _est()
        result = e.update(13.0, 5.0, dt=1.0, temp=25.0)
        self.assertIn("soc_std", result)
        self.assertGreaterEqual(result["soc_std"], 0.0)
        self.assertTrue(e._ekf.adaptive_r)          # AEKF on by default


if __name__ == "__main__":
    unittest.main()
