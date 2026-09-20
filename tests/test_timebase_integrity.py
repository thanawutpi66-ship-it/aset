"""Regression coverage for elapsed-time and wall-clock separation."""
import csv
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from aset_batt.acquisition.analysis import (
    _integration_gap_metrics,
    _quick_step_on_latency,
    identify_dcir,
)
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler


class TimebaseIntegrityTests(unittest.TestCase):
    def test_estimator_rejects_invalid_and_long_intervals_without_integrating(self):
        estimator = StateEstimator(10.0)
        estimator.use_ekf = False
        initial_ah = estimator.ah_accumulated
        for dt in (0.0, -0.1, float("nan"), float("inf")):
            estimator.update(12.5, 2.0, dt, temp=25.0)
            self.assertEqual(estimator.last_timing_status, "INVALID_DT")
            self.assertEqual(estimator.ah_accumulated, initial_ah)
        estimator.update(12.5, 2.0, 30.001, temp=25.0)
        self.assertEqual(estimator.last_timing_status, "DATA_GAP")
        self.assertEqual(estimator.ah_accumulated, initial_ah)

    @staticmethod
    def _profile():
        return BatteryProfile(
            name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
            capacity_ah=10.0, max_charge_v=14.4, cutoff_v=10.5,
            max_charge_a=10.0, max_discharge_a=10.0, ovp=15.0, uvp=9.0,
            otp_warn=50.0, otp_crit=60.0, internal_r=0.03,
        )

    def test_dcir_accepts_limit_and_rejects_duplicate_backward_or_late_edges(self):
        profile = self._profile()
        for edge_dt, accepted in ((0.5, True), (0.0, False), (-0.1, False), (0.5001, False)):
            t = np.array([0.0, 1.0, 1.0 + edge_dt, 2.0 + edge_dt])
            current = np.array([0.0, 0.0, 1.0, 1.0])
            voltage = np.array([12.6, 12.6, 12.57, 12.57])
            result = identify_dcir(current, voltage, np.full(4, 25.0), profile, time_s=t)
            latency = _quick_step_on_latency(
                current, ["OCV_SETTLE", "OCV_SETTLE", "MINI_PULSE", "MINI_PULSE"], t
            )
            self.assertEqual(result[3], accepted, f"edge dt {edge_dt}")
            self.assertEqual(np.isfinite(latency), accepted, f"latency dt {edge_dt}")
            if accepted:
                self.assertAlmostEqual(latency, edge_dt)

    def test_gap_ceiling_and_invalid_elapsed_edges(self):
        for dt in (0.2, 0.5, 1.0, 5.0, 30.0):
            metrics = _integration_gap_metrics([0.0, dt], ["VALID", "VALID"])
            self.assertFalse(metrics["excluded_interval_mask"][0], f"{dt}s")
        metrics = _integration_gap_metrics([0.0, 30.001, 31.001, 60.001, 90.001],
                                           ["VALID"] * 5)
        self.assertTrue(metrics["excluded_interval_mask"][0])
        for times in ([1.0, 1.0, 2.0], [1.0, 0.9, 2.0]):
            metrics = _integration_gap_metrics(times, ["VALID"] * 3)
            self.assertEqual(metrics["integration_quality_status"], "INVALID_TIMESTAMPS")
            self.assertTrue(metrics["excluded_interval_mask"][0])

    def test_quick_scan_phase_gap_policy_remains_documented_as_unresolved(self):
        # Current common integration classifier has no mode-specific cadence
        # limits: only explicit GAP tags and >30 s intervals are excluded.
        for dt in (0.2, 0.5, 1.0, 5.0, 12.5, 30.0):
            metrics = _integration_gap_metrics([0.0, dt], quick_scan=True)
            self.assertFalse(metrics["excluded_interval_mask"][0], f"{dt}s")
        metrics = _integration_gap_metrics([0.0, 30.001, 31.001, 60.001, 90.001],
                                           ["VALID"] * 5,
                                           ["MAIN_DISCHARGE"] * 5, quick_scan=True)
        self.assertTrue(metrics["excluded_interval_mask"][0])

    def test_session_csv_keeps_wall_iso_and_elapsed_session_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "timing.csv")
            handler = DataHandler()
            ok, message = handler.start_logging(path)
            self.assertTrue(ok, message)
            try:
                handler.log_row(0.125, 12.5, 1.0, 80.0, 60.0, 25.0, phase="TEST")
                __import__("time").sleep(0.003)
                handler.log_row(0.375, 12.4, 1.0, 80.0, 60.0, 25.0, phase="TEST")
                handler.csv_file.flush()
                with open(path, encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(rows[0]["Elapsed_s"], "0.125")
                self.assertEqual(rows[1]["Elapsed_s"], "0.375")
                self.assertRegex(rows[0]["Timestamp_ISO"], r"^\d{4}-\d\d-\d\dT")
                self.assertNotEqual(rows[0]["Timestamp_ISO"], rows[1]["Timestamp_ISO"])
            finally:
                handler.stop_logging()

    def test_wall_clock_jump_does_not_change_monotonic_sensor_freshness(self):
        from aset_batt.hardware.mock_hardware import MockHardwareController

        hw = MockHardwareController()
        hw.is_esp_connected = True
        hw.last_esp_heartbeat = __import__("time").perf_counter() - 0.1
        with patch("time.time", side_effect=[1.0e9 + 60.0, 1.0e9 - 60.0]):
            self.assertFalse(hw.temp_is_stale(max_age_s=1.0))
            self.assertFalse(hw.temp_is_stale(max_age_s=1.0))

    def test_cloud_analysis_throttle_ignores_wall_clock_jumps(self):
        from aset_batt.storage import cloud_push

        pusher = cloud_push.CloudPusher(
            "https://example.invalid", token="token", interval=3.0,
            analysis_interval=10.0)
        calls = []
        mono = iter((100.0, 109.0, 110.0))
        with patch.object(cloud_push.time, "monotonic", side_effect=lambda: next(mono)), \
             patch.object(cloud_push.time, "time", side_effect=(10_000.0, 10_060.0, 9_940.0)), \
             patch.object(cloud_push, "_run_analysis", side_effect=lambda *a: calls.append(1) or {"ok": True}), \
             patch.object(cloud_push, "build_payload", return_value={"summary": {"row_count": 0}}), \
             patch.object(cloud_push, "push", return_value=(200, "ok")):
            self.assertTrue(pusher.push_once())
            self.assertTrue(pusher.push_once())
            self.assertTrue(pusher.push_once())
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
