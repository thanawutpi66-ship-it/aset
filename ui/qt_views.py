"""
PySide6 GUI — Universal Battery Tester (5-panel)

แทน Tkinter UI โดย "ใช้ logic/HAL/estimator/controller เดิมทั้งหมด" — เปลี่ยนแค่ชั้น
presentation. controller คุยกับ UI ผ่าน method ชุดเดียวกับ Tk UI
(update_display / set_profile_status / set_button_enabled / set_charge_status /
set_loading_state / handle_*). ทุก method ที่อาจถูกเรียกจาก worker thread จะ "emit signal"
→ slot อัปเดต widget บน GUI thread (Qt thread-safety) แทน root.after ของ Tk

Panels:
  1. Test Config   — เลือกแบตจาก profile DB, เชื่อมต่อ, manual, charge, profile
  2. Real-time     — การ์ด V/I/SoC/Rin/Temp/SoH + temp gauge สี + PyQtGraph live plots
  3. Analytics     — SoH/capacity/grade + diagnostic
  4. Safety        — E-STOP ปุ่มใหญ่ + Alarm log console
  5. Data          — CSV status + PDF report
"""
import os
import logging
import threading
import webbrowser
from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton, QComboBox, QLineEdit, QListWidget,
    QGroupBox, QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QTextEdit, QFrame, QSplitter, QMessageBox, QFileDialog, QSizePolicy,
)
import pyqtgraph as pg

import battery_profiles
from analysis_module import ChemistryDetector
from iec61960_standard import IEC61960Standard

logger = logging.getLogger(__name__)


# ===========================================================================
# Qt "root" shim — ให้ controller/event-system เรียก root.after(ms, fn, *args)
# ได้เหมือน Tk โดย marshaling เข้า GUI thread ผ่าน queued signal
# ===========================================================================
class QtRootShim(QObject):
    _invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self._close_handler = None
        self._invoke.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception as e:
            logger.error(f"QtRootShim invoke error: {e}")

    def after(self, ms, fn=None, *args):
        """เลียนแบบ tk root.after(ms, func, *args)"""
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
        from PySide6.QtWidgets import QApplication
        QApplication.quit()


# ===========================================================================
# Theme
# ===========================================================================
PRIMARY = "#005a9e"
SUCCESS = "#107c10"
DANGER = "#c50f1f"
WARNING = "#d83b01"
BG = "#f3f4f6"
CARD = "#ffffff"
TEXT = "#1a1a1a"
MUTED = "#6b7280"

_BTN_CSS = """
QPushButton {{ background:{bg}; color:{fg}; border:none; border-radius:4px;
    padding:7px 10px; font-weight:600; }}
QPushButton:hover {{ background:{hover}; }}
QPushButton:disabled {{ background:#cbd5e1; color:#6b7280; }}
"""


def _btn(text, bg=PRIMARY, fg="white", hover="#004578"):
    b = QPushButton(text)
    b.setStyleSheet(_BTN_CSS.format(bg=bg, fg=fg, hover=hover))
    b.setCursor(Qt.PointingHandCursor)
    return b


# ===========================================================================
# Main window
# ===========================================================================
class BatteryQtWindow(QMainWindow):
    # signals สำหรับ thread-safe UI update
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

        self.iec_standard = IEC61960Standard(
            self.config.battery.rated_capacity,
            self.config.battery.battery_type,
            self.config.battery.pack_nominal_voltage,
        )
        self._profile_map = {}
        self._buttons = {}

        max_pts = config_manager.system.max_points
        self.buf_t = deque(maxlen=max_pts)
        self.buf_v = deque(maxlen=max_pts)
        self.buf_i = deque(maxlen=max_pts)
        self.buf_soc = deque(maxlen=max_pts)
        self.buf_rin = deque(maxlen=max_pts)
        self.buf_temp = deque(maxlen=max_pts)
        self._t_count = 0

        self._build_ui()
        self._connect_signals()

        # status tick (GUI-thread QTimer — ไม่ผ่าน root.after)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_connection_status)
        self._tick.start(1000)

        logger.info("BatteryQtWindow initialized")

    # -------------------------------------------------------------------
    # controller binding (เรียกหลัง core components ถูกสร้าง)
    # -------------------------------------------------------------------
    def bind_controller(self, controller):
        self.controller = controller
        self.hw = controller.hw
        self.data = controller.data
        self.estimator = controller.estimator
        self._refresh_ports()
        self._update_connection_status()
        self._refresh_battery_readout()

    # -------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("ASET Universal Battery Tester — PySide6")
        self.resize(1600, 950)
        self.setStyleSheet(f"QMainWindow {{ background:{BG}; }} QWidget {{ color:{TEXT}; }}"
                           f"QGroupBox {{ font-weight:600; border:1px solid #d1d5db;"
                           f" border-radius:6px; margin-top:8px; background:{CARD}; }}"
                           f"QGroupBox::title {{ subcontrol-origin:margin; left:10px;"
                           f" padding:0 4px; color:{PRIMARY}; }}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        root.addWidget(self._build_header())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 1220])
        root.addWidget(splitter, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"color:{MUTED}; padding:4px;")
        root.addWidget(self.status_label)

    def _build_header(self):
        bar = QFrame()
        bar.setStyleSheet(f"background:{CARD}; border-radius:6px;")
        lay = QHBoxLayout(bar)
        title = QLabel("ASET LABORATORY")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        sub = QLabel("Universal Battery Tester · IEC 61960")
        sub.setStyleSheet(f"color:{MUTED};")
        box = QVBoxLayout()
        box.setSpacing(0)
        box.addWidget(title)
        box.addWidget(sub)
        lay.addLayout(box)
        lay.addStretch(1)
        mode = "SIMULATION" if self.config.system.simulation_mode else "HARDWARE"
        badge = QLabel(f"  {mode}  ")
        badge.setStyleSheet(f"background:{PRIMARY}; color:white; border-radius:4px;"
                            f" padding:6px; font-weight:600;")
        lay.addWidget(badge)
        return bar

    def _build_left_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._grp_test_config())
        lay.addWidget(self._grp_charge_profile())
        lay.addWidget(self._grp_safety())
        lay.addWidget(self._grp_data())
        lay.addStretch(1)
        return panel

    # ---- Panel 1: Test Config -----------------------------------------
    def _grp_test_config(self):
        g = QGroupBox("1 · Test Configuration")
        form = QVBoxLayout(g)

        # Battery profile dropdown (จาก profile DB)
        row = QHBoxLayout()
        row.addWidget(QLabel("Battery:"))
        self.cb_product = QComboBox()
        self.cb_product.addItems(battery_profiles.list_products())
        self.cb_product.currentTextChanged.connect(self._on_product_changed)
        row.addWidget(self.cb_product, 1)
        form.addLayout(row)

        self.lbl_battery_readout = QLabel("—")
        self.lbl_battery_readout.setStyleSheet(f"color:{MUTED};")
        form.addWidget(self.lbl_battery_readout)

        drow = QHBoxLayout()
        self.btn_detect = _btn("Detect Chemistry (AI)", bg="#374151", hover="#1f2937")
        self.btn_detect.clicked.connect(self._on_detect_chemistry)
        self.btn_save_default = _btn("Save as Default", bg="#6b7280", hover="#4b5563")
        self.btn_save_default.clicked.connect(self._on_save_default)
        drow.addWidget(self.btn_detect, 2)
        drow.addWidget(self.btn_save_default, 1)
        form.addLayout(drow)

        # Connections
        form.addWidget(self._hline())
        self.cb_psu = QComboBox()
        self.cb_load = QComboBox()
        self.cb_esp = QComboBox()
        cf = QFormLayout()
        cf.addRow("PSU (VISA):", self.cb_psu)
        cf.addRow("Load (VISA):", self.cb_load)
        cf.addRow("ESP32 (COM):", self.cb_esp)
        form.addLayout(cf)
        crow = QHBoxLayout()
        self.btn_connect = _btn("Connect", bg=SUCCESS, hover="#0b5a0b")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = _btn("Disconnect", bg=DANGER, hover="#990b16")
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        crow.addWidget(self.btn_connect)
        crow.addWidget(self.btn_disconnect)
        form.addLayout(crow)
        btn_refresh = _btn("Refresh Ports", bg="#6b7280", hover="#4b5563")
        btn_refresh.clicked.connect(self._refresh_ports)
        form.addWidget(btn_refresh)

        # Manual control
        form.addWidget(self._hline())
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("PSU V:"))
        self.ed_psu_v = QLineEdit("13.8")
        self.ed_psu_v.setMaximumWidth(70)
        mrow.addWidget(self.ed_psu_v)
        b_on = _btn("ON", bg=SUCCESS, hover="#0b5a0b")
        b_on.clicked.connect(lambda: self._psu_manual(True))
        b_off = _btn("OFF", bg="#6b7280", hover="#4b5563")
        b_off.clicked.connect(lambda: self._psu_manual(False))
        mrow.addWidget(b_on)
        mrow.addWidget(b_off)
        form.addLayout(mrow)

        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Load A:"))
        self.ed_load_a = QLineEdit("0.7")
        self.ed_load_a.setMaximumWidth(70)
        lrow.addWidget(self.ed_load_a)
        bl_on = _btn("ON", bg=SUCCESS, hover="#0b5a0b")
        bl_on.clicked.connect(lambda: self._load_manual(True))
        bl_off = _btn("OFF", bg="#6b7280", hover="#4b5563")
        bl_off.clicked.connect(lambda: self._load_manual(False))
        lrow.addWidget(bl_on)
        lrow.addWidget(bl_off)
        form.addLayout(lrow)
        return g

    # ---- Panel 1b: Charge + Profile + Monitor -------------------------
    def _grp_charge_profile(self):
        g = QGroupBox("Charge · Profile · Monitor")
        lay = QVBoxLayout(g)

        # Chemistry-aware charge
        crow = QHBoxLayout()
        self.btn_charge = _btn("⚡ CHARGE", bg=SUCCESS, hover="#0b5a0b")
        self.btn_charge.clicked.connect(self._on_charge)
        self.btn_stop_charge = _btn("Stop", bg=DANGER, hover="#990b16")
        self.btn_stop_charge.clicked.connect(self._on_stop_charge)
        crow.addWidget(self.btn_charge, 2)
        crow.addWidget(self.btn_stop_charge, 1)
        lay.addLayout(crow)
        self.lbl_charge = QLabel("Charge idle")
        self.lbl_charge.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_charge)

        lay.addWidget(self._hline())
        self.lst_profiles = QListWidget()
        self.lst_profiles.setMaximumHeight(120)
        self._populate_profiles()
        lay.addWidget(self.lst_profiles)
        prow = QHBoxLayout()
        self._buttons["btn_start_profile"] = self.btn_start_profile = _btn("RUN", bg=PRIMARY)
        self.btn_start_profile.clicked.connect(self._on_run_profile)
        btn_stop_profile = _btn("STOP", bg=DANGER, hover="#990b16")
        btn_stop_profile.clicked.connect(lambda: self.controller and self.controller.stop_profile())
        prow.addWidget(self.btn_start_profile)
        prow.addWidget(btn_stop_profile)
        lay.addLayout(prow)
        self.lbl_profile_status = QLabel("No profile selected")
        self.lbl_profile_status.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_profile_status)

        lay.addWidget(self._hline())
        mrow = QHBoxLayout()
        self._buttons["btn_start_monitor"] = self.btn_start_monitor = _btn("START MONITOR", bg=SUCCESS, hover="#0b5a0b")
        self.btn_start_monitor.clicked.connect(self._on_start_monitor)
        btn_stop_monitor = _btn("STOP", bg=DANGER, hover="#990b16")
        btn_stop_monitor.clicked.connect(lambda: self.controller and self.controller.stop_monitor())
        mrow.addWidget(self.btn_start_monitor)
        mrow.addWidget(btn_stop_monitor)
        lay.addLayout(mrow)
        return g

    # ---- Panel 4: Safety ----------------------------------------------
    def _grp_safety(self):
        g = QGroupBox("4 · Safety")
        lay = QVBoxLayout(g)
        self.btn_estop = QPushButton("⛔  EMERGENCY STOP")
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{DANGER}; color:white; border:none;"
            f" border-radius:8px; padding:18px; font-size:16px; font-weight:800; }}"
            f"QPushButton:hover {{ background:#990b16; }}")
        self.btn_estop.setCursor(Qt.PointingHandCursor)
        self.btn_estop.clicked.connect(self._on_estop)
        lay.addWidget(self.btn_estop)
        return g

    # ---- Panel 5: Data ------------------------------------------------
    def _grp_data(self):
        g = QGroupBox("5 · Data")
        lay = QVBoxLayout(g)
        self.lbl_csv = QLabel(f"CSV: {self.config.system.csv_filepath}")
        self.lbl_csv.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_csv)
        self.btn_log = _btn("START DATA LOGGING", bg="#6b7280", hover="#4b5563")
        self.btn_log.clicked.connect(self._on_toggle_logging)
        lay.addWidget(self.btn_log)
        self.btn_pdf = _btn("📄 Generate PDF Report", bg=PRIMARY)
        self.btn_pdf.clicked.connect(self._on_pdf_report)
        lay.addWidget(self.btn_pdf)
        btn_dash = _btn("Open Web Dashboard", bg="#374151", hover="#1f2937")
        btn_dash.clicked.connect(self._on_open_dashboard)
        lay.addWidget(btn_dash)
        return g

    # ---- Right: realtime + tabs ---------------------------------------
    def _build_right_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        # metric cards
        cards = QFrame()
        cards.setStyleSheet(f"background:{CARD}; border-radius:6px;")
        grid = QGridLayout(cards)
        self.metric_labels = {}
        metrics = [("Voltage", "V"), ("Current", "A"), ("SoC", "%"),
                   ("Rin", "mΩ"), ("Temp", "°C"), ("SoH", "%")]
        for col, (name, unit) in enumerate(metrics):
            t = QLabel(name)
            t.setStyleSheet(f"color:{MUTED}; font-size:11px; font-weight:600;")
            v = QLabel(f"0.0 {unit}")
            v.setFont(QFont("Consolas", 18, QFont.Bold))
            grid.addWidget(t, 0, col)
            grid.addWidget(v, 1, col)
            self.metric_labels[name] = (v, unit)
        lay.addWidget(cards)

        tabs = QTabWidget()
        tabs.addTab(self._tab_plots(), "2 · Live Plots")
        tabs.addTab(self._tab_analytics(), "3 · Analytics")
        tabs.addTab(self._tab_alarms(), "Alarm Log")
        lay.addWidget(tabs, 1)
        return panel

    def _tab_plots(self):
        pg.setConfigOptions(antialias=True, background=CARD, foreground=TEXT)
        w = QWidget()
        lay = QGridLayout(w)
        self._curves = {}
        specs = [("Voltage (V)", "v", PRIMARY), ("Current (A)", "i", WARNING),
                 ("SoC (%)", "soc", SUCCESS), ("Rin (mΩ)", "rin", "#6b7280"),
                 ("Temperature (°C)", "temp", "#8b0000")]
        for idx, (title, key, color) in enumerate(specs):
            p = pg.PlotWidget(title=title)
            p.showGrid(x=True, y=True, alpha=0.3)
            curve = p.plot(pen=pg.mkPen(color, width=2))
            self._curves[key] = curve
            lay.addWidget(p, idx // 2, idx % 2)

        # temp gauge
        self.gauge = QLabel("-- °C")
        self.gauge.setAlignment(Qt.AlignCenter)
        self.gauge.setFont(QFont("Consolas", 22, QFont.Bold))
        self.gauge.setStyleSheet("background:#16a34a; color:white; border-radius:8px; padding:14px;")
        lay.addWidget(self.gauge, 2, 1)
        return w

    def _tab_analytics(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_analytics = QLabel("ยังไม่มีผลวิเคราะห์ — รัน test หรือ Analyze CSV")
        self.lbl_analytics.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_analytics)
        self.txt_analytics = QTextEdit()
        self.txt_analytics.setReadOnly(True)
        self.txt_analytics.setFont(QFont("Consolas", 10))
        lay.addWidget(self.txt_analytics, 1)
        btn = _btn("Analyze Last CSV (AI Grade)", bg=PRIMARY)
        btn.clicked.connect(self._on_analyze_csv)
        lay.addWidget(btn)
        return w

    def _tab_alarms(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_alarms = QTextEdit()
        self.txt_alarms.setReadOnly(True)
        self.txt_alarms.setFont(QFont("Consolas", 10))
        self.txt_alarms.setStyleSheet("background:#0f172a; color:#e5e7eb;")
        lay.addWidget(self.txt_alarms)
        self._log_alarm("System ready.")
        return w

    def _hline(self):
        ln = QFrame()
        ln.setFrameShape(QFrame.HLine)
        ln.setStyleSheet("color:#e5e7eb;")
        return ln

    # -------------------------------------------------------------------
    # signal/slot plumbing — UI methods (worker-thread-safe) emit signals
    # -------------------------------------------------------------------
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

    # ---- methods called by controller / event handler -----------------
    def update_display(self, v, i, soc, rin, temp=None, soh=None):
        if temp is None:
            temp = getattr(self.hw, "current_temp", 25.0)
        if soh is None:
            soh = getattr(self.estimator, "soh", 100.0)
        self.sig_display.emit(float(v), float(i), float(soc), float(rin),
                              float(temp), float(soh))

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

    def handle_safety_trigger(self, reason):
        self.sig_safety.emit(str(reason))

    def handle_profile_completed(self, data):
        self.sig_profile_done.emit(data)

    def handle_analysis_completed(self, result):
        self.sig_analysis_done.emit(result)

    # ---- slots (GUI thread) -------------------------------------------
    @Slot(float, float, float, float, float, float)
    def _slot_display(self, v, i, soc, rin, temp, soh):
        rin_mohm = rin * 1000
        vals = {"Voltage": v, "Current": i, "SoC": soc, "Rin": rin_mohm,
                "Temp": temp, "SoH": soh}
        for name, (lbl, unit) in self.metric_labels.items():
            lbl.setText(f"{vals[name]:.2f} {unit}")
        self.buf_t.append(self._t_count)
        self.buf_v.append(v)
        self.buf_i.append(i)
        self.buf_soc.append(soc)
        self.buf_rin.append(rin_mohm)
        self.buf_temp.append(temp)
        self._t_count += 1
        t = list(self.buf_t)
        self._curves["v"].setData(t, list(self.buf_v))
        self._curves["i"].setData(t, list(self.buf_i))
        self._curves["soc"].setData(t, list(self.buf_soc))
        self._curves["rin"].setData(t, list(self.buf_rin))
        self._curves["temp"].setData(t, list(self.buf_temp))
        self._update_gauge(temp)

    def _update_gauge(self, temp):
        if temp < 35:
            color = "#16a34a"
        elif temp < 45:
            color = "#d97706"
        else:
            color = "#dc2626"
        self.gauge.setText(f"{temp:.1f} °C")
        self.gauge.setStyleSheet(f"background:{color}; color:white;"
                                 f" border-radius:8px; padding:14px;")

    @Slot(str, str)
    def _slot_profile_status(self, text, color):
        self.lbl_profile_status.setText(text)
        self.lbl_profile_status.setStyleSheet(f"color:{color};")

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
        connected = getattr(self.hw, "is_connected", False)
        self.status_label.setText("Hardware connected" if connected
                                  else "Ready — connect hardware to begin")

    @Slot(str)
    def _slot_safety(self, reason):
        self._log_alarm(f"⛔ SAFETY: {reason}")
        QMessageBox.critical(self, "Safety Triggered",
                             f"System safety triggered:\n{reason}\n\nAll operations stopped.")

    @Slot(object)
    def _slot_profile_done(self, data):
        success = data if isinstance(data, bool) else data.get("success", False)
        if success and isinstance(data, dict) and data.get("report"):
            self._show_text_dialog("IEC 61960 Test Report", data["report"])
        elif success:
            QMessageBox.information(self, "Profile Completed", "Test completed successfully.")
        else:
            err = data.get("error", "") if isinstance(data, dict) else ""
            QMessageBox.warning(self, "Profile Stopped", err or "Test stopped.")

    @Slot(object)
    def _slot_analysis_done(self, result):
        if not getattr(result, "success", False):
            self.lbl_analytics.setText("Analysis failed")
            self._log_alarm(f"Analysis failed: {getattr(result, 'error', '?')}")
            return
        f = result.features
        self.lbl_analytics.setText(
            f"Grade {result.grade}  (confidence {result.confidence*100:.0f}%, {result.method})")
        lines = [
            f"SoH:            {f.soh_pct:.1f} %",
            f"Capacity:       {f.capacity_ah:.3f} Ah",
            f"Energy:         {f.energy_wh:.2f} Wh",
            f"R0 (ohmic):     {f.r0_mohm:.2f} mΩ",
            f"Rp (polar.):    {f.rp_mohm:.2f} mΩ",
            f"tau (RC):       {f.tau_s:.2f} s",
            f"Pulses fitted:  {f.num_pulses}",
            f"Avg temp:       {f.avg_temp_c:.1f} °C",
        ]
        if result.notes:
            lines += ["", "Notes:"] + [f"  - {n}" for n in result.notes]
        self.txt_analytics.setPlainText("\n".join(lines))
        self._last_analysis = result

    # -------------------------------------------------------------------
    # actions
    # -------------------------------------------------------------------
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
            if len(visa) > 1:
                self.cb_load.setCurrentIndex(1)
        except Exception as e:
            logger.error(f"refresh ports: {e}")

    def _refresh_battery_readout(self):
        b = self.config.battery
        self.lbl_battery_readout.setText(
            f"{b.battery_type} · {b.cells_series}S{b.cells_parallel}P · "
            f"{b.pack_nominal_voltage:.1f}V · {b.rated_capacity:.1f}Ah")

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
        # ตั้งหน้าต่างแรงดันให้สอดคล้องเคมีใหม่ (กัน pack_max/min_voltage ค้างค่ารุ่นเดิม)
        if prod.max_voltage_per_cell:
            b.max_voltage = prod.max_voltage_per_cell
        if prod.min_voltage_per_cell:
            b.min_voltage = prod.min_voltage_per_cell
        # อัปเดต safety window ระดับแพ็คให้ตรงรุ่น
        if prod.safety_ovp_pack and self.config.system.safety_limits:
            self.config.system.safety_limits["max_voltage"] = prod.safety_ovp_pack
        if prod.safety_uvp_pack and self.config.system.safety_limits:
            self.config.system.safety_limits["min_voltage"] = prod.safety_uvp_pack
        # rebuild model + re-point estimator (ถ้า controller bound แล้ว)
        try:
            from battery_model import BatteryModel
            model = BatteryModel(b.battery_type, b.nominal_voltage,
                                 b.cells_series, b.cells_parallel)
            if self.estimator is not None:
                self.estimator.battery_model = model
                if hasattr(self.estimator, "rated_capacity"):
                    self.estimator.rated_capacity = b.rated_capacity
            self.iec_standard = IEC61960Standard(
                b.rated_capacity, b.battery_type, b.pack_nominal_voltage)
            self._populate_profiles()
        except Exception as e:
            logger.error(f"apply product: {e}")
        # apply เฉพาะ session — ไม่เขียนทับ config.json อัตโนมัติ (กันเลือกดูแล้ว
        # default ถาวรเปลี่ยนโดยไม่ตั้งใจ); persist ผ่านปุ่ม Save as Default เท่านั้น
        self._refresh_battery_readout()
        self._log_alarm(f"เลือกแบต (session): {name} → {prod.chemistry} {prod.cells_series}S")

    def _on_save_default(self):
        """persist battery/safety config ปัจจุบันลง config.json (ผู้ใช้สั่งเอง)"""
        if self.config.save_config():
            self._log_alarm("บันทึกเป็นค่าเริ่มต้นแล้ว (config.json)")
            QMessageBox.information(self, "Save as Default", "บันทึก config.json แล้ว")
        else:
            QMessageBox.critical(self, "Save as Default", "บันทึกไม่สำเร็จ")

    def _on_detect_chemistry(self):
        if self.estimator is None:
            return
        try:
            model = self.estimator.battery_model
            v, s = ChemistryDetector.features_from_model(model)
            res = ChemistryDetector().detect(v, s)
            self._log_alarm(f"Chemistry detect → {res.chemistry} "
                            f"(confidence {res.confidence*100:.0f}%)")
            QMessageBox.information(self, "Chemistry Detection",
                                    f"Detected: {res.chemistry}\n"
                                    f"Confidence: {res.confidence*100:.0f}%")
        except Exception as e:
            QMessageBox.warning(self, "Chemistry Detection", str(e))

    def _on_connect(self):
        psu, load, esp = self.cb_psu.currentText(), self.cb_load.currentText(), self.cb_esp.currentText()
        if not psu or not load:
            QMessageBox.warning(self, "Connect", "เลือก PSU และ Load port ก่อน")
            return
        try:
            self.hw.connect_instruments(psu, load)
            if esp:
                self.hw.connect_esp32(esp)
            self.config.hardware.psu_port = psu
            self.config.hardware.load_port = load
            self.config.hardware.esp_port = esp
            self.config.save_config()
            self._update_connection_status()
            self._log_alarm("Hardware connected.")
        except Exception as e:
            QMessageBox.critical(self, "Connect Error", str(e))

    def _on_disconnect(self):
        try:
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            self._update_connection_status()
            self._log_alarm("Hardware disconnected.")
        except Exception as e:
            QMessageBox.critical(self, "Disconnect Error", str(e))

    def _psu_manual(self, on):
        try:
            self.hw.set_psu(on, str(float(self.ed_psu_v.text())) if on else "0")
        except ValueError:
            QMessageBox.warning(self, "PSU", "ค่าแรงดันไม่ถูกต้อง")

    def _load_manual(self, on):
        try:
            self.hw.set_load(on, str(float(self.ed_load_a.text())) if on else "0")
        except ValueError:
            QMessageBox.warning(self, "Load", "ค่ากระแสไม่ถูกต้อง")

    def _on_charge(self):
        if self.controller is None:
            return
        if not getattr(self.hw, "is_connected", False):
            QMessageBox.warning(self, "Charge", "เชื่อมต่อ hardware ก่อน")
            return
        ok = self.controller.start_charge()
        self._log_alarm("เริ่มชาร์จ" if ok else "เริ่มชาร์จไม่สำเร็จ (ดู log)")

    def _on_stop_charge(self):
        if self.controller:
            self.controller.stop_charge()
            self._log_alarm("หยุดชาร์จ")

    def _on_estop(self):
        if self.controller:
            self.controller._trigger_safety("E-STOP pressed by operator")
        self._log_alarm("⛔ E-STOP — ตัดไฟ PSU + Load")

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
            QMessageBox.warning(self, "Profile", "เชื่อมต่อ hardware ก่อน")
            return
        item = self.lst_profiles.currentItem()
        if item is None:
            QMessageBox.warning(self, "Profile", "เลือก profile ก่อน")
            return
        ptype, pid = self._profile_map.get(item.text(), (None, None))
        try:
            if ptype == "iec":
                self.controller.start_iec61960_test(pid, self.iec_standard)
        except Exception as e:
            QMessageBox.critical(self, "Profile Error", str(e))

    def _on_start_monitor(self):
        if not getattr(self.hw, "is_connected", False):
            QMessageBox.warning(self, "Monitor", "เชื่อมต่อ hardware ก่อน")
            return
        self.controller.start_monitor()
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
            else:
                QMessageBox.critical(self, "Logging", msg)

    def _on_pdf_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF Report",
                                              "battery_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            from report_generator import generate_pdf_report
            result = getattr(self, "_last_analysis", None)
            generate_pdf_report(path, self.config, self.estimator,
                                analysis=result,
                                csv_path=self.config.system.csv_filepath)
            self._log_alarm(f"สร้าง PDF: {path}")
            QMessageBox.information(self, "PDF Report", f"บันทึกแล้ว:\n{path}")
        except Exception as e:
            logger.error(f"pdf report: {e}")
            QMessageBox.critical(self, "PDF Report", str(e))

    def _on_open_dashboard(self):
        port = getattr(self.config.system, "web_server_port", 8000)
        webbrowser.open(f"http://127.0.0.1:{port}/")

    def _on_analyze_csv(self):
        analyzer = getattr(self.controller, "analyzer", None)
        if analyzer is None:
            QMessageBox.warning(self, "AI Analysis", "Analysis subsystem ไม่พร้อม")
            return
        csv_path = self.config.system.csv_filepath
        if not os.path.exists(csv_path):
            QMessageBox.warning(self, "AI Analysis", f"ไม่พบ CSV:\n{csv_path}")
            return
        self.lbl_analytics.setText("กำลังวิเคราะห์ CSV...")
        threading.Thread(target=analyzer.analyze, args=(csv_path,), daemon=True).start()

    def _show_text_dialog(self, title, text):
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text[:4000])
        dlg.exec()

    # -------------------------------------------------------------------
    def closeEvent(self, event):
        reply = QMessageBox.question(self, "Quit", "ปิดโปรแกรมและตัดไฟทดสอบ?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                if self.controller:
                    self.controller.shutdown()
            except Exception as e:
                logger.error(f"shutdown on close: {e}")
            event.accept()
        else:
            event.ignore()
