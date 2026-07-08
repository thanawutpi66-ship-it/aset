"""Industrial-grade audit follow-ups R1 and R6.

R1: Alarm Log "Clear" used to wipe the table with zero confirmation, and a
stale comment claimed a "full SCADA" override lived in BatteryQtWindow and
would take over via MRO — it did not (grep confirms _alarm_clear is defined
exactly once, in zones.py). Same shadowing-comment failure class as the
_zone_characterize bug documented in CLAUDE.md. Fixed: confirmation dialog
(skipped in headless/test mode, same established pattern as
_on_ssr_manual_on), stale comment removed.

R6: Manual PSU/Load/Direct-Control controls only checked `_seq_running`,
missing RUN TEST (AcquisitionWorker) and CHARACTERIZE-tab tests — both drive
self.hw from their own background thread exactly like a sequence does, so an
operator could click Manual PSU ON mid-test and issue a conflicting SCPI
command to the same instrument. Fixed by routing through the same
_busy_reason() guard already used by _on_run_test/sequences.
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


class TestAlarmClearConfirmation(unittest.TestCase):
    def test_headless_clear_proceeds_without_blocking(self):
        w = _make_window()
        try:
            w._log_alarm("test event")
            self.assertGreater(w.tbl_alarms.rowCount(), 0)
            w._alarm_clear()   # headless -> confirmation skipped, must not hang
            self.assertEqual(w.tbl_alarms.rowCount(), 0)
        finally:
            w.close()

    def test_only_one_alarm_clear_defined_in_the_codebase(self):
        """Regression guard for the stale-comment bug itself: if a second
        _alarm_clear ever gets added to BatteryQtWindow's own MRO chain, it
        would silently shadow this one exactly like the old comment wrongly
        claimed already happened."""
        owners = [cls for cls in type(_make_window()).__mro__
                  if "_alarm_clear" in cls.__dict__]
        self.assertEqual(len(owners), 1)


class TestManualControlsRespectBusyReason(unittest.TestCase):
    def test_psu_manual_on_blocked_during_run_test(self):
        w = _make_window()
        try:
            w._test_thread = object()   # simulate RUN TEST in progress
            w.hw.set_psu = MagicMock()
            w.ed_psu_v.setText("12.0")
            w.ed_psu_i.setText("1.0")
            w._psu_manual(True)
            w.hw.set_psu.assert_not_called()
        finally:
            w._test_thread = None
            w.close()

    def test_load_manual_on_blocked_during_run_test(self):
        w = _make_window()
        try:
            w._test_thread = object()
            w.hw.set_load = MagicMock()
            w.ed_load_a.setText("1.0")
            w._load_manual(True)
            w.hw.set_load.assert_not_called()
        finally:
            w._test_thread = None
            w.close()

    def test_psu_manual_off_still_allowed_during_run_test(self):
        """Turning OFF must never be blocked — that would leave no way to cut
        power manually while stuck mid-test, exactly the E-STOP-adjacent
        scenario this app treats as always-safe-to-do immediately."""
        w = _make_window()
        try:
            w._test_thread = object()
            w.hw.set_psu = MagicMock()
            w._psu_manual(False)
            w.hw.set_psu.assert_called_once()
        finally:
            w._test_thread = None
            w.close()

    def test_psu_manual_on_allowed_when_nothing_running(self):
        w = _make_window()
        try:
            w.hw.set_psu = MagicMock()
            w.ed_psu_v.setText("12.0")
            w.ed_psu_i.setText("1.0")
            w._psu_manual(True)
            w.hw.set_psu.assert_called_once()
        finally:
            w.close()

    def test_direct_toggle_blocked_during_run_test(self):
        w = _make_window()
        try:
            w._test_thread = object()
            w.rb_charge.setChecked(True)
            w._on_direct_toggled(True)
            self.assertNotEqual(w.run_stack.currentIndex(), 3)
        finally:
            w._test_thread = None
            w.close()


if __name__ == "__main__":
    unittest.main()
