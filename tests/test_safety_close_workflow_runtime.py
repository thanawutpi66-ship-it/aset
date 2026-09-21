import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEvent

from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.app.auto_controller import AutoController

_app = QApplication.instance() or QApplication([])


class _CloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False
    def accept(self): self.accepted = True
    def ignore(self): self.ignored = True


def test_real_close_event_idle_accepts_without_worker():
    w = BatteryQtWindow(ConfigManager())
    try:
        event = _CloseEvent()
        w.closeEvent(event)
        assert event.accepted
        assert w.operation_state.state.value == "SHUTTING_DOWN"
    finally:
        w._shutdown_services()


def test_real_close_event_active_owner_requests_shutdown_and_defers_close():
    w = BatteryQtWindow(ConfigManager())
    try:
        w.controller = MagicMock()
        w.controller._monitor_thread = None
        w.controller._live_readback_thread = None
        w.controller._charge_thread = None
        w._test_worker = MagicMock()
        w._test_thread = None
        lease = w.operation_state.claim("characterize:pk")
        w.operation_state.running(lease)
        w._char_leases["pk"] = lease
        event = _CloseEvent()
        w.closeEvent(event)
        assert event.ignored
        assert w.operation_state.state.value == "SHUTTING_DOWN"
        w._test_worker.stop.assert_called_once()
        w.controller.stop_charge.assert_called_once()
        w.controller.stop_monitor.assert_called_once()
        w.controller.stop_live_readback.assert_called()
    finally:
        w._close_force_after_timeout = True
        w.operation_state.active = None
        w._shutdown_services()


def test_close_cleanup_independent_output_failures():
    w = BatteryQtWindow(ConfigManager())
    try:
        controller = AutoController.__new__(AutoController)
        controller.hw = MagicMock()
        controller.hw.set_ssr.side_effect = OSError("ssr")
        controller.hw.load_off.side_effect = OSError("pel")
        controller.hw.psu_off.return_value = True
        w.controller = controller
        w.controller._emergency_shutdown()
        w.controller.hw.set_ssr.assert_called_once_with(False)
        w.controller.hw.load_off.assert_called_once()
        w.controller.hw.psu_off.assert_called_once()
    finally:
        w._shutdown_services()
