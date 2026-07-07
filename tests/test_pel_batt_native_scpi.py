"""Regression test for the NATIVE_BATT_SCPI correction (pel_batt_test.py).

An earlier version of NATIVE_BATT_SCPI had 5 of 8 command strings wrong (guessed,
never verified against the real Programming Manual) — see
docs/rig_investigation_findings.md. This was harmless at the time because
native_supported() probed one of the wrong commands and therefore always
returned False, so the wrong strings were never actually sent to hardware.

The strings are now corrected, but native_supported() stays hardcoded False:
the manual has no command to retrieve *accumulated* Ah/Wh after a native BATT
test (only an instantaneous current/voltage query), so enabling the native
path would discharge a real battery with no way to get a usable result back.
"""
import unittest
from unittest.mock import MagicMock

from aset_batt.hardware.pel_batt_test import PelBattTest, NATIVE_BATT_SCPI


class TestNativeBattScpiCorrected(unittest.TestCase):
    def test_corrected_strings_match_the_verified_manual(self):
        self.assertEqual(NATIVE_BATT_SCPI["set_current"], ":BATTery:VALue {a}")
        self.assertEqual(NATIVE_BATT_SCPI["datalog_int"], ":BATTery:DATalog:TIMer {s}")
        self.assertEqual(NATIVE_BATT_SCPI["enable"], ":BATTery:STATe ON")
        self.assertEqual(NATIVE_BATT_SCPI["run"], ":BATT:RUN")
        self.assertEqual(NATIVE_BATT_SCPI["running?"], ":BATT:CHANnel:STATus?")
        self.assertEqual(NATIVE_BATT_SCPI["abort"], ":BATTery:STATe OFF")

    def test_fetch_ah_wh_have_no_real_command(self):
        # documents the actual manual gap rather than a guessed/wrong string
        self.assertIsNone(NATIVE_BATT_SCPI["fetch_ah"])
        self.assertIsNone(NATIVE_BATT_SCPI["fetch_wh"])


class TestNativeSupportedStaysDisabled(unittest.TestCase):
    def test_native_supported_is_always_false(self):
        tester = PelBattTest(MagicMock(), rated_capacity_ah=7.0)
        self.assertFalse(tester.native_supported())

    def test_run_native_batt_test_never_touches_the_load(self):
        load = MagicMock()
        tester = PelBattTest(load, rated_capacity_ah=7.0)

        result = tester.run_native_batt_test(1.0, 10.5)

        self.assertIsNone(result)
        load.write.assert_not_called()
        load.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
