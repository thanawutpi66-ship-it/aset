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
    QGridLayout,
    QGroupBox,
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
    QDoubleSpinBox,
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

logger = logging.getLogger(__name__)


# ISA-101 palette: neutral gray shell with color reserved for state/alarm only.
BG = "#b9bdc1"
PANEL = "#c9cdd1"
PANEL2 = "#d7dadd"
FIELD = "#eceef0"
BORDER = "#8c9296"
TEXT = "#1d2123"
MUTED = "#54595d"
OK = "#2e7d32"
WARN = "#c98a00"
CRIT = "#c62828"
INFO = "#1565c0"
NEUTRAL = "#6b7075"


class _FalseEvent:
    """Sentinel event that is never set — used as a default guard in characterize handlers."""
    def is_set(self):
        return False


def _btn(text, bg=PANEL2, fg=TEXT, hover=FIELD):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        "QPushButton {{ background:{0}; color:{1}; border:1px solid {2}; "
        "border-radius:4px; padding:7px 10px; font-weight:600; }}"
        "QPushButton:hover {{ background:{3}; }}".format(bg, fg, BORDER, hover)
    )
    return b


def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color:{BORDER}; background:{BORDER}; max-height:1px;")
    return line


class QtRootShim(QObject):
    _invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception as exc:
            logger.error("QtRootShim invoke error: %s", exc)

    def after(self, ms, fn=None, *args):
        if fn is None:
            return
        cb = (lambda: fn(*args)) if args else fn
        if ms and ms > 0:
            QTimer.singleShot(int(ms), lambda: self._invoke.emit(cb))
        else:
            self._invoke.emit(cb)

    def protocol(self, name, fn):
        self._close_handler = fn

    def destroy(self):
        QApplication.quit()


class DigitalReadout(QFrame):
    def __init__(self, label: str, unit: str):
        super().__init__()
        self.unit = unit
        self.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(1)
        cap = QLabel(label.upper())
        cap.setStyleSheet(
            f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;"
        )
        self.value = QLabel(f"-- {unit}")
        self.value.setFont(QFont("Consolas", 20, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap)
        lay.addWidget(self.value)

    def set_value(self, value: float, fmt: str = "{:.3f}", alarm: bool = False):
        self.value.setText(f"{fmt.format(value)} {self.unit}")
        self.value.setStyleSheet(f"color:{CRIT if alarm else TEXT}; border:0;")


class TemperatureGauge(QFrame):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 10)
        cap = QLabel("CASE TEMPERATURE")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(
            f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;"
        )
        self.value = QLabel("-- °C")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setFont(QFont("Consolas", 30, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap)
        lay.addWidget(self.value)

    def update_temp(self, temp: float, warn: float, crit: float):
        if math.isnan(temp):
            self.value.setText("-- °C")
            return
        color = CRIT if temp >= crit else WARN if temp >= warn else OK
        self.value.setText(f"{temp:.1f} °C")
        self.value.setStyleSheet(f"color:{color}; border:0;")


class MultiAxisTrend(pg.GraphicsLayoutWidget):
    """Voltage (left) + Current (right) + Temperature (far right) over time."""

    def __init__(self):
        super().__init__()
        self.setBackground(PANEL2)
        self.p = self.addPlot()
        self.p.setLabel("bottom", "Elapsed", units="s")
        self.p.setLabel("left", "Voltage", units="V", color=INFO)
        self.p.showGrid(x=True, y=True, alpha=0.2)
        self.p.getAxis("left").setPen(INFO)

        self.vb_i = pg.ViewBox()
        self.p.showAxis("right")
        self.p.scene().addItem(self.vb_i)
        self.p.getAxis("right").linkToView(self.vb_i)
        self.p.getAxis("right").setLabel("Current", units="A", color=WARN)
        self.p.getAxis("right").setPen(WARN)
        self.vb_i.setXLink(self.p)

        self.ax_t = pg.AxisItem("right")
        self.p.layout.addItem(self.ax_t, 2, 3)
        self.vb_t = pg.ViewBox()
        self.p.scene().addItem(self.vb_t)
        self.ax_t.linkToView(self.vb_t)
        self.ax_t.setLabel("Temp", units="°C", color=CRIT)
        self.ax_t.setPen(CRIT)
        self.vb_t.setXLink(self.p)

        self.c_v = self.p.plot(pen=pg.mkPen(INFO, width=2))
        self.c_i = pg.PlotCurveItem(pen=pg.mkPen(WARN, width=2))
        self.c_t = pg.PlotCurveItem(pen=pg.mkPen(CRIT, width=2, style=Qt.PenStyle.DashLine))
        self.vb_i.addItem(self.c_i)
        self.vb_t.addItem(self.c_t)

        self.p.vb.sigResized.connect(self._sync)

    def _sync(self):
        self.vb_i.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_t.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_i.linkedViewChanged(self.p.vb, self.vb_i.XAxis)
        self.vb_t.linkedViewChanged(self.p.vb, self.vb_t.XAxis)

    def update(self, t, v, i, temp):
        self.c_v.setData(t, v)
        self.c_i.setData(t, i)
        self.c_t.setData(t, temp)


class SplitTrend(QWidget):
    """Voltage+Current (top) / Temperature (bottom) — 2 separate plots."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._vi = pg.PlotWidget()
        self._vi.setBackground(PANEL2)
        self._vi.setLabel("bottom", "Elapsed", units="s")
        self._vi.setLabel("left", "Voltage", units="V", color=INFO)
        self._vi.showGrid(x=True, y=True, alpha=0.2)
        self._vi.getAxis("left").setPen(INFO)
        self._vi.showAxis("right")
        self._vb_i = pg.ViewBox()
        self._vi.scene().addItem(self._vb_i)
        self._vi.getAxis("right").linkToView(self._vb_i)
        self._vi.getAxis("right").setLabel("Current", units="A", color=WARN)
        self._vi.getAxis("right").setPen(WARN)
        self._vb_i.setXLink(self._vi.getPlotItem())
        self._c_v = self._vi.plot(pen=pg.mkPen(INFO, width=2))
        self._c_i = pg.PlotCurveItem(pen=pg.mkPen(WARN, width=2))
        self._vb_i.addItem(self._c_i)
        self._vi.getPlotItem().vb.sigResized.connect(self._sync_vi)

        self._tp = pg.PlotWidget()
        self._tp.setBackground(PANEL2)
        self._tp.setLabel("bottom", "Elapsed", units="s")
        self._tp.setLabel("left", "Temp", units="°C", color=CRIT)
        self._tp.showGrid(x=True, y=True, alpha=0.2)
        self._tp.getAxis("left").setPen(CRIT)
        self._c_t = self._tp.plot(pen=pg.mkPen(CRIT, width=2, style=Qt.PenStyle.DashLine))

        lay.addWidget(self._vi, 3)
        lay.addWidget(self._tp, 1)

    def _sync_vi(self):
        self._vb_i.setGeometry(self._vi.getPlotItem().vb.sceneBoundingRect())
        self._vb_i.linkedViewChanged(self._vi.getPlotItem().vb, self._vb_i.XAxis)

    def update(self, t, v, i, temp):
        self._c_v.setData(t, v)
        self._c_i.setData(t, i)
        self._c_t.setData(t, temp)


class TripleTrend(QWidget):
    """Voltage / Current / Temperature — 3 fully independent plots."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        specs = [
            ("Voltage", "V", INFO, Qt.PenStyle.SolidLine),
            ("Current", "A", WARN, Qt.PenStyle.SolidLine),
            ("Temp",    "°C", CRIT, Qt.PenStyle.DashLine),
        ]
        self._curves = []
        for label, unit, color, style in specs:
            pw = pg.PlotWidget()
            pw.setBackground(PANEL2)
            pw.setLabel("bottom", "Elapsed", units="s")
            pw.setLabel("left", label, units=unit, color=color)
            pw.showGrid(x=True, y=True, alpha=0.2)
            pw.getAxis("left").setPen(color)
            curve = pw.plot(pen=pg.mkPen(color, width=2, style=style))
            self._curves.append(curve)
            lay.addWidget(pw, 1)

    def update(self, t, v, i, temp):
        for curve, data in zip(self._curves, [v, i, temp]):
            curve.setData(t, data)


class TrendContainer(QWidget):
    """Wraps the 3 trend modes with a toggle bar. Press A to toggle 10s zoom."""

    MODES = ["Combined", "Split 2", "Split 3"]
    _ZOOM_WINDOW = 10  # seconds

    def __init__(self):
        super().__init__()
        self._zoom_active = False
        self._last_t: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        bar = QHBoxLayout()
        self._zoom_btn = QPushButton("A")
        self._zoom_btn.setCheckable(True)
        self._zoom_btn.setFixedSize(24, 22)
        self._zoom_btn.setToolTip("Toggle 10s zoom")
        self._zoom_btn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{MUTED};border:1px solid {MUTED};border-radius:3px;font-weight:bold;}}"
            f"QPushButton:checked{{background:{INFO};color:#000;border:1px solid {INFO};}}"
            f"QPushButton:hover{{border-color:#aaa;}}"
        )
        self._zoom_btn.clicked.connect(self._on_zoom_btn)
        bar.addWidget(self._zoom_btn)
        bar.addStretch()
        bar.addWidget(QLabel("Graph mode:"))
        self._btn_group = QButtonGroup(self)
        for idx, label in enumerate(self.MODES):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            self._btn_group.addButton(btn, idx)
            bar.addWidget(btn)
        bar.addStretch()
        root.addLayout(bar)

        self._stack = QStackedWidget()
        self._combined = MultiAxisTrend()
        self._split2   = SplitTrend()
        self._split3   = TripleTrend()
        self._stack.addWidget(self._combined)
        self._stack.addWidget(self._split2)
        self._stack.addWidget(self._split3)
        root.addWidget(self._stack, 1)

        self._btn_group.buttons()[1].setChecked(True)
        self._stack.setCurrentIndex(1)
        self._btn_group.idClicked.connect(self._on_mode_changed)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _on_mode_changed(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._apply_zoom()

    def _all_plots(self):
        """Return all PlotWidget/PlotItem x-axes currently visible."""
        plots = []
        idx = self._stack.currentIndex()
        if idx == 0:
            plots.append(self._combined.p)
        elif idx == 1:
            plots.append(self._split2._vi.getPlotItem())
            plots.append(self._split2._tp.getPlotItem())
        else:
            for i in range(self._split3.layout().count()):
                w = self._split3.layout().itemAt(i).widget()
                if isinstance(w, pg.PlotWidget):
                    plots.append(w.getPlotItem())
        return plots

    def _apply_zoom(self):
        if not self._last_t:
            return
        plots = self._all_plots()
        if self._zoom_active and len(self._last_t) >= 2:
            t_end = self._last_t[-1]
            t_start = max(self._last_t[0], t_end - self._ZOOM_WINDOW)
            for p in plots:
                p.setXRange(t_start, t_end, padding=0.02)
        else:
            for p in plots:
                p.enableAutoRange(axis='x')

    def _on_zoom_btn(self, checked: bool):
        self._zoom_active = checked
        self._apply_zoom()

    def update(self, t, v, i, temp):
        self._last_t = t
        idx = self._stack.currentIndex()
        [self._combined, self._split2, self._split3][idx].update(t, v, i, temp)
        if self._zoom_active:
            self._apply_zoom()


class _PdfNotifier(QObject):
    finished = Signal(bool, str)


class _PdfTask(QRunnable):
    def __init__(self, notifier: _PdfNotifier, path: str, config, estimator, analysis, csv_path: str):
        super().__init__()
        self.notifier = notifier
        self.path = path
        self.config = config
        self.estimator = estimator
        self.analysis = analysis
        self.csv_path = csv_path

    def run(self):
        try:
            from aset_batt.storage.report_generator import generate_pdf_report

            generate_pdf_report(
                self.path,
                self.config,
                self.estimator,
                analysis=self.analysis,
                csv_path=self.csv_path,
            )
            self.notifier.finished.emit(True, self.path)
        except Exception as exc:
            logger.exception("PDF generation failed")
            self.notifier.finished.emit(False, str(exc))


class BatteryQtWindow(QMainWindow):
    sig_display = Signal(float, float, float, float, float, float)
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

        self.buf_t = deque()
        self.buf_v = deque()
        self.buf_i = deque()
        self.buf_soc = deque()
        self.buf_rin = deque()
        self.buf_temp = deque()
        self._elapsed_t0 = None
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

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_connection_status)
        self._tick.start(1000)

    def bind_controller(self, controller):
        self.controller = controller
        self.hw = controller.hw
        self.data = controller.data
        self.estimator = controller.estimator
        self._refresh_ports()
        self._on_product_changed(self.cb_product.currentText())
        self._update_connection_status()
        # load bleed value from config into spinbox (block signal to avoid spurious save)
        bleed_val = getattr(controller.config.hardware, "psu_bleed_a", 0.0)
        self.spn_psu_bleed.blockSignals(True)
        self.spn_psu_bleed.setValue(bleed_val)
        self.spn_psu_bleed.blockSignals(False)

    def _build_ui(self):
        self.setWindowTitle("ASET Battery Tester — ISA-101 Command Center")
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
        splitter.setSizes([300, 880, 360])
        self.setCentralWidget(splitter)

    def _logo(self, filename, h=40):
        lbl = QLabel()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        pix = QPixmap(path)
        if not pix.isNull():
            lbl.setPixmap(pix.scaledToHeight(h, Qt.TransformationMode.SmoothTransformation))
        return lbl

    def _build_header(self):
        bar = QFrame()
        bar.setFixedHeight(62)
        bar.setStyleSheet(f"background:{PANEL}; border:1px solid {BORDER}; border-radius:4px;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(12)
        lay.addWidget(self._logo("00021f2021030914260622.png", 42))
        lay.addWidget(self._logo("00021b2021031713352962.png", 38))

        title = QLabel("BATTERY TEST & SORTING COMMAND CENTER")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lay.addWidget(title)
        lay.addStretch(1)

        self.conn_led = QLabel("●")
        self.conn_led.setStyleSheet(f"color:{NEUTRAL}; font-size:16px;")
        self.conn_text = QLabel("Disconnected")
        self.conn_text.setStyleSheet(f"color:{MUTED}; font-weight:600;")
        lay.addWidget(self.conn_led)
        lay.addWidget(self.conn_text)

        mode = "SIMULATION" if self.config.system.simulation_mode else "HARDWARE"
        color = WARN if self.config.system.simulation_mode else OK
        self.mode_badge = QLabel(f"  {mode}  ")
        self.mode_badge.setStyleSheet(
            f"background:transparent; color:{color}; border:1px solid {color}; border-radius:4px; padding:4px 8px; font-weight:700; letter-spacing:1px;"
        )
        lay.addWidget(self.mode_badge)

        self.state_pill = QLabel("  IDLE  ")
        self.state_pill.setStyleSheet(self._pill(NEUTRAL))
        lay.addWidget(self.state_pill)
        return bar

    def _pill(self, color):
        return f"background:{color}; color:white; border-radius:3px; padding:5px 12px; font-weight:700; letter-spacing:1px;"

    # ---- International standard: Menu bar / Toolbar / Status bar ---------------

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
        m.addAction("OCV Calibrate", self._on_ocv_calibrate)
        m.addSeparator()
        m.addAction("Charge", self._on_charge)
        m.addAction("Stop Charge", self._on_stop_charge)
        m.addSeparator()
        m.addAction("Run Test", self._on_run_test)
        m.addAction("Stop Test", self._on_stop_test)
        m.addSeparator()
        m.addAction("Start Monitor", self._on_start_monitor)
        m.addAction("Stop Monitor", lambda: self.controller and self.controller.stop_monitor())
        m.addSeparator()
        m.addAction("Auto Sequence", self._on_auto_sequence)
        m.addAction("Quick Scan", self._on_quick_scan)

        m = bar.addMenu("View")
        g = m.addMenu("Graph Mode")
        for _lbl in ("Combined", "Split 2", "Split 3"):
            g.addAction(_lbl, lambda l=_lbl: self._set_graph_mode(l))

        m = bar.addMenu("Tools")
        m.addAction("Detect Chemistry", self._on_detect_chemistry)
        m.addSeparator()
        m.addAction("Refresh Ports", self._refresh_ports)
        m.addSeparator()
        m.addAction("Open Cloud Dashboard", self._on_open_dashboard)
        m.addSeparator()
        m.addAction("Generate PDF Report", self._on_pdf_report)

        m = bar.addMenu("Help")
        m.addAction("About ASET Battery Tester", self._on_about)

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        tb.addAction("Connect", self._on_connect)
        tb.addAction("Disconnect", self._on_disconnect)
        tb.addSeparator()
        tb.addAction("OCV", self._on_ocv_calibrate)
        tb.addSeparator()
        tb.addAction("▶ Auto Seq", self._on_auto_sequence)
        tb.addAction("⚡ Quick Scan", self._on_quick_scan)
        tb.addSeparator()
        tb.addAction("Start Monitor", self._on_start_monitor)
        tb.addAction("Stop Monitor", lambda: self.controller and self.controller.stop_monitor())
        tb.addSeparator()

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        mode = "SIMULATION" if self.config.system.simulation_mode else "HARDWARE"
        color = WARN if self.config.system.simulation_mode else OK
        self.mode_badge = QLabel(f"  {mode}  ")
        self.mode_badge.setStyleSheet(
            f"background:transparent; color:{color}; border:1px solid {color}; "
            f"border-radius:4px; padding:3px 8px; font-weight:700; letter-spacing:1px;"
        )
        tb.addWidget(self.mode_badge)

        self.state_pill = QLabel("  IDLE  ")
        self.state_pill.setStyleSheet(self._pill(NEUTRAL))
        tb.addWidget(self.state_pill)
        tb.addSeparator()

        self.btn_estop = QPushButton("⛔ E-STOP")
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{CRIT}; color:white; border:none; border-radius:5px; "
            f"padding:7px 14px; font-size:13px; font-weight:800; }}"
            f"QPushButton:hover {{ background:#9b2020; }}"
        )
        self.btn_estop.setCursor(Qt.PointingHandCursor)
        self.btn_estop.clicked.connect(self._on_estop)
        tb.addWidget(self.btn_estop)

    def _build_statusbar(self):
        sb = self.statusBar()
        self.status_label = QLabel("Ready — connect hardware to begin")
        self.status_label.setStyleSheet(f"color:{MUTED};")
        sb.addWidget(self.status_label, 1)
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
                           ("Rin", "mΩ"), ("Temp", "°C"), ("SoH", "%")]:
            cards_row.addWidget(self._metric_card(name, unit), 1)
        lay.addLayout(cards_row)

        self._temp_gauge = TemperatureGauge()
        lay.addWidget(self._temp_gauge)

        self.trend = TrendContainer()
        lay.addWidget(self.trend, 2)
        return panel

    def _set_graph_mode(self, label: str):
        if not hasattr(self, "trend"):
            return
        modes = TrendContainer.MODES
        idx = modes.index(label) if label in modes else 0
        self.trend._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.trend._btn_group.buttons()):
            btn.setChecked(i == idx)

    def _on_about(self):
        if not self._headless:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.about(
                self,
                "About ASET Battery Tester",
                "ASET Battery Tester — ISA-101 Command Center\n\n"
                "มหาวิทยาลัยอุบลราชธานี  Faculty of Engineering — ASET Lab\n\n"
                "Built with PySide6 · Python",
            )

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
        tabs.addTab(_scroll(self._zone_setup()),     "1 · SETUP")
        tabs.addTab(_scroll(self._zone_test_mode()), "TEST MODE")
        tabs.addTab(_scroll(self._zone_tools()),     "3 · TOOLS")
        return tabs

    # ---- small UI helpers --------------------------------------------------
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

    def _estop_bar(self):
        self.btn_estop = QPushButton("⛔  EMERGENCY STOP")
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{CRIT}; color:white; border:none; border-radius:8px; padding:16px; font-size:16px; font-weight:800; }}"
            f"QPushButton:hover {{ background:#9b2020; }}"
        )
        self.btn_estop.setCursor(Qt.PointingHandCursor)
        self.btn_estop.clicked.connect(self._on_estop)
        return self.btn_estop

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
        self.lbl_battery_readout.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_battery_readout)
        actions = QHBoxLayout()
        self.btn_detect = _btn("Detect Chemistry", bg="#e0e2e4", hover="#d4d7da")
        self.btn_detect.clicked.connect(self._on_detect_chemistry)
        self.btn_save_default = _btn("Save as Default", bg="#d0d4d7", hover="#c2c6ca")
        self.btn_save_default.clicked.connect(self._on_save_default)
        actions.addWidget(self.btn_detect, 2)
        actions.addWidget(self.btn_save_default, 1)
        lay.addLayout(actions)
        btn_edit_profile = _btn("Edit Battery Profile…", bg="#e8f0fe", hover="#c5d8fd")
        btn_edit_profile.setToolTip("แก้ไขค่า BatteryConfig ในแอพโดยตรง")
        btn_edit_profile.clicked.connect(self._on_edit_battery_profile)
        lay.addWidget(btn_edit_profile)

        # Connections — each port row has a status LED (● gray=idle, ✓ green=ok, ✗ red=fail)
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("CONNECTIONS"))
        self.cb_psu = QComboBox()
        self.cb_load = QComboBox()
        self.cb_esp = QComboBox()

        def _led():
            lbl = QLabel("●")
            lbl.setStyleSheet(f"color:{NEUTRAL}; font-size:15px; min-width:18px;")
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
        self.btn_connect = _btn("Connect", bg=OK, fg="white", hover="#266a2a")
        self.btn_disconnect = _btn("Disconnect", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_disconnect)
        lay.addLayout(row)
        btn_refresh = _btn("Refresh Ports", bg="#d0d4d7", hover="#c2c6ca")
        btn_refresh.clicked.connect(self._refresh_ports)
        lay.addWidget(btn_refresh)

        # PSU Bleed compensation
        lay.addWidget(_hline())
        lay.addWidget(self._subheader("PSU COMPENSATION"))
        bleed_row = QHBoxLayout()
        bleed_lbl = QLabel("PSU bleed current:")
        bleed_row.addWidget(bleed_lbl)
        self.spn_psu_bleed = QDoubleSpinBox()
        self.spn_psu_bleed.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self.spn_psu_bleed.setRange(0.0, 5.0)
        self.spn_psu_bleed.setSingleStep(0.1)
        self.spn_psu_bleed.setDecimals(2)
        self.spn_psu_bleed.setSuffix(" A")
        self.spn_psu_bleed.setMinimumWidth(90)
        self.spn_psu_bleed.setToolTip(
            "กระแสที่ PSU ดูดผ่าน internal bleed resistor ตลอดเวลา\n"
            "ปรับด้วยลูกศร ▲▼ หรือพิมพ์ค่า  •  PSW 80-40.5 ≈ 0.60 A\n"
            "ตั้ง 0.00 ถ้าไม่มีปัญหา"
        )
        bleed_row.addWidget(self.spn_psu_bleed)
        bleed_row.addStretch(1)
        lay.addLayout(bleed_row)
        lbl_bleed_hint = QLabel(
            "ⓘ ปรับค่าได้ตามรุ่น PSU — เป็นกระแสที่ PSU ปล่อยทิ้งตลอดเวลา "
            "ระบบจะหักออกเพื่อให้ค่า discharge และ capacity แม่นยำ  •  ตั้งก่อนกด Connect")
        lbl_bleed_hint.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        lbl_bleed_hint.setWordWrap(True)
        lay.addWidget(lbl_bleed_hint)
        self.spn_psu_bleed.valueChanged.connect(self._on_psu_bleed_changed)

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
        ("1", "OCV",       "Calibrate SoC"),
        ("2", "REST",      "5 min settle"),
        ("3", "DISCHARGE", "1C rapid test"),
        ("4", "ANALYZE",   "Peukert SoH"),
    ]
    _HPPC_SEQ_STEPS = [
        ("1", "CHARGE",  "CC-CV to 100%"),
        ("2", "REST",    "OCV settle"),
        ("3", "HPPC",    "Pulse/relax cycles"),
        ("4", "ANALYZE", "R0/R1/C1/τ ECM"),
    ]
    _CYCLE_STEPS = [
        ("1", "CHARGE",    "CC-CV"),
        ("2", "DISCHARGE", "CC to cutoff"),
        ("3", "REPEAT",    "N cycles"),
        ("4", "ANALYZE",   "Capacity fade"),
    ]

    def _zone_workflow(self):
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(6)

        def _step_widget(steps_list, led_list, min_name_w, desc_list=None):
            sw = QWidget()
            sl = QVBoxLayout(sw)
            sl.setContentsMargins(0, 4, 0, 4)
            sl.setSpacing(2)
            for _num, name, desc in steps_list:
                row = QHBoxLayout()
                dot = QLabel("○")
                dot.setStyleSheet(f"color:{NEUTRAL}; font-size:16px; min-width:22px;")
                dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
                name_lbl = QLabel(name)
                name_lbl.setStyleSheet(
                    f"color:{MUTED}; font-weight:700; min-width:{min_name_w}px;"
                )
                desc_lbl = QLabel(desc)
                desc_lbl.setStyleSheet(f"color:{MUTED}; font-size:11px;")
                row.addWidget(dot)
                row.addWidget(name_lbl)
                row.addWidget(desc_lbl)
                row.addStretch(1)
                sl.addLayout(row)
                led_list.append((dot, name_lbl))
                if desc_list is not None:
                    desc_list.append(desc_lbl)
            return sw

        # ── Workflow selector dropdown ─────────────────────────
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Workflow:"))
        self.cb_workflow_type = QComboBox()
        self.cb_workflow_type.addItems([
            "IEC 61960 Standard  (~10–12h LeadAcid / ~8h Li-ion)",
            "Quick Scan  (~1.5h  Peukert-corrected SoH)",
            "HPPC Full Sequence  (~2–3h  R0/R1/C1/τ ECM)",
            "Cycle Life Test  (N × charge + discharge)",
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
        iec_lay.addWidget(_step_widget(self._WF_STEPS, self._wf_leds, 65, self._wf_desc_lbls))

        # separator
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setStyleSheet(f"color:{BORDER}; margin:2px 0;")
        iec_lay.addWidget(_sep)

        # Charge mode
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Charge mode:"))
        self.cb_charge_mode = QComboBox()
        self.cb_charge_mode.addItems(["Auto (by chemistry)", "CC-CV", "3-Stage (Lead-Acid)"])
        self.cb_charge_mode.setToolTip(
            "Auto — ใช้ strategy ของเคมีแบต  |  CC-CV — Lithium  |  3-Stage — Lead-Acid"
        )
        mode_row.addWidget(self.cb_charge_mode, 1)
        iec_lay.addLayout(mode_row)

        # Charge C-rate
        crate_row = QHBoxLayout()
        crate_row.addWidget(QLabel("Charge C-rate:"))
        self.cb_seq_crate = QComboBox()
        self.cb_seq_crate.addItems(["0.05C", "0.1C", "0.2C", "0.3C", "0.5C", "1.0C"])
        self.cb_seq_crate.setCurrentText("0.5C")
        self.lbl_seq_crate_a = QLabel("— A")
        self.lbl_seq_crate_a.setStyleSheet(
            f"color:{INFO}; font-weight:700; font-size:11px;"
        )
        crate_row.addWidget(self.cb_seq_crate)
        crate_row.addWidget(self.lbl_seq_crate_a)
        crate_row.addStretch(1)
        iec_lay.addLayout(crate_row)
        self.cb_seq_crate.currentTextChanged.connect(self._on_seq_crate_changed)

        # Stage breakdown
        self.lbl_charge_crate = QLabel("Charge rate: — (เลือกแบตก่อน)")
        self.lbl_charge_crate.setStyleSheet(
            f"color:{MUTED}; font-size:10px; padding-left:24px; padding-bottom:2px;"
        )
        self.lbl_charge_crate.setWordWrap(True)
        iec_lay.addWidget(self.lbl_charge_crate)

        # REST duration
        rest_row = QHBoxLayout()
        rest_row.addWidget(QLabel("REST duration:"))
        self.spn_rest_min = QSpinBox()
        self.spn_rest_min.setRange(5, 120)
        self.spn_rest_min.setValue(30)
        self.spn_rest_min.setSingleStep(5)
        self.spn_rest_min.setSuffix(" min")
        self.spn_rest_min.setToolTip("เวลา rest หลังชาร์จ ก่อนเริ่ม discharge test (5–120 นาที)")
        rest_row.addWidget(self.spn_rest_min)
        rest_row.addStretch(1)
        iec_lay.addLayout(rest_row)

        # Test discharge C-rate
        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("Test discharge:"))
        self.cb_test_crate = QComboBox()
        self.cb_test_crate.addItems(["0.1C", "0.2C", "0.5C", "1.0C"])
        self.cb_test_crate.setCurrentText("0.2C")
        self.cb_test_crate.setToolTip("C-rate สำหรับ IEC discharge test (มาตรฐาน = 0.2C)")
        self.lbl_test_crate_a = QLabel("— A")
        self.lbl_test_crate_a.setStyleSheet(
            f"color:{INFO}; font-weight:700; font-size:11px;"
        )
        test_row.addWidget(self.cb_test_crate)
        test_row.addWidget(self.lbl_test_crate_a)
        test_row.addStretch(1)
        iec_lay.addLayout(test_row)
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
        iec_lay.addLayout(skip_row)

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
        iec_lay.addLayout(soc_row)

        self.btn_auto_seq = _btn("▶  AUTO SEQUENCE", bg=INFO, fg="white", hover="#0d4a89")
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
        qs_lay.addWidget(_step_widget(self._QS_STEPS, self._qs_leds, 75, self._qs_desc_lbls))

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
        hppc_seq_lay.addWidget(_step_widget(
            self._HPPC_SEQ_STEPS, self._hppc_seq_leds, 65))

        hppc_seq_sep = QFrame()
        hppc_seq_sep.setFrameShape(QFrame.Shape.HLine)
        hppc_seq_sep.setStyleSheet(f"color:{BORDER}; margin:2px 0;")
        hppc_seq_lay.addWidget(hppc_seq_sep)

        hppc_seq_form = QFormLayout()
        hppc_seq_form.setSpacing(4)
        hppc_seq_form.setContentsMargins(0, 0, 0, 0)
        self.spn_hppc_cycles = QSpinBox()
        self.spn_hppc_cycles.setRange(1, 20)
        self.spn_hppc_cycles.setValue(5)
        self.spn_hppc_cycles.setToolTip(
            "Number of pulse/relax cycles — more cycles = better R1/C1 statistics")
        hppc_seq_form.addRow("HPPC cycles:", self.spn_hppc_cycles)
        hppc_seq_lay.addLayout(hppc_seq_form)

        hppc_seq_note = QLabel("Pulse/relax timing from MANUAL → HPPC tab")
        hppc_seq_note.setStyleSheet(f"color:{MUTED}; font-size:10px;")
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
        cycle_lay.addWidget(_step_widget(
            self._CYCLE_STEPS, self._cycle_leds, 75))

        cycle_sep = QFrame()
        cycle_sep.setFrameShape(QFrame.Shape.HLine)
        cycle_sep.setStyleSheet(f"color:{BORDER}; margin:2px 0;")
        cycle_lay.addWidget(cycle_sep)

        cycle_form = QFormLayout()
        cycle_form.setSpacing(4)
        cycle_form.setContentsMargins(0, 0, 0, 0)

        self.spn_cycle_n = QSpinBox()
        self.spn_cycle_n.setRange(1, 100)
        self.spn_cycle_n.setValue(3)
        self.spn_cycle_n.setToolTip("Total number of charge+discharge cycles")
        cycle_form.addRow("Cycles:", self.spn_cycle_n)

        self.cb_cycle_charge_crate = QComboBox()
        self.cb_cycle_charge_crate.addItems(["0.1C", "0.2C", "0.3C", "0.5C", "1.0C"])
        self.cb_cycle_charge_crate.setCurrentText("0.3C")
        cycle_form.addRow("Charge C-rate:", self.cb_cycle_charge_crate)

        self.cb_cycle_dis_crate = QComboBox()
        self.cb_cycle_dis_crate.addItems(["0.1C", "0.2C", "0.5C", "1.0C"])
        self.cb_cycle_dis_crate.setCurrentText("0.2C")
        cycle_form.addRow("Discharge C-rate:", self.cb_cycle_dis_crate)

        self.spn_cycle_rest = QSpinBox()
        self.spn_cycle_rest.setRange(1, 60)
        self.spn_cycle_rest.setValue(5)
        self.spn_cycle_rest.setSuffix(" min")
        self.spn_cycle_rest.setToolTip("Rest between charge and discharge in each cycle")
        cycle_form.addRow("Rest/cycle:", self.spn_cycle_rest)
        cycle_lay.addLayout(cycle_form)

        self.lbl_cycle_counter = QLabel("Cycle: —")
        self.lbl_cycle_counter.setStyleSheet(f"color:{INFO}; font-weight:700; font-size:11px;")
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

        self.cb_workflow_type.currentIndexChanged.connect(self._wf_stack.setCurrentIndex)

        # ── Shared CANCEL + status ────────────────────────────
        self.btn_seq_cancel = _btn("■  CANCEL", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_seq_cancel.setEnabled(False)
        self.btn_seq_cancel.clicked.connect(self._on_seq_cancel)
        self._buttons["btn_seq_cancel"] = self.btn_seq_cancel
        outer_lay.addWidget(self.btn_seq_cancel)

        self.lbl_wf_status = QLabel("เลือก workflow แล้วกดปุ่ม RUN")
        self.lbl_wf_status.setStyleSheet(
            f"color:{MUTED}; font-size:11px; padding-top:2px;"
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
            f"QProgressBar{{border:1px solid {BORDER};border-radius:3px;"
            f"background:{PANEL2};text-align:center;font-size:9px;}}"
            f"QProgressBar::chunk{{background:{INFO};border-radius:2px;}}"
        )
        self.wf_progress.hide()
        outer_lay.addWidget(self.wf_progress)

        self.lbl_eta = QLabel("")
        self.lbl_eta.setStyleSheet(f"color:{INFO}; font-size:10px; font-weight:600;")
        self.lbl_eta.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_eta.hide()
        outer_lay.addWidget(self.lbl_eta)

        # Inline result card — shown after sequence completes
        self.frm_seq_result = QFrame()
        self.frm_seq_result.setStyleSheet(
            f"QFrame{{background:{PANEL2};border:1px solid {INFO};"
            f"border-radius:5px;padding:4px 8px;}}"
        )
        result_lay = QVBoxLayout(self.frm_seq_result)
        result_lay.setContentsMargins(4, 4, 4, 4)
        result_lay.setSpacing(2)
        self.lbl_seq_result = QLabel("—")
        self.lbl_seq_result.setStyleSheet(f"color:{TEXT}; font-size:11px; font-weight:600;")
        self.lbl_seq_result.setWordWrap(True)
        result_lay.addWidget(self.lbl_seq_result)
        self.frm_seq_result.hide()
        outer_lay.addWidget(self.frm_seq_result)

        # ── IEC Profiles (moved from 3·TOOLS → Profile tab) ──────────────
        outer_lay.addWidget(_hline())
        outer_lay.addWidget(self._subheader("IEC PROFILES"))
        prow_sel = QHBoxLayout()
        prow_sel.addWidget(QLabel("Profile:"))
        self.cb_profiles = QComboBox()
        self._populate_profiles()
        self._combo_shrink(self.cb_profiles, 10)
        prow_sel.addWidget(self.cb_profiles, 1)
        outer_lay.addLayout(prow_sel)
        prow = QHBoxLayout()
        self.btn_start_profile = _btn("RUN", bg=INFO, fg="white", hover="#0d4a89")
        self.btn_start_profile.clicked.connect(self._on_run_profile)
        self.btn_stop_profile = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_stop_profile.clicked.connect(
            lambda: self.controller and self.controller.stop_profile())
        self._buttons["btn_start_profile"] = self.btn_start_profile
        prow.addWidget(self.btn_start_profile)
        prow.addWidget(self.btn_stop_profile)
        outer_lay.addLayout(prow)
        self.lbl_profile_status = QLabel("No profile selected")
        self.lbl_profile_status.setStyleSheet(f"color:{MUTED};")
        outer_lay.addWidget(self.lbl_profile_status)

        return outer

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
        self.lbl_run_grade.setStyleSheet(f"color:{MUTED}; padding-top:4px;")
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
        psu_on  = _btn("ON",  bg=OK,       fg="white", hover="#266a2a")
        psu_off = _btn("OFF", bg="#d0d4d7",            hover="#c2c6ca")
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
        load_on  = _btn("ON",  bg=OK,       fg="white", hover="#266a2a")
        load_off = _btn("OFF", bg="#d0d4d7",            hover="#c2c6ca")
        load_on.clicked.connect( lambda: self._load_manual(True))
        load_off.clicked.connect(lambda: self._load_manual(False))
        load_row.addWidget(load_on)
        load_row.addWidget(load_off)
        lay.addLayout(load_row)

        note = QLabel("⚠  ใช้เฉพาะทดสอบฮาร์ดแวร์  —  ไม่มี SoC หรือ safety interlock")
        note.setStyleSheet(f"color:{WARN}; font-size:10px;")
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
        self.btn_ocv = _btn("OCV CALIBRATE", bg=WARN, fg="white", hover="#a06800")
        self.btn_ocv.setToolTip(
            "Turn off PSU & Load, wait 3 s, read OCV → set correct SoC.\n"
            "Press before CHARGE to fix the SOC display."
        )
        self.btn_ocv.clicked.connect(self._on_ocv_calibrate)
        self._buttons["btn_ocv"] = self.btn_ocv   # register for sig_loading
        ocv_row.addWidget(self.btn_ocv)
        lay.addLayout(ocv_row)
        crow = QHBoxLayout()
        self.btn_charge = _btn("CHARGE", bg=OK, fg="white", hover="#266a2a")
        self.btn_stop_charge = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_charge.clicked.connect(self._on_charge)
        self.btn_stop_charge.clicked.connect(self._on_stop_charge)
        crow.addWidget(self.btn_charge, 2)
        crow.addWidget(self.btn_stop_charge, 1)
        lay.addLayout(crow)
        self.lbl_charge = QLabel("Charge idle")
        self.lbl_charge.setStyleSheet(f"color:{MUTED};")
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
        crow2 = QHBoxLayout()
        self.btn_run_test = _btn("RUN TEST", bg=INFO, fg="white", hover="#0d4a89")
        self.btn_run_test.clicked.connect(self._on_run_test)
        self.btn_stop_test = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_stop_test.clicked.connect(self._on_stop_test)
        crow2.addWidget(self.btn_run_test, 2)
        crow2.addWidget(self.btn_stop_test, 1)
        lay.addLayout(crow2)
        self.lbl_test_status = QLabel("Test idle")
        self.lbl_test_status.setStyleSheet(f"color:{MUTED};")
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
        note.setStyleSheet(f"color:{MUTED}; font-size:10px;")
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

        self.ed_hppc_pulse = QLineEdit("30")
        self.ed_hppc_pulse.setValidator(QDoubleValidator(1.0, 600.0, 1))
        self.ed_hppc_pulse.setToolTip(
            "Pulse duration (s)\nLead-acid τ ≈ 10–60 s → use ≥ 30 s to resolve R₁/C₁"
        )
        form.addRow("Pulse (s):", self.ed_hppc_pulse)

        self.ed_hppc_relax = QLineEdit("30")
        self.ed_hppc_relax.setValidator(QDoubleValidator(1.0, 600.0, 1))
        self.ed_hppc_relax.setToolTip("Rest/relaxation duration (s) between pulses")
        form.addRow("Relax (s):", self.ed_hppc_relax)

        lay.addLayout(form)

        # Phase indicator — updates live via _on_hppc_telemetry
        self.lbl_hppc_phase = QLabel("IDLE")
        self.lbl_hppc_phase.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_hppc_phase.setStyleSheet(
            f"background:{PANEL2}; color:{MUTED}; border:1px solid {BORDER}; "
            f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
        )
        lay.addWidget(self.lbl_hppc_phase)

        brow = QHBoxLayout()
        self.btn_run_hppc = _btn("RUN HPPC", bg=INFO, fg="white", hover="#0d4a89")
        self.btn_run_hppc.clicked.connect(self._on_run_hppc)
        self._buttons["btn_run_hppc"] = self.btn_run_hppc
        self.btn_stop_hppc = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_stop_hppc.clicked.connect(self._on_stop_test)
        brow.addWidget(self.btn_run_hppc, 2)
        brow.addWidget(self.btn_stop_hppc, 1)
        lay.addLayout(brow)

        lay.addStretch(1)
        return w

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

    # ---- ZONE: TEST MODE — AUTO tab (workflow) + MANUAL tab (charge/discharge) --
    def _zone_test_mode(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setStyleSheet(
            f"QTabBar::tab {{ padding:5px 18px; }} "
            f"QTabBar::tab:selected {{ font-weight:700; }}"
        )
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
        self.btn_start_monitor = _btn("START MONITOR", bg=OK, fg="white", hover="#266a2a")
        self.btn_stop_monitor  = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_start_monitor.clicked.connect(self._on_start_monitor)
        self.btn_stop_monitor.clicked.connect(
            lambda: self.controller and self.controller.stop_monitor())
        self._buttons["btn_start_monitor"] = self.btn_start_monitor
        self.btn_start_monitor.hide()
        self.btn_stop_monitor.hide()

        # ── Data / Reporting ────────────────────────────────────────────────
        lay.addWidget(self._subheader("DATA"))
        self.lbl_csv = QLabel("CSV: —")
        self.lbl_csv.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.lbl_csv.setWordWrap(True)
        lay.addWidget(self.lbl_csv)

        self.btn_log = _btn("START DATA LOGGING", bg="#d0d4d7", hover="#c2c6ca")
        self.btn_log.clicked.connect(self._on_toggle_logging)
        lay.addWidget(self.btn_log)
        self.btn_pdf = _btn("Generate PDF Report", bg=PANEL2, hover=FIELD)
        self.btn_pdf.clicked.connect(self._on_pdf_report)
        lay.addWidget(self.btn_pdf)
        btn_dash = _btn("Open Cloud Dashboard", bg="#d0d4d7", hover="#c2c6ca")
        btn_dash.clicked.connect(self._on_open_dashboard)
        lay.addWidget(btn_dash)

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

        lay.addStretch(1)
        return w

    def _build_right_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(300)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        self._right_tabs = QTabWidget()
        self._right_tabs.addTab(self._tab_analytics(), "Analytics")
        self._right_tabs.addTab(self._tab_diagnostics(), "Diagnostics (ICA/DTV)")
        self._right_tabs.addTab(self._tab_alarms(), "Alarm Log")
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
        self.plot_ica.setBackground(PANEL2)
        self.plot_ica.setLabel("bottom", "Voltage", units="V")
        self.plot_ica.setLabel("left", "dQ/dV")
        self.plot_ica.setTitle("ICA (Incremental Capacity)")
        lay.addWidget(self.plot_ica, 1)
        return w

    def _metric_card(self, name, unit):
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-top:2px solid {INFO}; border-radius:6px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(2)
        t = QLabel(name.upper())
        t.setStyleSheet(f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;")
        # SoH is a final-analysis metric (not live); Rin is only valid under load.
        # Both start "pending" so a placeholder number is never mistaken for a reading.
        val = QLabel("—" if name in ("SoH", "Rin") else f"0.0 {unit}")
        val.setFont(QFont("Consolas", 19, QFont.Weight.Bold))
        val.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(t)
        lay.addWidget(val)
        self.metric_labels[name] = (val, unit)
        # Current card: add a direction badge below the number (CHG / DSG / REST)
        if name == "Current":
            self._lbl_i_dir = QLabel("—")
            self._lbl_i_dir.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            self._lbl_i_dir.setStyleSheet(f"color:{MUTED}; border:0;")
            lay.addWidget(self._lbl_i_dir)
        return card

    def _tab_analytics(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(4)

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
        self.lbl_analytics.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_analytics)

        # วงจร Thevenin ECM
        self.btn_ecm_toggle = QPushButton("▶ Show Equivalent Circuit")
        self.btn_ecm_toggle.setCheckable(True)
        self.btn_ecm_toggle.setEnabled(False)
        self.btn_ecm_toggle.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{MUTED};border:1px solid {MUTED};"
            f"border-radius:4px;padding:3px 8px;text-align:left;}}"
            f"QPushButton:checked{{background:{PANEL};color:{TEXT};border-color:{INFO};}}"
            f"QPushButton:enabled:hover{{border-color:#aaa;}}"
        )
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
        self.lbl_grade.setStyleSheet(f"background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; border-radius:6px; padding:10px;")
        lay.addWidget(self.lbl_grade)
        btn = _btn("Analyze Last CSV", bg=INFO, fg="white", hover="#0d4a89")
        btn.clicked.connect(self._on_analyze_csv)
        lay.addWidget(btn)

        # ── SoH trend + capacity fade charts ─────────────────────────────
        trend_row = QHBoxLayout()
        btn_trend = _btn("SoH Trend", bg=PANEL2, hover=FIELD)
        btn_trend.setToolTip("Plot SoH history across all sessions")
        btn_trend.clicked.connect(self._on_soh_trend)
        btn_fade = _btn("Capacity Fade", bg=PANEL2, hover=FIELD)
        btn_fade.setToolTip("Plot capacity fade from Cycle Life sessions")
        btn_fade.clicked.connect(self._on_capacity_fade)
        trend_row.addWidget(btn_trend)
        trend_row.addWidget(btn_fade)
        lay.addLayout(trend_row)
        return w

    def _build_ecm_svg(self, r0=None, r1=None, c1=None, ocv=None, tau=None) -> str:
        """วงจรสมมูลแบตเตอรี่ (Thévenin 1-RC) ตามรูปตำรา:

            V_oc ── R_I ──┬── R_d ──┬── + (V_t)
                          └── C_d ──┘

        ค่าที่เป็น None จะแสดงเป็นตัวแปร (สัญลักษณ์เปล่า) — ใช้กับการทดสอบที่
        ไม่ใช่ HPPC ซึ่งระบุ R_d/C_d ไม่ได้.
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
            sym(182, y_main - 18, "R", "I"),
            val(182, y_main + 26, ri_txt),

            # ── กิ่งล่าง: R_d (อยู่บนเส้นหลัก) ──
            wire(nA, y_main, 272, y_main),
            resistor(272, 337, y_main),
            wire(337, y_main, nB, y_main),
            sym(304, y_main + 28, "R", "d"),
            val(304, y_main + 43, rd_txt),

            # ── กิ่งบน: C_d (กิ่งขนานยกขึ้น) ──
            wire(nA, y_main, nA, y_cap),
            wire(nA, y_cap, 296, y_cap),
            capacitor(304, y_cap),
            wire(312, y_cap, nB, y_cap),
            wire(nB, y_cap, nB, y_main),
            sym(304, y_cap - 16, "C", "d"),
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
                f'font-size="10" fill="{muted}">Non-HPPC test — Rd, Cd shown as symbols '
                f'(not identifiable without pulses)</text>'
            )
        elif tau is not None:
            parts.append(
                f'<text x="{W//2}" y="{H-10}" text-anchor="middle" font-family="Consolas, monospace" '
                f'font-size="10" fill="{muted}">1-RC Thévenin model · τ = Rd·Cd = {tau:.1f} s</text>'
            )

        parts.append("</svg>")
        return "".join(parts)

    def _build_results_html(self, results: dict) -> str:
        """Rich HTML table for the analytics results pane."""
        grade = results["grade"]
        gc = {"A": OK, "B": INFO, "C": WARN, "REJECT": CRIT, "REVIEW": NEUTRAL}.get(grade, NEUTRAL)
        soh = results["soh"]
        soh_txt = "N/A" if soh != soh else f"{soh:.1f}"
        conf = results.get("confidence", 1.0)
        dcir = results.get("dcir_mohm", results.get("ri_mohm", 0.0))
        dstd = results.get("dcir_std_mohm", 0.0)
        nstep = results.get("dcir_n_steps", 0)
        ocv = results.get("ocv_v", 0.0)
        cap_ah = results["capacity_ah"]
        cap_norm = results.get("capacity_norm_ah")
        warns = results.get("quality_warnings", [])

        def hdr(text):
            return (
                f'<tr><td colspan="2" style="background:{PANEL2};padding:5px 8px;'
                f'font-weight:bold;color:{TEXT};font-size:11px;'
                f'border-top:2px solid {BORDER};border-bottom:1px solid {BORDER}">'
                f'{text}</td></tr>'
            )

        def row(label, value, unit="", sub=""):
            sub_html = (
                f'<br><span style="font-size:9px;color:{MUTED}">{sub}</span>'
            ) if sub else ""
            return (
                f'<tr>'
                f'<td style="padding:4px 8px 4px 14px;color:{MUTED};font-size:11px;vertical-align:top">'
                f'{label}</td>'
                f'<td style="padding:4px 8px;color:{INFO};font-family:Consolas,monospace;'
                f'font-size:12px;font-weight:bold;vertical-align:top">'
                f'{value}'
                f'<span style="color:{MUTED};font-size:10px;font-weight:normal"> {unit}</span>'
                f'{sub_html}</td>'
                f'</tr>'
            )

        parts = [
            '<table width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;">'
        ]

        # ── Summary ──
        parts.append(hdr("Summary"))
        parts.append(row(
            "Grade",
            f'<span style="color:{gc};font-size:14px">{grade}</span>',
            f'conf {conf * 100:.0f}%'
        ))
        parts.append(row("State of Health", soh_txt, "%"))
        cap_sub = ""
        if cap_norm and abs(cap_norm - cap_ah) > 1e-4:
            k = results.get("peukert_k", 1.1)
            i_avg = results.get("mean_discharge_a", 0)
            cap_sub = f"rate-norm. {cap_norm:.3f} Ah @ k={k:.2f}, Ī={i_avg:.1f} A"
        parts.append(row("Capacity", f"{cap_ah:.3f}", "Ah", cap_sub))
        parts.append(row("Rested OCV", f"{ocv:.3f}", "V"))

        # ── DCIR ──
        parts.append(hdr("Resistance &amp; Cranking  (DCIR @ ~250 ms, norm. 25 °C)"))
        meas_hint = "" if results.get("dcir_measured", True) else "no current step → profile baseline"
        step_sub = f"n={nstep} step{'s' if nstep != 1 else ''}" + (
            f"  {meas_hint}" if meas_hint else ""
        )
        parts.append(row("DCIR", f"{dcir:.2f} ± {dstd:.2f}", "mΩ", step_sub))
        parts.append(row("Voltage sag (load)", f"{results.get('voltage_sag_v', 0.0):.3f}", "V"))
        parts.append(row("CCA proxy", f"{results.get('cca_est_a', 0.0):.0f}", "A",
                         "(OCV − cutoff) / DCIR"))
        slope = results.get("dcir_slope_mohm")
        if slope is not None and slope == slope and results.get("dcir_slope_r2", 0) >= 0.9:
            parts.append(row("DCIR (V–I slope)", f"{slope:.2f}", "mΩ",
                             f"R² {results['dcir_slope_r2']:.3f}, OCV-cancelled"))

        # ── ECM (HPPC only) ──
        if results.get("ecm_identified"):
            r2 = results.get("ecm_r2", 0.0)
            parts.append(hdr(f"1-RC Thévenin ECM  (HPPC, R² {r2:.3f})"))
            parts.append(row("R₀  (ohmic, t=0 extrap.)", f"{results['r0_mohm']:.2f}", "mΩ"))
            parts.append(row("R₁  (polarisation)", f"{results['r1_mohm']:.2f}", "mΩ"))
            parts.append(row("C₁", f"{results['c1_farad']:.0f}", "F"))
            parts.append(row("τ  (R₁·C₁)", f"{results['tau_s']:.1f}", "s"))
            parts.append(row("Total (R₀+R₁)", f"{results['ri_mohm']:.2f}", "mΩ"))

        # ── Quality flags ──
        if warns:
            parts.append(hdr("⚠ Data Quality Flags"))
            for w in warns:
                parts.append(
                    f'<tr><td colspan="2" style="padding:3px 14px;color:{CRIT};font-size:11px">'
                    f'• {w}</td></tr>'
                )

        parts.append('</table>')
        return "".join(parts)

    def _tab_alarms(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(0)
        lay.setContentsMargins(0, 0, 0, 0)

        # ── Header bar ────────────────────────────────────────────────
        hdr = QFrame()
        hdr.setStyleSheet(f"background:{PANEL}; border-bottom:1px solid #888;")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(8, 5, 8, 5)
        lbl_title = QLabel("EVENT / ALARM LOG")
        lbl_title.setStyleSheet(f"font-weight:700; font-size:12px; color:{TEXT}; border:0; background:transparent;")
        hdr_lay.addWidget(lbl_title)
        hdr_lay.addStretch()
        lbl_count = QLabel("0 events")
        lbl_count.setObjectName("alarm_count")
        lbl_count.setStyleSheet(f"color:{MUTED}; font-size:10px; border:0; background:transparent;")
        self._alarm_count_lbl = lbl_count
        hdr_lay.addWidget(lbl_count)
        hdr_lay.addSpacing(12)
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedSize(60, 24)
        btn_clear.setStyleSheet(
            f"QPushButton{{background:{PANEL2};border:1px solid #999;border-radius:3px;font-size:10px;}}"
            f"QPushButton:hover{{background:{FIELD};}}"
        )
        btn_clear.clicked.connect(self._alarm_clear)
        hdr_lay.addWidget(btn_clear)
        lay.addWidget(hdr)

        # ── Table ─────────────────────────────────────────────────────
        self.tbl_alarms = QTableWidget()
        self.tbl_alarms.setColumnCount(4)
        self.tbl_alarms.setHorizontalHeaderLabels(["DATE/TIME", "POINT NAME", "STATE", "EVENT"])
        hh = self.tbl_alarms.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
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
        self.tbl_alarms.setRowCount(0)
        self._alarm_count_lbl.setText("0 events")
        self._alarm_statusbar.setText("  LOG CLEARED")
        self._alarm_statusbar.setStyleSheet(
            "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
            " font-family:Consolas,monospace; border-top:1px solid #333;"
        )

    def _connect_signals(self):
        self.sig_display.connect(self._slot_display)
        self.sig_profile_status.connect(self._slot_profile_status)
        self.sig_charge_status.connect(self._slot_charge_status)
        self.sig_button.connect(self._slot_button)
        self.sig_loading.connect(self._slot_loading)
        self.sig_conn.connect(self._slot_conn)
        self.sig_alarm.connect(self._log_alarm)
        self.sig_safety.connect(self._slot_safety)
        self.sig_profile_done.connect(self._slot_profile_done)
        self.sig_analysis_done.connect(self._slot_analysis_done)
        self.sig_workflow.connect(self._slot_workflow)
        self.sig_qs_workflow.connect(self._slot_qs_workflow)
        self.sig_hppc_seq_wf.connect(self._slot_hppc_seq_wf)
        self.sig_cycle_wf.connect(self._slot_cycle_wf)
        self.sig_wf_status.connect(self._slot_wf_status)
        self.sig_phase_progress.connect(self._slot_phase_progress)
        self.sig_seq_result.connect(self._slot_seq_result)
        self.sig_seq_done.connect(self._slot_seq_done)
        self.sig_char_update.connect(self._slot_char_update)

    def update_display(self, v, i, soc, rin, temp=None, soh=None):
        if temp is None:
            temp = getattr(self.hw, "current_temp", 25.0)
        if soh is None:
            soh = getattr(self.estimator, "soh", 100.0)
        self.sig_display.emit(float(v), float(i), float(soc), float(rin), float(temp), float(soh))

    def set_profile_status(self, text, color=None):
        self.sig_profile_status.emit(str(text), str(color or MUTED))

    def set_charge_status(self, text):
        self.sig_charge_status.emit(str(text))

    def set_button_enabled(self, key, enabled):
        self.sig_button.emit(str(key), bool(enabled))

    def set_loading_state(self, key, loading, text=None):
        self.sig_loading.emit(str(key), bool(loading), str(text or ""))

    def _update_connection_status(self):
        self.sig_conn.emit()

    def update_status_bar(self):
        self._update_connection_status()

    def handle_safety_trigger(self, reason):
        self.sig_safety.emit(str(reason))

    def handle_profile_completed(self, data):
        self.sig_profile_done.emit(data)

    def handle_analysis_completed(self, result):
        self.sig_analysis_done.emit(result)

    @Slot(float, float, float, float, float, float)
    def _slot_display(self, v, i, soc, rin, temp, soh):
        rin_mohm = rin * 1000.0
        # LIVE metrics (valid every sample): Voltage, SoC, Temp — shown with signed value.
        for name, val, fmt in [("Voltage", v, "{:.2f}"), ("SoC", soc, "{:.1f}"), ("Temp", temp, "{:.2f}")]:
            lbl, unit = self.metric_labels[name]
            lbl.setText(f"{fmt.format(val)} {unit}")
        # Current: always show absolute value; direction shown by badge + color.
        i_lbl, i_unit = self.metric_labels["Current"]
        i_lbl.setText(f"{abs(i):.2f} {i_unit}")
        _IDLE = 0.05   # A — threshold below which current is considered "at rest"
        if i < -_IDLE:                              # charging (convention: negative)
            i_lbl.setStyleSheet(f"color:{INFO}; border:0;")
            self._lbl_i_dir.setText("▲  CHG")
            self._lbl_i_dir.setStyleSheet(f"color:{INFO}; border:0;")
        elif i > _IDLE:                             # discharging (convention: positive)
            i_lbl.setStyleSheet(f"color:{WARN}; border:0;")
            self._lbl_i_dir.setText("▼  DSG")
            self._lbl_i_dir.setStyleSheet(f"color:{WARN}; border:0;")
        else:                                       # at rest
            i_lbl.setStyleSheet(f"color:{TEXT}; border:0;")
            self._lbl_i_dir.setText("—  REST")
            self._lbl_i_dir.setStyleSheet(f"color:{MUTED}; border:0;")
        # Rin: a DC resistance reading needs current flowing. At rest, (OCV−V)/I is
        # undefined and explodes on the flat LFP plateau → keep "pending" rather than
        # show a wild number. The final analysis fills the proper R0+R1.
        rin_lbl, rin_unit = self.metric_labels["Rin"]
        if abs(i) >= 0.1:
            rin_lbl.setText(f"{rin_mohm:.2f} {rin_unit}")
        # SoH is intentionally NOT updated here — it is a final-analysis metric,
        # written once by _on_test_finished. (soh arg is kept for signal compatibility.)

        if self._elapsed_t0 is None:
            self._elapsed_t0 = datetime.now().timestamp()
        elapsed = datetime.now().timestamp() - self._elapsed_t0

        self.buf_t.append(elapsed)
        self.buf_v.append(v)
        self.buf_i.append(i)
        self.buf_soc.append(soc)
        self.buf_rin.append(rin_mohm)
        self.buf_temp.append(temp)
        self._sample_index += 1
        self.trend.update(list(self.buf_t), list(self.buf_v), list(self.buf_i), list(self.buf_temp))
        self._cloud_push_update(v, i, soc, temp)

        self._update_temp_gauge(temp)
        i_dir = "CHG" if i < -_IDLE else "DSG" if i > _IDLE else "REST"
        self.status_label.setText(
            f"V={v:.2f} V  I={abs(i):.2f} A ({i_dir})  SoC={soc:.1f}%  Rin={rin_mohm:.1f} mΩ  Temp={temp:.1f} °C"
        )

    def _update_temp_gauge(self, temp):
        if hasattr(self, "_temp_gauge") and self._temp_gauge is not None:
            self._temp_gauge.update_temp(temp, warn=35.0, crit=45.0)

    @Slot(str, str)
    def _slot_profile_status(self, text, color):
        self.lbl_profile_status.setText(text)
        self.lbl_profile_status.setStyleSheet(f"color:{color};")
        self.state_pill.setText(f"  {text.upper()}  ")
        pill_color = INFO if "RUN" in text.upper() else CRIT if "STOP" in text.upper() or "FAIL" in text.upper() else NEUTRAL
        self.state_pill.setStyleSheet(self._pill(pill_color))

    @Slot(str)
    def _slot_charge_status(self, text):
        self.lbl_charge.setText(text)

    @Slot(str, bool)
    def _slot_button(self, key, enabled):
        b = self._buttons.get(key)
        if b is not None:
            b.setEnabled(enabled)

    @Slot(str, bool, str)
    def _slot_loading(self, key, loading, text):
        b = self._buttons.get(key)
        if b is None:
            return
        if loading:
            b._orig = b.text()
            b.setText(text or "…")
            b.setEnabled(False)
        else:
            b.setText(getattr(b, "_orig", b.text()))
            b.setEnabled(True)

    @Slot()
    def _slot_conn(self):
        connected  = bool(getattr(self.hw, "is_connected", False))
        esp_ok     = bool(getattr(self.hw, "is_esp_connected", False))
        conn_err   = getattr(self.hw, "connect_error", "")
        esp_err    = getattr(self.hw, "esp_connect_error", "")
        # Header LED
        if connected:
            led_color, conn_label = OK, "Connected"
        elif conn_err:
            led_color, conn_label = CRIT, "Connection Failed"
        else:
            led_color, conn_label = NEUTRAL, "Disconnected"
        self.conn_led.setStyleSheet(f"color:{led_color}; font-size:16px;")
        self.conn_text.setText(conn_label)
        self.conn_text.setStyleSheet(f"color:{led_color}; font-weight:600;")
        if connected:
            self.status_label.setText("Hardware connected")
        elif conn_err:
            self.status_label.setText(f"เชื่อมต่อล้มเหลว: {conn_err.splitlines()[0]}")
        else:
            self.status_label.setText("Ready — connect hardware to begin")
        # Per-port LEDs: ✓ connected | ✗ error | ● idle
        def _set_led(lbl, ok, err, tip_ok, tip_err, tip_no):
            if ok:
                lbl.setText("✓")
                lbl.setStyleSheet(f"color:{OK}; font-size:13px; min-width:18px; font-weight:700;")
                lbl.setToolTip(tip_ok)
            elif err:
                lbl.setText("✗")
                lbl.setStyleSheet(f"color:{CRIT}; font-size:13px; min-width:18px; font-weight:700;")
                lbl.setToolTip(tip_err)
            else:
                lbl.setText("●")
                lbl.setStyleSheet(f"color:{NEUTRAL}; font-size:15px; min-width:18px;")
                lbl.setToolTip(tip_no)
        _set_led(self.led_psu,  connected, conn_err, "PSU connected",   conn_err,  "PSU: not connected")
        _set_led(self.led_load, connected, conn_err, "Load connected",  conn_err,  "Load: not connected")
        _set_led(self.led_esp,  esp_ok,    esp_err,  "ESP32 connected", esp_err,   "ESP32: not connected")

    @Slot(str)
    def _slot_safety(self, reason):
        self._log_alarm(f"⛔ SAFETY: {reason}")
        self.state_pill.setText("  ESTOP  ")
        self.state_pill.setStyleSheet(self._pill(CRIT))
        if not self._headless:
            QMessageBox.critical(self, "Safety Triggered", f"System safety triggered:\n{reason}\n\nAll operations stopped.")

    @Slot(object)
    def _slot_profile_done(self, data):
        success = data if isinstance(data, bool) else data.get("success", False)
        if success and isinstance(data, dict) and data.get("report") and not self._headless:
            self._show_text_dialog("IEC 61960 Test Report", data["report"])
        elif success:
            self._log_alarm("Profile completed.")
        else:
            err = data.get("error", "") if isinstance(data, dict) else ""
            self._log_alarm(err or "Profile stopped.")

    @Slot(object)
    def _slot_analysis_done(self, result):
        """Display a unified-analysis result (dict). Same renderer as a live test
        — Analyze-CSV and the controller's auto-analyze both arrive here."""
        if not isinstance(result, dict) or "error" in result:
            msg = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
            self.lbl_analytics.setText(f"Analysis failed: {msg}")
            self._log_alarm(f"Analysis failed: {msg}")
            return
        self._last_analysis = result
        self._on_test_finished(result)

    def _log_alarm(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        m = msg.strip()
        m_low = m.lower()

        # ── Classify event ─────────────────────────────────────────
        if any(x in m_low for x in ["safety", "estop", "e-stop", "fail", "error",
                                      "abort", "⛔", "alarm", "overvolt", "underv",
                                      "overtemp", "otp"]):
            event, state = "ALARM",   "ACTIVE"
            row_bg, row_fg, evt_fg = "#3D1A1A", "#E0E3E6", "#FF5555"
        elif any(x in m_low for x in ["warn", "⚠", "timeout", "timeout"]):
            event, state = "WARNING", "ACTIVE"
            row_bg, row_fg, evt_fg = "#3D3010", "#E0E3E6", "#FFB700"
        elif any(x in m_low for x in ["complete", "✓", "success", "connected",
                                        "ready", "done", "normal"]):
            event, state = "NORMAL",  "CLEARED"
            row_bg, row_fg, evt_fg = "#1A2E1A", "#E0E3E6", "#55CC55"
        elif any(x in m_low for x in ["start", "started", "enable", "begin",
                                        "on ", "charge started", "discharge"]):
            event, state = "ON",      "ACTIVE"
            row_bg, row_fg, evt_fg = "#1A2240", "#E0E3E6", "#5599FF"
        elif any(x in m_low for x in ["stop", "stopped", "disable", "disconnected",
                                        "cancel", "off"]):
            event, state = "OFF",     "INACTIVE"
            row_bg, row_fg, evt_fg = "#282828", "#A8A8A8", "#888888"
        else:
            event, state = "INFO",    ""
            row_bg, row_fg, evt_fg = "#1C1F23", "#C0C4C8", "#7A9A5A"

        # ── Parse POINTNAME ────────────────────────────────────────
        prefix_m = re.match(r'^\[([^\]]+)\]\s*', m)
        if prefix_m:
            prefix = prefix_m.group(1)
            body   = m[prefix_m.end():]
            point  = f"{prefix} · {body}" if body else prefix
        else:
            point = m

        # ── Insert row ─────────────────────────────────────────────
        if not hasattr(self, "tbl_alarms"):
            return
        tbl = self.tbl_alarms
        row = tbl.rowCount()
        tbl.insertRow(row)

        bg = QColor(row_bg)
        fg = QColor(row_fg)
        for col, (text, bold) in enumerate([
            (ts,    False),
            (point, False),
            (state, False),
            (event, True),
        ]):
            item = QTableWidgetItem(text)
            item.setBackground(bg)
            item.setForeground(fg if col != 3 else QColor(evt_fg))
            if bold:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            tbl.setItem(row, col, item)
        tbl.setRowHeight(row, 22)
        tbl.scrollToBottom()

        # ── Update header count & status bar ───────────────────────
        n = tbl.rowCount()
        if hasattr(self, "_alarm_count_lbl"):
            self._alarm_count_lbl.setText(f"{n} events")
        if hasattr(self, "_alarm_statusbar"):
            if event == "ALARM":
                self._alarm_statusbar.setText(f"  ⛔  ALARM ACTIVE — {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#7A0000; color:#FFCCCC; padding:3px 10px; font-size:10px;"
                    " font-weight:700; font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            elif event == "WARNING":
                self._alarm_statusbar.setText(f"  ⚠  WARNING — {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#5A4000; color:#FFE080; padding:3px 10px; font-size:10px;"
                    " font-weight:700; font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            elif event == "NORMAL":
                self._alarm_statusbar.setText(f"  ✓  {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
                    " font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            else:
                self._alarm_statusbar.setText(f"  {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
                    " font-family:Consolas,monospace; border-top:1px solid #333;"
                )

    def _refresh_ports(self):
        if self.hw is None:
            return
        try:
            visa = self.hw.get_visa_ports() if hasattr(self.hw, "get_visa_ports") else []
            coms = self.hw.get_com_ports() if hasattr(self.hw, "get_com_ports") else []
            for cb, items in ((self.cb_psu, visa), (self.cb_load, visa), (self.cb_esp, coms)):
                cb.clear()
                cb.addItems(items)
            # Restore saved selections from config; fall back to positional defaults
            hw = self.config.hardware if self.config else None
            def _restore(cb, saved):
                if saved:
                    idx = cb.findText(saved)
                    if idx >= 0:
                        cb.setCurrentIndex(idx)
            if hw:
                _restore(self.cb_psu, hw.psu_port)
                _restore(self.cb_load, hw.load_port)
                _restore(self.cb_esp, hw.esp_port)
            elif len(visa) > 1:
                self.cb_load.setCurrentIndex(1)
        except Exception as exc:
            logger.error("refresh ports: %s", exc)

    def _refresh_battery_readout(self):
        b = self.config.battery
        self.lbl_battery_readout.setText(
            f"{b.battery_type} · {b.cells_series}S{b.cells_parallel}P · {b.pack_nominal_voltage:.1f}V · {b.rated_capacity:.1f}Ah"
        )

    def _on_product_changed(self, name):
        prod = battery_profiles.get_product(name)
        if not prod or self.config is None:
            return
        b = self.config.battery
        b.battery_type = prod.chemistry
        b.nominal_voltage = prod.nominal_voltage_per_cell
        b.cells_series = prod.cells_series
        b.cells_parallel = prod.cells_parallel
        b.rated_capacity = prod.rated_capacity_ah
        if prod.mass_grams:
            b.mass_grams = prod.mass_grams
        if prod.max_voltage_per_cell:
            b.max_voltage = prod.max_voltage_per_cell
        if prod.min_voltage_per_cell:
            b.min_voltage = prod.min_voltage_per_cell
        # อัป max_current จากสเปคแบต (ถ้าระบุ) — ใช้เป็น clamp สำหรับ 1C Quick Scan
        if prod.max_cont_discharge_a:
            b.max_current = prod.max_cont_discharge_a
        if self.config.system.safety_limits:
            if prod.safety_ovp_pack:
                self.config.system.safety_limits["max_voltage"] = prod.safety_ovp_pack
            if prod.safety_uvp_pack:
                self.config.system.safety_limits["min_voltage"] = prod.safety_uvp_pack
            # safety max_current = peak ถ้ามี, ไม่งั้นใช้ cont * 1.5
            if prod.max_peak_discharge_a:
                self.config.system.safety_limits["max_current"] = prod.max_peak_discharge_a
            elif prod.max_cont_discharge_a:
                self.config.system.safety_limits["max_current"] = prod.max_cont_discharge_a * 1.5
        try:
            from aset_batt.core.battery_model import BatteryModel

            model = BatteryModel(b.battery_type, b.nominal_voltage, b.cells_series, b.cells_parallel)
            if self.estimator is not None:
                self.estimator.battery_model = model
                if hasattr(self.estimator, "rated_capacity"):
                    self.estimator.rated_capacity = b.rated_capacity
            self.iec_standard = IEC61960Standard(b.rated_capacity, b.battery_type, b.pack_nominal_voltage)
            self._populate_profiles()
        except Exception as exc:
            logger.error("apply product: %s", exc)
        # อัป CHARGE step description ให้ตรงกับ strategy ของเคมีแบต
        cp   = battery_profiles.get_chemistry(prod.chemistry).charge
        charge_desc = "Full 3-stage (Bulk→Absorption→Float)" if cp.strategy == "three_stage" else "CC-CV"
        if len(self._wf_desc_lbls) > 1:
            self._wf_desc_lbls[1].setText(charge_desc)

        # Sync C-rate selector กับค่า default ของ profile (ถ้ามีใน list)
        default_crate_text = f"{cp.bulk_c_rate:g}C"
        idx = self.cb_seq_crate.findText(default_crate_text)
        if idx >= 0:
            self.cb_seq_crate.setCurrentIndex(idx)
        # Force-อัป lbl_seq_crate_a เสมอ (capacity อาจเปลี่ยนแม้ C-rate text เหมือนเดิม)
        self._on_seq_crate_changed(self.cb_seq_crate.currentText())

        # Reset charge mode → "Auto (by chemistry)" ให้สอดคล้องกับแบตใหม่
        self.cb_charge_mode.setCurrentText("Auto (by chemistry)")

        # อัป IEC TEST step (index 3) → แสดง A จริงของ C-rate ที่เลือก
        try:
            c_test = float(self.cb_test_crate.currentText().rstrip("C"))
        except (AttributeError, ValueError):
            c_test = 0.2
        i_test = round(c_test * prod.rated_capacity_ah, 2)
        if len(self._wf_desc_lbls) > 3:
            self._wf_desc_lbls[3].setText(f"Discharge {c_test:g}C = {i_test:.2f} A")
        if hasattr(self, "lbl_test_crate_a"):
            self.lbl_test_crate_a.setText(f"= {i_test:.2f} A")

        # อัป Quick Scan DISCHARGE step (index 2) → แสดง A จริงของ 1C
        i_1c = prod.max_cont_discharge_a if prod.max_cont_discharge_a else prod.rated_capacity_ah
        if len(self._qs_desc_lbls) > 2:
            self._qs_desc_lbls[2].setText(f"1C = {i_1c:.2f} A")

        self._refresh_battery_readout()
        self._log_alarm(f"Selected product: {name} → {prod.chemistry} {prod.cells_series}S")
        # refresh characterize tab params panel (if already built)
        if hasattr(self, "txt_char_params"):
            self._refresh_char_params()

    def _on_test_crate_changed(self, text: str):
        """ผู้ใช้เปลี่ยน Test discharge C-rate — อัป amp label + WF step desc"""
        try:
            c_test = float(text.rstrip("C"))
        except ValueError:
            return
        prod_name = self.cb_product.currentText() if hasattr(self, "cb_product") else ""
        prod = battery_profiles.get_product(prod_name)
        cap = prod.rated_capacity_ah if prod else (
            self.config.battery.rated_capacity if self.config else 0.0)
        i_test = round(c_test * cap, 2) if cap else 0.0
        if hasattr(self, "lbl_test_crate_a"):
            self.lbl_test_crate_a.setText(f"= {i_test:.2f} A" if cap else "— A")
        if len(self._wf_desc_lbls) > 3:
            self._wf_desc_lbls[3].setText(
                f"Discharge {c_test:g}C = {i_test:.2f} A" if cap else f"Discharge {c_test:g}C"
            )

    def _on_psu_bleed_changed(self, value: float):
        """ผู้ใช้เปลี่ยน PSU bleed compensation — อัป config + driver + estimator ทันที"""
        if self.config:
            self.config.hardware.psu_bleed_a = value
            self.config.save_config()
        if hasattr(self.hw, "psu_bleed_a"):
            self.hw.psu_bleed_a = value
        # keep state estimator in sync so OCV correction window tracks actual bleed
        if self.estimator is not None and hasattr(self.estimator, "set_standby_current"):
            self.estimator.set_standby_current(value)

    def _on_seq_crate_changed(self, text: str):
        """ผู้ใช้เปลี่ยน C-rate selector — อัป amp label + stage breakdown"""
        try:
            c_rate = float(text.rstrip("C"))
        except ValueError:
            return
        prod_name = self.cb_product.currentText() if hasattr(self, "cb_product") else ""
        prod = battery_profiles.get_product(prod_name)
        cap = prod.rated_capacity_ah if prod else (
            self.config.battery.rated_capacity if self.config else 0.0)
        self.lbl_seq_crate_a.setText(f"= {c_rate * cap:.2f} A" if cap else "— A")
        if prod:
            self._update_charge_crate_label(prod, c_rate_override=c_rate)

    def _update_charge_crate_label(self, prod, c_rate_override: float = None):
        """สร้างข้อความ stage breakdown และอัป lbl_charge_crate"""
        cp    = battery_profiles.get_chemistry(prod.chemistry).charge
        cap   = prod.rated_capacity_ah
        s     = prod.cells_series
        c_rate = c_rate_override if c_rate_override is not None else cp.bulk_c_rate
        i_bulk = c_rate * cap
        i_tail = cp.tail_current_c_rate * cap
        if cp.strategy == "cc_cv":
            cv_v = cp.cv_voltage_per_cell * s
            lines = [
                f"① CC: {c_rate:.2g}C = {i_bulk:.2f} A",
                f"② CV: {cv_v:.1f} V  (กระแส taper ลง)",
                f"จบเมื่อ < {cp.tail_current_c_rate:.2g}C = {i_tail:.2f} A",
            ]
        else:
            abs_v = cp.absorption_voltage_per_cell * s
            flt_v = cp.float_voltage_per_cell * s
            lines = [
                f"① Bulk CC: {c_rate:.2g}C = {i_bulk:.2f} A",
                f"② Absorption CV: {abs_v:.1f} V  (taper)",
                f"③ Float: {flt_v:.1f} V  "
                f"(จบเมื่อ < {cp.tail_current_c_rate:.2g}C = {i_tail:.2f} A)",
            ]
        self.lbl_charge_crate.setText("\n".join(lines))

    def _on_save_default(self):
        if self.config.save_config():
            self._log_alarm("Saved as default (config.json).")
            if not self._headless:
                QMessageBox.information(self, "Save as Default", "config.json saved")
        elif not self._headless:
            QMessageBox.critical(self, "Save as Default", "Save failed")

    def _on_edit_battery_profile(self):
        """In-app dialog to edit BatteryConfig fields and save to config.json."""
        b = self.config.battery
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Battery Profile")
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        form.setSpacing(6)

        fields = [
            ("battery_type",  "Chemistry / Type",   str),
            ("nominal_voltage","Nominal V (per cell)", float),
            ("max_voltage",    "Max V (per cell)",   float),
            ("min_voltage",    "Min V cutoff (per cell)", float),
            ("rated_capacity", "Rated Capacity (Ah)", float),
            ("max_current",    "Max Current (A)",    float),
            ("cells_series",   "Cells Series",       int),
            ("cells_parallel", "Cells Parallel",     int),
            ("mass_grams",     "Mass (g)",           float),
        ]
        editors: dict[str, QLineEdit] = {}
        for attr, label, _typ in fields:
            ed = QLineEdit(str(getattr(b, attr, "")))
            form.addRow(label + ":", ed)
            editors[attr] = ed
        lay.addLayout(form)

        hint = QLabel("Changes saved to config.json and applied immediately.")
        hint.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_ok = _btn("Save", bg=INFO, fg="white", hover="#0d4a89")
        btn_cancel = _btn("Cancel", bg="#d0d4d7", hover="#c2c6ca")
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_ok, 2); btn_row.addWidget(btn_cancel, 1)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        errors = []
        for attr, label, typ in fields:
            raw = editors[attr].text().strip()
            try:
                setattr(b, attr, typ(raw))
            except (ValueError, TypeError):
                errors.append(f"{label}: '{raw}' is not a valid {typ.__name__}")
        if errors:
            QMessageBox.warning(self, "Edit Battery Profile",
                                "Some fields were invalid:\n" + "\n".join(errors))
        self.config.save_config()
        self._on_product_changed(self.cb_product.currentText())
        self._log_alarm("[CONFIG] Battery profile updated and saved")

    def _on_detect_chemistry(self):
        if self.estimator is None:
            return
        try:
            model = self.estimator.battery_model
            v, s = ChemistryDetector.features_from_model(model)
            res = ChemistryDetector().detect(v, s)
            self._log_alarm(f"Chemistry detect → {res.chemistry} ({res.confidence * 100:.0f}%)")
            if not self._headless:
                QMessageBox.information(
                    self,
                    "Chemistry Detection",
                    f"Detected: {res.chemistry}\nConfidence: {res.confidence * 100:.0f}%",
                )
        except Exception as exc:
            if not self._headless:
                QMessageBox.warning(self, "Chemistry Detection", str(exc))

    def _on_connect(self):
        psu, load, esp = self.cb_psu.currentText(), self.cb_load.currentText(), self.cb_esp.currentText()
        if not psu or not load:
            if not self._headless:
                QMessageBox.warning(self, "Connect", "Select PSU and Load ports first")
            return
        try:
            self.hw.connect_instruments(psu, load)
            if esp:
                baud = getattr(self.config.hardware, "serial_baudrate", 9600)
                try:
                    self.hw.connect_esp32(esp, baudrate=baud)
                    if hasattr(self.hw, "esp_connect_error"):
                        self.hw.esp_connect_error = ""
                except Exception as esp_exc:
                    # ESP32 fail is non-fatal — store error so _slot_conn shows ✗
                    if hasattr(self.hw, "esp_connect_error"):
                        self.hw.esp_connect_error = str(esp_exc)
                    self._log_alarm(f"ESP32 connect failed (non-fatal): {esp_exc}")
            self.config.hardware.psu_port = psu
            self.config.hardware.load_port = load
            self.config.hardware.esp_port = esp
            self.config.save_config()
            # sync bleed compensation จาก config เข้า driver + state estimator
            bleed = getattr(self.config.hardware, "psu_bleed_a", 0.0)
            if hasattr(self.hw, "psu_bleed_a"):
                self.hw.psu_bleed_a = bleed
            if self.estimator is not None and hasattr(self.estimator, "set_standby_current"):
                self.estimator.set_standby_current(bleed)
            self._update_connection_status()
            self._log_alarm("Hardware connected.")
            self._cloud_push_start()
        except Exception as exc:
            # connect_error already set in hw.connect_instruments — let _slot_conn show ✗
            self._update_connection_status()
            if not self._headless:
                QMessageBox.critical(self, "เชื่อมต่อล้มเหลว", str(exc))

    def _on_disconnect(self):
        try:
            self._cloud_push_stop()
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            self._update_connection_status()
            self._log_alarm("Hardware disconnected.")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "Disconnect Error", str(exc))

    # ── Cloud push helpers ────────────────────────────────────────────────
    _cloud_svc = None

    def _cloud_push_start(self):
        try:
            from aset_batt.services.cloud_push import CloudPushService
            self._cloud_svc = CloudPushService(self.config)
            self._cloud_svc.start()
            if getattr(self.config.system, "cloud_push_enabled", False):
                self._log_alarm("[CLOUD] Push service started")
        except Exception as e:
            self._log_alarm(f"[CLOUD] Start failed: {e}")

    def _cloud_push_stop(self):
        try:
            if self._cloud_svc:
                self._cloud_svc.stop()
                self._cloud_svc = None
        except Exception:
            pass

    def _cloud_push_update(self, v, i, soc, temp):
        """Call from the telemetry slot to keep the cloud payload fresh."""
        try:
            if self._cloud_svc:
                from datetime import datetime as _dt
                self._cloud_svc.push_now({
                    "ts": _dt.utcnow().isoformat(),
                    "voltage_v": round(v, 3),
                    "current_a": round(i, 3),
                    "soc_pct":   round(soc, 1),
                    "temp_c":    round(temp, 1),
                    "battery":   self.config.battery.battery_type,
                })
        except Exception:
            pass

    def _on_direct_toggled(self, on: bool):
        if not on:
            return
        if self._seq_running.is_set():
            # A sequence is active — refuse to switch into direct control.
            if not self._headless:
                QMessageBox.warning(
                    self, "Direct Control",
                    "ไม่สามารถใช้ Direct Control ขณะที่ AUTO sequence กำลังรันอยู่\n"
                    "กด CANCEL SEQUENCE ก่อน"
                )
            # Revert radio selection back to whichever page was showing.
            idx = self.run_stack.currentIndex()
            [self.rb_charge, self.rb_discharge, self.rb_hppc][min(idx, 2)].setChecked(True)
            return
        self.run_stack.setCurrentIndex(3)

    def _psu_manual(self, on):
        if on and self._seq_running.is_set():
            if not self._headless:
                QMessageBox.warning(self, "Direct Control",
                                    "ไม่สามารถใช้ Direct Control ขณะที่ AUTO sequence กำลังรันอยู่")
            return
        try:
            if on:
                self.hw.set_psu(
                    True,
                    str(float(self.ed_psu_v.text())),
                    str(float(self.ed_psu_i.text())),
                )
            else:
                self.hw.set_psu(False)
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "PSU", "Invalid voltage / current")

    def _load_manual(self, on):
        if on and self._seq_running.is_set():
            if not self._headless:
                QMessageBox.warning(self, "Direct Control",
                                    "ไม่สามารถใช้ Direct Control ขณะที่ AUTO sequence กำลังรันอยู่")
            return
        try:
            self.hw.set_load(on, str(float(self.ed_load_a.text())) if on else "0")
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "Load", "Invalid current")

    def _on_charge(self):
        if self.controller is None:
            return
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Charge", "Connect hardware first")
            return
        strategy = {"CC-CV": "cc_cv",
                    "3-Stage (Lead-Acid)": "three_stage"}.get(self.cb_charge_mode.currentText())
        ok = self.controller.start_charge(strategy=strategy)
        mode = self.cb_charge_mode.currentText()
        self._log_alarm(f"Charge started ({mode})." if ok else "Charge start failed.")

    def _on_stop_charge(self):
        if self.controller:
            self.controller.stop_charge()
            self._log_alarm("Charge stopped.")

    def _on_ocv_calibrate(self):
        """ปิด PSU+Load แล้วรอให้แรงดันนิ่ง (ΔV/Δt criterion) ก่อนคำนวณ SoC"""
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "OCV", "Connect hardware first")
            return
        if getattr(self.controller, "is_charging", False):
            if not self._headless:
                QMessageBox.warning(self, "OCV", "Stop charging before OCV calibration")
            return

        chemistry = getattr(self.controller.config.battery, "battery_type", "LiPO")
        _min_labels = {"LeadAcid": "5 นาที", "LiFePO4": "2 นาที"}
        min_label = _min_labels.get(chemistry, "1 นาที")

        self.sig_loading.emit("btn_ocv", True, "Settling…")
        self.sig_charge_status.emit(
            f"OCV: ปิดอุปกรณ์ — รอ settle ({chemistry}, ขั้นต่ำ {min_label})…"
        )

        import threading
        def _run():
            try:
                self.hw.psu_off()
                self.hw.load_off()

                def on_progress(elapsed, v, dv_mv, status):
                    chemistry_now = getattr(
                        self.controller.config.battery, "battery_type", "LiPO"
                    )
                    min_rest = self.controller._OCV_SETTLE.get(
                        chemistry_now, self.controller._OCV_SETTLE["LiPO"]
                    )[0]
                    dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                    if status == "waiting":
                        remaining = max(0, int(min_rest - elapsed))
                        self.sig_charge_status.emit(
                            f"OCV รอขั้นต่ำ: {remaining}s | {v:.3f} V | ΔV {dv_str}"
                        )
                    elif status == "checking":
                        self.sig_charge_status.emit(
                            f"OCV กำลัง settle: {int(elapsed)}s | {v:.3f} V | ΔV {dv_str}"
                        )

                soc, v_final, result = self.controller.calibrate_from_ocv_stable(
                    on_progress=on_progress
                )
                temp = self.controller.hw.current_temp
                flag = "✓ settled" if result == "settled" else "⚠ timeout (ใช้ค่าล่าสุด)"
                msg = (
                    f"OCV {flag}: {v_final:.3f} V  →  SoC {soc:.1f}%"
                    f"  (Temp {temp:.1f}°C)"
                )
                self.sig_alarm.emit(f"[OCV] {msg}")
                self.sig_charge_status.emit(msg)
            except Exception as exc:
                self.sig_alarm.emit(f"[OCV] failed: {exc}")
                self.sig_charge_status.emit(f"OCV failed: {exc}")
            finally:
                self.sig_loading.emit("btn_ocv", False, "")
        threading.Thread(target=_run, daemon=True).start()

    # ---- Workflow guide slots -----------------------------------------------

    @Slot(int, str)
    def _slot_workflow(self, step: int, state: str):
        """Update a step indicator.  state: idle/active/done/skip."""
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:65px;",   "○"),
            "active": (f"color:{INFO};    font-size:16px; min-width:22px;",
                       f"color:{INFO};    font-weight:700; min-width:65px;",  "●"),
            "done":   (f"color:{OK};      font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK};      font-weight:700; min-width:65px;",  "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:65px;",  "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._wf_leds):
            dot, name_lbl = self._wf_leds[step]
            dot.setText(symbol)
            dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, str)
    def _slot_qs_workflow(self, phase: int, state: str):
        _styles = {
            "active": (f"color:#e67e22; font-size:16px; min-width:22px; font-weight:700;",
                       f"color:#e67e22; font-weight:700; min-width:75px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:75px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:14px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "—"),
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "○"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= phase < len(self._qs_leds):
            dot, name_lbl = self._qs_leds[phase]
            dot.setText(symbol)
            dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(str)
    def _slot_wf_status(self, text: str):
        """Cross-thread safe wrapper for lbl_wf_status.setText."""
        self.lbl_wf_status.setText(text)

    @Slot(int, str)
    def _slot_hppc_seq_wf(self, step: int, state: str):
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:65px;", "○"),
            "active": (f"color:#7b2d8b; font-size:16px; min-width:22px;",
                       f"color:#7b2d8b; font-weight:700; min-width:65px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:65px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:65px;", "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._hppc_seq_leds):
            dot, name_lbl = self._hppc_seq_leds[step]
            dot.setText(symbol); dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, str)
    def _slot_cycle_wf(self, step: int, state: str):
        _styles = {
            "idle":   (f"color:{NEUTRAL}; font-size:16px; min-width:22px;",
                       f"color:{MUTED}; font-weight:700; min-width:75px;", "○"),
            "active": (f"color:#6c3483; font-size:16px; min-width:22px;",
                       f"color:#6c3483; font-weight:700; min-width:75px;", "●"),
            "done":   (f"color:{OK}; font-size:13px; min-width:22px; font-weight:700;",
                       f"color:{OK}; font-weight:700; min-width:75px;", "✓"),
            "skip":   (f"color:{NEUTRAL}; font-size:13px; min-width:22px;",
                       f"color:{NEUTRAL}; font-weight:700; min-width:75px;", "—"),
        }
        dot_style, name_style, symbol = _styles.get(state, _styles["idle"])
        if 0 <= step < len(self._cycle_leds):
            dot, name_lbl = self._cycle_leds[step]
            dot.setText(symbol); dot.setStyleSheet(dot_style)
            name_lbl.setStyleSheet(name_style)

    @Slot(int, int)
    def _slot_phase_progress(self, elapsed_s: int, total_s: int):
        if total_s <= 0:
            self.wf_progress.hide(); self.lbl_eta.hide(); return
        self.wf_progress.setRange(0, total_s)
        self.wf_progress.setValue(min(elapsed_s, total_s))
        self.wf_progress.setFormat(f"%p%  ({elapsed_s // 60}m {elapsed_s % 60:02d}s / "
                                   f"{total_s // 60}m {total_s % 60:02d}s)")
        rem = max(0, total_s - elapsed_s)
        self.lbl_eta.setText(f"ETA: {rem // 60}m {rem % 60:02d}s remaining")
        self.wf_progress.show(); self.lbl_eta.show()

    @Slot(str)
    def _slot_seq_result(self, html: str):
        self.lbl_seq_result.setText(html)
        self.frm_seq_result.show()

    @Slot(str, str)
    def _slot_seq_done(self, title: str, body: str):
        """Sound + popup notification when a sequence finishes."""
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            QApplication.beep()
        if not self._headless:
            msg = QMessageBox(self)
            msg.setWindowTitle(title)
            msg.setText(body)
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.setWindowModality(Qt.WindowModality.NonModal)
            msg.show()

    def _show_pretest_dialog(self, title: str, plan_lines: list, eta_min: int) -> bool:
        """Show a pre-test confirmation card.  Returns True iff user clicks Confirm."""
        if self._headless:
            return True
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Confirm: {title}")
        dlg.setMinimumWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)

        # Battery / plan card
        card = QFrame()
        card.setStyleSheet(
            f"QFrame{{background:{PANEL2};border:1px solid {BORDER};"
            f"border-radius:5px;padding:6px 10px;}}"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setSpacing(3)
        for line in plan_lines:
            lbl = QLabel(line)
            lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
            card_lay.addWidget(lbl)
        lay.addWidget(card)

        # ETA row
        eta_lbl = QLabel(f"Estimated duration: ~{eta_min} min  ({eta_min//60}h {eta_min%60:02d}m)")
        eta_lbl.setStyleSheet(f"color:{INFO}; font-weight:600;")
        lay.addWidget(eta_lbl)

        # Confirm / Cancel
        btn_row = QHBoxLayout()
        btn_conf = _btn("▶  CONFIRM START", bg=INFO, fg="white", hover="#0d4a89")
        btn_canc = _btn("Cancel", bg="#d0d4d7", hover="#c2c6ca")
        btn_conf.clicked.connect(dlg.accept)
        btn_canc.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_conf, 2); btn_row.addWidget(btn_canc, 1)
        lay.addLayout(btn_row)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _seq_common_start(self, btn_key: str, loading_label: str):
        """Shared startup: reset all step leds, buffers, progress, result card."""
        for i in range(len(self._WF_STEPS)):       self.sig_workflow.emit(i, "idle")
        for i in range(len(self._QS_STEPS)):       self.sig_qs_workflow.emit(i, "idle")
        for i in range(len(self._HPPC_SEQ_STEPS)): self.sig_hppc_seq_wf.emit(i, "idle")
        for i in range(len(self._CYCLE_STEPS)):    self.sig_cycle_wf.emit(i, "idle")
        for buf in (self.buf_t, self.buf_v, self.buf_i,
                    self.buf_soc, self.buf_rin, self.buf_temp):
            buf.clear()
        self._elapsed_t0 = None
        self._seq_last_meas_time = 0.0   # reset watchdog
        self.sig_phase_progress.emit(0, 0)   # hide progress bar
        self.frm_seq_result.hide()
        self._seq_running.set()
        self.btn_seq_cancel.setEnabled(True)
        self.sig_loading.emit(btn_key, True, loading_label)

    def _on_auto_sequence(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Auto Sequence", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        try:
            v_now, _, _ = self.hw.read_vi()
            temp_now = self.hw.current_temp
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            rated = self.controller.config.battery.rated_capacity
            crate = self.cb_seq_crate.currentText()
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.2f} V  ·  SoC: {soc_now:.0f}%  ·  Temp: {temp_now:.1f} °C",
                f"Charge: {crate} ({float(crate.rstrip('C'))*rated:.2f} A)  →  "
                f"REST {self.spn_rest_min.value()} min  →  "
                f"Discharge {self.cb_test_crate.currentText()}",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        if not self._show_pretest_dialog("IEC 61960 AUTO SEQUENCE", plan, eta_min=600):
            return
        self._seq_common_start("btn_auto_seq", "Running…")
        import threading
        threading.Thread(target=self._auto_sequence_thread, daemon=True).start()

    def _on_quick_scan(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Quick Scan", "Connect hardware first")
            return
        if self._seq_running.is_set():
            return
        try:
            v_now, _, _ = self.hw.read_vi()
            soc_now = getattr(self.controller.estimator, "soc", 0.0)
            rated = self.controller.config.battery.rated_capacity
            plan = [
                f"Battery: {self.controller.config.battery.battery_type}",
                f"OCV: {v_now:.2f} V  ·  SoC: {soc_now:.0f}%",
                f"OCV → REST 5 min → Discharge 1C ({rated:.2f} A) → Peukert SoH",
            ]
        except Exception:
            plan = ["(hardware not ready — values unavailable)"]
        if not self._show_pretest_dialog("QUICK SCAN", plan, eta_min=90):
            return
        self._seq_common_start("btn_quick_scan", "Scanning…")
        import threading
        threading.Thread(target=self._quick_scan_thread, daemon=True).start()

    def _on_hppc_sequence(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "HPPC Sequence", "Connect hardware first")
            return
        if self._seq_running.is_set():
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
                f"OCV: {v_now:.2f} V  ·  SoC: {soc_now:.0f}%",
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
        import threading
        threading.Thread(target=self._hppc_seq_thread, daemon=True).start()

    def _on_cycle_life(self):
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Cycle Life", "Connect hardware first")
            return
        if self._seq_running.is_set():
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
        import threading
        threading.Thread(target=self._cycle_life_thread, daemon=True).start()

    # ── Safety helpers ───────────────────────────────────────────────────────
    _WATCHDOG_TIMEOUT_S: int = 300   # 5 min without a measurement → abort

    def _otp_limit(self) -> float:
        try:
            return float(self.controller.config.system.safety_limits["max_temperature"])
        except Exception:
            return 60.0

    def _seq_kick_watchdog(self):
        """Call after every successful measurement read inside a sequence thread."""
        import time as _t
        self._seq_last_meas_time = _t.time()

    def _seq_check_otp(self, temp: float) -> bool:
        """Returns True if temperature is safe.  Clears _seq_running + alarms if OTP."""
        limit = self._otp_limit()
        if temp > limit:
            self._seq_running.clear()
            self.sig_alarm.emit(
                f"[SAFETY] OTP triggered: {temp:.1f}°C > {limit:.0f}°C — sequence aborted")
            return False
        return True

    def _seq_sleep(self, seconds: float) -> bool:
        """Sleep แบบ interruptible — คืน True ถ้าครบเวลา, False ถ้า cancel หรือ watchdog หมดเวลา"""
        import time
        t_end = time.time() + seconds
        while self._seq_running.is_set():
            left = t_end - time.time()
            if left <= 0:
                return True
            # watchdog: abort if no measurement update for _WATCHDOG_TIMEOUT_S
            last = getattr(self, "_seq_last_meas_time", 0.0)
            if last and (time.time() - last) > self._WATCHDOG_TIMEOUT_S:
                self._seq_running.clear()
                self.sig_alarm.emit(
                    "[SAFETY] Watchdog: ไม่มีการวัดค่า > 5 นาที — sequence ถูกยกเลิก")
                return False
            time.sleep(min(0.3, left))
        return False

    def _on_seq_cancel(self):
        self._seq_running.clear()
        # หยุด hardware ทันที
        try:
            if self.controller:
                self.controller.stop_charge()
            self.hw.load_off()
            self.hw.psu_off()
        except Exception:
            pass
        self.lbl_wf_status.setText("ยกเลิก")
        self.btn_seq_cancel.setEnabled(False)
        self.sig_phase_progress.emit(0, 0)
        self.frm_seq_result.hide()
        for btn in ("btn_auto_seq", "btn_quick_scan", "btn_hppc_seq", "btn_cycle_life"):
            self.sig_loading.emit(btn, False, "")
        self.sig_alarm.emit("[AUTO] Sequence cancelled — hardware stopped.")

    def _auto_sequence_thread(self):
        """Background thread: PREPARE → CHARGE → REST → TEST → ANALYZE."""
        import time

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        skip_charge = self.chk_skip_charge.isChecked()
        skip_rest   = self.chk_skip_rest.isChecked()
        soc_thresh  = self.spn_soc_threshold.value()

        try:
            # ── PHASE 0: OCV CALIBRATE ────────────────────────────────────
            self.sig_workflow.emit(0, "active")
            self.hw.psu_off()
            self.hw.load_off()
            # Use ΔV/Δt criterion (Fick diffusion settling) instead of a fixed sleep.
            # calibrate_from_ocv_stable() enforces the chemistry-specific minimum rest
            # (Lead-Acid: 300 s min, ΔV < 10 mV over 60 s window) and then syncs
            # the estimator — giving a true OCV anchor rather than a polarized reading.
            def _ocv_progress(elapsed, v, dv_mv, st):
                dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                status(f"PREPARE: OCV settle {int(elapsed)} s | {v:.3f} V | ΔV {dv_str} [{st}]")

            soc, v, result = self.controller.calibrate_from_ocv_stable(
                on_progress=_ocv_progress,
                cancel_check=self._seq_running.is_set,
            )
            if not self._seq_running.is_set():
                return
            flag = "✓ settled" if result == "settled" else "⚠ timeout"
            self.sig_alarm.emit(f"[AUTO] OCV: {v:.3f} V → SoC {soc:.1f}% ({flag})")
            self.sig_workflow.emit(0, "done")

            # ── PHASE 1: CHARGE ──────────────────────────────────────────
            if skip_charge or soc >= soc_thresh:
                reason = "skip-charge checked" if skip_charge else f"SoC={soc:.0f}% ≥ {soc_thresh}%"
                self.sig_alarm.emit(f"[AUTO] Skipping charge ({reason})")
                self.sig_workflow.emit(1, "skip")
            else:
                self.sig_workflow.emit(1, "active")
                try:
                    _c_rate_override = float(self.cb_seq_crate.currentText().rstrip("C"))
                except (ValueError, AttributeError):
                    _c_rate_override = None
                status(f"CHARGE: SoC={soc:.0f}% → charging "
                       f"({self.cb_seq_crate.currentText()})...")
                self.controller.start_charge(strategy=None,
                                             bulk_c_rate_override=_c_rate_override)
                _ch_t0 = time.time()
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v2, _, _ = self.hw.read_vi()
                        elapsed_ch = int(time.time() - _ch_t0)
                        status(f"CHARGE: {v2:.2f} V  (elapsed {elapsed_ch//60}m {elapsed_ch%60:02d}s)")
                        self.sig_phase_progress.emit(elapsed_ch, 0)  # indeterminate — total unknown
                    except Exception:
                        pass
                    if not self._seq_sleep(30.0):
                        break
                if not self._seq_running.is_set():
                    return
                self.sig_phase_progress.emit(0, 0)
                self.sig_workflow.emit(1, "done")
                self.sig_alarm.emit("[AUTO] Charge complete")

            # ── PHASE 2: REST ─────────────────────────────────────────────
            if skip_rest:
                self.sig_alarm.emit("[AUTO] Skipping REST phase")
                self.sig_workflow.emit(2, "skip")
            else:
                self.sig_workflow.emit(2, "active")
                rest_total = self.spn_rest_min.value() * 60
                t_rest_end = time.time() + rest_total
                while self._seq_running.is_set():
                    remaining = int(t_rest_end - time.time())
                    if remaining <= 0:
                        break
                    elapsed_r = rest_total - remaining
                    mins, secs = divmod(remaining, 60)
                    status(f"REST: เหลือ {mins:d}:{secs:02d} นาที")
                    self.sig_phase_progress.emit(elapsed_r, rest_total)
                    if not self._seq_sleep(10.0):
                        return
                self.sig_phase_progress.emit(0, 0)
                # OCV reset after rest
                soc2 = self.controller.calibrate_from_ocv()
                v2, _, _ = self.hw.read_vi()
                self.sig_alarm.emit(f"[AUTO] Post-rest OCV: {v2:.3f} V → SoC {soc2:.1f}%")
                self.sig_workflow.emit(2, "done")

            # ── PHASE 3: DISCHARGE TEST (IEC — C-rate จาก cb_test_crate) ───────
            self.sig_workflow.emit(3, "active")
            try:
                c_test = float(self.cb_test_crate.currentText().rstrip("C"))
            except (AttributeError, ValueError):
                c_test = 0.2
            rated   = self.controller.config.battery.rated_capacity
            i_dis   = round(c_test * rated, 2)
            pack_min = self.controller.config.battery.pack_min_voltage
            status(f"TEST: discharge {i_dis:.2f} A ({c_test:g}C) จนถึง {pack_min:.1f} V")
            self.sig_alarm.emit(f"[AUTO] Starting discharge {i_dis:.2f} A")
            self.controller._ensure_logging()
            self.hw.set_load(True, i_dis)
            import time as _t
            last_log = _t.time()
            _dis_t0 = _t.time()
            # Estimate discharge duration from SoC and C-rate (seconds)
            rated2 = self.controller.config.battery.rated_capacity
            _dis_est = int(rated2 / max(i_dis, 0.01) * 3600)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    temp3 = self.hw.current_temp
                    now = _t.time()
                    dt = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    self._seq_kick_watchdog()
                    elapsed_d = int(now - _dis_t0)
                    status(f"TEST: {v3:.2f} V  {i3:.2f} A  SoC {state3['soc']:.0f}%")
                    self.sig_phase_progress.emit(elapsed_d, _dis_est)
                    if not self._seq_check_otp(temp3):
                        break
                    if v3 <= pack_min:
                        break
                except Exception as e:
                    self.sig_alarm.emit(f"[AUTO] discharge read error: {e}")
                    break
                if not self._seq_sleep(5.0):
                    break
            self.hw.set_load(False)
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.controller._ocv_reset_after_rest("discharge")
            self.sig_workflow.emit(3, "done")
            self.sig_alarm.emit("[AUTO] Discharge complete")

            # ── PHASE 4: ANALYZE ─────────────────────────────────────────
            self.sig_workflow.emit(4, "active")
            status("ANALYZE: วิเคราะห์ CSV...")
            res = self.controller._auto_analyze()
            self.sig_workflow.emit(4, "done")
            if res:
                self.sig_seq_result.emit(self._format_seq_result(res))
            status("เสร็จสิ้น — ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[AUTO] Sequence complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            self.sig_seq_done.emit("IEC 61960 Sequence Complete",
                                   f"Grade: {grade_str}\nดูผลเพิ่มเติมที่แท็บ Analytics")

        except Exception as exc:
            self.sig_alarm.emit(f"[AUTO] Error: {exc}")
            status(f"Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.sig_loading.emit("btn_auto_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    def _quick_scan_thread(self):
        """Quick Scan: OCV → REST 5min → Discharge 1C → Analyze  (~1.5h)
        ใช้ Peukert correction ที่มีอยู่ใน analyze_series เพื่อประเมิน capacity จาก 1C rate."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        try:
            # ── Phase 0: OCV ────────────────────────────────────────────────
            self.sig_qs_workflow.emit(0, "active")
            status("QUICK: ปิดอุปกรณ์, อ่าน OCV...")
            self.hw.psu_off()
            self.hw.load_off()
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
                if not self._seq_sleep(10.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            soc2 = self.controller.calibrate_from_ocv()
            v2, _, _ = self.hw.read_vi()
            self.sig_alarm.emit(f"[QUICK] Post-rest OCV: {v2:.3f} V → SoC {soc2:.1f}%")
            self.sig_qs_workflow.emit(1, "done")

            # ── Phase 2: DISCHARGE 1C ────────────────────────────────────
            self.sig_qs_workflow.emit(2, "active")
            rated    = self.controller.config.battery.rated_capacity
            max_i    = self.controller.config.battery.max_current
            i_dis    = min(round(1.0 * rated, 2), max_i)   # 1C, clamped to rig limit
            pack_min = self.controller.config.battery.pack_min_voltage
            status(f"QUICK DISCHARGE: {i_dis:.2f} A (1C) → cutoff {pack_min:.1f} V")
            self.sig_alarm.emit(f"[QUICK] Discharge 1C: {i_dis:.2f} A  (rated {rated:.1f} Ah)")
            self.controller._ensure_logging()
            self.hw.set_load(True, i_dis)
            last_log = _t.time()
            _dis_t0 = _t.time()
            _dis_est = int(rated / max(i_dis, 0.01) * 3600)
            while self._seq_running.is_set():
                try:
                    v3, i3 = self.hw.read_measurements(prefer_load_v=True)
                    temp3  = self.hw.current_temp
                    now    = _t.time()
                    dt     = now - last_log
                    last_log = now
                    state3 = self.controller.estimator.update(v3, i3, dt=dt, temp=temp3)
                    self.controller._log_sample(v3, i3)
                    self._seq_kick_watchdog()
                    elapsed_d = int(now - _dis_t0)
                    status(f"QUICK: {v3:.2f} V  {i3:.2f} A  SoC {state3['soc']:.0f}%")
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
                self.sig_seq_result.emit(self._format_seq_result(res))
            status("QUICK SCAN เสร็จ — ดูผลที่แท็บ Analytics  (ค่า capacity ถูก Peukert-correct แล้ว)")
            self.sig_alarm.emit("[QUICK] Scan complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            self.sig_seq_done.emit("Quick Scan Complete",
                                   f"Grade: {grade_str}\nดูผลเพิ่มเติมที่แท็บ Analytics")

        except Exception as exc:
            self.sig_alarm.emit(f"[QUICK] Error: {exc}")
            status(f"QUICK Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.sig_loading.emit("btn_quick_scan", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- result formatting -----------------------------------------------
    @staticmethod
    def _format_seq_result(res: dict) -> str:
        """Format an analyze_csv result dict into a short HTML string for the
        inline result card."""
        grade   = res.get("grade", "?")
        soh     = res.get("soh", float("nan"))
        cap     = res.get("capacity_ah", float("nan"))
        dcir    = res.get("dcir_mohm", float("nan"))
        conf    = res.get("confidence", 0.0)
        ecm     = res.get("ecm_identified", False)
        r0      = res.get("r0_mohm", float("nan"))
        r1      = res.get("r1_mohm", float("nan"))
        tau     = res.get("tau_s", float("nan"))
        r2      = res.get("ecm_r2", float("nan"))
        import math
        soh_str = f"{soh:.1f}%" if not math.isnan(soh) else "N/A"
        cap_str = f"{cap:.2f} Ah" if not math.isnan(cap) else "N/A"
        dcir_str = f"{dcir:.1f} mΩ" if not math.isnan(dcir) else "N/A"
        lines = [
            f"<b>Grade: {grade}</b>   SoH: {soh_str}   Cap: {cap_str}",
            f"DCIR: {dcir_str}   Confidence: {conf*100:.0f}%",
        ]
        if ecm and not math.isnan(r0):
            lines.append(
                f"ECM — R0: {r0:.1f} mΩ  R1: {r1:.1f} mΩ  τ: {tau:.1f}s  R²: {r2:.3f}"
            )
        return "<br>".join(lines)

    # ---- HPPC full-sequence thread ----------------------------------------
    def _hppc_seq_thread(self):
        """HPPC Full Sequence: CHARGE → REST 30 min → N×HPPC pulse/relax → ECM fit."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        try:
            # ── PHASE 0: CHARGE CC-CV ─────────────────────────────────────
            self.sig_hppc_seq_wf.emit(0, "active")
            status("HPPC SEQ: ชาร์จ CC-CV → 100%...")
            rated = self.controller.config.battery.rated_capacity
            self.controller.start_charge(strategy=None)
            _ch_t0 = _t.time()
            while self._seq_running.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                try:
                    v_c, _, _ = self.hw.read_vi()
                    elapsed_ch = int(_t.time() - _ch_t0)
                    status(f"HPPC CHARGE: {v_c:.2f} V  ({elapsed_ch//60}m {elapsed_ch%60:02d}s)")
                    self.sig_phase_progress.emit(elapsed_ch, 0)
                except Exception:
                    pass
                if not self._seq_sleep(30.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_hppc_seq_wf.emit(0, "done")
            self.sig_alarm.emit("[HPPC SEQ] Charge complete")

            # ── PHASE 1: REST 30 min ─────────────────────────────────────
            self.sig_hppc_seq_wf.emit(1, "active")
            _rest_total = 30 * 60
            t_rest_end = _t.time() + _rest_total
            while self._seq_running.is_set():
                remaining = int(t_rest_end - _t.time())
                if remaining <= 0:
                    break
                elapsed_r = _rest_total - remaining
                mins, secs = divmod(remaining, 60)
                status(f"HPPC REST (OCV settle): เหลือ {mins}:{secs:02d}")
                self.sig_phase_progress.emit(elapsed_r, _rest_total)
                if not self._seq_sleep(10.0):
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            soc_h = self.controller.calibrate_from_ocv()
            v_h, _, _ = self.hw.read_vi()
            self.sig_alarm.emit(f"[HPPC SEQ] Post-rest OCV: {v_h:.3f} V → SoC {soc_h:.1f}%")
            self.sig_hppc_seq_wf.emit(1, "done")

            # ── PHASE 2: HPPC N cycles ────────────────────────────────────
            self.sig_hppc_seq_wf.emit(2, "active")
            n_cyc    = self.spn_hppc_cycles.value()
            try:
                pulse_s = max(1.0, float(self.ed_hppc_pulse.text() or "30"))
                relax_s = max(1.0, float(self.ed_hppc_relax.text() or "30"))
                crate   = max(0.1, float(self.ed_hppc_crate.text() or "1.0"))
            except (ValueError, AttributeError):
                pulse_s, relax_s, crate = 30.0, 30.0, 1.0
            max_dis = self.controller.config.battery.max_current
            i_pulse = min(crate * rated, max_dis)
            pack_min = self.controller.config.battery.pack_min_voltage
            _hppc_total = n_cyc * (relax_s + pulse_s)
            self.controller._ensure_logging()
            self.hw.psu_off()
            self.hw.load_off()
            _hppc_t0 = _t.time()
            for cyc in range(1, n_cyc + 1):
                if not self._seq_running.is_set():
                    break
                # Relax (REST) leg
                status(f"HPPC {cyc}/{n_cyc}: REST {relax_s:.0f}s...")
                t_phase = _t.time() + relax_s
                while self._seq_running.is_set() and _t.time() < t_phase:
                    try:
                        v_r, _, _ = self.hw.read_vi()
                        self.controller._log_sample(v_r, 0.0)
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_r <= pack_min:
                            self._seq_running.clear(); break
                        temp_h = self.hw.current_temp
                        if not self._seq_check_otp(temp_h):
                            break
                    except Exception:
                        pass
                    _t.sleep(1.0)
                if not self._seq_running.is_set():
                    break
                # Pulse leg
                self.hw.set_load(True, str(i_pulse))
                status(f"HPPC {cyc}/{n_cyc}: PULSE {pulse_s:.0f}s  {i_pulse:.2f} A")
                t_phase = _t.time() + pulse_s
                while self._seq_running.is_set() and _t.time() < t_phase:
                    try:
                        v_p, i_p = self.hw.read_measurements(prefer_load_v=True)
                        self.controller._log_sample(v_p, -i_p)
                        self._seq_kick_watchdog()
                        elapsed_h = int(_t.time() - _hppc_t0)
                        self.sig_phase_progress.emit(elapsed_h, int(_hppc_total))
                        if v_p <= pack_min:
                            self._seq_running.clear(); break
                        temp_h = self.hw.current_temp
                        if not self._seq_check_otp(temp_h):
                            break
                    except Exception:
                        pass
                    _t.sleep(1.0)
                self.hw.load_off()
                if not self._seq_running.is_set():
                    break
            self.sig_phase_progress.emit(0, 0)
            if not self._seq_running.is_set():
                return
            self.sig_hppc_seq_wf.emit(2, "done")
            self.sig_alarm.emit(f"[HPPC SEQ] {n_cyc} HPPC cycles complete")

            # ── PHASE 3: ANALYZE (ECM fit) ────────────────────────────────
            self.sig_hppc_seq_wf.emit(3, "active")
            status("HPPC SEQ ANALYZE: ECM fit R0/R1/C1/τ...")
            res = self.controller._auto_analyze(force_hppc=True)
            self.sig_hppc_seq_wf.emit(3, "done")
            if res:
                self.sig_seq_result.emit(self._format_seq_result(res))
            status("HPPC SEQUENCE เสร็จ — ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[HPPC SEQ] Complete ✓")
            grade_str = res.get("grade", "?") if res else "?"
            ecm_str = res.get("ecm_model", "1RC") if res else "1RC"
            self.sig_seq_done.emit("HPPC Sequence Complete",
                                   f"Grade: {grade_str}  ({ecm_str} ECM)\nดูผลที่แท็บ Analytics")

        except Exception as exc:
            self.sig_alarm.emit(f"[HPPC SEQ] Error: {exc}")
            status(f"HPPC SEQ Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.hw.load_off()
            self.sig_loading.emit("btn_hppc_seq", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- Cycle Life test thread -------------------------------------------
    def _cycle_life_thread(self):
        """Cycle Life: N × (Charge CC-CV → REST → Discharge CC) with capacity fade tracking."""
        import time as _t

        def status(msg):
            self.sig_charge_status.emit(msg)
            self.sig_wf_status.emit(msg)

        try:
            n_cyc     = self.spn_cycle_n.value()
            rest_s    = self.spn_cycle_rest.value() * 60
            rated     = self.controller.config.battery.rated_capacity
            try:
                c_ch  = float(self.cb_cycle_charge_crate.currentText().rstrip("C"))
            except (ValueError, AttributeError):
                c_ch  = 0.3
            try:
                c_di  = float(self.cb_cycle_dis_crate.currentText().rstrip("C"))
            except (ValueError, AttributeError):
                c_di  = 0.2
            max_current = self.controller.config.battery.max_current
            pack_min  = self.controller.config.battery.pack_min_voltage
            i_ch      = min(c_ch * rated, max_current)
            i_dis     = min(c_di * rated, max_current)
            cap_history: list[float] = []

            for cyc in range(1, n_cyc + 1):
                if not self._seq_running.is_set():
                    break
                status(f"CYCLE {cyc}/{n_cyc}: ชาร์จ {i_ch:.2f} A ({c_ch}C)...")
                # ── step 1: CHARGE
                self.sig_cycle_wf.emit(0, "active")
                self.controller.start_charge(strategy=None,
                                             bulk_c_rate_override=c_ch)
                _ch_t0 = _t.time()
                while self._seq_running.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    try:
                        v_c, _, _ = self.hw.read_vi()
                        elapsed_c = int(_t.time() - _ch_t0)
                        status(f"CYCLE {cyc}/{n_cyc} CHARGE: {v_c:.2f} V  "
                               f"({elapsed_c//60}m {elapsed_c%60:02d}s)")
                        self.sig_phase_progress.emit(elapsed_c, 0)
                    except Exception:
                        pass
                    if not self._seq_sleep(30.0):
                        break
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break
                self.sig_cycle_wf.emit(0, "done")

                # ── step 2: REST
                self.sig_cycle_wf.emit(1, "active") if cyc == 1 else None
                t_rest_end = _t.time() + rest_s
                while self._seq_running.is_set():
                    remaining = int(t_rest_end - _t.time())
                    if remaining <= 0:
                        break
                    elapsed_r = rest_s - remaining
                    mins, secs = divmod(remaining, 60)
                    status(f"CYCLE {cyc}/{n_cyc} REST: เหลือ {mins}:{secs:02d}")
                    self.sig_phase_progress.emit(elapsed_r, rest_s)
                    if not self._seq_sleep(10.0):
                        break
                self.sig_phase_progress.emit(0, 0)
                if not self._seq_running.is_set():
                    break

                # ── step 3: DISCHARGE (integrate capacity)
                self.sig_cycle_wf.emit(1, "active")
                status(f"CYCLE {cyc}/{n_cyc}: ดิสชาร์จ {i_dis:.2f} A ({c_di}C)...")
                self.controller._ensure_logging()
                self.hw.set_load(True, str(i_dis))
                _dis_t0 = _t.time()
                _dis_est = int(rated / max(i_dis, 0.01) * 3600)
                ah_acc = 0.0
                last_log = _t.time()
                while self._seq_running.is_set():
                    try:
                        v_d, i_d = self.hw.read_measurements(prefer_load_v=True)
                        now = _t.time()
                        dt  = now - last_log
                        last_log = now
                        ah_acc += abs(i_d) * dt / 3600.0
                        self.controller._log_sample(v_d, -i_d)
                        self._seq_kick_watchdog()
                        elapsed_d = int(now - _dis_t0)
                        temp_d = self.hw.current_temp
                        status(f"CYCLE {cyc}/{n_cyc} DIS: {v_d:.2f} V  "
                               f"{ah_acc:.3f} Ah  SoC ~{max(0, 100-100*ah_acc/rated):.0f}%")
                        self.sig_phase_progress.emit(elapsed_d, _dis_est)
                        if not self._seq_check_otp(temp_d):
                            break
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
                self.lbl_cycle_counter.setText(
                    f"Cycle {cyc}/{n_cyc}  —  {ah_acc:.3f} Ah  ({fade:.1f}% of rated)"
                )
                self.sig_alarm.emit(
                    f"[CYCLE] Cycle {cyc}: {ah_acc:.3f} Ah  ({fade:.1f}%)"
                )
                self.sig_cycle_wf.emit(1, "done")

            self.sig_cycle_wf.emit(2, "done")
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
            self.sig_cycle_wf.emit(3, "done")
            status(f"CYCLE LIFE เสร็จ — {n_cyc} รอบ, ดูผลที่แท็บ Analytics")
            self.sig_alarm.emit("[CYCLE] Cycle Life complete ✓")
            self.sig_seq_done.emit("Cycle Life Test Complete",
                                   f"ทดสอบครบ {len(cap_history)} รอบ\nดูผล capacity fade ที่แท็บ Analytics")

        except Exception as exc:
            self.sig_alarm.emit(f"[CYCLE] Error: {exc}")
            status(f"CYCLE Error: {exc}")
        finally:
            self._seq_running.clear()
            self.sig_phase_progress.emit(0, 0)
            self.hw.load_off()
            self.sig_loading.emit("btn_cycle_life", False, "")
            self.sig_button.emit("btn_seq_cancel", False)

    # ---- characterization test (acquisition worker on the real HAL) -------
    def _acq_profile(self) -> AcqProfile:
        """Analysis/test profile from config (shared with the controller's
        auto-analyze), plus the HPPC durations entered in the Test Config panel."""
        from aset_batt.acquisition.analysis import profile_from_config

        def _fld(widget, default):
            try:
                return max(1.0, float(widget.text()))
            except (ValueError, AttributeError):
                return default

        p = profile_from_config(self.config)
        p.hppc_pulse_duration = _fld(self.ed_hppc_pulse, 30.0)
        p.hppc_relaxation_duration = _fld(self.ed_hppc_relax, 30.0)
        p.hppc_pulse_crate = _fld(self.ed_hppc_crate, 1.0)
        return p

    def _on_run_hppc(self):
        self._on_run_test(mode=OperationMode.HPPC)

    def _on_run_test(self, mode=None):
        if self._test_thread is not None:
            return
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Run Test", "Connect hardware first")
            return
        if self.controller and self.controller.is_charging:
            self.controller.stop_charge()
            self._log_alarm("Charge stopped (auto) — starting test.")
        if self.controller and self.controller.monitor_running:
            self.controller.stop_monitor()

        op_mode = mode or OperationMode(self.cb_op_mode.currentText())
        cfg = TestConfig(self._acq_profile(), op_mode)
        self.buf_t.clear(); self.buf_v.clear(); self.buf_i.clear()
        self.buf_soc.clear(); self.buf_rin.clear(); self.buf_temp.clear()
        self._elapsed_t0 = None
        os.makedirs("sessions", exist_ok=True)
        csv_path = os.path.join("sessions", f"test_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self._last_csv = csv_path
        self.lbl_csv.setText(f"CSV: {csv_path}")

        backend = HardwareBackend(self.hw)
        self._test_thread = QThread()
        self._test_worker = AcquisitionWorker(backend, cfg, csv_path, estimator=self.estimator)
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.telemetry.connect(self._on_test_telemetry)
        self._test_worker.alarm.connect(lambda sev, msg: self._log_alarm(f"[{sev}] {msg}"))
        self._test_worker.state.connect(lambda st: self.lbl_test_status.setText(f"Test: {st}"))
        self._test_worker.finished.connect(self._on_test_finished)
        self._test_worker.finished.connect(self._test_thread.quit)
        self._test_thread.finished.connect(self._cleanup_test_thread)
        if op_mode == OperationMode.HPPC:
            self._test_worker.telemetry.connect(self._on_hppc_telemetry)
            self.btn_run_hppc.setEnabled(False)
            self.lbl_hppc_phase.setText("RUNNING…")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{INFO}; color:white; border:1px solid {BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        else:
            self.btn_run_test.setEnabled(False)
        self._test_thread.start()
        self._log_alarm(f"Characterization started: {cfg.mode.value}")

    def _on_stop_test(self):
        if self._test_worker:
            self._test_worker.stop()
            self._log_alarm("Test stop requested.")

    def _on_test_telemetry(self, row: dict):
        self.buf_t.append(row["elapsed"]); self.buf_v.append(row["v"])
        self.buf_i.append(row["i"]); self.buf_temp.append(row["temp"])
        v_lbl = self.metric_labels.get("Voltage")
        if v_lbl:
            self.metric_labels["Voltage"][0].setText(f'{row["v"]:.3f} {self.metric_labels["Voltage"][1]}')
            self.metric_labels["Current"][0].setText(f'{row["i"]:.3f} {self.metric_labels["Current"][1]}')
            if row.get("soc") == row.get("soc"):  # not NaN
                self.metric_labels["SoC"][0].setText(f'{row["soc"]:.1f} {self.metric_labels["SoC"][1]}')
            self.metric_labels["Temp"][0].setText(f'{row["temp"]:.1f} {self.metric_labels["Temp"][1]}')
        self._temp_gauge.update_temp(
            row["temp"], self.config.system.safety_limits.get("max_temperature", 55) - 10,
            self.config.system.safety_limits.get("max_temperature", 55))
        self.trend.update(list(self.buf_t), list(self.buf_v), list(self.buf_i), list(self.buf_temp))

    def _on_hppc_telemetry(self, row: dict):
        """Update the HPPC phase indicator (REST / PULSE / cycle count) from elapsed time."""
        try:
            pulse = max(1.0, float(self.ed_hppc_pulse.text() or "30"))
            relax = max(1.0, float(self.ed_hppc_relax.text() or "30"))
            elapsed = row["elapsed"]
            cycle = pulse + relax
            cycle_num = int(elapsed / cycle) + 1
            t_in_cycle = elapsed % cycle
            if t_in_cycle < relax:
                remaining = int(relax - t_in_cycle)
                text = f"Cycle {cycle_num}  ·  REST  ({remaining} s left)"
                bg, fg = PANEL2, MUTED
            else:
                remaining = int(pulse - (t_in_cycle - relax))
                text = f"Cycle {cycle_num}  ·  PULSE  ({remaining} s left)"
                bg, fg = OK, "white"
            self.lbl_hppc_phase.setText(text)
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{bg}; color:{fg}; border:1px solid {BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        except Exception:
            pass

    def _on_test_finished(self, results: dict):
        # SoH is N/A when not measurable (e.g. HPPC pulse test — see analyze_series).
        soh = results["soh"]
        soh_txt = "N/A" if soh != soh else f"{soh:.1f}"   # soh != soh → NaN
        self.metric_labels["SoH"][0].setText(
            "N/A" if soh != soh else f'{soh:.1f} {self.metric_labels["SoH"][1]}')
        self.metric_labels["Rin"][0].setText(f'{results["ri_mohm"]:.1f} {self.metric_labels["Rin"][1]}')
        grade = results["grade"]
        gc = {"A": OK, "B": INFO, "C": WARN, "REJECT": CRIT, "REVIEW": NEUTRAL}.get(grade, NEUTRAL)
        conf = results.get("confidence", 1.0)
        self.lbl_grade.setText(grade if grade == "REVIEW" else f"{grade}")
        self.lbl_grade.setStyleSheet(
            f"background:{gc}; color:white; border:1px solid {BORDER}; border-radius:6px; padding:10px;")
        dcir = results.get("dcir_mohm", results.get("ri_mohm", 0.0))
        dstd = results.get("dcir_std_mohm", 0.0)
        nstep = results.get("dcir_n_steps", 0)
        warns = results.get("quality_warnings", [])
        self.lbl_analytics.setText(
            f"Grade {grade} (conf {conf*100:.0f}%) · SoH {soh_txt}% · "
            f"DCIR {dcir:.1f}±{dstd:.1f} mΩ · Sag {results.get('voltage_sag_v', 0.0):.2f} V · "
            f"CCA~{results.get('cca_est_a', 0.0):.0f} A · Cap {results['capacity_ah']:.3f} Ah")
        # 5 Hz-measurable sorting features (see project pivot): SoH + DCIR + sag + CCA proxy
        if results.get("ecm_identified"):
            svg = self._build_ecm_svg(
                r0=results['r0_mohm'], r1=results['r1_mohm'],
                c1=results['c1_farad'], tau=results['tau_s'],
                ocv=results.get('ocv_v', 0.0),
            )
            self.lbl_ecm_diagram.load(QByteArray(svg.encode()))
            self.btn_ecm_toggle.setEnabled(True)
            self.btn_ecm_toggle.setStyleSheet(
                f"QPushButton{{background:{PANEL2};color:{TEXT};border:1px solid {INFO};"
                f"border-radius:4px;padding:3px 8px;text-align:left;}}"
                f"QPushButton:checked{{background:{PANEL};border-color:{INFO};}}"
                f"QPushButton:hover{{border-color:#aaa;}}"
            )
        else:
            # ไม่ใช่ HPPC — แสดงวงจรเดียวกัน แต่ R_d/C_d เป็นตัวแปร (ไม่มีค่า)
            svg = self._build_ecm_svg(
                r0=results.get('dcir_mohm', results.get('ri_mohm', 0.0)),
                ocv=results.get('ocv_v', 0.0),
            )
            self.lbl_ecm_diagram.load(QByteArray(svg.encode()))
            self.btn_ecm_toggle.setEnabled(True)
            self.btn_ecm_toggle.setStyleSheet(
                f"QPushButton{{background:{PANEL2};color:{TEXT};border:1px solid {MUTED};"
                f"border-radius:4px;padding:3px 8px;text-align:left;}}"
                f"QPushButton:checked{{background:{PANEL};border-color:{MUTED};}}"
                f"QPushButton:hover{{border-color:#aaa;}}"
            )
        self.txt_analytics.setHtml(self._build_results_html(results))
        iv, ic = results["ica"]
        if len(iv):
            self.plot_ica.clear(); self.plot_ica.plot(iv, ic, pen=pg.mkPen("#1f4e79", width=2))
        wmsg = f" — {len(warns)} quality flag(s), review" if warns else ""
        # echo the headline grade in the RUN zone (full breakdown is in this tab)
        if hasattr(self, "lbl_run_grade"):
            self.lbl_run_grade.setText(
                f"Grade: {grade} · SoH {soh_txt}% · conf {conf*100:.0f}%")
        if hasattr(self, "lbl_hppc_phase") and results.get("ecm_identified"):
            ecm_r2 = results.get("ecm_r2", 0.0)
            r0 = results.get("r0_mohm", 0.0)
            r1 = results.get("r1_mohm", 0.0)
            tau = results.get("tau_s", 0.0)
            self.lbl_hppc_phase.setText(
                f"DONE · R₀={r0:.1f} mΩ  R₁={r1:.1f} mΩ  τ={tau:.1f} s  R²={ecm_r2:.3f}")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{INFO}; color:white; border:1px solid {BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        self._log_alarm(
            f"Test complete — Grade {grade} (conf {conf*100:.0f}%), "
            f"SoH {soh_txt}%, DCIR {dcir:.1f}±{dstd:.1f} mΩ{wmsg}")

    def _cleanup_test_thread(self):
        if self._test_thread:
            self._test_thread.deleteLater()
        self._test_thread = None
        self._test_worker = None
        self.btn_run_test.setEnabled(True)
        if hasattr(self, "btn_run_hppc"):
            self.btn_run_hppc.setEnabled(True)
        if hasattr(self, "lbl_hppc_phase"):
            self.lbl_hppc_phase.setText("IDLE")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{PANEL2}; color:{MUTED}; border:1px solid {BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        self.lbl_test_status.setText("Test idle")

    def _on_estop(self):
        if self._test_worker:
            self._test_worker.emergency_stop()   # immediate instrument override
        if self.controller:
            self.controller._trigger_safety("E-STOP pressed by operator")
        self._log_alarm("⛔ E-STOP issued.")

    def _populate_profiles(self):
        self.cb_profiles.clear()
        self._profile_map.clear()
        for tid in self.iec_standard.get_available_tests():
            prof = self.iec_standard.get_test_profile(tid)
            if not prof:
                continue
            disp = f"[IEC] {prof.name}"
            self.cb_profiles.addItem(disp)
            self._profile_map[disp] = ("iec", tid)

    def _on_run_profile(self):
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Profile", "Connect hardware first")
            return
        sel = self.cb_profiles.currentText()
        if not sel:
            if not self._headless:
                QMessageBox.warning(self, "Profile", "Select a profile first")
            return
        ptype, pid = self._profile_map.get(sel, (None, None))
        try:
            if ptype == "iec":
                self.controller.start_iec61960_test(pid, self.iec_standard)
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "Profile Error", str(exc))

    def _on_start_monitor(self):
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Monitor", "Connect hardware first")
            return
        self.controller.start_monitor()
        self._elapsed_t0 = datetime.now().timestamp()
        self.status_label.setText("Monitor running")
        # แสดงชื่อ session file ที่เพิ่งสร้าง
        if self.data and self.data.current_path:
            self._last_csv = self.data.current_path
            self.lbl_csv.setText(f"CSV: {os.path.basename(self.data.current_path)}")

    # map ชนิดการทดสอบ → ชื่อย่อที่อ่านง่าย (จากคอลัมน์ Mode ใน CSV)
    _SESSION_TYPE_MAP = {
        "hppc": "HPPC",
        "discharge": "Discharge",
        "charge": "Charge",
    }

    def _detect_session_type(self, fpath: str) -> str:
        """อ่านคอลัมน์ Mode ของ CSV เพื่อบอกชนิดการทดสอบ.
        ไฟล์จาก START DATA LOGGING ไม่มีคอลัมน์ Mode → 'Data Log'."""
        try:
            with open(fpath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return "—"
                mode_idx = next((i for i, h in enumerate(header)
                                 if h.strip().lower() == "mode"), None)
                if mode_idx is None:
                    return "Data Log"
                modes = set()
                for n, row in enumerate(reader):
                    if mode_idx < len(row) and row[mode_idx]:
                        modes.add(row[mode_idx].lower())
                    if n > 500:          # อ่านพอประมาณ — ชนิดไม่เปลี่ยนกลางคัน
                        break
                if not modes:
                    return "Data Log"
                for key, label in self._SESSION_TYPE_MAP.items():
                    if any(key in m for m in modes):
                        return label
                return next(iter(modes)).title()
        except OSError:
            return "—"

    @staticmethod
    def _format_session_time(fname: str) -> str:
        """แปลง test_YYYYMMDD_HHMMSS.csv → '28 Jun 2026  18:47'."""
        try:
            stem = fname[len("test_"):-len(".csv")]
            dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
            return dt.strftime("%d %b %Y  %H:%M")
        except ValueError:
            return fname

    # ── Session metadata (rename / tag) ──────────────────────────────────
    _SESSION_META_FILE = os.path.join("sessions", ".session_meta.json")

    def _load_session_meta(self) -> dict:
        try:
            import json as _json
            with open(self._SESSION_META_FILE, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _save_session_meta(self, meta: dict):
        try:
            import json as _json
            os.makedirs("sessions", exist_ok=True)
            with open(self._SESSION_META_FILE, "w", encoding="utf-8") as f:
                _json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log_alarm(f"session meta save failed: {e}")

    def _on_session_context_menu(self, pos):
        item = self.lst_sessions.itemAt(pos)
        if item is None:
            return
        fpath = item.data(Qt.ItemDataRole.UserRole)
        fname = os.path.basename(fpath)
        from PySide6.QtWidgets import QMenu, QInputDialog
        menu = QMenu(self)
        act_rename = menu.addAction("✏  Rename / Label")
        act_tag    = menu.addAction("🏷  Add Tag")
        act_clear  = menu.addAction("✗  Clear Label & Tag")
        action = menu.exec(self.lst_sessions.mapToGlobal(pos))
        meta = self._load_session_meta()
        entry = meta.get(fname, {})
        if action == act_rename:
            text, ok = QInputDialog.getText(self, "Rename Session",
                                            "Label:", text=entry.get("label", ""))
            if ok:
                entry["label"] = text.strip()
                meta[fname] = entry
                self._save_session_meta(meta)
                self._refresh_session_list()
        elif action == act_tag:
            text, ok = QInputDialog.getText(self, "Add Tag",
                                            "Tag:", text=entry.get("tag", ""))
            if ok:
                entry["tag"] = text.strip()
                meta[fname] = entry
                self._save_session_meta(meta)
                self._refresh_session_list()
        elif action == act_clear:
            meta.pop(fname, None)
            self._save_session_meta(meta)
            self._refresh_session_list()

    def _refresh_session_list(self):
        """อัพเดทรายการ session files จาก sessions/ directory.
        แสดง: ลำดับ · ชนิดการทดสอบ · วันเวลา · ขนาด · label/tag ถ้ามี"""
        if not hasattr(self, "lst_sessions"):
            return
        self.lst_sessions.clear()
        logs_dir = "sessions"
        if not os.path.isdir(logs_dir):
            return
        meta = self._load_session_meta()
        files = sorted(
            [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")],
            reverse=True,
        )
        for seq, fname in enumerate(files, start=1):
            fpath = os.path.join(logs_dir, fname)
            ttype = self._detect_session_type(fpath)
            when = self._format_session_time(fname)
            try:
                size_kb = os.path.getsize(fpath) / 1024
                size_txt = f"{size_kb:.0f} KB"
            except OSError:
                size_txt = "—"
            entry   = meta.get(fname, {})
            label_s = f"  [{entry['label']}]" if entry.get("label") else ""
            tag_s   = f"  #{entry['tag']}" if entry.get("tag") else ""
            label   = f"{seq}.  {ttype:<10}{when}   ·  {size_txt}{label_s}{tag_s}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, fpath)
            item.setToolTip(f"{fname}\nType: {ttype}\n{when}  ·  {size_txt}"
                            f"{label_s}{tag_s}\nRight-click to rename/tag")
            self.lst_sessions.addItem(item)

    def _on_session_selected(self, item):
        """เลือก session file → analyze ทันทีในแท็บ Analytics เดียวกัน"""
        fpath = item.data(Qt.ItemDataRole.UserRole)
        if fpath:
            self._last_csv = fpath
            self.lbl_csv.setText(f"CSV: {os.path.basename(fpath)}")
            self._on_analyze_csv()

    def _on_toggle_logging(self):
        if self.data is None:
            return
        if self.data.is_recording:
            self.data.stop_logging()
            self.btn_log.setText("START DATA LOGGING")
            self._refresh_session_list()
        else:
            from aset_batt.storage.data_utils import DataHandler
            csv_path = DataHandler.make_session_path()
            ok, msg = self.data.start_logging(csv_path)
            if ok:
                self.btn_log.setText("STOP DATA LOGGING")
                self._last_csv = csv_path
                self.lbl_csv.setText(f"CSV: {os.path.basename(csv_path)}")
            elif not self._headless:
                QMessageBox.critical(self, "Logging", msg)

    def _on_pdf_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF Report", "battery_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        self.btn_pdf.setEnabled(False)
        self.btn_pdf.setText("Generating...")
        task = _PdfTask(self._pdf_notifier, path, self.config, self.estimator,
                        self._last_analysis, self._last_csv or self.config.system.csv_filepath)
        self.thread_pool.start(task)

    def _on_pdf_finished(self, ok: bool, payload: str):
        self.btn_pdf.setEnabled(True)
        self.btn_pdf.setText("Generate PDF Report")
        if ok:
            self._log_alarm(f"PDF generated: {payload}")
            if not self._headless:
                QMessageBox.information(self, "PDF Report", f"Saved:\n{payload}")
        else:
            self._log_alarm(f"PDF failed: {payload}")
            if not self._headless:
                QMessageBox.critical(self, "PDF Report", payload)

    # ── SoH Trend chart ──────────────────────────────────────────────────
    def _on_soh_trend(self):
        """Parse all sessions for SoH, show a matplotlib window with timeline."""
        import threading
        threading.Thread(target=self._soh_trend_worker, daemon=True).start()

    def _soh_trend_worker(self):
        try:
            import matplotlib
            matplotlib.use("Qt5Agg")
            import matplotlib.pyplot as plt
            from aset_batt.acquisition.analysis import analyze_csv, profile_from_config

            logs_dir = "sessions"
            if not os.path.isdir(logs_dir):
                return
            files = sorted(
                [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")]
            )
            profile = profile_from_config(self.config)
            dates, sohs, labels = [], [], []
            for fname in files:
                fpath = os.path.join(logs_dir, fname)
                try:
                    res = analyze_csv(fpath, profile)
                    import math
                    if not math.isnan(res.get("soh", float("nan"))):
                        from datetime import datetime as _dt
                        stem = fname[len("test_"):-len(".csv")]
                        d = _dt.strptime(stem, "%Y%m%d_%H%M%S")
                        dates.append(d)
                        sohs.append(res["soh"])
                        meta = self._load_session_meta()
                        e = meta.get(fname, {})
                        labels.append(e.get("label") or e.get("tag") or stem[-6:])
                except Exception:
                    continue

            if not sohs:
                self.sig_alarm.emit("[TREND] No sessions with valid SoH found")
                return
            fig, ax = plt.subplots(figsize=(9, 4))
            ax.plot(dates, sohs, "o-", color="#005a9e", linewidth=1.8)
            for d, s, lb in zip(dates, sohs, labels):
                ax.annotate(f"{s:.1f}%", (d, s), textcoords="offset points",
                            xytext=(0, 7), ha="center", fontsize=8)
            ax.axhline(80, color="orange", linestyle="--", linewidth=0.9, label="80% SoH limit")
            ax.set_ylabel("SoH (%)")
            ax.set_title("State of Health Trend")
            ax.legend()
            ax.grid(True, alpha=0.3)
            fig.autofmt_xdate()
            fig.tight_layout()
            plt.show()
        except Exception as e:
            self.sig_alarm.emit(f"[TREND] Error: {e}")

    # ── Capacity Fade chart ───────────────────────────────────────────────
    def _on_capacity_fade(self):
        """Parse cycle-life sessions and show capacity fade bar chart."""
        import threading
        threading.Thread(target=self._capacity_fade_worker, daemon=True).start()

    def _capacity_fade_worker(self):
        try:
            import matplotlib
            matplotlib.use("Qt5Agg")
            import matplotlib.pyplot as plt
            import csv as _csv

            logs_dir = "sessions"
            if not os.path.isdir(logs_dir):
                return
            files = sorted(
                [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")]
            )
            # collect per-session capacity
            session_caps = []
            session_labels = []
            meta = self._load_session_meta()
            for fname in files:
                fpath = os.path.join(logs_dir, fname)
                try:
                    cap_ah = 0.0
                    with open(fpath, encoding="utf-8-sig") as f:
                        reader = _csv.DictReader(f)
                        rows = list(reader)
                    # find last Capacity_Ah value
                    for row in reversed(rows):
                        v = row.get("Capacity_Ah") or row.get("capacity_ah", "")
                        try:
                            cap_ah = float(v)
                            break
                        except (ValueError, TypeError):
                            continue
                    if cap_ah > 0.01:
                        e = meta.get(fname, {})
                        stem = fname[len("test_"):-len(".csv")]
                        session_caps.append(cap_ah)
                        session_labels.append(e.get("label") or stem[-8:])
                except Exception:
                    continue

            if not session_caps:
                self.sig_alarm.emit("[FADE] No sessions with capacity data found")
                return

            fig, ax = plt.subplots(figsize=(max(6, len(session_caps) * 0.6 + 2), 4))
            colors_list = ["#005a9e" if c >= session_caps[0] * 0.8 else "#d83b01"
                           for c in session_caps]
            bars = ax.bar(range(len(session_caps)), session_caps, color=colors_list)
            ax.set_xticks(range(len(session_caps)))
            ax.set_xticklabels(session_labels, rotation=45, ha="right", fontsize=8)
            ax.set_ylabel("Capacity (Ah)")
            ax.set_title("Capacity Fade — Session History")
            ax.axhline(session_caps[0] * 0.8, color="orange", linestyle="--",
                       linewidth=0.9, label="80% of first session")
            for bar, cap in zip(bars, session_caps):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.002,
                        f"{cap:.3f}", ha="center", va="bottom", fontsize=7)
            ax.legend()
            ax.grid(axis="y", alpha=0.3)
            fig.tight_layout()
            plt.show()
        except Exception as e:
            self.sig_alarm.emit(f"[FADE] Error: {e}")

    def _on_cloud_push_toggle(self, state):
        enabled = bool(state)
        self.config.system.cloud_push_enabled = enabled
        self.config.save_config()
        if enabled:
            self._cloud_push_stop()
            self._cloud_push_start()
        else:
            self._cloud_push_stop()
        self._log_alarm(f"[CLOUD] Push {'enabled' if enabled else 'disabled'}")

    def _on_cloud_url_changed(self):
        url = self.ed_cloud_url.text().strip()
        self.config.system.cloud_dashboard_url = url
        self.config.save_config()
        # restart push service with new URL
        if getattr(self.config.system, "cloud_push_enabled", False):
            self._cloud_push_stop()
            self._cloud_push_start()

    def _on_open_dashboard(self):
        url = getattr(self.config.system, "cloud_dashboard_url", "").strip()
        if url:
            webbrowser.open(url)
            return
        # Local web server removed; inform the user instead of opening localhost
        if not self._headless:
            QMessageBox.information(self, "Cloud Dashboard", "Cloud dashboard URL not configured. See cloud_dashboard/README.md for deployment instructions.")
        else:
            logger.warning("Cloud dashboard URL not configured")

    def _on_analyze_csv(self):
        csv_path = self._last_csv or self.config.system.csv_filepath
        if not csv_path or not os.path.exists(csv_path):
            if not self._headless:
                QMessageBox.warning(self, "Analyze CSV",
                                    f"CSV not found:\n{csv_path}\n\nRun a test first.")
            return
        self.lbl_analytics.setText(f"Analyzing {os.path.basename(csv_path)}...")
        prof = self._acq_profile()

        def work():
            from aset_batt.acquisition.analysis import analyze_csv
            try:
                res = analyze_csv(csv_path, prof)
            except Exception as e:
                res = {"error": str(e)}
            self.sig_analysis_done.emit(res)   # → _slot_analysis_done → _on_test_finished

        threading.Thread(target=work, daemon=True).start()

    def _show_text_dialog(self, title, text):
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text[:4000])
        dlg.exec()

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

    def _char_guard(self) -> bool:
        """Return True if OK to start a new test.  Shows a warning if not."""
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE", "Connect hardware first.")
            return False
        if self._seq_running.is_set():
            if not self._headless:
                QMessageBox.warning(self, "CHARACTERIZE",
                                    "AUTO sequence is running — stop it first.")
            return False
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
            self.sig_char_update.emit("pk", msg)
            self.sig_alarm.emit(f"[CHAR/Peukert] {msg}")

        try:
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

                # ── charge to full ─────────────────────────────────────────
                self.controller.start_charge(strategy=None)
                while ev.is_set():
                    if not getattr(self.controller, "is_charging", False):
                        break
                    if not self._char_sleep(ev, 30.0):
                        return

                if not ev.is_set():
                    return

                # ── rest 5 min ─────────────────────────────────────────────
                status(f"({idx+1}/4) พักหลังชาร์จ 5 นาที...")
                if not self._char_sleep(ev, 300):
                    return

                # ── discharge at i_test until UVP ──────────────────────────
                status(f"({idx+1}/4) discharge {i_test:.3f} A ({c:g}C)...")
                t0 = time.time()
                self.hw.set_load(True, i_test)
                last_log = t0

                while ev.is_set():
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        temp = self.hw.current_temp
                        now  = time.time()
                        dt   = now - last_log
                        last_log = now
                        self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                        elapsed = int(now - t0)
                        status(f"({idx+1}/4) {c:g}C — {v:.2f} V  {i_meas:.3f} A  "
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
            else:
                status("⚠ ได้ข้อมูลไม่พอ fit — ต้องการ ≥ 2 discharge runs")

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
            self.sig_char_update.emit("eta", msg)
            self.sig_alarm.emit(f"[CHAR/η] {msg}")

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
            rated    = self.controller.config.battery.rated_capacity
            pack_min = self.controller.config.battery.pack_min_voltage

            # ── Phase 1: Charge to full; track Ah_in per SoC band ─────────
            status("Phase 1/2: ชาร์จ (นับ Ah_in ต่อ band)...")
            ah_in  = {"bulk": 0.0, "absorb": 0.0, "full": 0.0}
            self.controller.start_charge(strategy=None)
            last = time.time()

            while ev.is_set():
                if not getattr(self.controller, "is_charging", False):
                    break
                try:
                    v, i_ch = self.hw.read_measurements(prefer_load_v=False)
                    temp = self.hw.current_temp
                    now = time.time()
                    dt  = now - last
                    last = now
                    state = self.controller.estimator.update(v, i_ch, dt=dt, temp=temp)
                    soc_now = state["soc"]
                    # i_ch is negative during charging; accumulate absolute Ah
                    dah = abs(i_ch) * dt / 3600.0
                    ah_in[_band(soc_now)] += dah
                    status(f"Charge: {v:.2f} V  SoC {soc_now:.0f}%  "
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
            if not self._char_sleep(ev, 1800):
                return

            # OCV anchor
            soc_now = self.controller.calibrate_from_ocv()

            # ── Phase 2: Discharge at 0.1C; track Ah_out per SoC band ─────
            i_dis = round(0.1 * rated, 3)
            status(f"Phase 2/2: discharge {i_dis:.3f} A (0.1C, นับ Ah_out)...")
            ah_out = {"bulk": 0.0, "absorb": 0.0, "full": 0.0}
            self.hw.set_load(True, i_dis)
            last = time.time()

            while ev.is_set():
                try:
                    v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                    temp = self.hw.current_temp
                    now  = time.time()
                    dt   = now - last
                    last = now
                    state = self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
                    soc_now = state["soc"]
                    dah = abs(i_meas) * dt / 3600.0
                    ah_out[_band(soc_now)] += dah
                    status(f"Discharge: {v:.2f} V  SoC {soc_now:.0f}%  "
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
            self.sig_char_update.emit("gitt", msg)
            self.sig_alarm.emit(f"[CHAR/GITT] {msg}")

        try:
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
            if not ev.is_set():
                return

            for step in range(N_STEPS):
                if not ev.is_set():
                    return

                status(f"Step {step+1}/{N_STEPS}: discharge {i_dis:.3f} A × {dis_dur//60} min...")
                self.hw.set_load(True, i_dis)
                last = time.time()

                # ── discharge phase ────────────────────────────────────────
                while ev.is_set() and (time.time() - last) < dis_dur:
                    try:
                        v, i_meas = self.hw.read_measurements(prefer_load_v=True)
                        temp = self.hw.current_temp
                        now  = time.time()
                        dt   = now - last
                        last = now
                        self.controller.estimator.update(v, i_meas, dt=dt, temp=temp)
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
                t_rest0 = time.time()
                v_window: list = []
                t_window: list = []
                v_rest   = None

                while ev.is_set() and (time.time() - t_rest0) < REST_MAX_S:
                    try:
                        v_now, _, _ = self.hw.read_vi()
                        t_now = time.time()
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
            else:
                status(f"⚠ ได้ข้อมูล {len(soc_points)} จุด — ต้องการ ≥ 3 จุด")

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


    def closeEvent(self, event):
        if self._headless:
            try:
                if self.controller:
                    self.controller.shutdown()
            except Exception as exc:
                logger.error("shutdown on close: %s", exc)
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            "Quit",
            "Close the program and stop the test?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if self.controller:
                    self.controller.shutdown()
            except Exception as exc:
                logger.error("shutdown on close: %s", exc)
            event.accept()
        else:
            event.ignore()
