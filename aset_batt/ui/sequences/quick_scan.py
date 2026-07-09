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
            # See the comment in _auto_sequence_thread — log from PREPARE so the CSV
            # actually contains a genuine rest window (otherwise the file only starts
            # once start_charge()/start_monitor() implicitly opens one, and
            # _quality_flags always flags "no clear rest before load").
            self.controller._ensure_logging(label="QuickScan")
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
                try:
                    v_r, _, _ = self.hw.read_vi()
                    self.controller._log_sample(v_r, 0.0)
                    self.update_display(v_r, 0.0, self.controller.estimator.soc,
                                        self.controller.estimator.rin)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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
            self.controller._ensure_logging(label="QuickScan")
            self.hw.set_load(True, i_dis)
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last_log = _t.perf_counter()
            _dis_t0 = _t.perf_counter()
            _dis_est = int(rated / max(i_dis, 0.01) * 3600)
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

