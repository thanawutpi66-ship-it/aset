"""Regression test: the E-STOP button (and mode/state badges) must live in a real,
reliably-rendered widget, not a QMenuBar corner widget.

Root cause of the original bug report: these were added via
`QMenuBar.setCornerWidget()`, which constructs and wires the widgets correctly (so
every prior headless/offscreen smoke test passed) but is unreliable with native
platform menu-bar styles — on a real Windows session the corner widget silently
failed to paint at all, making E-STOP (and the mode/state indicators) invisible in
the running app despite the code being "correct." Offscreen/headless rendering does
not reproduce this — it only proves the widgets exist and are wired, not that a real
window paints them — so this test asserts placement (a real QToolBar, docked, always
visible) rather than just existence, to actually catch a regression back to
setCornerWidget().

Note: a concurrent session fixed this same bug independently (see
_build_toolbar(), "Main" toolbar via addToolBar()) while this session's own
"Status" toolbar fix was still uncommitted — reconciled by adopting the
already-merged _build_toolbar() version and dropping the duplicate. This test
was updated to match what's actually on main now, not this session's original
(superseded) implementation.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication, QToolBar
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestEstopLivesInARealToolbar(unittest.TestCase):
    def test_estop_button_parent_is_a_toolbar_not_a_menubar_corner(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            self.assertTrue(hasattr(win, "btn_estop"))
            self.assertIsInstance(win.btn_estop.parentWidget(), QToolBar)
            # setCornerWidget() must not be used for this again — assert the menu
            # bar reports no corner widget at all.
            self.assertIsNone(win.menuBar().cornerWidget())
        finally:
            win.close()

    def test_toolbar_is_docked_and_not_movable(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            toolbars = [c for c in win.children() if isinstance(c, QToolBar)]
            self.assertEqual(len(toolbars), 1)
            tb = toolbars[0]
            self.assertEqual(tb.windowTitle(), "Main")
            self.assertFalse(tb.isMovable())
            self.assertIs(win.btn_estop.parentWidget(), tb)
            self.assertIs(win.mode_badge.parentWidget(), tb)
            self.assertIs(win.state_pill.parentWidget(), tb)
        finally:
            win.close()

    def test_estop_click_triggers_safety(self):
        from unittest.mock import MagicMock
        win = BatteryQtWindow(ConfigManager())
        try:
            win.controller = MagicMock()
            win.btn_estop.click()
            win.controller._trigger_safety.assert_called_once()
        finally:
            win.close()

    def test_no_dead_build_header_method(self):
        """_build_header() built mode_badge/conn_led/state_pill widgets that were
        never added to any layout — every widget it created was immediately
        overwritten by the real ones from _build_menubar()/_build_statusbar(), so it
        was a fully dead method. Regression guard against it reappearing."""
        win = BatteryQtWindow(ConfigManager())
        try:
            self.assertFalse(hasattr(win, "_build_header"))
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
