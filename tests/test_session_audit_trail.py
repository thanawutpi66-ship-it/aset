"""Industrial-grade audit follow-up R3.

Session results used to carry NO audit trail at all: no operator identity, no
software version, no snapshot of the calibration (harness_resistance_ohm,
product measured_params) in effect at test time. Since config.json/
battery_profiles.json aren't versioned, a later recalibration made an old
result unreconstructable even from the archived CSV alone. Fixed:
write_session_metadata() writes a <csv_path>.meta.json sidecar at
start_logging() time (captured even on a crash mid-test), wired into all 4
start_logging() call sites (AutoController.start_monitor/_run_capacity_test/
_ensure_logging, isa101_views._on_toggle_logging).
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from aset_batt.storage.data_utils import get_app_version, write_session_metadata
from aset_batt.core.config import ConfigManager


class TestGetAppVersion(unittest.TestCase):
    def test_returns_a_non_empty_string(self):
        v = get_app_version()
        self.assertIsInstance(v, str)
        self.assertGreater(len(v), 0)

    def test_result_is_cached(self):
        v1 = get_app_version()
        v2 = get_app_version()
        self.assertEqual(v1, v2)


class TestWriteSessionMetadata(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aset_meta_test_")
        self.csv_path = os.path.join(self.tmpdir, "session.csv")
        with open(self.csv_path, "w", encoding="utf-8") as f:
            f.write("Timestamp,Elapsed_s\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sidecar_written_with_expected_fields(self):
        cfg = ConfigManager()
        cfg.battery.product_name = ""   # no product selected -> no measured_params lookup
        cfg.battery.harness_resistance_ohm = 0.065
        cfg.system.operator_name = "artit.r"

        write_session_metadata(self.csv_path, cfg)

        meta_path = self.csv_path + ".meta.json"
        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

        self.assertEqual(meta["operator"], "artit.r")
        self.assertEqual(meta["harness_resistance_ohm"], 0.065)
        self.assertIn("app_version", meta)
        self.assertIn("written_at", meta)
        self.assertIn("battery_type", meta)
        self.assertIn("measured_params", meta)

    def test_empty_operator_name_falls_back_to_os_username(self):
        cfg = ConfigManager()
        cfg.system.operator_name = ""

        write_session_metadata(self.csv_path, cfg)

        with open(self.csv_path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertNotEqual(meta["operator"], "")

    def test_product_with_measured_params_is_captured(self):
        cfg = ConfigManager()
        cfg.battery.product_name = "YTZ7V (12V 7Ah VRLA)"   # a real built-in product

        write_session_metadata(self.csv_path, cfg)

        with open(self.csv_path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["product_name"], "YTZ7V (12V 7Ah VRLA)")
        self.assertIsInstance(meta["measured_params"], dict)

    def test_failure_is_non_fatal_does_not_raise(self):
        """A malformed/duck-typed config object must not crash test startup —
        this is best-effort audit context, not a hard requirement to run."""
        class _BrokenConfig:
            @property
            def battery(self):
                raise RuntimeError("boom")
        write_session_metadata(self.csv_path, _BrokenConfig())   # must not raise
        # No sidecar on failure — no partial/garbage metadata left behind.
        self.assertFalse(os.path.exists(self.csv_path + ".meta.json"))


class TestAuditTrailWiredIntoStartLogging(unittest.TestCase):
    """Confirms the actual call sites invoke write_session_metadata(), not just
    that the function itself works in isolation."""

    def test_ensure_logging_writes_metadata_sidecar(self):
        from aset_batt.core.config import ConfigManager
        from aset_batt.core.battery_model import BatteryModel
        from aset_batt.core.state_estimator import StateEstimator
        from aset_batt.storage.data_utils import DataHandler
        from aset_batt.app.auto_controller import AutoController
        from aset_batt.hardware.mock_hardware import MockHardwareController

        cfg = ConfigManager()
        hw = MockHardwareController()
        model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                              cfg.battery.cells_series, cfg.battery.cells_parallel)
        estimator = StateEstimator(cfg.battery.rated_capacity, model)
        data = DataHandler()
        ctrl = AutoController(None, hw, data, estimator, cfg)

        try:
            ctrl._ensure_logging(label="Test")
            self.assertTrue(data.is_recording)
            meta_path = data.current_path + ".meta.json"
            self.assertTrue(os.path.exists(meta_path))
        finally:
            data.stop_logging()
            for suffix in ("", ".sha256", ".meta.json"):
                p = data.current_path + suffix if suffix else data.current_path
                if p and os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()
