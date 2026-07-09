"""Regression tests for StateEstimator._detect_step_r0 — the universal
single-step R0 detector.

Context: only aset_batt/acquisition/worker.py's AcquisitionWorker ("RUN TEST")
ever called update_ecm()/set_ecm_table() before this — none of the AUTO
SEQUENCE threads (manual charge, IEC, Quick Scan, Cycle Life, and HPPC's own
CHARGE/REST legs) ever fed a real R0 into the live estimator, so
_ecm_calibrated stayed False and self.rin stayed at the generic uncalibrated
guess for the entire test in every one of those modes (confirmed on real CSVs:
Rin_Calibrated was 0 for 100% of both a full HPPC test and a full IEC test).

_detect_step_r0() runs inside update() itself (the one function every mode
routes through), so it improves R0 accuracy everywhere without per-mode
wiring. It only ever supplies R0 (a single step can't resolve R1/C1), so it
sets a separate, easier-to-reach _r0_calibrated flag rather than the stricter
_ecm_calibrated the UI's "measured vs estimated" Rin label keys off.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


def _est(chemistry="LeadAcid", rated=5.3):
    model = BatteryModel(chemistry, 2.0, 6, 1)
    return StateEstimator(rated_capacity=rated, battery_model=model)


class TestCleanStepIsDetected(unittest.TestCase):
    def test_r0_calibrated_flips_true_on_a_clean_current_step(self):
        e = _est()
        self.assertFalse(e._r0_calibrated)
        v_rest = 13.15
        # Fill the rolling buffer with a tight, genuine rest plateau.
        for _ in range(3):
            e.update(v_rest, 0.0, dt=0.1, temp=25.0)
        # A clean step: current jumps 0 -> -0.533A (charge), voltage drops
        # accordingly, arriving well within the staleness gate (dt=0.1s).
        r0_true = 0.03
        v_step = v_rest - abs(-0.533) * r0_true
        e.update(v_step, -0.533, dt=0.1, temp=25.0)
        self.assertTrue(e._r0_calibrated)
        self.assertAlmostEqual(e._ekf.R0, r0_true, delta=0.005)

    def test_ecm_calibrated_is_not_flipped_by_r0_only(self):
        """R0-only must NOT claim the UI's stricter "fully measured" label --
        R1/C1 are still generic guesses after a single-step detection."""
        e = _est()
        for _ in range(3):
            e.update(13.15, 0.0, dt=0.1, temp=25.0)
        e.update(13.10, -0.533, dt=0.1, temp=25.0)
        self.assertTrue(e._r0_calibrated)
        self.assertFalse(e._ecm_calibrated)


class TestNoiseIsNotMistakenForAStep(unittest.TestCase):
    def test_small_current_jitter_does_not_trigger(self):
        e = _est()
        for _ in range(3):
            e.update(13.15, 0.0, dt=0.1, temp=25.0)
        # A jitter well under _STEP_MIN_DI_A (0.15A).
        e.update(13.14, 0.05, dt=0.1, temp=25.0)
        self.assertFalse(e._r0_calibrated)

    def test_stale_post_edge_sample_does_not_trigger(self):
        """A real edge whose post-edge sample arrives too late (dt beyond the
        staleness gate) has already relaxed into the RC region -- R would
        carry R1, not just R0. Must be rejected, same as the batch
        identify_dcir()'s own dt-gate."""
        e = _est()
        for _ in range(3):
            e.update(13.15, 0.0, dt=0.1, temp=25.0)
        e.update(13.10, -0.533, dt=1.0, temp=25.0)   # beyond _STEP_MAX_DT_S
        self.assertFalse(e._r0_calibrated)

    def test_already_mid_transition_reference_does_not_trigger(self):
        """If the buffer itself already shows a wide voltage spread (not a
        genuine settled plateau), don't treat it as a trustworthy 'before'
        reference."""
        e = _est()
        # A buffer that itself is drifting, not resting.
        for v in (13.0, 12.8, 12.6):
            e.update(v, -0.533, dt=0.1, temp=25.0)
        e.update(12.4, -0.6, dt=0.1, temp=25.0)
        self.assertFalse(e._r0_calibrated)


class TestGatesTheEkfRunawayGuard(unittest.TestCase):
    def test_r0_calibrated_alone_is_enough_to_stop_the_runaway(self):
        """The original bug (SoC 80%->100% within ~2 minutes of a real 0.1C
        charge) was fixed by skipping the EKF's voltage update while
        uncalibrated and active. Confirm a real single-step R0 detection
        (without any full ECM fit / update_ecm() call) is now enough on its
        own to let that voltage update resume safely."""
        e = _est()
        e.soc = 80.94
        e.soc_initial = 80.94
        for _ in range(3):
            e.update(12.66, 0.0, dt=0.1, temp=25.0)
        e.update(12.66 - 0.533 * 0.03, -0.533, dt=0.1, temp=25.0)
        self.assertTrue(e._r0_calibrated)

        # Continue the same charge for a while -- SoC should track coulomb
        # counting (a small fraction of a percent), not run away toward 100%,
        # now that R0 is confirmed real rather than a generic guess.
        v = 12.66 - 0.533 * 0.03
        for _ in range(200):
            v += 0.0001
            e.update(v, -0.533, dt=0.1, temp=25.0)
        self.assertLess(e.soc, 82.5)


if __name__ == "__main__":
    unittest.main()
