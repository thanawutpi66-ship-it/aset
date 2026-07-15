"""Regression tests for live theme retheme (aset_batt/ui/theme.py's style()/
on_retheme()/retheme() registry) and the pyqtgraph shared crosshair
(aset_batt/ui/widgets.py's TrendCrosshair), added alongside the qt-material
UI upgrade. Exercises real widget wiring (registry re-application, actual
mouse-move scene coordinates, actual checkbox clicks) rather than just the
pure-function pieces, per this repo's testing convention.
"""
import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QLabel
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.ui.widgets import TrendContainer

_app = QApplication.instance() or QApplication([])


def _sample_trend():
    tc = TrendContainer()
    tc.set_default_ranges(60, 30, 60)
    t = [i * 0.5 for i in range(40)]
    v = [12 + 0.1 * math.sin(x) for x in t]
    i = [1.0 + 0.05 * math.cos(x) for x in t]
    temp = [25 + 0.2 * x for x in t]
    tc.update(t, v, i, temp)
    return tc, t, v, i, temp


class TestRetheme(unittest.TestCase):
    def tearDown(self):
        theme.set_theme("light")

    def test_retheme_updates_module_constants(self):
        theme.set_theme("light")
        light_bg = theme.BG
        theme.retheme("dark")
        self.assertNotEqual(theme.BG, light_bg)
        self.assertEqual(theme.current_theme(), "dark")

    def test_retheme_reapplies_registered_stylesheets(self):
        theme.set_theme("light")
        lbl = QLabel()
        theme.style(lbl, lambda: f"color:{theme.MUTED};")
        light_style = lbl.styleSheet()
        theme.retheme("dark")
        self.assertNotEqual(lbl.styleSheet(), light_style)
        self.assertIn(theme.MUTED, lbl.styleSheet())

    def test_retheme_survives_a_garbage_collected_widget(self):
        """theme.style() only weakrefs the widget — a widget that goes away
        without being unregistered must not crash a later retheme()."""
        import gc
        theme.set_theme("light")
        lbl = QLabel()
        theme.style(lbl, lambda: f"color:{theme.MUTED};")
        del lbl
        gc.collect()
        theme.retheme("dark")  # must not raise

    def test_trend_container_retheme_updates_graph_pens(self):
        tc, *_ = _sample_trend()
        theme.set_theme("light")
        tc.retheme()
        light_pen_color = tc._combined.c_v.opts["pen"].color().name()
        theme.retheme("dark")
        tc.retheme()
        dark_pen_color = tc._combined.c_v.opts["pen"].color().name()
        self.assertNotEqual(light_pen_color, dark_pen_color)
        self.assertEqual(dark_pen_color, theme.INFO.lower())

    def test_theme_checkbox_toggle_saves_config_and_rethemes_live(self):
        theme.set_theme("light")
        cfg = ConfigManager()
        cfg.system.ui_theme = "light"
        win = BatteryQtWindow(cfg)
        try:
            self.assertTrue(hasattr(win, "chk_dark_theme"),
                             "Dark theme checkbox (TOOLS tab) must be built and reachable")
            self.assertFalse(win.chk_dark_theme.isChecked())
            status_before = win.status_label.styleSheet()

            win.chk_dark_theme.setChecked(True)

            self.assertEqual(win.config.system.ui_theme, "dark")
            self.assertEqual(theme.current_theme(), "dark")
            # A widget registered via theme.style() in _build_statusbar should
            # have actually repainted, not just the module constants changing.
            self.assertNotEqual(win.status_label.styleSheet(), status_before)
        finally:
            win.close()

    def test_metric_cards_do_not_go_stale_when_idle_across_a_toggle(self):
        """Regression: Voltage/Current/SoC/Rin/Temp value labels are colored
        once at construction (or only re-colored when live telemetry arrives).
        With the app idle (no test running, no fresh samples), toggling the
        theme used to leave them showing whatever color was picked under the
        OLD theme — worst case, white text a light theme's near-white card
        background renders as invisible."""
        theme.set_theme("dark")
        cfg = ConfigManager()
        cfg.system.ui_theme = "dark"
        win = BatteryQtWindow(cfg)
        try:
            # Simulate the pre-test Connect readback (idle state, no test running).
            win._slot_live_readback(12.40, 0.0, 25.0)
            dark_text_color = theme.TEXT
            for name in ("Voltage", "Current", "SoC", "Temp"):
                lbl, _ = win.metric_labels[name]
                self.assertIn(dark_text_color, lbl.styleSheet())

            win.chk_dark_theme.setChecked(False)  # -> light, no new telemetry in between

            light_text_color = theme.TEXT
            self.assertNotEqual(dark_text_color, light_text_color)
            for name in ("Voltage", "Current", "SoC", "Temp"):
                lbl, _ = win.metric_labels[name]
                self.assertIn(light_text_color, lbl.styleSheet(),
                              f"{name} label still shows the old theme's color while idle")
                self.assertNotIn(dark_text_color, lbl.styleSheet())
        finally:
            win.close()

    def test_characterize_status_labels_track_both_state_and_theme(self):
        """Regression: CHARACTERIZE tab's Peukert/ETA/GITT/CCA status labels are
        only ever styled from the event-driven _slot_char_update slot. A first
        pass at fixing their idle-placeholder staleness (baseline theme.style()
        registration) would have unconditionally reset them to MUTED on every
        retheme() — silently erasing a completed test's ✓/✗ color. Verify both
        directions: a completed result keeps its OK/CRIT color (just updated
        for the new theme), and a never-run test still gets the fresh MUTED
        placeholder."""
        theme.set_theme("dark")
        cfg = ConfigManager()
        cfg.system.ui_theme = "dark"
        win = BatteryQtWindow(cfg)
        try:
            win._slot_char_update("pk", "✓ k=1.12 (R²=0.98)")
            dark_ok = theme.OK
            self.assertIn(dark_ok, win.lbl_char_pk_status.styleSheet())
            # eta/gitt/cca never ran — still the construction-time placeholder.
            dark_muted = theme.MUTED
            for attr in ("lbl_char_eta_status", "lbl_char_gitt_status", "lbl_char_cca_status"):
                self.assertIn(dark_muted, getattr(win, attr).styleSheet())

            win.chk_dark_theme.setChecked(False)  # -> light, no new test event

            light_ok = theme.OK
            light_muted = theme.MUTED
            self.assertNotEqual(dark_ok, light_ok)
            self.assertIn(light_ok, win.lbl_char_pk_status.styleSheet(),
                          "completed ✓ result must keep its OK color, just retheme'd")
            self.assertNotIn(dark_ok, win.lbl_char_pk_status.styleSheet())
            for attr in ("lbl_char_eta_status", "lbl_char_gitt_status", "lbl_char_cca_status"):
                self.assertIn(light_muted, getattr(win, attr).styleSheet())
        finally:
            win.close()

    def test_estop_pill_resets_to_idle_on_successful_reconnect(self):
        """Regression: _slot_safety latched the state pill at "ESTOP"/CRIT with
        no code path anywhere ever resetting it — the operator's explicit
        reconnect (the resume action after a safety trip) must restore
        IDLE/NEUTRAL."""
        from unittest.mock import MagicMock
        theme.set_theme("light")
        win = BatteryQtWindow(ConfigManager())
        try:
            win._slot_safety("test trip")
            self.assertEqual(win.state_pill.text().strip(), "ESTOP")
            self.assertIn(theme.CRIT, win.state_pill.styleSheet())

            win.hw = MagicMock()
            win.hw.apply_default_safety_protection.return_value = {"warnings": [], "info": {}}
            win.cb_psu.addItem("COM1")
            win.cb_load.addItem("COM2")
            win.cb_psu.setCurrentIndex(win.cb_psu.count() - 1)
            win.cb_load.setCurrentIndex(win.cb_load.count() - 1)
            win._on_connect()
            _app.processEvents()   # sig_profile_status delivery

            self.assertEqual(win.state_pill.text().strip(), "IDLE")
            self.assertIn(theme.NEUTRAL, win.state_pill.styleSheet())
        finally:
            win.close()

    def test_ica_plot_background_follows_retheme(self):
        """Regression: plot_ica is a standalone pyqtgraph widget outside
        TrendContainer's retheme() — its background froze at whichever theme
        was active at construction (a stark white rectangle in dark mode)."""
        theme.set_theme("dark")
        cfg = ConfigManager()
        cfg.system.ui_theme = "dark"
        win = BatteryQtWindow(cfg)
        try:
            dark_bg = win.plot_ica.backgroundBrush().color().name()
            win.chk_dark_theme.setChecked(False)  # -> light
            light_bg = win.plot_ica.backgroundBrush().color().name()
            self.assertNotEqual(dark_bg, light_bg)
            # Plot canvases use the dedicated GRAPH_BG role (near-white in the
            # light theme for report screenshots), not the PANEL2 shell surface.
            self.assertEqual(light_bg, theme.GRAPH_BG.lower())
        finally:
            win.close()

    def test_grade_bar_and_ecm_button_track_both_state_and_theme(self):
        """Regression: lbl_grade (the big grade bar) and btn_ecm_toggle are
        styled once at construction, then restyled by _slot_analysis_done with
        STATE-dependent colors (grade color / identified-ECM accent border). A
        retheme must recolor for the new palette WITHOUT losing that state."""
        theme.set_theme("dark")
        cfg = ConfigManager()
        cfg.system.ui_theme = "dark"
        win = BatteryQtWindow(cfg)
        try:
            # Pristine: neutral placeholder that follows the theme.
            self.assertIn(theme.PANEL, win.lbl_grade.styleSheet())
            self.assertIn(theme.MUTED, win.btn_ecm_toggle.styleSheet())

            # Simulate _slot_analysis_done's state updates (grade A, ECM found).
            win._last_grade = "A"
            win._last_soh_valid = True
            win._ecm_identified = True
            win._apply_final_metric_styles()
            win.btn_ecm_toggle.setStyleSheet(win._ecm_toggle_style())
            dark_ok, dark_info = theme.OK, theme.INFO
            self.assertIn(dark_ok, win.lbl_grade.styleSheet())
            self.assertIn(dark_info, win.btn_ecm_toggle.styleSheet())

            win.chk_dark_theme.setChecked(False)  # -> light, no new analysis

            self.assertNotEqual(dark_ok, theme.OK)
            self.assertIn(theme.OK, win.lbl_grade.styleSheet(),
                          "grade A bar must stay grade-colored, just retheme'd")
            self.assertIn(theme.INFO, win.btn_ecm_toggle.styleSheet(),
                          "identified-ECM accent border must survive retheme")
            grade_lbl, _ = win.metric_labels_final["Grade"]
            self.assertIn(theme.OK, grade_lbl.styleSheet())
        finally:
            win.close()


class TestTrendCrosshair(unittest.TestCase):
    def tearDown(self):
        theme.set_theme("light")

    @staticmethod
    def _move_mouse(tc, plot, x_view, y_view=None):
        vb = plot.vb
        if y_view is None:
            y_view = sum(vb.viewRange()[1]) / 2
        scene_pt = vb.mapViewToScene(QPointF(x_view, y_view))
        tc._crosshair._on_mouse_moved(plot, scene_pt)

    def test_crosshair_snaps_to_nearest_sample_and_shows_values(self):
        tc, t, v, i, temp = _sample_trend()
        plot = tc._all_plots()[0]
        self._move_mouse(tc, plot, 10.0)

        line = tc._crosshair._lines[plot]
        self.assertTrue(line.isVisible())
        self.assertAlmostEqual(line.value(), 10.0)

        idx = min(range(len(t)), key=lambda k: abs(t[k] - 10.0))
        html = tc._crosshair._labels[plot].textItem.toHtml()
        self.assertIn(f"{v[idx]:.3f}", html)
        self.assertIn(f"{i[idx]:.3f}", html)

    def test_crosshair_synced_across_split_mode_subplots(self):
        tc, t, v, i, temp = _sample_trend()
        tc._btn_group.buttons()[1].click()  # Split 2
        plots = tc._all_plots()
        self.assertEqual(len(plots), 2)

        self._move_mouse(tc, plots[0], 5.0)

        for p in plots:
            self.assertTrue(tc._crosshair._lines[p].isVisible())
            self.assertAlmostEqual(tc._crosshair._lines[p].value(), 5.0)
        # Tooltip only shown on the subplot the mouse is actually over.
        self.assertTrue(tc._crosshair._labels[plots[0]].isVisible())
        self.assertFalse(tc._crosshair._labels[plots[1]].isVisible())

    def test_crosshair_rewires_on_every_mode_without_crash(self):
        tc, t, v, i, temp = _sample_trend()
        for idx in range(3):
            tc._btn_group.buttons()[idx].click()
            plots = tc._all_plots()
            self.assertTrue(plots)
            self.assertTrue(all(p in tc._crosshair._lines for p in plots))
            self._move_mouse(tc, plots[0], 3.0)
            self.assertTrue(tc._crosshair._lines[plots[0]].isVisible())

    def test_crosshair_hides_when_mouse_leaves_plot_area(self):
        tc, t, v, i, temp = _sample_trend()
        plot = tc._all_plots()[0]
        self._move_mouse(tc, plot, 10.0)
        self.assertTrue(tc._crosshair._lines[plot].isVisible())

        far_outside_scene = QPointF(-999999, -999999)
        tc._crosshair._on_mouse_moved(plot, far_outside_scene)
        self.assertFalse(tc._crosshair._lines[plot].isVisible())

    def test_crosshair_retheme_changes_line_color(self):
        tc, *_ = _sample_trend()
        theme.set_theme("light")
        plot = tc._all_plots()[0]
        self._move_mouse(tc, plot, 5.0)
        light_color = tc._crosshair._lines[plot].pen.color().name()

        theme.retheme("dark")
        tc.retheme()

        dark_color = tc._crosshair._lines[plot].pen.color().name()
        self.assertNotEqual(light_color, dark_color)


if __name__ == "__main__":
    unittest.main()
