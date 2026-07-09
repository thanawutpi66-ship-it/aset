"""Regression test: the live rin from the EKF path must follow the chemistry's
SoC U-shape relative to where its R0/R1 were established — not stay flat.

Root cause: StateEstimator.update()'s EKF branch computed
rin = (R0+R1) * temp_mult with no SoC term at all (the SoC-dependent path only
existed when a full ECM table was loaded, which no live mode ever loads). A
real IEC discharge's logged Resistance_mOhm went 66→65 mΩ — mildly FALLING —
across an entire 100%→cutoff sweep, when lead-acid resistance genuinely rises
toward empty. The chemistry model itself already knows the shape
(rin_params['soc_coeff'], the same term _calculate_base_rin uses); it just was
never applied on this path.

The shape is applied RELATIVE to _ecm_fit_soc (the SoC where the current R0/R1
were measured), so the calibrated point itself is untouched and only the trend
away from it follows the chemistry curve.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


def _est():
    model = BatteryModel("LeadAcid", 2.0, 6, 1)
    return StateEstimator(rated_capacity=5.3, battery_model=model)


class TestLiveRinFollowsSocShape(unittest.TestCase):
    def _rin_at_soc(self, est, soc):
        est.soc = soc
        if est._ekf is not None:
            est._ekf.set_soc(soc)
        state = est.update(est.battery_model.get_ocv_from_soc(soc), 0.0,
                           dt=0.1, temp=25.0)
        return state["rin"]

    def test_rin_rises_moving_away_from_the_fit_soc(self):
        est = _est()
        est.update(12.9, 0.0, dt=0.1, temp=25.0)      # create the EKF
        est.update_ecm(0.025, 0.068, 73.5)            # fit lands at soc=50 (default)
        r_at_fit = self._rin_at_soc(est, 50.0)
        r_low = self._rin_at_soc(est, 5.0)
        self.assertGreater(r_low, r_at_fit)

    def test_rin_unchanged_at_the_fit_soc_itself(self):
        est = _est()
        est.soc = 90.0
        est.update(12.9, 0.0, dt=0.1, temp=25.0)
        est.update_ecm(0.025, 0.068, 73.5)            # anchors _ecm_fit_soc at ~90
        r = self._rin_at_soc(est, est._ecm_fit_soc)
        # At the anchor the shape ratio is exactly 1 → pure (R0+R1)*temp_mult.
        temp_mult = est.battery_model.temp_rin_multiplier(25.0)
        self.assertAlmostEqual(r, (0.025 + 0.068) * temp_mult, places=6)

    def test_step_detector_also_anchors_the_shape(self):
        est = _est()
        est.soc = 80.0
        for _ in range(3):
            est.update(12.9, 0.0, dt=0.1, temp=25.0)
        soc_at_edge = est.soc     # SoC when the edge sample arrives
        est.update(12.9 - 0.533 * 0.03, -0.533, dt=0.1, temp=25.0)
        self.assertTrue(est._r0_calibrated)
        # The anchor must capture the SoC at DETECTION time — the same update()
        # call may legitimately move soc afterwards (the freshly-enabled EKF
        # voltage update runs later in the call), so compare against the value
        # at the edge, not the post-call one.
        self.assertAlmostEqual(est._ecm_fit_soc, soc_at_edge, delta=1.0)


if __name__ == "__main__":
    unittest.main()
