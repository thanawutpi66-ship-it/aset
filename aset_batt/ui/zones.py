"""
UI construction for the left-panel zones (SETUP / workflow guide / RUN pages /
TEST MODE / TOOLS) and the right panel tabs (analytics, diagnostics, alarms).
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


class ZonesMixin:
    # ---- ZONE 1: SETUP (battery + connections) -----------------------------
    def _zone_setup(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        # Battery selection
        lay.addWidget(self._subheader("BATTERY"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Battery:"))
        self.cb_product = QComboBox()
        self.cb_product.addItems(battery_profiles.list_products())
        self.cb_product.currentTextChanged.connect(self._on_product_changed)
        self._combo_shrink(self.cb_product, 8)
        row.addWidget(self.cb_product, 1)
        lay.addLayout(row)
        self.lbl_battery_readout = QLabel("—")
        theme.style(self.lbl_battery_readout, lambda: f"color:{theme.MUTED};")
        lay.addWidget(self.lbl_battery_readout)
        self.lbl_safety_limits = QLabel("—")
        theme.style(self.lbl_safety_limits, lambda: f"color:{theme.CRIT}; font-size:11px; font-weight:600;")
        lay.addWidget(self.lbl_safety_limits)
        actions = QHBoxLayout()
        self.btn_detect = _btn("Detect Chemistry", bg="PANEL", hover="PANEL2")
        self.btn_detect.clicked.connect(self._on_detect_chemistry)
        actions.addWidget(self.btn_detect)
        lay.addLayout(actions)



        # Connections — each port row has a status LED (● gray=idle, ✓ green=ok, ✗ red=fail)
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("CONNECTIONS"))
        self.cb_psu = QComboBox()
        self.cb_load = QComboBox()
        self.cb_esp = QComboBox()

        def _led():
            lbl = QLabel("●")
            lbl.setStyleSheet(f"color:{theme.NEUTRAL}; font-size:15px; min-width:18px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return lbl

        self.led_psu  = _led()
        self.led_load = _led()
        self.led_esp  = _led()

        for label_text, cb, led in [
            ("PSU (VISA):", self.cb_psu, self.led_psu),
            ("Load (VISA):", self.cb_load, self.led_load),
            ("ESP32 (COM):", self.cb_esp, self.led_esp),
        ]:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setMinimumWidth(78)
            row.addWidget(lbl)
            row.addWidget(cb, 1)
            row.addWidget(led)
            lay.addLayout(row)
        row = QHBoxLayout()
        self.btn_connect = _btn("Connect", bg="OK", fg="white", hover="#266a2a")
        self.btn_disconnect = _btn("Disconnect", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_disconnect)
        lay.addLayout(row)
        btn_refresh = _btn("Refresh Ports", bg="PANEL", hover="PANEL2")
        btn_refresh.clicked.connect(self._refresh_ports)
        lay.addWidget(btn_refresh)



        return w

    # ---- WORKFLOW GUIDE (5-step sequence with auto-run) ----------------------
    _WF_STEPS = [
        ("1", "PREPARE",  "OCV calibrate"),
        ("2", "CHARGE",   "Full 3-stage"),
        ("3", "REST",     "30 min rest"),
        ("4", "TEST",     "Discharge 0.2C"),
        ("5", "ANALYZE",  "SoH + Grade"),
    ]
    _QS_STEPS = [
        ("1", "PREPARE",   "OCV calibrate"),
        ("2", "REST",      "5 min settle"),
        ("3", "DISCHARGE", "1C rapid test"),
        ("4", "ANALYZE",   "Peukert SoH"),
    ]
    _HPPC_SEQ_STEPS = [
        ("1", "PREPARE", "OCV calibrate"),
        ("2", "CHARGE",  "CC-CV to 100%"),
        ("3", "REST",    "OCV settle"),
        ("4", "HPPC",    "Pulse/relax cycles"),
        ("5", "ANALYZE", "R0/R1/C1/τ ECM"),
    ]
    _CYCLE_STEPS = [
        ("1", "PREPARE",   "OCV calibrate"),
        ("2", "CHARGE",    "CC-CV"),
        ("3", "DISCHARGE", "CC to cutoff"),
        ("4", "REPEAT",    "N cycles"),
        ("5", "ANALYZE",   "Capacity fade"),
    ]

    def _zone_workflow(self):
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(6)

        # ── Persistent phase banner — always shows TEST · PHASE while running ──
        self.lbl_phase_banner = QLabel("● IDLE — เลือก workflow แล้วกด RUN")
        self.lbl_phase_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_phase_banner.setStyleSheet(
            f"background:{theme.PANEL2}; color:{theme.MUTED}; border:1px solid {theme.BORDER}; "
            f"border-radius:5px; padding:6px 8px; font-size:13px; font-weight:700;"
        )
        outer_lay.addWidget(self.lbl_phase_banner)
        self._current_test_name = ""

        def _step_widget(steps_list, led_list, min_name_w, desc_list=None, time_list=None):
            sw = QWidget()
            sl = QVBoxLayout(sw)
            sl.setContentsMargins(0, 4, 0, 4)
            sl.setSpacing(2)
            for _num, name, desc in steps_list:
                row = QHBoxLayout()
                dot = QLabel("○")
                theme.style(dot, lambda: f"color:{theme.NEUTRAL}; font-size:16px; min-width:22px;")
                dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
                name_lbl = QLabel(name)
                theme.style(name_lbl, lambda w=min_name_w: (
                    f"color:{theme.MUTED}; font-weight:700; min-width:{w}px;"))
                desc_lbl = QLabel(desc)
                theme.style(desc_lbl, lambda: f"color:{theme.MUTED}; font-size:11px;")
                row.addWidget(dot)
                row.addWidget(name_lbl)
                row.addWidget(desc_lbl)
                row.addStretch(1)
                sl.addLayout(row)
                led_list.append((dot, name_lbl))
                if desc_list is not None:
                    desc_list.append(desc_lbl)
                if time_list is not None:
                    # Estimated-duration line directly under the step — indented past
                    # the dot so it visually belongs to the row above it.
                    time_lbl = QLabel("~ — ")
                    time_lbl.setStyleSheet(
                        f"color:{theme.INFO}; font-size:10px; padding-left:{min_name_w + 22}px;"
                    )
                    sl.addWidget(time_lbl)
                    time_list.append(time_lbl)
            return sw

        # ── Workflow selector dropdown ─────────────────────────
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Workflow:"))
        self.cb_workflow_type = QComboBox()
        self.cb_workflow_type.addItems([
            "IEC 61960 Standard",
            "Quick Scan",
            "HPPC Full Sequence",
            "Cycle Life Test",
            # Same PREPARE→CHARGE→REST→DISCHARGE→ANALYZE machinery as the IEC
            # workflow (page index 0 — see _on_workflow_type_changed's page map);
            # selecting it PRESETS the standard's own conditions visibly (I10
            # reference rate, no skipped phases) instead of silently overriding
            # at run time. The result carries the EN 50342-1 Ce-vs-Cn verdict.
            "EN 50342-1 Lead-Acid C10",
        ])
        self._combo_shrink(self.cb_workflow_type, 10)
        sel_row.addWidget(self.cb_workflow_type, 1)
        outer_lay.addLayout(sel_row)

        # ── QStackedWidget — สลับเนื้อหาตาม workflow ─────────
        self._wf_stack = QStackedWidget()

        # ── Page 0: IEC 61960 ────────────────────────────────
        iec_page = QWidget()
        iec_lay = QVBoxLayout(iec_page)
        iec_lay.setContentsMargins(0, 0, 0, 0)
        iec_lay.setSpacing(4)

        self._wf_leds = []
        self._wf_desc_lbls = []
        self._wf_time_lbls = []
        iec_lay.addWidget(_step_widget(
            self._WF_STEPS, self._wf_leds, 65, self._wf_desc_lbls, self._wf_time_lbls))

        # separator
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        theme.style(_sep, lambda: f"color:{theme.BORDER}; margin:2px 0;")
        iec_lay.addWidget(_sep)

        # Advanced Parameters Group
        from PySide6.QtWidgets import QGroupBox
        self.grp_advanced = QGroupBox("Advanced Test Parameters")
        self.grp_advanced.setEnabled(False)
        theme.style(self.grp_advanced, lambda: f"QGroupBox {{ color:{theme.MUTED}; font-weight:bold; font-size:11px; border:1px solid {theme.BORDER}; border-radius:4px; margin-top:6px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 3px 0 3px; }}")
        adv_lay = QVBoxLayout(self.grp_advanced)
        adv_lay.setContentsMargins(8, 12, 8, 8)
        adv_lay.setSpacing(4)
        
        # Charge mode
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Charge mode:"))
        self.cb_charge_mode = QComboBox()
        self.cb_charge_mode.addItems(["Auto (by chemistry)", "CC-CV", "3-Stage (Lead-Acid)"])
        self.cb_charge_mode.setToolTip(
            "Auto — ใช้ strategy ของเคมีแบต  |  CC-CV — Lithium  |  3-Stage — Lead-Acid"
        )
        mode_row.addWidget(self.cb_charge_mode, 1)
        adv_lay.addLayout(mode_row)

        # Charge C-rate
        crate_row = QHBoxLayout()
        crate_row.addWidget(QLabel("Charge C-rate:"))
        self.cb_seq_crate = QComboBox()
        self.cb_seq_crate.addItems(["0.05C", "0.1C", "0.2C", "0.3C", "0.5C", "1.0C"])
        self.cb_seq_crate.setCurrentText("0.5C")
        self.lbl_seq_crate_a = QLabel("— A")
        self.lbl_seq_crate_a.setStyleSheet(
            f"color:{theme.INFO}; font-weight:700; font-size:11px;"
        )
        crate_row.addWidget(self.cb_seq_crate)
        crate_row.addWidget(self.lbl_seq_crate_a)
        crate_row.addStretch(1)
        adv_lay.addLayout(crate_row)
        self.cb_seq_crate.currentTextChanged.connect(self._on_seq_crate_changed)

        # Stage breakdown
        self.lbl_charge_crate = QLabel("Charge rate: — (เลือกแบตก่อน)")
        self.lbl_charge_crate.setStyleSheet(
            f"color:{theme.MUTED}; font-size:10px; padding-left:24px; padding-bottom:2px;"
        )
        self.lbl_charge_crate.setWordWrap(True)
        adv_lay.addWidget(self.lbl_charge_crate)

        # REST duration
        rest_row = QHBoxLayout()
        rest_row.addWidget(QLabel("REST duration:"))
        self.spn_rest_min = QSpinBox()
        self.spn_rest_min.setRange(5, 120)
        self.spn_rest_min.setValue(30)
        self.spn_rest_min.setSingleStep(5)
        self.spn_rest_min.setSuffix(" min")
        self.spn_rest_min.setToolTip("เวลา rest หลังชาร์จ ก่อนเริ่ม discharge test (5–120 นาที)")
        self.spn_rest_min.valueChanged.connect(lambda _v: self._refresh_step_time_estimates())
        rest_row.addWidget(self.spn_rest_min)
        rest_row.addStretch(1)
        adv_lay.addLayout(rest_row)

        # Test discharge C-rate
        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("Test discharge:"))
        self.cb_test_crate = QComboBox()
        self.cb_test_crate.addItems(["0.1C", "0.2C", "0.5C", "1.0C"])
        self.cb_test_crate.setCurrentText("0.2C")
        self.cb_test_crate.setToolTip(
            "C-rate ของ discharge test — lead-acid: เลือก 0.1C (= I10 อัตราอ้างอิง "
            "EN 50342-1, ได้ Ce เทียบ Cn ตรงๆ ไม่พึ่ง Peukert) · lithium: 0.2C ตาม "
            "IEC 61960. อัตราอื่นยังเทสได้แต่ผลจะติดป้าย non-standard")
        self.lbl_test_crate_a = QLabel("— A")
        self.lbl_test_crate_a.setStyleSheet(
            f"color:{theme.INFO}; font-weight:700; font-size:11px;"
        )
        test_row.addWidget(self.cb_test_crate)
        test_row.addWidget(self.lbl_test_crate_a)
        test_row.addStretch(1)
        adv_lay.addLayout(test_row)
        self.cb_test_crate.currentTextChanged.connect(self._on_test_crate_changed)

        # Skip-phase toggles
        skip_row = QHBoxLayout()
        self.chk_skip_charge = QCheckBox("Skip charge")
        self.chk_skip_charge.setToolTip("Force-skip CHARGE phase (use if battery is already full)")
        self.chk_skip_rest = QCheckBox("Skip REST")
        self.chk_skip_rest.setToolTip("Skip the post-charge rest period (faster, less accurate)")
        skip_row.addWidget(self.chk_skip_charge)
        skip_row.addWidget(self.chk_skip_rest)
        skip_row.addStretch(1)
        adv_lay.addLayout(skip_row)

        # SoC charge threshold
        soc_row = QHBoxLayout()
        soc_row.addWidget(QLabel("Charge if SoC <"))
        self.spn_soc_threshold = QSpinBox()
        self.spn_soc_threshold.setRange(50, 99)
        self.spn_soc_threshold.setValue(95)
        self.spn_soc_threshold.setSuffix(" %")
        self.spn_soc_threshold.setToolTip("Skip CHARGE when battery SoC is at or above this level")
        soc_row.addWidget(self.spn_soc_threshold)
        soc_row.addStretch(1)
        adv_lay.addLayout(soc_row)
        
        iec_lay.addWidget(self.grp_advanced)
        
        # Start button

        self.btn_auto_seq = _btn("▶  AUTO SEQUENCE", bg="INFO", fg="white", hover="#0d4a89")
        self.btn_auto_seq.setToolTip(
            "IEC 61960: OCV → Charge → Rest → Discharge → Analyze"
        )
        self.btn_auto_seq.clicked.connect(self._on_auto_sequence)
        self._buttons["btn_auto_seq"] = self.btn_auto_seq
        iec_lay.addWidget(self.btn_auto_seq)
        iec_lay.addStretch(1)

        # ── Page 1: Quick Scan ───────────────────────────────
        qs_page = QWidget()
        qs_lay = QVBoxLayout(qs_page)
        qs_lay.setContentsMargins(0, 0, 0, 0)
        qs_lay.setSpacing(4)

        self._qs_leds = []
        self._qs_desc_lbls = []
        self._qs_time_lbls = []
        qs_lay.addWidget(_step_widget(
            self._QS_STEPS, self._qs_leds, 75, self._qs_desc_lbls, self._qs_time_lbls))

        self.btn_quick_scan = _btn("⚡  QUICK SCAN", bg="#e67e22", fg="white", hover="#c0392b")
        self.btn_quick_scan.setToolTip("OCV → Rest 5 min → Discharge 1C → Analyze (~1.5h)")
        self.btn_quick_scan.clicked.connect(self._on_quick_scan)
        self._buttons["btn_quick_scan"] = self.btn_quick_scan
        qs_lay.addWidget(self.btn_quick_scan)
        qs_lay.addStretch(1)

        # ── Page 2: HPPC Full Sequence ──────────────────────────
        hppc_seq_page = QWidget()
        hppc_seq_lay = QVBoxLayout(hppc_seq_page)
        hppc_seq_lay.setContentsMargins(0, 0, 0, 0)
        hppc_seq_lay.setSpacing(4)

        self._hppc_seq_leds = []
        self._hppc_seq_time_lbls = []
        hppc_seq_lay.addWidget(_step_widget(
            self._HPPC_SEQ_STEPS, self._hppc_seq_leds, 65,
            time_list=self._hppc_seq_time_lbls))

        hppc_seq_sep = QFrame()
        hppc_seq_sep.setFrameShape(QFrame.Shape.HLine)
        theme.style(hppc_seq_sep, lambda: f"color:{theme.BORDER}; margin:2px 0;")
        hppc_seq_lay.addWidget(hppc_seq_sep)

        hppc_seq_form = QFormLayout()
        hppc_seq_form.setSpacing(4)
        hppc_seq_form.setContentsMargins(0, 0, 0, 0)
        # Same setting as MANUAL → HPPC tab's "Pulse C-rate" field (ed_hppc_crate) —
        # kept in sync both ways (see _hppc_page) so users running the AUTO full
        # sequence don't have to switch tabs just to tune the pulse current before
        # a run (a too-high default here was tripping the under-voltage floor mid-HPPC).
        self.ed_hppc_seq_crate = QLineEdit("1.0")
        self.ed_hppc_seq_crate.setValidator(QDoubleValidator(0.1, 10.0, 2))
        self.ed_hppc_seq_crate.setToolTip(
            "Pulse C-rate × rated capacity = pulse current (A)\n"
            "Clamped to max_discharge_a from the active profile.\n"
            "Synced with MANUAL → HPPC tab."
        )
        hppc_seq_form.addRow("Pulse C-rate:", self.ed_hppc_seq_crate)
        self.spn_hppc_cycles = QSpinBox()
        self.spn_hppc_cycles.setRange(1, 20)
        self.spn_hppc_cycles.setValue(5)
        self.spn_hppc_cycles.setToolTip(
            "Number of pulse/relax cycles — more cycles = better R1/C1 statistics")
        self.spn_hppc_cycles.valueChanged.connect(
            lambda _v: self._refresh_step_time_estimates())
        hppc_seq_form.addRow("HPPC cycles:", self.spn_hppc_cycles)
        hppc_seq_lay.addLayout(hppc_seq_form)

        hppc_seq_note = QLabel("Pulse/relax duration from MANUAL → HPPC tab")
        theme.style(hppc_seq_note, lambda: f"color:{theme.MUTED}; font-size:10px;")
        hppc_seq_lay.addWidget(hppc_seq_note)

        self.btn_hppc_seq = _btn("▶  HPPC SEQUENCE", bg="#7b2d8b", fg="white", hover="#5c2068")
        self.btn_hppc_seq.setToolTip(
            "Charge → Rest 30min → HPPC N cycles → Analyze ECM (R0/R1/C1/τ)")
        self.btn_hppc_seq.clicked.connect(self._on_hppc_sequence)
        self._buttons["btn_hppc_seq"] = self.btn_hppc_seq
        hppc_seq_lay.addWidget(self.btn_hppc_seq)
        hppc_seq_lay.addStretch(1)

        # ── Page 3: Cycle Life ───────────────────────────────
        cycle_page = QWidget()
        cycle_lay = QVBoxLayout(cycle_page)
        cycle_lay.setContentsMargins(0, 0, 0, 0)
        cycle_lay.setSpacing(4)

        self._cycle_leds = []
        self._cycle_time_lbls = []
        cycle_lay.addWidget(_step_widget(
            self._CYCLE_STEPS, self._cycle_leds, 75,
            time_list=self._cycle_time_lbls))

        cycle_sep = QFrame()
        cycle_sep.setFrameShape(QFrame.Shape.HLine)
        theme.style(cycle_sep, lambda: f"color:{theme.BORDER}; margin:2px 0;")
        cycle_lay.addWidget(cycle_sep)

        cycle_form = QFormLayout()
        cycle_form.setSpacing(4)
        cycle_form.setContentsMargins(0, 0, 0, 0)

        self.spn_cycle_n = QSpinBox()
        self.spn_cycle_n.setRange(1, 100)
        self.spn_cycle_n.setValue(3)
        self.spn_cycle_n.setToolTip("Total number of charge+discharge cycles")
        self.spn_cycle_n.valueChanged.connect(lambda _v: self._refresh_step_time_estimates())
        cycle_form.addRow("Cycles:", self.spn_cycle_n)

        self.cb_cycle_charge_crate = QComboBox()
        self.cb_cycle_charge_crate.addItems(["0.1C", "0.2C", "0.3C", "0.5C", "1.0C"])
        self.cb_cycle_charge_crate.setCurrentText("0.3C")
        self.cb_cycle_charge_crate.currentTextChanged.connect(
            lambda _t: self._refresh_step_time_estimates())
        cycle_form.addRow("Charge C-rate:", self.cb_cycle_charge_crate)

        self.cb_cycle_dis_crate = QComboBox()
        self.cb_cycle_dis_crate.addItems(["0.1C", "0.2C", "0.5C", "1.0C"])
        self.cb_cycle_dis_crate.setCurrentText("0.2C")
        self.cb_cycle_dis_crate.currentTextChanged.connect(
            lambda _t: self._refresh_step_time_estimates())
        cycle_form.addRow("Discharge C-rate:", self.cb_cycle_dis_crate)

        self.spn_cycle_rest = QSpinBox()
        self.spn_cycle_rest.setRange(1, 60)
        self.spn_cycle_rest.setValue(5)
        self.spn_cycle_rest.setSuffix(" min")
        self.spn_cycle_rest.setToolTip("Rest between charge and discharge in each cycle")
        self.spn_cycle_rest.valueChanged.connect(lambda _v: self._refresh_step_time_estimates())
        cycle_form.addRow("Rest/cycle:", self.spn_cycle_rest)
        cycle_lay.addLayout(cycle_form)

        self.lbl_cycle_counter = QLabel("Cycle: —")
        theme.style(self.lbl_cycle_counter, lambda: f"color:{theme.INFO}; font-weight:700; font-size:11px;")
        cycle_lay.addWidget(self.lbl_cycle_counter)

        self.btn_cycle_life = _btn("▶  CYCLE LIFE TEST", bg="#6c3483", fg="white", hover="#4a235a")
        self.btn_cycle_life.setToolTip(
            "Automated N×(Charge→Rest→Discharge) — logs capacity fade per cycle")
        self.btn_cycle_life.clicked.connect(self._on_cycle_life)
        self._buttons["btn_cycle_life"] = self.btn_cycle_life
        cycle_lay.addWidget(self.btn_cycle_life)
        cycle_lay.addStretch(1)

        self._wf_stack.addWidget(iec_page)       # index 0
        self._wf_stack.addWidget(qs_page)        # index 1
        self._wf_stack.addWidget(hppc_seq_page)  # index 2
        self._wf_stack.addWidget(cycle_page)     # index 3
        outer_lay.addWidget(self._wf_stack)

        # ให้ stack สูงตามหน้าที่กำลังแสดง — หน้าที่ซ่อนไม่ดันความสูง (กันพื้นที่ว่าง
        # ใต้ Quick Scan ซึ่งเตี้ยกว่าหน้า IEC)
        for _i in range(self._wf_stack.count()):
            _pg = self._wf_stack.widget(_i)
            _pg.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self._wf_stack.currentChanged.connect(self._on_wf_stack_changed)
        self._on_wf_stack_changed(self._wf_stack.currentIndex())

        # NOT a direct setCurrentIndex connect: item 4 (EN 50342-1) shares the
        # IEC page (index 0) and applies standard presets — see the slot in
        # SequencesMixin.
        self.cb_workflow_type.currentIndexChanged.connect(self._on_workflow_type_changed)

        # ── Shared CANCEL + status ────────────────────────────
        self.btn_seq_cancel = _btn("■  CANCEL", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_seq_cancel.setEnabled(False)
        self.btn_seq_cancel.clicked.connect(self._on_seq_cancel)
        self._buttons["btn_seq_cancel"] = self.btn_seq_cancel
        outer_lay.addWidget(self.btn_seq_cancel)

        self.lbl_wf_status = QLabel("เลือก workflow แล้วกดปุ่ม RUN")
        self.lbl_wf_status.setStyleSheet(
            f"color:{theme.MUTED}; font-size:11px; padding-top:2px;"
        )
        self.lbl_wf_status.setWordWrap(True)
        outer_lay.addWidget(self.lbl_wf_status)

        # Phase progress bar + ETA
        self.wf_progress = QProgressBar()
        self.wf_progress.setRange(0, 100)
        self.wf_progress.setValue(0)
        self.wf_progress.setTextVisible(True)
        self.wf_progress.setFormat("%p%  (%v / %m s)")
        self.wf_progress.setMaximumHeight(14)
        self.wf_progress.setStyleSheet(
            f"QProgressBar{{border:1px solid {theme.BORDER};border-radius:3px;"
            f"background:{theme.PANEL2};text-align:center;font-size:9px;}}"
            f"QProgressBar::chunk{{background:{theme.INFO};border-radius:2px;}}"
        )
        self.wf_progress.hide()
        outer_lay.addWidget(self.wf_progress)

        self.lbl_eta = QLabel("")
        theme.style(self.lbl_eta, lambda: f"color:{theme.INFO}; font-size:10px; font-weight:600;")
        self.lbl_eta.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_eta.hide()
        outer_lay.addWidget(self.lbl_eta)

        # Inline result card — shown after sequence completes
        self.frm_seq_result = QFrame()
        self.frm_seq_result.setStyleSheet(
            f"QFrame{{background:{theme.PANEL2};border:1px solid {theme.INFO};"
            f"border-radius:5px;padding:4px 8px;}}"
        )
        result_lay = QVBoxLayout(self.frm_seq_result)
        result_lay.setContentsMargins(4, 4, 4, 4)
        result_lay.setSpacing(2)
        self.lbl_seq_result = QLabel("—")
        theme.style(self.lbl_seq_result, lambda: f"color:{theme.TEXT}; font-size:11px; font-weight:600;")
        self.lbl_seq_result.setWordWrap(True)
        result_lay.addWidget(self.lbl_seq_result)
        self.btn_seq_export = _btn("EXPORT PDF", bg="INFO", fg="white", hover="#0d4a89")
        self.btn_seq_export.clicked.connect(self._on_seq_export)
        result_lay.addWidget(self.btn_seq_export)
        self.frm_seq_result.hide()
        outer_lay.addWidget(self.frm_seq_result)

        return outer

    def _on_seq_export(self):
        if hasattr(self, '_on_pdf_report'):
            self._on_pdf_report()

    def _on_wf_stack_changed(self, idx: int):
        """ปรับให้เฉพาะหน้าที่กำลังแสดงดันความสูงของ stack — หน้าที่ซ่อนตั้งเป็น
        Ignored เพื่อไม่ให้หน้า IEC (สูงกว่า) ทิ้งช่องว่างใต้หน้า Quick Scan."""
        for i in range(self._wf_stack.count()):
            page = self._wf_stack.widget(i)
            policy = page.sizePolicy()
            policy.setVerticalPolicy(
                QSizePolicy.Policy.Preferred if i == idx else QSizePolicy.Policy.Ignored
            )
            page.setSizePolicy(policy)
        self._wf_stack.adjustSize()

    # ---- ZONE 2: RUN (charge ⇄ discharge) ----------------------------------
    def _zone_run(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Operation toggle — swaps the controls below between Charge / Discharge / HPPC / Direct.
        trow = QHBoxLayout()
        self.rb_charge    = QRadioButton("Charge")
        self.rb_discharge = QRadioButton("Discharge")
        self.rb_hppc      = QRadioButton("HPPC")
        self.rb_direct    = QRadioButton("Direct")
        self.rb_direct.setToolTip("ควบคุม PSU / Load โดยตรง (manual voltage/current)")
        self.rb_discharge.setChecked(True)
        grp = QButtonGroup(self)
        for rb in (self.rb_charge, self.rb_discharge, self.rb_hppc, self.rb_direct):
            grp.addButton(rb)
            trow.addWidget(rb)
        trow.addStretch(1)
        lay.addLayout(trow)

        self.run_stack = QStackedWidget()
        self.run_stack.addWidget(self._charge_page())      # index 0
        self.run_stack.addWidget(self._discharge_page())   # index 1
        self.run_stack.addWidget(self._hppc_page())        # index 2
        self.run_stack.addWidget(self._direct_page())      # index 3
        self.run_stack.setCurrentIndex(1)
        self.rb_charge.toggled.connect(   lambda on: on and self.run_stack.setCurrentIndex(0))
        self.rb_discharge.toggled.connect(lambda on: on and self.run_stack.setCurrentIndex(1))
        self.rb_hppc.toggled.connect(     lambda on: on and self.run_stack.setCurrentIndex(2))
        self.rb_direct.toggled.connect(   self._on_direct_toggled)
        lay.addWidget(self.run_stack)

        # Last grade echo (the full breakdown stays in the Analytics tab).
        self.lbl_run_grade = QLabel("Grade: —")
        theme.style(self.lbl_run_grade, lambda: f"color:{theme.MUTED}; padding-top:4px;")
        lay.addWidget(self.lbl_run_grade)
        lay.addStretch(1)
        return w

    def _direct_page(self):
        """Direct hardware control — PSU voltage/current and e-load current."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)

        lay.addWidget(self._subheader("PSU"))
        psu_row = QHBoxLayout()
        psu_row.addWidget(QLabel("V:"))
        self.ed_psu_v = QLineEdit("13.8")
        self.ed_psu_v.setMaximumWidth(72)
        self.ed_psu_v.setToolTip("Output voltage (V)")
        psu_row.addWidget(self.ed_psu_v)
        psu_row.addWidget(QLabel("A:"))
        self.ed_psu_i = QLineEdit("1.0")
        self.ed_psu_i.setMaximumWidth(56)
        self.ed_psu_i.setValidator(QDoubleValidator(0.0, 40.0, 2))
        self.ed_psu_i.setToolTip("CC current limit (A)")
        psu_row.addWidget(self.ed_psu_i)
        psu_on  = _btn("ON",  bg="OK",       fg="white", hover="#266a2a")
        psu_off = _btn("OFF", bg="PANEL", hover="PANEL2")
        psu_on.clicked.connect( lambda: self._psu_manual(True))
        psu_off.clicked.connect(lambda: self._psu_manual(False))
        psu_row.addWidget(psu_on)
        psu_row.addWidget(psu_off)
        lay.addLayout(psu_row)

        lay.addWidget(self._subheader("E-LOAD"))
        load_row = QHBoxLayout()
        load_row.addWidget(QLabel("A:"))
        self.ed_load_a = QLineEdit("0.7")
        self.ed_load_a.setMaximumWidth(72)
        self.ed_load_a.setToolTip("CC load current (A)")
        load_row.addWidget(self.ed_load_a)
        load_on  = _btn("ON",  bg="OK",       fg="white", hover="#266a2a")
        load_off = _btn("OFF", bg="PANEL", hover="PANEL2")
        load_on.clicked.connect( lambda: self._load_manual(True))
        load_off.clicked.connect(lambda: self._load_manual(False))
        load_row.addWidget(load_on)
        load_row.addWidget(load_off)
        lay.addLayout(load_row)

        # Hardware OVP/OCP/OTP protection is set automatically on every Connect
        # (see _on_connect) — a trip means something real happened, so clearing it
        # is a deliberate operator action here, never auto-retried by software.
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("PSU PROTECTION"))
        prot_row = QHBoxLayout()
        self.lbl_psu_trip = QLabel("Trip: —")
        theme.style(self.lbl_psu_trip, self._psu_trip_style)
        prot_row.addWidget(self.lbl_psu_trip, 1)
        self.btn_check_trip = _btn("Check", bg="PANEL", hover="PANEL2")
        self.btn_clear_trip = _btn("Clear Trip", bg="WARN", fg="white", hover="#a06800")
        self.btn_check_trip.clicked.connect(self._on_check_psu_trip)
        self.btn_clear_trip.clicked.connect(self._on_clear_psu_trip)
        prot_row.addWidget(self.btn_check_trip)
        prot_row.addWidget(self.btn_clear_trip)
        lay.addLayout(prot_row)

        note = QLabel("⚠  ใช้เฉพาะทดสอบฮาร์ดแวร์  —  ไม่มี SoC หรือ safety interlock")
        theme.style(note, lambda: f"color:{theme.WARN}; font-size:10px;")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return w

    def _charge_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        # OCV calibration row — press before CHARGE to read resting voltage and anchor SoC
        ocv_row = QHBoxLayout()
        self.btn_ocv = _btn("OCV CALIBRATE", bg="WARN", fg="white", hover="#a06800")
        self.btn_ocv.setToolTip(
            "Turn off PSU & Load, wait 3 s, read OCV → set correct SoC.\n"
            "Press before CHARGE to fix the SOC display."
        )
        self.btn_ocv.clicked.connect(self._on_ocv_calibrate)
        self._buttons["btn_ocv"] = self.btn_ocv   # register for sig_loading
        ocv_row.addWidget(self.btn_ocv)
        lay.addLayout(ocv_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.cb_manual_charge_mode = QComboBox()
        self.cb_manual_charge_mode.addItems(["Auto (by chemistry)", "CC-CV", "3-Stage (Lead-Acid)"])
        mode_row.addWidget(self.cb_manual_charge_mode, 1)
        lay.addLayout(mode_row)

        crate_row = QHBoxLayout()
        crate_row.addWidget(QLabel("C-rate:"))
        self.cb_manual_charge_crate = QComboBox()
        self.cb_manual_charge_crate.addItems(["0.1C", "0.2C", "0.3C", "0.5C", "1.0C"])
        self.cb_manual_charge_crate.setEditable(True)
        self.cb_manual_charge_crate.setMinimumWidth(70)
        self.cb_manual_charge_crate.setCurrentText("0.5C")
        crate_row.addWidget(self.cb_manual_charge_crate)
        crate_row.addStretch(1)
        lay.addLayout(crate_row)

        crow = QHBoxLayout()
        self.btn_charge = _btn("CHARGE", bg="OK", fg="white", hover="#266a2a")
        self.btn_stop_charge = _btn("STOP", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_charge.clicked.connect(self._on_charge)
        self.btn_stop_charge.clicked.connect(self._on_stop_charge)
        crow.addWidget(self.btn_charge, 2)
        crow.addWidget(self.btn_stop_charge, 1)
        lay.addLayout(crow)
        self.lbl_charge = QLabel("Charge idle")
        theme.style(self.lbl_charge, lambda: f"color:{theme.MUTED};")
        lay.addWidget(self.lbl_charge)
        return w

    def _discharge_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Discharge mode:"))
        self.cb_op_mode = QComboBox()
        self.cb_op_mode.addItems([m.value for m in OperationMode
                                  if m not in (OperationMode.CC_CV_CHARGE,
                                               OperationMode.HPPC)])
        trow.addWidget(self.cb_op_mode, 1)
        lay.addLayout(trow)

        dcrate_row = QHBoxLayout()
        dcrate_row.addWidget(QLabel("C-rate:"))
        self.cb_manual_discharge_crate = QComboBox()
        self.cb_manual_discharge_crate.addItems(["0.1C", "0.2C", "0.5C", "1.0C"])
        self.cb_manual_discharge_crate.setEditable(True)
        self.cb_manual_discharge_crate.setMinimumWidth(70)
        self.cb_manual_discharge_crate.setCurrentText("0.2C")
        dcrate_row.addWidget(self.cb_manual_discharge_crate)
        dcrate_row.addStretch(1)
        lay.addLayout(dcrate_row)

        cutoff_row = QHBoxLayout()
        cutoff_row.addWidget(QLabel("Cutoff V:"))
        self.ed_manual_cutoff_v = QLineEdit()
        self.ed_manual_cutoff_v.setPlaceholderText("Auto (Profile)")
        cutoff_row.addWidget(self.ed_manual_cutoff_v)
        cutoff_row.addStretch(1)
        lay.addLayout(cutoff_row)

        crow2 = QHBoxLayout()
        self.btn_run_test = _btn("RUN TEST", bg="INFO", fg="white", hover="#0d4a89")
        self.btn_run_test.clicked.connect(self._on_run_test)
        self.btn_stop_test = _btn("STOP", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_stop_test.clicked.connect(self._on_stop_test)
        crow2.addWidget(self.btn_run_test, 2)
        crow2.addWidget(self.btn_stop_test, 1)
        lay.addLayout(crow2)
        self.lbl_test_status = QLabel("Test idle")
        theme.style(self.lbl_test_status, lambda: f"color:{theme.MUTED};")
        lay.addWidget(self.lbl_test_status)
        return w

    def _hppc_page(self):
        """Dedicated HPPC Pulse Test page — pulse/relax sequencer with phase indicator."""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Capability note
        note = QLabel(
            "R₀ from 250 ms voltage step · R₁/C₁ from RC-tail fit · "
            "Max observable: ~2 Hz  (SCPI limit) · pulse ≥ 3×τ recommended"
        )
        theme.style(note, lambda: f"color:{theme.MUTED}; font-size:10px;")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addWidget(_hline())

        form = QFormLayout()
        form.setSpacing(4)
        form.setContentsMargins(0, 0, 0, 0)

        self.ed_hppc_crate = QLineEdit("1.0")
        self.ed_hppc_crate.setValidator(QDoubleValidator(0.1, 10.0, 2))
        self.ed_hppc_crate.setToolTip(
            "Pulse C-rate × rated capacity = pulse current (A)\n"
            "Clamped to max_discharge_a from the active profile."
        )
        form.addRow("Pulse C-rate:", self.ed_hppc_crate)
        # Two-way sync with the AUTO tab's HPPC Full Sequence "Pulse C-rate" field —
        # one logical setting, editable from either tab. setText() is a no-op (and
        # emits nothing) when the text is already equal, so this can't loop forever.
        self.ed_hppc_crate.textChanged.connect(self.ed_hppc_seq_crate.setText)
        self.ed_hppc_seq_crate.textChanged.connect(self.ed_hppc_crate.setText)

        self.ed_hppc_pulse = QLineEdit("30")
        self.ed_hppc_pulse.setValidator(QDoubleValidator(1.0, 600.0, 1))
        self.ed_hppc_pulse.setToolTip(
            "Pulse duration (s)\nLead-acid τ ≈ 10–60 s → use ≥ 30 s to resolve R₁/C₁"
        )
        form.addRow("Pulse (s):", self.ed_hppc_pulse)

        # Lead-acid needs much longer than 30 s to stop climbing after a discharge
        # pulse ends — real rig data: terminal voltage still rising ~10-20 mV per
        # few seconds at t=30 s post-pulse (0.30 V total climb in 30 s, nowhere near
        # flat). A too-short relax window measures R0/R1 against a "rest" voltage
        # that's still mid-recovery, biasing every pulse but the first (which usually
        # gets a long pre-test rest instead). 180 s doesn't fully settle it either
        # (that took ~27 min after charge) but meaningfully reduces the bias; raise
        # further (up to 600 s) if precision matters more than test duration.
        # Canonical chemistry resolution (same registry en50342_capacity_conditions()
        # and _cca_cutoff_v() use), not an ad-hoc substring match on the raw
        # battery_type string — an alias not containing "lead" would otherwise be
        # silently misclassified as fast-relaxing lithium.
        is_lead_acid = battery_profiles.get_chemistry(
            self.config.battery.battery_type).name == "LeadAcid"
        default_relax = "180" if is_lead_acid else "30"
        self.ed_hppc_relax = QLineEdit(default_relax)
        self.ed_hppc_relax.setValidator(QDoubleValidator(1.0, 600.0, 1))
        self.ed_hppc_relax.setToolTip(
            "Rest/relaxation duration (s) between pulses\n"
            "Lead-acid relaxes slowly after a pulse (still ~10-20 mV/few-s climbing "
            "at 30 s on real data) — 180 s default reduces R0/R1 bias on pulses "
            "after the first; lithium settles fast, 30 s is fine."
        )
        form.addRow("Relax (s):", self.ed_hppc_relax)

        lay.addLayout(form)

        # Phase indicator — updates live via _on_hppc_telemetry
        self.lbl_hppc_phase = QLabel("IDLE")
        self.lbl_hppc_phase.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_hppc_phase.setStyleSheet(
            f"background:{theme.PANEL2}; color:{theme.MUTED}; border:1px solid {theme.BORDER}; "
            f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
        )
        lay.addWidget(self.lbl_hppc_phase)

        brow = QHBoxLayout()
        self.btn_run_hppc = _btn("RUN HPPC", bg="INFO", fg="white", hover="#0d4a89")
        self.btn_run_hppc.clicked.connect(self._on_run_hppc)
        self._buttons["btn_run_hppc"] = self.btn_run_hppc
        self.btn_stop_hppc = _btn("STOP", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_stop_hppc.clicked.connect(self._on_stop_test)
        brow.addWidget(self.btn_run_hppc, 2)
        brow.addWidget(self.btn_stop_hppc, 1)
        lay.addLayout(brow)

        lay.addStretch(1)
        return w


    # ---- ZONE: TEST MODE — AUTO tab (workflow) + MANUAL tab (charge/discharge) --
    def _zone_test_mode(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        theme.style(tabs, self._tabbar_style)
        tabs.addTab(self._zone_workflow(),     "AUTO")
        tabs.addTab(self._zone_run(),          "MANUAL")
        tabs.addTab(self._zone_characterize(), "CHARACTERIZE")

        # Make each tab page shrink to its own content height instead of
        # reserving the height of the tallest page at all times.
        for i in range(tabs.count()):
            p = tabs.widget(i)
            sp = p.sizePolicy()
            sp.setVerticalPolicy(QSizePolicy.Policy.Ignored)
            p.setSizePolicy(sp)

        def _sync_height(idx):
            for i in range(tabs.count()):
                p = tabs.widget(i)
                sp = p.sizePolicy()
                sp.setVerticalPolicy(
                    QSizePolicy.Policy.Preferred if i == idx
                    else QSizePolicy.Policy.Ignored)
                p.setSizePolicy(sp)
            # defer adjustSize so we don't trigger a layout feedback loop
            QTimer.singleShot(0, tabs.adjustSize)

        tabs.currentChanged.connect(_sync_height)
        _sync_height(tabs.currentIndex())

        return tabs

    # ---- ZONE 3: TOOLS (data / reporting) ----------------------------------
    def _zone_tools(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        # Hidden monitor buttons — triggered by toolbar actions, not shown directly.
        self.btn_start_monitor = _btn("START MONITOR", bg="OK", fg="white", hover="#266a2a")
        self.btn_stop_monitor  = _btn("STOP", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_start_monitor.clicked.connect(self._on_start_monitor)
        self.btn_stop_monitor.clicked.connect(
            lambda: self.controller and self.controller.stop_monitor())
        self._buttons["btn_start_monitor"] = self.btn_start_monitor
        self.btn_start_monitor.hide()
        self.btn_stop_monitor.hide()

        # ── Data / Reporting ────────────────────────────────────────────────
        lay.addWidget(self._subheader("DATA"))
        self.lbl_csv = QLabel("CSV: —")
        theme.style(self.lbl_csv, lambda: f"color:{theme.MUTED}; font-size:11px;")
        self.lbl_csv.setWordWrap(True)
        lay.addWidget(self.lbl_csv)

        self.btn_open_logs = _btn("Open Logs Folder", bg="PANEL", hover="PANEL2")
        self.btn_open_logs.clicked.connect(self._on_open_logs_folder)
        lay.addWidget(self.btn_open_logs)
        self.btn_pdf = _btn("Generate PDF Report", bg="PANEL", hover="PANEL2")
        self.btn_pdf.clicked.connect(self._on_pdf_report)
        lay.addWidget(self.btn_pdf)
        btn_dash = _btn("Open Cloud Dashboard", bg="PANEL", hover="PANEL2")
        btn_dash.clicked.connect(self._on_open_dashboard)
        lay.addWidget(btn_dash)

        lay.addWidget(_hline())
        lay.addWidget(self._subheader("BATTERY CONFIGURATION"))
        self.btn_edit_profile = _btn("Edit Battery Profile…", bg="PANEL", hover="PANEL2")
        self.btn_edit_profile.setToolTip("แก้ไขค่า BatteryConfig ในแอพโดยตรง")
        self.btn_edit_profile.clicked.connect(self._on_edit_battery_profile)
        self.btn_edit_profile.setVisible(False)
        lay.addWidget(self.btn_edit_profile)
        
        # Hardware Calibration Group
        from PySide6.QtWidgets import QGroupBox, QDoubleSpinBox, QFormLayout
        self.grp_calibration = QGroupBox("Hardware Calibration (Offsets)")
        self.grp_calibration.setVisible(False)
        theme.style(self.grp_calibration, lambda: f"QGroupBox {{ color:{theme.MUTED}; font-weight:bold; font-size:11px; border:1px solid {theme.BORDER}; border-radius:4px; margin-top:6px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 3px 0 3px; }}")
        cal_lay = QFormLayout(self.grp_calibration)
        cal_lay.setContentsMargins(8, 12, 8, 8)
        
        self.spn_psu_v_offset = QDoubleSpinBox()
        self.spn_psu_v_offset.setRange(-1.0, 1.0)
        self.spn_psu_v_offset.setSingleStep(0.01)
        self.spn_psu_v_offset.setDecimals(3)
        
        self.spn_psu_i_offset = QDoubleSpinBox()
        self.spn_psu_i_offset.setRange(-1.0, 1.0)
        self.spn_psu_i_offset.setSingleStep(0.01)
        self.spn_psu_i_offset.setDecimals(3)
        
        self.spn_load_v_offset = QDoubleSpinBox()
        self.spn_load_v_offset.setRange(-1.0, 1.0)
        self.spn_load_v_offset.setSingleStep(0.01)
        self.spn_load_v_offset.setDecimals(3)
        
        self.spn_load_i_offset = QDoubleSpinBox()
        self.spn_load_i_offset.setRange(-1.0, 1.0)
        self.spn_load_i_offset.setSingleStep(0.01)
        self.spn_load_i_offset.setDecimals(3)
        
        cal_lay.addRow("PSU Voltage Offset (V):", self.spn_psu_v_offset)
        cal_lay.addRow("PSU Current Offset (A):", self.spn_psu_i_offset)
        cal_lay.addRow("Load Voltage Offset (V):", self.spn_load_v_offset)
        cal_lay.addRow("Load Current Offset (A):", self.spn_load_i_offset)
        
        self.btn_save_cal = _btn("Save Calibration", bg="PANEL", hover="PANEL2")
        self.btn_save_cal.clicked.connect(self._on_save_calibration)
        cal_lay.addRow(self.btn_save_cal)
        
        lay.addWidget(self.grp_calibration)
        
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("SSR POWER RELAY (GPIO16)"))
        ssr_row = QHBoxLayout()
        lbl_ssr = QLabel("Relay:")
        lbl_ssr.setMinimumWidth(78)
        ssr_row.addWidget(lbl_ssr)
        
        def _led():
            lbl = QLabel("●")
            lbl.setStyleSheet(f"color:{theme.NEUTRAL}; font-size:15px; min-width:18px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return lbl

        self.led_ssr = _led()
        ssr_row.addWidget(self.led_ssr)
        self.lbl_ssr_state = QLabel("—")
        self.lbl_ssr_state.setStyleSheet(f"color:{theme.MUTED}; font-weight:600;")
        ssr_row.addWidget(self.lbl_ssr_state)
        ssr_row.addStretch(1)
        lay.addLayout(ssr_row)
        ssr_btn_row = QHBoxLayout()
        self.btn_ssr_on = _btn("Manual ON", bg="OK", fg="white", hover="#266a2a")
        self.btn_ssr_off = _btn("Manual OFF", bg="CRIT", fg="white", hover="#9b2020")
        self.btn_ssr_on.clicked.connect(self._on_ssr_manual_on)
        self.btn_ssr_off.clicked.connect(self._on_ssr_manual_off)
        self.btn_ssr_on.setEnabled(False)
        self.btn_ssr_off.setEnabled(False)
        self.btn_ssr_on.setVisible(False)
        self.btn_ssr_off.setVisible(False)
        ssr_btn_row.addWidget(self.btn_ssr_on)
        ssr_btn_row.addWidget(self.btn_ssr_off)
        lay.addLayout(ssr_btn_row)
        lbl_ssr_hint = QLabel(
            "ⓘ ตัดไฟ PSU/Load ทางกายภาพผ่าน SSR ที่ ESP32 GPIO16 — ปกติทำงานอัตโนมัติ: "
            "ON ทันทีที่เริ่มชาร์จ (ทุกโหมดเทสต์), OFF ทันทีที่หยุดชาร์จ/E-STOP\n"
            "ปุ่ม Manual ด้านบนไว้สั่งตรงสำหรับ diagnostic/recovery เท่านั้น "
            "(ต้องต่อ ESP32 ก่อนถึงจะกดได้) — ไม่ได้ไปเริ่ม/หยุดการทดสอบ")
        theme.style(lbl_ssr_hint, lambda: f"color:{theme.MUTED}; font-size:10px;")
        lbl_ssr_hint.setWordWrap(True)
        lay.addWidget(lbl_ssr_hint)

        lay.addWidget(_hline())
        lay.addWidget(self._subheader("CLOUD PUSH"))
        self.chk_cloud_push = QCheckBox("Enable cloud push")
        self.chk_cloud_push.setChecked(
            getattr(self.config.system, "cloud_push_enabled", False))
        self.chk_cloud_push.setToolTip(
            "ส่งข้อมูล V/I/SoC/Temp ไปยัง cloud endpoint ทุก push interval\n"
            "ตั้ง cloud_dashboard_url และ push interval ใน config.json")
        self.chk_cloud_push.stateChanged.connect(self._on_cloud_push_toggle)
        lay.addWidget(self.chk_cloud_push)
        cloud_url_row = QHBoxLayout()
        cloud_url_row.addWidget(QLabel("Endpoint URL:"))
        self.ed_cloud_url = QLineEdit(
            getattr(self.config.system, "cloud_dashboard_url", ""))
        self.ed_cloud_url.setPlaceholderText("https://...")
        self.ed_cloud_url.editingFinished.connect(self._on_cloud_url_changed)
        cloud_url_row.addWidget(self.ed_cloud_url, 1)
        lay.addLayout(cloud_url_row)

        lay.addWidget(_hline())
        lay.addWidget(self._subheader("APPEARANCE"))
        self.chk_dark_theme = QCheckBox("Dark theme")
        self.chk_dark_theme.setChecked(
            getattr(self.config.system, "ui_theme", "light") == "dark")
        self.chk_dark_theme.setToolTip("สลับโทนสีหน้าจอ — มีผลทันทีโดยไม่ต้องรีสตาร์ท")
        self.chk_dark_theme.stateChanged.connect(self._on_theme_toggle)
        lay.addWidget(self.chk_dark_theme)

        lay.addStretch(1)
        return w

    def _build_right_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(300)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        self._right_tabs = QTabWidget()
        theme.style(self._right_tabs, self._tabbar_style)
        # Alarm Log first — it's now the primary/default tab (operators should see
        # live safety events immediately, not the historical Analytics view) —
        # QTabWidget defaults to whichever tab is added first.
        self._right_tabs.addTab(self._tab_alarms(), "Alarm Log")
        self._right_tabs.addTab(self._tab_analytics(), "Analytics")
        self._right_tabs.addTab(self._tab_diagnostics(), "Diagnostics (ICA/DTV)")
        lay.addWidget(self._right_tabs, 1)
        return panel

    def _tab_diagnostics(self):
        """Post-test ICA dQ/dV curve (populated by the worker).

        DTV (dT/dV) was removed: at 12 V / low C-rate with a slow (~240 ms) IR sensor
        the battery's self-heating is too small/noisy to differentiate usefully on this
        rig — see docs/project_pivot.md."""
        w = QWidget()
        lay = QHBoxLayout(w)
        self.plot_ica = pg.PlotWidget()
        self.plot_ica.setBackground(theme.PANEL2)
        self.plot_ica.setLabel("bottom", "Voltage", units="V")
        self.plot_ica.setLabel("left", "dQ/dV")
        self.plot_ica.setTitle("ICA (Incremental Capacity)")
        lay.addWidget(self.plot_ica, 1)
        # Standalone pyqtgraph widget outside TrendContainer's own retheme() —
        # background would otherwise stay frozen at whichever theme was active
        # at construction (see the curve itself, plotted fresh from theme.INFO
        # each time a new analysis is shown, which needs no such hook).
        theme.on_retheme(lambda: self.plot_ica.setBackground(theme.PANEL2))
        return w

    @staticmethod
    def _tabbar_style():
        """Shared QTabWidget/QTabBar look for every tab group in the app
        (left-panel SETUP/TEST MODE/TOOLS, nested AUTO/MANUAL/CHARACTERIZE,
        right-panel Alarm Log/Analytics/Diagnostics) — qt-material's own
        default tab styling left unselected tabs with no background at all,
        reading as washed-out/near-invisible against the grey ISA-101 shell.
        The selected tab gets a raised PANEL2 look (same elevation language as
        _btn()'s hover state) instead of just a thin underline."""
        return (
            f"QTabWidget::pane {{ border:1px solid {theme.BORDER}; background:{theme.PANEL2}; }}"
            f"QTabBar::tab {{ background:{theme.PANEL}; color:{theme.MUTED}; padding:6px 14px; "
            f"border:1px solid {theme.BORDER}; border-bottom:0; }}"
            f"QTabBar::tab:selected {{ background:{theme.PANEL2}; color:{theme.TEXT}; font-weight:700; }}"
        )

    @staticmethod
    def _metric_accent_color(name):
        """Top-border accent per metric card — matches that metric's curve
        color in the main trend graph (widgets.py: Voltage=INFO, Current=WARN,
        Temp=CRIT) so the card row visually maps to the graph below it. SoC/Rin
        aren't plotted there, so they get a distinct unused semantic color
        instead (OK=green "charge state", NEUTRAL=gray "informational")."""
        return {
            "Voltage": theme.INFO, "Current": theme.WARN, "Temp": theme.CRIT,
            "SoC": theme.OK, "Rin": theme.NEUTRAL,
        }.get(name, theme.INFO)

    def _metric_card(self, name, unit, store=None):
        if store is None:
            store = self.metric_labels
        card = QFrame()
        theme.style(card, lambda: (
            f"QFrame {{ background:{theme.PANEL2}; border:1px solid {theme.BORDER}; "
            f"border-top:2px solid {self._metric_accent_color(name)}; border-radius:6px; }}"))
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 9, 12, 9)
        tooltips = {
            "Voltage": "Terminal Voltage (แรงดันไฟฟ้า)",
            "Current": "Charge/Discharge Current (กระแส)",
            "SoC": "State of Charge (เปอร์เซ็นต์แบตเตอรี่)",
            "Rin": "Internal Resistance (ความต้านทานภายใน)",
            "Temp": "Battery Case Temperature (อุณหภูมิ)"
        }
        if name in tooltips:
            card.setToolTip(tooltips[name])
        lay.setSpacing(2)
        t = QLabel(name.upper())
        theme.style(t, lambda: (
            f"color:{theme.MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;"))
        # SoH/Rin(final)/Grade are only ever set once a test's analysis completes
        # (never live) — start "pending" so a placeholder number is never mistaken
        # for a reading. Rin in the live row is only valid under load. Styled in
        # MUTED (not TEXT) while pending so a "—" placeholder is visually distinct
        # from an actual reading at a glance, not just distinguishable by re-reading
        # the digits — callers restore color:{TEXT} when they set a real value (see
        # _slot_display's Rin update). Registered via theme.style() as a baseline —
        # state-dependent callers (Current/Temp) re-apply their own color on top
        # after every retheme (see BatteryQtWindow._on_retheme()), but without this
        # baseline a value that's NEVER been touched since construction (Voltage,
        # SoC, or any pending placeholder) would keep showing whatever color was
        # picked under the theme active at startup — invisible if that happens to
        # land on white-on-white after a light/dark switch.
        is_pending = name in ("SoH", "Rin", "Grade")
        val = QLabel("—" if is_pending else f"0.0 {unit}")
        val.setFont(QFont("Consolas", 19, QFont.Weight.Bold))
        theme.style(val, lambda: f"color:{theme.MUTED if is_pending else theme.TEXT}; border:0;")
        lay.addWidget(t)
        lay.addWidget(val)
        store[name] = (val, unit)
        # Current card: add a direction badge below the number (CHG / DSG / REST)
        if name == "Current":
            self._lbl_i_dir = QLabel("—")
            self._lbl_i_dir.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            theme.style(self._lbl_i_dir, lambda: f"color:{theme.MUTED}; border:0;")
            lay.addWidget(self._lbl_i_dir)
        # SoC card: same sub-label pattern as Current's CHG/DSG/REST badge — shows
        # "Topping off ≤X.XXXA" right where the operator is looking at "100%" and
        # wondering why charging hasn't stopped, instead of only in the separate
        # workflow status line. Same wording as _charge_status_text's tail message,
        # just condensed to fit the card. Empty outside the CV/absorption tail.
        if name == "SoC":
            self._lbl_soc_note = QLabel("")
            self._lbl_soc_note.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            theme.style(self._lbl_soc_note, lambda: f"color:{theme.MUTED}; border:0;")
            lay.addWidget(self._lbl_soc_note)
        return card

    # ---- Analytics styling helpers ------------------------------------------
    # Single source of truth for widgets whose color depends on BOTH the theme
    # AND analysis state: the theme.style() registry replays these on retheme(),
    # and _slot_analysis_done re-applies the same fn after updating the cached
    # state — so neither path can stomp the other (see the CHARACTERIZE status
    # labels for the same pattern).

    def _grade_color(self, grade):
        return {"A": theme.OK, "B": theme.INFO, "C": theme.WARN,
                "REJECT": theme.CRIT, "REVIEW": theme.NEUTRAL}.get(grade, theme.NEUTRAL)

    def _grade_bar_style(self):
        grade = getattr(self, "_last_grade", None)
        if grade is None:   # never analyzed — neutral placeholder bar
            return (f"background:{theme.PANEL}; color:{theme.TEXT}; "
                    f"border:1px solid {theme.BORDER}; border-radius:6px; padding:10px;")
        return (f"background:{self._grade_color(grade)}; color:white; "
                f"border:1px solid {theme.BORDER}; border-radius:6px; padding:10px;")

    def _ecm_toggle_style(self):
        # None = never analyzed (button still disabled), True = 1-RC ECM was
        # identified (HPPC — accent border), False = analyzed but no ECM fit.
        identified = getattr(self, "_ecm_identified", None)
        border = theme.INFO if identified else theme.MUTED
        fg = theme.MUTED if identified is None else theme.TEXT
        return (f"QPushButton{{background:{theme.PANEL2};color:{fg};border:1px solid {border};"
                f"border-radius:4px;padding:3px 8px;text-align:left;}}"
                f"QPushButton:checked{{background:{theme.PANEL};color:{theme.TEXT};border-color:{border};}}"
                f"QPushButton:enabled:hover{{border-color:#aaa;}}")

    def _psu_trip_style(self):
        # None = not checked yet ("Trip: —"), True = tripped, False = OK —
        # set by _on_check_psu_trip, which re-applies this same fn.
        tripped = getattr(self, "_psu_tripped", None)
        if tripped is None:
            return f"color:{theme.MUTED};"
        return f"color:{theme.CRIT if tripped else theme.OK}; font-weight:600;"

    def _tab_analytics(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(4)

        # Analysis Results — moved here from the center panel so the final-test
        # numbers (Grade/SoH/Rin) live alongside the rest of the Analytics tab's
        # session/history tools instead of taking up space in the live-telemetry
        # column. Only ever written by a completed test's final analysis
        # (_on_test_finished) — never overwritten by live telemetry, so a result
        # here can't be mistaken for a live sensor reading.
        lay.addWidget(self._subheader("ANALYSIS RESULTS  (last completed test)"))
        analysis_row = QHBoxLayout()
        analysis_row.setSpacing(8)
        self.metric_labels_final = {}
        for name, unit in [("Grade", ""), ("SoH", "%"), ("Rin", "mΩ")]:
            analysis_row.addWidget(self._metric_card(name, unit, store=self.metric_labels_final), 1)
        lay.addLayout(analysis_row)
        lay.addWidget(_hline())

        # Session selector อยู่บนสุดของ Analytics
        sess_hdr = QHBoxLayout()
        sess_hdr.addWidget(QLabel("Sessions:"))
        btn_ref = QPushButton("↻")
        btn_ref.setFixedWidth(28)
        btn_ref.setToolTip("Refresh session list")
        btn_ref.clicked.connect(self._refresh_session_list)
        sess_hdr.addStretch()
        sess_hdr.addWidget(btn_ref)
        lay.addLayout(sess_hdr)

        self.lst_sessions = QListWidget()
        self.lst_sessions.setMaximumHeight(110)
        self.lst_sessions.setFont(QFont("Consolas", 9))
        self.lst_sessions.setToolTip("Click to analyze  ·  Right-click for rename/tag")
        self.lst_sessions.itemClicked.connect(self._on_session_selected)
        self.lst_sessions.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lst_sessions.customContextMenuRequested.connect(self._on_session_context_menu)
        lay.addWidget(self.lst_sessions)
        self._refresh_session_list()

        lay.addWidget(_hline())

        # ผลวิเคราะห์
        self.lbl_analytics = QLabel("Select a session above to analyze.")
        theme.style(self.lbl_analytics, lambda: f"color:{theme.MUTED};")
        lay.addWidget(self.lbl_analytics)

        # วงจร Thevenin ECM
        self.btn_ecm_toggle = QPushButton("▶ Show Equivalent Circuit")
        self.btn_ecm_toggle.setCheckable(True)
        self.btn_ecm_toggle.setEnabled(False)
        theme.style(self.btn_ecm_toggle, self._ecm_toggle_style)
        self.btn_ecm_toggle.clicked.connect(
            lambda checked: (
                self.lbl_ecm_diagram.setVisible(checked),
                self.btn_ecm_toggle.setText(
                    "▼ Hide Equivalent Circuit" if checked else "▶ Show Equivalent Circuit")
            )
        )
        lay.addWidget(self.btn_ecm_toggle)

        self.lbl_ecm_diagram = QSvgWidget()
        self.lbl_ecm_diagram.setFixedHeight(240)
        self.lbl_ecm_diagram.setVisible(False)
        lay.addWidget(self.lbl_ecm_diagram)

        self.txt_analytics = QTextEdit()
        self.txt_analytics.setReadOnly(True)
        self.txt_analytics.setFont(QFont("Segoe UI", 10))
        lay.addWidget(self.txt_analytics, 1)
        self.lbl_grade = QLabel("—")
        self.lbl_grade.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_grade.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
        theme.style(self.lbl_grade, self._grade_bar_style)
        lay.addWidget(self.lbl_grade)
        btn = _btn("Analyze Last CSV", bg="INFO", fg="white", hover="#0d4a89")
        btn.clicked.connect(self._on_analyze_csv)
        lay.addWidget(btn)

        # ── SoH trend + capacity fade charts (Removed per user request) ──
        return w

    def _build_ecm_svg(self, r0=None, r1=None, c1=None, ocv=None, tau=None) -> str:
        """วงจรสมมูลแบตเตอรี่ (Thévenin 1-RC) — ใช้ชื่อ R0/R1/C1 ให้ตรงกับตารางผล
        Analysis (เดิมใช้ R_I/R_d/C_d ตามตำรา ทำให้สับสนว่าเป็นคนละค่า):

            V_oc ── R0 ──┬── R1 ──┬── + (V_t)
                         └── C1 ──┘

        ค่าที่เป็น None จะแสดงเป็นตัวแปร (สัญลักษณ์เปล่า) — ใช้กับการทดสอบที่
        ไม่ใช่ HPPC ซึ่งระบุ R1/C1 ไม่ได้.
        """
        W, H = 560, 240
        ink    = "#1a1a1a"     # เส้น/สัญลักษณ์
        accent = "#1565c0"     # ค่าตัวเลข
        muted  = "#6a6a6a"
        bg     = "#fbfbfb"

        y_main = 120           # เส้นหลัก (R_I, R_d)
        y_cap  = 70            # กิ่งขนานยกขึ้น (C_d)
        y_bot  = 195           # สายกลับ
        bat_x  = 80
        term_x = 505

        def wire(x1, y1, x2, y2):
            return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{ink}" stroke-width="1.8"/>'

        def resistor(x1, x2, y, a=9):
            u = (x2 - x1) / 8.0
            ys = [0, 0, -a, a, -a, a, -a, 0, 0]
            pts = " ".join(f"{x1 + k*u:.1f},{y + ys[k]:.1f}" for k in range(9))
            return f'<polyline points="{pts}" fill="none" stroke="{ink}" stroke-width="1.8" stroke-linejoin="round"/>'

        def capacitor(cx, y, gap=9, ph=24):
            half = ph / 2
            p1, p2 = cx - gap / 2, cx + gap / 2
            return (
                f'<line x1="{p1}" y1="{y-half}" x2="{p1}" y2="{y+half}" stroke="{ink}" stroke-width="2.2"/>'
                f'<line x1="{p2}" y1="{y-half}" x2="{p2}" y2="{y+half}" stroke="{ink}" stroke-width="2.2"/>'
            )

        def sym(x, y, base, sub, size=15):
            return (f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Georgia, serif" '
                    f'font-size="{size}" fill="{ink}">{base}'
                    f'<tspan dy="4" font-size="{size-5}">{sub}</tspan></text>')

        def val(x, y, text):
            if not text:
                return ''
            return (f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Consolas, monospace" '
                    f'font-size="11" fill="{accent}">{text}</text>')

        ri_txt = f"{r0:.2f} mΩ" if r0 is not None else ""
        rd_txt = f"{r1:.2f} mΩ" if r1 is not None else ""
        cd_txt = f"{c1:.0f} F"  if c1 is not None else ""
        oc_txt = f"{ocv:.3f} V" if ocv is not None else ""

        # node A (แยกกิ่ง) และ node B (รวมกิ่ง)
        nA, nB = 255, 360

        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" rx="8" fill="{bg}"/>',

            # ── แบตเตอรี่ (V_oc) ในสายตั้งซ้าย ──
            wire(bat_x, y_main, bat_x, 148),
            f'<line x1="{bat_x-17}" y1="150" x2="{bat_x+17}" y2="150" stroke="{ink}" stroke-width="2"/>',   # plate +
            f'<line x1="{bat_x-9}" y1="159" x2="{bat_x+9}" y2="159" stroke="{ink}" stroke-width="3.5"/>',   # plate −
            wire(bat_x, 161, bat_x, y_bot),
            sym(bat_x + 32, 150, "V", "oc"),
            val(bat_x + 34, 168, oc_txt),

            # ── สายหลัก: bat → R_I → node A ──
            wire(bat_x, y_main, 150, y_main),
            resistor(150, 215, y_main),
            wire(215, y_main, nA, y_main),
            sym(182, y_main - 18, "R", "0"),
            val(182, y_main + 26, ri_txt),

            # ── กิ่งล่าง: R_d (อยู่บนเส้นหลัก) ──
            wire(nA, y_main, 272, y_main),
            resistor(272, 337, y_main),
            wire(337, y_main, nB, y_main),
            sym(304, y_main + 28, "R", "1"),
            val(304, y_main + 43, rd_txt),

            # ── กิ่งบน: C_d (กิ่งขนานยกขึ้น) ──
            wire(nA, y_main, nA, y_cap),
            wire(nA, y_cap, 296, y_cap),
            capacitor(304, y_cap),
            wire(312, y_cap, nB, y_cap),
            wire(nB, y_cap, nB, y_main),
            sym(304, y_cap - 16, "C", "1"),
            val(304, y_cap + 30, cd_txt),

            # ── ออกขั้ว + และสายกลับขั้ว − ──
            wire(nB, y_main, term_x, y_main),
            wire(bat_x, y_bot, term_x, y_bot),
            f'<circle cx="{term_x}" cy="{y_main}" r="4.5" fill="{bg}" stroke="{ink}" stroke-width="1.8"/>',
            f'<circle cx="{term_x}" cy="{y_bot}" r="4.5" fill="{bg}" stroke="{ink}" stroke-width="1.8"/>',
            f'<text x="{term_x-14}" y="{y_main-6}" font-family="Georgia, serif" font-size="15" fill="{ink}">+</text>',
            f'<text x="{term_x-14}" y="{y_bot+18}" font-family="Georgia, serif" font-size="15" fill="{ink}">−</text>',

            # ── V_t (แรงดันขั้ว) ──
            wire(term_x + 22, y_main, term_x + 22, y_bot),
            f'<polyline points="{term_x+18},{y_main+9} {term_x+22},{y_main} {term_x+26},{y_main+9}" '
            f'fill="none" stroke="{ink}" stroke-width="1.4"/>',
            f'<polyline points="{term_x+18},{y_bot-9} {term_x+22},{y_bot} {term_x+26},{y_bot-9}" '
            f'fill="none" stroke="{ink}" stroke-width="1.4"/>',
            sym(term_x + 38, (y_main + y_bot) // 2 + 4, "V", "t"),
        ]

        if r1 is None:
            parts.append(
                f'<text x="{W//2}" y="{H-10}" text-anchor="middle" font-family="Segoe UI" '
                f'font-size="10" fill="{muted}">Non-HPPC test — R1, C1 shown as symbols '
                f'(not identifiable without pulses)</text>'
            )
        elif tau is not None:
            parts.append(
                f'<text x="{W//2}" y="{H-10}" text-anchor="middle" font-family="Consolas, monospace" '
                f'font-size="10" fill="{muted}">1-RC Thévenin model · τ = R1·C1 = {tau:.1f} s</text>'
            )

        parts.append("</svg>")
        return "".join(parts)

    def _tab_alarms(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(0)
        lay.setContentsMargins(0, 0, 0, 0)

        # ── SCADA state ────────────────────────────────────────────────
        # Set of row indices that have ALARM/WARNING and are not yet ACKed
        self._unack_rows: set = set()
        self._flash_state: bool = False   # current flash phase (True=bright, False=dim)
        # Flash timer — 500 ms tick, SCADA standard blink rate
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._alarm_flash_tick)
        # Row colour bookkeeping {row_index: (bright_bg, dim_bg, fg, evt_fg)}
        self._alarm_row_colors: dict = {}

        # R2/G2 (industrial-grade audit, ISA-18.2 dedup/rate-limit): every
        # _log_alarm() call used to unconditionally insertRow() — a stuck sensor
        # re-firing the identical fault every telemetry tick would flood the log
        # with duplicate rows, burying whatever alarm actually mattered. See
        # _log_alarm()'s own comment for how these are used.
        self._ALARM_DEDUP_WINDOW_S = 30.0    # coalesce the SAME (event, point) within this
        self._last_alarm_key = None          # (event, point) of the most recent row
        self._last_alarm_row = None          # its row index, so a repeat can update in place
        self._last_alarm_occurrence = 1      # occurrence count shown as "(xN)"
        self._last_alarm_time = 0.0
        self._ALARM_RATE_LIMIT = 20          # max distinct rows...
        self._ALARM_RATE_WINDOW_S = 10.0     # ...within this many seconds
        self._alarm_recent_times: deque = deque()
        self._alarm_rate_limit_row = None    # row index of the current flood's summary row
        self._alarm_rate_suppressed = 0      # count shown in that row's text

        # ── Header bar ────────────────────────────────────────────────
        hdr = QFrame()
        theme.style(hdr, lambda: f"background:{theme.PANEL}; border-bottom:1px solid #888;")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(8, 5, 8, 5)
        lbl_title = QLabel("⚡  EVENT / ALARM LOG")
        theme.style(lbl_title, lambda: (
            f"font-weight:700; font-size:12px; color:{theme.TEXT}; border:0; background:transparent;"))
        hdr_lay.addWidget(lbl_title)
        hdr_lay.addStretch()
        lbl_count = QLabel("0 events")
        lbl_count.setObjectName("alarm_count")
        theme.style(lbl_count, lambda: f"color:{theme.MUTED}; font-size:10px; border:0; background:transparent;")
        self._alarm_count_lbl = lbl_count
        hdr_lay.addWidget(lbl_count)
        hdr_lay.addSpacing(12)
        # ── ACKNOWLEDGE button (SCADA standard) ────────────────────────
        self._btn_ack = QPushButton("ACKNOWLEDGE")
        self._btn_ack.setFixedSize(110, 24)
        self._btn_ack.setEnabled(False)
        self._btn_ack.setStyleSheet(
            "QPushButton{background:#5A1A1A;border:1px solid #FF5555;border-radius:3px;"
            "font-size:10px;font-weight:700;color:#FF5555;}"
            "QPushButton:hover{background:#7A2A2A;color:#FFaaaa;}"
            "QPushButton:disabled{background:#2A2A2A;border:1px solid #555;color:#555;}"
        )
        self._btn_ack.clicked.connect(self._alarm_acknowledge)
        hdr_lay.addWidget(self._btn_ack)
        hdr_lay.addSpacing(8)
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedSize(60, 24)
        btn_clear.setStyleSheet(
            f"QPushButton{{background:{theme.PANEL2};border:1px solid #999;border-radius:3px;font-size:10px;}}"
            f"QPushButton:hover{{background:{theme.FIELD};}}"
        )
        btn_clear.clicked.connect(self._alarm_clear)
        hdr_lay.addWidget(btn_clear)
        lay.addWidget(hdr)

        # ── Table ─────────────────────────────────────────────────────
        self.tbl_alarms = QTableWidget()
        self.tbl_alarms.setColumnCount(5)
        self.tbl_alarms.setHorizontalHeaderLabels(
            ["DATE/TIME", "POINT NAME", "STATE", "EVENT", "ACK STATUS"]
        )
        hh = self.tbl_alarms.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tbl_alarms.verticalHeader().setVisible(False)
        self.tbl_alarms.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_alarms.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_alarms.setAlternatingRowColors(False)
        self.tbl_alarms.setShowGrid(True)
        self.tbl_alarms.setStyleSheet(
            f"QTableWidget{{background:#1C1F23; color:#E0E3E6; gridline-color:#333; border:0; font-size:11px;}}"
            f"QHeaderView::section{{background:#2C3036; color:#A8B0B8; padding:4px 8px; border:0;"
            f" border-bottom:1px solid #444; font-size:11px; font-weight:700;}}"
            f"QTableWidget::item{{padding:2px 8px; border:0;}}"
            f"QTableWidget::item:selected{{background:#3A5080; color:white;}}"
        )
        lay.addWidget(self.tbl_alarms, 1)

        # ── Status bar ────────────────────────────────────────────────
        self._alarm_statusbar = QLabel("  SYSTEM READY")
        self._alarm_statusbar.setStyleSheet(
            "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
            " font-family:Consolas,monospace; border-top:1px solid #333;"
        )
        lay.addWidget(self._alarm_statusbar)

        self._log_alarm("System ready.")
        return w

    def _alarm_clear(self):
        """Wipes the alarm/event log — irreversible (no undo, no export-first
        prompt), so this asks for confirmation first. A stale comment used to sit
        right here claiming a "full SCADA implementation" of this method lived in
        BatteryQtWindow and would override this one via MRO — it does not; grep
        confirms this is the only _alarm_clear in the codebase, so the unconfirmed
        version was what actually ran until now. Same class of bug as the
        _zone_characterize shadowing case documented in CLAUDE.md: verify what's
        live via grep, don't trust a comment's claim about it."""
        if not self._headless:
            reply = QMessageBox.question(
                self, "Clear Alarm Log",
                f"Clear all {self.tbl_alarms.rowCount()} event(s) from the log?\n\n"
                "This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.tbl_alarms.setRowCount(0)
        self._unack_rows.clear()
        self._alarm_row_colors.clear()
        self._flash_timer.stop()
        self._btn_ack.setEnabled(False)
        self._alarm_count_lbl.setText("0 events")
        self._alarm_statusbar.setText("  LOG CLEARED")
        self._alarm_statusbar.setStyleSheet(
            "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
            " font-family:Consolas,monospace; border-top:1px solid #333;"
        )
