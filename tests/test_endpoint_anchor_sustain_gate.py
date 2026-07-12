"""Regression test for a real-hardware HPPC test that got wrecked mid-run.

Root cause, traced from an actual CSV (test_20260706_180907.csv, a degraded
battery's HPPC Full Sequence): the 0% endpoint anchor computes
``ocv_est = voltage + cur * self.rin`` and fires as soon as that crosses
below the pack's 0%-SoC OCV reference (+1% hysteresis). At row 17434
(t=2071.7s), a single sample — voltage sagged just 0.01V, self.rin was
still an uncalibrated pre-fit guess (~69 mOhm, no real HPPC pulse had been
fitted into the EKF yet at that point in the run) — pushed ocv_est to
11.7368V against an 11.7443V threshold: a 0.0075V margin. SoC hard-reset
from 65.03% to 0.00% in that one sample, mid-pulse, corrupting the entire
test's grade (it came back REJECT / SoH N/A / Capacity 0.00 Ah / "discharge
did not reach cutoff").

The fix: require the anchor condition to hold continuously for
_anchor_min_sustain_s (mirrors the existing Peukert sustain gate) before
firing, so one glitchy sample can't hard-reset SoC — a genuinely empty/full
pack stays past the threshold far longer than that.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


class TestZeroAnchorSustainGate(unittest.TestCase):
    def _est(self, rated_capacity=5.3):
        e = StateEstimator(rated_capacity, BatteryModel("LeadAcid", 2.0, 6, 1))
        e._reset_to_soc(65.0)
        return e

    def test_single_marginal_sample_does_not_fire(self):
        """Reproduces the exact real-CSV failure: one sample crossing the 0%
        threshold by a hair must NOT hard-reset SoC to 0."""
        e = self._est()
        # Same numbers as the real CSV row: rin ~69.23 mOhm, I=5.01A, V=11.39V.
        e.rin = 0.06923
        # v retuned 11.39->11.36 after the LeadAcid Ea/R 4000->2200 K correction:
        # update() recomputes self.rin each call ((R0+R1)*temp_mult), and the
        # weaker (physically-correct) multiplier raised the recomputed rin at
        # 28.6 C from ~69 to ~75 mOhm, so the ORIGINAL voltage no longer sat a
        # hair past the 0%-anchor threshold. Same marginal-crossing spirit kept.
        v, i = 11.36, 5.01
        anchor_v_empty = e.battery_model.get_ocv_from_soc(0.0)
        ocv_est = v + i * e.rin
        # sanity-check this reproduces the actual marginal crossing before asserting
        # the fix — if this ever stops being true the test would be vacuous.
        self.assertLessEqual(ocv_est, anchor_v_empty * 1.01)

        e.update(v, i, dt=0.1, temp=28.6)
        self.assertGreater(e.soc, 60.0,
                           "a single marginal sample must not hard-reset SoC to 0")

    def test_sustained_genuine_empty_condition_still_fires(self):
        """The gate must not block a REAL empty condition — just require it to
        persist past a single noisy sample."""
        e = self._est()
        e.rin = 0.06923
        # v retuned 11.39->11.36 after the LeadAcid Ea/R 4000->2200 K correction:
        # update() recomputes self.rin each call ((R0+R1)*temp_mult), and the
        # weaker (physically-correct) multiplier raised the recomputed rin at
        # 28.6 C from ~69 to ~75 mOhm, so the ORIGINAL voltage no longer sat a
        # hair past the 0%-anchor threshold. Same marginal-crossing spirit kept.
        v, i = 11.36, 5.01
        for _ in range(50):          # 50 x 0.1s = 5s, past _anchor_min_sustain_s
            e.update(v, i, dt=0.1, temp=28.6)
        self.assertLess(e.soc, 5.0,
                        "a genuinely sustained empty condition should still anchor")

    def test_single_slow_cadence_sample_does_not_fire(self):
        """Regression for test_QuickScan_20260712_150458.csv: Quick Scan/IEC/Cycle
        Life discharge loops poll every ~5s (see _seq_sleep(5.0) in quick_scan.py),
        so a single sample's own dt (~5s) already clears _anchor_min_sustain_s (3s)
        — the "hold continuously" gate was a no-op for any loop slower than the
        threshold. Real run: SoC hard-reset 24.25%->0.00% in one 5s sample
        (est.OCV 11.742V vs 11.628*1.01=11.744V threshold, a ~2mV margin) while the
        pack kept discharging another 4.6 min to the real voltage cutoff. Requiring
        >= _anchor_min_samples consecutive qualifying calls (not just accumulated
        dt) closes this regardless of loop cadence."""
        e = self._est()
        e.rin = 0.0804
        v, i = 11.34, 5.0
        anchor_v_empty = e.battery_model.get_ocv_from_soc(0.0)
        ocv_est = v + i * e.rin
        self.assertLessEqual(ocv_est, anchor_v_empty * 1.01)

        e.update(v, i, dt=5.03, temp=26.6)  # one ~5s-cadence sample, dt alone > 3s
        self.assertGreater(e.soc, 20.0,
                           "one slow-cadence sample must not hard-reset SoC to 0")

    def test_sustained_condition_still_fires_at_slow_cadence(self):
        """The extra consecutive-sample requirement must not block a genuinely
        sustained empty condition just because the loop is slow — two ~5s-cadence
        samples in a row (>= _anchor_min_samples, and >= _anchor_min_sustain_s of
        real elapsed time) should still anchor."""
        e = self._est()
        e.rin = 0.0804
        v, i = 11.34, 5.0
        for _ in range(2):
            e.update(v, i, dt=5.03, temp=26.6)
        self.assertLess(e.soc, 5.0,
                        "two consecutive slow-cadence samples should still anchor")

    def test_sustain_timer_resets_when_condition_drops(self):
        """A brief dip below threshold followed by recovery shouldn't accumulate
        toward firing — mirrors the Peukert sustain gate's reset-on-rest behavior."""
        e = self._est()
        e.rin = 0.06923
        # v retuned 11.39->11.36 after the LeadAcid Ea/R 4000->2200 K correction:
        # update() recomputes self.rin each call ((R0+R1)*temp_mult), and the
        # weaker (physically-correct) multiplier raised the recomputed rin at
        # 28.6 C from ~69 to ~75 mOhm, so the ORIGINAL voltage no longer sat a
        # hair past the 0%-anchor threshold. Same marginal-crossing spirit kept.
        v, i = 11.36, 5.01
        for _ in range(20):          # 2s of the marginal condition (not enough alone)
            e.update(v, i, dt=0.1, temp=28.6)
        self.assertGreater(e._zero_anchor_sustain_s, 0.0)
        e.update(12.6, 0.0, dt=1.0, temp=28.6)      # rest — condition drops
        self.assertEqual(e._zero_anchor_sustain_s, 0.0)


if __name__ == "__main__":
    unittest.main()
