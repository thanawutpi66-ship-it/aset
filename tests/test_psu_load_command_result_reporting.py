"""Industrial-grade audit follow-up G9.

HardwareController.set_psu()/set_load() used to only log a failed SCPI write —
the caller had no way to know the instrument didn't actually change state.
Worse, set_psu() unconditionally called set_ssr(bool(state)) even after a
caught exception, so a failed PSU write could leave the SSR relay state out
of sync with the PSU's real output. Fixed: both now return True/False, and
set_psu() only touches the SSR on success. The manual PSU/Load buttons (the
highest-value caller — an operator is directly watching) now warn on failure
instead of silently assuming success.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController
from aset_batt.hardware.mock_hardware import MockHardwareController


def _make_hw():
    with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
        hw = HardwareController()
    hw.is_connected = True
    hw.psu_inst = MagicMock()
    hw.load_inst = MagicMock()
    return hw


class TestSetPsuReturnValue(unittest.TestCase):
    def test_returns_true_on_success(self):
        hw = _make_hw()
        self.assertTrue(hw.set_psu(True, "12.0", "1.0"))

    def test_returns_false_on_scpi_write_failure(self):
        hw = _make_hw()
        hw.psu_inst.write.side_effect = Exception("VISA timeout")
        self.assertFalse(hw.set_psu(True, "12.0", "1.0"))

    def test_returns_false_when_not_connected(self):
        hw = _make_hw()
        hw.is_connected = False
        self.assertFalse(hw.set_psu(True, "12.0", "1.0"))

    def test_ssr_not_touched_when_scpi_write_fails(self):
        """The bug this closes: set_ssr() used to fire unconditionally, even after
        a caught write failure, desyncing the relay from the PSU's real state."""
        hw = _make_hw()
        hw.set_ssr = MagicMock(return_value=True)
        hw.psu_inst.write.side_effect = Exception("VISA timeout")
        hw.set_psu(True, "12.0", "1.0")
        hw.set_ssr.assert_not_called()

    def test_ssr_is_touched_when_scpi_write_succeeds(self):
        hw = _make_hw()
        hw.set_ssr = MagicMock(return_value=True)
        hw.set_psu(True, "12.0", "1.0")
        hw.set_ssr.assert_called_once_with(True)


class TestSetLoadReturnValue(unittest.TestCase):
    def test_returns_true_on_success(self):
        hw = _make_hw()
        self.assertTrue(hw.set_load(True, "1.0"))

    def test_returns_false_on_scpi_write_failure(self):
        hw = _make_hw()
        hw.load_inst.write.side_effect = Exception("VISA timeout")
        self.assertFalse(hw.set_load(True, "1.0"))

    def test_returns_false_when_not_connected(self):
        hw = _make_hw()
        hw.is_connected = False
        self.assertFalse(hw.set_load(True, "1.0"))


class TestMockHardwareMirrorsTheContract(unittest.TestCase):
    """MockHardwareController must present the same True/False return contract as
    the real driver — otherwise every mock-based test in this suite would silently
    exercise a different interface than production."""

    def test_mock_set_psu_returns_true(self):
        hw = MockHardwareController()
        self.assertTrue(hw.set_psu(True, "12.0", "1.0"))
        self.assertTrue(hw.set_psu(False))

    def test_mock_set_load_returns_true(self):
        hw = MockHardwareController()
        self.assertTrue(hw.set_load(True, "1.0"))
        self.assertTrue(hw.set_load(False))


if __name__ == "__main__":
    unittest.main()
