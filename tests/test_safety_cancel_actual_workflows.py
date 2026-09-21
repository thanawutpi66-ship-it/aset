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
    w.hw = MagicMock(is_connected=True, current_temp=25.0)
    w.hw.read_vi.return_value = (12.4, 0.0, 25.0)
    w.controller = MagicMock(safety_triggered=False, monitor_running=False,
                             _monitor_thread=None, _live_readback_thread=None)
    w.controller.config = w.config
    w.controller.estimator.soc = 50.0
    return w


def _controlled_worker(entered, release, phase):
    entered.set()
    release.wait()


def _run_sequence_cancel(start_name, worker_name, kind):
    w = _window(); entered = threading.Event(); release = threading.Event()
    try:
        target = lambda *args: _controlled_worker(entered, release, kind)
        setattr(w, worker_name, target)
        with patch.object(w, "_show_pretest_dialog", return_value=True):
            getattr(w, start_name)()
        assert entered.wait(2000), f"{kind} owner did not enter"
        lease = w._seq_lease
        assert lease is not None
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            w._on_seq_cancel()
        assert lease.cancel.is_set()
        assert not w._seq_running.is_set()
        w.hw.load_off.assert_called_once(); w.hw.psu_off.assert_called_once()
        assert w.operation_state.active is lease
        release.set(); w._seq_thread.join(2000)
        w._on_operation_worker_exited(lease.run_id)
        assert w.operation_state.active is None
    finally:
        release.set()
        w.close()


def test_iec_capacity_active_discharge_cancel():
    _run_sequence_cancel("_on_auto_sequence", "_auto_sequence_thread", "IEC active discharge")


def test_hppc_active_pulse_cancel():
    _run_sequence_cancel("_on_hppc_sequence", "_hppc_seq_thread", "HPPC active pulse")


def test_cycle_life_active_charge_cancel():
    _run_sequence_cancel("_on_cycle_life", "_cycle_life_thread", "Cycle charge")


def test_cycle_life_active_discharge_cancel():
    _run_sequence_cancel("_on_cycle_life", "_cycle_life_thread", "Cycle discharge")


def _run_eta_cancel(phase):
    w = _window(); entered = threading.Event(); release = threading.Event()
    try:
        w._char_eta_thread = lambda: _controlled_worker(entered, release, phase)
        w._on_char_eta_start = MagicMock()
        lease = w.operation_state.claim("characterize:eta"); w.operation_state.running(lease)
        w._char_leases["eta"] = lease
        w._char_running["eta"] = threading.Event(); w._char_running["eta"].set()
        w._char_threads["eta"] = threading.Thread(target=lambda: _controlled_worker(entered, release, phase))
        lease.thread = w._char_threads["eta"]
        w._operation_leases[lease.run_id] = lease
        w._operation_threads[lease.run_id] = lease.thread
        w._char_threads["eta"].start()
        assert entered.wait(2000)
        w._on_char_eta_cancel()
        assert lease.cancel.is_set(); assert not w._char_running["eta"].is_set()
        assert w.controller.stop_charge.call_count >= 1
        release.set(); w._char_threads["eta"].join(2000)
        w._on_operation_worker_exited(lease.run_id)
        assert w.operation_state.active is None
    finally:
        release.set(); w.close()


def test_eta_charge_cc_cancel():
    _run_eta_cancel("CHARGE_CC")


def test_eta_reference_discharge_cancel():
    # The η cancel owner is identical after the real worker transitions from
    # CHARGE_CC/charge completion into REFERENCE_DISCHARGE; preserve that phase
    # marker while exercising the actual UI cancellation handler.
    _run_eta_cancel("REFERENCE_DISCHARGE")
