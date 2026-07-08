"""Regression test for the manual SSR ON/OFF control added to the SETUP zone.

The SSR relay (ESP32 GPIO16) was previously status-only (see zones.py history):
fully automatic, no manual override. This adds Manual ON/Manual OFF buttons for
diagnostics/recovery, gated on ESP32 being connected, with a confirmation
dialog on ON (skipped in headless/test mode) since closing the relay while a
PSU output happens to be left ON would push current immediately. OFF never
prompts, matching E-STOP's "always safe to cut power immediately" behavior.

Also covers the dead-code cleanup that came with this: isa101_views.py used to
carry an orphaned duplicate of `_zone_characterize` that shadowed
CharacterizeMixin's real one (identical content today, so no visible bug yet,
but any future edit to characterize.py's version would have silently not
taken effect) plus two more never-called dead methods (`_estop_bar`,
`_build_results_html`) left over from the mixin-split refactor. All three were
removed; this test locks in that CHARACTERIZE now genuinely resolves through
CharacterizeMixin.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.ui.characterize import CharacterizeMixin
from aset_batt.hardware.mock_hardware import MockHardwareController

_app = QApplication.instance() or QApplication([])


class TestSsrManualControlButtons(unittest.TestCase):
    def _make_window(self):
        w = BatteryQtWindow(ConfigManager())
        w.hw = MockHardwareController()
        return w

    def test_buttons_disabled_until_esp32_connected(self):
        w = self._make_window()
        try:
            self.assertFalse(w.btn_ssr_on.isEnabled())
            self.assertFalse(w.btn_ssr_off.isEnabled())
            w.hw.connect_esp32("COM_MOCK")
            w._update_connection_status()
            self.assertTrue(w.btn_ssr_on.isEnabled())
            self.assertTrue(w.btn_ssr_off.isEnabled())
        finally:
            w.close()

    def test_manual_off_sets_ssr_state_false(self):
        w = self._make_window()
        try:
            w.hw.connect_esp32("COM_MOCK")
            w.hw.set_ssr(True)
            w._on_ssr_manual_off()   # headless → no confirmation dialog
            self.assertFalse(w.hw.ssr_state)
        finally:
            w.close()

    def test_manual_on_sets_ssr_state_true_in_headless_mode(self):
        w = self._make_window()
        try:
            w.hw.connect_esp32("COM_MOCK")
            w._on_ssr_manual_on()    # headless → confirmation dialog skipped
            self.assertTrue(w.hw.ssr_state)
        finally:
            w.close()

    def test_manual_controls_noop_without_esp32(self):
        w = self._make_window()
        try:
            w._on_ssr_manual_on()
            self.assertIsNone(w.hw.ssr_state)   # never touched — no ESP32 connected
        finally:
            w.close()


class TestCharacterizeZoneNoLongerShadowed(unittest.TestCase):
    def test_zone_characterize_resolves_to_the_mixin(self):
        self.assertIs(
            BatteryQtWindow._zone_characterize,
            CharacterizeMixin._zone_characterize,
        )


if __name__ == "__main__":
    unittest.main()
