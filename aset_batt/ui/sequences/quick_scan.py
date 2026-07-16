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
from aset_batt.core.battery_model import DEFAULT_SAMPLE_HZ
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

# Quick Scan mini-pulse (accuracy fix): the discharge-only record used to leave
# every DCIR/ECM field as an unmeasured profile fallback whenever the discharge
# edge landed stale (>0.5s post-edge latency) — a real run graded C and reported
# a 189.7A CCA proxy purely from the 30 mΩ chemistry-generic baseline, not a
# measurement. A short 1C pulse right after PREPARE's settle, sampled at
# DEFAULT_SAMPLE_HZ like HPPC's own pulse leg, gives identify_ecm_fit() a real
# transient to fit — real HPPC data on this same rig/chemistry (FB FTZ6V)
# validated R²=0.94-0.99 and τ=4.1-5.1s from an identical 30s/10Hz pulse, so a
# single mini-pulse here is expected to reach the same fit quality.
# 30s at 1C, chosen to be ≥3× the τ≈4-5s this chemistry actually measures at
# (see the comment above) — the general "≥3τ" resolving-window reasoning HPPC's
# adaptive relax also uses, not a chemistry-generic guess.
QUICK_MINI_PULSE_S = 30.0
QUICK_MINI_RELAX_S = 90.0
# Tail rest after the main discharge — replaces what used to be a THIRD full
# calibrate_from_ocv_stable() settle call (≥300s floor for lead-acid). The
# estimator is already anchored at the cutoff endpoint and the next test's own
# PREPARE re-anchors from scratch anyway; a long settled tail here only risks
# pulling _load_metrics' whole-record rest-median OCV toward the near-empty
# tail voltage instead of the head's genuine full-pack rest. Short + logged
# keeps the head rest dominant while still recording the immediate post-
# discharge relaxation as real data.
QUICK_TAIL_REST_S = 60.0

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

class QuickScanMixin:
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
        eta_min = 90
        try:
            v_now, _, _ = self.hw.read_vi()
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            rated = self.controller.config.battery.rated_capacity
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.3f} V  ·  Temp: {self.hw.current_temp:.1f} °C",
                f"OCV settle → Mini-pulse DCIR/ECM → Discharge 1C ({rated:.3f} A) → Peukert SoH",
            ]
            # Honest ETA (was a flat hardcoded 90 regardless of starting SoC or
            # chemistry): ~10 min fixed overhead (settle + mini-pulse + relax +
            # tail rest — see the phase comments in _quick_scan_thread for the
            # breakdown) plus the 1C discharge itself scaled by how much charge
            # is actually left to remove (a half-empty pack finishes in half the
            # time, not the full-pack worst case every run used to quote).
            eta_min = int(10 + max(0.0, soc_now) / 100.0 * 60)
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        if not self._show_pretest_dialog("QUICK SCAN", plan, eta_min=eta_min):
            return
        self._seq_common_start("btn_quick_scan", "Scanning…")
        import threading
        threading.Thread(target=self._quick_scan_thread, daemon=True).start()

    def _quick_scan_thread(self):
        """Quick Scan: OCV settle → Mini-pulse DCIR/ECM → Discharge 1C → Tail rest
        → Analyze (~1-1.5h ขึ้นกับ SoC เริ่มต้น — ดู eta_min ใน _on_quick_scan)
        ใช้ Peukert correction ที่มีอยู่ใน analyze_series เพื่อประเมิน capacity จาก 1C rate
        และ fit_ecm=True เพื่อวัด DCIR/R0/R1/τ จริงจาก mini-pulse แทนค่า fallback."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        completed_ok = False
        try:
            # ── Phase 0: OCV ────────────────────────────────────────────────
            self.sig_qs_workflow.emit(0, "active")
            status("QUICK: ปิดอุปกรณ์, รอ OCV settle...")
            self.hw.psu_off()
            self.hw.load_off()
            # See the comment in _auto_sequence_thread — log from PREPARE so the CSV
            # actually contains a genuine rest window (otherwise the file only starts
            # once start_charge()/start_monitor() implicitly opens one, and
            # _quality_flags always flags "no clear rest before load").
            self.controller._ensure_logging(label="QuickScan")

            # Trailing rest samples for the mini-pulse's ECM fit below — same
            # role as the relax leg's tail buffer in HPPC's own pulse loop:
            # identify_ecm_fit() needs to see the actual rest->pulse edge to
            # locate the step, not just the pulse's own already-loaded current.
            _rest_tail_v = []

            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"QUICK PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")
                self.controller._log_sample(v, 0.0)
                self.update_display(v, 0.0, self.controller.estimator.soc,
                                    self.controller.estimator.rin)
                _rest_tail_v.append(v)
                if len(_rest_tail_v) > 5:
                    _rest_tail_v.pop(0)

            # A one-shot instant read here (old behavior: 5s sleep then a raw
            # calibrate_from_ocv()) could catch the pack still polarized from
            # whatever happened right before this test started — use the same
            # ΔV/Δt settle-checked anchor every other sequence's PREPARE uses.
            #
            # This anchor now ALSO serves the purpose the old fixed 5-min REST +
            # a SECOND calibrate_from_ocv_stable() call used to: this settle's
            # own final logged samples ARE the pre-edge reference identify_dcir()
            # needs before the mini-pulse edge fires next — a real settle-check
            # already proves the rest, so timing out an extra 5 min then
            # re-proving it a second time was pure duplication (verified against
            # the real Quick Scan CSV: that record's only edge came 10.36s after
            # the settle ended and was dropped as stale regardless of the extra
            # wait — the redundant call bought no accuracy, only ~10-25 min).
            soc, v, ocv_result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress,
                cancel_check=self._seq_running.is_set,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ settled" if ocv_result == "settled" else "⚠ timeout"
            self.sig_alarm.emit(f"[QUICK] OCV: {v:.3f} V → SoC {soc:.1f}% ({flag})")
            self.sig_qs_workflow.emit(0, "done")

            # ── Phase 1: MINI-PULSE (DCIR/ECM) ────────────────────────────
            # A short 1C pulse right after the settled anchor — gives
            # identify_dcir()/identify_ecm_fit() a real, freshly-sampled
            # transient to measure instead of the single (often stale) edge the
            # main discharge below used to be the only source of. Real HPPC data
            # on this same rig/chemistry validated R²=0.94-0.99 from an
            # identical 30s/10Hz pulse — see QUICK_MINI_PULSE_S's comment.
            self.sig_qs_workflow.emit(1, "active")
            rated    = self.controller.config.battery.rated_capacity
            max_i    = self.controller.config.battery.max_current
            i_dis    = min(round(1.0 * rated, 2), max_i)   # same 1C the main discharge uses
            pack_min = self.controller.config.battery.pack_min_voltage
            # Under LOAD the voltage sags below OCV by design — abort against the
            # hardware safety floor, never the steady-state cutoff (see HPPC's
            # identical _uvp_floor() usage and its own comment for why).
            quick_load_floor = self._uvp_floor()
            if quick_load_floor <= 0 or quick_load_floor >= pack_min:
                quick_load_floor = pack_min * 0.95
            status(f"QUICK MINI-PULSE: {i_dis:.3f} A (1C) × {QUICK_MINI_PULSE_S:.0f}s...")
            self.sig_alarm.emit(f"[QUICK] Mini-pulse: {i_dis:.3f} A × {QUICK_MINI_PULSE_S:.0f}s (DCIR/ECM)")
            self.hw.set_load(True, i_dis)
            # Immediate low-latency edge sample — identify_dcir()'s staleness
            # gate is 0.5s; waiting for the next paced loop iteration would blow
            # straight past it, same reasoning as the main discharge loop below.
            try:
                v_mp0, i_mp0 = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v_mp0, i_mp0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            _upd_last = _t.perf_counter()
            _fit_t0 = _t.perf_counter()
            _rest_n = len(_rest_tail_v)
            # Seed with the trailing rest samples (negative relative time, i=0)
            # so identify_ecm_fit() can actually locate the rest->pulse edge.
            _fit_t = [-(_rest_n - k) / DEFAULT_SAMPLE_HZ for k in range(_rest_n)]
            _fit_i = [0.0] * _rest_n
            _fit_v = list(_rest_tail_v)
            voc_for_fit = (sorted(_rest_tail_v)[_rest_n // 2] if _rest_n else v)
            t_phase = _t.time() + QUICK_MINI_PULSE_S
            while self._seq_running.is_set() and _t.time() < t_phase:
                _iter_t0 = _t.perf_counter()
                try:
                    v_mp, i_mp = self.hw.read_measurements(prefer_load_v=True)
                    temp_mp = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    if not self._seq_check_otp(temp_mp):
                        break
                    _upd_now = _t.perf_counter()
                    state_mp = self.controller.estimator.update(
                        v_mp, i_mp, dt=max(1e-3, _upd_now - _upd_last), temp=temp_mp)
                    _upd_last = _upd_now
                    self.controller._log_sample(v_mp, i_mp)
                    _fit_t.append(_t.perf_counter() - _fit_t0)
                    _fit_i.append(i_mp)
                    _fit_v.append(v_mp)
                    self.update_display(v_mp, i_mp, state_mp["soc"], state_mp["rin"],
                                        temp_mp, state_mp.get("soh"))
                    self._seq_kick_watchdog()
                    if v_mp <= quick_load_floor:
                        self._seq_running.clear()
                        reason = (f"Under-voltage during Quick Scan mini-pulse: "
                                  f"{v_mp:.3f}V ≤ {quick_load_floor:.3f}V hardware floor")
                        self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                        self.sig_wf_status.emit(f"⛔ {reason}")
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[QUICK] mini-pulse read error: {exc}")
                    break
                # Paced to DEFAULT_SAMPLE_HZ (10Hz) — same technique as HPPC's own
                # pulse leg: enough points densely sampled at the edge for
                # identify_ecm_fit() to actually resolve R1/C1, not just R0.
                _elapsed_iter = _t.perf_counter() - _iter_t0
                if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                    break
            self.hw.set_load(False)
            # Same low-latency edge sample as the pulse-start above, for the
            # pulse-end transition.
            try:
                v_mp1, i_mp1 = self.hw.read_measurements(prefer_load_v=False)
                self.controller._log_sample(v_mp1, i_mp1)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            if not self._seq_running.is_set():
                return

            # Relaxation rest — the curve itself is data, and this also gives
            # identify_dcir() a real rest window before the main discharge edge.
            t_phase = _t.time() + QUICK_MINI_RELAX_S
            while self._seq_running.is_set() and _t.time() < t_phase:
                _iter_t0 = _t.perf_counter()
                try:
                    v_rl, _, _ = self.hw.read_vi()
                    temp_rl = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    if not self._seq_check_otp(temp_rl):
                        break
                    _upd_now = _t.perf_counter()
                    state_rl = self.controller.estimator.update(
                        v_rl, 0.0, dt=max(1e-3, _upd_now - _upd_last), temp=temp_rl)
                    _upd_last = _upd_now
                    self.controller._log_sample(v_rl, 0.0)
                    self.update_display(v_rl, 0.0, state_rl["soc"], state_rl["rin"])
                    self._seq_kick_watchdog()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                _elapsed_iter = _t.perf_counter() - _iter_t0
                if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                    break
            if not self._seq_running.is_set():
                return

            # Live fit-and-feed — same pattern as HPPC's per-cycle block: fit
            # this pulse's own buffers, harness-correct, temp-normalize to the
            # estimator's 25°C contract, feed update_ecm(). Skip the feed (not
            # a fallback to raw values) if normalization itself fails, same
            # guard HPPC uses.
            if voc_for_fit is not None and len(_fit_t) >= 10:
                try:
                    from aset_batt.acquisition.analysis import (
                        identify_ecm_fit, _correct_for_harness_r)
                    ecm, _reason = identify_ecm_fit(_fit_t, _fit_i, _fit_v, voc_for_fit)
                    if ecm is not None:
                        r0 = float(ecm["R0_ohm"])
                        harness_r = max(0.0, float(getattr(
                            self.controller.config.battery, "harness_resistance_ohm", 0.0)))
                        if harness_r > 0.0:
                            r0, _warn = _correct_for_harness_r(r0, harness_r, "live ECM R0", [])
                            if _warn:
                                self.sig_alarm.emit(f"[QUICK] {_warn[0]}")
                        r1 = float(ecm["R1_ohm"])
                        c1 = float(ecm["C1_farad"])
                        tau_fit = float(ecm.get("tau1_s", ecm.get("tau_s", 0.0)))
                        r2_fit = float(ecm.get("r_squared", 0.0))
                        try:
                            _mult = self.controller.estimator.battery_model \
                                .temp_rin_multiplier(self.hw.current_temp)
                        except Exception as _exc:
                            logger.debug(
                                "Quick Scan mini-pulse temp-normalization failed (%s) — "
                                "skipping update_ecm() feed", _exc)
                            _mult = None
                        if _mult is not None and _mult > 1e-6:
                            r0n, r1n, c1n = r0 / _mult, r1 / _mult, c1 * _mult
                            self.controller.estimator.update_ecm(r0n, r1n, c1n)
                            self.sig_alarm.emit(
                                f"[QUICK] Mini-pulse ECM: R0={r0n*1e3:.1f}mΩ "
                                f"R1={r1n*1e3:.1f}mΩ τ={tau_fit:.1f}s R²={r2_fit:.3f}")
                    elif _reason:
                        self.sig_alarm.emit(f"[QUICK] Mini-pulse ECM not identified — {_reason}")
                except Exception as exc:
                    logger.debug("Quick Scan mini-pulse fit failed (non-fatal): %s", exc)
            if not self._seq_running.is_set():
                return
            self.sig_qs_workflow.emit(1, "done")

            # ── Phase 2: DISCHARGE 1C ────────────────────────────────────
            # rated/max_i/i_dis/pack_min already computed for the mini-pulse
            # above (Phase 1 uses the same 1C rate) — reuse, don't recompute.
            self.sig_qs_workflow.emit(2, "active")
            status(f"QUICK DISCHARGE: {i_dis:.3f} A (1C) → cutoff {pack_min:.1f} V")
            self.sig_alarm.emit(f"[QUICK] Discharge 1C: {i_dis:.3f} A  (rated {rated:.1f} Ah)")
            self.controller._ensure_logging(label="QuickScan")
            self.hw.set_load(True, i_dis)
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            _dis_est = self._estimate_discharge_s(i_dis)
            _cutoff_confirm_n = 0
            # Same low-latency edge sample as _auto_sequence_thread's IEC discharge —
            # this loop's own pacing (~5s) is 10x identify_dcir()'s staleness gate
            # (0.5s), so every discharge-start edge was guaranteed dropped as stale.
            try:
                v3_0, i3_0 = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v3_0, i3_0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    now    = _t.perf_counter()   # stamp AT the measurement
                    temp3  = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    dt     = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    # see the same comment in _auto_sequence_thread — the shared monitor
                    # loop is stopped for the duration of this sequence, so the live
                    # graph needs its own feed here too, not just CSV/cloud.
                    self.update_display(v3, i3, state3["soc"], state3["rin"], temp3, state3.get("soh"))
                    self._seq_kick_watchdog()
                    elapsed_d = int(now - _dis_t0)
                    status(f"QUICK: {v3:.3f} V  {i3:.3f} A  SoC {state3['soc']:.0f}%")
                    self.sig_phase_progress.emit(elapsed_d, _dis_est)
                    if not self._seq_check_otp(temp3):
                        break
                    # Same debounce as worker.py's CC_DISCHARGE cutoff check — 5
                    # consecutive at/below-cutoff samples, not just one.
                    _cutoff_confirm_n = (_cutoff_confirm_n + 1) if v3 <= pack_min else 0
                    if _cutoff_confirm_n >= 5:
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[QUICK] read error: {exc}")
                    break
                if not self._seq_sleep(5.0):
                    break
            # Fresh pre-edge sample right before load-off — the real Quick Scan
            # CSV this fix is grounded in was MISSING the discharge-end edge
            # entirely (file ended under load at cutoff), so identify_dcir()
            # never had an OFF transition to measure at all. Mirrors the
            # pre-edge pattern already used at every load ON transition above.
            try:
                v3_end, i3_end = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v3_end, i3_end)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.hw.set_load(False)
            # Immediate low-latency edge sample for the OFF transition itself.
            try:
                v3_off, i3_off = self.hw.read_measurements(prefer_load_v=False)
                self.controller._log_sample(v3_off, i3_off)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_qs_workflow.emit(2, "done")
            self.sig_alarm.emit("[QUICK] Discharge complete (1C) — Peukert correction applied in analysis")

            # ── Phase 3: TAIL REST (short, fixed) ─────────────────────────
            # Replaces what used to be a THIRD full calibrate_from_ocv_stable()
            # settle (≥300s floor for lead-acid) — see QUICK_TAIL_REST_S's
            # comment for why a long settled tail isn't worth that cost here.
            self.sig_qs_workflow.emit(3, "active")
            status(f"QUICK: พัก {QUICK_TAIL_REST_S:.0f} วิหลัง discharge...")
            t_phase = _t.time() + QUICK_TAIL_REST_S
            while self._seq_running.is_set() and _t.time() < t_phase:
                try:
                    v_tail, _, _ = self.hw.read_vi()
                    self.controller._log_sample(v_tail, 0.0)
                    self.update_display(v_tail, 0.0, self.controller.estimator.soc,
                                        self.controller.estimator.rin)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                if not self._seq_sleep(1.0):
                    break
            if not self._seq_running.is_set():
                return
            self.sig_qs_workflow.emit(3, "done")

            # ── Phase 4: ANALYZE ─────────────────────────────────────────
            # fit_ecm=True (NOT force_hppc — see _auto_analyze's docstring):
            # this record now carries a real analyzable pulse (Phase 1) even
            # though it's not an HPPC test, so attempt the same 1-RC/2-RC fit
            # without suppressing SoH, which force_hppc would do.
            self.sig_qs_workflow.emit(4, "active")
            status("QUICK ANALYZE: คำนวณ Peukert-corrected SoH + DCIR/ECM...")
            res = self.controller._auto_analyze(fit_ecm=True)
            self.sig_qs_workflow.emit(4, "done")
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
            self._seq_hw_safe_off()
            self._seq_running.clear()
            if self.controller:
                self.controller.end_session()
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_quick_scan", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------

    # ---- Cycle Life test thread -------------------------------------------

