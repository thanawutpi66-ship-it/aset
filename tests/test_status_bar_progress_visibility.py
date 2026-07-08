"""Industrial-grade audit follow-up G3.

wf_progress/lbl_eta (the test progress bar + ETA label) used to live only
inside the SETUP tab's AUTO-sequence sub-page — switching to TEST MODE or a
different workflow sub-tab during a running test hid progress entirely, with
no way to check it without switching back. status_progress (in the status
bar, always visible regardless of the active tab) now mirrors it, driven from
the same single update site (_slot_phase_progress).
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


class TestStatusBarProgressMirrorsWorkflowProgress(unittest.TestCase):
    def test_progress_update_shows_both_bars_with_matching_values(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            w._slot_phase_progress(45, 300)

            # isHidden() (not isVisible()) — the test window is never shown, so
            # isVisible() would read False regardless of these widgets' own
            # explicit show()/hide() calls (Qt visibility composes with ancestors).
            self.assertFalse(w.wf_progress.isHidden())
            self.assertFalse(w.status_progress.isHidden())
            self.assertEqual(w.wf_progress.value(), 45)
            self.assertEqual(w.status_progress.value(), 45)
            self.assertEqual(w.status_progress.maximum(), 300)
        finally:
            w.close()

    def test_zero_total_hides_both_bars(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            w._slot_phase_progress(10, 100)   # show first
            w._slot_phase_progress(0, 0)       # then hide

            self.assertTrue(w.wf_progress.isHidden())
            self.assertTrue(w.status_progress.isHidden())
        finally:
            w.close()

    def test_status_progress_shows_eta_text(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            w._slot_phase_progress(60, 180)
            self.assertIn("ETA", w.status_progress.format())
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
