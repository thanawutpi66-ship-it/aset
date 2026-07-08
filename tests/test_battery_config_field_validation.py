"""Industrial-grade audit G4: ConfigManager.validate_config() used to check only
rated_capacity/max_voltage-vs-min_voltage/harness_resistance_ohm (D2) — cells_series,
cells_parallel, nominal_voltage, and mass_grams passed straight through unchecked
despite feeding pack_nominal_voltage/pack_max_voltage/pack_min_voltage directly (see
config.py's own properties). A mistyped 0 or negative value there used to silently
corrupt every pack-voltage-derived safety window and grading baseline.
"""
import unittest

from aset_batt.core.config import ConfigManager


def _valid_config():
    cfg = ConfigManager()
    # Known-good baseline so each test below can flip exactly one field.
    cfg.battery.rated_capacity = 5.3
    cfg.battery.max_voltage = 2.45
    cfg.battery.min_voltage = 1.75
    cfg.battery.harness_resistance_ohm = 0.065
    cfg.battery.cells_series = 6
    cfg.battery.cells_parallel = 1
    cfg.battery.nominal_voltage = 2.0
    cfg.battery.mass_grams = 1900.0
    return cfg


class TestCellsSeriesValidation(unittest.TestCase):
    def test_valid_value_passes(self):
        self.assertTrue(_valid_config().validate_config())

    def test_zero_rejected(self):
        cfg = _valid_config()
        cfg.battery.cells_series = 0
        self.assertFalse(cfg.validate_config())

    def test_negative_rejected(self):
        cfg = _valid_config()
        cfg.battery.cells_series = -6
        self.assertFalse(cfg.validate_config())


class TestCellsParallelValidation(unittest.TestCase):
    def test_zero_rejected(self):
        cfg = _valid_config()
        cfg.battery.cells_parallel = 0
        self.assertFalse(cfg.validate_config())

    def test_negative_rejected(self):
        cfg = _valid_config()
        cfg.battery.cells_parallel = -1
        self.assertFalse(cfg.validate_config())


class TestNominalVoltageValidation(unittest.TestCase):
    def test_zero_rejected(self):
        cfg = _valid_config()
        cfg.battery.nominal_voltage = 0.0
        self.assertFalse(cfg.validate_config())

    def test_negative_rejected(self):
        cfg = _valid_config()
        cfg.battery.nominal_voltage = -2.0
        self.assertFalse(cfg.validate_config())


class TestMassGramsValidation(unittest.TestCase):
    def test_zero_is_valid(self):
        """0 = not specified is a legitimate, pre-existing convention elsewhere in
        this registry (ProductProfile.mass_grams) — only negative is implausible."""
        cfg = _valid_config()
        cfg.battery.mass_grams = 0.0
        self.assertTrue(cfg.validate_config())

    def test_negative_rejected(self):
        cfg = _valid_config()
        cfg.battery.mass_grams = -100.0
        self.assertFalse(cfg.validate_config())


class TestPackVoltageDerivesFromTheseFields(unittest.TestCase):
    """Proves WHY this matters: these fields feed pack_*_voltage directly."""

    def test_pack_voltage_reflects_valid_series_count(self):
        cfg = _valid_config()
        self.assertAlmostEqual(cfg.battery.pack_max_voltage, 2.45 * 6, places=3)


if __name__ == "__main__":
    unittest.main()
