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
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html

logger = logging.getLogger(__name__)


# EN 50342-1 (SLI lead-acid) Cn capacity-test conditions this rig can verify.
# The standard defines capacity at the n-hour reference rate In = Cn/n (this
# project's lead-acid ratings are C10 — see ChemistryProfile.peukert_hr), with a
# 1.75 V/cell end voltage, from a fully-charged, rested battery. Measuring AT
# the reference rate is what makes the result a direct Ce-vs-Cn comparison with
# Peukert correction mathematically a no-op — the number stands on its own
# instead of leaning on a rate-conversion model.
_EN50342_END_V_PER_CELL = 1.75
_EN50342_RATE_TOL = 0.15       # ±15% around In still counts as the reference rate
_EN50342_END_V_TOL = 0.06      # V/cell tolerance on the configured cutoff


def en50342_capacity_conditions(chemistry: str, c_test: float, pack_min_v: float,
                                cells_series: int, skip_charge: bool,
                                skip_rest: bool):
    """Check a capacity run's settings against EN 50342-1's Cn-test conditions.

    Returns ``(applicable, violations)``: ``applicable`` False for non-lead-acid
    chemistries (IEC 61960 applies there instead); ``violations`` lists every
    condition this run does NOT satisfy — empty means the measured Ah is a
    direct standard-basis Ce, reportable against the rated Cn as-is.
    """
    from aset_batt.core import battery_profiles
    chem = battery_profiles.get_chemistry(chemistry)
    if chem.name != "LeadAcid":
        return False, []
    violations = []
    ref_hr = float(getattr(chem, "peukert_hr", 10.0) or 10.0)
    ref_rate = 1.0 / ref_hr
    if abs(c_test - ref_rate) > _EN50342_RATE_TOL * ref_rate:
        violations.append(
            f"discharge rate {c_test:g}C is not the I{ref_hr:.0f} reference rate "
            f"({ref_rate:g}C)")
    end_v_cell = pack_min_v / max(1, cells_series)
    if abs(end_v_cell - _EN50342_END_V_PER_CELL) > _EN50342_END_V_TOL:
        violations.append(
            f"end voltage {end_v_cell:.2f} V/cell is not the standard "
            f"{_EN50342_END_V_PER_CELL:.2f} V/cell")
    if skip_charge:
        violations.append("CHARGE phase skipped — standard requires a fully "
                          "charged battery")
    if skip_rest:
        violations.append("REST phase skipped — standard requires a rested "
                          "battery before discharge")
    return True, violations


class BaseSequenceMixin:
    # ---- Workflow guide slots -----------------------------------------------

    # combo index → _wf_stack page. Item 4 (EN 50342-1 Lead-Acid C10) reuses the
    # IEC page: the standard test IS the same PREPARE→CHARGE→REST→DISCHARGE
    # machinery, just with the standard's own conditions preset.
    _WF_PAGE_MAP = {0: 0, 1: 1, 2: 2, 3: 3, 4: 0}
    _WF_EN50342_INDEX = 4

    @Slot(int)
    def _on_workflow_type_changed(self, idx: int):
        """Switch the settings page; for the EN 50342-1 item also preset the
        standard's conditions VISIBLY on the shared IEC page (I10 reference
        rate, no skipped phases) — the operator sees exactly what will run and
        can still change them, in which case the run is honestly re-labelled
        non-standard by the en50342_capacity_conditions() verdict at the end."""
        self._wf_stack.setCurrentIndex(self._WF_PAGE_MAP.get(idx, 0))
        if idx == self._WF_EN50342_INDEX:
            try:
                from aset_batt.core import battery_profiles
                chem = battery_profiles.get_chemistry(
                    self.config.battery.battery_type)
                ref_hr = float(getattr(chem, "peukert_hr", 10.0) or 10.0)
                self.cb_test_crate.setCurrentText(f"{1.0 / ref_hr:g}C")
                self.chk_skip_charge.setChecked(False)
                self.chk_skip_rest.setChecked(False)
                if chem.name != "LeadAcid":
                    self.sig_alarm.emit(
                        "[EN 50342-1] selected battery chemistry is "
                        f"{chem.name} — this standard applies to lead-acid only; "
                        "the run will be reported as IEC 61960 instead")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    @Slot(int, str)
    def _slot_cloud_phase(self, step: int, state: str,
                          phases: list, test_mode: str, workflow: str):
        """Mirror workflow step changes to the cloud dashboard meta."""
        try:
            from aset_batt.storage.cloud_push import set_cloud_meta
            if state == "active" and step < len(phases):
                set_cloud_meta(phase=phases[step], test_mode=test_mode, workflow=workflow)
            elif state in ("done", "skip") and step == len(phases) - 1:
                set_cloud_meta(phase="complete", total_s=0, sub_phase="")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

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
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        # G3 (industrial-grade audit): status_progress (status bar, isa101_views.py)
        # mirrors wf_progress so test progress stays visible no matter which tab is
        # active — see its own comment for why.
        has_status_progress = hasattr(self, "status_progress")
        if total_s <= 0:
            self.wf_progress.hide(); self.lbl_eta.hide()
            if has_status_progress:
                self.status_progress.hide()
            return
        self.wf_progress.setRange(0, total_s)
        self.wf_progress.setValue(min(elapsed_s, total_s))
        self.wf_progress.setFormat(f"%p%  ({elapsed_s // 60}m {elapsed_s % 60:02d}s / "
                                   f"{total_s // 60}m {total_s % 60:02d}s)")
        rem = max(0, total_s - elapsed_s)
        self.lbl_eta.setText(f"ETA: {rem // 60}m {rem % 60:02d}s remaining")
        self.wf_progress.show(); self.lbl_eta.show()
        if has_status_progress:
            self.status_progress.setRange(0, total_s)
            self.status_progress.setValue(min(elapsed_s, total_s))
            self.status_progress.setFormat(f"%p% · ETA {rem // 60}m{rem % 60:02d}s")
            self.status_progress.show()

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

    def _capacity_standard_name(self) -> str:
        """Chemistry-correct capacity-test standard label. "IEC 61960" is a
        SECONDARY LITHIUM standard — stamping it on a lead-acid report claims a
        methodology that standard does not define for this chemistry. SLI
        lead-acid capacity testing lives in EN 50342-1 / IEC 60896 instead. The
        workflow's internal name stays as-is (it is an app identity, not a
        compliance claim); only user-facing result/report text uses this."""
        try:
            chem = self.controller.config.battery.battery_type
            is_lead_acid = battery_profiles.get_chemistry(chem).name == "LeadAcid"
        except Exception:
            is_lead_acid = False
        if is_lead_acid:
            return "EN 50342-1 (SLI lead-acid)"
        return "IEC 61960"

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
        if hasattr(self, "_lbl_soc_note"):
            self._lbl_soc_note.setText("")   # clear any stale "Topping off" from a previous run
        self.sig_phase_progress.emit(0, 0)   # hide progress bar
        self.frm_seq_result.hide()
        self._seq_running.set()
        self.btn_seq_cancel.setEnabled(True)
        self.sig_loading.emit(btn_key, True, loading_label)

    # G8 (industrial-grade audit): a momentary staleness blip only warns — a hard
    # stop on that alone would be its own false-trip hazard. Sustained staleness
    # beyond this means OTP protection has genuinely been blind for real time, not
    # a glitch — see the escalation branch below. Mirrors AutoController's own
    # _TEMP_STALE_TRIP_S.
    _SEQ_TEMP_STALE_TRIP_S = 60.0

    def _seq_check_temp_stale(self) -> bool:
        """Warn once per sequence run if the ESP32 temperature reading has gone
        stale (serial glitch / hung sensor); escalate to aborting the sequence if it
        stays stale for _SEQ_TEMP_STALE_TRIP_S. We deliberately don't touch the
        temperature value itself (no NaN injection) since these sequence loops feed
        it straight into the EKF and Arrhenius Rin compensation — corrupting it
        there would poison the whole estimate, which is worse than trusting a
        slightly-stale-but-sane last value; the escalation stops the TEST instead.
        Not called from AutoController's own monitor loop (it has its own guard) —
        only from the ISA-101 sequence threads, which had no staleness check at all.
        Also called from CHARACTERIZE's Peukert/eta/GITT threads (characterize.py),
        which never run _seq_common_start() — so this can't assume that ran first;
        getattr() with a default avoids an AttributeError crashing their first
        sample. Those callers currently ignore the return value (each characterize
        test tracks its own running-flag in self._char_running, not self._seq_running,
        so auto-abort there needs its own wiring — out of scope for this pass) —
        returns True (safe to continue) unless sequences.py's own _seq_running loops
        check it, matching the _seq_check_otp() pattern right next to it.

        Returns False (and has already cleared self._seq_running + emitted an
        alarm) only on the sustained-staleness trip.
        """
        if getattr(self.hw, "temp_is_stale", None) and \
                self.hw.temp_is_stale(self._SEQ_TEMP_STALE_TRIP_S):
            self._seq_running.clear()
            reason = f"ESP32 temperature stale for {self._SEQ_TEMP_STALE_TRIP_S:.0f}s+"
            self.sig_alarm.emit(f"[SAFETY] {reason} — OTP protection is blind, sequence aborted")
            self.sig_wf_status.emit(f"⛔ {reason}")
            return False
        if getattr(self, "_seq_temp_stale_warned", False):
            return True
        if getattr(self.hw, "temp_is_stale", None) and self.hw.temp_is_stale():
            self._seq_temp_stale_warned = True
            self.sig_alarm.emit(
                "[WARNING] ESP32 temperature reading is stale — Rin/OCV temperature "
                "compensation and OTP protection may not reflect the real battery.")
        return True





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
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, 'Confirm Cancel', 
            'คุณต้องการยกเลิกการทดสอบ (Cancel Sequence) กลางคันใช่หรือไม่?\n\nข้อมูลที่ทดสอบไปแล้วอาจไม่สมบูรณ์', 
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.No:
            return

        self._seq_running.clear()
        # หยุด hardware ทันที
        try:
            if self.controller:
                self.controller.stop_charge()
                self.controller.end_session()   # ปิด session ให้รอบถัดไปเริ่มไฟล์ใหม่แน่ๆ
            self.hw.load_off()
            self.hw.psu_off()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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
        counting, so instead the current-vs-target progress goes in the status text.

        Also mirrors the same "Topping off ≤X.XXXA" wording onto the SoC card's own
        sub-label (self._lbl_soc_note) — the workflow status line carrying this same
        message lives elsewhere on screen, away from the "100%" the operator is
        actually staring at wondering why charging hasn't stopped."""
        base = f"{prefix}: {v:.2f}V {i:.3f}A  (elapsed {elapsed_ch//60}m {elapsed_ch%60:02d}s)"
        ctrl = getattr(self.controller, "_charge_ctrl", None)
        stage = getattr(ctrl, "stage", None)
        note_lbl = getattr(self, "_lbl_soc_note", None)
        if stage in ("absorption", "cv"):
            tail_a = getattr(ctrl.params, "tail_current_a", 0.0)
            if note_lbl is not None:
                note_lbl.setText(f"Topping off ≤{tail_a:.3f}A")
            return base + f"  ·  Topping off, waiting for tail ≤{tail_a:.3f}A"
        if note_lbl is not None:
            note_lbl.setText("")
        return base

    def _estimate_charge_s(self, soc_now: float, c_rate: float) -> int:
        """Rough charge-time estimate (s) so the progress bar/ETA can show at CHARGE
        start, before any real tail-current data exists yet. Ah needed to reach full ÷
        bulk current, +50% headroom for the CV/absorption taper — a static guess, since
        the real CV/absorption duration depends on the pack's actual RC time constant
        (varies with SoC/temperature/health), not a fixed fraction of bulk time. Once the
        charger is actually in its tail-watching stage, _project_tail_eta() below
        supersedes this with a live fit of the real decay — this is only the fallback
        for the bulk phase and the first few tail samples before that fit is reliable."""
        try:
            rated = self.config.battery.rated_capacity
            ah_needed = max(0.0, (100.0 - soc_now) / 100.0) * rated
            i_chg = max(0.05, abs(c_rate) * rated)
            t_bulk = ah_needed / i_chg * 3600.0
            return max(60, int(t_bulk * 1.5))
        except Exception:
            return 0

    def _project_tail_eta(self, t_hist: list, i_hist: list, tail_a: float,
                          elapsed_ch: int, fallback_total: int) -> int:
        """Adaptive ETA for the CV/absorption tail: fit the REAL exponential current
        decay (log(I) is linear in t for an RC tail) from recent samples and extrapolate
        when it crosses tail_a, instead of trusting _estimate_charge_s's fixed 50%
        headroom guess — that guess is the same regardless of whether this particular
        pack's polarization settles in 10 minutes or 2 hours, so it was often badly
        wrong in exactly the phase that used to make CHARGE feel "stuck" (see the
        tail-current status feature added earlier this session)."""
        if len(t_hist) < 5:
            return fallback_total
        try:
            import numpy as np
            t = np.asarray(t_hist[-40:], dtype=float)   # recent window only — a fit
            i = np.asarray(i_hist[-40:], dtype=float)    # anchored on stale early
            i = np.clip(i, 1e-4, None)                   # samples reacts slowly to
            if i[-1] <= tail_a:                          # a real change in decay rate
                return elapsed_ch
            log_i = np.log(i)
            slope, intercept = np.polyfit(t, log_i, 1)
            if slope >= -1e-6:            # not actually decaying (noise/plateau) — the
                return fallback_total      # fit isn't trustworthy, keep the static guess
            # R² of the fit — rejects a "slope" that's really just noise around a flat
            # signal (a small random dip can still produce a nominally-negative slope;
            # requiring the line to actually explain the variance catches that case).
            pred = slope * t + intercept
            ss_res = float(np.sum((log_i - pred) ** 2))
            ss_tot = float(np.sum((log_i - log_i.mean()) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
            if r2 < 0.5:
                return fallback_total
            t_tail = (np.log(tail_a) - intercept) / slope
            if not np.isfinite(t_tail) or t_tail <= elapsed_ch:
                return fallback_total
            # Clamp so one noisy sample can't make the bar's ETA jump wildly —
            # cap the projection at 3x the original static estimate.
            return int(min(t_tail, fallback_total * 3.0))
        except Exception:
            return fallback_total

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
            self._wf_time_lbls[0].setText("")
            c_ch = _crate(self.cb_seq_crate, 0.1) if hasattr(self, "cb_seq_crate") else 0.1
            ch_m = charge_min(c_ch)
            self._wf_time_lbls[1].setText("")
            rest_min = self.spn_rest_min.value() if hasattr(self, "spn_rest_min") else 30
            self._wf_time_lbls[2].setText("")
            c_test = _crate(self.cb_test_crate, 0.2) if hasattr(self, "cb_test_crate") else 0.2
            dis_m = discharge_min(c_test)
            self._wf_time_lbls[3].setText("")
            total_h = ((ocv_timeout / 60.0) + ch_m + rest_min + dis_m) / 60.0
            self._wf_time_lbls[4].setText(f"(Total est. ~{total_h:.1f} h)")

        # --- Quick Scan ---
        if len(self._qs_time_lbls) >= 4:
            self._qs_time_lbls[0].setText("")
            self._qs_time_lbls[1].setText("")
            dis_m = discharge_min(1.0)
            self._qs_time_lbls[2].setText("")
            total_h = ((ocv_timeout / 60.0) + 5 + dis_m) / 60.0
            self._qs_time_lbls[3].setText(f"(Total est. ~{total_h:.1f} h)")

        # --- HPPC Full Sequence ---
        if len(self._hppc_seq_time_lbls) >= 5:
            self._hppc_seq_time_lbls[0].setText("")
            cp = battery_profiles.get_chemistry(chemistry).charge
            ch_m = charge_min(cp.bulk_c_rate or 0.1)
            self._hppc_seq_time_lbls[1].setText("")
            self._hppc_seq_time_lbls[2].setText("")
            try:
                n_cyc = self.spn_hppc_cycles.value()
                pulse_s = float(self.ed_hppc_pulse.text() or "30")
                relax_s = float(self.ed_hppc_relax.text() or "30")
            except (ValueError, AttributeError):
                n_cyc, pulse_s, relax_s = 5, 30.0, 30.0
            hppc_min = n_cyc * (pulse_s + relax_s) / 60.0
            self._hppc_seq_time_lbls[3].setText("")
            total_h = ((ocv_timeout / 60.0) + ch_m + 30 + hppc_min) / 60.0
            self._hppc_seq_time_lbls[4].setText(f"(Total est. ~{total_h:.1f} h)")

        # --- Cycle Life ---
        if len(self._cycle_time_lbls) >= 5:
            self._cycle_time_lbls[0].setText("")
            c_ch = _crate(self.cb_cycle_charge_crate, 0.3) \
                if hasattr(self, "cb_cycle_charge_crate") else 0.3
            c_di = _crate(self.cb_cycle_dis_crate, 0.2) \
                if hasattr(self, "cb_cycle_dis_crate") else 0.2
            n_cyc = self.spn_cycle_n.value() if hasattr(self, "spn_cycle_n") else 3
            rest_min = self.spn_cycle_rest.value() if hasattr(self, "spn_cycle_rest") else 5
            ch_min, dis_min = charge_min(c_ch), discharge_min(c_di)
            total_h = (ch_min + rest_min + dis_min) * n_cyc / 60.0
            self._cycle_time_lbls[1].setText("")
            self._cycle_time_lbls[2].setText("")
            self._cycle_time_lbls[3].setText("")
            self._cycle_time_lbls[4].setText(f"(Total est. ~{total_h:.1f} h)")



    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------

    # ---- Cycle Life test thread -------------------------------------------


