"""
PySide6 ISA-101 HMI for ASET Battery Tester.

This is the supported desktop UI for the main application. It keeps the
existing controller / estimator / analysis contracts, but presents them in the
desaturated high-performance style used by the standalone command center.
"""

import logging
import math
import os
import threading
import webbrowser
from collections import deque
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt, QThread, QRunnable, QThreadPool

from aset_batt.acquisition.models import TestConfig, OperationMode, BatteryProfile as AcqProfile
from aset_batt.acquisition.backends import HardwareBackend
from aset_batt.acquisition.worker import AcquisitionWorker
from PySide6.QtGui import QDoubleValidator, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
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

        max_pts = self.config.system.max_points
        self.buf_t = deque(maxlen=max_pts)
        self.buf_v = deque(maxlen=max_pts)
        self.buf_i = deque(maxlen=max_pts)
        self.buf_soc = deque(maxlen=max_pts)
        self.buf_rin = deque(maxlen=max_pts)
        self.buf_temp = deque(maxlen=max_pts)
        self._elapsed_t0 = None
        self._sample_index = 0
        self._buttons = {}
        self._profile_map = {}
        self._last_analysis = None
        self._test_thread = None      # characterization worker (QThread)
        self._test_worker = None
        self._last_csv = None         # CSV written by the most recent test/monitor run

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
        self._refresh_battery_readout()
        self._update_connection_status()

    def _build_ui(self):
        self.setWindowTitle("ASET Battery Tester — ISA-101 Command Center")
        self.resize(1600, 980)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; font-family:'Segoe UI','Inter',sans-serif; font-size:12px; }}
            QGroupBox {{ border:1px solid {BORDER}; border-radius:4px; margin-top:12px; background:{PANEL}; font-weight:700; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:1px 6px; color:{TEXT}; background:{PANEL}; letter-spacing:1px; }}
            QLabel {{ background:transparent; }}
            QComboBox, QLineEdit {{ background:{FIELD}; border:1px solid {BORDER}; border-radius:3px; padding:4px 6px; color:{TEXT}; }}
            QComboBox:focus, QLineEdit:focus {{ border:1px solid {INFO}; }}
            QComboBox QAbstractItemView {{ background:{FIELD}; color:{TEXT}; selection-background-color:{INFO}; selection-color:white; }}
            QListWidget {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}
            QListWidget::item {{ padding:5px 6px; }}
            QListWidget::item:selected {{ background:{INFO}; color:white; }}
            QTextEdit {{ background:{PANEL2}; border:1px solid {BORDER}; color:{TEXT}; }}
            QTabWidget::pane {{ border:1px solid {BORDER}; background:{PANEL2}; }}
            QTabBar::tab {{ background:{PANEL}; padding:6px 14px; border:1px solid {BORDER}; border-bottom:0; color:{MUTED}; }}
            QTabBar::tab:selected {{ background:{PANEL2}; color:{TEXT}; font-weight:700; }}
            """
        )

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(8)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 1210])
        root.addWidget(splitter, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(
            f"color:{MUTED}; padding:5px 10px; background:{PANEL}; border:1px solid {BORDER}; border-radius:4px;"
        )
        root.addWidget(self.status_label)

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

    def _build_left_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 4)
        lay.setSpacing(8)
        lay.addWidget(self._block_config())
        lay.addWidget(self._block_connection())
        lay.addWidget(self._block_manual())
        lay.addWidget(self._block_operations())
        lay.addWidget(self._block_safety())
        lay.addWidget(self._block_data())
        lay.addStretch(1)

        # Wrap in a scroll area so the sidebar never compresses its widgets when the
        # window is too short (e.g. maximized on a low-height screen) — it scrolls
        # instead of overlapping comboboxes/lists with their labels.
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(340)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        return scroll

    def _block_config(self):
        g = QGroupBox("1 · TEST CONFIGURATION")
        lay = QVBoxLayout(g)

        row = QHBoxLayout()
        row.addWidget(QLabel("Battery:"))
        self.cb_product = QComboBox()
        self.cb_product.addItems(battery_profiles.list_products())
        self.cb_product.currentTextChanged.connect(self._on_product_changed)
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

        # HPPC pulse timing (used by the HPPC test mode). Longer pulse/relaxation
        # lets the RC transient fully develop so R1/C1 are not under-resolved.
        lay.addWidget(_hline())
        hppc = QFormLayout()
        self.ed_hppc_pulse = QLineEdit("30")
        self.ed_hppc_pulse.setValidator(QDoubleValidator(1.0, 600.0, 1))
        self.ed_hppc_relax = QLineEdit("30")
        self.ed_hppc_relax.setValidator(QDoubleValidator(1.0, 600.0, 1))
        hppc.addRow("HPPC pulse (s):", self.ed_hppc_pulse)
        hppc.addRow("HPPC relax (s):", self.ed_hppc_relax)
        lay.addLayout(hppc)
        return g

    def _block_connection(self):
        g = QGroupBox("2 · CONNECTIONS")
        lay = QVBoxLayout(g)
        self.cb_psu = QComboBox()
        self.cb_load = QComboBox()
        self.cb_esp = QComboBox()
        form = QFormLayout()
        form.addRow("PSU (VISA):", self.cb_psu)
        form.addRow("Load (VISA):", self.cb_load)
        form.addRow("ESP32 (COM):", self.cb_esp)
        lay.addLayout(form)

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
        return g

    def _block_manual(self):
        g = QGroupBox("3 · MANUAL CONTROL")
        lay = QVBoxLayout(g)

        row = QHBoxLayout()
        row.addWidget(QLabel("PSU V:"))
        self.ed_psu_v = QLineEdit("13.8")
        self.ed_psu_v.setMaximumWidth(72)
        row.addWidget(self.ed_psu_v)
        on = _btn("ON", bg=OK, fg="white", hover="#266a2a")
        off = _btn("OFF", bg="#d0d4d7", hover="#c2c6ca")
        on.clicked.connect(lambda: self._psu_manual(True))
        off.clicked.connect(lambda: self._psu_manual(False))
        row.addWidget(on)
        row.addWidget(off)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Load A:"))
        self.ed_load_a = QLineEdit("0.7")
        self.ed_load_a.setMaximumWidth(72)
        row.addWidget(self.ed_load_a)
        on = _btn("ON", bg=OK, fg="white", hover="#266a2a")
        off = _btn("OFF", bg="#d0d4d7", hover="#c2c6ca")
        on.clicked.connect(lambda: self._load_manual(True))
        off.clicked.connect(lambda: self._load_manual(False))
        row.addWidget(on)
        row.addWidget(off)
        lay.addLayout(row)
        return g

    def _block_operations(self):
        g = QGroupBox("4 · OPERATIONS")
        lay = QVBoxLayout(g)

        # charge-mode selector — Auto follows battery chemistry, or force a strategy
        mrow0 = QHBoxLayout()
        mrow0.addWidget(QLabel("Charge mode:"))
        self.cb_charge_mode = QComboBox()
        self.cb_charge_mode.addItems(["Auto (by chemistry)", "CC-CV", "3-Stage (Lead-Acid)"])
        mrow0.addWidget(self.cb_charge_mode, 1)
        lay.addLayout(mrow0)

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

        # Characterization test — worker-driven (CC-CV / Discharge / HPPC) → ICA/DTV/grade
        lay.addWidget(_hline())
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Test mode:"))
        self.cb_op_mode = QComboBox()
        self.cb_op_mode.addItems([m.value for m in OperationMode])
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

        lay.addWidget(_hline())
        self.lst_profiles = QListWidget()
        self.lst_profiles.setMaximumHeight(120)
        self._populate_profiles()
        lay.addWidget(self.lst_profiles)

        prow = QHBoxLayout()
        self.btn_start_profile = _btn("RUN", bg=INFO, fg="white", hover="#0d4a89")
        self.btn_start_profile.clicked.connect(self._on_run_profile)
        self.btn_stop_profile = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_stop_profile.clicked.connect(lambda: self.controller and self.controller.stop_profile())
        self._buttons["btn_start_profile"] = self.btn_start_profile
        prow.addWidget(self.btn_start_profile)
        prow.addWidget(self.btn_stop_profile)
        lay.addLayout(prow)
        self.lbl_profile_status = QLabel("No profile selected")
        self.lbl_profile_status.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_profile_status)

        lay.addWidget(_hline())
        mrow = QHBoxLayout()
        self.btn_start_monitor = _btn("START MONITOR", bg=OK, fg="white", hover="#266a2a")
        self.btn_stop_monitor = _btn("STOP", bg=CRIT, fg="white", hover="#9b2020")
        self.btn_start_monitor.clicked.connect(self._on_start_monitor)
        self.btn_stop_monitor.clicked.connect(lambda: self.controller and self.controller.stop_monitor())
        self._buttons["btn_start_monitor"] = self.btn_start_monitor
        mrow.addWidget(self.btn_start_monitor)
        mrow.addWidget(self.btn_stop_monitor)
        lay.addLayout(mrow)
        return g

    def _block_safety(self):
        g = QGroupBox("5 · SAFETY")
        lay = QVBoxLayout(g)
        self.btn_estop = QPushButton("⛔  EMERGENCY STOP")
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{CRIT}; color:white; border:none; border-radius:8px; padding:18px; font-size:16px; font-weight:800; }}"
            f"QPushButton:hover {{ background:#9b2020; }}"
        )
        self.btn_estop.setCursor(Qt.PointingHandCursor)
        self.btn_estop.clicked.connect(self._on_estop)
        lay.addWidget(self.btn_estop)
        return g

    def _block_data(self):
        g = QGroupBox("6 · DATA")
        lay = QVBoxLayout(g)
        self.lbl_csv = QLabel(f"CSV: {self.config.system.csv_filepath}")
        self.lbl_csv.setStyleSheet(f"color:{MUTED};")
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
        return g

    def _build_right_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self.metric_labels = {}
        for name, unit in [("Voltage", "V"), ("Current", "A"), ("SoC", "%"), ("Rin", "mΩ"), ("Temp", "°C"), ("SoH", "%")]:
            cards_row.addWidget(self._metric_card(name, unit), 1)
        lay.addLayout(cards_row)

        self._temp_gauge = TemperatureGauge()
        lay.addWidget(self._temp_gauge)

        self.trend = MultiAxisTrend()
        lay.addWidget(self.trend, 2)

        tabs = QTabWidget()
        tabs.addTab(self._tab_analytics(), "Analytics")
        tabs.addTab(self._tab_diagnostics(), "Diagnostics (ICA/DTV)")
        tabs.addTab(self._tab_alarms(), "Alarm Log")
        lay.addWidget(tabs, 1)
        return panel

    def _tab_diagnostics(self):
        """Post-test ICA dQ/dV + DTV dT/dV curves (populated by the worker)."""
        w = QWidget()
        lay = QHBoxLayout(w)
        self.plot_ica = pg.PlotWidget()
        self.plot_ica.setBackground(PANEL2)
        self.plot_ica.setLabel("bottom", "Voltage", units="V")
        self.plot_ica.setLabel("left", "dQ/dV")
        self.plot_ica.setTitle("ICA (Incremental Capacity)")
        self.plot_dtv = pg.PlotWidget()
        self.plot_dtv.setBackground(PANEL2)
        self.plot_dtv.setLabel("bottom", "Voltage", units="V")
        self.plot_dtv.setLabel("left", "dT/dV")
        self.plot_dtv.setTitle("DTV (Differential Thermal)")
        lay.addWidget(self.plot_ica, 1)
        lay.addWidget(self.plot_dtv, 1)
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
        val = QLabel(f"0.0 {unit}")
        val.setFont(QFont("Consolas", 19, QFont.Weight.Bold))
        val.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(t)
        lay.addWidget(val)
        self.metric_labels[name] = (val, unit)
        return card

    def _tab_analytics(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_analytics = QLabel("No analysis yet — run a profile or analyze the latest CSV.")
        self.lbl_analytics.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_analytics)
        self.txt_analytics = QTextEdit()
        self.txt_analytics.setReadOnly(True)
        self.txt_analytics.setFont(QFont("Consolas", 10))
        lay.addWidget(self.txt_analytics, 1)
        self.lbl_grade = QLabel("—")
        self.lbl_grade.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_grade.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
        self.lbl_grade.setStyleSheet(f"background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; border-radius:6px; padding:10px;")
        lay.addWidget(self.lbl_grade)
        btn = _btn("Analyze Last CSV", bg=INFO, fg="white", hover="#0d4a89")
        btn.clicked.connect(self._on_analyze_csv)
        lay.addWidget(btn)
        return w

    def _tab_alarms(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_alarms = QTextEdit()
        self.txt_alarms.setReadOnly(True)
        self.txt_alarms.setFont(QFont("Consolas", 10))
        lay.addWidget(self.txt_alarms, 1)
        self._log_alarm("System ready.")
        return w

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
        values = {"Voltage": v, "Current": i, "SoC": soc, "Rin": rin_mohm, "Temp": temp, "SoH": soh}
        for name, (lbl, unit) in self.metric_labels.items():
            fmt = "{:.2f}" if name not in ("SoC", "SoH") else "{:.1f}"
            lbl.setText(f"{fmt.format(values[name])} {unit}")

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

        self._update_temp_gauge(temp)
        self.status_label.setText(f"V={v:.2f} V  I={i:.2f} A  SoC={soc:.1f}%  Rin={rin_mohm:.1f} mΩ  Temp={temp:.1f} °C")

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
        connected = bool(getattr(self.hw, "is_connected", False))
        led = OK if connected else NEUTRAL
        self.conn_led.setStyleSheet(f"color:{led}; font-size:16px;")
        self.conn_text.setText("Connected" if connected else "Disconnected")
        self.conn_text.setStyleSheet(f"color:{OK if connected else MUTED}; font-weight:600;")
        self.status_label.setText("Hardware connected" if connected else "Ready — connect hardware to begin")

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

    def _log_alarm(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_alarms.append(f"[{ts}] {msg}")

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
        if self.config.system.safety_limits:
            if prod.safety_ovp_pack:
                self.config.system.safety_limits["max_voltage"] = prod.safety_ovp_pack
            if prod.safety_uvp_pack:
                self.config.system.safety_limits["min_voltage"] = prod.safety_uvp_pack
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
        self._refresh_battery_readout()
        self._log_alarm(f"Selected product: {name} → {prod.chemistry} {prod.cells_series}S")

    def _on_save_default(self):
        if self.config.save_config():
            self._log_alarm("Saved as default (config.json).")
            if not self._headless:
                QMessageBox.information(self, "Save as Default", "config.json saved")
        elif not self._headless:
            QMessageBox.critical(self, "Save as Default", "Save failed")

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
                self.hw.connect_esp32(esp, baudrate=baud)
            self.config.hardware.psu_port = psu
            self.config.hardware.load_port = load
            self.config.hardware.esp_port = esp
            self.config.save_config()
            self._update_connection_status()
            self._log_alarm("Hardware connected.")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "Connect Error", str(exc))

    def _on_disconnect(self):
        try:
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            self._update_connection_status()
            self._log_alarm("Hardware disconnected.")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "Disconnect Error", str(exc))

    def _psu_manual(self, on):
        try:
            self.hw.set_psu(on, str(float(self.ed_psu_v.text())) if on else "0")
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "PSU", "Invalid voltage")

    def _load_manual(self, on):
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
        return p

    def _on_run_test(self):
        if self._test_thread is not None:
            return
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Run Test", "Connect hardware first")
            return
        if self.controller and (self.controller.is_charging or self.controller.monitor_running):
            if not self._headless:
                QMessageBox.warning(self, "Run Test", "Stop charge/monitor before running a test")
            return

        cfg = TestConfig(self._acq_profile(), OperationMode(self.cb_op_mode.currentText()))
        self.buf_t.clear(); self.buf_v.clear(); self.buf_i.clear()
        self.buf_soc.clear(); self.buf_temp.clear()
        os.makedirs("logs", exist_ok=True)
        csv_path = os.path.join("logs", f"test_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self._last_csv = csv_path                       # most-recent run → Analyze/PDF use this
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
        self._test_thread.start()
        self.btn_run_test.setEnabled(False)
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

    def _on_test_finished(self, results: dict):
        self.metric_labels["SoH"][0].setText(f'{results["soh"]:.1f} {self.metric_labels["SoH"][1]}')
        self.metric_labels["Rin"][0].setText(f'{results["ri_mohm"]:.1f} {self.metric_labels["Rin"][1]}')
        grade = results["grade"]
        gc = {"A": OK, "B": INFO, "C": WARN, "REJECT": CRIT}.get(grade, NEUTRAL)
        self.lbl_grade.setText(grade)
        self.lbl_grade.setStyleSheet(
            f"background:{gc}; color:white; border:1px solid {BORDER}; border-radius:6px; padding:10px;")
        ecm = " · 1-RC ECM" if results.get("ecm_identified") else ""
        self.lbl_analytics.setText(
            f"Grade {grade}{ecm} · SoH {results['soh']:.1f}% · "
            f"R0 {results['r0_mohm']:.1f} mΩ · R1 {results['r1_mohm']:.1f} mΩ · "
            f"Cap {results['capacity_ah']:.3f} Ah")
        # detailed ECM breakdown
        header = "1-RC Thevenin ECM (HPPC)" if results.get("ecm_identified") \
            else "Internal resistance (single-point)"
        self.txt_analytics.setPlainText("\n".join([
            f"Grade:                 {grade}",
            f"State of Health:       {results['soh']:.1f} %",
            f"Capacity:              {results['capacity_ah']:.3f} Ah",
            "",
            header + ":",
            f"  R0 (ohmic):          {results['r0_mohm']:.2f} mΩ",
            f"  R1 (charge-transfer):{results['r1_mohm']:.2f} mΩ",
            f"  C1:                  {results['c1_farad']:.0f} F",
            f"  tau (R1·C1):         {results['tau_s']:.1f} s",
            f"  Total DCIR (R0+R1):  {results['ri_mohm']:.2f} mΩ",
        ]))
        iv, ic = results["ica"]
        if len(iv):
            self.plot_ica.clear(); self.plot_ica.plot(iv, ic, pen=pg.mkPen("#1f4e79", width=2))
        dv, dt = results["dtv"]
        if len(dv):
            self.plot_dtv.clear(); self.plot_dtv.plot(dv, dt, pen=pg.mkPen("#7a2020", width=2))
        self._log_alarm(
            f"Test complete — Grade {grade}, SoH {results['soh']:.1f}%, "
            f"R0 {results['r0_mohm']:.1f} mΩ, R1 {results['r1_mohm']:.1f} mΩ")

    def _cleanup_test_thread(self):
        if self._test_thread:
            self._test_thread.deleteLater()
        self._test_thread = None
        self._test_worker = None
        self.btn_run_test.setEnabled(True)
        self.lbl_test_status.setText("Test idle")

    def _on_estop(self):
        if self._test_worker:
            self._test_worker.emergency_stop()   # immediate instrument override
        if self.controller:
            self.controller._trigger_safety("E-STOP pressed by operator")
        self._log_alarm("⛔ E-STOP issued.")

    def _populate_profiles(self):
        self.lst_profiles.clear()
        self._profile_map.clear()
        for tid in self.iec_standard.get_available_tests():
            prof = self.iec_standard.get_test_profile(tid)
            if not prof:
                continue
            disp = f"[IEC] {prof.name}"
            self.lst_profiles.addItem(disp)
            self._profile_map[disp] = ("iec", tid)

    def _on_run_profile(self):
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Profile", "Connect hardware first")
            return
        item = self.lst_profiles.currentItem()
        if item is None:
            if not self._headless:
                QMessageBox.warning(self, "Profile", "Select a profile first")
            return
        ptype, pid = self._profile_map.get(item.text(), (None, None))
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

    def _on_toggle_logging(self):
        if self.data is None:
            return
        if self.data.is_recording:
            self.data.stop_logging()
            self.btn_log.setText("START DATA LOGGING")
        else:
            ok, msg = self.data.start_logging(self.config.system.csv_filepath)
            if ok:
                self.btn_log.setText("STOP DATA LOGGING")
                self._last_csv = self.config.system.csv_filepath   # monitor CSV becomes latest
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
