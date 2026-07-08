"""Regression tests for HardwareController.connect_instruments()/
disconnect_instruments() — the core VISA connect/disconnect flow that every
other hardware_driver.py feature added this session (protection, hardening,
range auto-set, PSU-offset calibration) is built on top of, but which had zero
direct test coverage of its own until now.
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController


def _make_hw():
    with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
        return HardwareController()


def _mock_instruments(hw, psu_idn="GW,PSW80-40.5,SN1,V1", load_idn="GW,PEL-3111,SN2,V1"):
    mock_psu = MagicMock()
    mock_psu.query.return_value = psu_idn
    mock_load = MagicMock()
    mock_load.query.return_value = load_idn
    hw.rm.open_resource.side_effect = lambda port: (
        mock_psu if port == "PSU_PORT" else mock_load)
    return mock_psu, mock_load


class TestConnectInstrumentsSuccess(unittest.TestCase):
    def test_sets_psu_and_load_inst_and_marks_connected(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)

        hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        self.assertTrue(hw.is_connected)
        self.assertIs(hw.psu_inst, mock_psu)
        self.assertIs(hw.load_inst, mock_load)
        self.assertEqual(hw.connect_error, "")

    def test_forces_output_and_input_off_after_connect(self):
        """Safe idle state — see the comment right after this in the source: the
        SSR state is unknown at this point, so the PSU/Load outputs must be
        forced off regardless of whatever they were left at."""
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)

        hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        mock_psu.write.assert_any_call(":OUTP OFF")
        mock_load.write.assert_any_call(":INP OFF")
        self.assertFalse(hw._psu_output_on)

    def test_configures_serial_parameters_on_both_instruments(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)

        hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        for inst in (mock_psu, mock_load):
            self.assertEqual(inst.baud_rate, 9600)
            self.assertEqual(inst.read_termination, "\n")
            self.assertEqual(inst.write_termination, "\n")
            self.assertEqual(inst.timeout, 5000)

    def test_reconnect_closes_previously_open_instruments_first(self):
        hw = _make_hw()
        old_psu, old_load = _mock_instruments(hw)
        hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        new_psu, new_load = MagicMock(), MagicMock()
        new_psu.query.return_value = "GW,PSW80-40.5,SN9,V2"
        new_load.query.return_value = "GW,PEL-3111,SN9,V2"
        hw.rm.open_resource.side_effect = lambda port: (
            new_psu if port == "PSU_PORT2" else new_load)

        hw.connect_instruments("PSU_PORT2", "LOAD_PORT2")

        old_psu.close.assert_called_once()
        old_load.close.assert_called_once()
        self.assertIs(hw.psu_inst, new_psu)
        self.assertIs(hw.load_inst, new_load)


class TestConnectInstrumentsFailure(unittest.TestCase):
    def test_psu_not_responding_raises_and_sets_connect_error(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)
        mock_psu.query.side_effect = Exception("VISA timeout")

        with self.assertRaises(RuntimeError):
            hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        self.assertIn("PSU", hw.connect_error)
        self.assertIn("ไม่ตอบสนอง", hw.connect_error)
        self.assertFalse(hw.is_connected)
        self.assertIsNone(hw.psu_inst)

    def test_load_not_responding_raises_and_sets_connect_error(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)
        mock_load.query.side_effect = Exception("VISA timeout")

        with self.assertRaises(RuntimeError):
            hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        self.assertIn("Load", hw.connect_error)
        self.assertFalse(hw.is_connected)
        self.assertIsNone(hw.psu_inst)   # both torn down, not left half-connected

    def test_psu_failure_closes_both_open_resources(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)
        mock_psu.query.side_effect = Exception("VISA timeout")

        with self.assertRaises(RuntimeError):
            hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        mock_psu.close.assert_called_once()
        mock_load.close.assert_called_once()


class TestDisconnectInstruments(unittest.TestCase):
    def test_turns_off_outputs_closes_and_clears_state(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)
        hw.connect_instruments("PSU_PORT", "LOAD_PORT")

        hw.disconnect_instruments()

        mock_psu.write.assert_any_call(":OUTP OFF")
        mock_psu.close.assert_called_once()
        mock_load.write.assert_any_call(":INP OFF")
        mock_load.close.assert_called_once()
        self.assertIsNone(hw.psu_inst)
        self.assertIsNone(hw.load_inst)
        self.assertFalse(hw.is_connected)
        self.assertFalse(hw._psu_output_on)

    def test_forces_ssr_off_as_defense_in_depth(self):
        hw = _make_hw()
        _mock_instruments(hw)
        hw.connect_instruments("PSU_PORT", "LOAD_PORT")
        hw.is_esp_connected = True
        hw.esp_serial = MagicMock()

        hw.disconnect_instruments()

        self.assertFalse(hw.ssr_state)

    def test_does_not_raise_when_never_connected(self):
        hw = _make_hw()
        hw.disconnect_instruments()   # must not raise
        self.assertIsNone(hw.psu_inst)
        self.assertIsNone(hw.load_inst)

    def test_survives_close_raising(self):
        hw = _make_hw()
        mock_psu, mock_load = _mock_instruments(hw)
        hw.connect_instruments("PSU_PORT", "LOAD_PORT")
        mock_psu.close.side_effect = RuntimeError("already closed")

        hw.disconnect_instruments()   # must not raise / must still clear load_inst

        self.assertIsNone(hw.psu_inst)
        self.assertIsNone(hw.load_inst)


if __name__ == "__main__":
    unittest.main()
