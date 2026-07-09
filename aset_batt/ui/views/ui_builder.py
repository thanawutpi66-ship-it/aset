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
    QDoubleSpinBox,
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

import aset_batt.core.battery_profiles as battery_profiles
from aset_batt.core.analysis_module import ChemistryDetector
from aset_batt.core.iec61960_standard import IEC61960Standard

logger = logging.getLogger(__name__)

# ISA-101 palette: neutral gray shell with color reserved for state/alarm only.
from aset_batt.ui.theme import (
    BG, PANEL, PANEL2, FIELD, BORDER, TEXT, MUTED, OK, WARN, CRIT, INFO, NEUTRAL,
)

from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html
from aset_batt.ui.zones import ZonesMixin
from aset_batt.ui.sequences import SequencesMixin
from aset_batt.ui.characterize import CharacterizeMixin

class UiBuilderMixin:
    def _build_ui(self):
        # Show which palette this process actually baked in — diagnoses stale
        # code / wrong-CWD launches where config says dark but the UI stays light.
        from aset_batt.ui import theme
        _theme_name = "dark" if BG == theme.DARK["BG"] else "light"
        self.setWindowTitle(
            f"ASET Battery Tester — ISA-101 Command Center  [{_theme_name}]")
        self.resize(1440, 900)
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aset_logo.png")
        if os.path.exists(_icon_path):
            from PySide6.QtGui import QIcon
            self.setWindowIcon(QIcon(_icon_path))
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; font-family:'Segoe UI','Inter',sans-serif; font-size:12px; }}
            QGroupBox {{ border:1px solid {BORDER}; border-radius:4px; margin-top:12px; background:{PANEL}; font-weight:700; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:1px 6px; color:{TEXT}; background:{PANEL}; letter-spacing:1px; }}
            QLabel {{ background:transparent; }}
            QComboBox, QLineEdit {{ background:{FIELD}; border:1px solid {BORDER}; border-radius:3px; padding:4px 6px; color:{TEXT}; }}
            QComboBox:focus, QLineEdit:focus {{ border:1px solid {INFO}; }}
            QDoubleSpinBox, QSpinBox {{ background:{FIELD}; border:1px solid {BORDER}; border-radius:3px; padding:3px 4px; color:{TEXT}; }}
            QDoubleSpinBox:focus, QSpinBox:focus {{ border:1px solid {INFO}; }}
            QDoubleSpinBox:hover, QSpinBox:hover {{ border:1px solid {INFO}; }}
            QComboBox QAbstractItemView {{ background:{FIELD}; color:{TEXT}; selection-background-color:{INFO}; selection-color:white; }}
            QListWidget {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}
            QListWidget::item {{ padding:5px 6px; }}
            QListWidget::item:selected {{ background:{INFO}; color:white; }}
            QTextEdit {{ background:{PANEL2}; border:1px solid {BORDER}; color:{TEXT}; }}
            QTabWidget::pane {{ border:1px solid {BORDER}; background:{PANEL2}; }}
            QTabBar::tab {{ background:{PANEL}; padding:6px 14px; border:1px solid {BORDER}; border-bottom:0; color:{MUTED}; }}
            QTabBar::tab:selected {{ background:{PANEL2}; color:{TEXT}; font-weight:700; }}
            QMenuBar {{ background:{PANEL}; border-bottom:1px solid {BORDER}; padding:1px 0; }}
            QMenuBar::item {{ padding:4px 10px; border-radius:3px; }}
            QMenuBar::item:selected {{ background:{INFO}; color:white; }}
            QMenu {{ background:{PANEL2}; border:1px solid {BORDER}; padding:3px 0; }}
            QMenu::item {{ padding:5px 22px; }}
            QMenu::item:selected {{ background:{INFO}; color:white; }}
            QMenu::separator {{ height:1px; background:{BORDER}; margin:3px 0; }}
            QToolBar {{ background:{PANEL}; border-bottom:1px solid {BORDER}; spacing:3px; padding:3px 6px; }}
            QStatusBar {{ background:{PANEL}; border-top:1px solid {BORDER}; font-size:11px; }}
            """
        )

        self._build_menubar()
        self._build_toolbar()
        self._build_statusbar()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)   # panels can't be dragged to 0
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_center_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([350, 830, 360])
        self.setCentralWidget(splitter)
    def _pill(self, color):
        return f"background:{color}; color:white; border-radius:3px; padding:5px 12px; font-weight:700; letter-spacing:1px;"
    def _build_menubar(self):
        bar = self.menuBar()

        m = bar.addMenu("File")
        m.addAction("Open CSV…", self._on_analyze_csv)
        m.addSeparator()
        m.addAction("Save as Default", self._on_save_default)
        m.addSeparator()
        m.addAction("Exit", self.close)

        m = bar.addMenu("Run")
        m.addAction("Connect", self._on_connect)
        m.addAction("Disconnect", self._on_disconnect)
        m.addSeparator()
        m.addAction("Charge", self._on_charge)
        m.addAction("Stop Charge", self._on_stop_charge)
        m.addSeparator()
        m.addAction("Run Test", self._on_run_test)
        m.addAction("Stop Test", self._on_stop_test)

        m = bar.addMenu("View")
        m.addAction("Toggle Dark Mode", self._on_toggle_dark_mode)
        m.addSeparator()
        g = m.addMenu("Graph Mode")
        for _lbl in ("Combined", "Split 2", "Split 3"):
            g.addAction(_lbl, lambda l=_lbl: self._set_graph_mode(l))

        # Tools: workflow/calibration utilities + settings, grouped together (moved
        # OCV Calibrate/Auto Sequence/Quick Scan out of Run, and folded the standalone
        # Settings menu's one action in here too) so Run stays limited to core
        # connect/charge/test start-stop actions.
        m = bar.addMenu("Tools")
        m.addAction("Detect Chemistry", self._on_detect_chemistry)
        m.addSeparator()
        m.addAction("OCV Calibrate", self._on_ocv_calibrate)
        m.addAction("Auto Sequence", self._on_auto_sequence)
        m.addAction("Quick Scan", self._on_quick_scan)
        m.addSeparator()
        m.addAction("Refresh Ports", self._refresh_ports)
        m.addSeparator()
        m.addAction("Open Cloud Dashboard", self._on_open_dashboard)
        m.addSeparator()
        m.addAction("Generate PDF Report", self._on_pdf_report)
        m.addSeparator()
        m.addAction("Preferences", self._on_open_settings)

        m = bar.addMenu("Help")
        m.addAction("About ASET Battery Tester", self._on_about)
    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setStyleSheet(f"QToolBar {{ background:{PANEL}; border-bottom:1px solid {BORDER}; }}")
        
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        spacer.setStyleSheet("border: none; background: transparent;")
        toolbar.addWidget(spacer)

        mode = "SIMULATION" if self.config.system.simulation_mode else "HARDWARE"
        color = WARN if self.config.system.simulation_mode else OK
        self.mode_badge = QLabel(f"  {mode}  ")
        self.mode_badge.setStyleSheet(
            f"background:transparent; color:{color}; border:1px solid {color}; "
            f"border-radius:4px; padding:3px 8px; font-weight:700; letter-spacing:1px; margin-right: 10px;"
        )
        toolbar.addWidget(self.mode_badge)

        self.state_pill = QLabel("  IDLE  ")
        self.state_pill.setStyleSheet(self._pill(NEUTRAL) + " border: none; margin-right: 10px;")
        toolbar.addWidget(self.state_pill)

        self.btn_estop = QPushButton("⛔ E-STOP")
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{CRIT}; color:white; border:none; border-radius:5px; "
            f"padding:4px 14px; font-size:13px; font-weight:800; margin-right: 10px; }}"
            f"QPushButton:hover {{ background:#9b2020; }}"
        )
        self.btn_estop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_estop.clicked.connect(self._on_estop)
        toolbar.addWidget(self.btn_estop)
        
        self.addToolBar(toolbar)
    def _build_statusbar(self):
        sb = self.statusBar()
        self.status_label = QLabel("Ready — connect hardware to begin")
        self.status_label.setStyleSheet(f"color:{MUTED};")
        sb.addWidget(self.status_label, 1)
        # G3 (industrial-grade audit): test progress/ETA (wf_progress/lbl_eta, see
        # zones.py) used to live only inside the SETUP tab's AUTO-sequence sub-page
        # — switching to TEST MODE or another workflow sub-tab during a running test
        # hid progress entirely. This compact mirror lives in the status bar, which
        # is visible regardless of which tab is active. Driven from the same single
        # update site as wf_progress (sequences.py's _slot_phase_progress).
        self.status_progress = QProgressBar()
        self.status_progress.setTextVisible(True)
        self.status_progress.setMaximumWidth(160)
        self.status_progress.setMaximumHeight(14)
        self.status_progress.setStyleSheet(
            f"QProgressBar{{border:1px solid {BORDER};border-radius:3px;"
            f"background:{PANEL2};text-align:center;font-size:9px;}}"
            f"QProgressBar::chunk{{background:{INFO};border-radius:2px;}}"
        )
        self.status_progress.hide()
        sb.addPermanentWidget(self.status_progress)
        # Update banner — hidden until a git check finds origin ahead of us.
        self.btn_update = QPushButton("⭯ Update available")
        self.btn_update.setVisible(False)
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setStyleSheet(
            f"QPushButton{{background:{INFO}; color:white; border:0; border-radius:3px; "
            f"padding:2px 10px; font-weight:700;}} QPushButton:hover{{background:#1565c0;}} "
            f"QPushButton:disabled{{background:{MUTED};}}")
        self.btn_update.clicked.connect(self._on_update_clicked)
        sb.addPermanentWidget(self.btn_update)
        self.conn_led = QLabel("●")
        self.conn_led.setStyleSheet(f"color:{NEUTRAL}; font-size:14px; padding:0 4px;")
        sb.addPermanentWidget(self.conn_led)
        self.conn_text = QLabel("Disconnected")
        self.conn_text.setStyleSheet(f"color:{MUTED}; font-weight:600; padding-right:8px;")
        sb.addPermanentWidget(self.conn_text)
    def _build_center_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self.metric_labels = {}
        for name, unit in [("Voltage", "V"), ("Current", "A"), ("SoC", "%"),
                           ("Rin", "mΩ"), ("Temp", "°C")]:
            cards_row.addWidget(self._metric_card(name, unit), 1)
        lay.addLayout(cards_row)

        # Analysis Results (Grade/SoH/Rin) now lives in the Analytics tab on the
        # right (see _tab_analytics) instead of here — keeps the center panel to
        # live telemetry only, and groups the final-test numbers with the rest of
        # the Analytics tab's session/history tools instead of duplicating them.

        # The old "CASE TEMPERATURE" box duplicated the TEMP metric card above it
        # (same signal, same value, twice) — removed; the TEMP card's own color now
        # carries the CRIT/WARN over-temperature signal instead (see
        # _set_temp_label_color), and the graph gets the reclaimed vertical space.
        self.trend = TrendContainer()
        # Sensible idle-state view (before any real telemetry exists) instead of
        # pyqtgraph auto-ranging an empty curve to an arbitrary small window that
        # doesn't include the pack's actual voltage.
        b = self.config.battery
        crit_temp = self.config.system.safety_limits.get("max_temperature", 55.0)
        self.trend.set_default_ranges(
            v_max=b.pack_max_voltage * 1.05, i_max=max(1.0, b.max_current),
            t_max=crit_temp * 1.2)
        lay.addWidget(self.trend, 3)
        return panel
    def _set_graph_mode(self, label: str):
        if not hasattr(self, "trend"):
            return
        modes = TrendContainer.MODES
        idx = modes.index(label) if label in modes else 0
        self.trend._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.trend._btn_group.buttons()):
            btn.setChecked(i == idx)
    def _build_left_panel(self):
        """Left column: three top-level tabs (SETUP / TEST MODE / TOOLS) that
        follow the 1→2→3 workflow order. Each tab scrolls independently."""
        def _scroll(inner):
            holder = QWidget()
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(8, 8, 8, 8)
            hl.addWidget(inner)
            hl.addStretch(1)
            sc = QScrollArea()
            sc.setWidget(holder)
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sc.setStyleSheet("QScrollArea { background: transparent; }")
            return sc

        tabs = QTabWidget()
        tabs.setMinimumWidth(300)
        tabs.addTab(_scroll(self._zone_setup()),     "SETUP")
        tabs.addTab(_scroll(self._zone_test_mode()), "TEST MODE")
        return tabs
    def _subheader(self, text):
        """Bold caption that groups related controls inside a zone."""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{TEXT}; font-size:11px; font-weight:800; letter-spacing:1px; padding-top:4px;")
        return lbl
    @staticmethod
    def _combo_shrink(cb, min_chars=6):
        """Let a combo with long items shrink below its content width (the
        current text is elided) so it never forces the whole panel wider than
        the column. The full text stays visible in the dropdown + tooltip."""
        cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        cb.setMinimumContentsLength(min_chars)
        cb.setToolTip(cb.currentText())
        cb.currentTextChanged.connect(cb.setToolTip)