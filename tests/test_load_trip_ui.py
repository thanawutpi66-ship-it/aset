"""Regression test for the e-load "Check"/"Clear Protection Trip" controls.

An e-load (PEL-3111) UVP alarm fired on the instrument itself during a real
run with NO on-screen indication — the PSU side has always had a trip check/
clear pair (see test_psu_trip_ui.py), the load side never did. Mirrors that
file's structure exactly.
"""
import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.hardware.mock_hardware import MockHardwareController

_app = QApplication.instance() or QApplication([])


def _make_window():
    w = BatteryQtWindow(ConfigManager())
    w.hw = MockHardwareController()
    return w


class TestCheckLoadTrip(unittest.TestCase):
    def test_shows_tripped_state(self):
        w = _make_window()
        try:
            w.hw.get_load_protection_tripped = MagicMock(return_value=True)
            w._on_check_load_trip()
            self.assertIn("TRIPPED", w.lbl_load_trip.text())
        finally:
            w.close()

    def test_shows_ok_state(self):
        w = _make_window()
        try:
            w.hw.get_load_protection_tripped = MagicMock(return_value=False)
            w._on_check_load_trip()
            self.assertIn("OK", w.lbl_load_trip.text())
        finally:
            w.close()

    def test_noop_when_hw_lacks_the_method(self):
        w = _make_window()
        try:
            w._on_check_load_trip()   # MockHardwareController has no get_load_protection_tripped -> must not raise
            self.assertEqual(w.lbl_load_trip.text(), "Trip: —")   # unchanged
        finally:
            w.close()


class TestClearLoadTrip(unittest.TestCase):
    def test_clears_in_headless_mode_without_confirmation_dialog(self):
        w = _make_window()
        try:
            w.hw.clear_load_protection = MagicMock(return_value=True)
            w.hw.get_load_protection_tripped = MagicMock(return_value=False)
            w._on_clear_load_trip()
            w.hw.clear_load_protection.assert_called_once()
        finally:
            w.close()

    def test_noop_when_hw_lacks_the_method(self):
        w = _make_window()
        try:
            w._on_clear_load_trip()   # must not raise
        finally:
            w.close()


class TestGetLoadProtectionTrippedScpi(unittest.TestCase):
    """Lower-level: the SCPI query itself (hardware_driver.py), not the UI.

    Best-effort per get_load_protection_tripped()'s own docstring — the
    PEL-3111 has no OUTPut:PROTection:TRIPped equivalent (that's PSW-only),
    so this reads the SCPI-standard Questionable Condition register and
    treats any nonzero value as "tripped" without identifying which specific
    protection fired."""

    def _hw(self):
        from aset_batt.hardware.hardware_driver import HardwareController
        hw = HardwareController()
        hw.load_inst = MagicMock()
        return hw

    def test_zero_condition_register_is_not_tripped(self):
        hw = self._hw()
        hw.load_inst.query.return_value = "+0"
        self.assertFalse(hw.get_load_protection_tripped())

    def test_nonzero_condition_register_is_tripped(self):
        hw = self._hw()
        hw.load_inst.query.return_value = "+8"
        self.assertTrue(hw.get_load_protection_tripped())

    def test_query_exception_is_not_tripped_not_raised(self):
        hw = self._hw()
        hw.load_inst.query.side_effect = Exception("timeout")
        self.assertFalse(hw.get_load_protection_tripped())

    def test_no_load_inst_is_not_tripped(self):
        from aset_batt.hardware.hardware_driver import HardwareController
        hw = HardwareController()
        self.assertFalse(hw.get_load_protection_tripped())

    def test_clear_reads_event_register(self):
        hw = self._hw()
        ok = hw.clear_load_protection()
        self.assertTrue(ok)
        hw.load_inst.query.assert_called_once_with(":STAT:QUES:EVEN?")

    def test_clear_returns_false_on_exception(self):
        hw = self._hw()
        hw.load_inst.query.side_effect = Exception("timeout")
        self.assertFalse(hw.clear_load_protection())

    def test_clear_no_load_inst_returns_false(self):
        from aset_batt.hardware.hardware_driver import HardwareController
        hw = HardwareController()
        self.assertFalse(hw.clear_load_protection())


from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController


def _make_bound_window():
    """Same helper as tests/test_temp_stale_escalation.py's _make_bound_window
    — a real BatteryQtWindow bound to a real AutoController over mock
    hardware, so _seq_check_load_trip() can be called directly the same way
    the discharge loops call it."""
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


class TestSeqCheckLoadTripEscalation(unittest.TestCase):
    def test_tripped_aborts_the_sequence_and_alarms(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            hw.get_load_protection_tripped = MagicMock(return_value=True)
            alarms = []
            win.sig_alarm.connect(alarms.append)

            result = win._seq_check_load_trip()

            self.assertFalse(result)
            self.assertFalse(win._seq_running.is_set())   # sequence aborted
            self.assertTrue(any("safety" in a.lower() for a in alarms))
            self.assertTrue(ctrl.safety_triggered)
        finally:
            win.close()

    def test_not_tripped_returns_true_no_alarm(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            hw.get_load_protection_tripped = MagicMock(return_value=False)
            alarms = []
            win.sig_alarm.connect(alarms.append)

            result = win._seq_check_load_trip()

            self.assertTrue(result)
            self.assertTrue(win._seq_running.is_set())   # not aborted
            self.assertEqual(alarms, [])
        finally:
            win.close()

    def test_backend_without_the_method_defaults_safe(self):
        """MockHardwareController has no get_load_protection_tripped by
        default (same as real simulation mode) -- must not raise, must not
        abort a healthy sequence."""
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            self.assertFalse(hasattr(hw, "get_load_protection_tripped"))

            result = win._seq_check_load_trip()

            self.assertTrue(result)
            self.assertTrue(win._seq_running.is_set())
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
