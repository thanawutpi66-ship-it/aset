"""Regression test: the EKF's voltage measurement update must not run SoC away
during active charge/discharge before a real R0 has been calibrated.

Root cause (found analysing a real HPPC test's own CHARGE phase): before the
first HPPC pulse ever runs, StateEstimator.update()'s EKF path uses
_ekf_rc_defaults()'s generic R0 guess (base_rin * _EKF_UNCALIBRATED_R0_MARGIN) for
the voltage measurement's IR-drop term (cur*r0_use). That guess can be off by a
large fraction of itself, and at any real charge/discharge current the resulting
model error in the *predicted* voltage is comparable to or larger than the EKF's
~7 mV measurement-noise floor (self.R) — so the filter read "R0 is a rough guess"
as if it were a genuine, fast SoC swing.

Confirmed on a real CSV: SoC read 80.94% -> 99.90% within 139 seconds of a
constant -0.533 A (0.1C) charge current. Reaching that via real coulomb counting
would require ~26.5 A average -- a ~50x discrepancy from the actual logged
current. Fix: while uncalibrated (not self._ecm_calibrated) AND away from rest
(same near-rest gate the non-EKF OCV-correction fallback already uses), skip the
EKF's voltage measurement update entirely and rely on coulomb counting alone --
near true rest the IR-drop term is negligible regardless of how wrong R0 is, so
the correction is still safe to apply there.
"""
import unittest

from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.battery_model import BatteryModel


def _lead_acid_estimator(soc0=80.94):
    model = BatteryModel("LeadAcid", 2.0, 6, 1)
    est = StateEstimator(rated_capacity=5.3, battery_model=model)
    est.soc = soc0
    est.soc_initial = soc0
    return est


class TestUncalibratedR0DoesNotRunawaySoc(unittest.TestCase):
    def test_soc_tracks_coulomb_counting_during_uncalibrated_charge(self):
        """~140s of a real 0.1C charge current must move SoC by roughly the
        coulomb-counted amount (a fraction of a percent), not run it to 100%."""
        est = _lead_acid_estimator(soc0=80.94)
        current = -0.533   # A, discharge-positive convention -> charging
        dt = 0.1
        v = 12.66
        for _ in range(1394):    # 139.4 s at 0.1 s steps
            v += 0.00005         # gentle real voltage rise, same order as the real CSV
            est.update(v, current, dt=dt, temp=25.0)

        # Real coulomb counting for this current/duration is ~0.023 %; allow generous
        # headroom for efficiency/Peukert terms but reject anything near the old bug's
        # ~19-point runaway.
        self.assertLess(est.soc, 82.0)
        self.assertGreater(est.soc, 80.5)

    def test_ekf_update_is_skipped_while_uncalibrated_and_active(self):
        est = _lead_acid_estimator()
        est.update(12.7, -0.533, dt=0.1, temp=25.0)   # lazily creates the EKF
        self.assertFalse(est._ecm_calibrated)
        called = {"n": 0}
        real_update = est._ekf.update
        def spy(*a, **k):
            called["n"] += 1
            return real_update(*a, **k)
        est._ekf.update = spy
        est.update(12.71, -0.533, dt=0.1, temp=25.0)   # active current, uncalibrated
        self.assertEqual(called["n"], 0)

    def test_ekf_update_still_runs_near_rest_while_uncalibrated(self):
        """The gate only guards against active current -- near true rest the
        IR-drop model error is negligible regardless of R0 accuracy, matching the
        non-EKF OCV-correction fallback's own near-rest threshold. The rest must
        also be LONGER than _min_rest_s: a later fix added the same polarization
        gate the fallback path always had (fresh post-pulse relaxation reads as a
        fake positive innovation with the default τ), so a just-started rest is
        deliberately not trusted yet — simulate a long-settled rest here."""
        est = _lead_acid_estimator()
        est.update(12.7, 0.0, dt=0.1, temp=25.0)       # lazily creates the EKF
        self.assertFalse(est._ecm_calibrated)
        est._rested_s = est._min_rest_s + 1.0          # long-rested, polarization gone
        called = {"n": 0}
        real_update = est._ekf.update
        def spy(*a, **k):
            called["n"] += 1
            return real_update(*a, **k)
        est._ekf.update = spy
        est.update(12.7, 0.0, dt=0.1, temp=25.0)       # near-rest current
        self.assertEqual(called["n"], 1)

    def test_ekf_update_runs_normally_once_calibrated(self):
        """Once a real HPPC/ECM fit lands, the gate must not suppress the
        measurement update even at active current -- it only guards the
        uncalibrated-guess window.

        The active-current probe uses a DISCHARGE current: CHARGE samples are
        now (deliberately, separately) gated regardless of calibration —
        charging terminal voltage carries no SoC information (see
        test_charge_voltage_gate.py) — so a charge sample would test the
        wrong gate. The probe voltage keeps the implied OCV (V + I*rin)
        inside the curve so the loaded surface-charge gate stays open too."""
        est = _lead_acid_estimator()
        est.update(12.7, -0.533, dt=0.1, temp=25.0)    # lazily creates the EKF
        est.update_ecm(0.025, 0.068, 2000.0)           # a real ECM fit lands
        self.assertTrue(est._ecm_calibrated)
        called = {"n": 0}
        real_update = est._ekf.update
        def spy(*a, **k):
            called["n"] += 1
            return real_update(*a, **k)
        est._ekf.update = spy
        est.update(12.40, 0.533, dt=0.1, temp=25.0)    # active DISCHARGE, calibrated
        self.assertEqual(called["n"], 1)


if __name__ == "__main__":
    unittest.main()
