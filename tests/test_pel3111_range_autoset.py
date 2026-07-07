"""Regression tests for auto-selecting the PEL-3111 CRANge/VRANge on connect.

Conservative by design (see recommend_pel3111_ranges docstring): only narrows
the range when there's >=25% headroom over the value actually used, else falls
back to HIGH/HIGH. The motivating edge case is a 12V lead-acid pack (pack_max
~14.7V) sitting right at the 15V LOW-voltage-range ceiling — that must NOT
get the narrow range, or a real test could clip against the range boundary.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController, recommend_pel3111_ranges


class TestRecommendPel3111Ranges(unittest.TestCase):
    def test_small_li_ion_pack_gets_narrow_ranges(self):
        # e.g. 1S Li-ion, 1A max discharge, 4.2V max — comfortably inside LOW/LOW
        i_range, v_range = recommend_pel3111_ranges(max_current_a=1.0, pack_max_voltage_v=4.2)
        self.assertEqual(i_range, "LOW")
        self.assertEqual(v_range, "LOW")

    def test_12v_lead_acid_pack_falls_back_to_high_voltage_range(self):
        # YTZ6V-like pack: pack_max_voltage ~14.7V, only ~2% headroom under 15V ceiling
        i_range, v_range = recommend_pel3111_ranges(max_current_a=5.0, pack_max_voltage_v=14.7)
        self.assertEqual(v_range, "HIGH")   # must NOT pick LOW here

    def test_mid_current_pack_gets_middle_current_range(self):
        i_range, v_range = recommend_pel3111_ranges(max_current_a=10.0, pack_max_voltage_v=4.2)
        self.assertEqual(i_range, "MIDDle")

    def test_current_right_at_low_boundary_falls_back_to_middle(self):
        # 2.1A LOW ceiling * 0.75 margin = 1.575A — just above that must NOT stay LOW
        i_range, _ = recommend_pel3111_ranges(max_current_a=1.6, pack_max_voltage_v=4.2)
        self.assertEqual(i_range, "MIDDle")

    def test_high_current_pack_falls_back_to_high(self):
        i_range, _ = recommend_pel3111_ranges(max_current_a=50.0, pack_max_voltage_v=4.2)
        self.assertEqual(i_range, "HIGH")


class TestSetLoadRange(unittest.TestCase):
    def test_writes_crange_and_vrange_to_load_inst(self):
        with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
            hw = HardwareController()
        hw.load_inst = MagicMock()

        hw.set_load_range("MIDDle", "HIGH")

        hw.load_inst.write.assert_any_call(":CRANge MIDDle")
        hw.load_inst.write.assert_any_call(":VRANge HIGH")

    def test_noop_when_not_connected(self):
        with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
            hw = HardwareController()
        hw.set_load_range("LOW", "LOW")   # load_inst is None — must not raise

    def test_write_failure_does_not_raise(self):
        with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
            hw = HardwareController()
        hw.load_inst = MagicMock()
        hw.load_inst.write.side_effect = RuntimeError("timeout")

        hw.set_load_range("LOW", "LOW")   # must not propagate


if __name__ == "__main__":
    unittest.main()
