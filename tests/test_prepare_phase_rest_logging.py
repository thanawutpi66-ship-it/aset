"""Regression test: PREPARE's OCV-settle wait now gets logged to CSV/cloud and
feeds the live graph, instead of being invisible to both.

Root cause: _ensure_logging(label=...) was only ever called at CHARGE/DISCHARGE
phase. But start_charge() (via start_monitor()) already implicitly opens a CSV
session the moment CHARGE begins — so by the time the later _ensure_logging()
call ran, data.is_recording was already True and its label was ignored. Net
effect: the PREPARE phase's rest/OCV-settle wait was NEVER captured in the CSV
at all, even though it genuinely happened. Every HPPC/sequence test therefore
started its CSV mid-charge, and _quality_flags' "no clear rest before load"
check (which looks at the first 25 samples) always fired — a systematic
false-positive on real, healthy rest data, not a real data-quality problem.

Fixed by calling _ensure_logging() right at the top of PREPARE (before the OCV
wait begins) and feeding each on_progress callback into _log_sample()/
update_display() in all four sequences (IEC/QuickScan/HPPC/CycleLife).

These tests mock calibrate_from_ocv_stable() itself to synchronously fire
on_progress a few times (representing real 5 s-interval OCV settle samples)
and then signal cancellation, so the thread exits right after PREPARE without
needing to actually wait through a real multi-minute settle or run the rest
of the (multi-hour) sequence.
"""
import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.hardware.mock_hardware import MockHardwareController

_app = QApplication.instance() or QApplication([])


def _make_bound_window():
    cfg = ConfigManager()
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    return win, ctrl, hw, data


def _fake_calibrate_stable(win, samples=4):
    """Replaces controller.calibrate_from_ocv_stable(): fires on_progress a few
    times synchronously (like real 5s-interval OCV samples), then cancels the
    sequence so the thread returns right after PREPARE."""
    def _fake(on_progress=None, cancel_check=None):
        for i in range(samples):
            if on_progress:
                on_progress(float(i * 5), 12.6 + i * 0.01, 5.0, "waiting")
        win._seq_running.clear()
        return 80.0, 12.64, "timeout"
    return _fake


class TestIecPrepareLogsRest(unittest.TestCase):
    def test_prepare_calls_ensure_logging_before_ocv_wait_and_logs_samples(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            win._seq_running.set()
            ctrl.calibrate_from_ocv_stable = _fake_calibrate_stable(win)

            log_calls = []
            orig_log_sample = ctrl._log_sample
            def _spy_log_sample(*a, **k):
                log_calls.append(a)
                orig_log_sample(*a, **k)
            ctrl._log_sample = _spy_log_sample

            display_calls = []
            orig_update_display = win.update_display
            def _spy_update_display(*a, **k):
                display_calls.append(a)
                orig_update_display(*a, **k)
            win.update_display = _spy_update_display

            opts = {"skip_charge": True, "skip_rest": True, "soc_thresh": 100,
                    "seq_crate": "1.0C", "rest_min": 1, "test_crate": "0.2C"}
            win._auto_sequence_thread(opts)

            self.assertTrue(data.is_recording, "CSV must already be open during PREPARE")
            self.assertEqual(len(log_calls), 4, "each on_progress sample must be logged")
            self.assertEqual(len(display_calls), 4, "each on_progress sample must feed the graph")
            # every logged sample during PREPARE is a rest sample (0.0 A)
            for args in log_calls:
                self.assertEqual(args[1], 0.0)
        finally:
            win.close()


class TestQuickScanPrepareLogsRest(unittest.TestCase):
    def test_5min_rest_loop_logs_samples(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            win._seq_running.set()
            ctrl.calibrate_from_ocv = MagicMock(return_value=75.0)

            log_calls = []
            orig_log_sample = ctrl._log_sample
            def _spy_log_sample(*a, **k):
                log_calls.append(a)
                orig_log_sample(*a, **k)
            ctrl._log_sample = _spy_log_sample

            # Let the very first sleep (before Phase 0) complete normally, then force
            # the 5-min rest loop to run exactly one iteration and stop.
            calls = {"n": 0}
            def _fake_sleep(seconds):
                calls["n"] += 1
                if calls["n"] == 1:
                    return True
                win._seq_running.clear()
                return False
            win._seq_sleep = _fake_sleep

            win._quick_scan_thread()

            self.assertTrue(data.is_recording)
            self.assertGreaterEqual(len(log_calls), 1)
            self.assertEqual(log_calls[0][1], 0.0)
        finally:
            win.close()


class TestQualityFlagNoLongerFalsePositive(unittest.TestCase):
    def test_rest_samples_at_head_clear_the_warning(self):
        from aset_batt.acquisition.analysis import _quality_flags
        from aset_batt.acquisition.models import BatteryProfile

        profile = BatteryProfile(
            name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
            capacity_ah=5.3, max_charge_v=14.7, cutoff_v=10.5,
            max_charge_a=1.0, max_discharge_a=5.0,
            ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0,
            internal_r=0.03,
        )
        # Before the fix: recording started mid-charge, first samples were bulk
        # charge current (~-0.5A), not rest (only a handful of near-zero samples
        # in the first 25 — below the "at least 5" threshold _quality_flags checks).
        current_before = [-0.5] * 21 + [0.0] * 4
        warnings_before, _ = _quality_flags(current_before, [12.7] * 25, [25.0] * 25,
                                            profile, is_hppc=True, n_steps=1,
                                            reached_cutoff=False)
        self.assertTrue(any("no clear rest" in w for w in warnings_before))

        # After the fix: PREPARE's rest samples (0.0 A) are now at the head.
        current_after = [0.0] * 10 + [-0.5] * 15
        warnings_after, _ = _quality_flags(current_after, [12.7] * 25, [25.0] * 25,
                                           profile, is_hppc=True, n_steps=1,
                                           reached_cutoff=False)
        self.assertFalse(any("no clear rest" in w for w in warnings_after))


if __name__ == "__main__":
    unittest.main()
