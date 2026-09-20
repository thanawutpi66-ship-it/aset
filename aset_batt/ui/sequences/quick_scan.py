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
QUICK_OCV_MIN_REST_S = 180.0
QUICK_OCV_MAX_REST_S = 600.0
QUICK_OCV_WINDOW_S = 60.0
QUICK_OCV_MAX_SPREAD_V = 0.010
# At the normal 1C discharge rate the record can be sparse, but the terminal
# region is safety- and capacity-critical.  Enter a visibly labelled 10 Hz
# phase before reaching the actual cut-off, then stop on its first measured
# crossing rather than letting a slow debounce pull the pack below its limit.
QUICK_NEAR_CUTOFF_MARGIN_V = 0.35
QUICK_NEAR_CUTOFF_SAMPLE_HZ = 10.0


def quick_scan_1c_current(reference_capacity_ah: float, safety_max_a: float) -> float:
    """Return 1C from the selected reference Ah, limited by the safety cap."""
    target = max(0.0, float(reference_capacity_ah))
    limit = max(0.0, float(safety_max_a))
    return min(target, limit) if limit > 0.0 else target

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
            from aset_batt.core import battery_profiles
            _product = battery_profiles.get_product(
                self.controller.config.battery.product_name)
            if _product and _product.capacity_10h_ah > 0.0:
                rated = _product.capacity_10h_ah
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
        if not self._seq_common_start("btn_quick_scan", "Scanning…"):
            return
        self._spawn_sequence_worker(self._quick_scan_thread, kind="quick-scan")

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
            tail_rest_s = QUICK_OCV_MIN_REST_S
            # ── Phase 0: OCV ────────────────────────────────────────────────
            self.sig_qs_workflow.emit(0, "active")
            status("QUICK: ปิดอุปกรณ์, รอ OCV settle...")
            self.hw.psu_off()
            self.hw.load_off()
            # See the comment in _auto_sequence_thread — log from PREPARE so the CSV
            # actually contains a genuine rest window (otherwise the file only starts
            # once start_charge()/start_monitor() implicitly opens one, and
            # _quality_flags always flags "no clear rest before load").
            self.controller._ensure_logging(
                label="QuickScan",
                protocol={
                    "id": "quick-scan-v2",
                    "purpose": "screening: OCV anchor + mini-pulse electrical check + 1C discharge estimate",
                    "phases": ["OCV_SETTLE", "MINI_PULSE", "RELAX",
                               "MAIN_DISCHARGE", "TAIL_REST"],
                    "mini_pulse_s": QUICK_MINI_PULSE_S,
                    "mini_relax_s": QUICK_MINI_RELAX_S,
                    "discharge_c_rate": 1.0,
                    "tail_rest_s": tail_rest_s,
                    "tail_rest_max_s": QUICK_OCV_MAX_REST_S,
                    "sampling_target_hz": {
                        "MINI_PULSE": DEFAULT_SAMPLE_HZ,
                        "RELAX": DEFAULT_SAMPLE_HZ,
                        "MAIN_DISCHARGE": 0.2,
                        "NEAR_CUTOFF": QUICK_NEAR_CUTOFF_SAMPLE_HZ,
                        "TAIL_REST": 1.0,
                    },
                    "analysis_version": "quick-screen-v5",
                    "grade_policy": "Quick SoH uses Peukert-normalized C10-equivalent charge with valid OCV anchors; verified grade requires C10 capacity",
                },
            )

            # Trailing rest samples for the mini-pulse's ECM fit below — same
            # role as the relax leg's tail buffer in HPPC's own pulse loop:
            # identify_ecm_fit() needs to see the actual rest->pulse edge to
            # locate the step, not just the pulse's own already-loaded current.
            _rest_tail_v = []

            ocv_progress = {}
            def _ocv_progress(elapsed, v, dv_mv, st, measured_i, temp_c):
                ocv_progress.update(elapsed=elapsed, dv_mv=dv_mv, current=measured_i,
                                    temperature=temp_c, status=st)
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"QUICK PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")
                self.controller._log_sample(v, measured_i, mode="OCV", expected_dt_s=5.0)
                self.update_display(v, measured_i, self.controller.estimator.soc,
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
                min_rest_override=QUICK_OCV_MIN_REST_S,
                max_rest_override=QUICK_OCV_MAX_REST_S,
                interval_override=10.0,
                window_override=QUICK_OCV_WINDOW_S,
                spread_override=QUICK_OCV_MAX_SPREAD_V,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ valid OCV" if ocv_result == "VALID_OCV" else f"⚠ {ocv_result}"
            self.sig_alarm.emit(f"[QUICK] OCV: {v:.3f} V → SoC {soc:.1f}% ({flag})")
            try:
                from aset_batt.storage.data_utils import update_session_metadata
                chem = self.controller.config.battery.battery_type
                from aset_batt.core import battery_profiles as _profiles
                _prod = _profiles.get_product(self.controller.config.battery.product_name)
                _chem_profile = _profiles.get_chemistry(chem)
                _ref_hr = ((_prod.peukert_hr if _prod and _prod.peukert_hr > 0.0 else
                            _chem_profile.peukert_hr))
                min_rest, win_s, spread_v = self.controller._OCV_SETTLE.get(
                    chem, self.controller._OCV_SETTLE["LiPO"])
                update_session_metadata(self.controller.data.current_path, {
                    "analysis_version": "quick-screen-v5",
                    "battery_product": self.controller.config.battery.product_name,
                    "rated_capacity_basis": getattr(_prod, "capacity_rating_basis", "UNKNOWN"),
                    "product_display_capacity_ah": getattr(_prod, "product_display_capacity_ah", 0.0) or None,
                    "capacity_10h_ah": getattr(_prod, "capacity_10h_ah", 0.0) or None,
                    "capacity_20h_ah": getattr(_prod, "capacity_20h_ah", 0.0) or None,
                    "selected_reference_capacity_ah": (
                        _prod.capacity_10h_ah if _prod and _prod.capacity_10h_ah > 0.0
                        else self.controller.config.battery.rated_capacity),
                    "selected_reference_rate_hr": _ref_hr,
                    "reference_current_c10_a": (
                        getattr(_prod, "capacity_10h_ah", 0.0) / 10.0
                        if _prod and getattr(_prod, "capacity_10h_ah", 0.0) > 0.0
                        else None),
                    "quick_scan_reference_capacity_ah": (
                        _prod.capacity_10h_ah if _prod and _prod.capacity_10h_ah > 0.0
                        else self.controller.config.battery.rated_capacity),
                    "quick_scan_discharge_rate_c": 1.0,
                    "peukert_reference_capacity_ah": (
                        _prod.capacity_10h_ah if _prod and _prod.capacity_10h_ah > 0.0
                        else self.controller.config.battery.rated_capacity),
                    "peukert_reference_rate_hr": _ref_hr,
                    "ocv_start_v": v,
                    "ocv_start_valid": ocv_result == "VALID_OCV",
                    "ocv_start_status": ocv_result,
                    "ocv_start_soc_pct": soc if ocv_result == "VALID_OCV" else None,
                    "ocv_start_soc_valid": ocv_result == "VALID_OCV",
                    "ocv_start_temperature_c": ocv_progress.get("temperature"),
                    "ocv_start_rest_duration_s": ocv_progress.get("elapsed", 0.0),
                    "ocv_start_voltage_window_v": (ocv_progress.get("dv_mv", 0.0) / 1000.0),
                    "ocv_start_max_abs_current_a": self.controller._OCV_MAX_ABS_CURRENT_A,
                    "current_threshold_a": self.controller._OCV_MAX_ABS_CURRENT_A,
                    "ocv_min_rest_s": QUICK_OCV_MIN_REST_S,
                    "ocv_max_rest_s": QUICK_OCV_MAX_REST_S,
                    "ocv_window_s": QUICK_OCV_WINDOW_S,
                    "min_rest_s": QUICK_OCV_MIN_REST_S,
                    "max_rest_s": QUICK_OCV_MAX_REST_S,
                    "stability_window_s": QUICK_OCV_WINDOW_S,
                    "ocv_max_spread_v": QUICK_OCV_MAX_SPREAD_V,
                    "voltage_spread_limit_v": QUICK_OCV_MAX_SPREAD_V,
                    "ocv_voltage_selection": "last_sample_of_stable_window",
                    "battery_profile_identifier": self.controller.config.battery.product_name or chem,
                    "ocv_curve_identifier": chem,
                    "profile_version": "battery-profiles-v2",
                    "capacity_basis_version": "ytz6v-c10-c20-v1",
                })
            except Exception as exc:
                logger.warning("Quick Scan OCV metadata update failed: %s", exc)
            self.sig_qs_workflow.emit(0, "done")

            # ── Phase 1: MINI-PULSE (DCIR/ECM) ────────────────────────────
            # A short 1C pulse right after the settled anchor — gives
            # identify_dcir()/identify_ecm_fit() a real, freshly-sampled
            # transient to measure instead of the single (often stale) edge the
            # main discharge below used to be the only source of. Real HPPC data
            # on this same rig/chemistry validated R²=0.94-0.99 from an
            # identical 30s/10Hz pulse — see QUICK_MINI_PULSE_S's comment.
            self.sig_qs_workflow.emit(1, "active")
            from aset_batt.core import battery_profiles as _profiles
            _product = _profiles.get_product(
                self.controller.config.battery.product_name)
            rated = (_product.capacity_10h_ah
                     if _product and _product.capacity_10h_ah > 0.0
                     else self.controller.config.battery.rated_capacity)
            max_i    = self.controller.config.battery.max_current
            i_target = round(1.0 * rated, 2)
            i_dis    = round(quick_scan_1c_current(rated, max_i), 2)
            try:
                from aset_batt.storage.data_utils import update_session_metadata
                update_session_metadata(self.controller.data.current_path, {
                    "quick_1c_current_a": i_dis,
                    "quick_1c_nominal_current_a": i_target,
                })
            except Exception as exc:
                logger.warning("Quick Scan current metadata update failed: %s", exc)
            if i_dis < i_target:
                self.sig_alarm.emit(
                    f"[QUICK] Requested 1C = {i_target:.3f} A but limited to "
                    f"{i_dis:.3f} A by Battery Max Current")
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
            if not self._seq_check_load_trip():
                return
            # Immediate low-latency edge sample — identify_dcir()'s staleness
            # gate is 0.5s; waiting for the next paced loop iteration would blow
            # straight past it, same reasoning as the main discharge loop below.
            try:
                v_mp0, i_mp0 = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v_mp0, i_mp0, mode="MINI_PULSE", expected_dt_s=1.0 / DEFAULT_SAMPLE_HZ)
                if not self._seq_check_load_trip():
                    return
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
            t_phase = _t.perf_counter() + QUICK_MINI_PULSE_S
            while self._seq_running.is_set() and _t.perf_counter() < t_phase:
                _iter_t0 = _t.perf_counter()
                try:
                    v_mp, i_mp = self.hw.read_measurements(prefer_load_v=True)
                    if not self._seq_check_load_trip():
                        break
                    temp_mp = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    if not self._seq_check_otp(temp_mp):
                        break
                    _upd_now = _t.perf_counter()
                    state_mp = self.controller.estimator.update(
                        v_mp, i_mp, dt=max(1e-3, _upd_now - _upd_last), temp=temp_mp)
                    _upd_last = _upd_now
                    self.controller._log_sample(v_mp, i_mp, mode="MINI_PULSE", expected_dt_s=1.0 / DEFAULT_SAMPLE_HZ)
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
                self.controller._log_sample(v_mp1, i_mp1, mode="RELAX", expected_dt_s=1.0 / DEFAULT_SAMPLE_HZ)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            if not self._seq_running.is_set():
                return

            # Relaxation rest — the curve itself is data, and this also gives
            # identify_dcir() a real rest window before the main discharge edge.
            t_phase = _t.perf_counter() + QUICK_MINI_RELAX_S
            while self._seq_running.is_set() and _t.perf_counter() < t_phase:
                _iter_t0 = _t.perf_counter()
                try:
                    v_rl, psu_i_rl, load_i_rl = self.hw.read_vi()
                    if not self._seq_check_load_trip():
                        break
                    i_rl = max(abs(psu_i_rl), abs(load_i_rl))
                    i_rl_net = load_i_rl - psu_i_rl
                    temp_rl = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    if not self._seq_check_otp(temp_rl):
                        break
                    _upd_now = _t.perf_counter()
                    state_rl = self.controller.estimator.update(
                        v_rl, i_rl_net, dt=max(1e-3, _upd_now - _upd_last), temp=temp_rl)
                    _upd_last = _upd_now
                    self.controller._log_sample(v_rl, i_rl_net, mode="RELAX", expected_dt_s=1.0 / DEFAULT_SAMPLE_HZ)
                    self.update_display(v_rl, i_rl_net, state_rl["soc"], state_rl["rin"])
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
            if not self._seq_check_load_trip():
                return
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            _dis_est = self._estimate_discharge_s(i_dis)
            # Same low-latency edge sample as _auto_sequence_thread's IEC discharge —
            # this loop's own pacing (~5s) is 10x identify_dcir()'s staleness gate
            # (0.5s), so every discharge-start edge was guaranteed dropped as stale.
            try:
                v3_0, i3_0 = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v3_0, i3_0, mode="MAIN_DISCHARGE", expected_dt_s=5.0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            near_cutoff = False
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    if not self._seq_check_load_trip():
                        break
                    now    = _t.perf_counter()   # stamp AT the measurement
                    temp3  = self.hw.current_temp
                    if not self._seq_check_temp_stale():
                        break
                    dt     = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    near_cutoff = v3 <= pack_min + QUICK_NEAR_CUTOFF_MARGIN_V
                    discharge_mode = "NEAR_CUTOFF" if near_cutoff else "MAIN_DISCHARGE"
                    expected_dt = (1.0 / QUICK_NEAR_CUTOFF_SAMPLE_HZ
                                   if near_cutoff else 5.0)
                    self.controller._log_sample(v3, i3, mode=discharge_mode,
                                                expected_dt_s=expected_dt)
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
                    # A Quick Scan has a coarse 5 s capacity-log cadence.  A
                    # five-sample debounce used to pull for another ~25 s below
                    # the configured end voltage; stop on the first confirmed
                    # measured cut-off instead.
                    if v3 <= pack_min:
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[QUICK] read error: {exc}")
                    break
                if not self._seq_sleep(1.0 / QUICK_NEAR_CUTOFF_SAMPLE_HZ
                                       if near_cutoff else 5.0):
                    break
            # Fresh pre-edge sample right before load-off — the real Quick Scan
            # CSV this fix is grounded in was MISSING the discharge-end edge
            # entirely (file ended under load at cutoff), so identify_dcir()
            # never had an OFF transition to measure at all. Mirrors the
            # pre-edge pattern already used at every load ON transition above.
            try:
                v3_end, i3_end = self.hw.read_measurements(prefer_load_v=True)
                self.controller._log_sample(v3_end, i3_end, mode="MAIN_DISCHARGE", expected_dt_s=5.0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.hw.set_load(False)
            # Immediate low-latency edge sample for the OFF transition itself.
            try:
                v3_off, i3_off = self.hw.read_measurements(prefer_load_v=False)
                self.controller._log_sample(v3_off, i3_off, mode="TAIL_REST", expected_dt_s=1.0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_qs_workflow.emit(2, "done")
            self.sig_alarm.emit("[QUICK] Discharge complete (1C) — Peukert correction applied in analysis")

            # ── Phase 3: TAIL REST (bounded OCV stabilization) ────────────
            self.sig_qs_workflow.emit(3, "active")
            status("QUICK: stabilizing end OCV (180–600 s)...")
            tail_t0 = _t.perf_counter()
            tail_samples = []
            tail_last_t = None
            end_ocv = {"valid": False, "status": "NOT_RESTED", "voltage_v": None,
                       "temperature_c": None, "rest_s": 0.0}
            while (self._seq_running.is_set()
                   and time.perf_counter() - tail_t0 < QUICK_OCV_MAX_REST_S):
                try:
                    v_tail, psu_i_tail, load_i_tail = self.hw.read_vi()
                    i_tail = max(abs(psu_i_tail), abs(load_i_tail))
                    i_tail_net = load_i_tail - psu_i_tail
                    tail_temp = self.hw.current_temp
                    self.controller._log_sample(v_tail, i_tail_net, mode="TAIL_REST", expected_dt_s=1.0)
                    self.update_display(v_tail, i_tail_net, self.controller.estimator.soc,
                                        self.controller.estimator.rin)
                    tail_now = time.perf_counter() - tail_t0
                    tail_valid = (not self.hw.temp_is_stale()
                                  and (tail_last_t is None or tail_now - tail_last_t <= 2.5))
                    tail_samples.append((tail_now, v_tail, i_tail, tail_temp, tail_valid))
                    tail_last_t = tail_now
                    if (tail_now >= QUICK_OCV_MIN_REST_S
                            and int(tail_now - QUICK_OCV_MIN_REST_S) % 10 == 0):
                        from aset_batt.acquisition.ocv_validation import evaluate_quick_ocv_window
                        end_ocv = evaluate_quick_ocv_window(
                            tail_samples, outputs_off=True,
                            max_abs_current_a=self.controller._OCV_MAX_ABS_CURRENT_A,
                            now_s=tail_now)
                        if end_ocv["valid"]:
                            # np.interp in get_soc_from_ocv clamps outside the
                            # calibrated curve. Reject those readings before
                            # allowing them to become a valid SoC anchor.
                            model = self.controller.estimator.battery_model
                            oor_mv = model.ocv_out_of_range_mv(
                                end_ocv["voltage_v"], end_ocv["temperature_c"])
                            if oor_mv != 0.0:
                                end_ocv["valid"] = False
                                end_ocv["status"] = "OUT_OF_RANGE"
                                end_ocv["out_of_range_mv"] = oor_mv
                            else:
                                break
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                if not self._seq_sleep(1.0):
                    break
            if not self._seq_running.is_set():
                return
            if not end_ocv["valid"] and time.perf_counter() - tail_t0 >= QUICK_OCV_MAX_REST_S:
                end_ocv["status"] = "OCV_TIMEOUT"
                end_ocv["rest_s"] = QUICK_OCV_MAX_REST_S
            self.sig_qs_workflow.emit(3, "done")

            # Validation keeps the established last-sample voltage selection
            # within the accepted final 60-second stability window.
            end_soc = (self.controller.estimator.battery_model.get_soc_from_ocv(
                end_ocv["voltage_v"], end_ocv["temperature_c"])
                if end_ocv["valid"] else None)
            try:
                from aset_batt.storage.data_utils import update_session_metadata
                update_session_metadata(self.controller.data.current_path, {
                    "ocv_end_v": end_ocv["voltage_v"],
                    "ocv_end_soc_pct": end_soc,
                    "ocv_end_valid": end_ocv["valid"],
                    "ocv_end_status": end_ocv["status"],
                    "ocv_end_rest_s": end_ocv["rest_s"],
                    "ocv_end_temp_c": end_ocv["temperature_c"],
                    "ocv_end_min_rest_s": QUICK_OCV_MIN_REST_S,
                    "ocv_end_max_rest_s": QUICK_OCV_MAX_REST_S,
                    "ocv_end_window_s": QUICK_OCV_WINDOW_S,
                    "ocv_end_max_spread_v": QUICK_OCV_MAX_SPREAD_V,
                    "ocv_end_max_abs_current_a": self.controller._OCV_MAX_ABS_CURRENT_A,
                    "ocv_end_current_threshold_a": self.controller._OCV_MAX_ABS_CURRENT_A,
                    "current_threshold_a": self.controller._OCV_MAX_ABS_CURRENT_A,
                    "voltage_spread_limit_v": QUICK_OCV_MAX_SPREAD_V,
                    "ocv_end_voltage_selection": "last_sample_of_stable_window",
                    "tail_rest_observation_v": tail_samples[-1][1] if tail_samples else None,
                    "tail_rest_observation_valid_ocv": end_ocv["valid"],
                })
            except Exception as exc:
                logger.warning("Quick Scan end OCV metadata update failed: %s", exc)

            # ── Phase 4: ANALYZE ─────────────────────────────────────────
            # fit_ecm=True (NOT force_hppc — see _auto_analyze's docstring):
            # this record now carries a real analyzable pulse (Phase 1) even
            # though it's not an HPPC test, so attempt the same 1-RC/2-RC fit
            # without suppressing SoH, which force_hppc would do.
            self.sig_qs_workflow.emit(4, "active")
            status("QUICK ANALYZE: คำนวณค่าประมาณ Quick SoH และ screening grade...")
            res = self.controller._auto_analyze(fit_ecm=True)
            self.sig_qs_workflow.emit(4, "done")
            if res:
                self.sig_seq_result.emit(format_seq_result(res))
            status("QUICK SCAN เสร็จ — ดู Quick Scan Grade และ Peukert SoH ที่แท็บ Analytics")
            self.sig_alarm.emit("[QUICK] Scan complete ✓")
            grade_str = res.get("quick_grade", res.get("grade", "?")) if res else "?"

            # --- Google Sheets Integration ---
            if res:
                try:
                    from aset_batt.app.gsheet_reporter import report_to_gsheet
                    b_name = self.controller.config.battery.battery_type
                    soh_val = res.get("soh_est", res.get("soh", float('nan')))
                    dcir_val = res.get("dcir_mohm", res.get("ri_mohm", float('nan')))
                    report_to_gsheet(b_name, grade_str, soh_val, dcir_val)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"GSheet reporting error: {e}")
            # ---------------------------------

            electrical = res.get("electrical_grade", "REVIEW") if res else "REVIEW"
            verified = res.get("overall_grade", "REVIEW") if res else "REVIEW"
            self.sig_seq_done.emit("Quick Scan Complete",
                                   f"Quick Scan Grade: {grade_str}  |  Electrical: {electrical}\n"
                                   f"Verified C10 Grade: {verified}\n"
                                   "ดูผลเพิ่มเติมที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[QUICK] Error: {exc}")
            status(f"QUICK Error: {exc}")
        finally:
            self._seq_hw_safe_off()
            self._seq_running.clear()
            if self.controller:
                self.controller.end_session(
                    "completed" if completed_ok else "safety_tripped" if self._seq_safety_reason else "aborted",
                    "quick scan completed" if completed_ok else self._seq_safety_reason or "quick scan cancelled or failed",
                )
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_quick_scan", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- result formatting: see aset_batt/ui/report_html.py ---------------

    # ---- HPPC full-sequence thread ----------------------------------------

    # ---- Cycle Life test thread -------------------------------------------

