import pytest
from unittest.mock import MagicMock, patch
from aset_batt.ui.sequences.base import BaseSequenceMixin
from aset_batt.ui.sequences.hppc import HppcMixin
from aset_batt.ui.sequences.cycle_life import CycleLifeMixin
from aset_batt.ui.sequences.iec_capacity import IecCapacityMixin
from aset_batt.ui.sequences.quick_scan import QuickScanMixin

from PySide6.QtWidgets import QApplication
import sys

# Ensure QApplication exists
if not QApplication.instance():
    app = QApplication(sys.argv)
else:
    app = QApplication.instance()

class MockSequence(BaseSequenceMixin, HppcMixin, CycleLifeMixin, IecCapacityMixin, QuickScanMixin):
    def __init__(self):
        self.hw = MagicMock()
        self.hw.current_voltage = 12.0
        self.hw.current_current = 0.0
        self.hw.current_temp = 25.0
        
        self.config = MagicMock()
        
        self.profile = MagicMock()
        self.profile.chemistry = "LFP"
        self.profile.capacity_ah = 100.0
        self.profile.max_charge_v = 14.4
        self.profile.cutoff_v = 10.0
        self.profile.charge_current_a = 50.0
        self.profile.discharge_current_a = 50.0
        
        self.data_handler = MagicMock()
        self.estimator = MagicMock()
        
        self._stop_event = MagicMock()
        self._stop_event.is_set.side_effect = [False, False, True] # Run 2 steps then exit
        self._seq_running = MagicMock()
        self._seq_safety_reason = ""
        self.operation_state = MagicMock()
        
        # Signals
        self.log_message = MagicMock()
        
        self.controller = MagicMock()
        self.controller.is_charging = False
        self.controller.estimator.update.return_value = {"soc": 50.0, "rin": 0.01, "soh": 100.0}
        self.controller.config.battery.rated_capacity = 100.0
        self.controller.config.battery.max_current = 50.0
        self.controller.config.battery.pack_min_voltage = 10.0
        self.controller.calibrate_from_ocv_stable.return_value = (50.0, 12.0, "settled")
        self.controller.calibrate_from_ocv.return_value = 50.0
        self.controller._auto_analyze.return_value = {"grade": "A"}
        self.estimator.update.return_value = {"soc": 50.0, "rin": 0.01, "soh": 100.0}
        self.test_progress_signal = MagicMock()
        self.set_test_button_state_signal = MagicMock()
        self.record_capacity_point_signal = MagicMock()
        self.run_analysis_signal = MagicMock()
        self.play_sound_signal = MagicMock()
        self.show_message_signal = MagicMock()
        self.save_temp_alarm_signal = MagicMock()
        
        self.sig_alarm = MagicMock()
        self.sig_seq_result = MagicMock()
        self.sig_seq_result = MagicMock()
        self.sig_seq_aborted = MagicMock()
        self.sig_seq_done = MagicMock()
        self.sig_phase_progress = MagicMock()
        self.sig_qs_workflow = MagicMock()
        self.sig_cycle_counter = MagicMock()
        self.sig_cycle_wf = MagicMock()
        self.sig_iec_workflow = MagicMock()
        self.sig_hppc_workflow = MagicMock()
        self.sig_cycle_workflow = MagicMock()
        self.sig_loading = MagicMock()
        self.sig_charge_status = MagicMock()
        self.sig_wf_status = MagicMock()
        self.sig_workflow = MagicMock()
        self.sig_button = MagicMock()
        
    def _seq_sleep(self, seconds, progress_callback=None):
        return True
    
    def _seq_kick_watchdog(self):
        pass
        
    def _seq_check_temp_stale(self):
        return True
        
    def _seq_check_otp(self, temp):
        return True
        
    def _hw_retry(self, func, *args, **kwargs):
        return func(*args, **kwargs)
        
    def emit_log(self, msg):
        pass
        
    def status(self, msg):
        pass
        
    def update_display(self, v, i, soc, rin, temp=25.0, soh=None):
        pass

from PySide6.QtCore import QEventLoop

@patch.object(QEventLoop, 'exec')
@patch('time.time')
def test_hppc_full_run(mock_time, mock_exec):
    mock_time.side_effect = range(0, 10000000, 1000)
    seq = MockSequence()
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    seq.hw.read_vi.return_value = (9.0, 10.0, 1)
    class SignalMock:
        def emit(self, *args, **kwargs): pass
    seq.test_progress_signal = SignalMock()
    seq.log_message = SignalMock()
    seq.run_analysis_signal = SignalMock()
    
    seq._stop_event.is_set.return_value = False
    seq._seq_running.is_set.return_value = True
    seq._hppc_seq_thread({
        "n_cyc": 10,
        "pulse_s": "30",
        "relax_s": "30",
        "crate": "1.0"
    })

@patch.object(QEventLoop, 'exec')
@patch('time.time')
def test_cycle_life_full_run(mock_time, mock_exec):
    mock_time.side_effect = range(0, 10000000, 1000)
    seq = MockSequence()
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    seq.hw.read_vi.return_value = (9.0, 10.0, 1)
    class SignalMock:
        def emit(self, *args, **kwargs): pass
    seq.test_progress_signal = SignalMock()
    seq.log_message = SignalMock()
    seq._stop_event.is_set.return_value = False
    seq._seq_running.is_set.return_value = True
    seq._cycle_life_thread({
        "n_cyc": 10,
        "rest_min": 1,
        "charge_crate": "1C",
        "dis_crate": "1C"
    })

@patch.object(QEventLoop, 'exec')
@patch('time.time')
def test_iec_capacity_full_run(mock_time, mock_exec):
    mock_time.side_effect = range(0, 10000000, 1000)
    seq = MockSequence()
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    seq.hw.read_vi.return_value = (9.0, 10.0, 1)
    class SignalMock:
        def emit(self, *args, **kwargs): pass
    seq.test_progress_signal = SignalMock()
    seq.log_message = SignalMock()
    seq._stop_event.is_set.return_value = False
    seq._seq_running.is_set.return_value = True
    seq._auto_sequence_thread({
        "skip_charge": False,
        "skip_rest": False,
        "soc_thresh": 95,
        "seq_crate": 0.5,
        "rest_min": 60,
        "test_crate": 0.5
    })


@pytest.mark.parametrize("soc", [99.0, 50.0])
def test_iec_validation_preset_forces_charge_at_high_and_low_soc(soc):
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (soc, 12.0, "settled")
    seq._seq_running.is_set.return_value = True
    seq.controller.is_charging = True
    seq.controller.start_charge.return_value = True
    seq.controller.last_charge_full_confirmed = True
    seq._seq_sleep = lambda _seconds: setattr(seq.controller, "is_charging", False) or True
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    opts = {"skip_charge": False, "skip_rest": True, "soc_thresh": 95,
            "seq_crate": "0.1C", "rest_min": 60, "test_crate": "0.1C",
            "validation_force_charge": True,
            "validation_campaign": {"enabled": True},
            "validation_preset": "c10-reference-v1"}

    seq._auto_sequence_thread(opts)

    seq.controller.start_charge.assert_called_once()
    assert seq.controller._ensure_logging.call_args.kwargs["protocol"]["charge_required"] is True
    assert seq.controller._ensure_logging.call_args.kwargs["protocol"]["charge_decision_source"] == "FORCED_BY_VALIDATION"


def test_iec_validation_protocol_metadata_starts_incomplete():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq._seq_running.is_set.return_value = True
    seq.controller.start_charge.return_value = False
    seq._auto_sequence_thread({"skip_charge": False, "skip_rest": False,
                              "soc_thresh": 95, "seq_crate": "0.1C",
                              "rest_min": 60, "test_crate": "0.1C",
                              "validation_force_charge": True,
                              "validation_campaign": {"enabled": True},
                              "validation_preset": "c10-reference-v1"})

    protocol = seq.controller._ensure_logging.call_args.kwargs["protocol"]
    assert protocol["charge_required"] is True
    assert protocol["charge_started"] is False
    assert protocol["charge_completed"] is False
    assert protocol["charge_decision_source"] == "FORCED_BY_VALIDATION"


def test_iec_routine_high_soc_preserves_auto_skip():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq._seq_running.is_set.return_value = True
    seq._auto_sequence_thread({"skip_charge": False, "skip_rest": True,
                              "soc_thresh": 95, "seq_crate": "0.1C",
                              "rest_min": 1, "test_crate": "0.1C"})
    seq.controller.start_charge.assert_not_called()


def test_iec_routine_explicit_skip_is_preserved():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (50.0, 12.0, "settled")
    seq._seq_running.is_set.return_value = True
    seq._auto_sequence_thread({"skip_charge": True, "skip_rest": True,
                              "soc_thresh": 95, "seq_crate": "0.1C",
                              "rest_min": 1, "test_crate": "0.1C"})
    seq.controller.start_charge.assert_not_called()


def test_iec_validation_safety_latch_refuses_charge_and_never_claims_completion():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq.controller.safety_triggered = True
    seq.controller.start_charge.return_value = False
    seq._seq_running.is_set.return_value = True
    seq._auto_sequence_thread({"skip_charge": False, "skip_rest": False,
                              "soc_thresh": 95, "seq_crate": "0.1C",
                              "rest_min": 60, "test_crate": "0.1C",
                              "validation_force_charge": True,
                              "validation_campaign": {"enabled": True},
                              "validation_preset": "c10-reference-v1"})

    assert seq.controller.start_charge.call_count == 1
    seq.controller.end_session.assert_called_once()
    assert seq.controller.end_session.call_args.args[0] in {"aborted", "safety_tripped"}


@pytest.mark.parametrize("full_confirmed, safety, error, expected", [
    (None, False, None, False),
    (False, False, None, False),
    (True, False, None, True),
    (True, True, None, False),
    (True, False, RuntimeError("charge error"), False),
])
def test_iec_charge_completion_requires_full_confirmation_and_no_fault(
        full_confirmed, safety, error, expected):
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq.controller.start_charge.return_value = True
    seq.controller.is_charging = True
    seq.controller.last_charge_full_confirmed = full_confirmed
    seq.controller.safety_triggered = safety
    seq.controller.last_charge_error = error
    seq._seq_running.is_set.return_value = True
    seq._seq_sleep = lambda _seconds: setattr(seq.controller, "is_charging", False) or True
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    opts = {"skip_charge": False, "skip_rest": True, "soc_thresh": 95,
            "seq_crate": "0.1C", "rest_min": 60, "test_crate": "0.1C",
            "validation_force_charge": True}

    with patch("aset_batt.storage.data_utils.update_session_metadata") as update:
        seq._auto_sequence_thread(opts)

    updates = [c.args[1] for c in update.call_args_list]
    assert any(u.get("charge_started") is True for u in updates)
    assert any(u.get("charge_completed") is expected for u in updates)


def test_iec_validation_start_refusal_keeps_started_and_completed_false():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq.controller.start_charge.return_value = False
    seq._seq_running.is_set.return_value = True

    with patch("aset_batt.storage.data_utils.update_session_metadata") as update:
        seq._auto_sequence_thread({
            "skip_charge": False, "skip_rest": False, "soc_thresh": 95,
            "seq_crate": "0.1C", "rest_min": 60, "test_crate": "0.1C",
            "validation_force_charge": True,
        })

    updates = [c.args[1] for c in update.call_args_list]
    assert any(u.get("charge_required") is True for u in updates)
    assert not any(u.get("charge_started") is True for u in updates)
    assert not any(u.get("charge_completed") is True for u in updates)
    seq.controller.end_session.assert_called_once()
    assert seq.controller.end_session.call_args.args[0] in {"aborted", "safety_tripped"}


def test_iec_missing_validation_snapshot_defaults_to_routine_mode():
    seq = MockSequence()
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.0, "settled")
    seq._seq_running.is_set.return_value = True
    seq._auto_sequence_thread({"skip_charge": False, "skip_rest": True,
                              "soc_thresh": 95, "seq_crate": "0.1C",
                              "rest_min": 1, "test_crate": "0.1C",
                              "validation_preset": "c10-reference-v1"})
    seq.controller.start_charge.assert_not_called()


def test_iec_posttest_verdict_uses_shared_temp_aware_checker():
    from aset_batt.ui.sequences import base
    seq = MockSequence()
    seq.controller.config.battery.battery_type = "LeadAcid"
    seq.controller.config.battery.cells_series = 6
    seq.controller.config.battery.rated_capacity = 5.3
    seq.controller.config.battery.pack_min_voltage = 10.5
    seq.controller.config.battery.product_name = ""
    seq.controller.calibrate_from_ocv_stable.return_value = (99.0, 12.6, "settled")
    seq.controller._auto_analyze.return_value = {
        "temperature_median_c": 25.0, "capacity_ah": 5.0, "grade": "A"
    }
    seq._seq_running.is_set.return_value = True
    seq._seq_check_load_trip = lambda: True
    seq.hw.read_measurements.return_value = (10.5, 0.5)
    opts = {"skip_charge": False, "skip_rest": True, "soc_thresh": 95,
            "seq_crate": "0.1C", "rest_min": 1, "test_crate": "0.1C"}

    with patch("aset_batt.ui.sequences.iec_capacity.en50342_capacity_conditions",
               wraps=base.en50342_capacity_conditions) as checker:
        seq._auto_sequence_thread(opts)

    checker.assert_called_once()
    assert checker.call_args.kwargs["temp_c"] == 25.0

@patch.object(QEventLoop, 'exec')
@patch('time.time')
def test_quick_scan_full_run(mock_time, mock_exec):
    mock_time.side_effect = range(0, 10000000, 1000)
    seq = MockSequence()
    seq.hw.read_measurements.return_value = (9.0, 10.0)
    seq.hw.read_vi.return_value = (9.0, 10.0, 1)
    class SignalMock:
        def emit(self, *args, **kwargs): pass
    seq.test_progress_signal = SignalMock()
    seq.log_message = SignalMock()
    seq._stop_event.is_set.return_value = False
    seq._seq_running.is_set.return_value = True
    seq._quick_scan_thread()
