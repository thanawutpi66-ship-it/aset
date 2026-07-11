"""
PySide6 ISA-101 HMI for ASET Battery Tester.

This is the supported desktop UI for the main application. It keeps the
existing controller / estimator / analysis contracts, but presents them in the
desaturated high-performance style used by the standalone command center.
"""

import csv
import logging
import math
import os
import threading
import webbrowser
from collections import deque
from datetime import datetime
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt, QThread, QRunnable, QThreadPool, QLocale, QByteArray
from PySide6.QtSvgWidgets import QSvgWidget

from aset_batt.acquisition.models import TestConfig, OperationMode, BatteryProfile as AcqProfile
from aset_batt.acquisition.backends import HardwareBackend
from aset_batt.acquisition.worker import AcquisitionWorker
import re
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QToolBar,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QProgressBar,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import aset_batt.core.battery_profiles as battery_profiles
from aset_batt.core.analysis_module import ChemistryDetector
from aset_batt.core.iec61960_standard import IEC61960Standard

logger = logging.getLogger(__name__)

# ISA-101 palette: neutral gray shell with color reserved for state/alarm only.
from aset_batt.ui import theme

from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html
from aset_batt.ui.zones import ZonesMixin
from aset_batt.ui.sequences import SequencesMixin
from aset_batt.ui.characterize import CharacterizeMixin

class UiSlotsMixin:
    def _connect_signals(self):
        self.sig_display.connect(self._slot_display)
        self.sig_profile_status.connect(self._slot_profile_status)
        self.sig_charge_status.connect(self._slot_charge_status)
        self.sig_button.connect(self._slot_button)
        self.sig_loading.connect(self._slot_loading)
        self.sig_conn.connect(self._slot_conn)
        self.sig_alarm.connect(self._log_alarm)
        self.sig_safety.connect(self._slot_safety)
        self.sig_profile_done.connect(self._slot_profile_done)
        self.sig_analysis_done.connect(self._slot_analysis_done)
        self.sig_workflow.connect(self._slot_workflow)
        self.sig_qs_workflow.connect(self._slot_qs_workflow)
        self.sig_hppc_seq_wf.connect(self._slot_hppc_seq_wf)
        self.sig_cycle_wf.connect(self._slot_cycle_wf)
        self.sig_wf_status.connect(self._slot_wf_status)
        # Mirror workflow phase changes to cloud dashboard
        _IEC   = (["prepare","charge","rest","discharge","analyze"], "IEC 61960", "IEC 61960 Standard")
        _QS    = (["ocv","rest","discharge","analyze"],              "Quick Scan", "Quick Scan")
        _HPPC  = (["prepare","charge","rest","test","analyze"],     "HPPC Sequence", "HPPC Full Sequence")
        _CYCLE = (["prepare","charge","discharge","test","analyze"], "Cycle Life", "Cycle Life")
        self.sig_workflow.connect(
            lambda s, st, _p=_IEC: self._slot_cloud_phase(s, st, *_p))
        self.sig_qs_workflow.connect(
            lambda s, st, _p=_QS: self._slot_cloud_phase(s, st, *_p))
        self.sig_hppc_seq_wf.connect(
            lambda s, st, _p=_HPPC: self._slot_cloud_phase(s, st, *_p))
        self.sig_cycle_wf.connect(
            lambda s, st, _p=_CYCLE: self._slot_cloud_phase(s, st, *_p))
        self.sig_phase_progress.connect(self._slot_phase_progress)
        self.sig_seq_result.connect(self._slot_seq_result)
        self.sig_seq_done.connect(self._slot_seq_done)
        self.sig_char_update.connect(self._slot_char_update)
        self.sig_live_readback.connect(self._slot_live_readback)
        self.sig_cycle_counter.connect(self.lbl_cycle_counter.setText)
        self.sig_seq_aborted.connect(self._on_seq_aborted)
        self.sig_update_available.connect(self._slot_update_available)
        self.sig_update_done.connect(self._slot_update_done)
    @Slot(float, float, float, float, float, float, int)
    def _slot_display(self, v, i, soc, rin, temp, soh, gen):
        # Drop a straggler from an already-stopped run: the monitor loop can
        # be mid-SCPI-read (real hardware only — Mock's near-zero latency
        # almost never hits this window) when stop_monitor() is requested,
        # still queuing one more update_display() call after a NEW
        # test/sequence has already cleared buf_t/buf_v and started its own
        # feed — without this check that stale sample lands in the fresh
        # buffer and renders as a second overlapping trace of the same color.
        if gen != self._run_generation:
            return
        import time
        rin_mohm = rin * 1000.0
        self._update_vi_temp_labels(v, i, temp)
        # SoC: show the EKF's live estimate WITH its 1σ uncertainty (±%), read from the
        # estimator covariance. Large ± early / on a flat plateau, tightening after an
        # OCV/endpoint anchor — so the operator knows how much to trust the number.
        soc_lbl, soc_unit = self.metric_labels["SoC"]
        soc_std = getattr(getattr(self, "estimator", None), "soc_std", None)
        if soc_std is not None and soc_std == soc_std:      # not None / NaN
            soc_lbl.setText(f"{soc:.1f} ±{min(soc_std, 99):.0f} {soc_unit}")
        else:
            soc_lbl.setText(f"{soc:.1f} {soc_unit}")
        # Rin: a DC resistance reading needs current flowing. At rest, (OCV−V)/I is
        # undefined and explodes on the flat LFP plateau → keep "pending" rather than
        # show a wild number. The final analysis fills the proper R0+R1.
        # The raw per-sample estimate is noisy, so display a smoothed value (reset at
        # rest) to keep the live number readable; the final analysis fills the proper
        # R0+R1. Adaptive alpha: a flat, heavy EMA (α=0.3) lags a genuine step change
        # (e.g. entering/leaving an HPPC pulse) by ~3 samples, showing a blurred value
        # right when Rin is changing fastest. Widen the weight on the new sample when
        # it disagrees with the trend by >15% (a real transient), keep the gentler
        # weight otherwise (steady-state jitter rejection) — tracks transients ~40%
        # faster without giving up noise rejection at steady state.
        rin_lbl, rin_unit = self.metric_labels["Rin"]
        if abs(i) >= 0.1:
            prev = getattr(self, "_rin_ema", None)
            if prev is None:
                self._rin_ema = rin_mohm
            else:
                rel_jump = abs(rin_mohm - prev) / max(1.0, prev)
                alpha = 0.6 if rel_jump > 0.15 else 0.3
                self._rin_ema = (1.0 - alpha) * prev + alpha * rin_mohm
            rin_lbl.setText(f"{self._rin_ema:.1f} {rin_unit}")
            rin_lbl.setStyleSheet(f"color:{theme.TEXT}; border:0;")  # no longer a "—" placeholder
        else:
            self._rin_ema = None                            # reset smoothing between loads
        # SoH is intentionally NOT updated here — it is a final-analysis metric,
        # written once by _on_test_finished. (soh arg is kept for signal compatibility.)

        # perf_counter (monotonic): this is an interval ("time since monitor start"),
        # not a real timestamp, so it should never use wall-clock — an NTP/clock jump
        # would otherwise offset every point already plotted on the graph's X-axis.
        if self._elapsed_t0 is None:
            self._elapsed_t0 = time.perf_counter()
        elapsed = time.perf_counter() - self._elapsed_t0

        self.buf_t.append(elapsed)
        self.buf_v.append(v)
        self.buf_i.append(i)
        self.buf_soc.append(soc)
        self.buf_rin.append(rin_mohm)
        self.buf_temp.append(temp)
        self._trim_trend_buffers()
        self._sample_index += 1
        # Redraw the graph at most ~5 Hz. The monitor loop feeds this at up to 10 Hz
        # during CHARGE — repainting pyqtgraph curves (and converting the deques to
        # lists) on every single sample is wasted work no one can see, and its cost
        # grows with buffer length, so throttling here is what actually keeps the UI
        # responsive over a multi-hour test rather than progressively laggier.
        _now_redraw = time.perf_counter()
        if _now_redraw - self._last_trend_redraw >= 0.2:
            self._last_trend_redraw = _now_redraw
            self.trend.update(list(self.buf_t), list(self.buf_v), list(self.buf_i), list(self.buf_temp))

        self._set_temp_label_color(temp)
        i_dir = "CHG" if i < -self._I_IDLE else "DSG" if i > self._I_IDLE else "REST"
        self.status_label.setText(
            f"V={v:.3f} V  I={abs(i):.3f} A ({i_dir})  SoC={soc:.1f}%  Rin={rin_mohm:.1f} mΩ  Temp={temp:.1f} °C"
        )
    @Slot(float, float, float)
    def _slot_live_readback(self, v, i, temp):
        """Pre-test Connect readback: shows Voltage/Current/Temp immediately after
        Connect succeeds, before any test is running. No SoC/Rin (needs the state
        estimator), no CSV logging, no graph buffer — those stay owned by the real
        test's _slot_display so the recorded session isn't polluted with idle data."""
        self._update_vi_temp_labels(v, i, temp)
        self._set_temp_label_color(temp)
    @Slot(str, str)
    def _slot_profile_status(self, text, color):
        # lbl_profile_status belonged to the legacy IEC PROFILES zone removed in
        # e7e9ab4 — a leftover reference here raised AttributeError and killed
        # the slot before the state-pill update below ever ran (Qt swallows slot
        # exceptions, so the pill just silently stopped tracking Run/E-STOP/Idle).
        self.state_pill.setText(f"  {text.upper()}  ")
        self.state_pill.setStyleSheet(self._pill(self._pill_color_for(text)))
        # Lock hardware disconnect during active test runs
        is_idle = any(x in text.upper() for x in ["IDLE", "STOP", "FAIL", "DONE", "REVIEW"])
        
        # Update active SN display
        self._refresh_sn_badge()


        if hasattr(self, 'btn_disconnect'):
            self.btn_disconnect.setEnabled(is_idle)
            self.btn_connect.setEnabled(is_idle)
            self.cb_psu.setEnabled(is_idle)
            self.cb_load.setEnabled(is_idle)
            self.cb_esp.setEnabled(is_idle)
            if hasattr(self, 'cb_product'):
                self.cb_product.setEnabled(is_idle)
            if hasattr(self, 'ed_sn'):
                self.ed_sn.setEnabled(is_idle)
    @Slot(str)
    def _slot_charge_status(self, text):
        self.lbl_charge.setText(text)
    @Slot(str, bool)
    def _slot_button(self, key, enabled):
        b = self._buttons.get(key)
        if b is not None:
            b.setEnabled(enabled)
    @Slot(str, bool, str)
    def _slot_loading(self, key, loading, text):
        b = self._buttons.get(key)
        if b is None:
            return
        if loading:
            b._orig = b.text()
            b.setText(text or "…")
            b.setEnabled(False)
        else:
            b.setText(getattr(b, "_orig", b.text()))
            b.setEnabled(True)
    @Slot()
    def _slot_conn(self):
        connected  = bool(getattr(self.hw, "is_connected", False))
        psu_ok     = bool(getattr(self.hw, "is_psu_connected", False))
        load_ok    = bool(getattr(self.hw, "is_load_connected", False))
        esp_ok     = bool(getattr(self.hw, "is_esp_connected", False))
        conn_err   = getattr(self.hw, "connect_error", "")
        esp_err    = getattr(self.hw, "esp_connect_error", "")
        # Header LED
        if connected:
            led_color, conn_label = theme.OK, "Connected"
        elif conn_err:
            led_color, conn_label = theme.CRIT, "Connection Failed"
        else:
            led_color, conn_label = theme.NEUTRAL, "Disconnected"
        self.conn_led.setStyleSheet(f"color:{led_color}; font-size:16px;")
        self.conn_text.setText(conn_label)
        self.conn_text.setStyleSheet(f"color:{led_color}; font-weight:600;")
        
        # Session ID update
        import os
        if getattr(self, "lbl_session", None):
            csv_path = None
            if getattr(self, "controller", None) and getattr(self.controller, "data", None):
                csv_path = self.controller.data.current_path
            if csv_path:
                self.lbl_session.setText(f"Session: {os.path.basename(csv_path)}")
                self.lbl_session.show()
            else:
                self.lbl_session.hide()

        if connected:
            self.status_label.setText("Hardware connected")
        elif conn_err:
            self.status_label.setText(f"เชื่อมต่อล้มเหลว: {conn_err.splitlines()[0]}")
        else:
            self.status_label.setText("Ready — connect hardware to begin")
        # Per-port LEDs: ✓ connected | ✗ error | ● idle
        def _set_led(lbl, ok, err, tip_ok, tip_err, tip_no):
            if ok:
                lbl.setText("✓")
                lbl.setStyleSheet(f"color:{theme.OK}; font-size:13px; min-width:18px; font-weight:700;")
                lbl.setToolTip(tip_ok)
            elif err:
                lbl.setText("✗")
                lbl.setStyleSheet(f"color:{theme.CRIT}; font-size:13px; min-width:18px; font-weight:700;")
                lbl.setToolTip(tip_err)
            else:
                lbl.setText("●")
                lbl.setStyleSheet(f"color:{theme.NEUTRAL}; font-size:15px; min-width:18px;")
                lbl.setToolTip(tip_no)
        _set_led(self.led_psu,  psu_ok,   conn_err and not psu_ok,  "PSU connected",   conn_err,  "PSU: not connected")
        _set_led(self.led_load, load_ok,  conn_err and not load_ok, "Load connected",  conn_err,  "Load: not connected")
        _set_led(self.led_esp,  esp_ok,    esp_err,  "ESP32 connected", esp_err,   "ESP32: not connected")
        # SSR relay LED — normally automatic (follows charge state); manual
        # ON/OFF buttons only enabled while ESP32 is actually connected.
        # Green=ON (charging), red=OFF (not charging / cut), gray=unknown.
        if hasattr(self, "btn_ssr_on"):
            self.btn_ssr_on.setEnabled(esp_ok)
            self.btn_ssr_off.setEnabled(esp_ok)
        if hasattr(self, "led_ssr"):
            ssr_state = getattr(self.hw, "ssr_state", None)
            if not esp_ok or ssr_state is None:
                self.led_ssr.setText("●")
                self.led_ssr.setStyleSheet(f"color:{theme.NEUTRAL}; font-size:15px; min-width:18px;")
                self.led_ssr.setToolTip("SSR: unknown / ESP32 not connected")
                self.lbl_ssr_state.setText("—")
                self.lbl_ssr_state.setStyleSheet(f"color:{theme.MUTED}; font-weight:600;")
            elif ssr_state:
                self.led_ssr.setText("✓")
                self.led_ssr.setStyleSheet(f"color:{theme.OK}; font-size:13px; min-width:18px; font-weight:700;")
                self.led_ssr.setToolTip("SSR: ON (charging — power connected)")
                self.lbl_ssr_state.setText("ON (charging)")
                self.lbl_ssr_state.setStyleSheet(f"color:{theme.OK}; font-weight:600;")
            else:
                self.led_ssr.setText("✗")
                self.led_ssr.setStyleSheet(f"color:{theme.CRIT}; font-size:13px; min-width:18px; font-weight:700;")
                self.led_ssr.setToolTip("SSR: OFF (power cut)")
                self.lbl_ssr_state.setText("OFF")
                self.lbl_ssr_state.setStyleSheet(f"color:{theme.CRIT}; font-weight:600;")
    @Slot(str)
    def _slot_safety(self, reason):
        self._log_alarm(f"⛔ SAFETY: {reason}")
        self.state_pill.setText("  ESTOP  ")
        self.state_pill.setStyleSheet(self._pill(theme.CRIT))
        if not self._headless:
            QMessageBox.critical(self, "Safety Triggered", f"System safety triggered:\n{reason}\n\nAll operations stopped.")
    @Slot(object)
    def _slot_profile_done(self, data):
        success = data if isinstance(data, bool) else data.get("success", False)
        if success and isinstance(data, dict) and data.get("report") and not self._headless:
            self._show_text_dialog("IEC 61960 Test Report", data["report"])
        elif success:
            self._log_alarm("Profile completed.")
        else:
            err = data.get("error", "") if isinstance(data, dict) else ""
            self._log_alarm(err or "Profile stopped.")
    @Slot(object)
    def _slot_analysis_done(self, result):
        """Display a unified-analysis result (dict). Same renderer as a live test
        — Analyze-CSV and the controller's auto-analyze both arrive here."""
        if not isinstance(result, dict) or "error" in result:
            msg = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
            self.lbl_analytics.setText(f"Analysis failed: {msg}")
            self._log_alarm(f"Analysis failed: {msg}")
            return
        self._last_analysis = result
        self._on_test_finished(result)

    @Slot()
    def _on_admin_toggle(self):
        is_admin = self.btn_admin.isChecked()
        if is_admin:
            # Simple passcode prompt
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            text, ok = QInputDialog.getText(self, "Admin Login", "Enter Admin PIN:", QLineEdit.EchoMode.Password)
            if ok and text == "2547":
                self.btn_admin.setText("Admin 🔒")
                self.btn_admin.setStyleSheet(
                    f"QPushButton {{ background:{theme.INFO}; color:white; border:1px solid {theme.INFO}; border-radius:4px; padding:4px 10px; font-size:12px; font-weight:700; margin-right: 10px; }}"
                )
                self.grp_advanced.setEnabled(True)
                if hasattr(self, "btn_ssr_on"): self.btn_ssr_on.setVisible(True)
                if hasattr(self, "btn_ssr_off"): self.btn_ssr_off.setVisible(True)
                if hasattr(self, "btn_edit_profile"): self.btn_edit_profile.setVisible(True)
                if hasattr(self, "grp_calibration"): self.grp_calibration.setVisible(True)
            else:
                self.btn_admin.setChecked(False)
                if ok:
                    QMessageBox.warning(self, "Error", "Incorrect PIN.")
        else:
            self.btn_admin.setText("Operator 🔓")
            self.btn_admin.setStyleSheet(
                f"QPushButton {{ background:{theme.PANEL2}; color:{theme.TEXT}; border:1px solid {theme.BORDER}; border-radius:4px; padding:4px 10px; font-size:12px; font-weight:700; margin-right: 10px; }}"
            )
            self.grp_advanced.setEnabled(False)
            if hasattr(self, "btn_ssr_on"): self.btn_ssr_on.setVisible(False)
            if hasattr(self, "btn_ssr_off"): self.btn_ssr_off.setVisible(False)
            if hasattr(self, "btn_edit_profile"): self.btn_edit_profile.setVisible(False)
            if hasattr(self, "grp_calibration"): self.grp_calibration.setVisible(False)

    @Slot()
    def _on_save_calibration(self):
        # Read from spinboxes
        psu_v = self.spn_psu_v_offset.value()
        psu_i = self.spn_psu_i_offset.value()
        load_v = self.spn_load_v_offset.value()
        load_i = self.spn_load_i_offset.value()
        
        # Save to config
        self.config.system.psu_v_offset = psu_v
        self.config.system.psu_i_offset = psu_i
        self.config.system.load_v_offset = load_v
        self.config.system.load_i_offset = load_i
        self.config.save_config()
        
        # Apply to hardware driver if running
        if self.hw:
            self.hw.apply_calibration(psu_v, psu_i, load_v, load_i)
            
        QMessageBox.information(self, "Calibration Saved", "Hardware calibration offsets saved successfully.")

    def _load_calibration(self):
        # Load from config to spinboxes
        if hasattr(self.config.system, "psu_v_offset"):
            self.spn_psu_v_offset.setValue(self.config.system.psu_v_offset)
            self.spn_psu_i_offset.setValue(self.config.system.psu_i_offset)
            self.spn_load_v_offset.setValue(self.config.system.load_v_offset)
            self.spn_load_i_offset.setValue(self.config.system.load_i_offset)
            # Apply to hardware driver immediately
            if self.hw:
                self.hw.apply_calibration(
                    self.config.system.psu_v_offset,
                    self.config.system.psu_i_offset,
                    self.config.system.load_v_offset,
                    self.config.system.load_i_offset
                )