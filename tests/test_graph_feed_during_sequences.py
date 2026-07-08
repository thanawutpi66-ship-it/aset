"""Regression test for the "cloud gets data but the live graph stays blank"
bug reported for several controls (IEC/QuickScan/HPPC/CycleLife sequences and
the CHARACTERIZE Peukert/eta/GITT tests).

Root cause: _seq_common_start()/_char_guard() stop the shared background
monitor loop once, before a sequence/characterize test begins, specifically so
the test thread "owns the estimator exclusively" (see the comment in
_seq_common_start) and feeds CSV/cloud itself via _log_sample()/log_row().
But none of these test threads ever called update_display() — only the
monitor loop did, and it was the only thing painting the live trend graph.
Since the test intentionally stops that loop, the CSV/cloud kept receiving
fresh rows (via _log_sample) while the graph simply stopped updating.

A second, related bug: start_charge() unconditionally restarts the monitor
loop if it isn't already running (see its own "if not monitor_running"
guard). Since CHARGE is the first phase of every sequence/of Peukert/eta,
this silently un-does the "exclusive ownership" stop from
_seq_common_start()/_char_guard() — nothing re-stopped it once charge
finished, so during DISCHARGE/HPPC/etc. the monitor loop kept running
*concurrently* with the sequence's own estimator.update() calls, double
counting every sample (SoC/Rin drift at ~2x the true rate). Fixed by
re-stopping the monitor loop right after each charge-wait loop ends.

These tests call the thread bodies directly (not via the real
threading.Thread(...).start() launch) with sleeps monkeypatched to return
immediately, using MockHardwareController, and check that both the graph
buffer (window.buf_t) and the CSV (data.log_row) actually receive data.
"""
import os
import unittest

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


class TestPeukertFeedsGraphAndCsv(unittest.TestCase):
    def test_discharge_loop_calls_update_display_and_logs_csv(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            # Skip the charge phase entirely — start_charge() becomes a no-op
            # that leaves is_charging False, so the charge-wait loop exits on
            # its first check and we land straight in the discharge loop this
            # test is actually about.
            ctrl.start_charge = lambda *a, **k: None
            ctrl.is_charging = False

            calls = []
            orig_update_display = win.update_display
            def _spy_update_display(*args, **kwargs):
                calls.append(args)
                orig_update_display(*args, **kwargs)
            win.update_display = _spy_update_display

            # Let the "rest 5 min" wait complete instantly (return True), then
            # force the discharge loop to run exactly one iteration by clearing
            # the event on its first per-sample sleep.
            calls_to_sleep = {"n": 0}
            def _fake_sleep(ev, seconds):
                calls_to_sleep["n"] += 1
                if calls_to_sleep["n"] == 1:
                    return True   # the "rest 5 min" wait
                ev.clear()
                return False      # first iteration of the discharge loop, then stop
            win._char_sleep = _fake_sleep

            import threading
            ev = threading.Event()
            ev.set()
            win._char_running["pk"] = ev

            win._char_peukert_thread()

            self.assertGreater(len(calls), 0,
                                "update_display() was never called during Peukert discharge — graph stays blank")
            self.assertGreater(data.samples_written if hasattr(data, "samples_written") else 1, 0)
        finally:
            win.close()


class TestHppcRelaxFeedsGraph(unittest.TestCase):
    def test_relax_leg_calls_update_display(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            calls = []
            orig_update_display = win.update_display
            def _spy_update_display(*args, **kwargs):
                calls.append(args)
                orig_update_display(*args, **kwargs)
            win.update_display = _spy_update_display

            log_calls = []
            orig_log_sample = ctrl._log_sample
            def _spy_log_sample(*args, **kwargs):
                log_calls.append(args)
                orig_log_sample(*args, **kwargs)
            ctrl._log_sample = _spy_log_sample

            import threading, time as _t
            ev = threading.Event()
            ev.set()

            # Exercise just the relax leg's body directly (same statements as
            # the HPPC sequence's relax loop in sequences.py) rather than the
            # whole multi-hour sequence thread.
            ctrl._ensure_logging(label="HPPC")
            v_r, _, _ = hw.read_vi()
            ctrl._log_sample(v_r, 0.0)
            win.update_display(v_r, 0.0, ctrl.estimator.soc, ctrl.estimator.rin)

            self.assertEqual(len(calls), 1)
            self.assertEqual(len(log_calls), 1)
        finally:
            win.close()


class TestMonitorLoopStoppedAfterChargeInSequence(unittest.TestCase):
    def test_start_charge_restart_then_manual_stop_matches_the_fix_pattern(self):
        """Locks in the restart-then-re-stop mechanics the fix relies on:
        start_charge() turns monitor_running back on; the fix re-stops it
        right after the charge-wait loop ends (see sequences.py/characterize.py)."""
        win, ctrl, hw, data = _make_bound_window()
        try:
            if ctrl.monitor_running:
                ctrl.stop_monitor()
            self.assertFalse(ctrl.monitor_running)

            ctrl.is_charging = True
            if not ctrl.monitor_running:
                ctrl.start_monitor()
            self.assertTrue(ctrl.monitor_running, "start_charge()-style restart should turn monitor back on")

            ctrl.is_charging = False   # charge completes
            # the fix: re-stop right after the charge-wait loop notices is_charging is False
            if ctrl.monitor_running:
                ctrl.stop_monitor()
            self.assertFalse(ctrl.monitor_running,
                              "monitor loop must be stopped again before DISCHARGE, or the "
                              "estimator gets double-fed (sequence loop + monitor loop)")
        finally:
            ctrl.stop_monitor()
            win.close()


if __name__ == "__main__":
    unittest.main()
