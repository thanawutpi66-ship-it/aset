import threading
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QMessageBox

from aset_batt.app.operation_state import ApplicationState, OperationState
from aset_batt.ui.sequences.base import BaseSequenceMixin


class _SequenceHost(BaseSequenceMixin):
    def __init__(self):
        self.controller = MagicMock()
        self.hw = MagicMock()
        self._seq_running = threading.Event(); self._seq_running.set()
        self.operation_state = OperationState()
        self._seq_lease = self.operation_state.claim("quick")
        self.operation_state.running(self._seq_lease)
        self.sig_alarm = MagicMock()
        self.lbl_wf_status = MagicMock(); self.lbl_phase_banner = MagicMock()


def test_cancel_attempts_outputs_even_when_session_finalize_fails():
    host = _SequenceHost()
    host.controller.end_session.side_effect = OSError("storage unavailable")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        host._on_seq_cancel()
    assert not host._seq_running.is_set()
    host.hw.load_off.assert_called_once()
    host.hw.psu_off.assert_called_once()
    host.controller.stop_charge.assert_called_once()
    assert host.operation_state.state is ApplicationState.CANCELLING


def test_cancel_reason_precedes_generic_worker_release():
    state = OperationState()
    lease = state.claim("characterize:pk")
    state.running(lease)
    state.request_cancel(lease)
    state.cleanup(lease)
    state.release(lease)
    assert state.terminal_status == "CANCELLED"
    assert "cancel" in state.terminal_reason
    assert not state.release(lease)


def test_close_reason_precedes_late_completion():
    state = OperationState()
    lease = state.claim("hppc")
    state.running(lease)
    state.begin_shutdown()
    state.cleanup(lease)
    state.release(lease)
    assert state.state is ApplicationState.SHUTTING_DOWN
    assert state.terminal_status == "APPLICATION_CLOSE"
    assert state.terminal_reason == "application close"
