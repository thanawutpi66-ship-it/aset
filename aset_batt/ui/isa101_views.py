"""
PySide6 ISA-101 HMI for ASET Battery Tester.

This is the supported desktop UI for the main application. It keeps the
existing controller / estimator / analysis contracts, but presents them in the
desaturated high-performance style used by the standalone command center.
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
    QApplication, QToolBar,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)

        # 1. Appearance
        lbl_app = QLabel("APPEARANCE")
        lbl_app.setStyleSheet("font-weight: bold; color: #a1a6ab; margin-top: 10px;")
        lay.addWidget(lbl_app)
        self.cb_dark = QCheckBox("Dark Theme")
        if parent and hasattr(parent, 'config'):
            # SystemConfig จริงมี ui_theme ("light"/"dark") ไม่มี dark_mode — field เดิม
            # ที่ dialog นี้อ้างถึงไม่เคยมีอยู่จริง เปิดกี่ครั้งก็ AttributeError ทันที
            self.cb_dark.setChecked(parent.config.system.ui_theme == "dark")
        lay.addWidget(self.cb_dark)

        # 2. Cloud Integration
        lbl_cloud = QLabel("CLOUD INTEGRATION")
        lbl_cloud.setStyleSheet("font-weight: bold; color: #a1a6ab; margin-top: 10px;")
        lay.addWidget(lbl_cloud)

        row1 = QHBoxLayout()
        self.cb_push = QCheckBox("Enable Cloud Push")
        if parent and hasattr(parent, 'config'):
            self.cb_push.setChecked(parent.config.system.cloud_push_enabled)
        row1.addWidget(self.cb_push)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Cloud Dashboard URL:"))
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("https://...")
        if parent and hasattr(parent, 'config'):
            self.ed_url.setText(parent.config.system.cloud_dashboard_url)
        row2.addWidget(self.ed_url, 1)
        lay.addLayout(row2)

        # NOTE: OVP/UVP/OTP/UTP safety limits are edited from the SETUP tab's
        # "Edit Safety Limits…" button (SafetyLimitsDialog in
        # aset_batt/ui/views/hardware_control.py), not here — they used to
        # live in this dialog too, but Tools → Preferences buried them behind
        # a menu nobody found (and, separately, this dialog was unreachable
        # via that menu until the NameError below was fixed — see dialogs.py).

        # 3. PDF Reporting
        lbl_pdf = QLabel("REPORTS")
        lbl_pdf.setStyleSheet("font-weight: bold; color: #a1a6ab; margin-top: 10px;")
        lay.addWidget(lbl_pdf)
        
        btn_pdf = QPushButton("Generate PDF Report")
        btn_pdf.clicked.connect(self._on_pdf)
        lay.addWidget(btn_pdf)

        lay.addStretch(1)
        btn_box = QHBoxLayout()
        btn_save = QPushButton("Save && Close")
        btn_save.clicked.connect(self.accept)
        btn_box.addStretch(1)
        btn_box.addWidget(btn_save)
        lay.addLayout(btn_box)

    def accept(self):
        parent = self.parent()
        if parent and hasattr(parent, 'config'):
            parent.config.system.ui_theme = "dark" if self.cb_dark.isChecked() else "light"
            parent.config.system.cloud_push_enabled = self.cb_push.isChecked()
            parent.config.system.cloud_dashboard_url = self.ed_url.text()
            parent.config.save_config()
            QMessageBox.information(self, "Restart Required", "Theme changes will take effect on next restart.")
        super().accept()

    def _on_pdf(self):
        parent = self.parent()
        if parent and hasattr(parent, '_on_pdf_report'):
            parent._on_pdf_report()
            self.accept()


import aset_batt.core.battery_profiles as battery_profiles
from aset_batt.core.analysis_module import ChemistryDetector
from aset_batt.core.iec61960_standard import IEC61960Standard

logger = logging.getLogger(__name__)

# ISA-101 palette: neutral gray shell with color reserved for state/alarm only.
from aset_batt.ui import theme

from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html
from aset_batt.ui.zones import ZonesMixin
from aset_batt.ui.sequences import SequencesMixin
from aset_batt.ui.characterize import CharacterizeMixin

from aset_batt.ui.views.ui_builder import UiBuilderMixin
from aset_batt.ui.views.ui_updater import UiUpdaterMixin
from aset_batt.ui.views.ui_slots import UiSlotsMixin
from aset_batt.ui.views.hardware_control import HardwareControlMixin
from aset_batt.ui.views.test_control import TestControlMixin
from aset_batt.ui.views.session_manager import SessionManagerMixin
from aset_batt.ui.views.dialogs import DialogsMixin

class BatteryQtWindow(ZonesMixin, SequencesMixin, CharacterizeMixin, UiBuilderMixin, UiUpdaterMixin, UiSlotsMixin, HardwareControlMixin, TestControlMixin, SessionManagerMixin, DialogsMixin, QMainWindow):
    sig_display = Signal(float, float, float, float, float, float, int)
    sig_profile_status = Signal(str, str)
    sig_charge_status = Signal(str)
    sig_button = Signal(str, bool)
    sig_loading = Signal(str, bool, str)
    sig_conn = Signal()
    sig_alarm = Signal(str)
    sig_safety = Signal(str)
    sig_profile_done = Signal(object)
    sig_analysis_done = Signal(object)
    sig_workflow        = Signal(int, str)   # IEC sequence (phase 0-4)
    sig_qs_workflow     = Signal(int, str)  # Quick Scan (phase 0-3)
    sig_hppc_seq_wf     = Signal(int, str)  # HPPC Full Sequence (phase 0-3)
    sig_cycle_wf        = Signal(int, str)  # Cycle Life (phase 0-3)
    sig_wf_status       = Signal(str)       # workflow status label text (cross-thread safe)
    sig_phase_progress  = Signal(int, int)  # (elapsed_s, total_s); (0,0) = hide
    sig_seq_result      = Signal(str)       # inline result summary after analyze
    sig_seq_done        = Signal(str, str)  # (title, body) — notify when sequence finishes
    sig_char_update     = Signal(str, str)  # (test_id, message) — characterize tab live update
    sig_live_readback   = Signal(float, float, float)  # (v, i, temp) — pre-test live readback
    sig_seq_aborted     = Signal()          # sequence thread ended without completing (error/safety trip)
    sig_cycle_counter   = Signal(str)       # cycle-life counter label text (cross-thread safe)
    sig_update_available = Signal(int, str)  # (behind_count, latest_commit_subject)
    sig_update_done      = Signal(bool, str)  # (ok, message) — result of applying an update

    def __init__(self, config_manager):
        super().__init__()
        self.config = config_manager
        self.controller = None
        self.hw = None
        self.data = None
        self.estimator = None
        self.thread_pool = QThreadPool.globalInstance()
        self._pdf_notifier = _PdfNotifier()
        self._pdf_notifier.finished.connect(self._on_pdf_finished)
        self._headless = os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"

        self.iec_standard = IEC61960Standard(
            self.config.battery.rated_capacity,
            self.config.battery.battery_type,
            self.config.battery.pack_nominal_voltage,
        )

        # Duration-based (not count-based) rolling window — bounds both memory AND
        # the cost of the list(deque) conversion done on every redraw (see
        # _slot_display): without it, a multi-hour test grows this unbounded, and
        # pyqtgraph setData() + the list() conversion both cost O(n), so the GUI
        # gets steadily more sluggish over the session. Was a fixed-COUNT
        # deque(maxlen=20000) — at a higher DEFAULT_SAMPLE_HZ (battery_model.py)
        # that made the visible history window shrink proportionally (66min at
        # 5Hz -> ~13min at 20Hz) purely as a side effect of the rate fix, which
        # operators don't want. _TREND_MAX_DURATION_S=4000s (~66min) preserves the
        # old visible-history length regardless of sample rate — see
        # _trim_trend_buffers, called after every append (test_control.py's
        # _on_test_telemetry, ui_slots.py's _slot_display).
        self._TREND_MAX_DURATION_S = 4000.0
        self.buf_t = deque()
        self.buf_v = deque()
        self.buf_i = deque()
        self.buf_soc = deque()
        self.buf_rin = deque()
        self.buf_temp = deque()
        self._last_trend_redraw = 0.0   # perf_counter of the last graph repaint — see _slot_display
        self._last_hppc_phase_text = None   # skip redundant setText/setStyleSheet — see _on_hppc_telemetry
        self._elapsed_t0 = None
        # Bumped by every exclusive-ownership "start a run" entry point
        # (_on_run_test, _seq_common_start, first CHARACTERIZE test) so a
        # straggling sample from an already-stopped run — e.g. the monitor
        # loop mid-SCPI-read when stop_monitor() is called, which real
        # hardware I/O latency makes easy to hit but Mock's near-zero
        # latency almost never triggers — can be told apart from a genuinely
        # current one and dropped in _slot_display instead of drawing a
        # second overlapping trace of the same color into buf_t/buf_v/etc.
        self._run_generation = 0
        self._sample_index = 0
        self._buttons = {}
        self._profile_map = {}
        self._last_analysis = None
        self._test_thread = None      # characterization worker (QThread)
        self._test_worker = None
        self._last_csv = None         # CSV written by the most recent test/monitor run
        self._seq_running = threading.Event()   # SET while a sequence thread is active
        self._char_running: dict = {}           # {test_id: threading.Event}; set while running
        self._char_results: dict = {}           # {test_id: result_dict} from last successful run

        self._build_ui()
        self._connect_signals()
        self._load_calibration()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_heartbeat_tick)
        self._tick.start(1000)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_timer.start(500)
        self._pulse_state = False

        self._updating = False
        self._start_update_check()

    def _trim_trend_buffers(self):
        """Drop samples older than _TREND_MAX_DURATION_S from the front of the
        trend buffers — duration-based equivalent of the old deque(maxlen=N).
        buf_t holds the same "elapsed" seconds value used as the graph's x-axis,
        so it's the natural trim key. Call after every append to buf_t (some
        call sites only populate a subset of the six buffers — pop only as many
        as are non-empty so they can't drift out of lockstep)."""
        bufs = (self.buf_t, self.buf_v, self.buf_i, self.buf_soc, self.buf_rin, self.buf_temp)
        while len(self.buf_t) > 1 and self.buf_t[-1] - self.buf_t[0] > self._TREND_MAX_DURATION_S:
            for buf in bufs:
                if buf:
                    buf.popleft()

    # ── In-app updater (git fast-forward) ────────────────────────────────




    def bind_controller(self, controller):
        self.controller = controller
        self.hw = controller.hw
        self.data = controller.data
        self.estimator = controller.estimator
        self._refresh_ports()
        self._on_product_changed(self.cb_product.currentText())
        self._update_connection_status()





    # ---- International standard: Menu bar / Toolbar / Status bar ---------------









    # ---- small UI helpers --------------------------------------------------


    # ── SCADA: flash tick ─────────────────────────────────────────────

    # ── SCADA: acknowledge ────────────────────────────────────────────














    _I_IDLE = 0.05  # A — threshold below which current is considered "at rest"





























    # ── Cloud push helpers ────────────────────────────────────────────────
    _cloud_svc = None











    # ---- Workflow slots + sequence threads: see aset_batt/ui/sequences.py --
    # ---- characterization test (acquisition worker on the real HAL) -------













    # map ชนิดการทดสอบ → ชื่อย่อที่อ่านง่าย (จากคอลัมน์ Mode ใน CSV)
    _SESSION_TYPE_MAP = {
        "hppc": "HPPC",
        "discharge": "Discharge",
        "charge": "Charge",
    }

    # ป้าย label ในชื่อไฟล์ (จาก _ensure_logging) → ชื่อที่อ่านง่ายในรายการ session
    _FILENAME_LABEL_MAP = {
        "iec": "IEC 61960", "quickscan": "Quick Scan",
        "hppc": "HPPC", "cyclelife": "Cycle Life",
    }













    # ── SoH Trend chart ──────────────────────────────────────────────────


    # ── Capacity Fade chart ───────────────────────────────────────────────








    # ---- CHARACTERIZE handlers/threads: see aset_batt/ui/characterize.py --


    def closeEvent(self, event):
        if self._headless:
            self._shutdown_services()
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            "Quit",
            "Close the program and stop the test?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._shutdown_services()
            event.accept()
        else:
            event.ignore()
