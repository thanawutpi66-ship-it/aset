"""G9: two safety-visibility gaps found from a real hardware test where
simulating over-temperature during a running test produced no visible/hard
stop:

1. calibrate_from_ocv_stable() (used by every sequence's PREPARE phase, and
   HPPC Full Sequence's post-charge PHASE 2 rest) waits up to 15 minutes with
   NO temperature check at all -- neither AutoController._monitor_loop's inline
   OTP check (the monitor loop is stopped during a sequence) nor the
   sequence-level _seq_check_otp() (only called in phases AFTER this wait
   returns) are watching while this function blocks.

2. Even where OTP/UVP *was* already checked during a sequence
   (_seq_check_otp, _seq_check_temp_stale, _char_check_safety, the two HPPC
   under-voltage trips), the only operator feedback was a small alarm-log
   line -- nothing like the big red "ESTOP" banner + blocking dialog a real
   E-STOP press produces, so a trip could read as "nothing happened".

Both are fixed by routing every one of these trips through the SAME
AutoController._trigger_safety() path a live E-STOP uses.
"""
import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.hardware.mock_hardware import MockHardwareController
from aset_batt.services.exceptions import SafetyError


def _make_controller(otp_limit=60.0):
    cfg = ConfigManager()
    if cfg.system.safety_limits is None:
        cfg.system.safety_limits = {}
    cfg.system.safety_limits["max_temperature"] = otp_limit
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    return ctrl, hw


class TestCalibrateFromOcvStableOtpGuard(unittest.TestCase):
    """The settle-wait itself must abort on over-temperature -- not just warn
    once it happens to return."""

    def test_raises_safety_error_when_temp_exceeds_limit_at_entry(self):
        ctrl, hw = _make_controller(otp_limit=60.0)
        hw.current_temp = 75.0   # already over-temp before the wait even starts
        with self.assertRaises(SafetyError):
            ctrl.calibrate_from_ocv_stable(cancel_check=lambda: True)

    def test_calls_trigger_safety_not_just_a_silent_raise(self):
        """The exception alone doesn't reach the operator's screen -- confirm
        the same _trigger_safety() path a live E-STOP uses actually fires."""
        ctrl, hw = _make_controller(otp_limit=60.0)
        hw.current_temp = 75.0
        ctrl._trigger_safety = MagicMock(wraps=ctrl._trigger_safety)
        with self.assertRaises(SafetyError):
            ctrl.calibrate_from_ocv_stable(cancel_check=lambda: True)
        ctrl._trigger_safety.assert_called_once()
        self.assertIn("OTP", ctrl._trigger_safety.call_args[0][0])

    def test_stays_safe_and_does_not_false_trip_below_the_limit(self):
        """Sanity check: the new guard must not false-trip a normal, in-range
        temperature -- run just a couple of poll iterations (not a real
        multi-minute settle-wait) and confirm no SafetyError is raised."""
        ctrl, hw = _make_controller(otp_limit=60.0)
        hw.current_temp = 25.0
        calls = {"n": 0}

        def _cancel_after_two():
            calls["n"] += 1
            return calls["n"] <= 2   # let it poll twice, then stop like a cancel

        try:
            ctrl.calibrate_from_ocv_stable(cancel_check=_cancel_after_two)
        except SafetyError:
            self.fail("must not raise SafetyError when temperature is within limit")

    def test_emergency_shutdown_cuts_hardware_on_trip(self):
        ctrl, hw = _make_controller(otp_limit=60.0)
        hw.current_temp = 90.0
        hw.load_off = MagicMock(wraps=hw.load_off)
        hw.psu_off = MagicMock(wraps=hw.psu_off)
        with self.assertRaises(SafetyError):
            ctrl.calibrate_from_ocv_stable(cancel_check=lambda: True)
        hw.load_off.assert_called()
        hw.psu_off.assert_called()


class TestSequenceOtpTripCallsTriggerSafety(unittest.TestCase):
    """_seq_check_otp()/_seq_check_temp_stale() must ALSO fire the shared
    big-banner path, not just clear their own local flag + log a line."""

    def _win(self, ctrl):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from aset_batt.ui import theme
        theme.set_theme("light")
        from PySide6.QtWidgets import QApplication
        from aset_batt.ui.isa101_views import BatteryQtWindow
        QApplication.instance() or QApplication([])
        win = BatteryQtWindow(ctrl.config)
        win.bind_controller(ctrl)
        ctrl.set_ui(win)
        return win

    def test_seq_check_otp_triggers_safety_banner(self):
        ctrl, hw = _make_controller(otp_limit=60.0)
        win = self._win(ctrl)
        try:
            ctrl._trigger_safety = MagicMock(wraps=ctrl._trigger_safety)
            win._seq_running.set()
            ok = win._seq_check_otp(75.0)
            self.assertFalse(ok)
            ctrl._trigger_safety.assert_called_once()
        finally:
            win.close()

    def test_temp_within_limit_does_not_trigger(self):
        ctrl, hw = _make_controller(otp_limit=60.0)
        win = self._win(ctrl)
        try:
            ctrl._trigger_safety = MagicMock(wraps=ctrl._trigger_safety)
            win._seq_running.set()
            ok = win._seq_check_otp(45.0)
            self.assertTrue(ok)
            ctrl._trigger_safety.assert_not_called()
        finally:
            win.close()


class TestCharacterizeOtpTripCallsTriggerSafety(unittest.TestCase):
    def _win(self, ctrl):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from aset_batt.ui import theme
        theme.set_theme("light")
        from PySide6.QtWidgets import QApplication
        from aset_batt.ui.isa101_views import BatteryQtWindow
        QApplication.instance() or QApplication([])
        win = BatteryQtWindow(ctrl.config)
        win.bind_controller(ctrl)
        ctrl.set_ui(win)
        return win

    def test_char_check_safety_triggers_banner_on_otp(self):
        import threading
        ctrl, hw = _make_controller(otp_limit=60.0)
        win = self._win(ctrl)
        try:
            ctrl._trigger_safety = MagicMock(wraps=ctrl._trigger_safety)
            ev = threading.Event()
            ev.set()
            ok = win._char_check_safety(ev, 75.0)
            self.assertFalse(ok)
            self.assertFalse(ev.is_set())
            ctrl._trigger_safety.assert_called_once()
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
