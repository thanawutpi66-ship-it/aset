"""Industrial-grade audit follow-up G1 (partial — see the summary given to the
user for what was deliberately NOT touched: chart series colors and button
accent colors, which are a different design concern — data-series
differentiation and interactive affordances, not status/alarm signaling).

The Current metric card used to render WARN (amber) unconditionally for the
entire duration of every normal discharge — amber is supposed to mean
"caution," and discharging is this device's routine, expected operating
state, not a caution condition (ISA-101: color reserved for abnormal, not
routine status — see theme.py's own docstring). Fixed: normal-range
discharge is neutral (TEXT); amber/red now only appear once current actually
approaches/reaches the configured max_current limit.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from aset_batt.ui.theme import TEXT, WARN, CRIT, INFO, MUTED
from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


def _make_window(max_current=5.0):
    w = BatteryQtWindow(ConfigManager())
    w.config.battery.max_current = max_current
    return w


class TestNormalDischargeIsNeutralNotAmber(unittest.TestCase):
    def test_low_current_discharge_is_neutral(self):
        w = _make_window(max_current=5.0)
        try:
            w._update_vi_temp_labels(v=12.0, i=1.0, temp=25.0)   # 20% of limit
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(TEXT, i_lbl.styleSheet())
            self.assertNotIn(WARN, i_lbl.styleSheet())
        finally:
            w.close()

    def test_moderate_discharge_still_neutral(self):
        w = _make_window(max_current=5.0)
        try:
            w._update_vi_temp_labels(v=12.0, i=3.0, temp=25.0)   # 60% of limit
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(TEXT, i_lbl.styleSheet())
        finally:
            w.close()


class TestCurrentNearLimitEscalatesColor(unittest.TestCase):
    def test_current_at_90_percent_of_limit_is_warn(self):
        w = _make_window(max_current=5.0)
        try:
            w._update_vi_temp_labels(v=12.0, i=4.6, temp=25.0)   # 92% of limit
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(WARN, i_lbl.styleSheet())
        finally:
            w.close()

    def test_current_at_limit_is_crit(self):
        w = _make_window(max_current=5.0)
        try:
            w._update_vi_temp_labels(v=12.0, i=5.0, temp=25.0)   # 100% of limit
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(CRIT, i_lbl.styleSheet())
        finally:
            w.close()


class TestChargingAndRestUnaffected(unittest.TestCase):
    def test_charging_still_uses_info_accent(self):
        w = _make_window()
        try:
            w._update_vi_temp_labels(v=13.5, i=-1.0, temp=25.0)   # negative = charging
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(INFO, i_lbl.styleSheet())
            self.assertEqual(w._lbl_i_dir.text(), "▲  CHG")
        finally:
            w.close()

    def test_rest_still_neutral_with_muted_badge(self):
        w = _make_window()
        try:
            w._update_vi_temp_labels(v=12.6, i=0.0, temp=25.0)
            i_lbl, _ = w.metric_labels["Current"]
            self.assertIn(TEXT, i_lbl.styleSheet())
            self.assertIn(MUTED, w._lbl_i_dir.styleSheet())
            self.assertEqual(w._lbl_i_dir.text(), "—  REST")
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
