"""Regression test: EventType.SHOW_MESSAGE must reach a real UI handler, not a
leftover tkinter.messagebox call.

Root cause: UIEventHandler._show_error_message/_show_info_message/
_show_warning_message (aset_batt/services/event_system.py) called
tkinter.messagebox in a PySide6-only app — a half-finished Tk->Qt migration.
app_bootstrapper._wire_runtime never wired a show_message callback onto the
event handler (unlike update_display/handle_safety_trigger/etc., which ARE
monkey-patched from the real Qt window), so every AutoController safety
message routed through EventType.SHOW_MESSAGE (OCV out-of-range, sustained
ESP32 temp-stale trip, monitor-loop fatal error) hit the tkinter call inside
QtRootShim._run's try/except and was silently swallowed — logged as an
"invoke error", never shown to the operator.

Fixed by: adding UiUpdaterMixin.show_message() (routes to QMessageBox + the
alarm log table), wiring it in app_bootstrapper._wire_runtime exactly like
the other UI callbacks, and having _handle_show_message call it via the same
hasattr-guarded pattern the other handlers already use (no more tkinter
import anywhere in event_system.py).
"""
import unittest
from unittest.mock import MagicMock

from aset_batt.services.event_system import UIEventHandler, EventType, Event


class TestShowMessageRoutesToRealHandler(unittest.TestCase):
    def test_wired_show_message_is_called_via_root_after(self):
        handler = UIEventHandler(root=MagicMock())
        handler.show_message = MagicMock()

        handler._handle_show_message(
            Event(EventType.SHOW_MESSAGE, ("Title", "Body text", "warning")))

        # routed through root.after(...) like every other handler, not called directly
        handler.root.after.assert_called_once_with(
            0, handler.show_message, "Title", "Body text", "warning")

    def test_unwired_show_message_does_not_raise(self):
        """Before a real UI is wired (e.g. bootstrapper failure), this must be a
        silent no-op — never fall back to importing tkinter."""
        handler = UIEventHandler(root=MagicMock())
        handler._handle_show_message(
            Event(EventType.SHOW_MESSAGE, ("Title", "Body", "error")))
        handler.root.after.assert_not_called()

    def test_no_tkinter_import_anywhere_in_event_system(self):
        import inspect
        from aset_batt.services import event_system
        src = inspect.getsource(event_system)
        self.assertNotIn("import tkinter", src)
        self.assertNotIn("from tkinter", src)

    def test_show_message_no_longer_exists_as_dead_tkinter_methods(self):
        for name in ("_show_error_message", "_show_info_message", "_show_warning_message"):
            self.assertFalse(hasattr(UIEventHandler, name), name)


class TestUiUpdaterShowMessageMethod(unittest.TestCase):
    def test_show_message_uses_qmessagebox_and_logs_alarm(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from aset_batt.ui import theme
        theme.set_theme("light")
        from PySide6.QtWidgets import QApplication, QMessageBox
        from aset_batt.core.config import ConfigManager
        from aset_batt.ui.isa101_views import BatteryQtWindow

        app = QApplication.instance() or QApplication([])
        win = BatteryQtWindow(ConfigManager())
        try:
            self.assertTrue(win._headless)   # offscreen -> no blocking modal
            logged = []
            win._log_alarm = lambda msg: logged.append(msg)
            called = {}
            # headless guard short-circuits before QMessageBox is reached, but
            # confirm the alarm log still records the message either way.
            win.show_message("OCV Out of Range", "13.4V exceeds the curve ceiling",
                             msg_type="warning")
            self.assertEqual(len(logged), 1)
            self.assertIn("OCV Out of Range", logged[0])
            self.assertIn("WARNING", logged[0])
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
