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


# Adaptive HPPC relax (G3 fix): a relax leg shorter than ~3τ truncates the RC
# tail, biasing the next pulse's fitted R1/C1/τ systematically LOW (fit R² can
# look excellent over the too-short window it was given). From cycle 2 the relax
# leg extends to ≥3× the PREVIOUS cycle's own fitted τ — per-unit, not the
# chemistry's order-of-magnitude guess (lead-acid's generic τ says 10-60 s, but
# the real FB FTZ6V measured τ≈4-5 s across all 5 pulses of
# test_HPPC_20260708_152502). Extend only — the configured relax_s is a floor —
# and cap so a pathological fit (τ=200 s from a noisy pulse) can't stall the
# sequence for an hour.
_RELAX_CAP_S = 300.0
# Settle early-exit for the extension: once the configured floor has elapsed,
# a voltage flat to <1 mV over two consecutive 10 s windows is settled — waiting
# out the rest of 3τ̂ adds nothing to the next fit's anchor.
_SETTLE_DV_V = 0.001
_SETTLE_WIN_S = 10.0


def effective_relax_s(relax_s: float, tau_fit: float) -> float:
    """Relax duration for this cycle: max(configured, 3×fitted-τ), capped.
    tau_fit=0 (no fit yet — cycle 1, or every fit so far failed) → configured."""
    return min(max(relax_s, 3.0 * tau_fit), _RELAX_CAP_S)


def relax_settled(settle_win: list, now_w: float, t_relax0: float,
                  relax_s: float, relax_eff: float) -> bool:
    """True when the adaptive relax EXTENSION may end early: the configured
    relax_s floor has elapsed, an extension is actually in effect, and voltage
    has been flat (ΔV < _SETTLE_DV_V) in each of the last two consecutive
    _SETTLE_WIN_S windows. ``settle_win`` is the [(wall_t, v), ...] buffer the
    relax loop maintains (pruned to the last 2×_SETTLE_WIN_S by the caller)."""
    if relax_eff <= relax_s or (now_w - t_relax0) < relax_s:
        return False
    if not settle_win or now_w - settle_win[0][0] < 2.0 * _SETTLE_WIN_S - 1.0:
        return False                       # window not full yet
    cut = now_w - _SETTLE_WIN_S
    v1 = [s[1] for s in settle_win if s[0] < cut]
    v2 = [s[1] for s in settle_win if s[0] >= cut]
    return (len(v1) >= 2 and len(v2) >= 2
            and max(v1) - min(v1) < _SETTLE_DV_V
            and max(v2) - min(v2) < _SETTLE_DV_V)


# SoC-sweep HPPC (G1/G2 fix): FreedomCAR sweeps pulse sets across SoC levels
# (typically every 10%) instead of pulsing only once at 100% SoC right after a
# full charge — the current default. These two helpers are pure math kept
# module-level (same as effective_relax_s/relax_settled above) so they're
# unit-testable without a running thread.

def soc_sweep_done(soc_now: float, soc_floor_pct: float) -> bool:
    """True once the sweep has discharged down to (or past) the configured
    floor and should stop scheduling further SoC levels."""
    return soc_now <= soc_floor_pct


def discharge_step_ah_target(rated_capacity_ah: float, soc_step_pct: float) -> float:
    """Ah to remove for one SoC-sweep discharge step (e.g. 10% of rated ->
    0.1*rated). Pure conversion for the ETA display only — the actual runtime
    stop condition inside the thread compares live estimator.soc directly
    (the same trusted value every other view already polls), not this number,
    so a second independent Ah accumulator can never disagree with it."""
    return max(0.0, rated_capacity_ah) * max(0.0, soc_step_pct) / 100.0


# Regen (charge) pulse leg (G6 fix): FreedomCAR's real HPPC profile is
# discharge-pulse -> rest -> regen(charge)-pulse -> rest, but this sequence
# only ever pulsed discharge. Pure helpers, same reasoning as the two above.

def regen_pulse_current(i_pulse: float, regen_fraction: float = 0.75) -> float:
    """Regen (charge-direction) pulse magnitude: FreedomCAR standard is 75% of
    the discharge pulse current. Returns a POSITIVE magnitude (A) — the caller
    supplies it to set_psu()'s current-limit argument; the actual current sign
    comes back negative from read_measurements(prefer_load_v=False) on its own
    (charge = PSU active), nothing here needs to negate anything."""
    return max(0.0, i_pulse) * max(0.0, regen_fraction)


def regen_allowed(soc_now: float, soc_ceiling_pct: float) -> bool:
    """True when live SoC is still below the configured regen ceiling — the
    gate that SKIPS (not aborts) a regen leg once the pack is high enough that
    pushing more charge in via a pulse is unsafe/uninformative."""
    return soc_now < soc_ceiling_pct


class HppcMixin:
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
                f"OCV: {v_now:.3f} V  ·  Temp: {self.hw.current_temp:.1f} °C",
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
            "soc_sweep_enabled": self.chk_hppc_soc_sweep.isChecked(),
            "soc_step_pct": self.ed_hppc_soc_step.text(),
            "soc_floor_pct": self.ed_hppc_soc_floor.text(),
            "regen_enabled": self.chk_hppc_regen.isChecked(),
        }
        import threading
        threading.Thread(target=self._hppc_seq_thread, args=(opts,), daemon=True).start()

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
        hppc_safety_tripped = False   # UVP/OTP mid-cycle — see PHASE 3 below
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
            # See the same comment in _auto_sequence_thread — log PREPARE's rest from
            # the start so the CSV actually contains a genuine rest window.
            self.controller._ensure_logging(label="HPPC")

            def _ocv_progress_factory(prefix, total_estimate=None):
                """Shared on_progress callback for calibrate_from_ocv_stable(),
                used by both the PREPARE (PHASE 0) and post-charge (PHASE 2) OCV
                settle calls below.

                calibrate_from_ocv_stable() can internally trigger
                _bleed_off_surface_charge() (a real C/20 discharge) when the
                rested voltage reads above the OCV curve's 100% point — that
                bleed loop already logs its own (v, i_bleed) samples via
                _log_sample(). This callback used to ALSO call
                _log_sample(v, 0.0) unconditionally on every progress tick,
                including during "bleeding" — so every real bled sample got a
                second, contradictory row logged right next to it with a
                hardcoded current of 0.0 A. A real run
                (test_HPPC_20260708_152502) had 137 of these fake zero-current
                rows / 272 fake current edges: the rest-median OCV anchor read
                wrong (load voltage tagged as rest), the coulomb integral on
                replay was off, and the CSV claimed no current flowed during a
                window where 0.264 A was actually being drawn. Skip the
                duplicate log (and the display update, which would show the
                same fake 0.0 A) whenever status is "bleeding" — the status
                text above still updates every tick so the operator still sees
                live feedback.
                """
                def _cb(elapsed, v, dv_mv, st):
                    dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                    status(f"{prefix}: {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")
                    if total_estimate is not None:
                        self.sig_phase_progress.emit(
                            int(elapsed), max(total_estimate, int(elapsed) + 30))
                    if st == "bleeding":
                        return
                    self.controller._log_sample(v, 0.0)
                    self.update_display(v, 0.0, self.controller.estimator.soc,
                                        self.controller.estimator.rin)
                return _cb

            # No bleed-off here: PHASE 1 below always runs a full CC-CV charge to
            # termination current regardless of this reading (no skip-charge
            # branch, unlike IEC/AUTO) — bleeding surface charge off only to have
            # the charger immediately put it right back (plus more) wastes
            # ~5-10 min for zero effect on the test outcome. This reading only
            # feeds the charge-duration ETA estimate below, not anything
            # accuracy-critical. Real bug seen on a pack charged the day before:
            # 12.91V (above the 100% point) triggered a bleed, then charged
            # CC-CV anyway.
            soc0_ocv, v0_ocv, ocv_result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress_factory("HPPC SEQ: OCV settle"),
                cancel_check=self._seq_running.is_set,
                allow_bleed_off=False,
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
            self.controller.start_charge(strategy=None, reuse_session=True)
            _ch_t0 = _t.time()
            _soc0 = getattr(self.controller.estimator, "soc", 50.0)
            _cp = battery_profiles.get_chemistry(
                self.controller.config.battery.battery_type).charge
            _ch_est = self._estimate_charge_s(_soc0, _cp.bulk_c_rate or 0.1)
            _tail_t_hist, _tail_i_hist = [], []
            while self._seq_running.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                try:
                    v_c, i_c, _ = self.hw.read_vi()
                    i_c = max(0.0, i_c)
                    elapsed_ch = int(_t.time() - _ch_t0)
                    status(self._charge_status_text(v_c, i_c, elapsed_ch, prefix="HPPC CHARGE"))
                    ctrl = getattr(self.controller, "_charge_ctrl", None)
                    if getattr(ctrl, "stage", None) in ("absorption", "cv"):
                        _tail_t_hist.append(elapsed_ch)
                        _tail_i_hist.append(i_c)
                        _ch_est = self._project_tail_eta(
                            _tail_t_hist, _tail_i_hist, ctrl.params.tail_current_a,
                            elapsed_ch, _ch_est)
                    self.sig_phase_progress.emit(elapsed_ch, max(_ch_est, elapsed_ch + 30))
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                if not self._seq_sleep(30.0):
                    break
            if self.controller.monitor_running:   # see the same comment in _auto_sequence_thread
                self.controller.stop_monitor()
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_hppc_seq_wf.emit(1, "done")
            self.sig_alarm.emit("[HPPC SEQ] Charge complete")

            # ── PHASE 2: REST (OCV settle, auto bleed-off surface charge) ──
            # Used to be a fixed 30-min timer + a single immediate
            # calibrate_from_ocv() read, with only a passive advisory warning
            # if the rest voltage was still above the OCV curve's 100% point
            # (see test_HPPC_20260708_152502: rest voltage stayed 430 mV over
            # range, the advisory fired, and the sequence pulsed on a still
            # surface-charged battery anyway — the CHARGE phase re-creates
            # exactly the surface charge PREPARE's own bleed-off had already
            # stripped, and nothing here repeated that bleed). Now reuses the
            # same calibrate_from_ocv_stable() PHASE 0 uses: real ΔV/Δt
            # settle-checking PLUS an automatic C/20 bleed-off when the
            # settled reading is still above range — instead of just warning
            # about it.
            self.sig_hppc_seq_wf.emit(2, "active")
            soc_h, v_h, ocv_result2 = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress_factory("HPPC REST (OCV settle)",
                                                  total_estimate=1800),
                cancel_check=self._seq_running.is_set,
            )
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            flag2 = "✓ settled" if ocv_result2 == "settled" else "⚠ timeout"
            self.sig_alarm.emit(
                f"[HPPC SEQ] Post-charge OCV: {v_h:.3f} V → SoC {soc_h:.1f}% ({flag2})")
            # Surface-charge advisory before the pulses: even with the automatic
            # bleed-off above, a real specimen can simply rest above this chemistry's
            # generic OCV curve (see CLAUDE.md) — keep this as a final confirmation
            # rather than assuming the bleed-off always fully clears it. A real run
            # (test_HPPC_20260708_152502) started its pulses with the rest voltage
            # still ABOVE the OCV curve's own 100% point, and the per-pulse rest
            # anchor then drifted 13.34→13.15 V across the 5 cycles — the raw edge
            # R0 declined 41.5→30.2 mΩ (37%) purely from that anchor drift, not from
            # the battery. The fit itself is protected (median-of-tail voc + voc-
            # divergence warning), but the operator should know THIS run's R0
            # spread will be inflated.
            try:
                _over_mv = self.controller.estimator.battery_model.ocv_out_of_range_mv(
                    v_h, self.hw.current_temp)
                if _over_mv > 0.0:
                    self.sig_alarm.emit(
                        f"[HPPC SEQ] ⚠ rest voltage {v_h:.3f} V is still "
                        f"{_over_mv:.0f} mV above the OCV curve's 100% point even "
                        f"after settle+bleed-off — surface charge not fully "
                        f"dissipated (or this specimen simply rests above the "
                        f"chemistry's generic OCV curve); per-pulse R0 anchors "
                        f"will drift downward across cycles — treat this run's "
                        f"R0 spread as inflated")
            except Exception:
                pass
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
            # If relax_s never reaches the chemistry's own _min_rest_s, the EKF's
            # still_polarised gate (state_estimator.py) stays true for the WHOLE
            # relax leg — the per-sample estimator.update() call still counts
            # coulombs, but the voltage-measurement correction never fires. Warn
            # once so a lead-acid/LFP run on the 30 s UI default isn't silently
            # missing half of what "relax leg now feeds the estimator" implies.
            _min_rest_s = getattr(self.controller.estimator, "_min_rest_s", 0.0)
            if relax_s < _min_rest_s:
                self.sig_alarm.emit(
                    f"[HPPC SEQ] relax_s={relax_s:.0f}s is shorter than this "
                    f"chemistry's rest-settle time ({_min_rest_s:.0f}s) — the relax "
                    f"leg's estimator update will only count coulombs, the voltage "
                    f"correction won't fire this run")
            # G3: a relax leg shorter than ~3τ captures only part of the RC decay,
            # so the fitted R1/C1 (and the τ = R1·C1 read off it) come out
            # systematically biased LOW no matter how high the fit R² looks — R²
            # only measures the fit over the window it was given, and a too-short
            # window can look excellent while missing the tail. From cycle 2 the
            # relax leg now AUTO-EXTENDS to ≥3× the previous cycle's own fitted τ
            # (see the adaptive-relax block in the cycle loop below) — cycle 1
            # still runs on the configured value because no per-unit τ exists yet,
            # so warn when that first leg is short vs the chemistry's generic τ
            # guess. (Phase-A note: the chemistry default τ for lead-acid is an
            # order-of-magnitude guess of 10-60 s, but the real FB FTZ6V measured
            # τ≈4-5 s — the per-unit fit, not this guess, is what the adaptive
            # extension trusts.)
            try:
                _r0d, _r1d, _c1d = self.controller.estimator._ekf_rc_defaults()
                _tau_est = _r1d * _c1d
            except Exception:
                _tau_est = 0.0
            if _tau_est > 1.0 and relax_s < 3.0 * _tau_est:
                self.sig_alarm.emit(
                    f"[HPPC SEQ] relax_s={relax_s:.0f}s captures only "
                    f"~{relax_s / _tau_est:.1f}τ of the RC tail (chemistry guess "
                    f"τ≈{_tau_est:.0f}s) — cycle 1's R1/C1/τ may be biased low; later "
                    f"cycles auto-extend relax to ≥3× the previous cycle's fitted τ")
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

            # SoC-sweep (G1/G2 fix): pulse at multiple SoC levels instead of only
            # once at 100% right after the mandatory full charge. Independent
            # toggle — off makes the outer level loop below run exactly once,
            # preserving the single-level behavior byte-for-byte.
            soc_sweep_enabled = bool(opts.get("soc_sweep_enabled", False))
            try:
                soc_step_pct = max(1.0, float(opts.get("soc_step_pct") or "10"))
                soc_floor_pct = max(0.0, float(opts.get("soc_floor_pct") or "20"))
            except (ValueError, AttributeError):
                soc_step_pct, soc_floor_pct = 10.0, 20.0

            # Regen (charge) pulse leg (G6 fix): FreedomCAR's real HPPC profile is
            # discharge-pulse -> rest -> regen(charge)-pulse -> rest, but this
            # sequence only ever pulsed discharge. Independent toggle — off makes
            # the regen block below a no-op, preserving the exact discharge-only
            # behavior byte-for-byte. i_regen/ceiling computed once (constant per
            # sequence, same as i_pulse above), not recomputed per cycle.
            regen_enabled = bool(opts.get("regen_enabled", False))
            i_regen = regen_pulse_current(i_pulse)
            pack_max = self.controller.config.battery.pack_max_voltage
            hppc_regen_ceiling = self._ovp_ceiling()
            if hppc_regen_ceiling <= 0 or hppc_regen_ceiling <= pack_max:
                hppc_regen_ceiling = pack_max * 1.05

            self.controller._ensure_logging(label="HPPC")
            self.hw.psu_off()
            self.hw.load_off()
            v_r = None   # rested voltage from the relax leg — voc for each cycle's ECM fit
            # Real per-sample dt for estimator.update() in both legs below — one clock
            # across relax/pulse/discharge-step boundaries so no interval is ever
            # dropped or doubled.
            _upd_last = _t.perf_counter()
            _rate_warned = False   # once-per-sequence low-sample-rate alarm
            # Adaptive relax (G3 fix): previous cycle's own fitted τ — local, not
            # instance state (CLAUDE.md: mixins hold no state). 0.0 until the first
            # successful per-cycle fit, so cycle 1 (of each SoC level) runs the
            # configured relax_s.
            _tau_fit = 0.0

            level = 0
            while self._seq_running.is_set():
                level += 1
                _level_label = f" [SoC lvl {level}]" if soc_sweep_enabled else ""

                if soc_sweep_enabled and level > 1:
                    # ── SoC-sweep DISCHARGE step ─────────────────────────────
                    # Same shape as cycle_life.py's own discharge step (set_load
                    # -> paced loop -> ah tracking not needed here, stop condition
                    # is live SoC), UVP-floor abort identical to the relax leg's.
                    soc_before = self.controller.estimator.soc
                    target_level_soc = max(soc_floor_pct, soc_before - soc_step_pct)
                    status(f"HPPC SoC-SWEEP: discharging toward ~{target_level_soc:.0f}%...")
                    self.sig_alarm.emit(
                        f"[HPPC SEQ] SoC-sweep: discharging level {level - 1}→{level} "
                        f"(target ~{target_level_soc:.0f}%)")
                    try:
                        v_pre, i_pre, _ = self.hw.read_vi()
                        self.controller._log_sample(v_pre, i_pre)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    self.hw.set_load(True, str(i_pulse))
                    try:
                        v_s0, i_s0 = self.hw.read_measurements(prefer_load_v=True)
                        self.controller._log_sample(v_s0, i_s0)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    _step_cutoff_n = 0
                    while self._seq_running.is_set():
                        try:
                            v_s, i_s = self.hw.read_measurements(prefer_load_v=True)
                            temp_s = self.hw.current_temp
                            if not self._seq_check_temp_stale():
                                hppc_safety_tripped = True
                                break
                            if not self._seq_check_otp(temp_s):
                                hppc_safety_tripped = True
                                break
                            _upd_now = _t.perf_counter()
                            state_s = self.controller.estimator.update(
                                v_s, i_s, dt=max(1e-3, _upd_now - _upd_last), temp=temp_s)
                            _upd_last = _upd_now
                            self.controller._log_sample(v_s, i_s)
                            self.update_display(v_s, i_s, state_s["soc"], state_s["rin"], temp_s)
                            self._seq_kick_watchdog()
                            status(f"HPPC SoC-SWEEP: {v_s:.3f} V  SoC {state_s['soc']:.1f}% "
                                   f"(target {target_level_soc:.0f}%)")
                            if v_s <= pack_min:
                                self._seq_running.clear()
                                hppc_safety_tripped = True
                                reason = (f"Under-voltage during HPPC SoC-sweep discharge: "
                                          f"{v_s:.3f}V ≤ {pack_min:.3f}V cutoff")
                                self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                                self.sig_wf_status.emit(f"⛔ {reason}")
                                break
                            # Same 5-sample debounce idiom as other discharge-cutoff
                            # checks in this codebase (worker.py CC_DISCHARGE,
                            # cycle_life.py) — here against the live estimator SoC,
                            # not voltage, since this is a targeted "remove X% SoC"
                            # step rather than a capacity-test cutoff.
                            _step_cutoff_n = (_step_cutoff_n + 1) if state_s["soc"] <= target_level_soc else 0
                            if _step_cutoff_n >= 5:
                                break
                        except Exception as exc:
                            self.sig_alarm.emit(f"[HPPC SEQ] SoC-sweep discharge read error: {exc}")
                            break
                        if not self._seq_sleep(5.0):
                            break
                    self.hw.load_off()
                    if not self._seq_running.is_set():
                        break

                    # ── Re-anchor after the step's rest ──────────────────────
                    # CRITICAL: state_estimator.py's still_polarised gate skips the
                    # EKF's voltage-measurement correction for the ENTIRE duration
                    # of an active discharge — SoC tracking during the step above
                    # is coulomb-counting only, with no correction until the pack
                    # genuinely rests. Uncorrected, this drift compounds across
                    # sweep levels. Re-anchor with the exact same call PREPARE/
                    # PHASE 2 already use; allow_bleed_off=False for the same
                    # reason PREPARE uses it — about to continue discharging/
                    # pulsing, not making a charge/skip decision.
                    status(f"HPPC SoC-SWEEP level {level}: OCV settle...")
                    soc_lvl, v_lvl, ocv_result_lvl = self.controller.calibrate_from_ocv_stable(
                        on_progress=_ocv_progress_factory(f"HPPC SoC-SWEEP level {level} settle"),
                        cancel_check=self._seq_running.is_set,
                        allow_bleed_off=False,
                    )
                    if not self._seq_running.is_set():
                        break
                    flag_lvl = "✓ settled" if ocv_result_lvl == "settled" else "⚠ timeout"
                    self.sig_alarm.emit(
                        f"[HPPC SEQ] SoC-sweep level {level}: {v_lvl:.3f} V → "
                        f"SoC {soc_lvl:.1f}% ({flag_lvl})")
                    # τ genuinely varies with SoC — don't carry a stale/foreign
                    # estimate from a different level into this level's adaptive
                    # relax extension. Cycle 1 of every level falls back to the
                    # configured relax_s floor, same as today's cycle 1 does.
                    _tau_fit = 0.0
                    if soc_sweep_done(soc_lvl, soc_floor_pct):
                        self.sig_alarm.emit(
                            f"[HPPC SEQ] SoC-sweep reached floor ({soc_lvl:.1f}% ≤ "
                            f"{soc_floor_pct:.0f}%) — stopping after this level")
                        break

                # Per-level progress-bar reset: the number of SoC-sweep levels
                # isn't knowable up front (depends on live discharge rate and when
                # estimator.soc crosses the floor), so this intentionally shows
                # per-level progress rather than a whole-sweep ETA with false
                # precision — see sig_alarm below for level-level visibility.
                _hppc_t0 = _t.time()
                _hppc_total = n_cyc * (relax_s + pulse_s)
                if regen_enabled:
                    _hppc_total += n_cyc * (relax_s + pulse_s)
                if soc_sweep_enabled:
                    self.sig_alarm.emit(
                        f"[HPPC SEQ] Starting SoC-sweep level {level} "
                        f"(~{self.controller.estimator.soc:.0f}%)")

                for cyc in range(1, n_cyc + 1):
                    if not self._seq_running.is_set():
                        break
                    # Relax (REST) leg. From cycle 2, extend to ≥3× the previous
                # cycle's fitted τ (capped) so R1/C1/τ aren't biased low by a
                # too-short window — extend ONLY; the configured relax_s is the
                # floor. Once the configured floor has elapsed, the extension may
                # end early if the voltage has genuinely settled — see
                # effective_relax_s/relax_settled (module level, unit-tested).
                relax_eff = effective_relax_s(relax_s, _tau_fit)
                if relax_eff > relax_s + 0.5:
                    status(f"HPPC {cyc}/{n_cyc}{_level_label}: REST {relax_eff:.0f}s "
                           f"(auto-extended from {relax_s:.0f}s, 3×τ̂={3.0 * _tau_fit:.0f}s)...")
                else:
                    status(f"HPPC {cyc}/{n_cyc}{_level_label}: REST {relax_s:.0f}s...")
                try:
                    from aset_batt.storage.cloud_push import set_cloud_meta
                    set_cloud_meta(sub_phase="relax", cycle_index=cyc, cycle_total=n_cyc)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                # Trailing rest samples for the upcoming pulse's ECM fit —
                # identify_ecm_fit() needs to see the actual rest->pulse edge to
                # locate the step (fit_model._detect_step()), not just the pulse's
                # own already-loaded current throughout.
                _relax_tail_v = []
                _t_relax0 = _t.time()
                t_phase = _t_relax0 + relax_eff
                _settle_win = []   # (wall_t, v) for the settle early-exit check
                while self._seq_running.is_set() and _t.time() < t_phase:
                    _iter_t0 = _t.perf_counter()
                    try:
                        v_r, _, _ = self.hw.read_vi()
                        temp_h = self.hw.current_temp
                        # Stale-temp escalation (G8) — IEC/Quick Scan discharge loops
                        # have had this since the industrial-grade audit; checked BEFORE
                        # feeding the estimator/CSV (same check-then-feed order as those
                        # loops) so a stale ESP32 reading never contaminates the
                        # Arrhenius-compensated fit or gets written to disk before the
                        # sequence aborts.
                        if not self._seq_check_temp_stale():
                            hppc_safety_tripped = True
                            break
                        if not self._seq_check_otp(temp_h):
                            hppc_safety_tripped = True
                            break
                        # estimator.update() per-sample — this leg deliberately did NOT
                        # call it for a long time ("R0/R1/C1 are fit afterwards"), but
                        # that froze coulomb counting for the entire pulse/relax phase:
                        # a real test's 5 pulses removed 0.226Ah (4.3% of rated) that
                        # was never counted anywhere, and the very next capacity test
                        # (charge skipped on a surface-charge OCV misread) then
                        # reported that exact missing charge as "SoH 95.66%" on a
                        # healthy pack (100% − 4.27% = 95.73%, matching within 0.07%).
                        # Safe to feed now: the monitor loop is stopped for the whole
                        # sequence (no double counting), and the EKF's uncalibrated-R0
                        # runaway guard + step detector are in place. Feeding the relax
                        # leg also keeps the R0 step detector's rolling buffer primed
                        # with rest samples so it can fire on each pulse edge.
                        _upd_now = _t.perf_counter()
                        state_r = self.controller.estimator.update(
                            v_r, 0.0, dt=max(1e-3, _upd_now - _upd_last),
                            temp=temp_h)
                        _upd_last = _upd_now
                        self.controller._log_sample(v_r, 0.0)
                        _relax_tail_v.append(v_r)
                        if len(_relax_tail_v) > 5:
                            _relax_tail_v.pop(0)
                        self.update_display(v_r, 0.0, state_r["soc"], state_r["rin"])
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_r <= pack_min:
                            self._seq_running.clear()
                            hppc_safety_tripped = True
                            reason = (f"Under-voltage during HPPC rest: "
                                      f"{v_r:.3f}V ≤ {pack_min:.3f}V cutoff")
                            self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                            self.sig_wf_status.emit(f"⛔ {reason}")
                            if self.controller:
                                self.controller._trigger_safety(reason)
                            break
                        # Settle early-exit for the ADAPTIVE extension only —
                        # checked AFTER the rest-UVP trip above so a settled-but-
                        # empty pack still aborts instead of pulsing.
                        _now_w = _t.time()
                        _settle_win.append((_now_w, v_r))
                        while _settle_win and _now_w - _settle_win[0][0] > 2.0 * _SETTLE_WIN_S:
                            _settle_win.pop(0)
                        if relax_settled(_settle_win, _now_w, _t_relax0,
                                         relax_s, relax_eff):
                            logger.info(
                                "HPPC %d/%d relax settled early at %.0fs of %.0fs "
                                "(ΔV < %.0f mV over 2×%.0fs)",
                                cyc, n_cyc, _now_w - _t_relax0, relax_eff,
                                _SETTLE_DV_V * 1e3, _SETTLE_WIN_S)
                            break
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    # _seq_sleep (not a bare sleep) so the 5-min "no measurement"
                    # watchdog actually gets checked — a hung hw.read_vi() call used
                    # to freeze this whole loop forever with no way out.
                    #
                    # Paced to DEFAULT_SAMPLE_HZ (battery_model.py — shared target
                    # across the whole app, was a hardcoded "0.2" here independent
                    # of worker.py's own config), same technique as
                    # AutoController._monitor_loop: sleep only the time REMAINING
                    # after the SCPI round-trip, not a flat 1 s on top of it — this
                    # was 1 Hz before, well under the 5 Hz identify_ecm_fit()'s own
                    # docstring assumes ("30s pulse at 5Hz gives ~150 points... R1/C1
                    # are well-resolved at 5Hz") for a good R1/C1 fit. Real achieved
                    # rate will land somewhat under target once USB/SCPI latency
                    # (~40-200 ms) is accounted for — still a large improvement over 1 Hz.
                    _elapsed_iter = _t.perf_counter() - _iter_t0
                    if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                        break
                if not self._seq_running.is_set():
                    break
                # Pulse leg
                self.hw.set_load(True, str(i_pulse))
                # Capture one sample immediately after the SCPI command returns, before
                # the ~0.2s-paced while loop below even starts its first iteration —
                # identify_dcir()'s single-step method needs a post-edge sample within
                # _DCIR_MAX_STEP_DT (0.5s) of the true current transition, and waiting
                # for the next full loop iteration stacks a whole pacing period on top
                # of set_load()'s own serial round-trip, which was regularly pushing
                # real pulses past the gate and dropping them as "stale" (n_stale).
                try:
                    v_p0, i_p0 = self.hw.read_measurements(prefer_load_v=True)
                    self.controller._log_sample(v_p0, i_p0)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                status(f"HPPC {cyc}/{n_cyc}{_level_label}: PULSE {pulse_s:.0f}s  {i_pulse:.3f} A")
                try:
                    from aset_batt.storage.cloud_push import set_cloud_meta
                    set_cloud_meta(sub_phase="pulse", cycle_index=cyc, cycle_total=n_cyc,
                                   pulse_current_a=i_pulse)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                # Buffer this cycle's own pulse curve for a live per-cycle ECM fit
                # once the pulse ends — see the fit-and-feed block after the loop.
                # voc = MEDIAN of the trailing relax samples, not just the single last
                # reading: real relax-end voltages are still visibly declining at the
                # end of a practical relax window (a real test showed 13.34→13.15 V
                # across 5 cycles' relax-ends), so any single sample carries both
                # noise and residual-relaxation bias into the fit's R0 — the median
                # of the tail is the same robustness idea analyze_series' own
                # _load_metrics uses for its OCV anchor.
                # Seed with the trailing rest samples (negative relative time, i=0)
                # so identify_ecm_fit() can actually locate the rest->pulse edge —
                # the pulse loop's own samples are ALL at i_pulse, with no edge in
                # them by themselves.
                _fit_t0 = _t.perf_counter()
                _rest_n = len(_relax_tail_v)
                # Must match the relax leg's own pacing period above (DEFAULT_SAMPLE_HZ) —
                # this is the same time axis identify_ecm_fit() reads.
                _fit_t = [-(_rest_n - k) / DEFAULT_SAMPLE_HZ for k in range(_rest_n)]
                _fit_i = [0.0] * _rest_n
                _fit_v = list(_relax_tail_v)
                voc_for_fit = (sorted(_relax_tail_v)[_rest_n // 2]
                               if _rest_n else v_r)
                t_phase = _t.time() + pulse_s
                # Per-substep timing breakdown for the achieved-rate alarm below —
                # "sampling only 0.7 Hz" alone doesn't say WHERE the ~1.4s/iteration
                # went (SCPI round-trip vs CSV/cloud log vs Qt display paint), so a
                # slow rig couldn't be diagnosed without adding print statements by
                # hand. Wall-clock, not CPU time — this is exactly the real latency
                # budget the pacing loop below is fighting.
                _t_scpi = _t_log = _t_display = 0.0
                while self._seq_running.is_set() and _t.time() < t_phase:
                    _iter_t0 = _t.perf_counter()
                    try:
                        _s0 = _t.perf_counter()
                        v_p, i_p = self.hw.read_measurements(prefer_load_v=True)
                        temp_h = self.hw.current_temp
                        _t_scpi += _t.perf_counter() - _s0
                        # discharge-positive convention (matches AUTO/QUICK SCAN) — do
                        # NOT negate i_p here, or the CSV's current sign is inverted and
                        # the 1-RC ECM fit never converges on this sequence's own data.
                        # Stale-temp escalation (G8) — IEC/Quick Scan discharge loops
                        # have had this since the industrial-grade audit; checked BEFORE
                        # feeding the estimator/CSV (same check-then-feed order as those
                        # loops) so a stale ESP32 reading never contaminates the
                        # Arrhenius-compensated fit or gets written to disk before the
                        # sequence aborts.
                        if not self._seq_check_temp_stale():
                            hppc_safety_tripped = True
                            break
                        if not self._seq_check_otp(temp_h):
                            hppc_safety_tripped = True
                            break
                        # estimator.update() per-sample — see the relax leg's comment
                        # above: without this the pulse's own Ah removal was never
                        # coulomb-counted (SoC stayed frozen at 100% through all 5
                        # pulses of a real test), and the pulse edge itself is exactly
                        # what the universal R0 step detector needs to see.
                        _upd_now = _t.perf_counter()
                        state_p = self.controller.estimator.update(
                            v_p, i_p, dt=max(1e-3, _upd_now - _upd_last),
                            temp=temp_h)
                        _upd_last = _upd_now
                        _s1 = _t.perf_counter()
                        self.controller._log_sample(v_p, i_p)
                        _t_log += _t.perf_counter() - _s1
                        _fit_t.append(_t.perf_counter() - _fit_t0)
                        _fit_i.append(i_p)
                        _fit_v.append(v_p)
                        _s2 = _t.perf_counter()
                        self.update_display(v_p, i_p, state_p["soc"], state_p["rin"])
                        _t_display += _t.perf_counter() - _s2
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_p <= hppc_load_floor:
                            self._seq_running.clear()
                            hppc_safety_tripped = True
                            reason = (f"Under-voltage during HPPC pulse: "
                                      f"{v_p:.3f}V ≤ {hppc_load_floor:.3f}V hardware floor")
                            self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                            self.sig_wf_status.emit(f"⛔ {reason}")
                            if self.controller:
                                self.controller._trigger_safety(reason)
                            break
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                    # Same DEFAULT_SAMPLE_HZ pacing as the relax leg above — see its
                    # comment. Extra important here: R0's t=0 extrapolation depends on
                    # having enough points densely sampled right at the pulse edge,
                    # which 1 Hz could barely resolve at all.
                    _elapsed_iter = _t.perf_counter() - _iter_t0
                    if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                        break
                self.hw.load_off()
                # Same low-latency edge sample as the pulse-start above, for the
                # pulse-end transition — otherwise this edge suffers the identical
                # staleness gap and identify_dcir() sees no valid steps at all.
                try:
                    v_r0, i_r0 = self.hw.read_measurements(prefer_load_v=False)
                    self.controller._log_sample(v_r0, i_r0)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                # Achieved-sample-rate instrumentation: on the real rig a pulse leg
                # designed for 5 Hz (0.2s pacing) measured only ~0.7 Hz — each
                # iteration's real cost (SCPI round-trip + display + log + cloud) was
                # ~1.4s, starving the ECM fit of points (22 instead of ~150 per 30s
                # pulse) with nothing anywhere reporting it. Log it every pulse and
                # alarm once per sequence when badly under target, so a slow rig is
                # visible instead of silently degrading every fit.
                _n_pulse_samples = len(_fit_t) - _rest_n
                _pulse_span = (_fit_t[-1] - max(0.0, _fit_t[_rest_n])) if _n_pulse_samples > 1 else 0.0
                if _pulse_span > 1.0:
                    _hz = _n_pulse_samples / _pulse_span
                    # Breakdown as % of the accounted-for time — the gap between
                    # (scpi+log+display) and the real per-iteration wall-clock is
                    # unaccounted overhead (Python/Qt event-loop scheduling, GIL
                    # contention with other threads, etc.), reported too so "the
                    # three measured substeps only added up to 60%" is visible
                    # instead of silently attributed to whichever substep is listed.
                    _t_accounted = _t_scpi + _t_log + _t_display
                    _t_total = max(_t_accounted, _pulse_span)
                    _t_other = max(0.0, _t_total - _t_accounted)
                    logger.info(
                        "HPPC pulse %d/%d sampled at %.1f Hz (%d samples / %.1fs) — "
                        "breakdown: SCPI %.0f%%  log %.0f%%  display %.0f%%  other %.0f%%",
                        cyc, n_cyc, _hz, _n_pulse_samples, _pulse_span,
                        100 * _t_scpi / _t_total, 100 * _t_log / _t_total,
                        100 * _t_display / _t_total, 100 * _t_other / _t_total)
                    if _hz < 2.0 and not _rate_warned:
                        _rate_warned = True
                        self.sig_alarm.emit(
                            f"[HPPC SEQ] Sampling only {_hz:.1f} Hz during pulses "
                            f"(target ~{DEFAULT_SAMPLE_HZ:.0f} Hz) — breakdown: SCPI {100*_t_scpi/_t_total:.0f}% "
                            f"log {100*_t_log/_t_total:.0f}% display {100*_t_display/_t_total:.0f}% "
                            f"other {100*_t_other/_t_total:.0f}% — R1/C1 fit quality degraded")
                # Feed this cycle's own real R0/R1/C1 into the live estimator — HPPC's
                # pulse leg used to never feed the estimator at all, so without this
                # the live SoC/Rin display never benefited from a real per-unit
                # calibration despite 5 real pulses' worth of fittable data being
                # collected every run. Reuses the exact same fit + harness-correction
                # the post-hoc analysis (analyze_csv) already does, just applied live
                # per cycle instead of once at the very end.
                if voc_for_fit is not None and len(_fit_t) >= 10:
                    try:
                        from aset_batt.acquisition.analysis import (
                            identify_ecm_fit, _correct_for_harness_r)
                        ecm, _reason = identify_ecm_fit(_fit_t, _fit_i, _fit_v, voc_for_fit)
                        if ecm is not None:
                            # This cycle's own fitted τ drives the NEXT cycle's
                            # adaptive relax extension (see relax_eff above). τ is
                            # temp-invariant here (R1·C1 — the normalization below
                            # divides R1 and multiplies C1 by the same factor).
                            _tau_new = float(ecm.get("tau1_s", ecm.get("tau_s", 0.0)))
                            if _tau_new > 0.0:
                                _tau_fit = _tau_new
                            r0 = float(ecm["R0_ohm"])
                            harness_r = max(0.0, float(getattr(
                                self.controller.config.battery, "harness_resistance_ohm", 0.0)))
                            if harness_r > 0.0:
                                r0, _warn = _correct_for_harness_r(r0, harness_r, "live ECM R0", [])
                                if _warn:
                                    self.sig_alarm.emit(f"[HPPC SEQ] {_warn[0]}")
                            # Normalize to the estimator's 25 °C contract:
                            # StateEstimator's live rin is (R0+R1)×temp_rin_multiplier,
                            # i.e. it treats stored values as 25 °C-basis (the step
                            # detector and analyze_series both divide by the
                            # multiplier already). Feeding raw at-bench-temp values
                            # here made the displayed rin come out UNDER-stated by
                            # the multiplier (~12% at this bench's ~30 °C). C1 scales
                            # inversely so the fitted τ = R1·C1 is preserved.
                            r1 = float(ecm["R1_ohm"])
                            c1 = float(ecm["C1_farad"])
                            # If normalization itself fails, do NOT fall back to
                            # feeding raw/un-normalized values — that would silently
                            # reintroduce the exact ~12% under-statement bug this
                            # block exists to fix, with zero diagnostic anywhere.
                            # Skip this cycle's live feed instead and log why.
                            try:
                                _mult = self.controller.estimator.battery_model \
                                    .temp_rin_multiplier(self.hw.current_temp)
                            except Exception as _exc:
                                logger.debug(
                                    "Live ECM temp-normalization failed (%s) — "
                                    "skipping this cycle's update_ecm() feed", _exc)
                                _mult = None
                            if _mult is not None and _mult > 1e-6:
                                r0, r1, c1 = r0 / _mult, r1 / _mult, c1 * _mult
                                self.controller.estimator.update_ecm(r0, r1, c1)
                    except Exception as exc:
                        logger.debug("Live per-cycle ECM fit failed (non-fatal): %s", exc)
                if not self._seq_running.is_set():
                    break

                # ── Regen (charge) pulse leg (G6) ────────────────────────────
                # Deliberately AFTER the discharge pulse's own live ECM fit above
                # so the discharge fit/adaptive-τ logic is completely untouched
                # regardless of whether regen is enabled. Do NOT feed the regen
                # pulse into update_ecm()/_tau_fit: FreedomCAR expects asymmetric
                # charge/discharge R0, and blending would silently average two
                # different physical resistances into one number with no way to
                # tell which produced it. Regen's own R0/R1/C1 is still
                # analyzable post-hoc via identify_hppc_pulses() (analysis.py).
                if regen_enabled and self._seq_running.is_set():
                    soc_now = self.controller.estimator.soc
                    if not regen_allowed(soc_now, self.controller.config.battery
                                         .hppc_regen_soc_ceiling_pct):
                        logger.info(
                            "HPPC %d/%d: regen pulse skipped — SoC %.1f%% >= "
                            "ceiling %.1f%%", cyc, n_cyc, soc_now,
                            self.controller.config.battery.hppc_regen_soc_ceiling_pct)
                    else:
                        # Regen-rest leg — own buffer, NOT _relax_tail_v: the
                        # NEXT cycle's discharge relax leg must not seed its
                        # fit from regen-rest samples.
                        status(f"HPPC {cyc}/{n_cyc}{_level_label}: REGEN REST {relax_s:.0f}s...")
                        _regen_tail_v = []
                        _t_regen_relax0 = _t.time()
                        while (self._seq_running.is_set()
                              and _t.time() < _t_regen_relax0 + relax_s):
                            _iter_t0 = _t.perf_counter()
                            try:
                                v_rg, _, _ = self.hw.read_vi()
                                temp_rg = self.hw.current_temp
                                if not self._seq_check_temp_stale():
                                    hppc_safety_tripped = True
                                    break
                                if not self._seq_check_otp(temp_rg):
                                    hppc_safety_tripped = True
                                    break
                                _upd_now = _t.perf_counter()
                                state_rg = self.controller.estimator.update(
                                    v_rg, 0.0, dt=max(1e-3, _upd_now - _upd_last),
                                    temp=temp_rg)
                                _upd_last = _upd_now
                                self.controller._log_sample(v_rg, 0.0)
                                _regen_tail_v.append(v_rg)
                                if len(_regen_tail_v) > 5:
                                    _regen_tail_v.pop(0)
                                self.update_display(v_rg, 0.0, state_rg["soc"], state_rg["rin"])
                                self._seq_kick_watchdog()
                                elapsed_h = int(_t.time() - _hppc_t0)
                                self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                                if v_rg <= pack_min:
                                    self._seq_running.clear()
                                    hppc_safety_tripped = True
                                    reason = (f"Under-voltage during HPPC regen rest: "
                                              f"{v_rg:.3f}V ≤ {pack_min:.3f}V cutoff")
                                    self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                                    self.sig_wf_status.emit(f"⛔ {reason}")
                                    break
                            except Exception as e:
                                import logging
                                logging.getLogger(__name__).error(
                                    'Ignored exception: %s', e, exc_info=True)
                            _elapsed_iter = _t.perf_counter() - _iter_t0
                            if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                                break

                        if self._seq_running.is_set():
                            # Regen-pulse leg — CV+CC-limit PSU write, not the
                            # e-load. current comes back negative on its own from
                            # read_measurements(prefer_load_v=False) (charge = PSU
                            # active) — do not re-negate.
                            self.hw.set_psu(True, str(hppc_regen_ceiling), str(i_regen))
                            try:
                                v_rp0, i_rp0 = self.hw.read_measurements(prefer_load_v=False)
                                self.controller._log_sample(v_rp0, i_rp0)
                            except Exception as e:
                                import logging
                                logging.getLogger(__name__).error(
                                    'Ignored exception: %s', e, exc_info=True)
                            status(f"HPPC {cyc}/{n_cyc}{_level_label}: REGEN PULSE "
                                  f"{pulse_s:.0f}s  -{i_regen:.3f} A")
                            _t_regen_pulse0 = _t.time()
                            while (self._seq_running.is_set()
                                  and _t.time() < _t_regen_pulse0 + pulse_s):
                                _iter_t0 = _t.perf_counter()
                                try:
                                    v_rp, i_rp = self.hw.read_measurements(prefer_load_v=False)
                                    temp_rp = self.hw.current_temp
                                    if not self._seq_check_temp_stale():
                                        hppc_safety_tripped = True
                                        break
                                    if not self._seq_check_otp(temp_rp):
                                        hppc_safety_tripped = True
                                        break
                                    _upd_now = _t.perf_counter()
                                    state_rp = self.controller.estimator.update(
                                        v_rp, i_rp, dt=max(1e-3, _upd_now - _upd_last),
                                        temp=temp_rp)
                                    _upd_last = _upd_now
                                    self.controller._log_sample(v_rp, i_rp)
                                    self.update_display(v_rp, i_rp, state_rp["soc"], state_rp["rin"])
                                    self._seq_kick_watchdog()
                                    elapsed_h = int(_t.time() - _hppc_t0)
                                    self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                                    if v_rp >= hppc_regen_ceiling:
                                        self._seq_running.clear()
                                        hppc_safety_tripped = True
                                        reason = (f"Over-voltage during HPPC regen pulse: "
                                                  f"{v_rp:.3f}V ≥ {hppc_regen_ceiling:.3f}V "
                                                  f"ceiling")
                                        self.sig_alarm.emit(f"[SAFETY] {reason} — sequence aborted")
                                        self.sig_wf_status.emit(f"⛔ {reason}")
                                        break
                                except Exception as e:
                                    import logging
                                    logging.getLogger(__name__).error(
                                        'Ignored exception: %s', e, exc_info=True)
                                _elapsed_iter = _t.perf_counter() - _iter_t0
                                if not self._seq_sleep(max(0.0, 1.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter)):
                                    break
                            self.hw.psu_off()
                            try:
                                v_rp_end, i_rp_end = self.hw.read_measurements(prefer_load_v=False)
                                self.controller._log_sample(v_rp_end, i_rp_end)
                            except Exception as e:
                                import logging
                                logging.getLogger(__name__).error(
                                    'Ignored exception: %s', e, exc_info=True)

                if not self._seq_running.is_set():
                    break
                if not soc_sweep_enabled:
                    break   # preserves the exact single-level behavior: run once
                if soc_sweep_done(self.controller.estimator.soc, soc_floor_pct):
                    self.sig_alarm.emit(
                        f"[HPPC SEQ] SoC-sweep reached floor after level {level} "
                        f"— stopping")
                    break
            self.sig_phase_progress.emit(0, 0)
            # A plain user Cancel should stop with nothing further — but a safety trip
            # (UVP/OTP) mid-cycle still leaves real pulse data logged in the CSV, and
            # that's exactly the data a degraded/"bad" battery test needs: tripping the
            # floor sooner is expected for high-Rin packs, and discarding the analysis
            # here would mean the worse the battery, the less you learn about it.
            if not self._seq_running.is_set() and not hppc_safety_tripped:
                return
            try:
                from aset_batt.storage.cloud_push import set_cloud_meta
                set_cloud_meta(sub_phase="")  # done with pulse/relax cycling
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.sig_hppc_seq_wf.emit(3, "done")
            if hppc_safety_tripped:
                self.sig_alarm.emit(
                    f"[HPPC SEQ] Safety trip mid-cycle ({cyc}/{n_cyc}) — analyzing partial data")
            else:
                self.sig_alarm.emit(f"[HPPC SEQ] {n_cyc} HPPC cycles complete")

            # ── PHASE 4: ANALYZE (ECM fit) ────────────────────────────────
            # Run even on a safety trip: a high-Rin/degraded pack is exactly the case
            # that trips UVP/OTP soonest, so skipping analysis here would mean the worse
            # the battery, the less data you get back — analyze whatever pulses were
            # logged before the trip instead of discarding them.
            self.sig_hppc_seq_wf.emit(4, "active")
            status("HPPC SEQ ANALYZE: ECM fit R0/R1/C1/τ...")
            res = self.controller._auto_analyze(force_hppc=True)
            self.sig_hppc_seq_wf.emit(4, "done")
            if res:
                self.sig_seq_result.emit(format_seq_result(res))
            grade_str = res.get("grade", "?") if res else "?"
            ecm_str = res.get("ecm_model", "1RC") if res else "1RC"
            if hppc_safety_tripped:
                status("HPPC SEQUENCE หยุดกลางคัน (Safety) — วิเคราะห์จากข้อมูลบางส่วน ดูผลที่แท็บ Analytics")
                self.sig_alarm.emit("[HPPC SEQ] Partial (safety trip) — see Analytics")
                self.sig_seq_done.emit(
                    "HPPC Sequence Partial (Safety Trip)",
                    f"Stopped after {cyc}/{n_cyc} cycles\n"
                    f"Grade: {grade_str}  ({ecm_str} ECM)\nดูผลที่แท็บ Analytics")
            else:
                status("HPPC SEQUENCE เสร็จ — ดูผลที่แท็บ Analytics")
                self.sig_alarm.emit("[HPPC SEQ] Complete ✓")
                self.sig_seq_done.emit("HPPC Sequence Complete",
                                       f"Grade: {grade_str}  ({ecm_str} ECM)\nดูผลที่แท็บ Analytics")
            completed_ok = True

        except Exception as exc:
            self.sig_alarm.emit(f"[HPPC SEQ] Error: {exc}")
            status(f"HPPC SEQ Error: {exc}")
        finally:
            self._seq_hw_safe_off()
            self._seq_running.clear()
            if self.controller:
                self.controller.end_session()
            self.sig_phase_progress.emit(0, 0)
            if not completed_ok:
                self.sig_seq_aborted.emit()
            self.sig_loading.emit("btn_hppc_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- Cycle Life test thread -------------------------------------------


