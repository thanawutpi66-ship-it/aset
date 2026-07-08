"""Regression test for the manual "Clear Protection Trip" control (Direct tab).

Hardware OVP/OCP/OTP protection is applied automatically on every Connect (see
_on_connect in isa101_views.py) — but clearing an actual trip stays a deliberate
operator action (a trip means something real happened), never auto-retried.
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


class TestCheckPsuTrip(unittest.TestCase):
    def test_shows_tripped_state(self):
        w = _make_window()
        try:
            w.hw.get_psu_protection_tripped = MagicMock(return_value=True)
            w._on_check_psu_trip()
            self.assertIn("TRIPPED", w.lbl_psu_trip.text())
        finally:
            w.close()

    def test_shows_ok_state(self):
        w = _make_window()
        try:
            w.hw.get_psu_protection_tripped = MagicMock(return_value=False)
            w._on_check_psu_trip()
            self.assertIn("OK", w.lbl_psu_trip.text())
        finally:
            w.close()

    def test_noop_when_hw_lacks_the_method(self):
        w = _make_window()
        try:
            w._on_check_psu_trip()   # MockHardwareController has no get_psu_protection_tripped -> must not raise
            self.assertEqual(w.lbl_psu_trip.text(), "Trip: —")   # unchanged
        finally:
            w.close()


class TestClearPsuTrip(unittest.TestCase):
    def test_clears_in_headless_mode_without_confirmation_dialog(self):
        w = _make_window()
        try:
            w.hw.clear_psu_protection = MagicMock(return_value=True)
            w.hw.get_psu_protection_tripped = MagicMock(return_value=False)
            w._on_clear_psu_trip()
            w.hw.clear_psu_protection.assert_called_once()
        finally:
            w.close()

    def test_noop_when_hw_lacks_the_method(self):
        w = _make_window()
        try:
            w._on_clear_psu_trip()   # must not raise
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
