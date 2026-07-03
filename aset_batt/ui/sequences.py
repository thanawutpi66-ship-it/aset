"""
Automated test sequences: workflow-guide slots, pre-test dialogs, safety
helpers, and the four background sequence threads (IEC auto, Quick Scan,
HPPC full sequence, Cycle Life).
Mixin for BatteryQtWindow — methods only, no state or signals of its own.
All attributes/signals it references live on BatteryQtWindow (which mixes
this in before QMainWindow). Split out of isa101_views.py purely to keep
file sizes and merge collisions down; `self` is still the one window object.
Same import-order caveat as isa101_views: theme.set_theme() must run first.
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
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QDoubleSpinBox,
    QProgressBar,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QHeaderView,
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

from aset_batt.ui.theme import (
    BG, PANEL, PANEL2, FIELD, BORDER, TEXT, MUTED, OK, WARN, CRIT, INFO, NEUTRAL,
)
from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim, DigitalReadout, TemperatureGauge,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html

logger = logging.getLogger(__name__)


class SequencesMixin:
    # ---- Workflow guide slots -----------------------------------------------

    @Slot(int, str)
    def _slot_cloud_phase(self, step: int, state: str,
                          phases: list, test_mode: str, workflow: str):
        """Mirror workflow step changes to the cloud dashboard meta."""
        try:
            from aset_batt.storage.cloud_push import set_cloud_meta
            if state == "active" and step < len(phases):
                set_cloud_meta(phase=phases[step], test_mode=test_mode, workflow=workflow)
            elif state in ("done", "skip") and step == len(phases) - 1:
                set_cloud_meta(phase="complete", total_s=0)
        except Exception:
            pass

    def _set_phase_banner_idle(self):
        self._current_test_name = ""
        self.lbl_phase_banner.setText("● IDLE — เลือก workflow แล้วกด RUN")
        self.lbl_phase_banner.setStyleSheet(
            f"background:{PANEL2}; color:{MUTED}; border:1px solid {BORDER}; "
            f"border-radius:5px; padding:6px 8px; font-size:13px; font-weight:700;")

    def _seq_reset_step_leds(self):
        """Reset every workflow's step LEDs to idle (start of a new run, or abort)."""
        for i in range(len(self._WF_STEPS)):       self.sig_workflow.emit(i, "idle")
        for i in range(len(self._QS_STEPS)):       self.sig_qs_workflow.emit(i, "idle")
        for i in range(len(self._HPPC_SEQ_STEPS)): self.sig_hppc_seq_wf.emit(i, "idle")
        for i in range(len(self._CYCLE_STEPS)):    self.sig_cycle_wf.emit(i, "idle")

    def _on_seq_aborted(self):
        """Slot for sig_seq_aborted — the banner alone used to reset on a safety trip,
        leaving the step LEDs and the status label under CANCEL stuck on their last
        in-progress text (e.g. "HPPC 3/5: PULSE 30s"), which looked like a hang even
        though the thread had already exited. The abort reason itself is pushed to
        lbl_wf_status right at the trip site (see the "⛔" sig_wf_status.emit calls) —
        this slot only handles the parts every abort path shares."""
        self._set_phase_banner_idle()
        self._seq_reset_step_leds()

    def _banner_active(self, led_list, step: int, color: str):
        """Update the always-visible banner to '▶ TEST · PHASE' for the active step."""
        if 0 <= step < len(led_list):
            phase = led_list[step][1].text()
            test = self._current_test_name or "TEST"
            self.lbl_phase_banner.setText(f"▶  {test}  ·  {phase}")
            self.lbl_phase_banner.setStyleSheet(
                f"background:{PANEL2}; color:{color}; border:1px solid {color}; "
                f"border-radius:5px; padding:6px 8px; font-size:13px; font-weight:700;")

    def _slot_workflow(self, step: int, state: str):
        """Update a step indicator.  state: idle/active/done/skip."""
        if state == "active":
            self._banner_active(self._wf_leds, step, INFO)
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:65px;",   "○"),
            "active": (f"color:{INFO};    font-size:16px; min-width:22px;",
                       f"color:{INFO};    font-weight:700; min-width:65px;",  "●"),
            "done":   (f"color:{OK};      font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK};      font-weight:700; min-width:65px;",  "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:65px;",  "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._wf_leds):
            dot, name_lbl = self._wf_leds[step]
            dot.setText(symbol)
            dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, str)
    def _slot_qs_workflow(self, phase: int, state: str):
        if state == "active":
            self._banner_active(self._qs_leds, phase, "#e67e22")
        _styles = {
            "active": (f"color:#e67e22; font-size:16px; min-width:22px; font-weight:700;",
                       f"color:#e67e22; font-weight:700; min-width:75px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:75px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:14px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "—"),
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "○"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= phase < len(self._qs_leds):
            dot, name_lbl = self._qs_leds[phase]
            dot.setText(symbol)
            dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(str)
    def _slot_wf_status(self, text: str):
        """Cross-thread safe wrapper for lbl_wf_status.setText."""
        self.lbl_wf_status.setText(text)

    @Slot(int, str)
    def _slot_hppc_seq_wf(self, step: int, state: str):
        if state == "active":
            self._banner_active(self._hppc_seq_leds, step, "#7b2d8b")
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:65px;", "○"),
            "active": (f"color:#7b2d8b; font-size:16px; min-width:22px;",
                       f"color:#7b2d8b; font-weight:700; min-width:65px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:65px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:65px;", "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._hppc_seq_leds):
            dot, name_lbl = self._hppc_seq_leds[step]
            dot.setText(symbol); dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, str)
    def _slot_cycle_wf(self, step: int, state: str):
        if state == "active":
            self._banner_active(self._cycle_leds, step, "#6c3483")
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "○"),
            "active": (f"color:#6c3483; font-size:16px; min-width:22px;",
                       f"color:#6c3483; font-weight:700; min-width:75px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:75px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:75px;", "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._cycle_leds):
            dot, name_lbl = self._cycle_leds[step]
            dot.setText(symbol); dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, int)
    def _slot_phase_progress(self, elapsed_s: int, total_s: int):
        try:
            from aset_batt.storage.cloud_push import set_cloud_meta
            set_cloud_meta(elapsed_s=elapsed_s, total_s=total_s)
        except Exception:
            pass
        if total_s <= 0:
            self.wf_progress.hide(); self.lbl_eta.hide(); return
        self.wf_progress.setRange(0, total_s)
        self.wf_progress.setValue(min(elapsed_s, total_s))
        self.wf_progress.setFormat(f"%p%  ({elapsed_s // 60}m {elapsed_s % 60:02d}s / "
                                   f"{total_s // 60}m {total_s % 60:02d}s)")
        rem = max(0, total_s - elapsed_s)
        self.lbl_eta.setText(f"ETA: {rem // 60}m {rem % 60:02d}s remaining")
        self.wf_progress.show(); self.lbl_eta.show()

    @Slot(str)
    def _slot_seq_result(self, html: str):
        self.lbl_seq_result.setText(html)
        self.frm_seq_result.show()

    @Slot(str, str)
    def _slot_seq_done(self, title: str, body: str):
        """Sound + popup notification when a sequence finishes."""
        self.lbl_phase_banner.setText(f"✓  {self._current_test_name or 'TEST'}  ·  เสร็จสิ้น")
        self.lbl_phase_banner.setStyleSheet(
            f"background:{PANEL2}; color:{OK}; border:1px solid {OK}; "
            f"border-radius:5px; padding:6px 8px; font-size:13px; font-weight:700;")
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            QApplication.beep()
        if not self._headless:
            msg = QMessageBox(self)
            msg.setWindowTitle(title)
            msg.setText(body)
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.setWindowModality(Qt.WindowModality.NonModal)
            msg.show()

    def _show_pretest_dialog(self, title: str, plan_lines: list, eta_min: int) -> bool:
        """Show a pre-test confirmation card.  Returns True iff user clicks Confirm."""
        if self._headless:
            return True
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Confirm: {title}")
        dlg.setMinimumWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)

        # Battery / plan card
        card = QFrame()
        card.setStyleSheet(
            f"QFrame{{background:{PANEL2};border:1px solid {BORDER};"
            f"border-radius:5px;padding:6px 10px;}}"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setSpacing(3)
        for line in plan_lines:
            lbl = QLabel(line)
            lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            card_lay.addWidget(lbl)
        lay.addWidget(card)

        # ETA row
        eta_lbl = QLabel(f"Estimated duration: ~{eta_min} min  ({eta_min//60}h {eta_min%60:02d}m)")
        eta_lbl.setStyleSheet(f"color:{INFO}; font-weight:600;")
        lay.addWidget(eta_lbl)

        # Confirm / Cancel
        btn_row = QHBoxLayout()
        btn_conf = _btn("▶  CONFIRM START", bg=INFO, fg="white", hover="#0d4a89")
        btn_canc = _btn("Cancel", bg="#d0d4d7", hover="#c2c6ca")
        btn_conf.clicked.connect(dlg.accept)
        btn_canc.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_conf, 2); btn_row.addWidget(btn_canc, 1)
        lay.addLayout(btn_row)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _seq_common_start(self, btn_key: str, loading_label: str):
        """Shared startup: reset all step leds, buffers, progress, result card."""
        # The background monitor loop (Start Monitor) also calls estimator.update()
        # at ~10 Hz. If it's left running while a sequence thread starts feeding the
        # same estimator directly, every sample gets counted twice — coulomb counting
        # (and therefore displayed SoC) drifts at roughly double the true rate. Mirror
        # _on_run_test's guard: a sequence owns the estimator exclusively while it runs.
        if self.controller and self.controller.monitor_running:
            self.controller.stop_monitor()
        # capture the test name for the always-visible phase banner
        self._current_test_name = self.cb_workflow_type.currentText().split("(")[0].strip()
        self.lbl_phase_banner.setText(f"▶  {self._current_test_name}  ·  เริ่ม...")
        self.lbl_phase_banner.setStyleSheet(
            f"background:{PANEL2}; color:{INFO}; border:1px solid {INFO}; "
            f"border-radius:5px; padding:6px 8px; font-size:13px; font-weight:700;")
        self._seq_reset_step_leds()
        for buf in (self.buf_t, self.buf_v, self.buf_i,
                    self.buf_soc, self.buf_rin, self.buf_temp):
            buf.clear()
        self._elapsed_t0 = None
        self._seq_last_meas_time = 0.0   # reset watchdog
        self._seq_temp_stale_warned = False   # one-shot guard, see _seq_check_temp_stale
        self.sig_phase_progress.emit(0, 0)   # hide progress bar
        self.frm_seq_result.hide()
        self._seq_running.set()
        self.btn_seq_cancel.setEnabled(True)
        self.sig_loading.emit(btn_key, True, loading_label)

    def _seq_check_temp_stale(self):
        """Warn once per sequence run if the ESP32 temperature reading has gone
        stale (serial glitch / hung sensor). Mirrors AutoController._monitor_loop's
        warn-only handling: we deliberately don't touch the temperature value itself
        (no NaN injection) since these sequence loops feed it straight into the EKF
        and Arrhenius Rin compensation — corrupting it there would poison the whole
        estimate, which is worse than trusting a slightly-stale-but-sane last value.
        Not called from AutoController's own monitor loop (it has its own guard) —
        only from the ISA-101 sequence threads, which had no staleness check at all."""
        if self._seq_temp_stale_warned:
            return
        if getattr(self.hw, "temp_is_stale", None) and self.hw.temp_is_stale():
            self._seq_temp_stale_warned = True
            self.sig_alarm.emit(
                "[WARNING] ESP32 temperature reading is stale — Rin/OCV temperature "
                "compensation and OTP protection may not reflect the real battery.")

    def _on_auto_sequence(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Auto Sequence", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "Auto Sequence", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return
        try:
            v_now, _, _ = self.hw.read_vi()
            temp_now = self.hw.current_temp
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            rated = self.controller.config.battery.rated_capacity
            crate = self.cb_seq_crate.currentText()
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.3f} V  ·  SoC: {soc_now:.0f}%  ·  Temp: {temp_now:.1f} °C",
                f"Charge: {crate} ({float(crate.rstrip('C'))*rated:.3f} A)  →  "
                f"REST {self.spn_rest_min.value()} min  →  "
                f"Discharge {self.cb_test_crate.currentText()}",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        if not self._show_pretest_dialog("IEC 61960 AUTO SEQUENCE", plan, eta_min=600):
            return
        self._seq_common_start("btn_auto_seq", "Running…")
        # Snapshot every widget value on the GUI thread BEFORE spawning the worker —
        # reading Qt widgets from a background thread races the GUI thread (the operator
        # could change a dropdown mid-run) and is not thread-safe.
        opts = {
            "skip_charge": self.chk_skip_charge.isChecked(),
            "skip_rest": self.chk_skip_rest.isChecked(),
            "soc_thresh": self.spn_soc_threshold.value(),
            "seq_crate": self.cb_seq_crate.currentText(),
            "rest_min": self.spn_rest_min.value(),
            "test_crate": self.cb_test_crate.currentText(),
        }
        import threading
        threading.Thread(target=self._auto_sequence_thread, args=(opts,), daemon=True).start()

    def _on_quick_scan(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Quick Scan", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "Quick Scan", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return
        try:
            v_now, _, _ = self.hw.read_vi()
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            rated = self.controller.config.battery.rated_capacity
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.3f} V  ·  SoC: {soc_now:.0f}%",
                f"OCV → REST 5 min → Discharge 1C ({rated:.3f} A) → Peukert SoH",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        if not self._show_pretest_dialog("QUICK SCAN", plan, eta_min=90):
            return
        self._seq_common_start("btn_quick_scan", "Scanning…")
        import threading
        threading.Thread(target=self._quick_scan_thread, daemon=True).start()

    def _on_hppc_sequence(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "HPPC Sequence", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "HPPC Sequence", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return
        try:
            v_now, _, _ = self.hw.read_vi()
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            pulse = float(self.ed_hppc_pulse.text() or "30")
            relax = float(self.ed_hppc_relax.text() or "30")
            n_cyc = self.spn_hppc_cycles.value()
            rated = self.controller.config.battery.rated_capacity
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.3f} V  ·  SoC: {soc_now:.0f}%",
                f"Charge CC-CV → REST 30 min → "
                f"HPPC {n_cyc} cycles ({pulse:.0f}s pulse / {relax:.0f}s relax) → ECM fit",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        eta = int(120 + self.spn_hppc_cycles.value() *
                  (float(self.ed_hppc_pulse.text() or "30") +
                   float(self.ed_hppc_relax.text() or "30")) // 60)
        if not self._show_pretest_dialog("HPPC FULL SEQUENCE", plan, eta_min=eta):
            return
        self._seq_common_start("btn_hppc_seq", "Running…")
        # Snapshot on the GUI thread — see the comment in _on_auto_sequence.
        opts = {
            "n_cyc": self.spn_hppc_cycles.value(),
            "pulse_s": self.ed_hppc_pulse.text(),
            "relax_s": self.ed_hppc_relax.text(),
            "crate": self.ed_hppc_crate.text(),
        }
        import threading
        threading.Thread(target=self._hppc_seq_thread, args=(opts,), daemon=True).start()

    def _on_cycle_life(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Cycle Life", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "Cycle Life", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return
        try:
            n = self.spn_cycle_n.value()
            rated = self.controller.config.battery.rated_capacity
            c_ch = self.cb_cycle_charge_crate.currentText()
            c_di = self.cb_cycle_dis_crate.currentText()
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"Cycles: {n}  ·  Charge: {c_ch}  ·  Discharge: {c_di}",
                f"Rest/cycle: {self.spn_cycle_rest.value()} min  ·  "
                f"Estimated capacity_ah: {rated:.2f} Ah",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        n = self.spn_cycle_n.value()
        eta = int(n * (90 + self.spn_cycle_rest.value()))  # rough: 90 min/cycle
        if not self._show_pretest_dialog("CYCLE LIFE TEST", plan, eta_min=eta):
            return
        self._seq_common_start("btn_cycle_life", f"Cycling…")
        # Snapshot on the GUI thread — see the comment in _on_auto_sequence.
        opts = {
            "n_cyc": self.spn_cycle_n.value(),
            "rest_min": self.spn_cycle_rest.value(),
            "charge_crate": self.cb_cycle_charge_crate.currentText(),
            "dis_crate": self.cb_cycle_dis_crate.currentText(),
        }
        import threading
        threading.Thread(target=self._cycle_life_thread, args=(opts,), daemon=True).start()

    # ── Safety helpers ───────────────────────────────────────────────────────
    _WATCHDOG_TIMEOUT_S: int = 300   # 5 min without a measurement → abort

    def _otp_limit(self) -> float:
        try:
            return float(self.controller.config.system.safety_limits["max_temperature"])
        except Exception:
            return 60.0

    def _uvp_floor(self) -> float:
        """Hardware under-voltage FLOOR (protects the pack/instruments) — this is the
        only threshold that may be checked on a voltage reading taken WHILE current is
        flowing (a pulse or a discharge). It is deliberately lower than
        ``pack_min_voltage``, which is the steady-state end-of-discharge cutoff: under
        load, V = OCV − I·R is naturally BELOW OCV — an HPPC pulse sagging under
        pack_min_voltage is expected physics, not "battery empty". Comparing a loaded
        reading against pack_min_voltage aborts the whole sequence after only a couple
        of pulses even on a healthy, well-charged battery. Use pack_min_voltage only
        against a RESTED (relaxed) reading."""
        try:
            return float(self.controller.config.system.safety_limits["min_voltage"])
        except Exception:
            return 0.0   # unknown → don't add a spurious cutoff on top of pack_min

    def _seq_kick_watchdog(self):
        """Call after every successful measurement read inside a sequence thread."""
        import time as _t
        self._seq_last_meas_time = _t.time()

    def _hw_retry(self, fn, *args, retries: int = 3, delay: float = 0.5, **kwargs):
        """Call ``fn(*args, **kwargs)``, retrying a transient exception with a short
        delay. The sequence threads' per-sample loops already tolerate a single failed
        read (they're inside their own try/except), but the ONE-SHOT hardware reads
        taken right after a REST phase were not — a single VISA/USB glitch there used
        to abort the whole multi-hour sequence and lose all its progress. Re-raises the
        last exception if every attempt fails, so the caller's own error handling
        (which aborts + reports) still applies."""
        last_exc = None
        for attempt in range(max(1, retries)):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    import time as _time
                    _time.sleep(delay)
        raise last_exc

    def _seq_check_otp(self, temp: float) -> bool:
        """Returns True if temperature is safe.  Clears _seq_running + alarms if OTP."""
        limit = self._otp_limit()
        if temp > limit:
            self._seq_running.clear()
            reason = f"OTP triggered: {temp:.1f}°C > {limit:.0f}°C"
            self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
            self.sig_wf_status.emit(f"⛔ {reason}")
            return False
        return True

    def _seq_sleep(self, seconds: float) -> bool:
        """Sleep แบบ interruptible — คืน True ถ้าครบเวลา, False ถ้า cancel หรือ watchdog หมดเวลา"""
        import time
        t_end = time.time() + seconds
        while self._seq_running.is_set():
            left = t_end - time.time()
            if left <= 0:
                return True
            # watchdog: abort if no measurement update for _WATCHDOG_TIMEOUT_S
            last = getattr(self, "_seq_last_meas_time", 0.0)
            if last and (time.time() - last) > self._WATCHDOG_TIMEOUT_S:
                self._seq_running.clear()
                reason = "Watchdog: ไม่มีการวัดค่า > 5 นาที"
                self.sig_alarm.emit(f"[SAFETY] {reason} — sequence ถูกยกเลิก")
                self.sig_wf_status.emit(f"⛔ {reason}")
                return False
            time.sleep(min(0.3, left))
        return False

    def _on_seq_cancel(self):
        self._seq_running.clear()
        # หยุด hardware ทันที
        try:
            if self.controller:
                self.controller.stop_charge()
            self.hw.load_off()
            self.hw.psu_off()
        except Exception:
            pass
        self.lbl_wf_status.setText("ยกเลิก")
        self._set_phase_banner_idle()
        self.btn_seq_cancel.setEnabled(False)
        self.sig_phase_progress.emit(0, 0)
        self.frm_seq_result.hide()
        for btn in ("btn_auto_seq", "btn_quick_scan", "btn_hppc_seq", "btn_cycle_life"):
            self.sig_loading.emit(btn, False, "")
        self.sig_alarm.emit("[AUTO] Sequence cancelled — hardware stopped.")

    def _charge_status_text(self, v: float, i: float, elapsed_ch: int, prefix: str = "CHARGE") -> str:
        """CHARGE-phase status line, e.g. "CHARGE: 13.85V 0.34A (elapsed 12m03s)" — and,
        once the charger is in its tail-current stage (absorption/CV), append the tail
        target so the operator can see real progress instead of a screen that looks
        "stuck": SoC is intentionally capped at 100% the moment the pack is functionally
        full (see the 100% anchor in state_estimator.py), but the charger keeps running
        for a while after that to actually finish tapering off — showing "97% ... 99% ...
        100%" during that tail would mean the number no longer reflects real coulomb
        counting, so instead the current-vs-target progress goes in the status text."""
        base = f"{prefix}: {v:.2f}V {i:.3f}A  (elapsed {elapsed_ch//60}m {elapsed_ch%60:02d}s)"
        ctrl = getattr(self.controller, "_charge_ctrl", None)
        stage = getattr(ctrl, "stage", None)
        if stage in ("absorption", "cv"):
            tail_a = getattr(ctrl.params, "tail_current_a", 0.0)
            return base + f"  ·  Topping off, waiting for tail ≤{tail_a:.3f}A"
        return base

    def _estimate_charge_s(self, soc_now: float, c_rate: float) -> int:
        """Rough charge-time estimate (s) so the progress bar/ETA can show during CHARGE.
        Ah needed to reach full ÷ bulk current, +50% headroom for the CV/absorption taper."""
        try:
            rated = self.config.battery.rated_capacity
            ah_needed = max(0.0, (100.0 - soc_now) / 100.0) * rated
            i_chg = max(0.05, abs(c_rate) * rated)
            t_bulk = ah_needed / i_chg * 3600.0
            return max(60, int(t_bulk * 1.5))
        except Exception:
            return 0

    def _refresh_step_time_estimates(self):
        """Recompute the rough "~N min" line shown under every workflow step, across
        all 4 workflow tabs. Called whenever a setting that affects timing changes
        (product, C-rate, rest minutes, HPPC cycles, cycle-life settings) so the
        preview always reflects the currently-configured test, not a fixed guess."""
        try:
            prod_name = self.cb_product.currentText() if hasattr(self, "cb_product") else ""
            prod = battery_profiles.get_product(prod_name)
            cap = prod.rated_capacity_ah if prod else (
                self.config.battery.rated_capacity if self.config else 5.0)
            chemistry = prod.chemistry if prod else (
                self.config.battery.battery_type if self.config else "LeadAcid")
        except Exception:
            return

        from aset_batt.app.auto_controller import AutoController
        settle = AutoController._OCV_SETTLE
        min_rest, _, _ = settle.get(chemistry, settle["LiPO"])
        ocv_timeout = max(min_rest * 4, 900)
        ocv_est = f"~{min_rest // 60}–{ocv_timeout // 60} min"

        def charge_min(c_rate: float, soc0: float = 20.0) -> float:
            return self._estimate_charge_s(soc0, c_rate) / 60.0

        def discharge_min(c_rate: float, soc0: float = 100.0) -> float:
            i_dis = max(0.05, abs(c_rate) * cap)
            return soc0 / 100.0 * cap / i_dis * 60.0

        def _crate(widget, default):
            try:
                return float(widget.currentText().rstrip("C"))
            except (ValueError, AttributeError):
                return default

        # --- IEC 61960 / AUTO Sequence ---
        if len(self._wf_time_lbls) >= 5:
            self._wf_time_lbls[0].setText(ocv_est)
            c_ch = _crate(self.cb_seq_crate, 0.1) if hasattr(self, "cb_seq_crate") else 0.1
            self._wf_time_lbls[1].setText(f"~{charge_min(c_ch):.0f} min")
            rest_min = self.spn_rest_min.value() if hasattr(self, "spn_rest_min") else 30
            self._wf_time_lbls[2].setText(f"{rest_min} min")
            c_test = _crate(self.cb_test_crate, 0.2) if hasattr(self, "cb_test_crate") else 0.2
            self._wf_time_lbls[3].setText(f"~{discharge_min(c_test):.0f} min")
            self._wf_time_lbls[4].setText("< 1 min")

        # --- Quick Scan ---
        if len(self._qs_time_lbls) >= 4:
            self._qs_time_lbls[0].setText(ocv_est)
            self._qs_time_lbls[1].setText("5 min")
            self._qs_time_lbls[2].setText(f"~{discharge_min(1.0):.0f} min")
            self._qs_time_lbls[3].setText("< 1 min")

        # --- HPPC Full Sequence ---
        if len(self._hppc_seq_time_lbls) >= 5:
            self._hppc_seq_time_lbls[0].setText(ocv_est)
            cp = battery_profiles.get_chemistry(chemistry).charge
            self._hppc_seq_time_lbls[1].setText(f"~{charge_min(cp.bulk_c_rate or 0.1):.0f} min")
            self._hppc_seq_time_lbls[2].setText("30 min")
            try:
                n_cyc = self.spn_hppc_cycles.value()
                pulse_s = float(self.ed_hppc_pulse.text() or "30")
                relax_s = float(self.ed_hppc_relax.text() or "30")
            except (ValueError, AttributeError):
                n_cyc, pulse_s, relax_s = 5, 30.0, 30.0
            hppc_min = n_cyc * (pulse_s + relax_s) / 60.0
            self._hppc_seq_time_lbls[3].setText(f"~{hppc_min:.0f} min")
            self._hppc_seq_time_lbls[4].setText("< 1 min")

        # --- Cycle Life ---
        if len(self._cycle_time_lbls) >= 5:
            self._cycle_time_lbls[0].setText(ocv_est)
            c_ch = _crate(self.cb_cycle_charge_crate, 0.3) \
                if hasattr(self, "cb_cycle_charge_crate") else 0.3
            c_di = _crate(self.cb_cycle_dis_crate, 0.2) \
                if hasattr(self, "cb_cycle_dis_crate") else 0.2
            n_cyc = self.spn_cycle_n.value() if hasattr(self, "spn_cycle_n") else 3
            rest_min = self.spn_cycle_rest.value() if hasattr(self, "spn_cycle_rest") else 5
            ch_min, dis_min = charge_min(c_ch), discharge_min(c_di)
            total_h = (ch_min + rest_min + dis_min) * n_cyc / 60.0
            self._cycle_time_lbls[1].setText(f"~{ch_min:.0f} min /cycle")
            self._cycle_time_lbls[2].setText(f"~{dis_min:.0f} min /cycle")
            self._cycle_time_lbls[3].setText(f"× {n_cyc} = ~{total_h:.1f} h total")
            self._cycle_time_lbls[4].setText("< 1 min")

    def _auto_sequence_thread(self, opts: dict):
        """Background thread: PREPARE → CHARGE → REST → TEST → ANALYZE.
        ``opts`` is a snapshot of the relevant widget values, taken on the GUI thread
        by the caller (_on_auto_sequence) before this thread started."""
        import time

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        skip_charge = opts["skip_charge"]
        skip_rest   = opts["skip_rest"]
        soc_thresh  = opts["soc_thresh"]
        seq_crate   = opts["seq_crate"]
        rest_min    = opts["rest_min"]
        test_crate  = opts["test_crate"]
        completed_ok = False

        try:
            # ── PHASE 0: OCV CALIBRATE ────────────────────────────────────
            self.sig_workflow.emit(0, "active")
            self.hw.psu_off()
            self.hw.load_off()
            # Use ΔV/Δt criterion (Fick diffusion settling) instead of a fixed sleep.
            # calibrate_from_ocv_stable() enforces the chemistry-specific minimum rest
            # (Lead-Acid: 300 s min, ΔV < 10 mV over 60 s window) and then syncs
            # the estimator — giving a true OCV anchor rather than a polarized reading.
            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")

            soc, v, result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress,
                cancel_check=self._seq_running.is_set,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ settled" if result == "settled" else "⚠ timeout"
            self.sig_alarm.emit(f"[AUTO] OCV: {v:.3f} V → SoC {soc:.1f}% ({flag})")
            self.sig_workflow.emit(0, "done")

            # ── PHASE 1: CHARGE ──────────────────────────────────────────
            if skip_charge or soc >= soc_thresh:
                reason = "skip-charge checked" if skip_charge else f"SoC={soc:.0f}% ≥ {soc_thresh}%"
                self.sig_alarm.emit(f"[AUTO] Skipping charge ({reason})")
                self.sig_workflow.emit(1, "skip")
            else:
                self.sig_workflow.emit(1, "active")
                try:
                    _c_rate_override = float(seq_crate.rstrip("C"))
                except (ValueError, AttributeError):
                    _c_rate_override = None
                status(f"CHARGE: SoC={soc:.0f}% → charging "
                       f"({seq_crate})...")
                self.controller.start_charge(strategy=None,
                                             bulk_c_rate_override=_c_rate_override)
                _ch_t0 = time.time()
                _ch_est = self._estimate_charge_s(soc, _c_rate_override or 0.1)
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v2, i2, _ = self.hw.read_vi()
                        elapsed_ch = int(time.time() - _ch_t0)
                        status(self._charge_status_text(v2, max(0.0, i2), elapsed_ch))
                        # estimated total so the bar/ETA show; clamp so it never reverses past 99%
                        self.sig_phase_progress.emit(elapsed_ch, max(_ch_est, elapsed_ch + 30))
                    except Exception:
                        pass
                    if not self._seq_sleep(30.0):
                        break
                if not self._seq_running.is_set():
                    return
                self.sig_phase_progress.emit(0, 0)
                self.sig_workflow.emit(1, "done")
                self.sig_alarm.emit("[AUTO] Charge complete")

            # ── PHASE 2: REST ─────────────────────────────────────────────
            if skip_rest:
                self.sig_alarm.emit("[AUTO] Skipping REST phase")
                self.sig_workflow.emit(2, "skip")
            else:
                self.sig_workflow.emit(2, "active")
                rest_total = rest_min * 60
                t_rest_end = time.time() + rest_total
                while self._seq_running.is_set():
                    remaining = int(t_rest_end - time.time())
                    if remaining <= 0:
                        break
                    elapsed_r = rest_total - remaining
                    mins, secs = divmod(remaining, 60)
                    status(f"REST: เหลือ {mins:d}:{secs:02d} นาที")
                    self.sig_phase_progress.emit(elapsed_r, rest_total)
                    if not self._seq_sleep(10.0):
                        return
                self.sig_phase_progress.emit(0, 0)
                # OCV reset after rest — retried: a single VISA hiccup right after a
                # multi-minute rest must not throw away the whole sequence.
                soc2 = self._hw_retry(self.controller.calibrate_from_ocv)
                v2, _, _ = self._hw_retry(self.hw.read_vi)
                self.sig_alarm.emit(f"[AUTO] Post-rest OCV: {v2:.3f} V → SoC {soc2:.1f}%")
                self.sig_workflow.emit(2, "done")

            # ── PHASE 3: DISCHARGE TEST (IEC — C-rate จาก cb_test_crate) ───────
            self.sig_workflow.emit(3, "active")
            try:
                c_test = float(test_crate.rstrip("C"))
            except (AttributeError, ValueError):
                c_test = 0.2
            rated   = self.controller.config.battery.rated_capacity
            i_dis   = round(c_test * rated, 2)
            pack_min = self.controller.config.battery.pack_min_voltage
            status(f"TEST: discharge {i_dis:.3f} A ({c_test:g}C) จนถึง {pack_min:.1f} V")
            self.sig_alarm.emit(f"[AUTO] Starting discharge {i_dis:.3f} A")
            self.controller._ensure_logging()
            self.hw.set_load(True, i_dis)
            import time as _t
            # perf_counter (monotonic, sub-ms) not time.time() (wall-clock): immune to
            # NTP/clock-jump and consistent with worker.py's own established convention.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            # Estimate discharge duration from SoC and C-rate (seconds)
            rated2 = self.controller.config.battery.rated_capacity
            _dis_est = int(rated2 / max(i_dis, 0.01) * 3600)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    now = _t.perf_counter()   # stamp AT the measurement, not after temp/etc.
                    temp3 = self.hw.current_temp
                    self._seq_check_temp_stale()
                    dt = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    self._seq_kick_watchdog()
                    elapsed_d = int(now - _dis_t0)
                    status(f"TEST: {v3:.3f} V  {i3:.3f} A  SoC {state3['soc']:.0f}%")
                    self.sig_phase_progress.emit(elapsed_d, _dis_est)
                    if not self._seq_check_otp(temp3):
                        break
                    if v3 <= pack_min:
                        break
                except Exception as e:
                    self.sig_alarm.emit(f"[AUTO] discharge read error: {e}")
                    break
                if not self._seq_sleep(5.0):
                    break
            self.hw.set_load(False)
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.controller._ocv_reset_after_rest("discharge")
            self.sig_workflow.emit(3, "done")
            self.sig_alarm.emit("[AUTO] Discharge complete")

            # ── PHASE 4: ANALYZE ─────────────────────────────────────────
            self.sig_workflow.emit(4, "active")
            status("ANALYZE: วิเคราะห์ CSV...")
            res = self.controller._auto_analyze()
            self.sig_workflow.emit(4, "done")
            if res:
                self.sig_seq_result.emit(format_seq_result(res))
            status("เสร็จสิ้น — ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[AUTO] Sequence complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            self.sig_seq_done.emit("IEC 61960 Sequence Complete",
                                   f"Grade: {grade_str}\nดูผลเพิ่มเติมที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[AUTO] Error: {exc}")
            status(f"Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_auto_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    def _quick_scan_thread(self):
        """Quick Scan: OCV → REST 5min → Discharge 1C → Analyze  (~1.5h)
        ใช้ Peukert correction ที่มีอยู่ใน analyze_series เพื่อประเมิน capacity จาก 1C rate."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        completed_ok = False
        try:
            # ── Phase 0: OCV ────────────────────────────────────────────────
            self.sig_qs_workflow.emit(0, "active")
            status("QUICK: ปิดอุปกรณ์, อ่าน OCV...")
            self.hw.psu_off()
            self.hw.load_off()
            if not self._seq_sleep(5.0):
                return

            soc = self.controller.calibrate_from_ocv()
            v, _, _ = self.hw.read_vi()
            self.sig_alarm.emit(f"[QUICK] OCV: {v:.3f} V → SoC {soc:.1f}%")
            self.sig_qs_workflow.emit(0, "done")

            # ── Phase 1: REST 5 นาที ─────────────────────────────────────
            self.sig_qs_workflow.emit(1, "active")
            _rest_total = 5 * 60
            t_end = _t.time() + _rest_total
            while self._seq_running.is_set():
                remaining = int(t_end - _t.time())
                if remaining <= 0:
                    break
                elapsed_r = _rest_total - remaining
                mins, secs = divmod(remaining, 60)
                status(f"QUICK REST: เหลือ {mins}:{secs:02d}")
                self.sig_phase_progress.emit(elapsed_r, _rest_total)
                if not self._seq_sleep(10.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            soc2 = self._hw_retry(self.controller.calibrate_from_ocv)
            v2, _, _ = self._hw_retry(self.hw.read_vi)
            self.sig_alarm.emit(f"[QUICK] Post-rest OCV: {v2:.3f} V → SoC {soc2:.1f}%")
            self.sig_qs_workflow.emit(1, "done")

            # ── Phase 2: DISCHARGE 1C ────────────────────────────────────
            self.sig_qs_workflow.emit(2, "active")
            rated    = self.controller.config.battery.rated_capacity
            max_i    = self.controller.config.battery.max_current
            i_dis    = min(round(1.0 * rated, 2), max_i)   # 1C, clamped to rig limit
            pack_min = self.controller.config.battery.pack_min_voltage
            status(f"QUICK DISCHARGE: {i_dis:.3f} A (1C) → cutoff {pack_min:.1f} V")
            self.sig_alarm.emit(f"[QUICK] Discharge 1C: {i_dis:.3f} A  (rated {rated:.1f} Ah)")
            self.controller._ensure_logging()
            self.hw.set_load(True, i_dis)
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            _dis_est = int(rated / max(i_dis, 0.01) * 3600)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    now    = _t.perf_counter()   # stamp AT the measurement
                    temp3  = self.hw.current_temp
                    self._seq_check_temp_stale()
                    dt     = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    self._seq_kick_watchdog()
                    elapsed_d = int(now - _dis_t0)
                    status(f"QUICK: {v3:.3f} V  {i3:.3f} A  SoC {state3['soc']:.0f}%")
                    self.sig_phase_progress.emit(elapsed_d, _dis_est)
                    if not self._seq_check_otp(temp3):
                        break
                    if v3 <= pack_min:
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[QUICK] read error: {exc}")
                    break
                if not self._seq_sleep(5.0):
                    break
            self.hw.set_load(False)
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            # รอ 30 วิให้แรงดันนิ่ง แล้ว re-anchor SoC
            status("QUICK: รอ 30 วิ OCV settle...")
            if not self._seq_sleep(30.0):
                return
            self.controller.calibrate_from_ocv()
            self.sig_qs_workflow.emit(2, "done")
            self.sig_alarm.emit("[QUICK] Discharge complete (1C) — Peukert correction applied in analysis")

            # ── Phase 3: ANALYZE ─────────────────────────────────────────
            self.sig_qs_workflow.emit(3, "active")
            status("QUICK ANALYZE: คำนวณ Peukert-corrected SoH...")
            res = self.controller._auto_analyze()
            self.sig_qs_workflow.emit(3, "done")
            if res:
                self.sig_seq_result.emit(format_seq_result(res))
            status("QUICK SCAN เสร็จ — ดูผลที่แท็บ Analytics  (ค่า capacity ถูก Peukert-correct แล้ว)")
            self.sig_alarm.emit("[QUICK] Scan complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            self.sig_seq_done.emit("Quick Scan Complete",
                                   f"Grade: {grade_str}\nดูผลเพิ่มเติมที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[QUICK] Error: {exc}")
            status(f"QUICK Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_quick_scan", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------
    def _hppc_seq_thread(self, opts: dict):
        """HPPC Full Sequence: CHARGE → REST 30 min → N×HPPC pulse/relax → ECM fit.
        ``opts`` is a widget-value snapshot taken on the GUI thread by the caller."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        completed_ok = False
        try:
            # ── PHASE 0: PREPARE (OCV calibrate) ──────────────────────────
            self.sig_hppc_seq_wf.emit(0, "active")
            # Calibrate SoC from the battery's actual RESTED voltage before charging —
            # otherwise start_charge() begins from whatever stale estimator.soc was left
            # over from a previous test/session (e.g. showing 100% while still mid-bulk).
            # Must wait for real settle (ΔV/Δt), same as AUTO Sequence's PREPARE phase —
            # an instant read right after connect/a previous test can catch the battery
            # still polarized, which would just relock onto a different wrong SoC.
            self.hw.psu_off()
            self.hw.load_off()

            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"HPPC SEQ: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")

            soc0_ocv, v0_ocv, ocv_result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress,
                cancel_check=self._seq_running.is_set,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ settled" if ocv_result == "settled" else "⚠ timeout"
            self.sig_alarm.emit(
                f"[HPPC SEQ] Pre-charge OCV: {v0_ocv:.3f} V → SoC {soc0_ocv:.1f}% ({flag})")
            self.sig_hppc_seq_wf.emit(0, "done")

            # ── PHASE 1: CHARGE CC-CV ─────────────────────────────────────
            self.sig_hppc_seq_wf.emit(1, "active")
            status("HPPC SEQ: ชาร์จ CC-CV → 100%...")
            rated = self.controller.config.battery.rated_capacity
            self.controller.start_charge(strategy=None)
            _ch_t0 = _t.time()
            _soc0 = getattr(self.controller.estimator, "soc", 50.0)
            _cp = battery_profiles.get_chemistry(
                self.controller.config.battery.battery_type).charge
            _ch_est = self._estimate_charge_s(_soc0, _cp.bulk_c_rate or 0.1)
            while self._seq_running.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                try:
                    v_c, i_c, _ = self.hw.read_vi()
                    elapsed_ch = int(_t.time() - _ch_t0)
                    status(self._charge_status_text(v_c, max(0.0, i_c), elapsed_ch, prefix="HPPC CHARGE"))
                    self.sig_phase_progress.emit(elapsed_ch, max(_ch_est, elapsed_ch + 30))
                except Exception:
                    pass
                if not self._seq_sleep(30.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_hppc_seq_wf.emit(1, "done")
            self.sig_alarm.emit("[HPPC SEQ] Charge complete")

            # ── PHASE 2: REST 30 min ─────────────────────────────────────
            self.sig_hppc_seq_wf.emit(2, "active")
            _rest_total = 30 * 60
            t_rest_end = _t.time() + _rest_total
            while self._seq_running.is_set():
                remaining = int(t_rest_end - _t.time())
                if remaining <= 0:
                    break
                elapsed_r = _rest_total - remaining
                mins, secs = divmod(remaining, 60)
                status(f"HPPC REST (OCV settle): เหลือ {mins}:{secs:02d}")
                self.sig_phase_progress.emit(elapsed_r, _rest_total)
                if not self._seq_sleep(10.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            soc_h = self._hw_retry(self.controller.calibrate_from_ocv)
            v_h, _, _ = self._hw_retry(self.hw.read_vi)
            self.sig_alarm.emit(f"[HPPC SEQ] Post-rest OCV: {v_h:.3f} V → SoC {soc_h:.1f}%")
            self.sig_hppc_seq_wf.emit(2, "done")

            # ── PHASE 3: HPPC N cycles ────────────────────────────────────
            self.sig_hppc_seq_wf.emit(3, "active")
            n_cyc    = opts["n_cyc"]
            try:
                pulse_s = max(1.0, float(opts["pulse_s"] or "30"))
                relax_s = max(1.0, float(opts["relax_s"] or "30"))
                crate   = max(0.1, float(opts["crate"] or "1.0"))
            except (ValueError, AttributeError):
                pulse_s, relax_s, crate = 30.0, 30.0, 1.0
            max_dis = self.controller.config.battery.max_current
            i_pulse = min(crate * rated, max_dis)
            pack_min = self.controller.config.battery.pack_min_voltage
            # Under LOAD (the pulse leg) the voltage sags below OCV by design — abort
            # only against the hardware safety floor, never the steady-state test
            # cutoff (see _uvp_floor). Fall back to a small margin under pack_min if
            # no hardware floor is configured.
            hppc_load_floor = self._uvp_floor()
            if hppc_load_floor <= 0 or hppc_load_floor >= pack_min:
                hppc_load_floor = pack_min * 0.95
            _hppc_total = n_cyc * (relax_s + pulse_s)
            self.controller._ensure_logging()
            self.hw.psu_off()
            self.hw.load_off()
            _hppc_t0 = _t.time()
            for cyc in range(1, n_cyc + 1):
                if not self._seq_running.is_set():
                    break
                # Relax (REST) leg
                status(f"HPPC {cyc}/{n_cyc}: REST {relax_s:.0f}s...")
                t_phase = _t.time() + relax_s
                while self._seq_running.is_set() and _t.time() < t_phase:
                    try:
                        v_r, _, _ = self.hw.read_vi()
                        self.controller._log_sample(v_r, 0.0)
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_r <= pack_min:
                            self._seq_running.clear()
                            reason = (f"Under-voltage during HPPC rest: "
                                      f"{v_r:.3f}V ≤ {pack_min:.3f}V cutoff")
                            self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                            self.sig_wf_status.emit(f"⛔ {reason}")
                            break
                        temp_h = self.hw.current_temp
                        if not self._seq_check_otp(temp_h):
                            break
                    except Exception:
                        pass
                    # _seq_sleep (not a bare sleep) so the 5-min "no measurement"
                    # watchdog actually gets checked — a hung hw.read_vi() call used
                    # to freeze this whole loop forever with no way out.
                    if not self._seq_sleep(1.0):
                        break
                if not self._seq_running.is_set():
                    break
                # Pulse leg
                self.hw.set_load(True, str(i_pulse))
                status(f"HPPC {cyc}/{n_cyc}: PULSE {pulse_s:.0f}s  {i_pulse:.3f} A")
                t_phase = _t.time() + pulse_s
                while self._seq_running.is_set() and _t.time() < t_phase:
                    try:
                        v_p, i_p = self.hw.read_measurements(prefer_load_v=True)
                        # discharge-positive convention (matches AUTO/QUICK SCAN) — do
                        # NOT negate i_p here, or the CSV's current sign is inverted and
                        # the 1-RC ECM fit never converges on this sequence's own data.
                        self.controller._log_sample(v_p, i_p)
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_p <= hppc_load_floor:
                            self._seq_running.clear()
                            reason = (f"Under-voltage during HPPC pulse: "
                                      f"{v_p:.3f}V ≤ {hppc_load_floor:.3f}V hardware floor")
                            self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                            self.sig_wf_status.emit(f"⛔ {reason}")
                            break
                        temp_h = self.hw.current_temp
                        if not self._seq_check_otp(temp_h):
                            break
                    except Exception:
                        pass
                    if not self._seq_sleep(1.0):
                        break
                self.hw.load_off()
                if not self._seq_running.is_set():
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_hppc_seq_wf.emit(3, "done")
            self.sig_alarm.emit(f"[HPPC SEQ] {n_cyc} HPPC cycles complete")

            # ── PHASE 4: ANALYZE (ECM fit) ────────────────────────────────
            self.sig_hppc_seq_wf.emit(4, "active")
            status("HPPC SEQ ANALYZE: ECM fit R0/R1/C1/τ...")
            res = self.controller._auto_analyze(force_hppc=True)
            self.sig_hppc_seq_wf.emit(4, "done")
            if res:
                self.sig_seq_result.emit(format_seq_result(res))
            status("HPPC SEQUENCE เสร็จ — ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[HPPC SEQ] Complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            ecm_str = res.get("ecm_model", "1RC") if res else "1RC"
            self.sig_seq_done.emit("HPPC Sequence Complete",
                                   f"Grade: {grade_str}  ({ecm_str} ECM)\nดูผลที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[HPPC SEQ] Error: {exc}")
            status(f"HPPC SEQ Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.hw.load_off()
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_hppc_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- Cycle Life test thread -------------------------------------------
    def _cycle_life_thread(self, opts: dict):
        """Cycle Life: N × (Charge CC-CV → REST → Discharge CC) with capacity fade tracking.
        ``opts`` is a widget-value snapshot taken on the GUI thread by the caller."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        completed_ok = False
        try:
            n_cyc     = opts["n_cyc"]
            rest_s    = opts["rest_min"] * 60
            rated     = self.controller.config.battery.rated_capacity
            try:
                c_ch  = float(opts["charge_crate"].rstrip("C"))
            except (ValueError, AttributeError):
                c_ch  = 0.3
            try:
                c_di  = float(opts["dis_crate"].rstrip("C"))
            except (ValueError, AttributeError):
                c_di  = 0.2
            max_current = self.controller.config.battery.max_current
            pack_min  = self.controller.config.battery.pack_min_voltage
            i_ch      = min(c_ch * rated, max_current)
            i_dis     = min(c_di * rated, max_current)
            cap_history: list[float] = []

            # ── PHASE 0: PREPARE (OCV calibrate) ──────────────────────────
            # Same fix as HPPC Full Sequence: calibrate SoC from the battery's actual
            # rested voltage before cycle 1 charges, instead of trusting whatever
            # stale estimator.soc was left from a previous test/session.
            self.sig_cycle_wf.emit(0, "active")
            self.hw.psu_off()
            self.hw.load_off()

            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"CYCLE PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")

            soc0_ocv, v0_ocv, ocv_result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress,
                cancel_check=self._seq_running.is_set,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ settled" if ocv_result == "settled" else "⚠ timeout"
            self.sig_alarm.emit(
                f"[CYCLE] Pre-charge OCV: {v0_ocv:.3f} V → SoC {soc0_ocv:.1f}% ({flag})")
            self.sig_cycle_wf.emit(0, "done")

            for cyc in range(1, n_cyc + 1):
                if not self._seq_running.is_set():
                    break
                status(f"CYCLE {cyc}/{n_cyc}: ชาร์จ {i_ch:.3f} A ({c_ch}C)...")
                # ── step 1: CHARGE
                self.sig_cycle_wf.emit(1, "active")
                self.controller.start_charge(strategy=None,
                                             bulk_c_rate_override=c_ch)
                _ch_t0 = _t.time()
                _soc0 = getattr(self.controller.estimator, "soc", 50.0)
                _ch_est = self._estimate_charge_s(_soc0, c_ch or 0.1)
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v_c, i_c, _ = self.hw.read_vi()
                        elapsed_c = int(_t.time() - _ch_t0)
                        status(self._charge_status_text(v_c, max(0.0, i_c), elapsed_c,
                                                        prefix=f"CYCLE {cyc}/{n_cyc} CHARGE"))
                        self.sig_phase_progress.emit(elapsed_c, max(_ch_est, elapsed_c + 30))
                    except Exception:
                        pass
                    if not self._seq_sleep(30.0):
                        break
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break
                self.sig_cycle_wf.emit(1, "done")

                # ── step 2: REST (no dedicated LED — _CYCLE_STEPS has no REST entry;
                # the DISCHARGE LED below only goes "active" once discharge itself starts,
                # so REST no longer mislabels it early on cycle 1)
                t_rest_end = _t.time() + rest_s
                while self._seq_running.is_set():
                    remaining = int(t_rest_end - _t.time())
                    if remaining <= 0:
                        break
                    elapsed_r = rest_s - remaining
                    mins, secs = divmod(remaining, 60)
                    status(f"CYCLE {cyc}/{n_cyc} REST: เหลือ {mins}:{secs:02d}")
                    self.sig_phase_progress.emit(elapsed_r, rest_s)
                    if not self._seq_sleep(10.0):
                        break
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break

                # ── step 3: DISCHARGE (integrate capacity)
                self.sig_cycle_wf.emit(2, "active")
                status(f"CYCLE {cyc}/{n_cyc}: ดิสชาร์จ {i_dis:.3f} A ({c_di}C)...")
                self.controller._ensure_logging()
                self.hw.set_load(True, str(i_dis))
                # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
                _dis_t0 = _t.perf_counter()
                _dis_est = int(rated / max(i_dis, 0.01) * 3600)
                ah_acc = 0.0
                last_log = _t.perf_counter()
                while self._seq_running.is_set():
                    try:
                        v_d, i_d = self.hw.read_measurements(prefer_load_v=True)
                        now = _t.perf_counter()   # stamp AT the measurement
                        dt  = now - last_log
                        last_log = now
                        ah_acc += abs(i_d) * dt / 3600.0
                        # discharge-positive convention — do not negate (same fix as
                        # the HPPC pulse leg; a negated sign here corrupts the CSV that
                        # a later "Analyze CSV" pass or ECM fit would read back).
                        self.controller._log_sample(v_d, i_d)
                        self._seq_kick_watchdog()
                        elapsed_d = int(now - _dis_t0)
                        temp_d = self.hw.current_temp
                        status(f"CYCLE {cyc}/{n_cyc} DIS: {v_d:.3f} V  "
                               f"{ah_acc:.3f} Ah  SoC ~{max(0, 100-100*ah_acc/rated):.0f}%")
                        self.sig_phase_progress.emit(elapsed_d, _dis_est)
                        if not self._seq_check_otp(temp_d):
                            break
                        if v_d <= pack_min:
                            break
                    except Exception as exc:
                        self.sig_alarm.emit(f"[CYCLE] read error: {exc}")
                        break
                    if not self._seq_sleep(5.0):
                        break
                self.hw.set_load(False)
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break
                cap_history.append(ah_acc)
                fade = 100.0 * ah_acc / rated if rated else 0.0
                # Cross-thread safe: this is a background sequence thread — mutating a
                # Qt widget directly here (the old code did) can crash Qt (access
                # violation) or corrupt the UI; go through a signal like everything else.
                self.sig_cycle_counter.emit(
                    f"Cycle {cyc}/{n_cyc}  —  {ah_acc:.3f} Ah  ({fade:.1f}% of rated)"
                )
                self.sig_alarm.emit(
                    f"[CYCLE] Cycle {cyc}: {ah_acc:.3f} Ah  ({fade:.1f}%)"
                )
                self.sig_cycle_wf.emit(2, "done")

            self.sig_cycle_wf.emit(3, "done")
            # Final summary
            if cap_history:
                first, last = cap_history[0], cap_history[-1]
                import math
                soh_init  = 100.0 * first / rated if rated else float("nan")
                soh_final = 100.0 * last  / rated if rated else float("nan")
                fade_pct  = 100.0 * (first - last) / first if first else 0.0
                result_html = (
                    f"<b>Cycle Life ({len(cap_history)} cycles)</b><br>"
                    f"Cap(1): {first:.3f} Ah  →  Cap(N): {last:.3f} Ah<br>"
                    f"SoH init: {soh_init:.1f}%  SoH final: {soh_final:.1f}%  "
                    f"Fade: {fade_pct:.1f}%"
                )
                self.sig_seq_result.emit(result_html)
            self.sig_cycle_wf.emit(4, "done")
            status(f"CYCLE LIFE เสร็จ — {n_cyc} รอบ, ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[CYCLE] Cycle Life complete ✓")
            self.sig_seq_done.emit("Cycle Life Test Complete",
                                   f"ทดสอบครบ {len(cap_history)} รอบ\nดูผล capacity fade ที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[CYCLE] Error: {exc}")
            status(f"CYCLE Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.hw.load_off()
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_cycle_life", False, "")
            self.sig_button.emit("btn_seq_cancel", False)


