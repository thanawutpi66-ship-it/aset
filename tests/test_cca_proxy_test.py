"""Regression tests for the CCA-proxy test added to the CHARACTERIZE tab.

Real CCA (Cold Cranking Amps) current (e.g. 95A for a small motorcycle AGM
battery) far exceeds what this rig's wiring/breaker is rated for — the test
current is deliberately clamped to config.battery.max_current, and the result
is explicitly NOT a certified CCA rating (no 0°C control either), just a
comparative cranking-sag health check. Pass/fail threshold is 1.2V/cell
(SAE-style generalisation of the 7.2V/6-cell convention for 12V batteries).
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.hardware.mock_hardware import MockHardwareController
import threading

_app = QApplication.instance() or QApplication([])


def _make_bound_window():
    cfg = ConfigManager()
    cfg.battery.product_name = "YTZ6V (12V 5.3Ah VRLA)"   # cca_a=95.0 in battery_profiles.json
    hw = MockHardwareController()
    # _char_check_safety (safety audit ก.ค. 2026) aborts CHARACTERIZE tests when the
    # temperature link is down (OTP would be blind) — connect the mock ESP32 the same
    # way the real app flow does before any test can start.
    hw.connect_esp32("MOCK")
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    win.cb_product.setCurrentText(cfg.battery.product_name)
    return win, ctrl, hw, data


class TestCcaCurrentClamping(unittest.TestCase):
    def test_current_is_clamped_to_max_current(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            self.assertLess(ctrl.config.battery.max_current, 95.0,
                            "test assumes the rig's configured max_current is well under CCA rating")
            ctrl.start_charge = lambda *a, **k: None
            ctrl.is_charging = False

            ev = threading.Event()
            ev.set()
            win._char_running["cca"] = ev
            # Let the 5-min post-charge rest complete instantly, then run the pulse
            # loop for exactly one real body iteration by fast-forwarding
            # perf_counter past the 30s window on the loop's second condition check
            # (never via _char_sleep returning False, which the real code treats as
            # cancellation, not "pulse finished").
            win._char_sleep = MagicMock(return_value=True)

            load_calls = []
            orig_set_load = hw.set_load
            def _spy_set_load(state, current="0"):
                load_calls.append((state, current))
                orig_set_load(state, current)
            hw.set_load = _spy_set_load

            with patch("time.perf_counter", side_effect=[0.0, 5.0, 5.0] + [100.0] * 20):
                win._char_cca_thread()

            on_calls = [c for c in load_calls if c[0]]
            self.assertEqual(len(on_calls), 1)
            i_used = float(on_calls[0][1])
            self.assertAlmostEqual(i_used, ctrl.config.battery.max_current)
            result = win._char_results.get("cca")
            self.assertIsNotNone(result)
            self.assertTrue(result["cca_clamped"])
            self.assertEqual(result["cca_rated_a"], 95.0)
        finally:
            win.close()


class TestCcaPassFail(unittest.TestCase):
    def _run_with_forced_voltage(self, win, ctrl, hw, voltage):
        ctrl.start_charge = lambda *a, **k: None
        ctrl.is_charging = False
        hw.read_measurements = MagicMock(return_value=(voltage, ctrl.config.battery.max_current))

        ev = threading.Event()
        ev.set()
        win._char_running["cca"] = ev
        # Forced voltage is constant every sample, so the pulse loop's own
        # UVP-cutoff debounce (5 consecutive at/below-pack_min samples — see the
        # comment in _char_cca_thread) now needs 5 real iterations to fire before
        # the loop ends naturally, rather than the old single-sample break. Never
        # cancel via _char_sleep — cancelling mid-debounce would clear ev before
        # the confirm count is reached, and the post-loop "if not ev.is_set():
        # return" would then discard the result before it's ever stored.
        win._char_sleep = MagicMock(return_value=True)

        win._char_cca_thread()
        return win._char_results.get("cca")

    def test_passes_when_voltage_stays_above_floor(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            cells = ctrl.config.battery.cells_series
            result = self._run_with_forced_voltage(win, ctrl, hw, voltage=1.5 * cells)
            self.assertIsNotNone(result)
            self.assertTrue(result["cca_pass"])
        finally:
            win.close()

    def test_fails_when_voltage_sags_below_floor(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            cells = ctrl.config.battery.cells_series
            result = self._run_with_forced_voltage(win, ctrl, hw, voltage=1.0 * cells)
            self.assertIsNotNone(result)
            self.assertFalse(result["cca_pass"])
        finally:
            win.close()


class TestCcaSkipsWhenNoRating(unittest.TestCase):
    def test_start_refuses_when_cca_a_is_zero(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            win.controller = ctrl
            win.hw.is_connected = True
            with patch(
                    "aset_batt.ui.characterize.battery_profiles.get_product",
                    return_value=None):
                win._char_cca_thread = MagicMock()
                win._on_char_cca_start()
                win._char_cca_thread.assert_not_called()
        finally:
            win.close()


class TestCcaFeedsGraphAndCsv(unittest.TestCase):
    def test_pulse_loop_feeds_update_display_and_csv(self):
        win, ctrl, hw, data = _make_bound_window()
        try:
            ctrl.start_charge = lambda *a, **k: None
            ctrl.is_charging = False

            calls = []
            orig_update_display = win.update_display
            def _spy(*a, **k):
                calls.append(a)
                orig_update_display(*a, **k)
            win.update_display = _spy

            ev = threading.Event()
            ev.set()
            win._char_running["cca"] = ev
            calls_n = {"n": 0}
            def _fake_sleep(ev_, seconds):
                calls_n["n"] += 1
                if calls_n["n"] == 1:
                    return True
                ev_.clear()
                return False
            win._char_sleep = _fake_sleep

            win._char_cca_thread()

            self.assertGreater(len(calls), 0)
            self.assertTrue(data.is_recording)
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
