"""Regression tests for the UX polish pass on the center panel:

1. The "CASE TEMPERATURE" box was a byte-for-byte duplicate of the TEMP metric
   card (both fed the exact same `temp` value) — removed. The TEMP card now
   carries the CRIT/WARN over-temperature color instead, sourced from the REAL
   configured safety_limits.max_temperature (two of the three call sites used to
   hardcode 35/45 C, which didn't match the configured limit at all).
2. Rin/SoH/Grade metric cards show a "-"/"N/A" placeholder before a real value
   exists; that placeholder must be visually distinct (MUTED) from an actual
   reading (TEXT), not the same color as a real number.
3. Trend graphs must have a sensible default view (not an auto-ranged-on-nothing
   window that excludes the pack's actual voltage) before any telemetry exists.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from aset_batt.ui.theme import MUTED, TEXT, CRIT, WARN
from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestCaseTemperatureDuplicateRemoved(unittest.TestCase):
    def test_temp_gauge_widget_no_longer_exists(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            self.assertFalse(hasattr(win, "_temp_gauge"))
            self.assertFalse(hasattr(win, "_update_temp_gauge"))
        finally:
            win.close()

    def test_temp_card_color_reflects_configured_safety_limit(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            crit = win.config.system.safety_limits.get("max_temperature", 55.0)
            temp_lbl, _ = win.metric_labels["Temp"]

            win._set_temp_label_color(crit - 20)   # well below warn
            self.assertIn(TEXT, temp_lbl.styleSheet())

            win._set_temp_label_color(crit - 5)     # warn band
            self.assertIn(WARN, temp_lbl.styleSheet())

            win._set_temp_label_color(crit + 1)     # over the real configured limit
            self.assertIn(CRIT, temp_lbl.styleSheet())
        finally:
            win.close()

    def test_hardcoded_3545_thresholds_not_used(self):
        """Regression: _slot_display/_slot_live_readback used to color the (now
        removed) gauge against a hardcoded 35/45 C pair, independent of whatever
        max_temperature was actually configured."""
        win = BatteryQtWindow(ConfigManager())
        try:
            win.config.system.safety_limits["max_temperature"] = 80.0
            temp_lbl, _ = win.metric_labels["Temp"]
            win._set_temp_label_color(50.0)   # > old hardcoded warn(35)/crit(45)...
            self.assertIn(TEXT, temp_lbl.styleSheet())  # ...but well under the real 80
        finally:
            win.close()


class TestPendingPlaceholdersAreVisuallyDistinct(unittest.TestCase):
    def test_rin_soh_grade_start_muted(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            for name in ("Rin", "SoH", "Grade"):
                lbl, _ = win.metric_labels_final[name]
                self.assertIn(MUTED, lbl.styleSheet())
                self.assertNotIn(TEXT, lbl.styleSheet())
        finally:
            win.close()

    def test_live_rin_card_starts_muted_and_a_real_value_restores_text_color(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            rin_lbl, _ = win.metric_labels["Rin"]
            self.assertIn(MUTED, rin_lbl.styleSheet())

            win._slot_display(12.5, 5.0, 50.0, 0.05, 25.0, float("nan"), win._run_generation)
            self.assertIn(TEXT, rin_lbl.styleSheet())
        finally:
            win.close()

    def test_final_soh_stays_muted_for_nan_but_lights_up_for_a_real_value(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            soh_lbl, _ = win.metric_labels_final["SoH"]
            results = {
                "soh": float("nan"), "ri_mohm": 30.0, "grade": "B", "confidence": 0.9,
                "capacity_ah": 5.0, "dcir_mohm": 30.0, "dcir_std_mohm": 0.0,
                "dcir_n_steps": 3, "quality_warnings": [], "voltage_sag_v": 0.0,
                "cca_est_a": 0.0, "ica": ([], []),
            }
            win._on_test_finished(results)
            self.assertIn(MUTED, soh_lbl.styleSheet())

            results["soh"] = 88.0
            win._on_test_finished(results)
            self.assertIn(TEXT, soh_lbl.styleSheet())
        finally:
            win.close()


class TestTrendGraphDefaultRanges(unittest.TestCase):
    def test_combined_voltage_range_includes_pack_voltage(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            (x_lo, x_hi), (y_lo, y_hi) = win.trend._combined.p.getViewBox().viewRange()
            pack_v = win.config.battery.pack_nominal_voltage
            self.assertLess(y_lo, pack_v)
            self.assertGreater(y_hi, pack_v)
            self.assertGreaterEqual(x_hi, 30)  # a real idle window, not a near-zero one
        finally:
            win.close()

    def test_bottom_axis_si_prefix_locked_on_all_three_modes(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            self.assertFalse(win.trend._combined.p.getAxis("bottom").autoSIPrefix)
            self.assertFalse(win.trend._split2._vi.getAxis("bottom").autoSIPrefix)
            self.assertFalse(win.trend._split2._tp.getAxis("bottom").autoSIPrefix)
            for pw in win.trend._split3._plots:
                self.assertFalse(pw.getAxis("bottom").autoSIPrefix)
        finally:
            win.close()

    def test_split2_and_split3_also_get_sane_default_ranges(self):
        win = BatteryQtWindow(ConfigManager())
        try:
            pack_v = win.config.battery.pack_nominal_voltage
            (_, _), (y_lo, y_hi) = win.trend._split2._vi.getViewBox().viewRange()
            self.assertLess(y_lo, pack_v)
            self.assertGreater(y_hi, pack_v)

            v_plot = win.trend._split3._plots[0]
            (_, _), (y_lo3, y_hi3) = v_plot.getViewBox().viewRange()
            self.assertLess(y_lo3, pack_v)
            self.assertGreater(y_hi3, pack_v)
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
