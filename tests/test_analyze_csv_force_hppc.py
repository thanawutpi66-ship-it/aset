"""Regression test: manually re-analysing a just-finished (or older, properly
labelled) HPPC CSV via the "Analyze Last CSV" button must grade correctly.

Root cause of the original bug report: analyze_csv()'s Mode-column auto-detection
is dead in practice — DataHandler.log_row() never writes a Mode column — so it only
ever classifies a record as HPPC when the caller passes force_hppc=True explicitly.
sequences.py's own post-sequence auto-analysis does pass it
(_auto_analyze(force_hppc=True)), but _on_analyze_csv (the manual button) did not,
so re-analysing an HPPC record after the fact silently misclassified it as a plain
discharge test: it "never reaches cut-off" (HPPC pulses aren't meant to), giving an
ungradeable REVIEW result (shown as N/A) even though the exact same file grades A
right after the sequence finishes.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestAnalyzeCsvForceHppc(unittest.TestCase):
    def _make_win(self, csv_path, current_test_name=""):
        win = BatteryQtWindow(ConfigManager())
        win._last_csv = csv_path
        win._current_test_name = current_test_name
        win.controller = MagicMock()
        return win

    def test_uses_current_test_name_for_just_finished_hppc_sequence(self):
        """The exact scenario from the bug report: HPPC Full Sequence just ran in
        this app session (so _current_test_name is still set), then the user
        clicks "Analyze Last CSV" on a file whose name never got HPPC-labelled
        (e.g. logging was already open before the sequence started)."""
        win = self._make_win("nonexistent_but_present.csv", "HPPC Full Sequence")
        try:
            with patch("os.path.exists", return_value=True), \
                 patch.object(win, "_detect_session_type", return_value="Data Log"), \
                 patch("aset_batt.acquisition.analysis.analyze_csv_mp") as mock_analyze:
                mock_analyze.return_value = {"grade": "A"}
                win._on_analyze_csv()
                # work() runs on a daemon thread — give it a moment to execute.
                import time
                for _ in range(50):
                    if mock_analyze.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(mock_analyze.called)
                _, kwargs = mock_analyze.call_args
                self.assertTrue(kwargs.get("force_hppc"))
        finally:
            win.close()

    def test_falls_back_to_detect_session_type_for_older_labelled_file(self):
        """No in-session memory of the test type (e.g. after an app restart) —
        must still recognise a properly test_HPPC_*-labelled file via the same
        classifier that already labels it "HPPC" in the session list."""
        win = self._make_win("sessions/test_HPPC_20260101_000000.csv", "")
        try:
            with patch("os.path.exists", return_value=True), \
                 patch.object(win, "_detect_session_type", return_value="HPPC"), \
                 patch("aset_batt.acquisition.analysis.analyze_csv_mp") as mock_analyze:
                mock_analyze.return_value = {"grade": "A"}
                win._on_analyze_csv()
                import time
                for _ in range(50):
                    if mock_analyze.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(mock_analyze.called)
                _, kwargs = mock_analyze.call_args
                self.assertTrue(kwargs.get("force_hppc"))
        finally:
            win.close()

    def test_plain_discharge_file_is_not_forced_hppc(self):
        win = self._make_win("sessions/test_20260101_000000.csv", "Quick Scan")
        try:
            with patch("os.path.exists", return_value=True), \
                 patch.object(win, "_detect_session_type", return_value="Data Log"), \
                 patch("aset_batt.acquisition.analysis.analyze_csv_mp") as mock_analyze:
                mock_analyze.return_value = {"grade": "A"}
                win._on_analyze_csv()
                import time
                for _ in range(50):
                    if mock_analyze.called:
                        break
                    time.sleep(0.02)
                self.assertTrue(mock_analyze.called)
                _, kwargs = mock_analyze.call_args
                self.assertFalse(kwargs.get("force_hppc"))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
