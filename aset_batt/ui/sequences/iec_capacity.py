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

from aset_batt.ui import theme
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

class IecCapacityMixin:
    # ---- Workflow guide slots -----------------------------------------------

    # combo index → _wf_stack page. Item 4 (EN 50342-1 Lead-Acid C10) reuses the
    # IEC page: the standard test IS the same PREPARE→CHARGE→REST→DISCHARGE
    # machinery, just with the standard's own conditions preset.
    _WF_PAGE_MAP = {0: 0, 1: 1, 2: 2, 3: 3, 4: 0}
    _WF_EN50342_INDEX = 4

    # _SEQ_TEMP_STALE_TRIP_S and _WATCHDOG_TIMEOUT_S are declared once, on
    # BaseSequenceMixin (base.py) — it's listed first in SequencesMixin's MRO
    # (see sequences/__init__.py), so self._SEQ_TEMP_STALE_TRIP_S/
    # self._WATCHDOG_TIMEOUT_S always resolve there regardless of which
    # mixin's method does the lookup. This mixin used to re-declare its own
    # copies of both constants — never actually read (shadowed by
    # BaseSequenceMixin's earlier MRO position), just a silent trap for
    # anyone who edited one copy expecting it to take effect.

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
        if not self._show_pretest_dialog(
                f"{self._capacity_standard_name()} AUTO SEQUENCE", plan, eta_min=600):
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
            # Log PREPARE's rest from the start — otherwise CSV recording only truly
            # begins once start_charge() implicitly opens a session (via
            # start_monitor()) at PHASE 1, so the CSV never contains a genuine rest
            # window and _quality_flags always flags "no clear rest before load" even
            # though a real rest DID happen, just off-CSV.
            self.controller._ensure_logging(label="IEC")
            # Use ΔV/Δt criterion (Fick diffusion settling) instead of a fixed sleep.
            # calibrate_from_ocv_stable() enforces the chemistry-specific minimum rest
            # (Lead-Acid: 300 s min, ΔV < 10 mV over 60 s window) and then syncs
            # the estimator — giving a true OCV anchor rather than a polarized reading.
            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")
                self.controller._log_sample(v, 0.0)
                self.update_display(v, 0.0, self.controller.estimator.soc,
                                    self.controller.estimator.rin)

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
            # actual runtime skip (user flag OR auto-skip on SoC) — the EN 50342-1
            # verdict below needs what really happened, not just the checkbox.
            _charge_ran = not (skip_charge or soc >= soc_thresh)
            if not _charge_ran:
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
                                             bulk_c_rate_override=_c_rate_override,
                                             reuse_session=True)
                _ch_t0 = time.time()
                _ch_est = self._estimate_charge_s(soc, _c_rate_override or 0.1)
                _tail_t_hist, _tail_i_hist = [], []
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v2, i2, _ = self.hw.read_vi()
                        i2 = max(0.0, i2)
                        elapsed_ch = int(time.time() - _ch_t0)
                        status(self._charge_status_text(v2, i2, elapsed_ch))
                        ctrl = getattr(self.controller, "_charge_ctrl", None)
                        if getattr(ctrl, "stage", None) in ("absorption", "cv"):
                            _tail_t_hist.append(elapsed_ch)
                            _tail_i_hist.append(i2)
                            _ch_est = self._project_tail_eta(
                                _tail_t_hist, _tail_i_hist, ctrl.params.tail_current_a,
                                elapsed_ch, _ch_est)
                        # estimated total so the bar/ETA show; clamp so it never reverses past 99%
                        self.sig_phase_progress.emit(elapsed_ch, max(_ch_est, elapsed_ch + 30))
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    if not self._seq_sleep(30.0):
                        break
                if not self._seq_running.is_set():
                    return
                # start_charge() restarts the shared monitor loop (see its own
                # "if not monitor_running" guard) — stop it again now that charge
                # is done, or it keeps calling estimator.update() concurrently
                # with this sequence's own REST/DISCHARGE loops below and
                # double-counts every sample (see _seq_common_start's comment).
                if self.controller.monitor_running:
                    self.controller.stop_monitor()
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
                    # see the matching comment in _hppc_seq_thread's PHASE 2 — monitor
                    # is stopped here to avoid double-counting the estimator, but that
                    # left the CSV/live gauges frozen at the last CHARGE reading for
                    # the whole rest window. _log_sample() only logs (no
                    # estimator.update()), so it's safe to call during this loop.
                    try:
                        v_r, i_r, _ = self.hw.read_vi()
                        self.controller._log_sample(v_r, i_r)
                        self.update_display(v_r, i_r, self.controller.estimator.soc,
                                            self.controller.estimator.rin, self.hw.current_temp)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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
            self.controller._ensure_logging(label="IEC")
            self.hw.set_load(True, i_dis)
            import time as _t
            # perf_counter (monotonic, sub-ms) not time.time() (wall-clock): immune to
            # NTP/clock-jump and consistent with worker.py's own established convention.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            # Capture one sample immediately after set_load() returns, before the loop
            # below even reaches its first ~5s-paced iteration — identify_dcir()'s
            # single-step method needs a post-edge sample within _DCIR_MAX_STEP_DT
            # (0.5s) of the true current transition; this loop's own pacing is 10x
            # that, so every discharge-start edge was guaranteed to be dropped as
            # stale (same root cause already fixed for the HPPC sequence).
            try:
                v3_0, i3_0 = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v3_0, i3_0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            # Estimate discharge duration from SoC and C-rate (seconds)
            rated2 = self.controller.config.battery.rated_capacity
            _dis_est = int(rated2 / max(i_dis, 0.01) * 3600)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    now = _t.perf_counter()   # stamp AT the measurement, not after temp/etc.
                    temp3 = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    dt = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    # _log_sample feeds CSV/cloud only — the sequence intentionally
                    # stopped the shared monitor loop in _seq_common_start() (to avoid
                    # double-counting the estimator), so nothing else feeds the live
                    # graph during this loop unless we do it here too.
                    self.update_display(v3, i3, state3["soc"], state3["rin"], temp3, state3.get("soh"))
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
            # ── EN 50342-1 verdict (lead-acid only) ───────────────────────
            # When the run satisfied the standard's Cn-test conditions, the
            # measured Ah IS a direct standard-basis Ce — report it against the
            # rated Cn outright (Peukert is a no-op at the reference rate, so
            # this number leans on no rate-conversion model at all). When any
            # condition was violated, say exactly which, so a screenshot of the
            # result can never silently masquerade as a standard measurement.
            en_line = ""
            try:
                applicable, violations = en50342_capacity_conditions(
                    self.controller.config.battery.battery_type, c_test, pack_min,
                    self.controller.config.battery.cells_series,
                    not _charge_ran, skip_rest)
                if applicable and res:
                    ce = float(res.get("capacity_ah", 0.0))
                    pct = 100.0 * ce / rated if rated else 0.0
                    if not violations:
                        verdict = "PASS (Ce ≥ Cn)" if pct >= 100.0 else (
                            f"below Cn — standard allows up to 3 conditioning "
                            f"cycles to reach Cn")
                        en_line = (f"EN 50342-1 Ce = {ce:.2f} Ah = {pct:.0f}% of "
                                   f"Cn ({rated:g} Ah) → {verdict}")
                    else:
                        en_line = ("EN 50342-1: non-standard run — "
                                   + "; ".join(violations))
                    self.sig_alarm.emit(f"[AUTO] {en_line}")
            except Exception as exc:
                logger.debug("EN 50342-1 verdict skipped: %s", exc)
            status("เสร็จสิ้น — ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[AUTO] Sequence complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            body = f"Grade: {grade_str}\n"
            if en_line:
                body += en_line + "\n"
            body += "ดูผลเพิ่มเติมที่แท็บ Analytics"
            self.sig_seq_done.emit(f"{self._capacity_standard_name()} Sequence Complete",
                                   body)
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[AUTO] Error: {exc}")
            status(f"Error: {exc}")
        finally:
            self._seq_running.clear()
            if self.controller:
                self.controller.end_session()
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_auto_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------

    # ---- Cycle Life test thread -------------------------------------------

