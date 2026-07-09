"""Tests for AutoController's profile-test methods (aset_batt/app/auto_controller.py)
— the code path wired to the UI's RUN button (btn_start_profile, see
aset_batt/ui/zones.py) that had zero direct coverage before this: every prior
test replaced it wholesale (e.g. `ctrl.start_charge = lambda...`) instead of
exercising it.

Calls thread targets directly (not via threading.Thread(...).start()), same
technique as tests/test_graph_feed_during_sequences.py. Uses the real
MockHardwareController + DataHandler + IEC61960Standard so the assertions
exercise real integration, not mocked-out plumbing.

Includes the B1 regression case: aborting a capacity test before the first
sample used to leave voltage_data/current_data/time_data empty, which reached
iec61960_standard.calculate_capacity()/battery_model.calculate_iec61960_capacity()
and raised ZeroDivisionError during the abort-results step (see
tests/test_iec_capacity_empty_data_guard.py for the calculator-level guard
tests) instead of returning a safe zeroed result.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.iec61960_standard import IEC61960Standard, TestType
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
    return ctrl, hw, data, cfg


class TestCapacityTestAbortBeforeFirstSample(unittest.TestCase):
    """B1 regression, at the real trigger path (not just the calculator)."""

    def test_no_samples_leaves_empty_lists_and_does_not_raise(self):
        ctrl, hw, data, cfg = _make_controller()
        ctrl.is_profile_running = False  # already stopped before the loop starts
        ctrl._ocv_reset_after_rest = MagicMock()  # skip the real 30s rest wait

        iec_standard = IEC61960Standard(
            battery_capacity_ah=cfg.battery.rated_capacity,
            battery_type=cfg.battery.battery_type,
            nominal_voltage=cfg.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("capacity_02c")
        test_data = {"profile": profile, "iec_standard": iec_standard}

        ctrl._run_capacity_test(profile, test_data)

        self.assertEqual(test_data["voltage_data"], [])
        self.assertEqual(test_data["current_data"], [])
        self.assertEqual(test_data["time_data"], [])
        ctrl._ocv_reset_after_rest.assert_called_once_with("discharge")

        # Closes the loop end-to-end: feeding this empty result into the real
        # results-calculation step must not raise either.
        results = ctrl._calculate_test_results(test_data)
        self.assertEqual(results["capacity_ah"], 0.0)
        self.assertEqual(results["average_voltage"], 0.0)


class TestCapacityTestHappyPath(unittest.TestCase):
    def test_discharge_loop_logs_samples_and_stops_at_cutoff(self):
        ctrl, hw, data, cfg = _make_controller()
        ctrl._ocv_reset_after_rest = MagicMock()

        # Three readings: two above the pack cutoff, one at/below it so the
        # loop's own end-condition (not is_profile_running flipping False)
        # is what terminates it. A leading and a trailing reading are consumed
        # by the immediate post-edge samples taken right after set_load(True,
        # ...) (before the loop starts) and set_load(False) (after it ends).
        cutoff = cfg.battery.pack_min_voltage
        readings = [(cutoff + 0.6, 1.0), (cutoff + 0.5, 1.0), (cutoff + 0.2, 1.0),
                    (cutoff - 0.05, 1.0), (cutoff - 0.05, 0.0)]
        hw.read_measurements = MagicMock(side_effect=readings)
        ctrl.is_profile_running = True

        iec_standard = IEC61960Standard(
            battery_capacity_ah=cfg.battery.rated_capacity,
            battery_type=cfg.battery.battery_type,
            nominal_voltage=cfg.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("capacity_02c")
        test_data = {"profile": profile, "iec_standard": iec_standard}

        with patch("aset_batt.app.auto_controller.time.sleep"):
            ctrl._run_capacity_test(profile, test_data)

        self.assertEqual(len(test_data["voltage_data"]), 3)
        self.assertEqual(test_data["voltage_data"][-1], cutoff - 0.05)
        self.assertFalse(ctrl.safety_triggered)
        self.assertTrue(data.is_recording)
        ctrl._ocv_reset_after_rest.assert_called_once_with("discharge")
        data.stop_logging()

    def test_energy_density_test_delegates_to_capacity_test(self):
        ctrl, hw, data, cfg = _make_controller()
        ctrl._run_capacity_test = MagicMock()
        profile = MagicMock()
        test_data = {}

        ctrl._run_energy_density_test(profile, test_data)

        ctrl._run_capacity_test.assert_called_once_with(profile, test_data)


class TestCapacityTestSafetyBreak(unittest.TestCase):
    def test_overcurrent_triggers_safety_and_stops_loop(self):
        ctrl, hw, data, cfg = _make_controller()
        ctrl._ocv_reset_after_rest = MagicMock()
        hw.load_off = MagicMock()
        hw.psu_off = MagicMock()

        over_current = cfg.system.safety_limits["max_current"] + 1.0
        safe_v = cfg.battery.pack_min_voltage + 1.0
        # A leading safe reading is consumed by the immediate post-edge sample
        # taken right after set_load(True, ...), before the loop starts. The
        # loop's own first (and only) read already violates the current limit
        # and must break immediately, before ever reaching a second read — a
        # trailing safe reading is then consumed by the post-break
        # set_load(False) immediate sample.
        hw.read_measurements = MagicMock(side_effect=[
            (safe_v, 1.0), (safe_v, over_current), (safe_v, 0.0)])
        ctrl.is_profile_running = True

        iec_standard = IEC61960Standard(
            battery_capacity_ah=cfg.battery.rated_capacity,
            battery_type=cfg.battery.battery_type,
            nominal_voltage=cfg.battery.pack_nominal_voltage,
        )
        profile = iec_standard.get_test_profile("capacity_02c")
        test_data = {"profile": profile, "iec_standard": iec_standard}

        with patch("aset_batt.app.auto_controller.time.sleep"):
            ctrl._run_capacity_test(profile, test_data)

        self.assertTrue(ctrl.safety_triggered)
        self.assertEqual(len(test_data["voltage_data"]), 1)
        # Called twice in practice: once by _emergency_shutdown() (triggered
        # synchronously from check_safety_limits) and once more from the
        # loop's own post-break `self.hw.set_load(False)` cleanup — both
        # calls are real, intentional belt-and-suspenders behavior.
        hw.load_off.assert_called()
        hw.psu_off.assert_called_once()


if __name__ == "__main__":
    unittest.main()
