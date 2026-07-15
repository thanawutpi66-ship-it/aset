"""Regression test: a rested OCV reading outside the calibrated curve's own
defined range must be flagged, not silently clamped to 0/100% with no signal.

Root cause: BatteryModel.get_soc_from_ocv() clamps an out-of-range voltage to
0/100% via plain np.interp boundary behaviour. For lead-acid this hid a real
issue found analysing a live IEC 61960 test: its 300 s OCV-settle rest read
13.15 V, 260 mV above the chemistry's own calibrated 100% point (12.888 V for a
6S pack) -- the classic *surface charge* symptom (a temporary post-charge
voltage elevation that takes hours to relax, far longer than a settle window
sized for coulomb-counting drift). The reading looked "settled" (flat within
the ΔV/Δt window) while not actually being at true rest, and silently caused
the test to skip its CHARGE phase (SoC read as 100%).
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.services.event_system import EventType
from aset_batt.app.auto_controller import AutoController
from aset_batt.hardware.mock_hardware import MockHardwareController


class TestOcvOutOfRangeMv(unittest.TestCase):
    def setUp(self):
        self.model = BatteryModel("LeadAcid", 2.0, 6, 1)

    def test_within_range_reads_zero(self):
        v = self.model.get_ocv_from_soc(50.0, 25.0)
        self.assertEqual(self.model.ocv_out_of_range_mv(v, 25.0), 0.0)

    def test_exactly_at_100pct_point_reads_zero_at_every_grid_temperature(self):
        # A naive single-table lookup (not blended the same way get_ocv_from_soc
        # is) picks the WRONG temperature table whenever temp lands exactly on a
        # grid point other than the first -- this would misreport a normal full
        # charge as "out of range" at every calibrated temperature except -10°C.
        for t in self.model.temp_range:
            v100 = self.model.get_ocv_from_soc(100.0, t)
            self.assertEqual(self.model.ocv_out_of_range_mv(v100, t), 0.0)

    def test_surface_charge_voltage_flagged_above_range(self):
        # The real case: 13.15V read where the curve's 100% point is 12.888V.
        oor = self.model.ocv_out_of_range_mv(13.15, 25.0)
        self.assertGreater(oor, 200.0)

    def test_below_range_flagged_negative(self):
        oor = self.model.ocv_out_of_range_mv(5.0, 25.0)
        self.assertLess(oor, 0.0)


class TestCalibrateFromOcvStableSurfacesOutOfRangeWarning(unittest.TestCase):
    def _make_controller(self, ocv_voltage: float):
        from aset_batt.core.config import ConfigManager
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        cfg.battery.nominal_voltage = 2.0
        cfg.battery.cells_series = 6
        model = BatteryModel(cfg.battery.battery_type, cfg.battery.nominal_voltage,
                             cfg.battery.cells_series, cfg.battery.cells_parallel)
        estimator = StateEstimator(cfg.battery.rated_capacity, model)
        hw = MockHardwareController()
        hw.is_connected = True
        hw.read_vi = MagicMock(return_value=(ocv_voltage, 0.0, 0.0))
        hw.read_measurements = MagicMock(return_value=(ocv_voltage, 0.0))
        hw.current_temp = 25.0
        data = DataHandler()
        ctrl = AutoController(None, hw, data, estimator, cfg)
        ctrl.event_handler = MagicMock()
        return ctrl

    @staticmethod
    def _cancel_after_first_call():
        """True on the very first call (let one reading happen), False on every
        call after that (both the outer loop's own check and the inner
        interruptible-sleep loop's repeated checks) -- unlike a fixed-length
        side_effect list, this never raises StopIteration no matter how many
        times it's polled."""
        calls = {"n": 0}
        def cancel():
            calls["n"] += 1
            return calls["n"] <= 1
        return cancel

    def test_surface_charge_voltage_posts_a_warning_event(self):
        ctrl = self._make_controller(ocv_voltage=13.15)
        cancel = self._cancel_after_first_call()
        with patch("time.sleep"):
            ctrl.calibrate_from_ocv_stable(cancel_check=cancel)
        events = [c.args for c in ctrl.event_handler.post_event.call_args_list]
        self.assertTrue(any(a[0] == EventType.SHOW_MESSAGE and "OCV" in a[1][0]
                            for a in events))

    def test_in_range_voltage_posts_no_warning_event(self):
        ctrl = self._make_controller(ocv_voltage=12.3)   # well within range
        cancel = self._cancel_after_first_call()
        with patch("time.sleep"):
            ctrl.calibrate_from_ocv_stable(cancel_check=cancel)
        events = [c.args for c in ctrl.event_handler.post_event.call_args_list]
        self.assertFalse(any(a[0] == EventType.SHOW_MESSAGE and "OCV Out of Range" in a[1][0]
                             for a in events))

    def test_below_range_never_attempts_a_bleed_off(self):
        """Safety: a BELOW-range reading means the pack already reads as
        near-empty -- pulling more current to "fix" that would be actively
        unsafe, not helpful. Only ABOVE-range (surface charge) may bleed off."""
        ctrl = self._make_controller(ocv_voltage=5.0)   # below the 0% point
        ctrl.hw.set_load = MagicMock(return_value=True)
        cancel = self._cancel_after_first_call()
        with patch("time.sleep"):
            ctrl.calibrate_from_ocv_stable(cancel_check=cancel)
        ctrl.hw.set_load.assert_not_called()

    def test_above_range_attempts_a_bleed_off(self):
        ctrl = self._make_controller(ocv_voltage=13.15)   # surface-charge case
        ctrl.hw.set_load = MagicMock(return_value=True)
        ctrl.hw.load_off = MagicMock()
        cancel = self._cancel_after_first_call()
        with patch("time.sleep"):
            ctrl.calibrate_from_ocv_stable(cancel_check=cancel)
        ctrl.hw.set_load.assert_called_once()
        ctrl.hw.load_off.assert_called_once()


class TestSurfaceChargeBleedOff(unittest.TestCase):
    def _make_controller(self):
        from aset_batt.core.config import ConfigManager
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        cfg.battery.nominal_voltage = 2.0
        cfg.battery.cells_series = 6
        model = BatteryModel(cfg.battery.battery_type, cfg.battery.nominal_voltage,
                             cfg.battery.cells_series, cfg.battery.cells_parallel)
        estimator = StateEstimator(cfg.battery.rated_capacity, model)
        hw = MockHardwareController()
        hw.is_connected = True
        data = DataHandler()
        ctrl = AutoController(None, hw, data, estimator, cfg)
        return ctrl, hw, cfg

    def test_stops_early_at_safety_floor(self):
        ctrl, hw, cfg = self._make_controller()
        safety_floor = cfg.battery.pack_min_voltage * ctrl._SURFACE_CHARGE_BLEED_SAFETY_MARGIN
        hw.set_load = MagicMock(return_value=True)
        hw.load_off = MagicMock()
        hw.read_measurements = MagicMock(return_value=(safety_floor - 0.01, 0.5))
        ok = ctrl._bleed_off_surface_charge(cancel_check=lambda: True)
        self.assertTrue(ok)
        hw.set_load.assert_called_once()
        hw.load_off.assert_called_once()

    def test_load_always_released_even_if_read_raises(self):
        ctrl, hw, cfg = self._make_controller()
        hw.set_load = MagicMock(return_value=True)
        hw.load_off = MagicMock()
        hw.read_measurements = MagicMock(side_effect=Exception("boom"))
        ok = ctrl._bleed_off_surface_charge(cancel_check=lambda: True)
        self.assertFalse(ok)
        hw.load_off.assert_called_once()

    def test_bleed_current_is_c20_clamped_to_max_current(self):
        ctrl, hw, cfg = self._make_controller()
        hw.set_load = MagicMock(return_value=True)
        hw.load_off = MagicMock()
        # An immediate at-floor reading ends the loop after exactly one
        # set_load call, so its argument can be inspected directly.
        safety_floor = cfg.battery.pack_min_voltage * ctrl._SURFACE_CHARGE_BLEED_SAFETY_MARGIN
        hw.read_measurements = MagicMock(return_value=(safety_floor - 0.01, 0.0))
        ctrl._bleed_off_surface_charge(cancel_check=lambda: True)
        expected_i = min(max(0.05, cfg.battery.rated_capacity * 0.05), cfg.battery.max_current)
        hw.set_load.assert_called_once_with(True, expected_i)

    def test_cancelled_still_releases_load_and_returns_false(self):
        ctrl, hw, cfg = self._make_controller()
        hw.set_load = MagicMock(return_value=True)
        hw.load_off = MagicMock()
        ok = ctrl._bleed_off_surface_charge(cancel_check=lambda: False)
        self.assertFalse(ok)
        hw.load_off.assert_called_once()


if __name__ == "__main__":
    unittest.main()
