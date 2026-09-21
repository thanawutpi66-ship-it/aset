import os
import threading
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


def _window():
    w = BatteryQtWindow(ConfigManager())
    w.hw = MagicMock()
    w.hw.is_connected = True
    w.controller = MagicMock()
    w.controller.safety_triggered = False
    w.controller.operation_state = w.operation_state
    return w


def test_real_sequence_cancel_handler_requests_owner_and_safe_off():
    w = _window()
    try:
        lease = w.operation_state.claim("quick")
        w.operation_state.running(lease)
        w._seq_lease = lease
        w._seq_running.set()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            w._on_seq_cancel()
        assert lease.cancel.is_set()
        assert not w._seq_running.is_set()
        w.hw.load_off.assert_called_once()
        w.hw.psu_off.assert_called_once()
        w.controller.stop_charge.assert_called_once()
        assert w.operation_state.active is lease
        w.operation_state.cleanup(lease)
        assert w.operation_state.release(lease)
    finally:
        w.close()

def test_real_characterization_cancel_handler_requests_owner_and_stop():
    w = _window()
    try:
        lease = w.operation_state.claim("characterize:pk")
        w.operation_state.running(lease)
        w._char_leases["pk"] = lease
        ev = threading.Event(); ev.set(); w._char_running["pk"] = ev
        w._char_hw_stop = MagicMock()
        w._on_char_pk_cancel()
        assert lease.cancel.is_set()
        assert not ev.is_set()
        w._char_hw_stop.assert_called_once()
        assert w.operation_state.active is lease
        w.operation_state.cleanup(lease)
        assert w.operation_state.release(lease)
    finally:
        w.close()
