"""Industrial-grade audit follow-ups G5 and G6.

G5: ConfigManager silently fell back to defaults (wiping calibration like
harness_resistance_ohm) when config.json was corrupt/unreadable, with only a
log line most operators never watch. Fixed: the corrupt file is backed up
(.corrupt suffix) instead of overwritten blind, and ConfigManager.load_error
is set so aset_batt/app/run.py can show a blocking warning dialog before the
main window opens.

G6: ApplicationBootstrapper.cleanup() used one bare `except Exception: pass`
covering both "AutoController not registered yet" (expected) and a genuine
controller.shutdown() failure (which cuts PSU/Load/SSR — silently losing that
failure means no record a real crash-time hardware shutdown didn't happen).
Fixed: ServiceLocator.has() separates the two cases; only the expected one is
silent.
"""
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from aset_batt.core.config import ConfigManager
from aset_batt.app.app_bootstrapper import ApplicationBootstrapper
from aset_batt.services.service_locator import ServiceLocator


class TestCorruptConfigFallback(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aset_config_test_")
        self.config_path = os.path.join(self.tmpdir, "config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_config_leaves_load_error_none(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"battery": {}, "system": {}, "hardware": {}}, f)
        cfg = ConfigManager(self.config_path)
        self.assertIsNone(cfg.load_error)
        self.assertFalse(os.path.exists(self.config_path + ".corrupt"))

    def test_corrupt_json_sets_load_error_and_backs_up_file(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")
        cfg = ConfigManager(self.config_path)

        self.assertIsNotNone(cfg.load_error)
        self.assertIn("config.json", cfg.load_error)
        # The corrupt original must be preserved, not silently discarded.
        self.assertTrue(os.path.exists(self.config_path + ".corrupt"))
        with open(self.config_path + ".corrupt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not valid json!!!")
        # A fresh, valid default config must still exist so the app stays usable.
        self.assertTrue(os.path.exists(self.config_path))
        with open(self.config_path, encoding="utf-8") as f:
            json.load(f)   # must not raise

    def test_missing_file_is_not_treated_as_corrupt(self):
        """No config.json at all (first run) is a normal, expected case — must NOT
        set load_error (that's reserved for "a file existed and was unreadable")."""
        cfg = ConfigManager(self.config_path)
        self.assertIsNone(cfg.load_error)


class TestCleanupSeparatesExpectedFromRealFailure(unittest.TestCase):
    def setUp(self):
        ServiceLocator.clear()

    def tearDown(self):
        ServiceLocator.clear()

    def test_cleanup_does_not_raise_when_controller_never_registered(self):
        bootstrapper = ApplicationBootstrapper()
        bootstrapper.service_provider = object()   # truthy, no AutoController registered
        bootstrapper.cleanup()   # must not raise

    def test_shutdown_failure_is_logged_not_swallowed(self):
        from aset_batt.app.auto_controller import AutoController
        broken_controller = MagicMock(spec=AutoController)
        broken_controller.shutdown.side_effect = RuntimeError("SCPI write failed")
        ServiceLocator.register(AutoController, broken_controller)

        bootstrapper = ApplicationBootstrapper()
        bootstrapper.service_provider = object()

        with self.assertLogs("aset_batt.app.app_bootstrapper", level="ERROR") as cm:
            bootstrapper.cleanup()   # must not raise even though shutdown() did

        self.assertTrue(any("shutdown" in msg.lower() for msg in cm.output))
        broken_controller.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
