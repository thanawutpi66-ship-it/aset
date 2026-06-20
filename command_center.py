"""
Automated Battery Performance Testing & Sorting — Command Center (standalone bench)
==================================================================================
ISA-101 High-Performance HMI (PySide6): desaturated gray shell; bright color only
for alarms, status pills, the temperature gauge, and the sorting grade.

This is the thin UI; the acquisition engine (QThread worker, instrument backends,
analytics) lives in the reusable ``aset_batt.acquisition`` package and is shared
with the integrated application. The bench defaults to the simulated backend; pass
a ``HardwareBackend`` (which wraps the project HAL) to drive real instruments.

Run:  python command_center.py
"""
from __future__ import annotations

import os
import sys
import math
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, QThreadPool
from PySide6.QtGui import QFont, QDoubleValidator, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QTabWidget, QTextEdit, QFrame, QMessageBox, QFileDialog,
)
import pyqtgraph as pg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aset_batt.acquisition.models import (
    OperationMode, BatteryProfile, TestConfig, load_profiles,
)
from aset_batt.acquisition.backends import SimulatedBackend
from aset_batt.acquisition.worker import AcquisitionWorker, ReportTask

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("command_center")

# ===========================================================================
# ISA-101 High-Performance HMI palette — gray shell; color only for status/alarm.
# ===========================================================================
BG      = "#b9bdc1"
PANEL   = "#c9cdd1"
PANEL2  = "#d7dadd"
FIELD   = "#eceef0"
BORDER  = "#8c9296"
TEXT    = "#1d2123"
MUTED   = "#54595d"
OK      = "#2e7d32"
WARN    = "#c98a00"
CRIT    = "#c62828"
INFO    = "#1565c0"
NEUTRAL = "#6b7075"


# ===========================================================================
# Reusable HMI widgets
# ===========================================================================
class DigitalReadout(QFrame):
    """ISA-101 process readout: gray card, dark value; turns colored on alarm."""
    def __init__(self, label: str, unit: str):
        super().__init__()
        self.unit = unit
        self.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}")
        lay = QVBoxLayout(self); lay.setContentsMargins(10, 6, 10, 8); lay.setSpacing(1)
        cap = QLabel(label.upper())
        cap.setStyleSheet(f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;")
        self.value = QLabel(f"-- {unit}")
        self.value.setFont(QFont("Consolas", 20, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap); lay.addWidget(self.value)

    def set_value(self, v: float, fmt: str = "{:.3f}", alarm: bool = False):
        self.value.setText(f"{fmt.format(v)} {self.unit}")
        self.value.setStyleSheet(f"color:{CRIT if alarm else TEXT}; border:0;")


class TemperatureGauge(QFrame):
    """Surface-temperature gauge, color-coded Safe / Warning / Critical."""
    def __init__(self):
        super().__init__()
        self.setStyleSheet(f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}")
        lay = QVBoxLayout(self); lay.setContentsMargins(10, 6, 10, 10)
        cap = QLabel("SURFACE TEMP (MLX90614)")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;")
        self.value = QLabel("-- °C")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setFont(QFont("Consolas", 30, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap); lay.addWidget(self.value)

    def update_temp(self, t: float, warn: float, crit: float):
        if math.isnan(t):
            self.value.setText("-- °C"); return
        color = CRIT if t >= crit else WARN if t >= warn else OK
        self.value.setText(f"{t:.1f} °C")
        self.value.setStyleSheet(f"color:{color}; border:0;")


class MultiAxisTrend(pg.GraphicsLayoutWidget):
    """V (left) + I (right) + T (far right) vs elapsed time on a shared X axis."""
    def __init__(self):
        super().__init__()
        self.setBackground(PANEL2)
        self.p = self.addPlot()
        self.p.setLabel("bottom", "Elapsed", units="s")
        self.p.setLabel("left", "Voltage", units="V", color="#1f4e79")
        self.p.showGrid(x=True, y=True, alpha=0.2)
        self.p.getAxis("left").setPen("#1f4e79")

        self.vb_i = pg.ViewBox(); self.vb_t = pg.ViewBox()
        self.p.showAxis("right")
        self.p.scene().addItem(self.vb_i)
        self.p.getAxis("right").linkToView(self.vb_i)
        self.p.getAxis("right").setLabel("Current", units="A", color="#8a5a00")
        self.p.getAxis("right").setPen("#8a5a00")
        self.vb_i.setXLink(self.p)

        self.ax_t = pg.AxisItem("right")
        self.p.layout.addItem(self.ax_t, 2, 3)
        self.p.scene().addItem(self.vb_t)
        self.ax_t.linkToView(self.vb_t)
        self.ax_t.setLabel("Temp", units="°C", color="#7a2020")
        self.ax_t.setPen("#7a2020")
        self.vb_t.setXLink(self.p)

        self.c_v = self.p.plot(pen=pg.mkPen("#1f4e79", width=2))
        self.c_i = pg.PlotCurveItem(pen=pg.mkPen("#8a5a00", width=2)); self.vb_i.addItem(self.c_i)
        self.c_t = pg.PlotCurveItem(pen=pg.mkPen("#7a2020", width=2, style=Qt.PenStyle.DashLine)); self.vb_t.addItem(self.c_t)

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


# ===========================================================================
# Main window — 5 vertical blocks
# ===========================================================================
class CommandCenter(QMainWindow):
    def __init__(self):
        super().__init__()
        self.profiles = load_profiles()
        self.thread: Optional[QThread] = None
        self.worker: Optional[AcquisitionWorker] = None
        self.pool = QThreadPool.globalInstance()
        self._results = None
        self._csv_path = None
        self.t_buf, self.v_buf, self.i_buf, self.temp_buf = [], [], [], []

        self.setWindowTitle("Battery Performance Testing & Sorting — Command Center")
        self.resize(1500, 1000)
        self._apply_isa101()
        self._build()

        self._plot_timer = QTimer(self)   # decouple plot refresh from sample rate
        self._plot_timer.timeout.connect(self._refresh_plot)
        self._plot_timer.start(100)

    # ---- styling ----------------------------------------------------------
    def _apply_isa101(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT};
                font-family:'Segoe UI',sans-serif; font-size:12px; }}
            QGroupBox {{ border:1px solid {BORDER}; border-radius:4px; margin-top:12px;
                background:{PANEL}; font-weight:700; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:1px 6px;
                color:{TEXT}; background:{PANEL}; letter-spacing:1px; }}
            QLabel {{ background:transparent; }}
            QComboBox, QLineEdit {{ background:{FIELD}; border:1px solid {BORDER};
                border-radius:3px; padding:4px 6px; color:{TEXT}; }}
            QComboBox:focus, QLineEdit:focus {{ border:1px solid {INFO}; }}
            QComboBox QAbstractItemView {{ background:{FIELD}; color:{TEXT};
                selection-background-color:{INFO}; selection-color:white; }}
            QPushButton {{ background:#c2c6ca; border:1px solid {BORDER}; border-radius:3px;
                padding:7px 12px; color:{TEXT}; font-weight:600; }}
            QPushButton:hover {{ background:#b4b9bd; }}
            QPushButton:disabled {{ background:#cfd2d5; color:#8a8f93; }}
            QTabWidget::pane {{ border:1px solid {BORDER}; background:{PANEL2}; }}
            QTabBar::tab {{ background:{PANEL}; padding:6px 14px; border:1px solid {BORDER};
                border-bottom:0; color:{MUTED}; }}
            QTabBar::tab:selected {{ background:{PANEL2}; color:{TEXT}; font-weight:700; }}
            QTextEdit {{ background:#e9ebed; border:1px solid {BORDER}; color:{TEXT}; }}
        """)

    def _build(self):
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(8)
        root.addWidget(self._block_header())

        mid = QHBoxLayout(); mid.setSpacing(8)
        left = QVBoxLayout(); left.setSpacing(8)
        left.addWidget(self._block1_config())
        left.addWidget(self._block4_safety(), 1)
        left.addWidget(self._block5_data())
        lw = QWidget(); lw.setLayout(left); lw.setFixedWidth(380)
        mid.addWidget(lw)

        right = QVBoxLayout(); right.setSpacing(8)
        right.addWidget(self._block2_monitor(), 3)
        right.addWidget(self._block3_analytics(), 2)
        rw = QWidget(); rw.setLayout(right)
        mid.addWidget(rw, 1)
        root.addLayout(mid, 1)

        self._set_running_ui(False)

    def _block_header(self):
        bar = QFrame(); bar.setFixedHeight(58)
        bar.setStyleSheet(f"background:{PANEL}; border:1px solid {BORDER}; border-radius:4px;")
        lay = QHBoxLayout(bar); lay.setContentsMargins(14, 6, 14, 6)
        for fn in ("00021f2021030914260622.png", "00021b2021031713352962.png"):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aset_batt", "ui", fn)
            pix = QPixmap(path)
            if not pix.isNull():
                lb = QLabel(); lb.setPixmap(pix.scaledToHeight(40, Qt.TransformationMode.SmoothTransformation))
                lay.addWidget(lb)
        title = QLabel("BATTERY TEST & SORTING COMMAND CENTER")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        lay.addWidget(title); lay.addStretch(1)
        self.lbl_state = QLabel("  IDLE  ")
        self.lbl_state.setStyleSheet(self._pill(NEUTRAL))
        lay.addWidget(self.lbl_state)
        return bar

    def _pill(self, color):
        return (f"background:{color}; color:white; border-radius:3px; padding:5px 12px;"
                f" font-weight:700; letter-spacing:1px;")

    # ---- Block 1: Test Configuration --------------------------------------
    def _block1_config(self):
        g = QGroupBox("1 · TEST CONFIGURATION")
        lay = QVBoxLayout(g)
        form = QFormLayout(); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.cb_profile = QComboBox(); self.cb_profile.addItems(self.profiles.keys())
        self.cb_profile.currentTextChanged.connect(self._on_profile)
        self.cb_mode = QComboBox(); self.cb_mode.addItems([m.value for m in OperationMode])
        form.addRow("Battery model:", self.cb_profile)
        form.addRow("Operation mode:", self.cb_mode)
        lay.addLayout(form)

        lim = QGroupBox("Safety Limits (validated)")
        grid = QGridLayout(lim)
        self.in_cutoff = self._num_field(0, 100)
        self.in_maxchg = self._num_field(0, 100)
        self.in_maxchg_a = self._num_field(0, 500)
        self.in_maxdis_a = self._num_field(0, 500)
        for r, (lbl, w, u) in enumerate([
                ("Cut-off V", self.in_cutoff, "V"), ("Max charge V", self.in_maxchg, "V"),
                ("Max charge A", self.in_maxchg_a, "A"), ("Max discharge A", self.in_maxdis_a, "A")]):
            grid.addWidget(QLabel(lbl), r, 0); grid.addWidget(w, r, 1); grid.addWidget(QLabel(u), r, 2)
        lay.addWidget(lim)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("▶  START TEST")
        self.btn_start.setStyleSheet(f"QPushButton {{ background:{PANEL2}; border:2px solid {OK};"
                                     f" color:{TEXT}; font-weight:700; padding:10px; }}")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause = QPushButton("❚❚ PAUSE"); self.btn_pause.clicked.connect(self._on_pause)
        self.btn_stop = QPushButton("■ STOP"); self.btn_stop.clicked.connect(self._on_stop)
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            b.setMinimumHeight(40); btns.addWidget(b)
        lay.addLayout(btns)
        self._on_profile(self.cb_profile.currentText())
        return g

    def _num_field(self, lo, hi):
        e = QLineEdit(); val = QDoubleValidator(lo, hi, 2)
        val.setNotation(QDoubleValidator.Notation.StandardNotation)
        e.setValidator(val); e.setMaximumWidth(90)
        return e

    def _on_profile(self, name):
        p = self.profiles.get(name)
        if not p:
            return
        self.in_cutoff.setText(f"{p.cutoff_v}")
        self.in_maxchg.setText(f"{p.max_charge_v}")
        self.in_maxchg_a.setText(f"{p.max_charge_a}")
        self.in_maxdis_a.setText(f"{p.max_discharge_a}")

    # ---- Block 2: Monitoring ----------------------------------------------
    def _block2_monitor(self):
        g = QGroupBox("2 · REAL-TIME MONITORING")
        lay = QVBoxLayout(g)
        row = QHBoxLayout(); row.setSpacing(8)
        self.ro_v = DigitalReadout("Voltage", "V")
        self.ro_i = DigitalReadout("Current", "A")
        self.ro_cap = DigitalReadout("Capacity", "Ah")
        self.gauge = TemperatureGauge()
        for w in (self.ro_v, self.ro_i, self.ro_cap, self.gauge):
            row.addWidget(w, 1)
        lay.addLayout(row)
        self.trend = MultiAxisTrend()
        lay.addWidget(self.trend, 1)
        return g

    # ---- Block 3: Analytics & Grading -------------------------------------
    def _block3_analytics(self):
        g = QGroupBox("3 · ADVANCED ANALYTICS & GRADING")
        lay = QHBoxLayout(g)

        left = QVBoxLayout()
        self.ro_soh = DigitalReadout("State of Health", "%")
        self.ro_ri = DigitalReadout("Internal Res. (HPPC)", "mΩ")
        self.ro_fcap = DigitalReadout("Final Capacity", "Ah")
        for w in (self.ro_soh, self.ro_ri, self.ro_fcap):
            left.addWidget(w)
        grade_box = QFrame(); grade_box.setStyleSheet(
            f"background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px;")
        gl = QVBoxLayout(grade_box)
        cap = QLabel("SORTING GRADE"); cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(f"color:{MUTED}; font-size:10px; font-weight:700; border:0;")
        self.lbl_grade = QLabel("—"); self.lbl_grade.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_grade.setFont(QFont("Segoe UI", 34, QFont.Weight.Bold))
        self.lbl_grade.setStyleSheet(f"color:{MUTED}; border:0;")
        gl.addWidget(cap); gl.addWidget(self.lbl_grade)
        left.addWidget(grade_box, 1)
        lw = QWidget(); lw.setLayout(left); lw.setFixedWidth(220)
        lay.addWidget(lw)

        tabs = QTabWidget()
        self.plot_ica = pg.PlotWidget(); self.plot_ica.setBackground(PANEL2)
        self.plot_ica.setLabel("bottom", "Voltage", units="V")
        self.plot_ica.setLabel("left", "dQ/dV")
        self.plot_dtv = pg.PlotWidget(); self.plot_dtv.setBackground(PANEL2)
        self.plot_dtv.setLabel("bottom", "Voltage", units="V")
        self.plot_dtv.setLabel("left", "dT/dV")
        tabs.addTab(self.plot_ica, "ICA (dQ/dV)")
        tabs.addTab(self.plot_dtv, "DTV (dT/dV)")
        lay.addWidget(tabs, 1)
        return g

    # ---- Block 4: Safety & Alarm ------------------------------------------
    def _block4_safety(self):
        g = QGroupBox("4 · SAFETY & ALARM")
        lay = QVBoxLayout(g)
        self.btn_estop = QPushButton("⏻  EMERGENCY STOP")
        self.btn_estop.setMinimumHeight(80)
        self.btn_estop.setStyleSheet(
            f"QPushButton {{ background:{CRIT}; color:white; border:3px solid #7a1010;"
            f" border-radius:6px; font-size:18px; font-weight:800; letter-spacing:1px; }}"
            f"QPushButton:hover {{ background:#a81f1f; }}")
        self.btn_estop.clicked.connect(self._on_estop)
        lay.addWidget(self.btn_estop)
        lay.addWidget(QLabel("Alarm Console"))
        self.console = QTextEdit(); self.console.setReadOnly(True)
        self.console.setFont(QFont("Consolas", 9))
        lay.addWidget(self.console, 1)
        self._log("INFO", "System ready. Simulated backend active.")
        return g

    # ---- Block 5: Data Management ------------------------------------------
    def _block5_data(self):
        g = QGroupBox("5 · DATA MANAGEMENT")
        lay = QVBoxLayout(g)
        self.lbl_csv = QLabel("CSV: (none)")
        self.lbl_csv.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.lbl_csv)
        self.btn_pdf = QPushButton("⤓  GENERATE PDF REPORT")
        self.btn_pdf.clicked.connect(self._on_pdf); self.btn_pdf.setEnabled(False)
        lay.addWidget(self.btn_pdf)
        return g

    # =======================================================================
    # Control flow
    # =======================================================================
    def _build_config(self) -> Optional[TestConfig]:
        base = self.profiles[self.cb_profile.currentText()]
        try:
            p = BatteryProfile(**asdict(base))
            p.cutoff_v = float(self.in_cutoff.text())
            p.max_charge_v = float(self.in_maxchg.text())
            p.max_charge_a = float(self.in_maxchg_a.text())
            p.max_discharge_a = float(self.in_maxdis_a.text())
        except ValueError:
            QMessageBox.warning(self, "Validation", "Safety-limit fields must be valid numbers.")
            return None
        if p.cutoff_v >= p.max_charge_v:
            QMessageBox.warning(self, "Validation", "Cut-off V must be below Max charge V.")
            return None
        return TestConfig(profile=p, mode=OperationMode(self.cb_mode.currentText()))

    def _on_start(self):
        if self.thread is not None:
            return
        cfg = self._build_config()
        if cfg is None:
            return
        self.t_buf.clear(); self.v_buf.clear(); self.i_buf.clear(); self.temp_buf.clear()
        os.makedirs("logs", exist_ok=True)
        self._csv_path = os.path.join("logs", f"telemetry_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self.lbl_csv.setText(f"CSV: {self._csv_path}")
        self.btn_pdf.setEnabled(False)

        backend = SimulatedBackend()        # integrated app passes HardwareBackend(hw)
        self.thread = QThread()
        self.worker = AcquisitionWorker(backend, cfg, self._csv_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.telemetry.connect(self._on_telemetry)
        self.worker.alarm.connect(self._log)
        self.worker.state.connect(self._on_state)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()
        self._set_running_ui(True)
        self._log("INFO", f"Test started: {cfg.profile.name} · {cfg.mode.value}")

    def _on_pause(self):
        if self.worker:
            paused = self.btn_pause.text().startswith("❚❚")
            self.worker.pause(paused)
            self.btn_pause.setText("▶ RESUME" if paused else "❚❚ PAUSE")

    def _on_stop(self):
        if self.worker:
            self.worker.stop()
            self._log("INFO", "Stop requested by operator.")

    def _on_estop(self):
        if self.worker:
            self.worker.emergency_stop()
        else:
            self._log("CRITICAL", "E-STOP pressed (no test running).")
        self.lbl_state.setText("  E-STOP  "); self.lbl_state.setStyleSheet(self._pill(CRIT))

    # ---- worker signal handlers -------------------------------------------
    def _on_telemetry(self, row: dict):
        self.t_buf.append(row["elapsed"]); self.v_buf.append(row["v"])
        self.i_buf.append(row["i"]); self.temp_buf.append(row["temp"])
        p = self.profiles[self.cb_profile.currentText()]
        self.ro_v.set_value(row["v"], "{:.3f}", alarm=row["v"] > p.ovp)
        self.ro_i.set_value(row["i"], "{:.3f}")
        self.ro_cap.set_value(row["cap"], "{:.4f}")
        self.gauge.update_temp(row["temp"], p.otp_warn, p.otp_crit)

    def _refresh_plot(self):
        if self.t_buf:
            n = 2000   # cap points for render performance
            self.trend.update(self.t_buf[-n:], self.v_buf[-n:],
                              self.i_buf[-n:], self.temp_buf[-n:])

    def _on_state(self, st: str):
        colors = {"RUNNING": OK, "PAUSED": WARN, "STOPPED": NEUTRAL, "ESTOP": CRIT}
        self.lbl_state.setText(f"  {st}  ")
        self.lbl_state.setStyleSheet(self._pill(colors.get(st, NEUTRAL)))

    def _on_finished(self, results: dict):
        self._results = results
        self.ro_soh.set_value(results["soh"], "{:.1f}")
        self.ro_ri.set_value(results["ri_mohm"], "{:.2f}")
        self.ro_fcap.set_value(results["capacity_ah"], "{:.3f}")
        grade = results["grade"]
        gc = {"A": OK, "B": INFO, "C": WARN, "REJECT": CRIT}.get(grade, MUTED)
        self.lbl_grade.setText(grade); self.lbl_grade.setStyleSheet(f"color:{gc}; border:0;")
        iv, ic = results["ica"]
        if len(iv):
            self.plot_ica.clear(); self.plot_ica.plot(iv, ic, pen=pg.mkPen("#1f4e79", width=2))
        dv, dt = results["dtv"]
        if len(dv):
            self.plot_dtv.clear(); self.plot_dtv.plot(dv, dt, pen=pg.mkPen("#7a2020", width=2))
        self.btn_pdf.setEnabled(True)
        self._log("INFO", f"Test complete — SOH {results['soh']:.1f}%, Grade {grade}")
        self._set_running_ui(False)

    def _cleanup_thread(self):
        if self.thread:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None
        self.btn_pause.setText("❚❚ PAUSE")

    # ---- data / report ----------------------------------------------------
    def _on_pdf(self):
        if not self._results:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF Report",
                                              "battery_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        p = self.profiles[self.cb_profile.currentText()]
        task = ReportTask(path, p, self._results, self._csv_path or "", self._on_pdf_done)
        self.pool.start(task)
        self._log("INFO", "Generating PDF report (background)…")

    def _on_pdf_done(self, ok: bool, info: str):
        def show():
            self._log("INFO", f"PDF saved: {info}") if ok else self._log("CRITICAL", f"PDF failed: {info}")
        QTimer.singleShot(0, show)   # marshal from QRunnable thread to the UI thread

    # ---- helpers ----------------------------------------------------------
    def _log(self, sev: str, msg: str):
        color = {"CRITICAL": CRIT, "WARNING": WARN, "INFO": MUTED}.get(sev, MUTED)
        ts = datetime.now().strftime("%H:%M:%S")
        self.console.append(f'<span style="color:{color}">[{ts}] {sev:<8} {msg}</span>')

    def _set_running_ui(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.cb_profile.setEnabled(not running)
        self.cb_mode.setEnabled(not running)

    def closeEvent(self, e):
        if self.worker:
            self.worker.emergency_stop()
            if self.thread:
                self.thread.quit(); self.thread.wait(1500)
        e.accept()


def main():
    pg.setConfigOptions(antialias=True)
    app = QApplication(sys.argv)
    win = CommandCenter()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
