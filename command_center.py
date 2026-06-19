"""
Automated Battery Performance Testing & Sorting — Command Center
================================================================
Industrial-grade HMI (PyQt6) following the ISA-101 High-Performance HMI standard:
a desaturated gray shell where bright color is reserved exclusively for alarms,
status indicators, the temperature gauge, and the sorting grade.

Architecture
------------
UI thread (this module's widgets) is fully decoupled from a background
AcquisitionWorker that lives on a dedicated QThread. The worker owns all
instrument I/O (PyVISA/SCPI to PSU + e-load, pyserial UART to the ESP32/MLX90614)
and high-rate CSV logging, so the UI never blocks. Communication is one-way via
Qt signals (worker -> UI) and thread-safe command methods (UI -> worker) guarded
by QMutex. The E-Stop path takes the instrument mutex directly and zeroes every
output immediately — a true hardware override that does not wait for the loop.

PDF generation runs on a QThreadPool QRunnable so report rendering can't freeze
the UI either.

Run:  python command_center.py        (simulated backend — no hardware required)
Real hardware: implement VisaSerialBackend with your instrument addresses.
"""
from __future__ import annotations

import os
import sys
import csv
import json
import math
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np

from PyQt6.QtCore import (
    Qt, QThread, QObject, pyqtSignal, QMutex, QMutexLocker, QTimer,
    QRunnable, QThreadPool,
)
from PyQt6.QtGui import QFont, QDoubleValidator, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QTabWidget, QTextEdit, QFrame, QSizePolicy, QMessageBox, QFileDialog,
)
import pyqtgraph as pg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("command_center")

# ===========================================================================
# ISA-101 High-Performance HMI palette
#   Gray shell; saturated color ONLY for status/alarm/gauge/grade.
# ===========================================================================
BG      = "#b9bdc1"   # window (medium neutral gray)
PANEL   = "#c9cdd1"   # group panels
PANEL2  = "#d7dadd"   # cards / plot background
FIELD   = "#eceef0"   # input background
BORDER  = "#8c9296"   # outlines
TEXT    = "#1d2123"   # primary text (near-black)
MUTED   = "#54595d"   # secondary text
# status / alarm colors — used sparingly per ISA-101
OK      = "#2e7d32"   # normal / running / Grade A
WARN    = "#c98a00"   # warning / Grade C
CRIT    = "#c62828"   # critical / alarm / E-Stop / Reject
INFO    = "#1565c0"   # info / selection / Grade B
NEUTRAL = "#6b7075"   # idle / stopped


# ===========================================================================
# Domain model
# ===========================================================================
class OperationMode(Enum):
    CC_CV_CHARGE = "CC-CV Charge"
    CC_DISCHARGE = "Constant Current Discharge"
    HPPC = "HPPC Pulse Test"


@dataclass
class BatteryProfile:
    name: str
    chemistry: str
    nominal_v: float
    series: int
    capacity_ah: float
    max_charge_v: float
    cutoff_v: float
    max_charge_a: float
    max_discharge_a: float
    ovp: float
    uvp: float
    otp_warn: float
    otp_crit: float
    internal_r: float = 0.03


@dataclass
class TestConfig:
    profile: BatteryProfile
    mode: OperationMode
    sample_hz: float = 10.0


def load_profiles(path: str = "command_center_profiles.json") -> dict[str, BatteryProfile]:
    """Dynamic profile loading from an external JSON structure (+ built-in fallback)."""
    fallback = {
        "LiFePO4 25.6V (8S, 50Ah)": BatteryProfile(
            "LiFePO4 25.6V (8S, 50Ah)", "LiFePO4", 25.6, 8, 50.0,
            29.2, 20.0, 25.0, 50.0, 30.0, 18.0, 45.0, 55.0, 0.030),
        "Lead-Acid 12V (6S, 7Ah)": BatteryProfile(
            "Lead-Acid 12V (6S, 7Ah)", "Lead-Acid", 12.0, 6, 7.0,
            14.4, 10.5, 1.4, 7.0, 15.0, 10.0, 45.0, 55.0, 0.030),
    }
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        logger.warning("profiles file missing — using built-in fallback")
        return fallback
    try:
        with open(full, "r", encoding="utf-8") as f:
            data = json.load(f)
        out: dict[str, BatteryProfile] = {}
        for name, d in data.get("profiles", {}).items():
            out[name] = BatteryProfile(name=name, **d)
        return out or fallback
    except Exception as e:
        logger.error("profile load failed (%s) — fallback", e)
        return fallback


# ===========================================================================
# Instrument backend (SCPI / UART placeholders + simulation)
# ===========================================================================
class InstrumentBackend:
    """Abstract instrument access. start_mode/step/read_temperature are called
    ONLY from the worker thread, serialized by the worker's I/O mutex."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def start_mode(self, cfg: TestConfig) -> None: ...
    def step(self, dt: float, elapsed: float) -> tuple[float, float]:
        """Advance one control cycle; return measured (voltage_V, current_A).
        Sign convention: charge current positive, discharge negative."""
        raise NotImplementedError
    def read_temperature(self) -> float: ...
    def emergency_zero(self) -> None: ...
    def safe_shutdown(self) -> None: ...


class VisaSerialBackend(InstrumentBackend):
    """Real-hardware backend. SCPI command placeholders shown; wire to your
    GW-Instek PSU/e-load (PyVISA over LAN/USB) and ESP32 telemetry (pyserial)."""

    def __init__(self, psu_addr: str, load_addr: str, esp_port: str):
        self.psu_addr, self.load_addr, self.esp_port = psu_addr, load_addr, esp_port
        self.psu = self.load = self.ser = None
        self._cfg: Optional[TestConfig] = None

    def connect(self):
        import pyvisa, serial  # imported lazily so simulation needs neither
        rm = pyvisa.ResourceManager()
        self.psu = rm.open_resource(self.psu_addr)
        self.load = rm.open_resource(self.load_addr)
        self.ser = serial.Serial(self.esp_port, 115200, timeout=0.2)
        self.psu.write("*RST"); self.load.write("*RST")

    def start_mode(self, cfg: TestConfig):
        self._cfg = cfg
        p = cfg.profile
        if cfg.mode == OperationMode.CC_CV_CHARGE:
            self.load.write(":INP OFF")
            self.psu.write(f":VOLT {p.max_charge_v}")
            self.psu.write(f":CURR {p.max_charge_a}")
            self.psu.write(":OUTP ON")
        elif cfg.mode == OperationMode.CC_DISCHARGE:
            self.psu.write(":OUTP OFF")
            self.load.write(":MODE CC")
            self.load.write(f":CURR {p.max_discharge_a}")
            self.load.write(":INP ON")
        elif cfg.mode == OperationMode.HPPC:
            self.psu.write(":OUTP OFF"); self.load.write(":INP OFF")

    def step(self, dt, elapsed):
        v = float(self.psu.query("MEAS:VOLT?"))
        i_src = float(self.psu.query("MEAS:CURR?"))
        i_load = float(self.load.query("MEAS:CURR?"))
        return v, (i_src - i_load)

    def read_temperature(self):
        # ESP32 streams lines like "Object = 31.4 *C"; parse newest line.
        try:
            line = self.ser.readline().decode(errors="ignore")
            if "=" in line:
                return float(line.split("=")[1].split("*")[0])
        except Exception:
            pass
        return float("nan")

    def emergency_zero(self):
        # Independent, minimal SCPI to guarantee de-energization.
        for inst, cmd in ((self.psu, ":OUTP OFF"), (self.load, ":INP OFF")):
            try:
                inst.write(":VOLT 0"); inst.write(":CURR 0"); inst.write(cmd)
            except Exception:
                pass

    def safe_shutdown(self):
        self.emergency_zero()

    def disconnect(self):
        for h in (self.psu, self.load, self.ser):
            try: h.close()
            except Exception: pass


class SimulatedBackend(InstrumentBackend):
    """Physics-lite battery + instrument simulation so the full pipeline (plots,
    logging, analytics, grading, report) runs with no hardware attached."""

    def __init__(self, soh_factor: float = 0.93):
        self._cfg: Optional[TestConfig] = None
        self.soc = 0.2
        self.soh = soh_factor           # hidden "true" health -> drives capacity
        self.r = 0.03
        self.temp = 28.0
        self._i = 0.0
        self._t_phase = 0.0

    def connect(self): pass
    def disconnect(self): pass

    def start_mode(self, cfg: TestConfig):
        self._cfg = cfg
        self.r = cfg.profile.internal_r / max(0.5, self.soh)
        self.soc = 0.15 if cfg.mode == OperationMode.CC_CV_CHARGE else 0.95
        self._t_phase = 0.0

    def _ocv(self, soc: float) -> float:
        p = self._cfg.profile
        soc = min(1.0, max(0.0, soc))
        if p.chemistry == "Lead-Acid":
            cell = 1.95 + 0.18 * soc
        elif p.chemistry == "LiFePO4":
            cell = 3.0 + 0.25 * soc + 0.15 * (soc ** 6)   # flat plateau + knee
        else:
            cell = 3.4 + 0.8 * soc
        return cell * p.series

    def step(self, dt, elapsed):
        p = self._cfg.profile
        cap = p.capacity_ah * self.soh
        mode = self._cfg.mode
        if mode == OperationMode.CC_CV_CHARGE:
            ocv = self._ocv(self.soc)
            i = p.max_charge_a
            v = ocv + i * self.r
            if v >= p.max_charge_v:        # enter CV — taper current
                v = p.max_charge_v
                i = max(0.0, (p.max_charge_v - ocv) / self.r)
            self.soc = min(1.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        elif mode == OperationMode.CC_DISCHARGE:
            i = -p.max_discharge_a
            v = self._ocv(self.soc) + i * self.r
            self.soc = max(0.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        else:  # HPPC: 10s rest / 10s discharge pulse, repeating
            self._t_phase += dt
            phase = (elapsed % 20.0)
            if phase < 10.0:
                i = 0.0
            else:
                i = -p.max_discharge_a * 0.6
            v = self._ocv(self.soc) + i * self.r
            self.soc = max(0.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        # thermal model: ohmic rise toward ambient
        self.temp += (abs(self._i) ** 2 * self.r * 0.02 - (self.temp - 28.0) * 0.02) * dt
        self.temp += np.random.normal(0, 0.03)
        return v + np.random.normal(0, 0.002), self._i

    def read_temperature(self):
        return self.temp

    def emergency_zero(self):
        self._i = 0.0

    def safe_shutdown(self):
        self._i = 0.0


# ===========================================================================
# Background acquisition worker (lives on a QThread)
# ===========================================================================
class AcquisitionWorker(QObject):
    telemetry = pyqtSignal(object)     # dict per sample
    alarm = pyqtSignal(str, str)       # (severity, message)
    state = pyqtSignal(str)            # RUNNING / PAUSED / STOPPED / ESTOP
    finished = pyqtSignal(object)      # results dict for analytics

    def __init__(self, backend: InstrumentBackend, cfg: TestConfig, csv_path: str):
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self.csv_path = csv_path
        self._io = QMutex()            # serializes ALL instrument access
        self._ctrl = QMutex()          # guards control flags
        self._running = True
        self._paused = False
        self._estop = False
        self._warned = set()           # de-dupe one-shot warnings
        self.cap_ah = 0.0

    # ---- UI-thread-safe controls ------------------------------------------
    def pause(self, paused: bool):
        with QMutexLocker(self._ctrl):
            self._paused = paused
        self.state.emit("PAUSED" if paused else "RUNNING")

    def stop(self):
        with QMutexLocker(self._ctrl):
            self._running = False

    def emergency_stop(self):
        """Immediate hardware override — safe to call from the UI thread.
        Takes the instrument mutex and zeroes outputs without waiting for the loop."""
        with QMutexLocker(self._ctrl):
            self._estop = True
            self._running = False
        with QMutexLocker(self._io):
            try:
                self.backend.emergency_zero()
            except Exception as e:
                logger.error("emergency_zero failed: %s", e)
        self.alarm.emit("CRITICAL", "E-STOP — all instruments commanded to zero (hardware override)")
        self.state.emit("ESTOP")

    # ---- main loop --------------------------------------------------------
    def run(self):
        period = 1.0 / max(1.0, self.cfg.sample_hz)
        p = self.cfg.profile
        v_hist, q_hist, t_hist = [], [], []
        hppc_pulses = []
        f = open(self.csv_path, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        writer.writerow(["timestamp", "elapsed_s", "voltage_v", "current_a",
                         "capacity_ah", "temperature_c", "mode"])
        try:
            with QMutexLocker(self._io):
                self.backend.start_mode(self.cfg)
            self.state.emit("RUNNING")
            t0 = time.monotonic()
            last = t0
            prev_i = 0.0
            while True:
                with QMutexLocker(self._ctrl):
                    if not self._running:
                        break
                    paused, estop = self._paused, self._estop
                if estop:
                    break
                if paused:
                    QThread.msleep(40)
                    last = time.monotonic()
                    continue

                now = time.monotonic()
                dt = now - last
                last = now
                elapsed = now - t0

                with QMutexLocker(self._io):
                    v, i = self.backend.step(dt, elapsed)
                    temp = self.backend.read_temperature()

                self.cap_ah += abs(i) * dt / 3600.0
                v_hist.append(v); q_hist.append(self.cap_ah); t_hist.append(temp)

                # HPPC internal-resistance capture (current step edge)
                if self.cfg.mode == OperationMode.HPPC and abs(i - prev_i) > 0.05:
                    hppc_pulses.append((v, i))
                prev_i = i

                self._check_safety(v, i, temp, p)

                row = {"elapsed": elapsed, "v": v, "i": i,
                       "cap": self.cap_ah, "temp": temp,
                       "mode": self.cfg.mode.value}
                writer.writerow([datetime.now().isoformat(timespec="milliseconds"),
                                 f"{elapsed:.3f}", f"{v:.4f}", f"{i:.4f}",
                                 f"{self.cap_ah:.5f}", f"{temp:.2f}", self.cfg.mode.value])
                self.telemetry.emit(row)

                # natural end conditions
                if self.cfg.mode == OperationMode.CC_DISCHARGE and v <= p.cutoff_v:
                    self.alarm.emit("INFO", "Discharge reached cut-off voltage — test complete")
                    break
                if self.cfg.mode == OperationMode.CC_CV_CHARGE and i < 0.02 * p.max_charge_a and elapsed > 2:
                    self.alarm.emit("INFO", "Charge tapered to termination current — test complete")
                    break

                QThread.msleep(int(period * 1000))
        except Exception as e:
            logger.exception("worker loop error")
            self.alarm.emit("CRITICAL", f"Acquisition fault: {e}")
        finally:
            with QMutexLocker(self._io):
                try: self.backend.safe_shutdown()
                except Exception: pass
            f.close()

        results = self._post_process(v_hist, q_hist, t_hist, hppc_pulses, p)
        self.finished.emit(results)
        if not self._estop:
            self.state.emit("STOPPED")

    # ---- software failsafes -----------------------------------------------
    def _check_safety(self, v, i, temp, p: BatteryProfile):
        if v > p.ovp:
            self._oneshot("OVP", "CRITICAL", f"Over-voltage {v:.2f} V > {p.ovp} V")
            self.emergency_stop()
        elif v < p.uvp and i < 0:
            self._oneshot("UVP", "WARNING", f"Under-voltage {v:.2f} V < {p.uvp} V")
        if not math.isnan(temp):
            if temp >= p.otp_crit:
                self._oneshot("OTC", "CRITICAL", f"Over-temperature {temp:.1f}°C ≥ {p.otp_crit}°C")
                self.emergency_stop()
            elif temp >= p.otp_warn:
                self._oneshot("OTW", "WARNING", f"Temperature elevated {temp:.1f}°C ≥ {p.otp_warn}°C")
        if abs(i) > 1.05 * max(p.max_charge_a, p.max_discharge_a):
            self._oneshot("OCP", "CRITICAL", f"Over-current {i:.2f} A")
            self.emergency_stop()

    def _oneshot(self, key, sev, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.alarm.emit(sev, msg)

    # ---- analytics on collected arrays ------------------------------------
    def _post_process(self, v_hist, q_hist, t_hist, hppc_pulses, p: BatteryProfile):
        v = np.asarray(v_hist, float); q = np.asarray(q_hist, float)
        t = np.asarray(t_hist, float)
        capacity = float(q[-1]) if q.size else 0.0
        soh = 100.0 * capacity / p.capacity_ah if p.capacity_ah else 0.0
        soh = float(min(120.0, max(0.0, soh)))
        ri = Analytics.internal_resistance_hppc(hppc_pulses, p)
        ica_v, ica = Analytics.incremental_capacity(v, q)
        dtv_v, dtv = Analytics.differential_thermal(v, t)
        grade = Analytics.grade(soh, ri, p)
        return {
            "soh": soh, "capacity_ah": capacity, "ri_mohm": ri * 1000.0,
            "grade": grade,
            "ica": (ica_v, ica), "dtv": (dtv_v, dtv),
        }


# ===========================================================================
# Analytics (SOH, Ri/HPPC, ICA, DTV with Gaussian smoothing, grading)
# ===========================================================================
class Analytics:
    @staticmethod
    def _gaussian_smooth(y: np.ndarray, sigma: float = 2.0) -> np.ndarray:
        if y.size < 3:
            return y
        try:
            from scipy.ndimage import gaussian_filter1d
            return gaussian_filter1d(y, sigma)
        except Exception:
            # numpy fallback gaussian kernel
            radius = int(3 * sigma)
            x = np.arange(-radius, radius + 1)
            k = np.exp(-(x ** 2) / (2 * sigma ** 2)); k /= k.sum()
            return np.convolve(y, k, mode="same")

    @staticmethod
    def internal_resistance_hppc(pulses, p: BatteryProfile) -> float:
        """Ri = ΔV/ΔI across the pulse current step."""
        if len(pulses) >= 2:
            (v1, i1), (v2, i2) = pulses[-2], pulses[-1]
            if abs(i2 - i1) > 1e-3:
                return abs((v2 - v1) / (i2 - i1))
        return p.internal_r

    @staticmethod
    def incremental_capacity(v: np.ndarray, q: np.ndarray):
        """ICA: dQ/dV vs V with monotonic-V resampling + Gaussian smoothing."""
        if v.size < 10:
            return np.array([]), np.array([])
        order = np.argsort(v)
        vs, qs = v[order], q[order]
        vu, idx = np.unique(vs, return_index=True)
        qu = qs[idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        qg = np.interp(grid, vu, qu)
        dqdv = np.gradient(Analytics._gaussian_smooth(qg, 3.0), grid)
        return grid, Analytics._gaussian_smooth(dqdv, 2.0)

    @staticmethod
    def differential_thermal(v: np.ndarray, t: np.ndarray):
        """DTV: dT/dV vs V with Gaussian smoothing."""
        if v.size < 10:
            return np.array([]), np.array([])
        order = np.argsort(v)
        vs, ts = v[order], t[order]
        vu, idx = np.unique(vs, return_index=True)
        tu = ts[idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        tg = np.interp(grid, vu, tu)
        dtdv = np.gradient(Analytics._gaussian_smooth(tg, 3.0), grid)
        return grid, Analytics._gaussian_smooth(dtdv, 2.0)

    @staticmethod
    def grade(soh: float, ri_ohm: float, p: BatteryProfile) -> str:
        ri_ratio = ri_ohm / max(1e-6, p.internal_r)
        if soh >= 90 and ri_ratio <= 1.3:
            return "A"
        if soh >= 80 and ri_ratio <= 1.7:
            return "B"
        if soh >= 70 and ri_ratio <= 2.5:
            return "C"
        return "REJECT"


# ===========================================================================
# PDF report (rendered off the UI thread via QRunnable)
# ===========================================================================
class ReportTask(QRunnable):
    def __init__(self, path, profile, results, csv_path, done_cb):
        super().__init__()
        self.path, self.profile, self.results = path, profile, results
        self.csv_path, self.done_cb = csv_path, done_cb

    def run(self):
        try:
            self._build()
            self.done_cb(True, self.path)
        except Exception as e:
            logger.exception("report failed")
            self.done_cb(False, str(e))

    def _build(self):
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(self.path, pagesize=A4, topMargin=18*mm)
        r, p = self.results, self.profile
        story = [
            Paragraph("Battery Test &amp; Sorting Report", styles["Title"]),
            Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), styles["Normal"]),
            Spacer(1, 8*mm),
            Paragraph("Device Under Test", styles["Heading2"]),
        ]
        info = [["Profile", p.name], ["Chemistry", p.chemistry],
                ["Nominal", f"{p.nominal_v} V ({p.series}S)"],
                ["Rated capacity", f"{p.capacity_ah} Ah"]]
        res = [["Final capacity", f"{r['capacity_ah']:.3f} Ah"],
               ["State of Health", f"{r['soh']:.1f} %"],
               ["Internal resistance (HPPC)", f"{r['ri_mohm']:.2f} mΩ"],
               ["Sorting grade", r["grade"]]]
        for title, rows in (("", info), ("Results", res)):
            if title:
                story.append(Paragraph(title, styles["Heading2"]))
            tbl = Table(rows, colWidths=[60*mm, 100*mm])
            tbl.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story += [tbl, Spacer(1, 6*mm)]
        story.append(Paragraph(f"Raw telemetry: {os.path.basename(self.csv_path)}",
                               styles["Normal"]))
        doc.build(story)


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


# ===========================================================================
# Block 2 — multi-axis live trend
# ===========================================================================
class MultiAxisTrend(pg.GraphicsLayoutWidget):
    """V (left) + I (right) + T (far right) vs elapsed time on shared X."""
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

        # rolling telemetry buffers
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
        for fn in ("ui/00021f2021030914260622.png", "ui/00021b2021031713352962.png"):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fn)
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
        mode = OperationMode(self.cb_mode.currentText())
        return TestConfig(profile=p, mode=mode)

    def _on_start(self):
        if self.thread is not None:
            return
        cfg = self._build_config()
        if cfg is None:
            return
        self.t_buf.clear(); self.v_buf.clear(); self.i_buf.clear(); self.temp_buf.clear()
        os.makedirs("logs", exist_ok=True)
        self._csv_path = os.path.join(
            "logs", f"telemetry_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self.lbl_csv.setText(f"CSV: {self._csv_path}")
        self.btn_pdf.setEnabled(False)

        backend = SimulatedBackend()        # swap for VisaSerialBackend(...) on real rig
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
        # called from QRunnable thread -> marshal to UI via QTimer.singleShot
        def show():
            if ok:
                self._log("INFO", f"PDF saved: {info}")
            else:
                self._log("CRITICAL", f"PDF failed: {info}")
        QTimer.singleShot(0, show)

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
