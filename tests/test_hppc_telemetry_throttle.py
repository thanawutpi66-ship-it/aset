"""Regression test: _on_hppc_telemetry() must skip setText()/setStyleSheet()
when the phase-indicator text hasn't changed since the last sample.

Real-rig evidence (2026-07-10): AcquisitionWorker's new per-substep timing
breakdown showed a manual HPPC run's per-sample cost 80-92% "other" (SCPI
only 8-20%, log ~0%) despite the achieved rate already being well under the
5 Hz target. _on_hppc_telemetry ran unconditionally on EVERY telemetry
sample with no throttle at all (unlike _on_test_telemetry's already-
throttled trend redraw) — setStyleSheet() re-parses the whole stylesheet
string on every call, for a label whose text only actually changes once a
second (the countdown). It runs on the GUI thread reacting to the SAME
signal that paces the worker thread, so GIL contention here can slow the
worker down too, not just the GUI repaint.
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

_app = QApplication.instance() or QApplication([])


class TestHppcTelemetryThrottle(unittest.TestCase):
    def setUp(self):
        self.win = BatteryQtWindow(ConfigManager())
        self.win.ed_hppc_pulse.setText("30")
        self.win.ed_hppc_relax.setText("180")

    def tearDown(self):
        self.win.close()

    def test_unchanged_phase_text_skips_qt_calls(self):
        self.win.lbl_hppc_phase.setText = MagicMock(wraps=self.win.lbl_hppc_phase.setText)
        self.win.lbl_hppc_phase.setStyleSheet = MagicMock(wraps=self.win.lbl_hppc_phase.setStyleSheet)

        # 180 - 5.3 = 174.7 -> int() = 174 — mid-second, not on an integer
        # boundary, so nearby samples land in the same truncated-seconds bucket.
        self.win._on_hppc_telemetry({"elapsed": 5.3})    # REST, 174s left -> first render
        self.assertEqual(self.win.lbl_hppc_phase.setText.call_count, 1)
        self.assertEqual(self.win.lbl_hppc_phase.setStyleSheet.call_count, 1)

        # same integer-second bucket -> identical text -> must be skipped
        self.win._on_hppc_telemetry({"elapsed": 5.35})
        self.win._on_hppc_telemetry({"elapsed": 5.4})
        self.assertEqual(self.win.lbl_hppc_phase.setText.call_count, 1,
                         "unchanged phase text must not re-trigger setText")
        self.assertEqual(self.win.lbl_hppc_phase.setStyleSheet.call_count, 1,
                         "unchanged phase text must not re-trigger setStyleSheet")

    def test_changed_phase_text_still_updates(self):
        self.win._on_hppc_telemetry({"elapsed": 5.0})     # REST
        self.win.lbl_hppc_phase.setText = MagicMock(wraps=self.win.lbl_hppc_phase.setText)
        self.win._on_hppc_telemetry({"elapsed": 6.0})     # REST, one second later -> countdown changed
        self.assertEqual(self.win.lbl_hppc_phase.setText.call_count, 1)

    def test_new_test_start_resets_the_guard(self):
        self.win._on_hppc_telemetry({"elapsed": 5.0})
        self.assertIsNotNone(self.win._last_hppc_phase_text)
        # Simulate what _on_run_test does when starting a fresh run.
        self.win._last_hppc_phase_text = None
        self.win.lbl_hppc_phase.setText = MagicMock(wraps=self.win.lbl_hppc_phase.setText)
        self.win._on_hppc_telemetry({"elapsed": 5.0})     # identical elapsed as before the reset
        self.assertEqual(self.win.lbl_hppc_phase.setText.call_count, 1,
                         "a fresh run must render its first sample even if the text repeats")


if __name__ == "__main__":
    unittest.main()
