import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


def _window():
    win = BatteryQtWindow(ConfigManager())
    win.hw = MagicMock()
    win.hw.set_ssr.return_value = True
    win.controller = MagicMock()
    win._test_worker = MagicMock()
    return win


def test_estop_invalidates_delayed_progress_callback_and_outputs_cannot_reenable():
    win = _window()
    try:
        old_generation = win._run_generation
        win._on_estop()
        assert win._run_generation != old_generation
        assert win.operation_state.state.value == "ESTOP_LATCHED"
        win._update_vi_temp_labels = MagicMock()
        win._slot_display(12.0, 0.5, 50.0, 0.1, 25.0, 100.0, old_generation)
        win._update_vi_temp_labels.assert_not_called()
        assert not any(call.args and call.args[0] is True
                       for call in win.hw.set_ssr.call_args_list)
    finally:
        win.close()


def test_estop_drops_delayed_success_callback():
    win = _window()
    try:
        win._current_test_name = "Quick Scan"
        win._on_estop()
        win._play_test_complete_sound = MagicMock()
        win._slot_seq_done("Complete", "late success")
        win._play_test_complete_sound.assert_not_called()
        assert win.operation_state.state.value == "ESTOP_LATCHED"
    finally:
        win.close()


def test_estop_drops_delayed_error_callback_from_restarting_operation():
    win = _window()
    try:
        win._on_estop()
        win.sig_profile_status = MagicMock()
        win.sig_alarm = MagicMock()
        # A late worker error is allowed to be logged by the worker, but this
        # state must remain latched and no normal-running status may be emitted.
        win._slot_seq_done("Worker error", "late error")
        assert win.operation_state.state.value == "ESTOP_LATCHED"
        assert not any("DONE" in str(call) for call in win.sig_profile_status.call_args_list)
    finally:
        win.close()
