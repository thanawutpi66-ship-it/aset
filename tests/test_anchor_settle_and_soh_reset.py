"""Regression tests for two related real-hardware findings:

1. Post-anchor settle window: right after a 100%/0% endpoint anchor fires
   mid charge/discharge, terminal voltage is still surface-charge-inflated
   (charge) or freshly polarised (discharge) — trusting it immediately in
   the EKF's measurement update can pull SoC away from a genuinely-correct
   anchor before the transient dissipates, especially if the chemistry
   profile's OCV table doesn't exactly match this specific pack's true
   rested voltage. _reset_to_soc(start_settle_window=True) now starts a
   window (reusing _min_rest_s) during which the EKF measurement update's R
   is inflated so it barely moves SoC.

2. StateEstimator.soh (and full-sweep SoH tracking state) survived across a
   product change with nothing resetting it. A brand-new battery swapped in
   after testing a degraded one inherited the old SoH, which shrinks
   effective_capacity()'s denominator — coulomb counting then raced to
   100% SoC during CC/bulk charge, well before voltage even reached the
   absorption/CV ceiling. reset_battery_state() clears it; isa101_views.py's
   _on_product_changed() now calls it alongside the existing rated_capacity
   sync.
"""
import time
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


class TestAnchorSettleWindow(unittest.TestCase):
    def _est(self):
        e = StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.set_initial_soc(50.0)
        e.update(12.0, 0.0, dt=1.0, temp=25.0)   # lazily create the EKF
        return e

    def test_reset_to_soc_starts_settle_window(self):
        e = self._est()
        self.assertEqual(e._anchor_settle_until, 0.0)
        e._reset_to_soc(100.0, start_settle_window=True)
        self.assertGreater(e._anchor_settle_until, time.monotonic())

    def test_sync_with_ocv_does_not_start_settle_window(self):
        """Fresh-calibration entry points already require the caller to have waited
        out a real rest first — no settle window needed there."""
        e = self._est()
        e.sync_with_ocv(12.6, temp=25.0)
        self.assertEqual(e._anchor_settle_until, 0.0)

    def test_voltage_undershoot_during_settle_window_barely_moves_soc(self):
        """Simulate the exact failure mode: 100% anchor fires, then terminal voltage
        reads well BELOW the model's 100% OCV reference for several samples (as if
        surface charge were dissipating down past a slightly-mismatched reference) —
        SoC should stay close to 100 while still inside the settle window."""
        e = self._est()
        e.update(14.0, -0.1, dt=1.0, temp=25.0)      # near-zero current, near CV
        e._reset_to_soc(100.0, start_settle_window=True)
        low_v = e.battery_model.get_ocv_from_soc(100.0, 25.0) - 0.3   # well under the ref
        for _ in range(20):
            e.update(low_v, 0.0, dt=1.0, temp=25.0)
        self.assertGreater(e.soc, 97.0,
                           "settle window should have suppressed most of the pull")

    def test_same_undershoot_after_window_expires_pulls_soc_down(self):
        """Once the settle window has elapsed, a persistently-low voltage SHOULD be
        trusted again — confirms the window is temporary, not a permanent override.
        Uses a long simulated rest (dt=10s steps) so the EKF's covariance actually
        grows enough via process noise to respond — a real rest phase runs for
        minutes, not the handful of 1 s samples the other test uses."""
        e = self._est()
        e.update(14.0, -0.1, dt=1.0, temp=25.0)
        e._reset_to_soc(100.0, start_settle_window=True)
        e._anchor_settle_until = time.monotonic() - 1.0   # force-expire it
        low_v = e.battery_model.get_ocv_from_soc(100.0, 25.0) - 0.3
        for _ in range(200):
            e.update(low_v, 0.0, dt=10.0, temp=25.0)
        self.assertLess(e.soc, 96.0,
                        "after the window expires, a real voltage mismatch should pull SoC")


class TestResetBatteryState(unittest.TestCase):
    def test_clears_soh_and_sweep_tracking(self):
        e = StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.set_soh(62.0)
        e._cap_counting = True
        e._cap_counter_ah = 3.5
        e.measured_capacity_ah = 4.3
        e.reset_battery_state()
        self.assertEqual(e.soh, 100.0)
        self.assertFalse(e._cap_counting)
        self.assertEqual(e._cap_counter_ah, 0.0)
        self.assertEqual(e.measured_capacity_ah, 0.0)

    def test_stale_soh_would_have_raced_soc_during_charge(self):
        """Demonstrates the actual bug: a stale low SoH shrinks effective_capacity(),
        so the SAME real Ah delivered reads as a much bigger SoC jump. Confirms
        reset_battery_state() removes that distortion."""
        e = StateEstimator(5.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.set_initial_soc(87.0)
        e.set_soh(40.0)                          # stale, from a previously-tested pack
        cap_stale = e.effective_capacity()
        e.reset_battery_state()
        cap_fresh = e.effective_capacity()
        self.assertLess(cap_stale, cap_fresh)
        self.assertAlmostEqual(cap_fresh, 5.0, places=3)


if __name__ == "__main__":
    unittest.main()
