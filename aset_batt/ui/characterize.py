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

from aset_batt.ui import theme
from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html

logger = logging.getLogger(__name__)


def _char_status_color(msg: str) -> str:
    """Color for a Peukert/ETA/GITT/CCA status message: green for done (✓), red
    for failed (✗), amber for in-progress. Factored out of _slot_char_update so
    _on_retheme() can recompute the same color from a cached message — the
    status label is otherwise only ever styled from that one event-driven slot,
    so it would stay frozen at the OLD theme's color after a live toggle."""
    if msg.startswith("✓"):
        return theme.OK
    elif msg.startswith("✗"):
        return theme.CRIT
    return theme.WARN


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
        theme.style(note, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lay.addWidget(note)

        # ── Card 1 · Peukert k ────────────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("① Peukert  k  — multi-rate discharge"))

        self.lbl_char_pk = QLabel(
            "4 discharge runs (0.1C · 0.2C · 0.5C · 1C) → log-log fit → k\n"
            "ใช้เวลา: ~8–12 ชั่วโมง (ชาร์จ + discharge × 4)")
        self.lbl_char_pk.setWordWrap(True)
        theme.style(self.lbl_char_pk, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_pk)

        self.lbl_char_pk_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_pk_status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_pk_status)

        row_pk = QHBoxLayout()
        self.btn_char_pk_start  = _btn("START Peukert", bg="OK", fg="white", hover="#266a2a")
        self.btn_char_pk_cancel = _btn("CANCEL", bg="CRIT", fg="white", hover="#9b2020")
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
            "Condition to loaded cutoff → rest → verified full charge → rest → "
            "reference discharge to the same loaded cutoff. C10 = 0.500 A; "
            "Qout is not capacity SoH.\n"
            "Estimated duration depends on initial SoC and charge taper; may exceed 40 h.")
        self.lbl_char_eta.setWordWrap(True)
        theme.style(self.lbl_char_eta, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_eta)

        self.lbl_char_eta_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_eta_status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_eta_status)

        row_eta = QHBoxLayout()
        self.btn_char_eta_start  = _btn("START η", bg="OK", fg="white", hover="#266a2a")
        self.btn_char_eta_cancel = _btn("CANCEL", bg="CRIT", fg="white", hover="#9b2020")
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
        theme.style(self.lbl_char_gitt, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_gitt)

        self.lbl_char_gitt_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_gitt_status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_gitt_status)

        self.pgb_char_gitt = QProgressBar()
        self.pgb_char_gitt.setRange(0, 20)
        self.pgb_char_gitt.setValue(0)
        self.pgb_char_gitt.setFormat("0 / 20 จุด")
        self.pgb_char_gitt.setTextVisible(True)
        lay.addWidget(self.pgb_char_gitt)

        row_gitt = QHBoxLayout()
        self.btn_char_gitt_start  = _btn("START GITT", bg="OK", fg="white", hover="#266a2a")
        self.btn_char_gitt_cancel = _btn("CANCEL", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_char_gitt_cancel.setEnabled(False)
        self.btn_char_gitt_start.clicked.connect(self._on_char_gitt_start)
        self.btn_char_gitt_cancel.clicked.connect(self._on_char_gitt_cancel)
        row_gitt.addWidget(self.btn_char_gitt_start)
        row_gitt.addWidget(self.btn_char_gitt_cancel)
        lay.addLayout(row_gitt)

        # ── Card 4 · CCA proxy ──────────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("④ CCA Proxy  — cranking-current sag check"))

        self.lbl_char_cca = QLabel(
            "ชาร์จเต็ม → พัก 5 นาที → pulse ที่กระแส CCA ของ product 30 วิ → เช็ค V ไม่ตก\n"
            "ต่ำกว่า 1.2V/cell — ⚠ ไม่ใช่ CCA มาตรฐาน (ไม่มีคุม 0°C, กระแสอาจถูก clamp ตาม "
            "max_current ของ rig) ใช้เป็นตัวเทียบสุขภาพแบตเทียบกับตัวเองเท่านั้น")
        self.lbl_char_cca.setWordWrap(True)
        theme.style(self.lbl_char_cca, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lay.addWidget(self.lbl_char_cca)

        self.lbl_char_cca_status = QLabel("● ยังไม่ได้ทดสอบ")
        self.lbl_char_cca_status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_char_cca_status)

        row_cca = QHBoxLayout()
        self.btn_char_cca_start  = _btn("START CCA PROXY", bg="OK", fg="white", hover="#266a2a")
        self.btn_char_cca_cancel = _btn("CANCEL", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_char_cca_cancel.setEnabled(False)
        self.btn_char_cca_start.clicked.connect(self._on_char_cca_start)
        self.btn_char_cca_cancel.clicked.connect(self._on_char_cca_cancel)
        row_cca.addWidget(self.btn_char_cca_start)
        row_cca.addWidget(self.btn_char_cca_cancel)
        lay.addLayout(row_cca)

        # ── Profile Parameters panel ──────────────────────────────────────
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("PROFILE PARAMETERS (current + measured)"))

        self.txt_char_params = QTextEdit()
        self.txt_char_params.setReadOnly(True)
        self.txt_char_params.setFont(QFont("Segoe UI", 10))
        self.txt_char_params.setFixedHeight(130)
        lay.addWidget(self.txt_char_params)

        self.btn_char_save = _btn("SAVE TO PROFILE", bg="INFO", fg="white", hover="#0d4a89")
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
        from PySide6.QtCore import QEventLoop, QTimer

        t_end = time.perf_counter() + seconds
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        
        while ev.is_set():
            left = t_end - time.perf_counter()
            if left <= 0:
                return True
                
            sleep_ms = int(min(0.5, left) * 1000)
            if sleep_ms > 0:
                timer.start(sleep_ms)
                loop.exec()
                
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
        if getattr(getattr(self, "operation_state", None), "state", None) is not None \
                and self.operation_state.state.value == "ESTOP_LATCHED":
            return "E_STOP_LATCHED — explicit reset required"
        owner = getattr(getattr(self, "operation_state", None), "active", None)
        if owner is not None:
            return f"{owner.kind} worker still owns hardware"
        if self._char_running.get("eta", _FalseEvent()).is_set():
            return "Coulomb η cycle กำลังทำงานอยู่"
        if self._test_thread is not None:
            return "การทดสอบ Characterization (RUN TEST) กำลังทำงานอยู่"
        if self._seq_running.is_set():
            return "ลำดับทดสอบ (Sequence: AUTO/Quick Scan/HPPC/Cycle Life) กำลังทำงานอยู่"
        if include_char and self._char_any_running():
            return "การทดสอบในแท็บ CHARACTERIZE กำลังทำงานอยู่"
        return None

    def _char_guard(self, test_id: str = "characterize") -> bool:
        """Return True if OK to start a new test.  Shows a warning if not."""
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "Connect hardware first.")
            return False
        if self._char_any_running():
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "Stop the active CHARACTERIZE test first.")
            return False
        # CHARACTERIZE tests share a controller, estimator, and instrument set;
        # the guard above prevents overlapping acquisitions.
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
        if self.controller:
            self.controller.stop_live_readback()
            monitor = getattr(self.controller, "_monitor_thread", None)
            readback = getattr(self.controller, "_live_readback_thread", None)
            if ((monitor is not None and monitor.is_alive())
                    or (readback is not None and readback.is_alive())):
                if not self._headless:
                    QMessageBox.warning(self, "CHARACTERIZE", "Telemetry worker is still exiting; retry shortly.")
                return False
        if getattr(self.controller, "safety_triggered", False):
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "E_STOP_LATCHED — reset safety first.")
            return False
        lease = self.operation_state.claim(f"characterize:{test_id}")
        if lease is None:
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "HARDWARE_BUSY — cleanup is still active.")
            return False
        if hasattr(self, "_ensure_battery_sn"):
            self._ensure_battery_sn()
        self._prepare_new_physical_session(f"characterize:{test_id}")
        self._char_leases[test_id] = lease
        self._operation_leases[lease.run_id] = lease
        self.operation_state.running(lease)
        self._run_generation += 1
        return True

    def _spawn_char_worker(self, test_id: str, target):
        lease = self._char_leases[test_id]

        def run():
            try:
                target()
            finally:
                self.operation_state.cleanup(lease)
                self.sig_operation_worker_exited.emit(lease.run_id)

        thread = threading.Thread(target=run, name=f"aset-characterize-{test_id}", daemon=True)
        lease.thread = thread
        self._char_threads[test_id] = thread
        self._operation_threads[lease.run_id] = thread
        thread.start()
        return thread

    def _char_check_safety(self, ev, temp) -> bool:
        """OTP + temperature-staleness abort for CHARACTERIZE threads. Returns
        True if safe to continue; on a trip it clears ``ev`` (ของเทสต์นั้นเอง)
        and alarms, then the caller's loop breaks → set_load(False)/finally ตัดไฟ

        แยกจาก _seq_check_temp_stale()/_seq_check_otp() (sequences/base.py) เพราะ
        สองตัวนั้น clear self._seq_running ซึ่งเป็นแฟล็กของ sequence — CHARACTERIZE
        ใช้ threading.Event ของตัวเองใน self._char_running ต่อเทสต์ เดิมทีลูปพวกนี้
        เรียก _seq_check_temp_stale() แล้วทิ้งค่า return → OTP-blind ไม่เคยหยุดเทสต์
        และไม่มีการเช็ค OTP เลยด้วยซ้ำ (ดู docstring เดิมของ _seq_check_temp_stale
        ที่บันทึกว่า "needs its own wiring — out of scope") — นี่คือ wiring นั้น"""
        limit = self._otp_limit()
        trip_fn = getattr(self.hw, "get_load_protection_tripped", None)
        if "get_load_protection_tripped" not in getattr(type(self.hw), "__dict__", {}):
            trip_fn = None
        if callable(trip_fn):
            try:
                trip = trip_fn()
            except Exception:
                trip = None
            if trip is None or bool(trip):
                ev.clear()
                reason = "LOAD_PROTECTION_STATUS_UNAVAILABLE" if trip is None else "LOAD_PROTECTION_TRIPPED"
                self.sig_alarm.emit(f"[SAFETY] {reason} — CHARACTERIZE test aborted")
                if self.controller:
                    self.controller._trigger_safety(reason)
                return False
        stale_fn = getattr(self.hw, "temp_is_stale", None)
        if callable(stale_fn):
            info = self.hw.temperature_measurement() if hasattr(self.hw, "temperature_measurement") else None
            invalid = isinstance(info, dict) and not info.get("temperature_valid", False)
            # A short telemetry gap is tolerated and warned once. Once the
            # configured stale escalation interval is exceeded, OTP is blind and
            # the active characterization workflow must stop.
            try:
                sustained_stale = bool(stale_fn(getattr(self, "_SEQ_TEMP_STALE_TRIP_S", 60.0)))
            except TypeError:
                sustained_stale = bool(stale_fn())
            if invalid or stale_fn():
                if not getattr(ev, "_aset_stale_warned", False):
                    self.sig_alarm.emit("[WARNING] ESP32 temperature telemetry is temporarily stale")
                    ev._aset_stale_warned = True
            if sustained_stale:
                ev.clear()
                reason = "TEMP_SENSOR_STALE — OTP protection unavailable, CHARACTERIZE test aborted"
                self.sig_alarm.emit(f"[SAFETY] {reason}")
                if self.controller:
                    self.controller._trigger_safety(reason)
                return False
        if temp is not None and not math.isnan(temp) and temp > limit:
            ev.clear()
            reason = f"OTP: {temp:.1f}°C > {limit:.0f}°C — CHARACTERIZE test aborted"
            self.sig_alarm.emit(f"[SAFETY] {reason}")
            # Same big-banner + hardware-cut path a live E-STOP press uses (G9) — a
            # quiet alarm-log line alone reads as "nothing happened" to an operator
            # watching the main screen, not as the safety trip it actually is.
            if self.controller:
                self.controller._trigger_safety(reason)
            return False
        return True

    def _char_hw_stop(self):
        """Best-effort hardware stop called from cancel handlers."""
        try:
            if self.controller:
                self.controller.stop_charge()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    def _char_finalize_session(self, test_id: str, ev, result_key: str):
        """Close the session owned by Peukert/GITT/CCA on every terminal path."""
        if getattr(self.controller, "safety_triggered", False):
            outcome, reason = "safety_tripped", "safety latch set during characterization"
        elif not ev.is_set():
            outcome, reason = "cancelled", "operator cancelled characterization"
        elif result_key in self._char_results:
            outcome, reason = "completed", f"{test_id} characterization completed"
        else:
            outcome, reason = "fault", f"{test_id} characterization ended without a result"
        try:
            if self.controller:
                self.controller.end_session(outcome, reason)
        except Exception:
            logger.exception("%s session finalization failed", test_id)
        try:
            self.hw.load_off()
            self.hw.psu_off()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    # ── Peukert k ─────────────────────────────────────────────────────────────

    def _on_char_pk_start(self):
        if not self._char_guard("pk"):
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
        self._spawn_char_worker("pk", self._char_peukert_thread)

    def _on_char_pk_cancel(self):
        lease = self._char_leases.get("pk")
        if lease is not None:
            self.operation_state.request_cancel(lease)
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
            from aset_batt.core import battery_profiles
            _product = battery_profiles.get_product(
                self.controller.config.battery.product_name)
            if _product and _product.capacity_10h_ah > 0.0:
                rated = _product.capacity_10h_ah
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
                _cutoff_confirm_n = 0

                while ev.is_set():
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        now  = time.perf_counter()   # stamp AT the measurement
                        temp = self.hw.current_temp
                        if not self._char_check_safety(ev, temp):
                            break
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
                        # Require 5 consecutive at/below-cutoff samples, not just
                        # one — same debounce as worker.py's CC_DISCHARGE cutoff
                        # check (_CUTOFF_CONFIRM_SAMPLES), guarding a single noisy/
                        # sagging sample from ending the test early.
                        _cutoff_confirm_n = (_cutoff_confirm_n + 1) if v <= pack_min else 0
                        if _cutoff_confirm_n >= 5:
                            break
                    except Exception as exc:
                        self.sig_alarm.emit(f"[CHAR/Peukert] read error: {exc}")
                        break
                    if not self._char_sleep(ev, 5.0):
                        break

                self.hw.set_load(False)
                if not ev.is_set():
                    return

                elapsed_s = time.perf_counter() - t0
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
                characterized_at = datetime.now().astimezone().isoformat(timespec="seconds")
                characterization_hr = getattr(
                    self.controller.estimator.battery_model.chemistry,
                    "peukert_hr", 10.0)
                self._char_results["pk"] = {
                    "peukert_k": k, "peukert_k_r2": r2,
                    "peukert_hr": characterization_hr,
                    "characterization_timestamp": characterized_at,
                    "characterization_status": "MEASURED_PENDING_APPROVAL",
                    "data": list(zip(currents, durations)),
                }
                from aset_batt.storage.data_utils import update_session_metadata
                update_session_metadata(self.controller.data.current_path, {
                    "characterized_peukert_k": k,
                    "characterization_r2": r2,
                    "characterization_reference_hr": characterization_hr,
                    "characterization_timestamp": characterized_at,
                    "characterization_status": "MEASURED_PENDING_APPROVAL",
                })
                status(f"✓ k = {k:.3f}  R² = {r2:.4f}")
                self.sig_alarm.emit(f"[CHAR/Peukert] เสร็จสิ้น: k={k:.3f}  R²={r2:.4f}")
            else:
                status("⚠ ได้ข้อมูลไม่พอ fit — ต้องการ ≥ 2 discharge runs")
                self.sig_alarm.emit("[CHAR/Peukert] ⚠ ข้อมูลไม่พอ fit")

        except Exception as exc:
            self.sig_char_update.emit("pk", f"✗ Error: {exc}")
            logger.exception("Peukert thread error")
        finally:
            self._char_hw_stop()
            self._char_finalize_session("Peukert", ev, "pk")
            ev.clear()
            self.sig_char_update.emit("pk", "__DONE__")

    # ── Coulomb η ─────────────────────────────────────────────────────────────

    def _on_char_eta_start(self):
        if not self._char_guard("eta"):
            return
        if self._char_running.get("eta", _FalseEvent()).is_set():
            return
        if self._char_any_running():
            if not self._headless:
                QMessageBox.warning(self, "Coulomb η", "Stop other characterization runs first.")
            return
        ev = threading.Event()
        ev.set()
        self._char_running["eta"] = ev
        self.btn_char_eta_start.setEnabled(False)
        self.btn_char_eta_cancel.setEnabled(True)
        self.sig_char_update.emit("eta", "● กำลังทดสอบ Coulomb η...")
        import threading as _th
        self._spawn_char_worker("eta", self._char_eta_thread)

    def _on_char_eta_cancel(self):
        lease = self._char_leases.get("eta")
        if lease is not None:
            self.operation_state.request_cancel(lease)
        if "eta" in self._char_running:
            self._char_running["eta"].clear()
        if self.controller and getattr(self.controller, "is_charging", False):
            self.controller.stop_charge()
        self._char_hw_stop()

    def _char_eta_thread(self):
        """Run a boundary-matched, incrementally logged coulomb-efficiency cycle."""
        import time
        from aset_batt.core.characterization import (
            evaluate_coulomb_efficiency, integrate_coulomb_ah,
        )
        from aset_batt.storage.data_utils import update_session_metadata

        ev = self._char_running["eta"]
        controller = self.controller
        config = controller.config
        product_name = getattr(config.battery, "product_name", "") or ""
        from aset_batt.core import battery_profiles
        product = battery_profiles.get_product(product_name)
        c10_ah = (float(product.capacity_10h_ah) if product and product.capacity_10h_ah > 0
                  else float(config.battery.rated_capacity))
        c20_ah = (float(product.capacity_20h_ah) if product else 0.0)
        reference_a = c10_ah / 10.0
        cutoff_v = float(product.safety_uvp_pack if product and product.safety_uvp_pack
                         else config.battery.pack_min_voltage)
        limits = config.system.safety_limits or {}
        otp_limit = float(self._otp_limit())
        ovp_limit = float(product.safety_ovp_pack if product and product.safety_ovp_pack
                          else limits.get("max_voltage", float("inf")))
        uvp_floor = float(limits.get("min_voltage", float("-inf")))
        conditioning_limit_s = 1.5 * c10_ah / reference_a * 3600.0
        discharge_limit_s = conditioning_limit_s
        lower_rest_s = 300.0
        post_charge_rest_s = 1800.0
        phase_data = {"conditioning": [], "charge": [], "discharge": [],
                      "CHARGE_CC": [], "CHARGE_CV": [], "CHARGE_TAPER": []}
        phase_elapsed_s = {}
        active_phase = ["INITIALIZING"]
        temperatures = []
        last_charge_sample = [None]
        last_charge_checkpoint_phase = ["CHARGE_CC"]
        started = time.perf_counter()
        csv_path = ""
        status_code = "ERROR"
        reason = "unexpected exit"
        conditioning_ok = False
        full_charge_ok = False
        cutoff_ok = False
        q_in = {"ah": 0.0, "valid": False, "sample_count": 0}
        q_out = {"ah": 0.0, "valid": False, "sample_count": 0}
        eta_result = None

        def status(msg):
            self.sig_char_update.emit("eta", msg)

        def display_eta_result(result, code):
            self._char_results["eta"] = {
                "q_in_ah": result.get("q_in_ah"),
                "q_out_ah": result.get("q_out_ah"),
                "eta_coulomb_pct": result.get("eta_coulomb_pct"),
                "reference_current_a": reference_a,
                "charge_duration_s": sum(v for k, v in phase_elapsed_s.items()
                                           if k.startswith("CHARGE_")),
                "discharge_duration_s": phase_elapsed_s.get("REFERENCE_DISCHARGE", 0.0),
                "status": code, "valid": result.get("valid", False),
            }
            try:
                self._refresh_char_params()
            except RuntimeError:
                # The window may be closing while the daemon worker is finishing;
                # keep file finalization independent from widget lifetime.
                pass

        sample_counter = 0

        def set_phase(phase):
            active_phase[0] = phase
            try:
                controller.data.flush()
                update_session_metadata(csv_path, {
                    "current_phase": phase,
                    "coulomb_eta_checkpoint_at": time.time(),
                    "Qin_Ah": q_in.get("ah"), "Qout_Ah": q_out.get("ah"),
                    "integration": {"Qin": q_in, "Qout": q_out},
                })
            except Exception:
                logger.exception("Could not checkpoint Coulomb η metadata")

        def sample(phase, prefer_load_v, previous, cumulative_ah):
            nonlocal sample_counter
            v, current = self.hw.read_measurements(prefer_load_v=prefer_load_v)
            now = time.perf_counter()
            temp = float(self.hw.current_temp)
            if not self._char_check_safety(ev, temp):
                raise RuntimeError("TEMPERATURE_ABORT")
            if not math.isfinite(float(v)) or not math.isfinite(float(current)):
                raise RuntimeError("non-finite measurement")
            if phase in {"CHARGE_CC", "CHARGE_CV", "CHARGE_TAPER"} and float(v) >= ovp_limit:
                controller._trigger_safety(f"η charge OVP: {v:.3f} V >= {ovp_limit:.3f} V")
                ev.clear()
                raise RuntimeError("OVP_ABORT")
            # The product UVP is the normal, qualified discharge endpoint. A
            # system-wide lower floor remains an emergency trip only when it is
            # below that endpoint; an equal/higher floor must not pre-empt the
            # five-sample product-cutoff confirmation.
            if (phase in {"CONDITIONING_DISCHARGE", "REFERENCE_DISCHARGE"}
                    and uvp_floor < cutoff_v and float(v) <= uvp_floor):
                controller._trigger_safety(f"η discharge hardware UVP: {v:.3f} V <= {uvp_floor:.3f} V")
                ev.clear()
                raise RuntimeError("UVP_ABORT")
            temperatures.append(temp)
            if previous is not None:
                prev_t, prev_i = previous
                dt = now - prev_t
                phase_elapsed_s[phase] = phase_elapsed_s.get(phase, 0.0) + dt
                direction = -1.0 if phase in {"CHARGE_CC", "CHARGE_CV", "CHARGE_TAPER"} else 1.0
                cumulative_ah += 0.5 * (max(0.0, direction * prev_i)
                                        + max(0.0, direction * current)) * dt / 3600.0
            state = controller.estimator.update(v, current, dt=(now - previous[0] if previous else 0.0), temp=temp)
            controller._log_sample(v, current, mode=phase, expected_dt_s=5.0)
            self.update_display(v, current, state.get("soc", 0.0), state["rin"], temp,
                                state.get("soh"))
            phase_data.setdefault(phase, []).append((now - started, current))
            sample_counter += 1
            if sample_counter % 12 == 0:
                set_phase(phase)
            return float(v), float(current), now, temp, cumulative_ah, state

        def charge_sample(stage, voltage, current_in_a, note):
            """Capture every charge-controller read, including the verified tail sample."""
            nonlocal sample_counter
            phase = {"bulk": "CHARGE_CC", "absorption": "CHARGE_CV",
                     "float": "CHARGE_TAPER", "cc": "CHARGE_CC",
                     "cv": "CHARGE_CV", "done": "CHARGE_TAPER"}.get(
                         str(stage).lower(), "CHARGE_CV")
            now = time.perf_counter()
            temp = float(self.hw.current_temp)
            if not self._char_check_safety(ev, temp):
                return
            if not math.isfinite(float(voltage)) or not math.isfinite(float(current_in_a)):
                ev.clear()
                raise RuntimeError("non-finite charge measurement")
            if float(voltage) >= ovp_limit:
                controller._trigger_safety(
                    f"η charge OVP: {voltage:.3f} V >= {ovp_limit:.3f} V")
                ev.clear()
                raise RuntimeError("OVP_ABORT")
            current = -float(current_in_a)
            temperatures.append(temp)
            phase_data.setdefault(phase, []).append((now - started, current))
            sample_counter += 1
            previous_charge = last_charge_sample[0]
            if previous_charge is not None:
                dt = now - previous_charge[0]
                if 0.0 < dt <= 30.0:
                    q_in["ah"] += 0.5 * (
                        max(0.0, -previous_charge[1]) + max(0.0, -current)) * dt / 3600.0
            q_in["sample_count"] += 1
            last_charge_sample[0] = (now, current)
            state = controller.estimator.update(
                float(voltage), current,
                dt=(now - previous_charge[0]) if previous_charge else 0.0,
                temp=temp)
            controller._log_sample(float(voltage), current, mode=phase, expected_dt_s=1.0)
            if q_in["sample_count"] % 5 == 0:
                self.update_display(float(voltage), current, state.get("soc", 0.0),
                                    state["rin"], temp, state.get("soh"))
                status(f"Charge {phase}: {voltage:.3f} V, {current:.3f} A, "
                       f"Qin={q_in['ah']:.3f} Ah ({note})")
            if phase != last_charge_checkpoint_phase[0]:
                set_phase(phase)
                last_charge_checkpoint_phase[0] = phase

        try:
            if controller.data.is_recording:
                controller.end_session("interrupted", "closed before CoulombEfficiency session")
            controller._ensure_logging(label="CoulombEfficiency", protocol={
                "id": "coulomb-efficiency-v1",
                "analysis_version": "coulomb-efficiency-v1",
                "purpose": "boundary-matched Coulombic efficiency, Qout/Qin",
                "phases": ["CONDITIONING_DISCHARGE", "LOWER_REST", "CHARGE_CC",
                           "CHARGE_CV", "CHARGE_TAPER", "POST_CHARGE_REST",
                           "REFERENCE_DISCHARGE", "FINAL_REST"],
            })
            csv_path = controller.data.current_path
            update_session_metadata(csv_path, {
                "test_type": "CoulombEfficiency",
                "analysis_version": "coulomb-efficiency-v1",
                "profile_version": "battery-profiles-v2",
                "product_name": product_name,
                "battery_type": config.battery.battery_type,
                "battery_id": getattr(config.battery, "serial_number", ""),
                "capacity_10h_ah": c10_ah,
                "capacity_20h_ah": c20_ah or None,
                "reference_rate_hr": 10,
                "reference_discharge_current_a": reference_a,
                "cutoff_voltage_v": cutoff_v,
                "temperature_limit_c": otp_limit,
                "conditioning_current_a": reference_a,
                "charge_settings": {
                    "strategy": battery_profiles.get_chemistry(config.battery.battery_type).charge.strategy,
                    "bulk_c_rate": battery_profiles.get_chemistry(config.battery.battery_type).charge.bulk_c_rate,
                    "absorption_voltage_per_cell": battery_profiles.get_chemistry(config.battery.battery_type).charge.absorption_voltage_per_cell,
                    "tail_current_c_rate": battery_profiles.get_chemistry(config.battery.battery_type).charge.tail_current_c_rate,
                    "stage_timeout_min": battery_profiles.get_chemistry(config.battery.battery_type).charge.stage_timeout_min,
                },
            })

            # CONDITIONING_DISCHARGE establishes the common lower state; it is
            # excluded from measured Qin and Qout. Same configured cutoff is used
            # for the final reference discharge.
            controller.hw.load_off()
            controller.hw.psu_off()
            set_phase("CONDITIONING_DISCHARGE")
            status(f"Conditioning to loaded cutoff {cutoff_v:.2f} V at {reference_a:.3f} A...")
            self.hw.set_load(True, reference_a)
            t0 = time.perf_counter()
            previous = None
            cumulative = 0.0
            cutoff_n = 0
            while ev.is_set() and time.perf_counter() - t0 < conditioning_limit_s:
                v, current, now, temp, cumulative, state = sample(
                    "CONDITIONING_DISCHARGE", True, previous, cumulative)
                previous = (now, current)
                cutoff_n = cutoff_n + 1 if v <= cutoff_v else 0
                status(f"Conditioning: {v:.3f} V, {current:.3f} A, {cumulative:.3f} Ah")
                if cutoff_n >= 5:
                    conditioning_ok = True
                    break
                if not self._char_sleep(ev, 5.0):
                    break
            self.hw.load_off()
            self.hw.psu_off()
            if not ev.is_set():
                status_code, reason = (("TEMPERATURE_ABORT", "safety trip during conditioning")
                                       if getattr(controller, "safety_triggered", False)
                                       else ("CANCELLED", "cancelled during conditioning"))
                raise RuntimeError("CANCELLED")
            if not conditioning_ok:
                status_code, reason = "CONDITIONING_INCOMPLETE", "loaded cutoff not reached before timeout"
                raise RuntimeError(reason)

            # Verify the lower-state rest while both outputs remain off.
            set_phase("LOWER_REST")
            rest_until = time.perf_counter() + lower_rest_s
            while ev.is_set() and time.perf_counter() < rest_until:
                v, current, _, temp, _, _ = sample("LOWER_REST", False, None, 0.0)
                if abs(current) > 0.05:
                    raise RuntimeError("lower rest current was not near zero")
                if not self._char_sleep(ev, min(5.0, max(0.0, rest_until - time.perf_counter()))):
                    break
            if not ev.is_set():
                status_code, reason = (("TEMPERATURE_ABORT", "safety trip during lower rest")
                                       if getattr(controller, "safety_triggered", False)
                                       else ("CANCELLED", "cancelled during lower rest"))
                raise RuntimeError("CANCELLED")

            # Start Qin at charge-phase samples only; charge-positive Ah is
            # derived from the project's negative charging-current convention.
            set_phase("CHARGE_CC")
            controller.last_charge_full_confirmed = False
            controller._skip_ocv_reset = True
            controller.stop_live_readback()
            if not controller.start_charge(strategy=None, reuse_session=True,
                                           sample_callback=charge_sample,
                                           start_monitor=False):
                raise RuntimeError("charge controller did not start")
            if controller.monitor_running:
                controller.stop_monitor()
            while ev.is_set() and controller.is_charging:
                if not self._char_sleep(ev, 0.5):
                    break
            charge_thread = getattr(controller, "_charge_thread", None)
            if charge_thread is not None:
                charge_thread.join(timeout=10.0)
            # start_charge() starts the ordinary estimator monitor. This worker
            # owns estimator updates and CSV rows for η, so stop that loop before
            # sampling any following phase.
            if controller.monitor_running:
                controller.stop_monitor()
            charge_error = getattr(controller, "last_charge_error", None)
            if charge_error is not None:
                from aset_batt.services.exceptions import HardwareError
                is_communication_error = isinstance(charge_error, (OSError, HardwareError)) \
                    or "visa" in type(charge_error).__name__.lower()
                if is_communication_error:
                    status_code = "ERROR"
                    reason = f"communication failure during {active_phase[0]}: {charge_error}"
                    raise RuntimeError(reason) from charge_error
            if not ev.is_set():
                controller.stop_charge()
                status_code, reason = (("TEMPERATURE_ABORT", "safety trip during charge")
                                       if getattr(controller, "safety_triggered", False)
                                       else ("CANCELLED", "cancelled during charge"))
                raise RuntimeError("CANCELLED")
            if controller.is_charging:
                controller.stop_charge()
            if not getattr(controller, "last_charge_full_confirmed", False):
                status_code, reason = "FULL_CHARGE_NOT_CONFIRMED", "charge ended without verified taper termination"
                raise RuntimeError(reason)
            full_charge_ok = True
            q_in["valid"] = True
            q_in = integrate_coulomb_ah(
                [x[0] for x in phase_data["CHARGE_CC"] + phase_data["CHARGE_CV"] + phase_data["CHARGE_TAPER"]],
                [x[1] for x in phase_data["CHARGE_CC"] + phase_data["CHARGE_CV"] + phase_data["CHARGE_TAPER"]],
                phase="charge")
            if not q_in["valid"]:
                status_code, reason = "SAMPLING_INVALID", "charge sampling did not meet integration quality limits"
                raise RuntimeError(reason)
            set_phase("POST_CHARGE_REST")
            self.hw.load_off()
            self.hw.psu_off()
            rest_until = time.perf_counter() + post_charge_rest_s
            while ev.is_set() and time.perf_counter() < rest_until:
                v, current, _, temp, _, _ = sample("POST_CHARGE_REST", False, None, 0.0)
                if abs(current) > 0.05:
                    raise RuntimeError("post-charge rest current was not near zero")
                if not self._char_sleep(ev, min(5.0, max(0.0, rest_until - time.perf_counter()))):
                    break
            if not ev.is_set():
                status_code, reason = (("TEMPERATURE_ABORT", "safety trip during post-charge rest")
                                       if getattr(controller, "safety_triggered", False)
                                       else ("CANCELLED", "cancelled during post-charge rest"))
                raise RuntimeError("CANCELLED")

            # Reference discharge begins from the verified full state and ends
            # only after the same loaded cutoff used by conditioning.
            set_phase("REFERENCE_DISCHARGE")
            self.hw.set_load(True, reference_a)
            t0 = time.perf_counter()
            previous = None
            cumulative = 0.0
            cutoff_n = 0
            while ev.is_set() and time.perf_counter() - t0 < discharge_limit_s:
                v, current, now, temp, cumulative, state = sample(
                    "REFERENCE_DISCHARGE", True, previous, cumulative)
                previous = (now, current)
                q_out["ah"] = cumulative
                q_out["sample_count"] = len(phase_data["REFERENCE_DISCHARGE"])
                q_out.update(integrate_coulomb_ah(
                    [x[0] for x in phase_data["REFERENCE_DISCHARGE"]],
                    [x[1] for x in phase_data["REFERENCE_DISCHARGE"]],
                    phase="discharge"))
                if abs(current - reference_a) > max(0.05, reference_a * 0.10):
                    raise RuntimeError("measured reference current outside ±10% tolerance")
                status(f"Reference discharge: {v:.3f} V, {current:.3f} A, Qout={cumulative:.3f} Ah")
                cutoff_n = cutoff_n + 1 if v <= cutoff_v else 0
                if cutoff_n >= 5:
                    cutoff_ok = True
                    break
                if not self._char_sleep(ev, 5.0):
                    break
            self.hw.load_off()
            if not ev.is_set():
                status_code, reason = (("TEMPERATURE_ABORT", "safety trip during reference discharge")
                                       if getattr(controller, "safety_triggered", False)
                                       else ("CANCELLED", "cancelled during reference discharge"))
                raise RuntimeError("CANCELLED")
            if not cutoff_ok:
                status_code, reason = "CUTOFF_NOT_REACHED", "reference discharge cutoff not reached before timeout"
                raise RuntimeError(reason)
            q_out = integrate_coulomb_ah(
                [x[0] for x in phase_data["REFERENCE_DISCHARGE"]],
                [x[1] for x in phase_data["REFERENCE_DISCHARGE"]], phase="discharge")
            q_out["complete"] = bool(cutoff_ok)
            if not q_out["valid"]:
                status_code, reason = "SAMPLING_INVALID", "discharge sampling did not meet integration quality limits"
                raise RuntimeError(reason)
            eta_result = evaluate_coulomb_efficiency(
                q_in, q_out, conditioning_endpoint_valid=conditioning_ok,
                full_charge_confirmed=full_charge_ok,
                reference_cutoff_reached=cutoff_ok)
            status_code = eta_result["status"]
            reason = "; ".join(eta_result["reasons"])
            status(f"COULOMBIC EFFICIENCY: Qin={q_in['ah']:.3f} Ah, Qout={q_out['ah']:.3f} Ah, "
                   f"η={eta_result['eta_coulomb_pct']:.2f}% ({status_code})")
            display_eta_result(eta_result, status_code)
            self.sig_alarm.emit("[CHAR/η] This is Coulombic efficiency, not capacity SoH.")
            self.sig_alarm.emit(f"[CHAR/η] Valid cycle completed: Qin={q_in['ah']:.3f} Ah, "
                                f"Qout={q_out['ah']:.3f} Ah, η={eta_result['eta_coulomb_pct']:.2f}%")
            # End at the same lower state with both outputs off; retain an
            # explicit final-rest phase and check measured current near zero.
            set_phase("FINAL_REST")
            self.hw.load_off()
            self.hw.psu_off()
            final_until = time.perf_counter() + 60.0
            while ev.is_set() and time.perf_counter() < final_until:
                _, current, _, _, _, _ = sample("FINAL_REST", False, None, 0.0)
                if abs(current) > 0.05:
                    raise RuntimeError("final rest current was not near zero")
                if not self._char_sleep(ev, min(5.0, max(0.0, final_until - time.perf_counter()))):
                    break
            if not ev.is_set():
                status_code, reason = ("TEMPERATURE_ABORT", "safety trip during final rest") \
                    if getattr(controller, "safety_triggered", False) else ("CANCELLED", "cancelled during final rest")
                eta_result = evaluate_coulomb_efficiency(
                    q_in, q_out, conditioning_endpoint_valid=conditioning_ok,
                    full_charge_confirmed=full_charge_ok, reference_cutoff_reached=cutoff_ok,
                    aborted=True, abort_reason=reason)
                display_eta_result(eta_result, status_code)
                raise RuntimeError("CANCELLED")
        except Exception as exc:
            if str(exc) == "TEMPERATURE_ABORT":
                status_code, reason = "TEMPERATURE_ABORT", "temperature limit or stale temperature trip"
            elif str(exc) in {"OVP_ABORT", "UVP_ABORT"}:
                status_code, reason = "ERROR", str(exc)
            elif status_code == "ERROR" and getattr(controller, "safety_triggered", False):
                status_code, reason = "SAFETY_ABORT", str(exc)
            if status_code in {"ERROR", "CANCELLED", "TEMPERATURE_ABORT"} and str(exc) == "CANCELLED":
                if status_code == "ERROR":
                    status_code, reason = "CANCELLED", "cancelled"
            elif status_code == "ERROR" and str(exc) != "unexpected exit":
                reason = str(exc)
            logger.exception("Coulomb η sequence stopped: %s", exc)
        finally:
            self._char_hw_stop()
            # A measurement exception can leave either output enabled before
            # control reaches the normal phase-end OFF commands. Always restore
            # the instrument outputs here as well, including failures inside a
            # load or charge acquisition call.
            # All normal sequence code uses set_load(False); keep the same
            # established controller interface here for cleanup on exceptions.
            for output_off in (lambda: self.hw.set_load(False), self.hw.psu_off):
                try:
                    output_off()
                except Exception:
                    logger.exception("Coulomb η final output shutdown failed")
            try:
                if csv_path:
                    controller.data.flush()
                    q_out["complete"] = bool(cutoff_ok)
                    if eta_result is None:
                        eta_result = evaluate_coulomb_efficiency(
                            q_in, q_out, conditioning_endpoint_valid=conditioning_ok,
                            full_charge_confirmed=full_charge_ok,
                            reference_cutoff_reached=cutoff_ok,
                            aborted=True, abort_reason=reason or status_code)
                    display_eta_result(eta_result, status_code)
                    temps = [x for x in temperatures if math.isfinite(x)]
                    update_session_metadata(csv_path, {
                        "Qin_Ah": q_in.get("ah"), "Qout_Ah": q_out.get("ah"),
                        "eta_coulomb_pct": eta_result.get("eta_coulomb_pct"),
                        "eta_coulomb_valid": bool(eta_result.get("valid")),
                        "eta_status": status_code,
                        "termination_status": status_code,
                        "termination_reason": reason or status_code,
                        "termination_phase": active_phase[0],
                        "termination_cause": ("cancellation" if status_code == "CANCELLED"
                                               else "communication_failure"
                                               if status_code == "ERROR" and "communication failure" in reason.lower()
                                               else "safety" if status_code in {"TEMPERATURE_ABORT", "SAFETY_ABORT"}
                                               else "protocol_or_measurement"),
                        "eta_reasons": eta_result.get("reasons", []),
                        "conditioning_endpoint_valid": conditioning_ok,
                        "full_charge_confirmed": full_charge_ok,
                        "reference_cutoff_reached": cutoff_ok,
                        "reference_capacity_complete": bool(cutoff_ok),
                        "charge_duration_s": sum(v for k, v in phase_elapsed_s.items()
                                                   if k.startswith("CHARGE_")),
                        "discharge_duration_s": phase_elapsed_s.get("REFERENCE_DISCHARGE", 0.0),
                        "temperature_summary_c": ({"start": temps[0], "min": min(temps),
                                                    "max": max(temps), "end": temps[-1]}
                                                   if temps else None),
                        "integration": {"Qin": q_in, "Qout": q_out},
                        "capacity_interpretation": "Qout is measured charge under this test; not verified SoH",
                    })
                    outcome = ("completed" if status_code in {"VALID", "SUSPECT_RESULT"}
                               else "safety_tripped" if status_code in {"TEMPERATURE_ABORT", "SAFETY_ABORT"}
                               else "cancelled" if status_code == "CANCELLED"
                               else "fault" if status_code in {"ERROR", "FULL_CHARGE_NOT_CONFIRMED"}
                               else "aborted")
                    controller.end_session(outcome, reason or status_code)
            except Exception:
                logger.exception("Coulomb η session finalization failed")
            ev.clear()
            self.sig_char_update.emit("eta", "__DONE__")

    # ── OCV–SoC GITT ──────────────────────────────────────────────────────────

    def _on_char_gitt_start(self):
        if not self._char_guard("gitt"):
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
        self._spawn_char_worker("gitt", self._char_gitt_thread)

    def _on_char_gitt_cancel(self):
        lease = self._char_leases.get("gitt")
        if lease is not None:
            self.operation_state.request_cancel(lease)
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
            from aset_batt.core import battery_profiles
            _product = battery_profiles.get_product(
                self.controller.config.battery.product_name)
            if _product and _product.capacity_10h_ah > 0.0:
                rated = _product.capacity_10h_ah
            pack_min = self.controller.config.battery.pack_min_voltage
            cells    = self.controller.config.battery.cells_series
            campaign = getattr(self.controller.config.system, "validation_campaign", {}) or {}
            reference_capacity_ah = None
            if campaign.get("enabled"):
                try:
                    reference_capacity_ah = float(campaign["reference_capacity_ah"])
                except (KeyError, TypeError, ValueError):
                    status("⚠ Validation GITT requires a qualified C10 reference first")
                    self.sig_alarm.emit("[CHAR/GITT] validation blocked: missing qualified C10 reference")
                    return

            # discharge current for 5% SoC in 36 min = 0.1C (exactly)
            i_dis   = round(0.1 * rated, 3)
            dis_dur = 36 * 60         # 36 min at 0.1C → 6% capacity removed ≈ 5% SoC step
            N_STEPS = 20
            REST_MAX_S = 3600         # wait up to 60 min for settle
            DV_MV_THRESH = 2.0        # ΔV < 2 mV over 60 s window → settled
            DV_WIN_S     = 60

            soc_points: list = []
            ocv_points: list = []   # V per cell
            reference_ah_out = 0.0

            # OCV anchor before starting — no rest phase precedes this at all, so an
            # instant read here is the clearest case of "too-short rest": whatever
            # polarization the pack had from before this test started would go
            # straight into soc_start with no settle-check. Use the same ΔV/Δt
            # settle-checked anchor every other sequence's PREPARE uses.
            def _gitt_start_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"GITT: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")

            soc_start, _, _ = self.controller.calibrate_from_ocv_stable(
                on_progress=_gitt_start_progress,
                cancel_check=ev.is_set,
            )
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
                _cutoff_confirm_n = 0

                # ── discharge phase ────────────────────────────────────────
                while ev.is_set() and (time.perf_counter() - phase_start) < dis_dur:
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        now  = time.perf_counter()   # stamp AT the measurement
                        temp = self.hw.current_temp
                        if not self._char_check_safety(ev, temp):
                            break
                        dt   = now - last
                        last = now
                        if reference_capacity_ah is not None:
                            reference_ah_out += max(0.0, float(i_meas)) * max(0.0, dt) / 3600.0
                        state = self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                        # GITT never calls start_charge() so there's no monitor-loop
                        # safety net feeding CSV/cloud or the live graph — do it directly,
                        # same as the other CHARACTERIZE tests.
                        self.controller._log_sample(v, i_meas)
                        self.update_display(v, i_meas, state["soc"], state["rin"], temp, state.get("soh"))
                        # Same debounce as worker.py's CC_DISCHARGE cutoff check — 5
                        # consecutive at/below-cutoff samples, not just one.
                        _cutoff_confirm_n = (_cutoff_confirm_n + 1) if v <= pack_min else 0
                        if _cutoff_confirm_n >= 5:
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
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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

                if reference_capacity_ah is not None:
                    from aset_batt.core.validation_campaign import reference_soc_from_capacity
                    soc_now = reference_soc_from_capacity(
                        [reference_ah_out], reference_capacity_ah)[0]
                else:
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
                    "reference_soc_source": ("c10_capacity" if reference_capacity_ah is not None
                                             else "estimator"),
                    "reference_capacity_ah": reference_capacity_ah,
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
            self._char_hw_stop()
            self._char_finalize_session("GITT", ev, "gitt")
            ev.clear()
            self.sig_char_update.emit("gitt", "__DONE__")

    # ── CCA proxy ────────────────────────────────────────────────────────────

    def _on_char_cca_start(self):
        if not self._char_guard("cca"):
            return
        if self._char_running.get("cca", _FalseEvent()).is_set():
            return
        prod = battery_profiles.get_product(self.cb_product.currentText())
        cca_a = getattr(prod, "cca_a", 0.0) if prod else 0.0
        if cca_a <= 0:
            lease = self._char_leases.pop("cca", None)
            if lease is not None:
                self.operation_state.release(lease)
                self._operation_leases.pop(lease.run_id, None)
            if not self._headless:
                QMessageBox.warning(
                    self, "CCA Proxy",
                    "Product นี้ไม่มีค่า cca_a (0 = ไม่ใช่แบต starter หรือยังไม่ได้กรอกสเปก)")
            return
        ev = threading.Event()
        ev.set()
        self._char_running["cca"] = ev
        self.btn_char_cca_start.setEnabled(False)
        self.btn_char_cca_cancel.setEnabled(True)
        self.sig_char_update.emit("cca", "● กำลังทดสอบ CCA proxy...")
        import threading as _th
        self._spawn_char_worker("cca", self._char_cca_thread)

    def _on_char_cca_cancel(self):
        lease = self._char_leases.get("cca")
        if lease is not None:
            self.operation_state.request_cancel(lease)
        if "cca" in self._char_running:
            self._char_running["cca"].clear()
        self._char_hw_stop()

    def _char_cca_thread(self):
        """Background: charge full -> rest 5min -> single CCA-rated pulse (30s) ->
        pass/fail against a 1.2V/cell floor (SAE-style, generalised from the
        7.2V/6-cell 12V-battery convention). NOT a certified CCA test — no 0°C
        temperature control, and current is clamped to this rig's configured
        max_current (real CCA current, e.g. 95A, far exceeds what this rig's
        wiring/breaker is rated for on small AGM packs) — see the on-screen note
        and docs/rig_status_action_items.md. Comparative health-check only."""
        import time
        ev = self._char_running["cca"]

        def status(msg):
            # see the comment in _char_peukert_thread — no sig_alarm here, this
            # can tick every few seconds; milestones get their own explicit emit().
            self.sig_char_update.emit("cca", msg)

        try:
            self.controller._ensure_logging(label="CCA")
            prod = battery_profiles.get_product(self.cb_product.currentText())
            cca_a = getattr(prod, "cca_a", 0.0) if prod else 0.0
            max_i = self.controller.config.battery.max_current
            i_test = min(cca_a, max_i)
            clamped = i_test < cca_a
            cells = self.controller.config.battery.cells_series
            v_floor = 1.2 * cells
            pack_min = self.controller.config.battery.pack_min_voltage
            note = " (clamped to rig max_current — NOT true CCA current)" if clamped else ""

            status(f"CCA proxy: ชาร์จเต็มก่อน (rated CCA={cca_a:.0f}A, ทดสอบจริงที่ {i_test:.3f}A{note})...")
            self.sig_alarm.emit(f"[CHAR/CCA] เริ่มทดสอบ — I={i_test:.3f}A{note}")
            self.controller.start_charge(strategy=None)
            while ev.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                if not self._char_sleep(ev, 30.0):
                    return
            if not ev.is_set():
                return
            # see the comment in _char_peukert_thread — start_charge() restarted
            # the shared monitor loop; stop it again before this test's own pulse
            # loop starts double-feeding the estimator.
            if self.controller.monitor_running:
                self.controller.stop_monitor()

            status("CCA proxy: พักหลังชาร์จ 5 นาที...")
            if not self._char_sleep(ev, 300):
                return

            status(f"CCA proxy: pulse {i_test:.3f}A x 30s{note}...")
            t0 = time.perf_counter()
            self.hw.set_load(True, i_test)
            v_min = None
            last = t0
            _cutoff_confirm_n = 0
            while ev.is_set() and (time.perf_counter() - t0) < 30.0:
                try:
                    v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                    now = time.perf_counter()
                    temp = self.hw.current_temp
                    if not self._char_check_safety(ev, temp):
                        break
                    dt = now - last
                    last = now
                    state = self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                    self.controller._log_sample(v, i_meas)
                    self.update_display(v, i_meas, state["soc"], state["rin"], temp)
                    v_min = v if v_min is None else min(v_min, v)
                    elapsed = int(now - t0)
                    status(f"CCA pulse {elapsed}s/30s  {v:.3f}V (min so far {v_min:.3f}V)")
                    # Same debounce as worker.py's CC_DISCHARGE cutoff check — 5
                    # consecutive at/below-cutoff samples, not just one.
                    _cutoff_confirm_n = (_cutoff_confirm_n + 1) if v <= pack_min else 0
                    if _cutoff_confirm_n >= 5:
                        status(f"UVP reached ({v:.3f}V <= {pack_min:.3f}V) — หยุด")
                        break
                except Exception as exc:
                    self.sig_alarm.emit(f"[CHAR/CCA] read error: {exc}")
                    break
                if not self._char_sleep(ev, 0.2):
                    break
            self.hw.set_load(False)
            if not ev.is_set():
                return

            passed = (v_min is not None) and (v_min >= v_floor)
            self._char_results["cca"] = {
                "cca_current_a": i_test, "cca_rated_a": cca_a, "cca_clamped": clamped,
                "cca_v_min": v_min, "cca_v_floor": v_floor, "cca_pass": passed,
            }
            if v_min is None:
                status("✗ CCA proxy: ไม่มีข้อมูลวัดได้")
                self.sig_alarm.emit("[CHAR/CCA] ✗ ไม่มีข้อมูลวัดได้")
            else:
                verdict = "PASS" if passed else "FAIL"
                mark = "✓" if passed else "✗"
                status(f"{mark} CCA proxy {verdict}: V_min={v_min:.3f}V (floor {v_floor:.2f}V){note}")
                self.sig_alarm.emit(f"[CHAR/CCA] เสร็จสิ้น: {verdict} V_min={v_min:.3f}V "
                                    f"floor={v_floor:.2f}V{note}")

        except Exception as exc:
            self.sig_char_update.emit("cca", f"✗ Error: {exc}")
            logger.exception("CCA thread error")
        finally:
            self._char_hw_stop()
            self._char_finalize_session("CCA", ev, "cca")
            ev.clear()
            self.sig_char_update.emit("cca", "__DONE__")

    # ── slot & helpers ─────────────────────────────────────────────────────────

    def _slot_char_update(self, test_id: str, msg: str):
        """Dispatch characterize thread messages to the correct UI widgets."""
        if msg == "__DONE__":
            lease = getattr(self, "_char_leases", {}).get(test_id)
            thread = getattr(self, "_char_threads", {}).get(test_id)
            if lease is not None and thread is not None and thread.is_alive():
                # The worker's finally has run, but ownership remains until its
                # thread has actually returned.
                return
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
            elif test_id == "cca":
                self.btn_char_cca_start.setEnabled(True)
                self.btn_char_cca_cancel.setEnabled(False)
            self._refresh_char_params()
            # enable save if at least one result exists
            if self._char_results:
                self.btn_char_save.setEnabled(True)
            # __DONE__ fires unconditionally (finally-block in every char
            # thread), including after an E-STOP — skip the completion
            # chime then so it doesn't stack on top of _on_estop's siren.
            if not getattr(self.controller, "safety_triggered", False):
                self._play_test_complete_sound()
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
        elif test_id == "cca":
            lbl = self.lbl_char_cca_status

        if lbl is not None:
            lbl.setText(msg)
            if not hasattr(self, "_char_status_msgs"):
                self._char_status_msgs = {}
            self._char_status_msgs[test_id] = msg
            lbl.setStyleSheet(f"color:{_char_status_color(msg)}; font-size:11px; font-weight:600;")

    def _refresh_char_status_colors(self):
        """Called from _on_retheme(): re-picks each Peukert/ETA/GITT/CCA status
        label's color for the CURRENT theme, using the last message
        _slot_char_update saw (or the construction-time MUTED placeholder if
        that sub-test has never run) — otherwise a live theme toggle leaves a
        completed ✓/✗ result frozen at whatever color was picked under the OLD
        theme, since these labels are only ever styled from that one slot."""
        msgs = getattr(self, "_char_status_msgs", {})
        label_map = {
            "pk": getattr(self, "lbl_char_pk_status", None),
            "eta": getattr(self, "lbl_char_eta_status", None),
            "gitt": getattr(self, "lbl_char_gitt_status", None),
            "cca": getattr(self, "lbl_char_cca_status", None),
        }
        for test_id, lbl in label_map.items():
            if lbl is None:
                continue
            msg = msgs.get(test_id)
            color = _char_status_color(msg) if msg is not None else theme.MUTED
            lbl.setStyleSheet(f"color:{color}; font-size:11px; font-weight:600;")

    def _refresh_char_params(self):
        """Refresh the 'Profile Parameters' text panel from profile defaults + _char_results."""
        try:
            from aset_batt.core import battery_profiles as _bp
            prod_name = getattr(self, "cb_product", None)
            prod_name = self.cb_product.currentText() if prod_name else ""

            chem_name = getattr(self.controller.config.battery, "battery_type", "")
            chem = _bp.get_chemistry(chem_name)

            # Active Peukert k follows the same product→chemistry resolver as
            # the estimator and offline analysis profile.
            active_pk = _bp.resolve_peukert_parameters(prod_name, chem_name)
            k_def = active_pk["peukert_k"]
            hr_def = active_pk["peukert_reference_hr"]
            pk_res = self._char_results.get("pk", {})
            k_show = f"{k_def:.3f} ({active_pk['peukert_k_source']})"
            measured_k = (pk_res.get("peukert_k") if pk_res else None)

            # Coulomb η
            eta_res = self._char_results.get("eta", {})
            if eta_res:
                eta_value = eta_res.get("eta_coulomb_pct")
                eta_show = (f"{eta_value:.2f}% ({eta_res.get('status', 'UNKNOWN')}); "
                            f"Qin={eta_res.get('q_in_ah', 0):.3f} Ah, "
                            f"Qout={eta_res.get('q_out_ah', 0):.3f} Ah") \
                    if eta_value is not None else f"{eta_res.get('status', 'UNKNOWN')} (η unavailable)"
            else:
                eta_show = "No valid cycle measured"

            # OCV table
            gitt_res = self._char_results.get("gitt", {})
            ocv_show = (f"{gitt_res['n_points']} จุด วัดแล้ว"
                        if gitt_res else f"{len(chem.ocv_curve)} จุด built-in")

            lines = [
                f"Profile: {prod_name or '(ไม่ได้เลือก)'}",
                f"Peukert k  : {k_show}",
                f"C-rate hour: {hr_def:.0f} HR",
                f"Coulombic Efficiency: {eta_show}",
                f"OCV table  : {ocv_show}",
            ]

            # also show on-disk measured params if any
            if prod_name:
                mp = _bp.get_measured_params(prod_name)
                if mp:
                    lines.append(f"On-disk    : วัดล่าสุด {mp.get('measured_date','?')}")
                    if measured_k is None:
                        measured_k = mp.get("characterized_peukert_k", mp.get("peukert_k"))
                    if pk_res:
                        measured_k = pk_res.get("peukert_k")
            if measured_k is not None:
                measured_r2 = (pk_res.get("peukert_k_r2") if pk_res else
                               mp.get("peukert_k_r2", 0.0) if prod_name and mp else 0.0)
                lines.append(
                    f"Characterized candidate: {float(measured_k):.3f} "
                    f"(R²={float(measured_r2 or 0.0):.3f}; pending approval, inactive)")

            result = self._char_results.get("eta", {})
            if result:
                lines.extend([
                    f"Reference discharge: {result.get('reference_current_a', 0.5):.3f} A (C10)",
                    f"Charge duration: {result.get('charge_duration_s', 0.0) / 3600:.2f} h",
                    f"Discharge duration: {result.get('discharge_duration_s', 0.0) / 3600:.2f} h",
                    "This is Coulombic efficiency, not capacity SoH.",
                ])
            self.txt_char_params.setPlainText("\n".join(lines))
        except Exception as exc:
            self.txt_char_params.setPlainText(f"(ไม่สามารถโหลด params: {exc})")

    def _on_char_save(self):
        """Save _char_results back to battery_profiles.json for the current product."""
        if self._char_running.get("eta", _FalseEvent()).is_set():
            QMessageBox.warning(self, "Save Profile", "Wait until the active η cycle finishes.")
            return
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
                params["characterized_peukert_k"] = round(pk_res["peukert_k"], 4)
                params["peukert_k_r2"] = round(pk_res.get("peukert_k_r2", 0), 4)
                params["peukert_hr"]   = pk_res.get("peukert_hr", 10.0)
                params["peukert_k_source"] = "CHARACTERIZED_MEASURED"
                params["characterization_timestamp"] = pk_res.get(
                    "characterization_timestamp")
                params["characterization_status"] = "MEASURED_PENDING_APPROVAL"

            eta_res = self._char_results.get("eta", {})
            # A cycle η measurement is session evidence, not a battery profile
            # parameter. Its values and validity gates remain in CSV metadata.

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



