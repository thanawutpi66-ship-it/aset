"""Regression tests: the EKF must not trust terminal voltage while it carries
no SoC information — (a) during CHARGING, (b) while the IMPLIED OCV under
discharge load is still above the OCV curve's own 100% point.

Real-session evidence (all three 2026-07-08/09 files, same YTZ6V pack):
  * test_HPPC_20260708_152502: SoC hit 100% after 142 s of a 242-min charge —
    1.849 of 1.869 Ah (99%) went in AFTER the display already read full.
  * test_20260709_154818: SoC hit 100% after 28 s of a 102-min charge.
  * Root cause: bulk/absorption terminal voltage (13-14.4 V) sits above the
    OCV curve top plus gassing/CV overpotential the 1-RC model doesn't
    represent -> systematically positive innovation -> SoC races to 100%.
    The near-rest surface-charge/polarization gates can't catch it (they only
    apply at ~zero current).
  * After gating charge samples alone, the same replay exposed the loaded
    counterpart: the first ~8 min of DISCHARGE right after a charge still
    read V + I*R above the ceiling, and the (covariance-inflated) EKF pinned
    SoC at 100% for ~0.37 Ah -> the surface-charge gate now evaluates the
    implied OCV (voltage + discharge_current * rin), not just rest voltage.

Replay results with both gates (2026-07-10): RUN_0709 charge reaches 100% at
24.2 min / 0.82 Ah in (physically consistent with the eta model), discharge
leaves 100% in 5.2 s, ends 0% at the true 10.5 V cutoff; HPPC_0708 ends at
95.73% (theory: 100 - 4.27% unrecharged pulses); IEC_0708 unchanged.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


def _est(soc0=80.0):
    model = BatteryModel("LeadAcid", 5.3, 6, 1)
    est = StateEstimator(5.3, model)
    est._reset_to_soc(soc0)
    # R0 confirmed so the uncalibrated-R0 guard isn't what blocks updates —
    # these tests isolate the NEW gates.
    est._r0_calibrated = True
    return est


class TestChargingVoltageGate(unittest.TestCase):
    def test_soc_tracks_coulomb_not_voltage_during_charge(self):
        """60 s of absorption-voltage charging must move SoC by only the
        coulomb+eta increment (~0.7%), not race toward 100%."""
        est = _est(80.0)
        for _ in range(60):
            st = est.update(14.20, -2.65, dt=1.0, temp=25.0)
        # coulomb: 2.65 A * 60 s = 0.0442 Ah = 0.83% raw, x eta(0.92) ~ 0.77%
        self.assertLess(st["soc"], 82.5,
                        "charging voltage must not drag SoC upward")
        self.assertGreater(st["soc"], 80.3, "coulomb counting must still run")

    def test_old_behavior_would_have_raced_to_100(self):
        """Sanity check that the gate is what prevents the race: with the
        charging gate bypassed (simulated by feeding the same voltage at
        near-zero current with rest gates disarmed), the EKF drags SoC up —
        proving the charge samples genuinely carried the runaway pressure."""
        est = _est(80.0)
        est._rested_s = est._min_rest_s + 1.0
        # In-range rest voltage far ABOVE the 80% OCV -> update fires and pulls up.
        for _ in range(30):
            st = est.update(12.85, 0.0, dt=1.0, temp=25.0)
            est._rested_s = est._min_rest_s + 1.0   # keep the polarization gate open
        self.assertGreater(st["soc"], 85.0,
                           "control case: voltage updates must still be able to move SoC")

    def test_voltage_correction_resumes_at_rest_after_charge(self):
        est = _est(80.0)
        # charge phase (gated)
        for _ in range(30):
            est.update(14.20, -2.65, dt=1.0, temp=25.0)
        soc_after_charge = est.soc
        # rested, in-range voltage well above OCV(soc) -> correction fires again
        est._rested_s = est._min_rest_s + 1.0
        for _ in range(30):
            st = est.update(12.82, 0.0, dt=1.0, temp=25.0)
            est._rested_s = est._min_rest_s + 1.0
        self.assertGreater(st["soc"], soc_after_charge + 1.0,
                           "EKF voltage correction must resume once rested")


class TestLoadedSurfaceChargeGate(unittest.TestCase):
    def test_above_ceiling_implied_ocv_cannot_pull_soc_up_under_load(self):
        """Discharging at a terminal voltage whose implied OCV (V + I*rin) is
        above the curve's 100% point: SoC must fall by coulomb, never rise."""
        est = _est(97.0)
        socs = [est.soc]
        for _ in range(120):
            st = est.update(12.87, 2.65, dt=1.0, temp=25.0)   # 12.87 + I*rin > 12.888
            socs.append(st["soc"])
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(socs, socs[1:])),
                        "SoC must be monotonically non-increasing under load "
                        "while the implied OCV is surface-charged")
        # coulomb: 2.65*120/3600 = 0.0883 Ah = 1.67% -> ends near 95.3
        self.assertLess(socs[-1], 96.2)

    def test_in_range_discharge_voltage_still_corrects(self):
        """Do not over-gate: a discharge sample whose implied OCV is INSIDE the
        curve must still feed the EKF (voltage far below the current SoC's OCV
        has to pull SoC down faster than coulomb alone)."""
        est_gated_only = _est(90.0)
        est_gated_only.use_ocv = False          # coulomb-only reference
        est_full = _est(90.0)
        for _ in range(60):
            # 12.00 V at 2.65 A -> implied OCV ~12.2 V, inside the curve,
            # and far below OCV(90%) -> big downward innovation
            ref = est_gated_only.update(12.00, 2.65, dt=1.0, temp=25.0)
            st = est_full.update(12.00, 2.65, dt=1.0, temp=25.0)
        self.assertLess(st["soc"], ref["soc"] - 2.0,
                        "in-range loaded voltage must still correct SoC downward")


class TestSurfaceChargeLatchHysteresis(unittest.TestCase):
    """F3: the surface-charge gate must (a) build the implied OCV with the EKF's
    OWN R basis (R0+R1), not the blended self.rin, and (b) latch — hold the gate
    closed after tripping instead of releasing on the first sample the implied OCV
    grazes back into range under a still-sagging discharge load. A real HPPC replay
    showed the bare per-sample gate releasing on exactly that one grazing frame and
    letting a single big-innovation EKF update pin SoC at 100%."""

    def _boundary_v(self, est, cur):
        """Terminal voltage at which the implied OCV (v + cur*(R0+R1)) sits exactly
        on the curve's 100% point — feed above this to trip, below to clear."""
        ekf = est._ensure_ekf()
        top = est.battery_model.get_ocv_from_soc(100.0, 25.0)
        return top - cur * (ekf.R0 + ekf.R1)

    def test_latch_trips_then_holds_through_single_in_range_graze(self):
        est = _est(97.0)
        cur = 2.65
        b = self._boundary_v(est, cur)
        for _ in range(8):                       # sustained above-range implied OCV
            est.update(b + 0.100, cur, dt=1.0, temp=25.0)
        self.assertTrue(est._surface_charge_latched,
                        "sustained above-range implied OCV must trip the latch")
        soc_before = est.soc
        # one sample whose implied OCV dips 50 mV inside range — the 15 s hold must
        # keep the gate closed, so SoC cannot be pulled up by that single frame
        est.update(b - 0.050, cur, dt=1.0, temp=25.0)
        self.assertTrue(est._surface_charge_latched,
                        "one in-range graze must NOT release the surface-charge latch")
        self.assertLessEqual(est.soc, soc_before + 1e-9,
                             "SoC must not jump upward on the grazing frame")

    def test_latch_releases_after_sustained_in_range(self):
        est = _est(90.0)
        cur = 2.65
        b = self._boundary_v(est, cur)
        for _ in range(5):
            est.update(b + 0.100, cur, dt=1.0, temp=25.0)
        self.assertTrue(est._surface_charge_latched)
        # implied OCV held in range for longer than the clear-hold window -> release
        for _ in range(int(est._SURFACE_CHARGE_CLEAR_HOLD_S) + 3):
            est.update(b - 0.050, cur, dt=1.0, temp=25.0)
        self.assertFalse(est._surface_charge_latched,
                         "a sustained in-range implied OCV must release the latch")

    def test_endpoint_anchor_clears_a_pending_latch(self):
        est = _est(97.0)
        cur = 2.65
        b = self._boundary_v(est, cur)
        for _ in range(6):
            est.update(b + 0.100, cur, dt=1.0, temp=25.0)
        self.assertTrue(est._surface_charge_latched)
        est._reset_to_soc(100.0, start_settle_window=True)   # an explicit re-anchor
        self.assertFalse(est._surface_charge_latched,
                         "re-anchoring to a known SoC must clear the surface-charge latch")


class TestRinPlaceholderContinuity(unittest.TestCase):
    """A real IEC session CSV (test_IEC_20260708_203952) showed the displayed
    Resistance_mOhm sitting at the generic chemistry base_rin (30.0) through
    the whole pre-test rest, then jumping to the EKF-default basis (~64) at
    the first update() — same battery, same session, two different
    uncalibrated-placeholder regimes purely from init order. The initial
    value now uses the same (R0+R1) basis the EKF branch reports."""

    def test_initial_rin_matches_ekf_default_basis(self):
        model = BatteryModel("LeadAcid", 5.3, 6, 1)
        est = StateEstimator(5.3, model)
        r0_d, r1_d, _ = est._ekf_rc_defaults()
        self.assertAlmostEqual(est.rin, r0_d + r1_d, places=9)

    def test_first_rest_update_does_not_step_the_displayed_rin(self):
        model = BatteryModel("LeadAcid", 5.3, 6, 1)
        est = StateEstimator(5.3, model)
        rin_before = est.rin
        st = est.update(12.70, 0.0, dt=1.0, temp=25.0)   # first sample, at rest, 25 °C
        # same basis -> only temp/SoC-shape multipliers apply (≈1.0 at 25 °C);
        # the old init produced a ~2.1x step here.
        self.assertLess(abs(st["rin"] - rin_before) / rin_before, 0.30)


class TestHppcSurfaceChargeAdvisory(unittest.TestCase):
    def test_post_charge_rest_actively_bleeds_not_just_warns(self):
        """hppc.py's PHASE 2 (post-charge rest, before HPPC pulses) must call
        calibrate_from_ocv_stable() — which checks ocv_out_of_range_mv
        internally and, for a surface-charged lead-acid pack, actually runs a
        C/20 bleed-off and re-settles — instead of a fixed timer + a single
        immediate calibrate_from_ocv() read with only a passive warning.

        A real run (test_HPPC_20260708_152502) started its pulses 430 mV
        surface-charged: the old code's advisory fired but the sequence
        pulsed anyway, and the per-pulse R0 anchor drifted 37% across 5
        cycles purely from that unresolved surface charge, not the battery.
        PREPARE's own bleed-off (PHASE 0) had already stripped this once, but
        the CHARGE phase in between re-creates it and nothing repeated the
        bleed — see calibrate_from_ocv_stable's own bleed-off branch in
        auto_controller.py for the correction this now reuses."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "aset_batt" / "ui"
               / "sequences" / "hppc.py").read_text(encoding="utf-8")
        phase2 = src.index("PHASE 2")
        phase3 = src.index("PHASE 3", phase2)
        window = src[phase2:phase3]
        self.assertIn("calibrate_from_ocv_stable", window,
                      "PHASE 2 must settle+bleed via calibrate_from_ocv_stable, "
                      "not a fixed timer + one-shot calibrate_from_ocv")
        self.assertNotIn("_rest_total = 30 * 60", window,
                         "the old fixed 30-min timer should be gone")


if __name__ == "__main__":
    unittest.main()
