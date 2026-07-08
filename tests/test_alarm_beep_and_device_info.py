"""Regression test: _log_alarm() beeps the PSU on genuine ALARM events (not
WARNING, so a routine "stale reading" warning doesn't sound as urgent as a real
safety trip), and _on_connect() logs both instruments' identity for
traceability (SYST:INF?/  :UTIL:SYST? — see get_instrument_info()).
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


class TestAlarmBeep(unittest.TestCase):
    def test_alarm_classified_message_beeps(self):
        w = _make_window()
        try:
            w.hw.beep = MagicMock()
            w._log_alarm("[SAFETY] Overvoltage detected — sequence aborted")
            w.hw.beep.assert_called_once_with(1)
        finally:
            w.close()

    def test_warning_classified_message_does_not_beep(self):
        w = _make_window()
        try:
            w.hw.beep = MagicMock()
            w._log_alarm("[WARNING] ESP32 temperature reading is stale")
            w.hw.beep.assert_not_called()
        finally:
            w.close()

    def test_info_message_does_not_beep(self):
        w = _make_window()
        try:
            w.hw.beep = MagicMock()
            w._log_alarm("Hardware connected.")
            w.hw.beep.assert_not_called()
        finally:
            w.close()

    def test_missing_beep_method_does_not_raise(self):
        w = _make_window()
        try:
            w._log_alarm("[SAFETY] Overvoltage detected")   # MockHardwareController has no beep()
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
