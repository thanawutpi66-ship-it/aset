"""Regression tests for the toolbar S/N badge ("s/n ไม่ขึ้นเมื่อกรอกเสร็จ"):
it must behave like the SIMULATION badge — visible from app start whenever
config.battery.serial_number is set, and refreshed through every path that
writes the serial number, including the ones that emit no textChanged signal.
Exercises the real window wiring per this repo's testing convention.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestSnBadgePersistence(unittest.TestCase):
    def test_sn_from_config_shows_at_construction(self):
        """An S/N already saved in config.json must show from app start,
        without waiting for the first profile-status update."""
        cfg = ConfigManager()
        cfg.battery.serial_number = "SAVED-001"
        win = BatteryQtWindow(cfg)
        try:
            self.assertTrue(win._sn_action.isVisible())
            self.assertIn("SAVED-001", win.lbl_active_sn.text())
        finally:
            win.close()

    def test_refresh_covers_the_settext_same_text_gap(self):
        """The pretest dialog writes config then ed_sn.setText(sn); when the
        text is unchanged Qt emits no textChanged, so the direct
        _refresh_sn_badge() call is the only thing that shows the badge."""
        cfg = ConfigManager()
        cfg.battery.serial_number = ""
        win = BatteryQtWindow(cfg)
        try:
            win.ed_sn.blockSignals(True)          # simulate pre-filled field
            win.ed_sn.setText("LAB-42")
            win.ed_sn.blockSignals(False)
            self.assertFalse(win._sn_action.isVisible())

            # what on_confirm does: write config, setText (same text — no
            # signal), then refresh directly
            win.config.battery.serial_number = "LAB-42"
            win.ed_sn.setText("LAB-42")
            win._refresh_sn_badge()
            self.assertTrue(win._sn_action.isVisible())
            self.assertIn("LAB-42", win.lbl_active_sn.text())
        finally:
            win.close()

    def test_typing_updates_and_clearing_hides(self):
        cfg = ConfigManager()
        cfg.battery.serial_number = ""
        win = BatteryQtWindow(cfg)
        try:
            self.assertFalse(win._sn_action.isVisible())
            win.ed_sn.setText("BATT-7")           # textChanged path
            self.assertTrue(win._sn_action.isVisible())
            self.assertIn("BATT-7", win.lbl_active_sn.text())
            win.ed_sn.setText("")
            self.assertFalse(win._sn_action.isVisible())
            self.assertEqual(win.lbl_active_sn.text(), "")
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
