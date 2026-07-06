"""Regression test for the discharge-phase ETA fix.

The old estimate (_dis_est = rated_capacity / i_dis * 3600) used the full
nameplate rated_capacity regardless of the pack's actual SoH or its SoC at
the start of the discharge phase — for a degraded battery, or one that
doesn't start a discharge test from 100%, this systematically overestimates
how long the test will actually take (the test finishes well before the
predicted ETA every time). _estimate_discharge_s() uses
effective_capacity() (rated * SoH/100 — the same denominator the
estimator's own coulomb counting divides by) scaled by the current SoC, so
the estimate reflects the Ah actually available to discharge from here.

Qt-coupled (BatteryQtWindow.SequencesMixin), so verified via a headless
smoke-test-style unittest rather than mocking the whole widget tree — this
mirrors how _project_tail_eta/_estimate_charge_s are tested elsewhere.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.ui.sequences import SequencesMixin


class _FakeController:
    def __init__(self, estimator):
        self.estimator = estimator


class _Stub:
    _estimate_discharge_s = SequencesMixin._estimate_discharge_s

    def __init__(self, estimator):
        self.controller = _FakeController(estimator)


class TestEstimateDischargeS(unittest.TestCase):
    def _estimator(self, rated=5.3):
        return StateEstimator(rated, BatteryModel("LeadAcid", 2.0, 6, 1))

    def test_healthy_full_soc_matches_nameplate_math(self):
        e = self._estimator(rated=5.3)
        e.set_soh(100.0)
        e.set_initial_soc(100.0)
        s = _Stub(e)
        est = s._estimate_discharge_s(1.0)
        self.assertAlmostEqual(est, int(5.3 / 1.0 * 3600), delta=5)

    def test_degraded_soh_shortens_estimate(self):
        e = self._estimator(rated=5.3)
        e.set_soh(100.0)
        e.set_initial_soc(100.0)
        full = _Stub(e)._estimate_discharge_s(1.0)

        e2 = self._estimator(rated=5.3)
        e2.set_soh(60.0)
        e2.set_initial_soc(100.0)
        degraded = _Stub(e2)._estimate_discharge_s(1.0)

        self.assertLess(degraded, full * 0.7)
        self.assertAlmostEqual(degraded, int(5.3 * 0.60 / 1.0 * 3600), delta=5)

    def test_lower_starting_soc_shortens_estimate(self):
        e = self._estimator(rated=5.3)
        e.set_soh(100.0)
        e.set_initial_soc(100.0)
        full = _Stub(e)._estimate_discharge_s(1.0)

        e2 = self._estimator(rated=5.3)
        e2.set_soh(100.0)
        e2.set_initial_soc(50.0)
        half = _Stub(e2)._estimate_discharge_s(1.0)

        self.assertAlmostEqual(half, full / 2, delta=60)

    def test_never_below_floor(self):
        e = self._estimator(rated=5.3)
        e.set_soh(0.0)
        e.set_initial_soc(0.0)
        est = _Stub(e)._estimate_discharge_s(1.0)
        self.assertGreaterEqual(est, 60)

    def test_exception_safe_returns_zero(self):
        class _Broken:
            controller = None
        est = SequencesMixin._estimate_discharge_s(_Broken(), 1.0)
        self.assertEqual(est, 0)


if __name__ == "__main__":
    unittest.main()
