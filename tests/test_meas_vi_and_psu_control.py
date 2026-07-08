"""Regression tests for _meas_vi(), set_psu_cccv(), and transient_dcir_measure()
in hardware_driver.py — all live code paths (used by read_vi(), ChargeController,
and DCIR measurement respectively) that had zero direct unit test coverage.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController


def _make_hw():
    with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
        return HardwareController()


class TestMeasViCombinedQuery(unittest.TestCase):
    def test_uses_combined_query_when_capability_unknown(self):
        hw = _make_hw()
        inst = MagicMock()
        inst.query.return_value = "12.600,1.500,18.900"   # V,I,P

        v, i = hw._meas_vi(inst, "_load_all")

        inst.query.assert_called_once_with("MEAS:SCAL:ALL:DC?")
        self.assertAlmostEqual(v, 12.6)
        self.assertAlmostEqual(i, 1.5)
        self.assertTrue(hw._load_all, "capability should be cached True after success")

    def test_subtracts_psu_current_offset_only_for_psu_all(self):
        hw = _make_hw()
        hw._psu_current_offset = 0.6
        inst = MagicMock()
        inst.query.return_value = "13.000,1.000,13.000"

        v, i = hw._meas_vi(inst, "_psu_all")

        self.assertAlmostEqual(v, 13.0)
        self.assertAlmostEqual(i, 0.4)   # 1.000 - 0.6 offset

    def test_load_all_is_not_offset_corrected(self):
        hw = _make_hw()
        hw._psu_current_offset = 0.6   # only meaningful for the PSU channel
        inst = MagicMock()
        inst.query.return_value = "12.600,1.000,12.600"

        v, i = hw._meas_vi(inst, "_load_all")

        self.assertAlmostEqual(i, 1.0)   # unaffected by _psu_current_offset

    def test_cached_true_capability_skips_reprobing_but_still_uses_combined(self):
        hw = _make_hw()
        hw._load_all = True
        inst = MagicMock()
        inst.query.return_value = "12.0,0.5,6.0"

        v, i = hw._meas_vi(inst, "_load_all")

        inst.query.assert_called_once_with("MEAS:SCAL:ALL:DC?")
        self.assertAlmostEqual(v, 12.0)


class TestMeasViFallback(unittest.TestCase):
    def test_falls_back_to_separate_queries_when_combined_unsupported(self):
        hw = _make_hw()
        inst = MagicMock()
        inst.query.side_effect = [
            Exception("Undefined header"),   # MEAS:SCAL:ALL:DC? not supported
            "12.60",                         # MEAS:VOLT?
            "0.700",                         # MEAS:CURR?
        ]

        v, i = hw._meas_vi(inst, "_load_all")

        self.assertAlmostEqual(v, 12.60)
        self.assertAlmostEqual(i, 0.700)
        self.assertFalse(hw._load_all, "capability should be cached False after failure")

    def test_cached_false_capability_never_retries_combined_query(self):
        hw = _make_hw()
        hw._load_all = False
        inst = MagicMock()
        inst.query.side_effect = ["12.60", "0.700"]

        v, i = hw._meas_vi(inst, "_load_all")

        calls = [c.args[0] for c in inst.query.call_args_list]
        self.assertNotIn("MEAS:SCAL:ALL:DC?", calls)
        self.assertEqual(calls, ["MEAS:VOLT?", "MEAS:CURR?"])

    def test_transient_error_on_separate_query_retries_once(self):
        hw = _make_hw()
        hw._load_all = False
        inst = MagicMock()
        # First attempt: MEAS:VOLT? raises transiently. Second attempt: both succeed.
        inst.query.side_effect = [Exception("VI_ERROR_IO"), "12.60", "0.700"]

        with patch("time.sleep"):   # skip the real 200ms retry backoff
            v, i = hw._meas_vi(inst, "_load_all")

        self.assertAlmostEqual(v, 12.60)
        self.assertAlmostEqual(i, 0.700)

    def test_persistent_failure_on_separate_query_raises(self):
        hw = _make_hw()
        hw._load_all = False
        inst = MagicMock()
        inst.query.side_effect = Exception("VISA timeout")

        with patch("time.sleep"):
            with self.assertRaises(Exception):
                hw._meas_vi(inst, "_load_all")


class TestSetPsuCccv(unittest.TestCase):
    def test_writes_volt_curr_outp_on_in_order(self):
        hw = _make_hw()
        hw.is_connected = True
        hw.psu_inst = MagicMock()

        hw.set_psu_cccv(14.4, 1.5)

        calls = [c.args[0] for c in hw.psu_inst.write.call_args_list]
        self.assertEqual(calls, [":VOLT 14.4", ":CURR 1.5", ":OUTP ON"])
        self.assertTrue(hw._psu_output_on)

    def test_enables_ssr(self):
        hw = _make_hw()
        hw.is_connected = True
        hw.psu_inst = MagicMock()
        hw.is_esp_connected = True
        hw.esp_serial = MagicMock()

        hw.set_psu_cccv(14.4, 1.5)

        self.assertTrue(hw.ssr_state)

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        hw.is_connected = False
        hw.psu_inst = MagicMock()

        hw.set_psu_cccv(14.4, 1.5)

        hw.psu_inst.write.assert_not_called()

    def test_write_failure_does_not_raise(self):
        hw = _make_hw()
        hw.is_connected = True
        hw.psu_inst = MagicMock()
        hw.psu_inst.write.side_effect = RuntimeError("timeout")

        hw.set_psu_cccv(14.4, 1.5)   # must not raise


class TestTransientDcirMeasure(unittest.TestCase):
    def test_computes_dcir_from_voltage_step(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = ["12.600", "12.500"]   # v_before, v_after
        hw.load_inst = MagicMock()

        dcir_mohm = hw.transient_dcir_measure(current_target=2.0, delta_I=2.0)

        # |12.600 - 12.500| / 2.0 * 1000 = 50 mOhm
        self.assertAlmostEqual(dcir_mohm, 50.0, places=3)

    def test_uses_absolute_value_of_delta_i(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = ["12.600", "12.500"]
        hw.load_inst = MagicMock()

        dcir_mohm = hw.transient_dcir_measure(current_target=2.0, delta_I=-2.0)

        self.assertAlmostEqual(dcir_mohm, 50.0, places=3)

    def test_sends_current_target_to_load(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = ["12.600", "12.500"]
        hw.load_inst = MagicMock()

        hw.transient_dcir_measure(current_target=-3.5, delta_I=3.5)

        hw.load_inst.write.assert_called_once_with(":CURR 3.5")   # abs() applied

    def test_returns_zero_on_error(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = Exception("VISA timeout")
        hw.load_inst = MagicMock()

        dcir_mohm = hw.transient_dcir_measure(current_target=2.0, delta_I=2.0)

        self.assertEqual(dcir_mohm, 0.0)


if __name__ == "__main__":
    unittest.main()
