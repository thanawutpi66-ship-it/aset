"""Regression tests for the Rin "estimated vs measured" distinction.

Before any real HPPC pulse has been fitted, StateEstimator.rin is
_ekf_rc_defaults()'s uncalibrated placeholder guess (base_rin with a
deliberate safety margin, R0+R1 summed) — plausible enough to keep the EKF
sane, but not a measurement. Two separate real-hardware sessions in this
project independently mistook this placeholder for a bench ACIR/DCIR
reading (off by 2-4x depending on the pack).

The fix keeps showing Rin live every sample (operators want a continuous
real-time trend, not a gap) but rides a "calibrated" flag alongside it, so
the CSV/cloud dashboard can label it "(est.)" until a real fit lands instead
of presenting a placeholder as a reading.
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
        # Rin itself is still a live, sane, positive number — just not a measurement.
        self.assertGreater(state["rin"], 0.0)

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
        self.rows = []          # each entry: (resistance_mohm, rin_calibrated)
        self.is_recording = True

    def log_row(self, elapsed_s, v, i_net, soc, resistance_mohm, temp_c,
                rin_calibrated=True):
        self.rows.append((resistance_mohm, rin_calibrated))


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

    def test_logs_live_value_but_flags_uncalibrated(self):
        c, estimator, data = self._controller()
        estimator.update(12.6, 0.0, dt=1.0, temp=25.0)   # creates the EKF, still default
        c._log_sample(12.6, 0.0)
        self.assertEqual(len(data.rows), 1)
        rin_mohm, calibrated = data.rows[0]
        self.assertFalse(math.isnan(rin_mohm), "Rin should still log live, not a gap")
        self.assertGreater(rin_mohm, 0.0)
        self.assertFalse(calibrated,
                         "flag should mark this as the uncalibrated placeholder")

    def test_logs_calibrated_after_real_fit(self):
        c, estimator, data = self._controller()
        estimator.update(12.6, 0.0, dt=1.0, temp=25.0)
        estimator.update_ecm(0.044, 0.02, 1000.0)         # real fit: R0=44 mOhm
        c._log_sample(12.6, 0.0)
        self.assertEqual(len(data.rows), 1)
        rin_mohm, calibrated = data.rows[0]
        self.assertFalse(math.isnan(rin_mohm))
        self.assertGreater(rin_mohm, 0.0)
        self.assertTrue(calibrated)


class TestComputeSummaryRinCalibrated(unittest.TestCase):
    """_compute_summary() (data_utils.py) is what the cloud dashboard payload's
    summary.latest is built from — verify it surfaces Rin_Calibrated correctly,
    including the backward-compat default for CSVs logged before this column existed."""

    def test_reads_calibrated_column(self):
        from aset_batt.storage.data_utils import _compute_summary
        rows = [{"Voltage_V": "12.6", "Current_A": "0.0", "SoC_pct": "75.0",
                 "Resistance_mOhm": "44.0", "Temperature_C": "25.0",
                 "Rin_Calibrated": "1"}]
        summary = _compute_summary(rows)
        self.assertTrue(summary["latest"]["Rin_Calibrated"])

    def test_reads_uncalibrated_column(self):
        from aset_batt.storage.data_utils import _compute_summary
        rows = [{"Voltage_V": "12.6", "Current_A": "0.0", "SoC_pct": "75.0",
                 "Resistance_mOhm": "70.8", "Temperature_C": "25.0",
                 "Rin_Calibrated": "0"}]
        summary = _compute_summary(rows)
        self.assertFalse(summary["latest"]["Rin_Calibrated"])

    def test_missing_column_defaults_calibrated(self):
        """Old CSVs logged before this field existed always logged a real per-sample
        value — treat them as calibrated rather than unknown/false."""
        from aset_batt.storage.data_utils import _compute_summary
        rows = [{"Voltage_V": "12.6", "Current_A": "0.0", "SoC_pct": "75.0",
                 "Resistance_mOhm": "44.0", "Temperature_C": "25.0"}]
        summary = _compute_summary(rows)
        self.assertTrue(summary["latest"]["Rin_Calibrated"])


if __name__ == "__main__":
    unittest.main()
