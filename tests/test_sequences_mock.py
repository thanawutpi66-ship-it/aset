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
@patch('aset_batt.ui.sequences.hppc._t.time')
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
@patch('aset_batt.ui.sequences.cycle_life._t.time')
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
@patch('aset_batt.ui.sequences.iec_capacity._t.time')
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

@patch.object(QEventLoop, 'exec')
@patch('aset_batt.ui.sequences.quick_scan._t.time')
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
