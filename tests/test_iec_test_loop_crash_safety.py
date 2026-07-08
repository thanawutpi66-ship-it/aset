"""Industrial-grade audit follow-up R7.

_run_iec61960_test()'s except-Exception handler used to only log the failure
and post a UI event — it never called _emergency_shutdown(), and none of the
_run_*_test() loops it dispatches to (_run_capacity_test, _run_cycle_life_test,
etc.) have a try/except of their own. A genuinely unexpected fault (not a
checked safety-limit breach, which already goes through check_safety_limits ->
_trigger_safety) left the PSU/Load in whatever state they were in when the
exception fired, with zero attempt to cut power. Fixed by calling
_emergency_shutdown() in that handler unconditionally.
"""
import unittest
from unittest.mock import MagicMock

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.iec61960_standard import IEC61960Standard
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.hardware.mock_hardware import MockHardwareController


def _make_controller():
    cfg = ConfigManager()
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    return ctrl, hw


class TestUnhandledExceptionInTestLoopTriggersEmergencyShutdown(unittest.TestCase):
    def test_cycle_life_crash_cuts_load_and_psu(self):
        ctrl, hw = _make_controller()
        hw.load_off = MagicMock(wraps=hw.load_off)
        hw.psu_off = MagicMock(wraps=hw.psu_off)

        iec_standard = IEC61960Standard(
            battery_capacity_ah=ctrl.config.battery.rated_capacity,
            battery_type=ctrl.config.battery.battery_type,
            nominal_voltage=ctrl.config.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("cycle_life_300")
        test_data = {'profile': profile, 'iec_standard': iec_standard, 'test_id': 'cycle_life_300'}

        ctrl._run_cycle_life_test = MagicMock(side_effect=RuntimeError("simulated fault mid-loop"))
        ctrl._run_iec61960_test(test_data)   # must not raise — caught internally

        hw.load_off.assert_called_once()
        hw.psu_off.assert_called_once()
        self.assertIn('error', test_data)
        self.assertFalse(ctrl.is_profile_running)

    def test_capacity_test_crash_also_cuts_power(self):
        """Same guard, different dispatch branch — proves the fix is in the
        shared handler, not special-cased to one test type."""
        ctrl, hw = _make_controller()
        hw.load_off = MagicMock(wraps=hw.load_off)
        hw.psu_off = MagicMock(wraps=hw.psu_off)

        iec_standard = IEC61960Standard(
            battery_capacity_ah=ctrl.config.battery.rated_capacity,
            battery_type=ctrl.config.battery.battery_type,
            nominal_voltage=ctrl.config.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("capacity_02c")
        test_data = {'profile': profile, 'iec_standard': iec_standard, 'test_id': 'capacity_02c'}

        ctrl._run_capacity_test = MagicMock(side_effect=RuntimeError("simulated SCPI timeout"))
        ctrl._run_iec61960_test(test_data)

        hw.load_off.assert_called_once()
        hw.psu_off.assert_called_once()

    def test_clean_completion_does_not_trigger_emergency_shutdown(self):
        """Guard against over-correction: a normal, successful test must NOT
        call the emergency-shutdown path (that would be a false trip on every
        healthy test)."""
        ctrl, hw = _make_controller()
        hw.load_off = MagicMock(wraps=hw.load_off)
        hw.psu_off = MagicMock(wraps=hw.psu_off)

        iec_standard = IEC61960Standard(
            battery_capacity_ah=ctrl.config.battery.rated_capacity,
            battery_type=ctrl.config.battery.battery_type,
            nominal_voltage=ctrl.config.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("capacity_02c")
        test_data = {'profile': profile, 'iec_standard': iec_standard, 'test_id': 'capacity_02c'}

        ctrl._run_capacity_test = MagicMock()   # succeeds silently
        ctrl._run_iec61960_test(test_data)

        hw.load_off.assert_not_called()
        hw.psu_off.assert_not_called()
        self.assertNotIn('error', test_data)


if __name__ == "__main__":
    unittest.main()
