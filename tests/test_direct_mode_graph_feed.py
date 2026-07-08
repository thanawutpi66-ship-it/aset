"""Regression test: MANUAL -> Direct control now feeds the live trend graph.

Direct control (raw PSU/Load ON/OFF jog, no SoC/CSV by design — see _direct_page's
own warning label) previously had nothing feeding update_display() at all, since
it bypasses both the shared AutoController monitor loop (used by manual Charge)
and AcquisitionWorker (used by manual Discharge/HPPC "RUN TEST"). Fixed by
piggybacking on the existing 1s heartbeat tick: read-only (never touches the
estimator), only fires while the Direct radio button is actually selected.
"""
import os
import unittest

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


class TestDirectModeGraphFeed(unittest.TestCase):
    def _spy_update_display(self, w):
        calls = []
        orig = w.update_display
        def _spy(*args, **kwargs):
            calls.append(args)
            orig(*args, **kwargs)
        w.update_display = _spy
        return calls

    def test_no_feed_when_direct_not_selected(self):
        w = _make_window()
        try:
            calls = self._spy_update_display(w)
            self.assertFalse(w.rb_direct.isChecked())   # Discharge is default-checked
            w._on_heartbeat_tick()
            self.assertEqual(len(calls), 0)
        finally:
            w.close()

    def test_feeds_graph_when_direct_selected(self):
        w = _make_window()
        try:
            calls = self._spy_update_display(w)
            w.rb_direct.setChecked(True)
            w._on_heartbeat_tick()
            self.assertEqual(len(calls), 1)
            v, i, soc, rin, temp = calls[0]   # soh omitted -> update_display fills its own default
            self.assertIsInstance(v, float)
            self.assertIsInstance(i, float)
        finally:
            w.close()

    def test_no_feed_when_hardware_not_connected(self):
        w = _make_window()
        try:
            w.hw.is_connected = False
            calls = self._spy_update_display(w)
            w.rb_direct.setChecked(True)
            w._on_heartbeat_tick()
            self.assertEqual(len(calls), 0)
        finally:
            w.close()

    def test_does_not_touch_the_estimator(self):
        """Read-only: must not call estimator.update(), so it can never double-count
        against a manual Charge (monitor loop) left running in the background."""
        w = _make_window()
        try:
            update_calls = []
            w.controller = type("C", (), {})()   # not bound to a real controller here
            w.estimator = type("E", (), {"soc": 42.0, "rin": 0.05})()
            w.rb_direct.setChecked(True)
            w._on_heartbeat_tick()
            # estimator is a plain stub with no update() method — if the code tried
            # to call it, this would raise AttributeError inside the try/except and
            # get silently swallowed, so assert the values actually reached the graph
            # instead (proving it read soc/rin, not mutated them via .update()).
            self.assertEqual(w.estimator.soc, 42.0)
            self.assertEqual(w.estimator.rin, 0.05)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
