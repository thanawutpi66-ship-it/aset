"""Tests for AutoController.calibrate_from_ocv() (the non-`_stable` variant,
aset_batt/app/auto_controller.py:162) — a plain one-shot OCV→SoC sync used
outside the sequence/HPPC settle-wait flow. Previously this was only ever
mocked out in other tests (e.g. tests/test_prepare_phase_rest_logging.py),
never exercised for real.
"""
import unittest
from unittest.mock import patch

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.services.exceptions import HardwareError
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
    return ctrl, hw, estimator, model


class TestCalibrateFromOcvNotConnected(unittest.TestCase):
    def test_raises_hardware_error_when_disconnected(self):
        ctrl, hw, estimator, model = _make_controller()
        hw.is_connected = False
        with self.assertRaises(HardwareError):
            ctrl.calibrate_from_ocv()


class TestCalibrateFromOcvSyncsEstimator(unittest.TestCase):
    """Uses pack-level voltages (get_soc_from_ocv divides by cells_series
    internally) matching whatever chemistry/series ConfigManager()'s default
    config.json actually specifies — not a hardcoded per-cell LiPO guess,
    since the real default turned out to be lead-acid 6S (~12-13V pack)."""

    def test_reads_hw_voltage_and_syncs_soc(self):
        ctrl, hw, estimator, model = _make_controller()
        hw._sim_v = model.get_ocv_from_soc(70.0, 25.0)  # already pack-level
        hw.current_temp = 25.0

        expected_soc = model.get_soc_from_ocv(hw._sim_v, hw.current_temp)
        def valid_anchor():
            estimator.sync_with_ocv(hw._sim_v, hw.current_temp)
            return expected_soc, hw._sim_v, "VALID_OCV"
        # The public helper must delegate acceptance to the full stable-OCV
        # validator. Keep this unit test fast; its rest/current/stability gates
        # are exercised directly in test_quickscan_coordinated_corrections.
        with patch.object(ctrl, "calibrate_from_ocv_stable",
                          side_effect=valid_anchor):
            soc = ctrl.calibrate_from_ocv()

        self.assertAlmostEqual(soc, expected_soc, places=3)
        self.assertAlmostEqual(estimator.soc, expected_soc, places=3)
        # SoC-tracking state must be reset, not just the reported value.
        self.assertEqual(estimator.soc_initial, expected_soc)
        self.assertEqual(estimator.ah_accumulated, 0.0)

    def test_different_ocv_reading_yields_different_soc(self):
        ctrl, hw, estimator, model = _make_controller()

        hw._sim_v = model.get_ocv_from_soc(30.0, 25.0)  # already pack-level
        soc_mid_expected = model.get_soc_from_ocv(hw._sim_v, hw.current_temp)
        with patch.object(ctrl, "calibrate_from_ocv_stable",
                          return_value=(soc_mid_expected, hw._sim_v, "VALID_OCV")):
            soc_mid = ctrl.calibrate_from_ocv()

        hw._sim_v = model.get_ocv_from_soc(90.0, 25.0)  # already pack-level
        soc_high_expected = model.get_soc_from_ocv(hw._sim_v, hw.current_temp)
        with patch.object(ctrl, "calibrate_from_ocv_stable",
                          return_value=(soc_high_expected, hw._sim_v, "VALID_OCV")):
            soc_high = ctrl.calibrate_from_ocv()

        self.assertGreater(soc_high, soc_mid)


if __name__ == "__main__":
    unittest.main()
