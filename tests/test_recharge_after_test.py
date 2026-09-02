"""Regression test for the opt-in post-test recharge (prevent sulfation).

No sequence used to charge the battery back after finishing -- every
sequence's finally block only cuts PSU/load outputs, so a pack could sit at
whatever low SoC the test ended at (often near cutoff) indefinitely. Added as
config.system.recharge_after_test (default OFF -- this can add 1-8h to every
run, so it must be a deliberate operator choice) + BaseSequenceMixin.
_seq_recharge_after_test(), called from all 4 sequence threads right after
their own completed_ok = True.

Direct-call style (same pattern as test_load_trip_ui.py's
TestSeqCheckLoadTripEscalation / test_temp_stale_escalation.py's
TestSeqCheckTempStaleEscalation) -- exercises the shared helper itself
rather than driving a full multi-hour sequence thread end-to-end.
"""
import os
import unittest
from unittest.mock import MagicMock, call

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
    return win, ctrl, hw


class TestRechargeAfterTestGating(unittest.TestCase):
    def test_noop_when_config_flag_off(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = False
            win._seq_running.set()
            ctrl.start_charge = MagicMock()
            sig = MagicMock()

            win._seq_recharge_after_test(sig, 5)

            ctrl.start_charge.assert_not_called()
            sig.emit.assert_not_called()
        finally:
            win.close()

    def test_noop_when_sequence_already_aborted(self):
        """flag True but _seq_running already cleared (an abort/safety-trip
        happened before this point) -- must not recharge into whatever
        caused that."""
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = True
            win._seq_running.clear()
            ctrl.start_charge = MagicMock()
            sig = MagicMock()

            win._seq_recharge_after_test(sig, 5)

            ctrl.start_charge.assert_not_called()
            sig.emit.assert_not_called()
        finally:
            win.close()


class TestRechargeAfterTestExecution(unittest.TestCase):
    def test_runs_and_completes_when_enabled_and_running(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = True
            win._seq_running.set()
            ctrl.start_charge = MagicMock()
            ctrl.is_charging = False   # already "done" on first poll check
            ctrl.monitor_running = False
            sig = MagicMock()
            alarms = []
            win.sig_alarm.connect(alarms.append)

            win._seq_recharge_after_test(sig, 5)

            ctrl.start_charge.assert_called_once()
            sig.emit.assert_any_call(5, "active")
            sig.emit.assert_any_call(5, "done")
            self.assertTrue(any("recharge" in a.lower() for a in alarms))
        finally:
            win.close()

    def test_stops_monitor_after_recharge_if_it_was_restarted(self):
        """start_charge() restarts the shared monitor loop (same as every
        other CHARGE phase) -- must be stopped again after, or it
        double-counts alongside whatever the next test does."""
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = True
            win._seq_running.set()
            ctrl.start_charge = MagicMock()
            ctrl.is_charging = False
            ctrl.monitor_running = True
            ctrl.stop_monitor = MagicMock()
            sig = MagicMock()

            win._seq_recharge_after_test(sig, 5)

            ctrl.stop_monitor.assert_called_once()
        finally:
            win.close()

    def test_aborted_mid_recharge_does_not_emit_done(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = True
            win._seq_running.set()
            ctrl.start_charge = MagicMock()
            ctrl.is_charging = True   # still charging -> enters the poll loop

            def _abort_during_sleep(seconds):
                win._seq_running.clear()
                return False
            win._seq_sleep = MagicMock(side_effect=_abort_during_sleep)
            sig = MagicMock()

            win._seq_recharge_after_test(sig, 5)

            ctrl.start_charge.assert_called_once()
            sig.emit.assert_any_call(5, "active")
            self.assertNotIn(call(5, "done"), sig.emit.call_args_list)
        finally:
            win.close()

    def test_polls_with_read_vi_and_status_signal(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win.config.system.recharge_after_test = True
            win._seq_running.set()
            ctrl.start_charge = MagicMock()
            # charging for exactly one poll iteration, then done
            _calls = {"n": 0}
            def _is_charging_prop():
                _calls["n"] += 1
                return _calls["n"] <= 1
            type(ctrl).is_charging = property(lambda self: _is_charging_prop())
            ctrl.monitor_running = False
            sig = MagicMock()
            statuses = []
            win.sig_wf_status.connect(statuses.append)

            win._seq_recharge_after_test(sig, 5)

            self.assertTrue(any("RECHARGE" in s for s in statuses))
        finally:
            win.close()
            del type(ctrl).is_charging


class TestRechargeAfterTestConfigRoundTrip(unittest.TestCase):
    def test_default_is_false(self):
        cfg = ConfigManager()
        self.assertFalse(cfg.system.recharge_after_test)

    def test_roundtrips_through_save_load(self):
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as d:
            path = _os.path.join(d, "config.json")
            cfg = ConfigManager(config_file=path)
            cfg.system.recharge_after_test = True
            cfg.save_config()
            cfg2 = ConfigManager(config_file=path)
            self.assertTrue(cfg2.system.recharge_after_test)


if __name__ == "__main__":
    unittest.main()
