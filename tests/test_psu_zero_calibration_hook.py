"""Regression test for auto-invoking calibrate_psu_zero() on ESP32 connect.

calibrate_psu_zero() existed but was never called anywhere in the codebase —
the PSW 80-40.5's ~0.6A phantom MEAS:CURR? offset (see _psu_current_offset in
__init__) was measured/subtracted-out only if a caller remembered to invoke it
by hand. connect_esp32() now calls it right after set_ssr(False), the one
point where the SSR is confirmed open (PSU genuinely isolated from the
battery), matching calibrate_psu_zero()'s own precondition.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController


def _make_hw():
    with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
        return HardwareController()


class TestPsuZeroCalibrationHook(unittest.TestCase):
    def test_calibrate_runs_after_ssr_off_when_psu_connected(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "0.612"

        with patch("serial.Serial", return_value=MagicMock()), \
             patch("threading.Thread"):
            hw.connect_esp32("COM_MOCK")

        self.assertFalse(hw.ssr_state)                       # SSR forced OFF
        self.assertAlmostEqual(hw._psu_current_offset, 0.612, places=3)

    def test_no_crash_when_psu_not_yet_connected(self):
        hw = _make_hw()   # psu_inst stays None (e.g. ESP32-only connect)

        with patch("serial.Serial", return_value=MagicMock()), \
             patch("threading.Thread"):
            hw.connect_esp32("COM_MOCK")   # must not raise

        self.assertFalse(hw.ssr_state)
        self.assertEqual(hw._psu_current_offset, 0.0)        # untouched

    def test_query_failures_degrade_gracefully(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = RuntimeError("timeout")

        with patch("serial.Serial", return_value=MagicMock()), \
             patch("threading.Thread"):
            hw.connect_esp32("COM_MOCK")   # every MEAS:CURR? sample fails — must not crash connect

        self.assertTrue(hw.is_esp_connected)
        self.assertFalse(hw.ssr_state)
        self.assertEqual(hw._psu_current_offset, 0.0)   # no samples → falls back to 0.0


if __name__ == "__main__":
    unittest.main()
