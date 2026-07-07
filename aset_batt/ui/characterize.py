"""
CHARACTERIZE tab: zone builder plus the Peukert / ETA / GITT handlers,
background threads, and parameter save/refresh helpers.
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
    _btn, _hline, QtRootShim, TemperatureGauge,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html

logger = logging.getLogger(__name__)


class _FalseEvent:
    """Sentinel event that is never set — used as a default guard in characterize handlers."""
    def is_set(self):
        return False


class CharacterizeMixin:
    # ---- ZONE: TEST MODE — CHARACTERIZE tab (parameter identification) ------
    def _zone_characterize(self):
        """Three independent parameter-ID experiments: Peukert k, Coulomb η, OCV–SoC."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        lay.addWidget(self._subheader("CHARACTERIZE — Parameter Identification"))

        note = QLabel(
            "ทดสอบแต่ละรายการแยกอิสระ · ผลจะเก็บในหน่วยความจำจนกว่ากด SAVE TO PROFILE\n"
            "แต่ละการทดสอบต้องใช้เวลาหลายชั่วโมง — เชื่อมต่อฮาร์ดแวร์ก่อนเริ่ม"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        lay.addWidget(note)

        # ── Card 1 · Peukert k ────────────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("① Peukert  k  — multi-rate discharge"))

        self.lbl_char_pk = QLabel(
            "4 discharge runs (0.1C · 0.2C · 0.5C · 1C) → log-log fit → k\n"
            "ใช้เวลา: ~8–12 ชั่วโมง (ชาร์จ + discharge × 4)")
        self.lbl_char_pk.setWordWrap(True)
        self.lbl_char_pk.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_pk)

        self.lbl_char_pk_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_pk_status.setStyleSheet(f"color:{MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_pk_status)

        row_pk = QHBoxLayout()
        self.btn_char_pk_start  = _btn("START Peukert", bg=OK, fg="white", hover="#266a2a")
        self.btn_char_pk_cancel = _btn("CANCEL", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_char_pk_cancel.setEnabled(False)
        self.btn_char_pk_start.clicked.connect(self._on_char_pk_start)
        self.btn_char_pk_cancel.clicked.connect(self._on_char_pk_cancel)
        row_pk.addWidget(self.btn_char_pk_start)
        row_pk.addWidget(self.btn_char_pk_cancel)
        lay.addLayout(row_pk)

        # ── Card 2 · Coulomb η ────────────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("② Coulomb  η  — charge/discharge cycle"))

        self.lbl_char_eta = QLabel(
            "Discharge → full charge (count Ah_in/band) → discharge 0.1C (count Ah_out)\n"
            "ใช้เวลา: ~6–8 ชั่วโมง (ชาร์จ + discharge 0.1C)")
        self.lbl_char_eta.setWordWrap(True)
        self.lbl_char_eta.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_eta)

        self.lbl_char_eta_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_eta_status.setStyleSheet(f"color:{MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_eta_status)

        row_eta = QHBoxLayout()
        self.btn_char_eta_start  = _btn("START η", bg=OK, fg="white", hover="#266a2a")
        self.btn_char_eta_cancel = _btn("CANCEL", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_char_eta_cancel.setEnabled(False)
        self.btn_char_eta_start.clicked.connect(self._on_char_eta_start)
        self.btn_char_eta_cancel.clicked.connect(self._on_char_eta_cancel)
        row_eta.addWidget(self.btn_char_eta_start)
        row_eta.addWidget(self.btn_char_eta_cancel)
        lay.addLayout(row_eta)

        # ── Card 3 · OCV–SoC GITT ────────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("③ OCV–SoC Table  (GITT, ~22h)"))

        self.lbl_char_gitt = QLabel(
            "Discharge 5% SoC × 20 → rest จน ΔV/Δt < 2 mV/60s → V_rest = OCV\n"
            "ใช้เวลา: ~22 ชั่วโมง (discharge 36 min + rest ≥30 min × 20 จุด)")
        self.lbl_char_gitt.setWordWrap(True)
        self.lbl_char_gitt.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_gitt)

        self.lbl_char_gitt_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_gitt_status.setStyleSheet(f"color:{MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_gitt_status)

        self.pgb_char_gitt = QProgressBar()
        self.pgb_char_gitt.setRange(0, 20)
        self.pgb_char_gitt.setValue(0)
        self.pgb_char_gitt.setFormat("0 / 20 จุด")
        self.pgb_char_gitt.setTextVisible(True)
        lay.addWidget(self.pgb_char_gitt)

        row_gitt = QHBoxLayout()
        self.btn_char_gitt_start  = _btn("START GITT", bg=OK, fg="white", hover="#266a2a")
        self.btn_char_gitt_cancel = _btn("CANCEL", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_char_gitt_cancel.setEnabled(False)
        self.btn_char_gitt_start.clicked.connect(self._on_char_gitt_start)
        self.btn_char_gitt_cancel.clicked.connect(self._on_char_gitt_cancel)
        row_gitt.addWidget(self.btn_char_gitt_start)
        row_gitt.addWidget(self.btn_char_gitt_cancel)
        lay.addLayout(row_gitt)

        # ── Profile Parameters panel ──────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("PROFILE PARAMETERS (current + measured)"))

        self.txt_char_params = QTextEdit()
        self.txt_char_params.setReadOnly(True)
        self.txt_char_params.setFont(QFont("Segoe UI", 10))
        self.txt_char_params.setFixedHeight(130)
        lay.addWidget(self.txt_char_params)

        self.btn_char_save = _btn("SAVE TO PROFILE", bg=INFO, fg="white", hover="#0d4a89")
        self.btn_char_save.setEnabled(False)
        self.btn_char_save.setToolTip(
            "เขียนค่าที่วัดได้ลง battery_profiles.json ของ profile ที่เลือกอยู่")
        self.btn_char_save.clicked.connect(self._on_char_save)
        lay.addWidget(self.btn_char_save)

        lay.addStretch(1)
        return w


    # =========================================================================
    # CHARACTERIZE tab — handlers, threads, helpers
    # =========================================================================

    # ── shared helpers ────────────────────────────────────────────────────────

    def _char_sleep(self, ev, seconds: float) -> bool:
        """Interruptible sleep for characterize threads.  Returns True if time elapsed,
        False if the event was cleared (cancelled)."""
        import time
        t_end = time.time() + seconds
        while ev.is_set():
            left = t_end - time.time()
            if left <= 0:
                return True
            time.sleep(min(0.5, left))
        return False

    def _char_any_running(self) -> bool:
        return any(e.is_set() for e in self._char_running.values())

    def _busy_reason(self, include_char: bool = True) -> Optional[str]:
        """Return a description of whatever is currently running on the shared
        controller/estimator/hardware, or None if free to start something new.

        There are THREE independent entry points that each spawn a background
        thread calling the SAME ``self.estimator.update()`` and driving the SAME
        ``self.hw`` instruments: the characterization worker (RUN TEST), the
        ISA-101 sequence threads (AUTO/Quick Scan/HPPC Seq/Cycle Life), and the
        CHARACTERIZE-tab tests (Peukert/η/GITT). Before this check existed, e.g.
        clicking RUN TEST while a Cycle Life sequence was mid-run would start a
        second thread against the same estimator — double-counting coulombs on
        every sample AND issuing conflicting SCPI commands (e.g. one thread
        commanding a charge while another commands a discharge) to the same PSU/
        load. Every entry point below must check this before starting anything.
        """
        if self._test_thread is not None:
            return "การทดสอบ Characterization (RUN TEST) กำลังทำงานอยู่"
        if self._seq_running.is_set():
            return "ลำดับทดสอบ (Sequence: AUTO/Quick Scan/HPPC/Cycle Life) กำลังทำงานอยู่"
        if include_char and self._char_any_running():
            return "การทดสอบในแท็บ CHARACTERIZE กำลังทำงานอยู่"
        return None

    def _char_guard(self) -> bool:
        """Return True if OK to start a new test.  Shows a warning if not."""
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "Connect hardware first.")
            return False
        # CHARACTERIZE-tab tests only need to check the worker/sequence (not each
        # other) — per-test mutual exclusion among "pk"/"eta"/"gitt" is already
        # handled individually via self._char_running at each test's own start.
        busy = self._busy_reason(include_char=False)
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return False
        # Same dual-estimator-feed guard as _seq_common_start (sequences.py): the
        # background monitor loop (Start Monitor) also calls estimator.update() at
        # ~10 Hz. _busy_reason above only stops a DIFFERENT test type from starting
        # while one is already running — it never stopped the monitor loop, so a
        # Peukert/η/GITT test could still double-feed the estimator with it if the
        # operator left "Start Monitor" running.
        if self.controller and self.controller.monitor_running:
            self.controller.stop_monitor()
        return True

    def _char_hw_stop(self):
        """Best-effort hardware stop called from cancel handlers."""
        try:
            if self.controller:
                self.controller.stop_charge()
        except Exception:
            pass
        try:
            self.hw.load_off()
            self.hw.psu_off()
        except Exception:
            pass

    # ── Peukert k ─────────────────────────────────────────────────────────────

    def _on_char_pk_start(self):
        if not self._char_guard():
            return
        if self._char_running.get("pk", _FalseEvent()).is_set():
            return
        ev = threading.Event()
        ev.set()
        self._char_running["pk"] = ev
        self.btn_char_pk_start.setEnabled(False)
        self.btn_char_pk_cancel.setEnabled(True)
        self.sig_char_update.emit("pk", "● กำลังทดสอบ Peukert k...")
        import threading as _th
        _th.Thread(target=self._char_peukert_thread, daemon=True).start()

    def _on_char_pk_cancel(self):
        if "pk" in self._char_running:
            self._char_running["pk"].clear()
        self._char_hw_stop()

    def _char_peukert_thread(self):
        """Background: discharge at 4 C-rates, fit Peukert k."""
        import time
        ev = self._char_running["pk"]

        def status(msg):
            # sig_char_update alone drives this test's own status label (see
            # _slot_char_update) — status() fires every ~5s for hours, so it must
            # NOT also go to sig_alarm (unlike sequences.py's lighter sig_wf_status),
            # or the alarm table grows by thousands of rows over one test and gets
            # progressively slower to touch. Milestones get their own explicit
            # sig_alarm.emit() calls below instead.
            self.sig_char_update.emit("pk", msg)

        try:
            self.controller._ensure_logging(label="Peukert")
            rated    = self.controller.config.battery.rated_capacity
            pack_min = self.controller.config.battery.pack_min_voltage
            c_rates  = [0.1, 0.2, 0.5, 1.0]

            currents: list = []
            durations: list = []

            for idx, c in enumerate(c_rates):
                if not ev.is_set():
                    return

                i_test = round(c * rated, 3)
                status(f"({idx+1}/4) ชาร์จก่อน discharge {c:g}C ({i_test:.3f} A)...")
                self.sig_alarm.emit(f"[CHAR/Peukert] ({idx+1}/4) เริ่มชาร์จก่อน discharge {c:g}C")

                # ── charge to full ─────────────────────────────────────────
                self.controller.start_charge(strategy=None)
                while ev.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    if not self._char_sleep(ev, 30.0):
                        return

                if not ev.is_set():
                    return
                # start_charge() restarts the shared monitor loop (it was stopped
                # once by _char_guard() before this whole test began) — stop it
                # again now, or it keeps calling estimator.update() concurrently
                # with this test's own discharge loop below and double-counts
                # every sample (same guard as sequences.py's _auto_sequence_thread).
                if self.controller.monitor_running:
                    self.controller.stop_monitor()

                # ── rest 5 min ─────────────────────────────────────────────
                status(f"({idx+1}/4) พักหลังชาร์จ 5 นาที...")
                if not self._char_sleep(ev, 300):
                    return

                # ── discharge at i_test until UVP ──────────────────────────
                status(f"({idx+1}/4) discharge {i_test:.3f} A ({c:g}C)...")
                self.sig_alarm.emit(f"[CHAR/Peukert] ({idx+1}/4) เริ่ม discharge {i_test:.3f} A ({c:g}C)")
                # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
                t0 = time.perf_counter()
                self.hw.set_load(True, i_test)
                last_log = t0

                while ev.is_set():
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        now  = time.perf_counter()   # stamp AT the measurement
                        temp = self.hw.current_temp
                        self._seq_check_temp_stale()
                        dt   = now - last_log
                        last_log = now
                        self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                        # monitor loop is stopped for this test (see above) — feed
                        # the live graph + CSV/cloud directly, same as sequences.py.
                        self.controller._log_sample(v, i_meas)
                        self.update_display(v, i_meas, self.controller.estimator.soc,
                                            self.controller.estimator.rin, temp)
                        elapsed = int(now - t0)
                        status(f"({idx+1}/4) {c:g}C — {v:.3f} V  {i_meas:.3f} A  "
                               f"elapsed {elapsed//60}m{elapsed%60:02d}s")
                        if v <= pack_min:
                            break
                    except Exception as exc:
                        self.sig_alarm.emit(f"[CHAR/Peukert] read error: {exc}")
                        break
                    if not self._char_sleep(ev, 5.0):
                        break

                self.hw.set_load(False)
                if not ev.is_set():
                    return

                elapsed_s = time.time() - t0
                currents.append(i_test)
                durations.append(elapsed_s)
                status(f"({idx+1}/4) {c:g}C → {elapsed_s:.0f} s ✓")
                self.sig_alarm.emit(f"[CHAR/Peukert] ({idx+1}/4) {c:g}C discharge เสร็จ → {elapsed_s:.0f}s")

                # brief rest between rates
                if idx < len(c_rates) - 1:
                    if not self._char_sleep(ev, 60):
                        return

            # ── fit k ──────────────────────────────────────────────────────
            if len(currents) >= 2:
                from aset_batt.core.characterization import fit_peukert_k
                k, r2 = fit_peukert_k(currents, durations)
                self._char_results["pk"] = {
                    "peukert_k": k, "peukert_k_r2": r2,
                    "peukert_hr": self.controller.config.battery.rated_capacity,
                    "data": list(zip(currents, durations)),
                }
                status(f"✓ k = {k:.3f}  R² = {r2:.4f}")
                self.sig_alarm.emit(f"[CHAR/Peukert] เสร็จสิ้น: k={k:.3f}  R²={r2:.4f}")
            else:
                status("⚠ ได้ข้อมูลไม่พอ fit — ต้องการ ≥ 2 discharge runs")
                self.sig_alarm.emit("[CHAR/Peukert] ⚠ ข้อมูลไม่พอ fit")

        except Exception as exc:
            self.sig_char_update.emit("pk", f"✗ Error: {exc}")
            logger.exception("Peukert thread error")
        finally:
            ev.clear()
            self.sig_char_update.emit("pk", "__DONE__")

    # ── Coulomb η ─────────────────────────────────────────────────────────────

    def _on_char_eta_start(self):
        if not self._char_guard():
            return
        if self._char_running.get("eta", _FalseEvent()).is_set():
            return
        ev = threading.Event()
        ev.set()
        self._char_running["eta"] = ev
        self.btn_char_eta_start.setEnabled(False)
        self.btn_char_eta_cancel.setEnabled(True)
        self.sig_char_update.emit("eta", "● กำลังทดสอบ Coulomb η...")
        import threading as _th
        _th.Thread(target=self._char_eta_thread, daemon=True).start()

    def _on_char_eta_cancel(self):
        if "eta" in self._char_running:
            self._char_running["eta"].clear()
        self._char_hw_stop()

    def _char_eta_thread(self):
        """Background: full charge/discharge cycle → per-band coulomb efficiency."""
        import time
        ev = self._char_running["eta"]

        def status(msg):
            # see the same comment in _char_peukert_thread — no sig_alarm here,
            # this fires every ~5s for hours (both the charge- and discharge-
            # tracking loops). Milestones get their own explicit emit() below.
            self.sig_char_update.emit("eta", msg)

        # SoC band boundaries (%) — must match _coulomb_eta in state_estimator
        BULK_MAX = 75.0
        ABS_MAX  = 90.0

        def _band(soc):
            if soc < BULK_MAX:
                return "bulk"
            if soc < ABS_MAX:
                return "absorb"
            return "full"

        try:
            self.controller._ensure_logging(label="CoulombEta")
            rated    = self.controller.config.battery.rated_capacity
            pack_min = self.controller.config.battery.pack_min_voltage

            # ── Phase 1: Charge to full; track Ah_in per SoC band ─────────
            status("Phase 1/2: ชาร์จ (นับ Ah_in ต่อ band)...")
            self.sig_alarm.emit("[CHAR/η] เริ่ม Phase 1/2: ชาร์จ")
            ah_in  = {"bulk": 0.0, "absorb": 0.0, "full": 0.0}
            self.controller.start_charge(strategy=None)
            # This loop tracks Ah_in per SoC band itself (needs its own fine-grained
            # estimator.update() calls), which would double-count against the shared
            # monitor loop that start_charge() just restarted — stop it immediately
            # (same guard as _char_peukert_thread, just earlier since this loop reads
            # hardware through the CHARGE phase too, not only after).
            if self.controller.monitor_running:
                self.controller.stop_monitor()
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last = time.perf_counter()

            while ev.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                try:
                    v, i_ch = self.hw.read_measurements(prefer_load_v=False)
                    now = time.perf_counter()   # stamp AT the measurement
                    temp = self.hw.current_temp
                    self._seq_check_temp_stale()
                    dt  = now - last
                    last = now
                    state = self.controller.estimator.update(v, i_ch, dt=dt, temp=temp)
                    soc_now = state["soc"]
                    # i_ch is negative during charging; accumulate absolute Ah
                    dah = abs(i_ch) * dt / 3600.0
                    ah_in[_band(soc_now)] += dah
                    self.controller._log_sample(v, i_ch)
                    self.update_display(v, i_ch, soc_now, state["rin"], temp, state.get("soh"))
                    status(f"Charge: {v:.3f} V  SoC {soc_now:.0f}%  "
                           f"Ah_in={sum(ah_in.values()):.3f}")
                except Exception as exc:
                    self.sig_alarm.emit(f"[CHAR/η] charge read error: {exc}")
                    break
                if not self._char_sleep(ev, 5.0):
                    break

            if not ev.is_set():
                return

            # ── rest 30 min ───────────────────────────────────────────────
            status("Phase 1/2 done. พักหลังชาร์จ 30 นาที...")
            self.sig_alarm.emit(f"[CHAR/η] Phase 1/2 เสร็จ — Ah_in={sum(ah_in.values()):.3f}")
            if not self._char_sleep(ev, 1800):
                return

            # OCV anchor
            soc_now = self.controller.calibrate_from_ocv()

            # ── Phase 2: Discharge at 0.1C; track Ah_out per SoC band ─────
            i_dis = round(0.1 * rated, 3)
            status(f"Phase 2/2: discharge {i_dis:.3f} A (0.1C, นับ Ah_out)...")
            self.sig_alarm.emit(f"[CHAR/η] เริ่ม Phase 2/2: discharge {i_dis:.3f} A")
            ah_out = {"bulk": 0.0, "absorb": 0.0, "full": 0.0}
            self.hw.set_load(True, i_dis)
            # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
            last = time.perf_counter()

            while ev.is_set():
                try:
                    v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                    now  = time.perf_counter()   # stamp AT the measurement
                    temp = self.hw.current_temp
                    self._seq_check_temp_stale()
                    dt   = now - last
                    last = now
                    state = self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                    soc_now = state["soc"]
                    dah = abs(i_meas) * dt / 3600.0
                    ah_out[_band(soc_now)] += dah
                    self.controller._log_sample(v, i_meas)
                    self.update_display(v, i_meas, soc_now, state["rin"], temp, state.get("soh"))
                    status(f"Discharge: {v:.3f} V  SoC {soc_now:.0f}%  "
                           f"Ah_out={sum(ah_out.values()):.3f}")
                    if v <= pack_min:
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[CHAR/η] discharge read error: {exc}")
                    break
                if not self._char_sleep(ev, 5.0):
                    break

            self.hw.set_load(False)
            if not ev.is_set():
                return

            # ── compute η ─────────────────────────────────────────────────
            from aset_batt.core.characterization import compute_coulomb_eta
            eta = compute_coulomb_eta(ah_in, ah_out)

            self._char_results["eta"] = {
                "coulomb_eta_bulk":   eta.get("bulk"),
                "coulomb_eta_absorb": eta.get("absorb"),
                "coulomb_eta_full":   eta.get("full"),
                "coulomb_eta_overall": eta.get("overall"),
                "ah_in":  dict(ah_in),
                "ah_out": dict(ah_out),
            }
            b = eta.get("bulk")   or 0
            a = eta.get("absorb") or 0
            f = eta.get("full")   or 0
            status(f"✓ η bulk={b:.3f}  absorb={a:.3f}  full={f:.3f}")
            self.sig_alarm.emit(f"[CHAR/η] เสร็จสิ้น: bulk={b:.3f} absorb={a:.3f} full={f:.3f}")

        except Exception as exc:
            self.sig_char_update.emit("eta", f"✗ Error: {exc}")
            logger.exception("Eta thread error")
        finally:
            ev.clear()
            self.sig_char_update.emit("eta", "__DONE__")

    # ── OCV–SoC GITT ──────────────────────────────────────────────────────────

    def _on_char_gitt_start(self):
        if not self._char_guard():
            return
        if self._char_running.get("gitt", _FalseEvent()).is_set():
            return
        ev = threading.Event()
        ev.set()
        self._char_running["gitt"] = ev
        self.btn_char_gitt_start.setEnabled(False)
        self.btn_char_gitt_cancel.setEnabled(True)
        self.pgb_char_gitt.setValue(0)
        self.sig_char_update.emit("gitt", "● กำลังทดสอบ GITT OCV–SoC...")
        import threading as _th
        _th.Thread(target=self._char_gitt_thread, daemon=True).start()

    def _on_char_gitt_cancel(self):
        if "gitt" in self._char_running:
            self._char_running["gitt"].clear()
        self._char_hw_stop()

    def _char_gitt_thread(self):
        """Background: GITT OCV characterization — 20× (5% discharge + rest → V_rest)."""
        import time
        ev = self._char_running["gitt"]

        def status(msg):
            # see the same comment in _char_peukert_thread — the rest-phase loop
            # alone can tick every 15s for up to 60 min, across 20 steps.
            self.sig_char_update.emit("gitt", msg)

        try:
            self.controller._ensure_logging(label="GITT")
            rated    = self.controller.config.battery.rated_capacity
            pack_min = self.controller.config.battery.pack_min_voltage
            cells    = self.controller.config.battery.cells_series

            # discharge current for 5% SoC in 36 min = 0.1C (exactly)
            i_dis   = round(0.1 * rated, 3)
            dis_dur = 36 * 60         # 36 min at 0.1C → 6% capacity removed ≈ 5% SoC step
            N_STEPS = 20
            REST_MAX_S = 3600         # wait up to 60 min for settle
            DV_MV_THRESH = 2.0        # ΔV < 2 mV over 60 s window → settled
            DV_WIN_S     = 60

            soc_points: list = []
            ocv_points: list = []   # V per cell

            # OCV anchor before starting
            soc_start = self.controller.calibrate_from_ocv()
            status(f"GITT: OCV anchor SoC={soc_start:.0f}%  ·  {N_STEPS} จุดจะทดสอบ")
            self.sig_alarm.emit(f"[CHAR/GITT] เริ่มทดสอบ — OCV anchor SoC={soc_start:.0f}%")
            if not ev.is_set():
                return

            for step in range(N_STEPS):
                if not ev.is_set():
                    return

                status(f"Step {step+1}/{N_STEPS}: discharge {i_dis:.3f} A × {dis_dur//60} min...")
                self.sig_alarm.emit(f"[CHAR/GITT] Step {step+1}/{N_STEPS}: เริ่ม discharge")
                self.hw.set_load(True, i_dis)
                # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
                # phase_start is tracked SEPARATELY from `last` (the per-sample dt
                # reference) — the old code reused `last` for both, but `last` is
                # reassigned to `now` every iteration, so the while-condition below was
                # effectively re-checking "< dis_dur since the LAST SAMPLE" (always
                # true) instead of "< dis_dur since the phase started" — the discharge
                # phase never actually timed out on its own via dis_dur.
                phase_start = time.perf_counter()
                last = phase_start

                # ── discharge phase ────────────────────────────────────────
                while ev.is_set() and (time.perf_counter() - phase_start) < dis_dur:
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        now  = time.perf_counter()   # stamp AT the measurement
                        temp = self.hw.current_temp
                        self._seq_check_temp_stale()
                        dt   = now - last
                        last = now
                        state = self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                        # GITT never calls start_charge() so there's no monitor-loop
                        # safety net feeding CSV/cloud or the live graph — do it directly,
                        # same as the other CHARACTERIZE tests.
                        self.controller._log_sample(v, i_meas)
                        self.update_display(v, i_meas, state["soc"], state["rin"], temp, state.get("soh"))
                        if v <= pack_min:
                            status(f"Step {step+1}: UVP reached — หยุด")
                            break
                    except Exception as exc:
                        self.sig_alarm.emit(f"[CHAR/GITT] step {step+1} read err: {exc}")
                        break
                    self._char_sleep(ev, 5.0)

                self.hw.set_load(False)
                if not ev.is_set():
                    return

                # ── rest phase — wait for ΔV/Δt settle ───────────────────
                status(f"Step {step+1}/{N_STEPS}: พักจน ΔV settle (สูงสุด {REST_MAX_S//60} min)...")
                self.sig_alarm.emit(f"[CHAR/GITT] Step {step+1}/{N_STEPS}: discharge เสร็จ, เริ่มพัก settle")
                # perf_counter (monotonic, sub-ms): see the comment in _auto_sequence_thread.
                t_rest0 = time.perf_counter()
                v_window: list = []
                t_window: list = []
                v_rest   = None

                while ev.is_set() and (time.perf_counter() - t_rest0) < REST_MAX_S:
                    try:
                        v_now, _, _ = self.hw.read_vi()
                        t_now = time.perf_counter()
                        self.controller._log_sample(v_now, 0.0)
                        self.update_display(v_now, 0.0, self.controller.estimator.soc,
                                            self.controller.estimator.rin)
                        v_window.append(v_now)
                        t_window.append(t_now)
                        # keep only last DV_WIN_S seconds in window
                        while t_window and (t_now - t_window[0]) > DV_WIN_S:
                            v_window.pop(0)
                            t_window.pop(0)
                        if len(v_window) >= 4:
                            dv_mv = (max(v_window) - min(v_window)) * 1000
                            elapsed_r = int(t_now - t_rest0)
                            status(f"Step {step+1}/{N_STEPS}: rest {elapsed_r}s  "
                                   f"V={v_now:.4f}  ΔV={dv_mv:.1f} mV")
                            if dv_mv < DV_MV_THRESH and elapsed_r >= 300:
                                v_rest = v_now
                                break
                    except Exception:
                        pass
                    if not self._char_sleep(ev, 15.0):
                        break

                if not ev.is_set():
                    return

                # fallback: use last measured voltage if timeout
                if v_rest is None:
                    try:
                        v_rest, _, _ = self.hw.read_vi()
                    except Exception:
                        v_rest = v_window[-1] if v_window else 0.0

                soc_now = getattr(self.controller.estimator, "soc", 0.0)
                ocv_cell = v_rest / cells if cells > 0 else v_rest
                soc_points.append(soc_now)
                ocv_points.append(ocv_cell)
                self.sig_char_update.emit("gitt", f"__PROGRESS__{step+1}")
                status(f"Step {step+1}/{N_STEPS}: ✓ SoC={soc_now:.1f}%  "
                       f"V_rest={v_rest:.4f} V  OCV/cell={ocv_cell:.4f} V")

                if soc_now <= 5.0:
                    status(f"SoC ≤ 5% — หยุดที่ step {step+1}")
                    break

            # ── build OCV table ───────────────────────────────────────────
            if len(soc_points) >= 3:
                from aset_batt.core.characterization import build_ocv_table
                table = build_ocv_table(soc_points, ocv_points)
                self._char_results["gitt"] = {
                    "ocv_curve_measured": {str(k): v for k, v in table.items()},
                    "gitt_raw": list(zip(soc_points, ocv_points)),
                    "n_points": len(soc_points),
                }
                status(f"✓ OCV table สร้างแล้ว ({len(soc_points)} จุด วัดจริง)")
                self.sig_alarm.emit(f"[CHAR/GITT] เสร็จสิ้น: OCV table {len(soc_points)} จุด")
            else:
                status(f"⚠ ได้ข้อมูล {len(soc_points)} จุด — ต้องการ ≥ 3 จุด")
                self.sig_alarm.emit(f"[CHAR/GITT] ⚠ ข้อมูลไม่พอ ({len(soc_points)} จุด)")

        except Exception as exc:
            self.sig_char_update.emit("gitt", f"✗ Error: {exc}")
            logger.exception("GITT thread error")
        finally:
            ev.clear()
            self.sig_char_update.emit("gitt", "__DONE__")

    # ── slot & helpers ─────────────────────────────────────────────────────────

    def _slot_char_update(self, test_id: str, msg: str):
        """Dispatch characterize thread messages to the correct UI widgets."""
        if msg == "__DONE__":
            # re-enable start, disable cancel
            if test_id == "pk":
                self.btn_char_pk_start.setEnabled(True)
                self.btn_char_pk_cancel.setEnabled(False)
            elif test_id == "eta":
                self.btn_char_eta_start.setEnabled(True)
                self.btn_char_eta_cancel.setEnabled(False)
            elif test_id == "gitt":
                self.btn_char_gitt_start.setEnabled(True)
                self.btn_char_gitt_cancel.setEnabled(False)
            self._refresh_char_params()
            # enable save if at least one result exists
            if self._char_results:
                self.btn_char_save.setEnabled(True)
            return

        if test_id == "gitt" and msg.startswith("__PROGRESS__"):
            n = int(msg.replace("__PROGRESS__", ""))
            self.pgb_char_gitt.setValue(n)
            self.pgb_char_gitt.setFormat(f"{n} / 20 จุด")
            return

        # status text dispatch
        lbl = None
        if test_id == "pk":
            lbl = self.lbl_char_pk_status
        elif test_id == "eta":
            lbl = self.lbl_char_eta_status
        elif test_id == "gitt":
            lbl = self.lbl_char_gitt_status

        if lbl is not None:
            lbl.setText(msg)
            # colour: green for ✓, red for ✗, yellow for running
            if msg.startswith("✓"):
                lbl.setStyleSheet(f"color:{OK}; font-size:11px; font-weight:600;")
            elif msg.startswith("✗"):
                lbl.setStyleSheet(f"color:{CRIT}; font-size:11px; font-weight:600;")
            else:
                lbl.setStyleSheet(f"color:{WARN}; font-size:11px; font-weight:600;")

    def _refresh_char_params(self):
        """Refresh the 'Profile Parameters' text panel from profile defaults + _char_results."""
        try:
            from aset_batt.core import battery_profiles as _bp
            prod_name = getattr(self, "cb_product", None)
            prod_name = self.cb_product.currentText() if prod_name else ""

            chem_name = getattr(self.controller.config.battery, "battery_type", "")
            chem = _bp.get_chemistry(chem_name)

            # Peukert k
            k_def  = chem.peukert_k
            hr_def = chem.peukert_hr
            pk_res = self._char_results.get("pk", {})
            k_show = f"{pk_res['peukert_k']:.3f} (วัดแล้ว, R²={pk_res.get('peukert_k_r2',0):.3f})" \
                     if pk_res else f"{k_def:.3f} (ค่า default)"

            # Coulomb η
            eta_res = self._char_results.get("eta", {})
            if eta_res:
                b = eta_res.get("coulomb_eta_bulk")   or 0
                a = eta_res.get("coulomb_eta_absorb") or 0
                f = eta_res.get("coulomb_eta_full")   or 0
                eta_show = f"bulk={b:.3f}  absorb={a:.3f}  full={f:.3f} (วัดแล้ว)"
            else:
                eta_show = "bulk=0.970  absorb=0.920  full=0.750 (ค่า default)"

            # OCV table
            gitt_res = self._char_results.get("gitt", {})
            ocv_show = (f"{gitt_res['n_points']} จุด วัดแล้ว"
                        if gitt_res else f"{len(chem.ocv_curve)} จุด built-in")

            lines = [
                f"Profile: {prod_name or '(ไม่ได้เลือก)'}",
                f"Peukert k  : {k_show}",
                f"C-rate hour: {hr_def:.0f} HR",
                f"Coulomb η  : {eta_show}",
                f"OCV table  : {ocv_show}",
            ]

            # also show on-disk measured params if any
            if prod_name:
                mp = _bp.get_measured_params(prod_name)
                if mp:
                    lines.append(f"On-disk    : วัดล่าสุด {mp.get('measured_date','?')}")

            self.txt_char_params.setPlainText("\n".join(lines))
        except Exception as exc:
            self.txt_char_params.setPlainText(f"(ไม่สามารถโหลด params: {exc})")

    def _on_char_save(self):
        """Save _char_results back to battery_profiles.json for the current product."""
        if not self._char_results:
            return
        try:
            from aset_batt.core import battery_profiles as _bp
            prod_name = self.cb_product.currentText()
            if not prod_name:
                QMessageBox.warning(self, "Save Profile", "เลือก product ก่อน save")
                return

            params: dict = {}
            pk_res = self._char_results.get("pk", {})
            if pk_res:
                params["peukert_k"]    = round(pk_res["peukert_k"], 4)
                params["peukert_k_r2"] = round(pk_res.get("peukert_k_r2", 0), 4)
                params["peukert_hr"]   = pk_res.get("peukert_hr", 10.0)

            eta_res = self._char_results.get("eta", {})
            if eta_res:
                for key in ("coulomb_eta_bulk", "coulomb_eta_absorb",
                            "coulomb_eta_full", "coulomb_eta_overall"):
                    v = eta_res.get(key)
                    if v is not None:
                        params[key] = round(v, 4)

            gitt_res = self._char_results.get("gitt", {})
            if gitt_res and "ocv_curve_measured" in gitt_res:
                params["ocv_curve_measured"] = gitt_res["ocv_curve_measured"]

            ok = _bp.save_measured_params(prod_name, params)
            if ok:
                QMessageBox.information(self, "Save Profile",
                    f"บันทึกผลการวัดไปยัง battery_profiles.json สำเร็จ\n"
                    f"Profile: {prod_name}")
                self._refresh_char_params()
            else:
                QMessageBox.critical(self, "Save Profile",
                    "เขียนไฟล์ไม่ได้ — ดู log สำหรับรายละเอียด")
        except Exception as exc:
            QMessageBox.critical(self, "Save Profile", str(exc))



