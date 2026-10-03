"""OCV readings outside the model curve map to the nearest SoC boundary."""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.app.auto_controller import AutoController
from aset_batt.core.config import ConfigManager
from aset_batt.storage.data_utils import DataHandler


class TestOcvSocClamping(unittest.TestCase):
    def setUp(self):
        self.model = BatteryModel("LeadAcid", 2.0, 6, 1)

    def test_above_curve_maps_to_full_soc(self):
        self.assertEqual(self.model.get_soc_from_ocv(13.15, 25.0), 100.0)

    def test_below_curve_maps_to_empty_soc(self):
        self.assertEqual(self.model.get_soc_from_ocv(5.0, 25.0), 0.0)

    def test_curve_values_keep_their_normal_mapping(self):
        voltage = self.model.get_ocv_from_soc(55.0, 25.0)
        self.assertAlmostEqual(self.model.get_soc_from_ocv(voltage, 25.0), 55.0)

    def test_full_charge_anchor_clamps_and_resets_estimator(self):
        estimator = StateEstimator(2.0, self.model)
        estimator.set_soc_anchor(120.0, start_settle_window=True)
        self.assertEqual(estimator.soc, 100.0)
        self.assertTrue(estimator.soc_is_initialized)

    def test_stable_above_and_below_curve_are_valid_anchors_without_load(self):
        for voltage, expected in ((13.15, 100.0), (5.0, 0.0)):
            with self.subTest(voltage=voltage):
                cfg = ConfigManager()
                cfg.battery.battery_type = "LeadAcid"
                cfg.battery.nominal_voltage = 2.0
                cfg.battery.cells_series = 6
                model = BatteryModel("LeadAcid", 2.0, 6, 1)
                estimator = StateEstimator(cfg.battery.rated_capacity, model)
                hw = MagicMock()
                hw.is_connected = True
                hw.read_vi.return_value = (voltage, 0.0, 0.0)
                hw.current_temp = 25.0
                hw.last_voltage_source = "eload"
                hw._psu_output_on = False
                hw.temp_is_stale.return_value = False
                ctrl = AutoController(None, hw, DataHandler(), estimator, cfg)
                ctrl.event_handler = MagicMock()
                tick = {"value": 0.0}

                def advance(seconds):
                    tick["value"] += seconds

                with patch("aset_batt.app.auto_controller.time.perf_counter",
                           side_effect=lambda: tick["value"]), \
                     patch("aset_batt.app.auto_controller.time.sleep",
                           side_effect=advance):
                    soc, measured, status = ctrl.calibrate_from_ocv_stable(
                        min_rest_override=0.2, max_rest_override=2.0,
                        interval_override=0.2, window_override=2.0,
                        spread_override=0.001)

                self.assertEqual(measured, voltage)
                self.assertEqual(status, "VALID_OCV")
                self.assertEqual(soc, expected)
                hw.set_load.assert_not_called()
                ctrl.event_handler.post_event.assert_not_called()

    def test_preserved_full_charge_anchor_survives_post_rest_ocv(self):
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        cfg.battery.nominal_voltage = 2.0
        cfg.battery.cells_series = 6
        estimator = StateEstimator(cfg.battery.rated_capacity, self.model)
        estimator.set_soc_anchor(100.0)
        hw = MagicMock()
        hw.is_connected = True
        hw.read_vi.return_value = (13.15, 0.0, 0.0)
        hw.current_temp = 25.0
        hw.last_voltage_source = "eload"
        hw._psu_output_on = False
        hw.temp_is_stale.return_value = False
        ctrl = AutoController(None, hw, DataHandler(), estimator, cfg)
        tick = {"value": 0.0}

        def advance(seconds):
            tick["value"] += seconds

        with patch("aset_batt.app.auto_controller.time.perf_counter",
                   side_effect=lambda: tick["value"]), \
             patch("aset_batt.app.auto_controller.time.sleep", side_effect=advance):
            soc, _voltage, status = ctrl.calibrate_from_ocv_stable(
                min_rest_override=0.2, max_rest_override=2.0,
                interval_override=0.2, window_override=2.0,
                spread_override=0.001, preserve_soc=True)

        self.assertEqual(status, "VALID_OCV")
        self.assertEqual(soc, 100.0)
        self.assertEqual(estimator.soc, 100.0)
        hw.set_load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
