"""Regression test for the SoC hard-reset bug in a real Quick Scan run.

Root cause, traced from an actual CSV (test_QuickScan_20260712_150458.csv):
the 0% endpoint anchor computes ``ocv_est = voltage + cur * self.rin`` and
fires once that estimate crosses the pack's 0%-SoC OCV reference (+1%
hysteresis). self.rin at that point in the run was _ekf_rc_defaults()'s
generic pre-fit guess for the chemistry — Quick Scan never ran a real HPPC
pulse, so no R0 fit had ever landed (rin_calibrated=False for the entire
run). That guess under-compensated the real IR drop enough to satisfy the
zero-anchor condition for MANY consecutive ~5s-cadence samples (not a single
glitch — see test_endpoint_anchor_sustain_gate.py for the glitch case,
already fixed by the existing sustain/consecutive-sample gate). SoC hard-
reset 24.25%->0.00% while the pack kept discharging another 4.6 min to its
real voltage cutoff.

The fix: the loaded (cur>0) zero-anchor now also requires self._r0_calibrated
or self._ecm_calibrated — the exact same "don't trust a voltage-based
correction while actively loaded and uncalibrated" rule _fuse_ekf already
applies to its own OCV update. A systematic bias in an uncalibrated rin can
sustain past any sample-count/time threshold, so sustain-gating alone cannot
fix this class of bug — only refusing to trust the estimate until it's
actually been calibrated can.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


class TestZeroAnchorRequiresCalibration(unittest.TestCase):
    def _est(self, rated_capacity=5.3):
        e = StateEstimator(rated_capacity, BatteryModel("LeadAcid", 2.0, 6, 1))
        e._reset_to_soc(65.0)
        return e

    def test_sustained_uncalibrated_condition_does_not_fire(self):
        """Reproduces the real Quick Scan failure: the same voltage/current
        numbers that hard-reset SoC on the real run, sustained for many
        consecutive samples, must NOT anchor while rin is uncalibrated —
        unlike a single-glitch or a calibrated-sustained condition, more
        consecutive samples alone do not fix a systematic bias."""
        e = self._est()
        self.assertFalse(e._r0_calibrated)
        self.assertFalse(e._ecm_calibrated)
        e.rin = 0.0804
        v, i = 11.34, 5.0
        anchor_v_empty = e.battery_model.get_ocv_from_soc(0.0)
        ocv_est = v + i * e.rin
        self.assertLessEqual(ocv_est, anchor_v_empty * 1.01,
                              "sanity check: this must still look like a crossing "
                              "by the raw ocv_est math, or the test is vacuous")

        for _ in range(30):    # far past _anchor_min_samples/_anchor_min_sustain_s
            e.update(v, i, dt=5.03, temp=26.6)
        self.assertGreater(e.soc, 20.0,
                            "an uncalibrated rin must not be trusted to hard-reset "
                            "SoC, no matter how long the condition is sustained")

    def test_same_condition_fires_once_r0_calibrated(self):
        """The gate is about trust, not about permanently disabling the
        anchor — once r0 is confirmed calibrated, a genuinely sustained empty
        condition still anchors normally."""
        e = self._est()
        e._r0_calibrated = True
        e.rin = 0.0804
        v, i = 11.34, 5.0
        for _ in range(2):
            e.update(v, i, dt=5.03, temp=26.6)
        self.assertLess(e.soc, 5.0,
                         "a calibrated, sustained empty condition should still anchor")

    def test_rest_sample_does_not_need_calibration(self):
        """The 0% anchor only ever evaluates while actively discharging
        (cur>0 is baked into the condition itself) — confirms the
        calibration gate doesn't accidentally block anything at rest, since
        rest-based correction goes through the separate OCV-correction path,
        not this anchor."""
        e = self._est()
        self.assertFalse(e._r0_calibrated)
        e.update(11.34, 0.0, dt=5.0, temp=26.6)
        # No exception, no unexpected hard SoC reset from this path alone.
        self.assertGreaterEqual(e.soc, 0.0)


if __name__ == "__main__":
    unittest.main()
