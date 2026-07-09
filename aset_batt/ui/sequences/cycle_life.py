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


class CycleLifeMixin:
    # ---- Workflow guide slots -----------------------------------------------

    # combo index → _wf_stack page. Item 4 (EN 50342-1 Lead-Acid C10) reuses the
    # IEC page: the standard test IS the same PREPARE→CHARGE→REST→DISCHARGE
    # machinery, just with the standard's own conditions preset.
    _WF_PAGE_MAP = {0: 0, 1: 1, 2: 2, 3: 3, 4: 0}
    _WF_EN50342_INDEX = 4


















    # G8 (industrial-grade audit): a momentary staleness blip only warns — a hard
    # stop on that alone would be its own false-trip hazard. Sustained staleness
    # beyond this means OTP protection has genuinely been blind for real time, not
    # a glitch — see the escalation branch below. Mirrors AutoController's own
    # _TEMP_STALE_TRIP_S.
    _SEQ_TEMP_STALE_TRIP_S = 60.0





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














    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------

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
            # See the same comment in _auto_sequence_thread — log PREPARE's rest from
            # the start so the CSV actually contains a genuine rest window.
            self.controller._ensure_logging(label="CycleLife")

            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"CYCLE PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")
                self.controller._log_sample(v, 0.0)
                self.update_display(v, 0.0, self.controller.estimator.soc,
                                    self.controller.estimator.rin)

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
                                             bulk_c_rate_override=c_ch,
                                             reuse_session=True)
                _ch_t0 = _t.time()
                _soc0 = getattr(self.controller.estimator, "soc", 50.0)
                _ch_est = self._estimate_charge_s(_soc0, c_ch or 0.1)
                _tail_t_hist, _tail_i_hist = [], []
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v_c, i_c, _ = self.hw.read_vi()
                        i_c = max(0.0, i_c)
                        elapsed_c = int(_t.time() - _ch_t0)
                        status(self._charge_status_text(v_c, i_c, elapsed_c,
                                                        prefix=f"CYCLE {cyc}/{n_cyc} CHARGE"))
                        ctrl = getattr(self.controller, "_charge_ctrl", None)
                        if getattr(ctrl, "stage", None) in ("absorption", "cv"):
                            _tail_t_hist.append(elapsed_c)
                            _tail_i_hist.append(i_c)
                            _ch_est = self._project_tail_eta(
                                _tail_t_hist, _tail_i_hist, ctrl.params.tail_current_a,
                                elapsed_c, _ch_est)
                        self.sig_phase_progress.emit(elapsed_c, max(_ch_est, elapsed_c + 30))
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    if not self._seq_sleep(30.0):
                        break
                if self.controller.monitor_running:   # see the same comment in _auto_sequence_thread
                    self.controller.stop_monitor()
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
                    # see the matching comment in _hppc_seq_thread's PHASE 2 REST —
                    # monitor is stopped for this loop, so log/update explicitly here
                    # instead of leaving the CSV/gauges frozen at the last reading.
                    try:
                        v_r, i_r, _ = self.hw.read_vi()
                        self.controller._log_sample(v_r, i_r)
                        self.update_display(v_r, i_r, self.controller.estimator.soc,
                                            self.controller.estimator.rin, self.hw.current_temp)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    if not self._seq_sleep(10.0):
                        break
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break

                # ── step 3: DISCHARGE (integrate capacity)
                self.sig_cycle_wf.emit(2, "active")
                status(f"CYCLE {cyc}/{n_cyc}: ดิสชาร์จ {i_dis:.3f} A ({c_di}C)...")
                self.controller._ensure_logging(label="CycleLife")
                self.hw.set_load(True, str(i_dis))
                # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
                _dis_t0 = _t.perf_counter()
                _dis_est = int(rated / max(i_dis, 0.01) * 3600)
                ah_acc = 0.0
                last_log = _t.perf_counter()
                # Same low-latency edge sample as _auto_sequence_thread's IEC discharge —
                # this loop's own pacing (~5s) is 10x identify_dcir()'s staleness gate.
                try:
                    v_d0, i_d0 = self.hw.read_measurements(prefer_load_v=True)
                    self.controller._log_sample(v_d0, i_d0)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                while self._seq_running.is_set():
                    try:
                        v_d, i_d = self.hw.read_measurements(prefer_load_v=True)
                        now = _t.perf_counter()   # stamp AT the measurement
                        dt  = now - last_log
                        last_log = now
                        ah_acc += abs(i_d) * dt / 3600.0
                        temp_d = self.hw.current_temp
                        # Stale-temp escalation (G8), checked BEFORE feeding the
                        # estimator/CSV — same check-then-feed order as the IEC/Quick
                        # Scan discharge loops, so a stale ESP32 reading never
                        # contaminates the Arrhenius-compensated fit or gets written to
                        # disk before the sequence aborts.
                        if not self._seq_check_temp_stale():
                            break
                        if not self._seq_check_otp(temp_d):
                            break
                        # discharge-positive convention — do not negate (same fix as
                        # the HPPC pulse leg; a negated sign here corrupts the CSV that
                        # a later "Analyze CSV" pass or ECM fit would read back).
                        #
                        # estimator.update() per-sample — same frozen-SoC fix as the
                        # HPPC pulse leg (see its comment): ah_acc above is only this
                        # sequence's own capacity-fade tracker, so without this every
                        # cycle's real Ah removal was invisible to the shared
                        # estimator, leaving its SoC frozen through the whole test.
                        # Safe now for the same reasons (monitor stopped, EKF guard).
                        state_d = self.controller.estimator.update(
                            v_d, i_d, dt=max(1e-3, dt), temp=temp_d)
                        self.controller._log_sample(v_d, i_d)
                        self.update_display(v_d, i_d, state_d["soc"], state_d["rin"])
                        self._seq_kick_watchdog()
                        elapsed_d = int(now - _dis_t0)
                        status(f"CYCLE {cyc}/{n_cyc} DIS: {v_d:.3f} V  "
                               f"{ah_acc:.3f} Ah  SoC ~{max(0, 100-100*ah_acc/rated):.0f}%")
                        self.sig_phase_progress.emit(elapsed_d, _dis_est)
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
            if self.controller:
                self.controller.end_session()
            self.sig_phase_progress.emit(0, 0)
            self.hw.load_off()
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_cycle_life", False, "")
            self.sig_button.emit("btn_seq_cancel", False)


