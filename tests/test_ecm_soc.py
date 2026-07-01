"""SoC-dependent ECM: build_ecm_table interpolation + the EKF consuming it so the
RC dynamics track SoC instead of a single fixed fit."""
import unittest

from aset_batt.core.characterization import build_ecm_table
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.battery_model import BatteryModel


class TestBuildEcmTable(unittest.TestCase):
    def test_interpolates_to_5pct_grid(self):
        # R0/R1 rise toward empty; C1 falls
        t = build_ecm_table([90, 50, 10],
                            r0_list=[0.010, 0.012, 0.020],
                            r1_list=[0.006, 0.008, 0.016],
                            c1_list=[1200, 1000, 600])
        self.assertEqual(sorted(t.keys())[0], 0)
        self.assertEqual(sorted(t.keys())[-1], 100)
        self.assertEqual(len(t), 21)                      # 0..100 step 5
        # midpoint 50% equals the measured value there
        self.assertAlmostEqual(t[50]["r0"], 0.012, places=4)
        # 30% lies between the 10% and 50% fits → R0 between 0.012 and 0.020
        self.assertTrue(0.012 < t[30]["r0"] < 0.020)
        # monotonic rise of R0 toward empty
        self.assertGreater(t[10]["r0"], t[90]["r0"])

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            build_ecm_table([50], [0.01], [0.006], [1000])
        with self.assertRaises(ValueError):
            build_ecm_table([90, 10], [0.01], [0.006, 0.016], [1000, 600])


class TestEstimatorUsesSocEcm(unittest.TestCase):
    def _est(self):
        e = StateEstimator(rated_capacity=7.0,
                           battery_model=BatteryModel("LiFePO4", 3.2, 4, 1))
        e.set_initial_soc(80.0)
        return e

    def test_ekf_rc_tracks_soc(self):
        e = self._est()
        e.set_ecm_table(build_ecm_table([90, 50, 10],
                                        r0_list=[0.010, 0.012, 0.020],
                                        r1_list=[0.006, 0.008, 0.016],
                                        c1_list=[1200, 1000, 600]))
        # one update near 80% → EKF R1 should be near the ~80% interpolated value
        e.update(13.0, 5.0, dt=1.0, temp=25.0)
        r1_high = e._ekf.R1
        # force the estimator to a low SoC and update again → R1 should be larger
        e._reset_to_soc(15.0)
        e.update(11.5, 5.0, dt=1.0, temp=25.0)
        r1_low = e._ekf.R1
        self.assertGreater(r1_low, r1_high)             # R1 rises toward empty

    def test_no_table_falls_back(self):
        e = self._est()
        self.assertIsNone(e.ecm_table)
        # update_ecm still works when no table is set
        e.update(13.0, 5.0, dt=1.0, temp=25.0)
        e.update_ecm(0.011, 0.007, 1100.0)
        self.assertAlmostEqual(e._ekf.R1, 0.007, places=4)


if __name__ == "__main__":
    unittest.main()
