"""Regression tests for the Rin "estimated vs measured" distinction.

Before any real HPPC pulse has been fitted, StateEstimator.rin is
_ekf_rc_defaults()'s uncalibrated placeholder guess (base_rin with a
deliberate safety margin, R0+R1 summed) — plausible enough to keep the EKF
sane, but not a measurement. Two separate real-hardware sessions in this
project independently mistook this placeholder for a bench ACIR/DCIR
reading (off by 2-4x depending on the pack), so the estimator now exposes
whether Rin has actually been calibrated by a real fit, and _log_sample()
uses it to log NaN instead of the placeholder — the cloud dashboard/CSV
then show "no reading yet" instead of a confident wrong number.
"""
import math
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.app.auto_controller import AutoController


class TestRinCalibratedFlag(unittest.TestCase):
    def _est(self):
        return StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))

    def test_starts_uncalibrated(self):
        e = self._est()
        self.assertFalse(e._ecm_calibrated)

    def test_update_returns_uncalibrated_before_any_fit(self):
        e = self._est()
        state = e.update(12.6, 0.0, dt=1.0, temp=25.0)
        self.assertFalse(state["rin_calibrated"])

    def test_update_ecm_marks_calibrated(self):
        e = self._est()
        e.update(12.6, 0.0, dt=1.0, temp=25.0)   # lazily creates the EKF
        e.update_ecm(0.044, 0.02, 1000.0)         # a real HPPC fit lands
        self.assertTrue(e._ecm_calibrated)
        state = e.update(12.6, 0.0, dt=1.0, temp=25.0)
        self.assertTrue(state["rin_calibrated"])

    def test_ecm_table_marks_calibrated(self):
        e = self._est()
        e.update(12.6, 0.0, dt=1.0, temp=25.0)
        e.set_ecm_table({10.0: {"r0": 0.05, "r1": 0.03, "c1": 500.0},
                         90.0: {"r0": 0.04, "r1": 0.02, "c1": 500.0}})
        self.assertTrue(e._ecm_calibrated)


class _FakeHW:
    is_connected = True
    current_temp = 25.0


class _FakeDataHandler:
    def __init__(self):
        self.rows = []          # each entry: (elapsed, v, i, soc, rin_mohm, temp)
        self.is_recording = True

    def log_row(self, elapsed_s, v, i_net, soc, resistance_mohm, temp_c):
        self.rows.append(resistance_mohm)


class _FakeConfig:
    pass


class TestLogSampleUsesCalibratedFlag(unittest.TestCase):
    def _controller(self):
        estimator = StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        data = _FakeDataHandler()
        c = AutoController(root=None, hw=_FakeHW(), data=data,
                           estimator=estimator, config=_FakeConfig())
        c._start_time = 0.0
        return c, estimator, data

    def test_logs_nan_before_calibration(self):
        c, estimator, data = self._controller()
        estimator.update(12.6, 0.0, dt=1.0, temp=25.0)   # creates the EKF, still default
        c._log_sample(12.6, 0.0)
        self.assertEqual(len(data.rows), 1)
        self.assertTrue(math.isnan(data.rows[0]),
                        "Rin should log as NaN before any real HPPC fit lands")

    def test_logs_real_value_after_calibration(self):
        c, estimator, data = self._controller()
        estimator.update(12.6, 0.0, dt=1.0, temp=25.0)
        estimator.update_ecm(0.044, 0.02, 1000.0)         # real fit: R0=44 mOhm
        c._log_sample(12.6, 0.0)
        self.assertEqual(len(data.rows), 1)
        self.assertFalse(math.isnan(data.rows[0]))
        self.assertGreater(data.rows[0], 0.0)


if __name__ == "__main__":
    unittest.main()
