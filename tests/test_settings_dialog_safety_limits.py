"""OVP/UVP/OTP/UTP safety-limit editing (ก.ค. 2026): check_safety_limits()
(auto_controller.py) already enforces all four on every monitor-loop sample,
they just had no UI — only editable by hand-editing config.json.

Went through two prior designs before landing here:
1. Fields in SettingsDialog (Tools -> Preferences) — too easy to miss, and
   dialogs.py's _on_open_settings crashed with a NameError before the dialog
   could even open (no import of SettingsDialog anywhere in that module —
   never caught because earlier tests imported SettingsDialog directly
   instead of going through the menu action).
2. A "Edit Safety Limits…" button on the SETUP tab opening a popup dialog —
   still an extra click/window the operator didn't want.

Now: five QDoubleSpinBox fields live directly inline on the SETUP tab
(zones.py._zone_setup), with a "Save Limits" button next to them
(hardware_control.py._on_save_safety_limits). Product selection / Detect
Chemistry can also update safety_limits programmatically (see
_on_product_changed) — _refresh_battery_readout() pushes those values back
into the same spinboxes so they never show stale numbers.
"""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow, SettingsDialog

_app = QApplication.instance() or QApplication([])


def _make_window():
    return BatteryQtWindow(ConfigManager())


# ---------------------------------------------------------------------------
# Inline SETUP-tab fields
# ---------------------------------------------------------------------------

def test_setup_tab_prefills_spinboxes_from_config():
    """Spinboxes are populated at _zone_setup() build time from self.config —
    set safety_limits on the ConfigManager BEFORE the window is built."""
    cfg = ConfigManager()
    cfg.system.safety_limits = {
        "max_voltage": 16.2, "min_voltage": 9.8, "max_current": 90.0,
        "max_temperature": 55.0, "min_temperature": -8.0,
    }
    w = BatteryQtWindow(cfg)
    try:
        assert w.spn_ovp.value() == 16.2
        assert w.spn_uvp.value() == 9.8
        assert w.spn_max_current.value() == 90.0
        assert w.spn_otp.value() == 55.0
        assert w.spn_utp.value() == -8.0
    finally:
        w.close()


def test_save_rejects_ovp_below_uvp_without_saving():
    w = _make_window()
    try:
        w.spn_ovp.setValue(5.0)
        w.spn_uvp.setValue(10.0)
        before = dict(w.config.system.safety_limits)
        # _headless (like _char_guard's pattern) suppresses the popup under
        # offscreen pytest, but the rejection itself must still hold — force
        # non-headless to also exercise the popup branch a real GUI takes.
        w._headless = False
        with patch("aset_batt.ui.views.hardware_control.QMessageBox.warning") as mock_warn:
            w._on_save_safety_limits()
        mock_warn.assert_called_once()
        assert w.config.system.safety_limits == before
    finally:
        # _headless=False makes closeEvent() take the QMessageBox.question()
        # confirm branch instead of its headless fast-path — unpatched, that
        # blocks forever in a real modal loop. Reset before close().
        w._headless = True
        w.close()


def test_save_rejects_otp_below_utp_without_saving():
    w = _make_window()
    try:
        w.spn_otp.setValue(-20.0)
        w.spn_utp.setValue(0.0)
        before = dict(w.config.system.safety_limits)
        w._headless = False
        with patch("aset_batt.ui.views.hardware_control.QMessageBox.warning") as mock_warn:
            w._on_save_safety_limits()
        mock_warn.assert_called_once()
        assert w.config.system.safety_limits == before
    finally:
        w._headless = True
        w.close()


def test_save_persists_valid_limits_to_disk():
    w = _make_window()
    try:
        w.spn_ovp.setValue(16.5)
        w.spn_uvp.setValue(9.5)
        w.spn_max_current.setValue(120.0)
        w.spn_otp.setValue(65.0)
        w.spn_utp.setValue(-5.0)
        with patch.object(w.config, "save_config") as mock_save:
            w._on_save_safety_limits()
        mock_save.assert_called_once()
        limits = w.config.system.safety_limits
        assert limits["max_voltage"] == 16.5
        assert limits["min_voltage"] == 9.5
        assert limits["max_current"] == 120.0
        assert limits["max_temperature"] == 65.0
        assert limits["min_temperature"] == -5.0
    finally:
        w.close()


def test_save_logs_an_alarm_line_confirming_the_new_values():
    w = _make_window()
    try:
        w.spn_otp.setValue(58.0)
        with patch.object(w.config, "save_config"), \
             patch.object(w, "_log_alarm") as mock_log:
            w._on_save_safety_limits()
        assert any("58.0" in str(c) for c in mock_log.call_args_list)
    finally:
        w.close()


def test_refresh_battery_readout_pushes_config_into_spinboxes():
    """Simulates _on_product_changed's chemistry-based OVP/UVP override,
    then confirms the SETUP-tab fields reflect it instead of showing stale
    numbers the operator never touched."""
    w = _make_window()
    try:
        w.config.system.safety_limits["max_voltage"] = 14.7
        w.config.system.safety_limits["min_voltage"] = 10.5
        w._refresh_battery_readout()
        assert w.spn_ovp.value() == 14.7
        assert w.spn_uvp.value() == 10.5
    finally:
        w.close()


def test_no_leftover_popup_dialog_or_button():
    """Regression against reintroducing the popup design — the fields must
    stay inline on SETUP, not behind another dialog/button."""
    w = _make_window()
    try:
        assert not hasattr(w, "btn_edit_safety_limits")
        assert not hasattr(w, "lbl_safety_limits")
        import aset_batt.ui.views.hardware_control as hc
        assert not hasattr(hc, "SafetyLimitsDialog")
    finally:
        w.close()


# ---------------------------------------------------------------------------
# SettingsDialog (Tools -> Preferences) — appearance/cloud only; also
# regression-covers the NameError that made this menu action unreachable.
# ---------------------------------------------------------------------------

def test_on_open_settings_does_not_raise_nameerror():
    """dialogs.py's _on_open_settings used SettingsDialog with no import in
    scope anywhere in that module — clicking Tools -> Preferences raised
    NameError before the dialog could ever be constructed."""
    w = _make_window()
    try:
        with patch("aset_batt.ui.isa101_views.SettingsDialog") as mock_dlg_cls:
            mock_dlg_cls.return_value.exec.return_value = None
            w._on_open_settings()
            mock_dlg_cls.assert_called_once_with(w)
    finally:
        w.close()


def test_appearance_and_cloud_fields_use_real_systemconfig_attrs():
    """Regression: the dialog used to read/write dark_mode/cloud_push/cloud_url,
    none of which exist on SystemConfig — opening it raised AttributeError
    immediately."""
    w = _make_window()
    try:
        w.config.system.ui_theme = "dark"
        w.config.system.cloud_push_enabled = True
        w.config.system.cloud_dashboard_url = "https://example.test"
        dlg = SettingsDialog(w)
        assert dlg.cb_dark.isChecked() is True
        assert dlg.cb_push.isChecked() is True
        assert dlg.ed_url.text() == "https://example.test"

        dlg.cb_dark.setChecked(False)
        with patch("aset_batt.ui.isa101_views.QMessageBox.information"), \
             patch.object(w.config, "save_config"):
            dlg.accept()
        assert w.config.system.ui_theme == "light"
    finally:
        w.close()


def test_settings_dialog_no_longer_has_safety_limit_fields():
    """Safety limits live inline on the SETUP tab now — this dialog should
    not carry duplicate spinboxes for the same config values."""
    w = _make_window()
    try:
        dlg = SettingsDialog(w)
        assert not hasattr(dlg, "spn_ovp")
        assert not hasattr(dlg, "spn_uvp")
        assert not hasattr(dlg, "spn_otp")
        assert not hasattr(dlg, "spn_utp")
    finally:
        w.close()
