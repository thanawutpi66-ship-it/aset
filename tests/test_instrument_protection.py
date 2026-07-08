"""Regression tests for the hardware-level protection/hardening features.

SCPI syntax verified against the real PEL-3000H and PSW programming manuals
(extracted PDF text, not guessed) — [:CONFigure]:OCP/UVP/OVP for the PEL-3111,
[SOURce:]CURRent:PROTection[:LEVel]/VOLTage:PROTection[:LEVel] and
OUTPut:PROTection:CLEar/TRIPped? for the PSW.

These are a hardware-level safety backstop, independent of the PC's own
software safety_limits checks: they trip at the instrument even if the PC
hangs/crashes. Applied automatically on every Connect (see _on_connect) —
not an operator toggle — except clearing a trip, which stays a deliberate
manual action (a trip means something real happened).
"""
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.hardware.hardware_driver import HardwareController


def _make_hw():
    with patch("aset_batt.hardware.hardware_driver.pyvisa.ResourceManager"):
        return HardwareController()


class TestSetLoadProtection(unittest.TestCase):
    def test_writes_ocp_uvp_ovp_with_loff_mode(self):
        hw = _make_hw()
        hw.load_inst = MagicMock()
        hw.load_inst.query.return_value = "0,\"No error\""

        hw.set_load_protection(ocp_a=12.5, uvp_v=10.0, ovp_v=15.5)

        hw.load_inst.write.assert_any_call(":CONFigure:OCP LOFF")
        hw.load_inst.write.assert_any_call(":CONFigure:OCP 12.5")
        hw.load_inst.write.assert_any_call(":CONFigure:UVP 10.0")
        hw.load_inst.write.assert_any_call(":CONFigure:OVP 15.5")

    def test_skips_none_values(self):
        hw = _make_hw()
        hw.load_inst = MagicMock()
        hw.load_inst.query.return_value = "0,\"No error\""

        hw.set_load_protection(ocp_a=12.5)

        calls = [c.args[0] for c in hw.load_inst.write.call_args_list]
        self.assertTrue(any("OCP" in c for c in calls))
        self.assertFalse(any("UVP" in c for c in calls))
        self.assertFalse(any("OVP" in c for c in calls))

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        self.assertEqual(hw.set_load_protection(ocp_a=1.0), "")   # load_inst is None

    def test_returns_scpi_error_string(self):
        hw = _make_hw()
        hw.load_inst = MagicMock()
        hw.load_inst.query.return_value = '-222,"Data out of range"'

        err = hw.set_load_protection(ocp_a=999.0)

        self.assertIn("Data out of range", err)


class TestSetPsuProtection(unittest.TestCase):
    def test_writes_ocp_and_ovp(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "0,\"No error\""

        hw.set_psu_protection(ocp_a=8.0, ovp_v=16.0)

        hw.psu_inst.write.assert_any_call(":CURR:PROT:LEV 8.0")
        hw.psu_inst.write.assert_any_call(":CURR:PROT:STAT ON")
        hw.psu_inst.write.assert_any_call(":VOLT:PROT:LEV 16.0")

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        self.assertEqual(hw.set_psu_protection(ocp_a=1.0), "")


class TestPsuTripQueryAndClear(unittest.TestCase):
    def test_get_tripped_true(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "1"
        self.assertTrue(hw.get_psu_protection_tripped())

    def test_get_tripped_false(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "0"
        self.assertFalse(hw.get_psu_protection_tripped())

    def test_get_tripped_false_when_not_connected(self):
        hw = _make_hw()
        self.assertFalse(hw.get_psu_protection_tripped())

    def test_clear_sends_command(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        ok = hw.clear_psu_protection()
        hw.psu_inst.write.assert_called_with("OUTP:PROT:CLE")
        self.assertTrue(ok)

    def test_clear_false_when_not_connected(self):
        hw = _make_hw()
        self.assertFalse(hw.clear_psu_protection())


class TestHardenAndReleaseInstrumentConfig(unittest.TestCase):
    def test_harden_locks_both_panels_and_disables_psu_pon(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.load_inst = MagicMock()

        hw.harden_instrument_config()

        hw.psu_inst.write.assert_any_call("SYST:CONF:OUTP:PON OFF")
        hw.psu_inst.write.assert_any_call("SYST:KLOC ON")
        hw.load_inst.write.assert_any_call(":UTIL:REM ON")

    def test_harden_resets_resistance_emulation_and_sets_low_averaging(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()

        hw.harden_instrument_config()

        hw.psu_inst.write.assert_any_call(":RES 0.000")
        hw.psu_inst.write.assert_any_call("SENS:AVER:COUN LOW")

    def test_release_unlocks_both_panels(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.load_inst = MagicMock()

        hw.release_instrument_config()

        hw.psu_inst.write.assert_any_call("SYST:KLOC OFF")
        hw.load_inst.write.assert_any_call(":UTIL:REM OFF")

    def test_harden_does_not_raise_when_disconnected(self):
        hw = _make_hw()
        hw.harden_instrument_config()   # must not raise
        hw.release_instrument_config()  # must not raise

    def test_harden_enables_load_short_safety_and_alarm(self):
        hw = _make_hw()
        hw.load_inst = MagicMock()

        hw.harden_instrument_config()

        hw.load_inst.write.assert_any_call(":CONFigure:SHORt:SAFety ON")
        hw.load_inst.write.assert_any_call(":UTIL:ALAR ON")


class TestSetPsuResistanceEmulation(unittest.TestCase):
    def test_writes_resistance_value(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "0,\"No error\""

        hw.set_psu_resistance_emulation(0.050)

        hw.psu_inst.write.assert_called_with(":RES 0.05")

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        self.assertEqual(hw.set_psu_resistance_emulation(0.05), "")

    def test_returns_scpi_error_when_out_of_range(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = '-222,"Data out of range"'

        err = hw.set_psu_resistance_emulation(99.0)   # exceeds PSW 80-40.5's 1.975Ω max

        self.assertIn("Data out of range", err)


class TestSetPsuAveraging(unittest.TestCase):
    def test_writes_averaging_level(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.set_psu_averaging("HIGH")
        hw.psu_inst.write.assert_called_with("SENS:AVER:COUN HIGH")

    def test_default_level_is_low(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.set_psu_averaging()
        hw.psu_inst.write.assert_called_with("SENS:AVER:COUN LOW")

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        hw.set_psu_averaging()   # must not raise


class TestBeep(unittest.TestCase):
    def test_writes_beeper_command_with_duration(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.beep(2.0)
        hw.psu_inst.write.assert_called_with("SYST:BEEP 2.0")

    def test_noop_when_not_connected(self):
        hw = _make_hw()
        hw.beep(1.0)   # must not raise

    def test_failure_is_swallowed(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.write.side_effect = RuntimeError("timeout")
        hw.beep(1.0)   # must not raise


class TestGetInstrumentInfo(unittest.TestCase):
    def test_queries_both_instruments(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "#3212MFRS GW-INSTEK,Model PSW80-13.5"
        hw.load_inst = MagicMock()
        hw.load_inst.query.return_value = "PEL-3111,12345678,V1.01.001"

        info = hw.get_instrument_info()

        hw.psu_inst.query.assert_called_with("SYST:INF?")
        hw.load_inst.query.assert_called_with(":UTIL:SYST?")
        self.assertIn("PSW80-13.5", info["psu"])
        self.assertIn("PEL-3111", info["load"])

    def test_empty_when_not_connected(self):
        hw = _make_hw()
        info = hw.get_instrument_info()
        self.assertEqual(info, {"psu": "", "load": ""})

    def test_query_failure_reported_not_raised(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.side_effect = RuntimeError("timeout")
        info = hw.get_instrument_info()
        self.assertIn("query failed", info["psu"])


class TestCheckScpiError(unittest.TestCase):
    def test_clean_error_queue_returns_empty_string(self):
        hw = _make_hw()
        inst = MagicMock()
        inst.query.return_value = '0,"No error"'
        self.assertEqual(hw._check_scpi_error(inst, "Test"), "")

    def test_plus_zero_also_treated_as_clean(self):
        hw = _make_hw()
        inst = MagicMock()
        inst.query.return_value = '+0,"No error"'
        self.assertEqual(hw._check_scpi_error(inst, "Test"), "")

    def test_real_error_is_returned(self):
        hw = _make_hw()
        inst = MagicMock()
        inst.query.return_value = '-113,"Undefined header"'
        self.assertIn("Undefined header", hw._check_scpi_error(inst, "Test"))

    def test_none_instrument_returns_empty(self):
        hw = _make_hw()
        self.assertEqual(hw._check_scpi_error(None, "Test"), "")


class TestApplyDefaultSafetyProtection(unittest.TestCase):
    """G7 (industrial-grade audit): range-set + OVP/OCP/UVP protection +
    instrument hardening used to live ONLY inline in isa101_views.py's
    _on_connect() handler — any other real-hardware entry point (a script, a
    test harness, a future alternate UI) got no instrument-level backstop at
    all. Now consolidated into one HardwareController method any caller can
    invoke directly, independent of the UI."""

    def _connected_hw(self):
        hw = _make_hw()
        hw.psu_inst = MagicMock()
        hw.psu_inst.query.return_value = "0,\"No error\""
        hw.load_inst = MagicMock()
        hw.load_inst.query.return_value = "0,\"No error\""
        return hw

    def test_applies_load_range_load_protection_psu_protection_and_hardening(self):
        hw = self._connected_hw()

        result = hw.apply_default_safety_protection(
            max_current_a=5.0, pack_max_voltage_v=14.7, min_voltage_v=10.5)

        # Range auto-set
        load_writes = [c.args[0] for c in hw.load_inst.write.call_args_list]
        self.assertTrue(any(":CRANge" in w for w in load_writes))
        self.assertTrue(any(":VRANge" in w for w in load_writes))
        # Load protection: OCP = 5.0*1.25, OVP = 14.7*1.1, UVP = 10.5
        self.assertTrue(any(":CONFigure:OCP 6.25" == w for w in load_writes))
        self.assertTrue(any(":CONFigure:UVP 10.5" == w for w in load_writes))
        self.assertTrue(any(":CONFigure:OVP 16.17" == w for w in load_writes))
        # PSU protection
        psu_writes = [c.args[0] for c in hw.psu_inst.write.call_args_list]
        self.assertTrue(any(":CURR:PROT:LEV 6.25" == w for w in psu_writes))
        self.assertTrue(any(":VOLT:PROT:LEV 16.17" == w for w in psu_writes))
        # Hardening (panel lock is one of harden_instrument_config's writes)
        self.assertTrue(any("KLOC ON" in w for w in psu_writes))

    def test_returns_instrument_info(self):
        hw = self._connected_hw()
        hw.psu_inst.query.side_effect = lambda cmd: (
            "0,\"No error\"" if "SYST:ERR" in cmd else "PSW-ID")
        hw.load_inst.query.side_effect = lambda cmd: (
            "0,\"No error\"" if "SYST:ERR" in cmd else "PEL-ID")

        result = hw.apply_default_safety_protection(5.0, 14.7)

        self.assertEqual(result["info"]["psu"], "PSW-ID")
        self.assertEqual(result["info"]["load"], "PEL-ID")

    def test_a_single_scpi_failure_is_collected_as_a_warning_not_raised(self):
        hw = self._connected_hw()
        hw.load_inst.write.side_effect = Exception("VISA timeout")

        result = hw.apply_default_safety_protection(5.0, 14.7)   # must not raise

        self.assertTrue(any("skipped" in w.lower() or "error" in w.lower()
                            for w in result["warnings"]))
        # A failure on the Load side must not prevent PSU protection from
        # still being attempted.
        hw.psu_inst.write.assert_any_call(":CURR:PROT:LEV 6.25")

    def test_no_instruments_connected_does_not_raise(self):
        hw = _make_hw()   # psu_inst/load_inst both None
        result = hw.apply_default_safety_protection(5.0, 14.7)
        self.assertEqual(result["info"], {"psu": "", "load": ""})

    def test_omitted_min_voltage_skips_uvp(self):
        hw = self._connected_hw()
        hw.apply_default_safety_protection(5.0, 14.7)   # min_voltage_v defaults to 0.0
        load_writes = [c.args[0] for c in hw.load_inst.write.call_args_list]
        self.assertFalse(any("UVP" in w for w in load_writes))


if __name__ == "__main__":
    unittest.main()
