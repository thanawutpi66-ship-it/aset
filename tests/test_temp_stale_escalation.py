"""Industrial-grade audit follow-up G8.

temp_is_stale() used to only ever produce a one-shot UI warning, no matter how
long the ESP32 temperature sensor stayed dead — OTP (over-temperature)
protection could be blind for an entire multi-hour test with nothing but a log
line from minutes earlier. A momentary staleness blip deliberately still only
warns (a hard stop on that alone would be its own false-trip hazard — see the
original design comment preserved in both files), but SUSTAINED staleness
(60s+) now escalates to a real safety trip:
  - AutoController._monitor_loop -> _trigger_safety() (auto_controller.py)
  - sequences.py's AUTO/QuickScan/HPPC/CycleLife discharge loops ->
    _seq_check_temp_stale() now returns False and clears _seq_running,
    mirroring the existing _seq_check_otp() pattern right next to it.

Scope note: CHARACTERIZE-tab tests (characterize.py) also call
_seq_check_temp_stale() but track their own running-flag per test
(self._char_running["pk"/"eta"/"gitt"/"cca"], not self._seq_running) — wiring
auto-abort there needs separate per-test-type plumbing and was left out of
this pass; they still get the one-shot warning exactly as before (backward
compatible — they ignore the new return value).
"""
import os
import time
import unittest
from unittest.mock import MagicMock

from aset_batt.app.auto_controller import AutoController


# ---------------------------------------------------------------------------
# AutoController._monitor_loop (no Qt) — same fake style as
# test_bugfixes_hppc_cycle.py
# ---------------------------------------------------------------------------
class _FakeEventHandler:
    def __init__(self):
        self.events = []

    def post_event(self, etype, data):
        self.events.append((etype, data))


class _FakeEstimator:
    def update(self, v, i, dt, temp):
        return {"soc": 50.0, "rin": 0.03, "soh": 100.0}


class _FakeData:
    def log_row(self, *args, **kwargs):
        pass


class _FakeSystemConfig:
    safety_limits = {
        "max_temperature": 60.0, "min_temperature": -10.0,
        "max_current": 30.0, "max_voltage": 15.0, "min_voltage": 10.0,
    }


class _FakeConfig:
    def __init__(self):
        self.system = _FakeSystemConfig()


class _FakeHW:
    def __init__(self, read_vi_fn, stale_at_trip_threshold=False):
        self.is_connected = True
        self.current_temp = 25.0
        self._psu_output_on = False
        self.read_vi = read_vi_fn
        self._stale_at_trip = stale_at_trip_threshold

    def load_off(self):
        pass

    def psu_off(self):
        pass

    def temp_is_stale(self, max_age_s=10.0):
        # Only "stale" when queried with the SUSTAINED-staleness (trip) threshold —
        # simulates a sensor that's been dead a long time, not a momentary blip.
        if self._stale_at_trip and max_age_s == AutoController._TEMP_STALE_TRIP_S:
            return True
        return False


def _controller(read_vi_fn, stale_at_trip_threshold=False):
    c = AutoController(root=None, hw=_FakeHW(read_vi_fn, stale_at_trip_threshold),
                       data=_FakeData(), estimator=_FakeEstimator(), config=_FakeConfig())
    c.event_handler = _FakeEventHandler()
    c.monitor_running = True
    c._start_time = time.time()
    return c


class TestMonitorLoopEscalatesSustainedStaleness(unittest.TestCase):
    def test_sustained_stale_triggers_safety_stop(self):
        def read_vi():
            return (12.0, 0.0, 0.0)

        controller = _controller(read_vi, stale_at_trip_threshold=True)
        controller._monitor_loop()

        # Matches the established pattern of the sibling max_temperature/
        # max_current _trigger_safety() checks right next to this one in
        # _monitor_loop — they break the loop via safety_triggered, not by
        # resetting monitor_running themselves.
        self.assertTrue(controller.safety_triggered)

    def test_non_stale_sensor_never_trips_on_temperature_alone(self):
        calls = {"n": 0}

        def read_vi():
            calls["n"] += 1
            if calls["n"] >= 3:
                controller.monitor_running = False
            return (12.0, 0.0, 0.0)

        controller = _controller(read_vi, stale_at_trip_threshold=False)
        controller._monitor_loop()

        self.assertFalse(controller.safety_triggered)


# ---------------------------------------------------------------------------
# sequences.py's _seq_check_temp_stale (needs the real Qt mixin wiring)
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.hardware.mock_hardware import MockHardwareController

_app = QApplication.instance() or QApplication([])


def _make_bound_window():
    cfg = ConfigManager()
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    return win, ctrl, hw


class TestSeqCheckTempStaleEscalation(unittest.TestCase):
    def test_brief_staleness_only_warns_returns_true(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            hw.temp_is_stale = MagicMock(side_effect=lambda max_age_s=10.0: max_age_s == 10.0)
            alarms = []
            win.sig_alarm.connect(alarms.append)

            result = win._seq_check_temp_stale()

            self.assertTrue(result)
            self.assertTrue(win._seq_running.is_set())   # not aborted
            self.assertTrue(any("stale" in a.lower() for a in alarms))
        finally:
            win.close()

    def test_sustained_staleness_aborts_the_sequence(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            hw.temp_is_stale = MagicMock(return_value=True)   # stale at ANY threshold
            alarms = []
            win.sig_alarm.connect(alarms.append)

            result = win._seq_check_temp_stale()

            self.assertFalse(result)
            self.assertFalse(win._seq_running.is_set())   # sequence aborted
            self.assertTrue(any("safety" in a.lower() for a in alarms))
        finally:
            win.close()

    def test_healthy_sensor_returns_true_and_no_alarm(self):
        win, ctrl, hw = _make_bound_window()
        try:
            win._seq_running.set()
            hw.temp_is_stale = MagicMock(return_value=False)
            alarms = []
            win.sig_alarm.connect(alarms.append)

            result = win._seq_check_temp_stale()

            self.assertTrue(result)
            self.assertEqual(alarms, [])
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
