"""OVP/UVP/OTP/UTP safety-limit editing (ก.ค. 2026): check_safety_limits()
(auto_controller.py) already enforces all four on every monitor-loop sample,
they just had no UI — only editable by hand-editing config.json.

First pass put the fields in SettingsDialog (Tools -> Preferences), but that
menu path was easy to miss and (separately) crashed with a NameError before
ever reaching the dialog (dialogs.py's _on_open_settings referenced
SettingsDialog with no import in scope — never exercised by a test that
imports SettingsDialog directly instead of going through the menu action).
Moved to a dedicated SafetyLimitsDialog opened via a button right on the
SETUP tab, next to the [SAFETY] readout label, for direct visibility.
"""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow, SettingsDialog
from aset_batt.ui.views.hardware_control import SafetyLimitsDialog

_app = QApplication.instance() or QApplication([])


def _make_window():
    return BatteryQtWindow(ConfigManager())


# ---------------------------------------------------------------------------
# SafetyLimitsDialog — opened from the SETUP tab
# ---------------------------------------------------------------------------

def test_dialog_opens_without_crashing_and_prefills_from_config():
    w = _make_window()
    try:
        w.config.system.safety_limits = {
            "max_voltage": 15.0, "min_voltage": 10.0, "max_current": 100.0,
            "max_temperature": 60.0, "min_temperature": -10.0,
        }
        dlg = SafetyLimitsDialog(w)
        assert dlg.spn_ovp.value() == 15.0
        assert dlg.spn_uvp.value() == 10.0
        assert dlg.spn_max_current.value() == 100.0
        assert dlg.spn_otp.value() == 60.0
        assert dlg.spn_utp.value() == -10.0
    finally:
        w.close()


def test_accept_rejects_ovp_below_uvp_without_saving():
    w = _make_window()
    try:
        dlg = SafetyLimitsDialog(w)
        dlg.spn_ovp.setValue(5.0)
        dlg.spn_uvp.setValue(10.0)
        before = dict(w.config.system.safety_limits)
        with patch("aset_batt.ui.views.hardware_control.QMessageBox.warning") as mock_warn:
            dlg.accept()
        mock_warn.assert_called_once()
        assert w.config.system.safety_limits == before
        assert dlg.result() == 0   # not accepted — dialog stayed open
    finally:
        w.close()


def test_accept_rejects_otp_below_utp_without_saving():
    w = _make_window()
    try:
        dlg = SafetyLimitsDialog(w)
        dlg.spn_otp.setValue(-20.0)
        dlg.spn_utp.setValue(0.0)
        before = dict(w.config.system.safety_limits)
        with patch("aset_batt.ui.views.hardware_control.QMessageBox.warning") as mock_warn:
            dlg.accept()
        mock_warn.assert_called_once()
        assert w.config.system.safety_limits == before
    finally:
        w.close()


def test_accept_saves_valid_limits_and_persists_to_disk():
    w = _make_window()
    try:
        dlg = SafetyLimitsDialog(w)
        dlg.spn_ovp.setValue(16.5)
        dlg.spn_uvp.setValue(9.5)
        dlg.spn_max_current.setValue(120.0)
        dlg.spn_otp.setValue(65.0)
        dlg.spn_utp.setValue(-5.0)
        with patch.object(w.config, "save_config") as mock_save:
            dlg.accept()
        mock_save.assert_called_once()
        limits = w.config.system.safety_limits
        assert limits["max_voltage"] == 16.5
        assert limits["min_voltage"] == 9.5
        assert limits["max_current"] == 120.0
        assert limits["max_temperature"] == 65.0
        assert limits["min_temperature"] == -5.0
        assert dlg.result() == 1   # QDialog.Accepted
    finally:
        w.close()


def test_accept_refreshes_the_live_safety_label():
    w = _make_window()
    try:
        dlg = SafetyLimitsDialog(w)
        dlg.spn_otp.setValue(72.0)
        with patch.object(w.config, "save_config"):
            dlg.accept()
        assert "72.0" in w.lbl_safety_limits.text()
        assert "OTP" in w.lbl_safety_limits.text()
        assert "UTP" in w.lbl_safety_limits.text()
    finally:
        w.close()


def test_setup_tab_has_edit_safety_limits_button_wired_to_dialog():
    w = _make_window()
    try:
        assert hasattr(w, "btn_edit_safety_limits")
        with patch("aset_batt.ui.views.hardware_control.SafetyLimitsDialog") as mock_dlg_cls:
            mock_dlg_cls.return_value.exec.return_value = None
            w.btn_edit_safety_limits.click()
            mock_dlg_cls.assert_called_once_with(w)
            mock_dlg_cls.return_value.exec.assert_called_once()
    finally:
        w.close()


# ---------------------------------------------------------------------------
# SettingsDialog (Tools -> Preferences) — appearance/cloud only now; also
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
    """Safety limits moved to SafetyLimitsDialog (SETUP tab) — this dialog
    should not carry duplicate spinboxes for the same config values."""
    w = _make_window()
    try:
        dlg = SettingsDialog(w)
        assert not hasattr(dlg, "spn_ovp")
        assert not hasattr(dlg, "spn_uvp")
        assert not hasattr(dlg, "spn_otp")
        assert not hasattr(dlg, "spn_utp")
    finally:
        w.close()
