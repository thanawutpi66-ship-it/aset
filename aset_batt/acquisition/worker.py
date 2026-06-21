"""Background acquisition worker (QThread) + PDF report task (QThreadPool).

The worker owns ALL instrument I/O and high-rate CSV logging so the UI never
blocks. Two mutexes: ``_io`` serializes instrument access; ``_ctrl`` guards the
control flags. ``emergency_stop`` is callable from the UI thread — it takes the
I/O mutex directly and zeroes every output immediately (a true hardware override
that does not wait for the loop).

If a ``StateEstimator`` is supplied, live SoC and OCV-corrected SoH come from the
project's real estimation module; otherwise SoH falls back to coulomb capacity ÷ rated.
"""
from __future__ import annotations

import os
import csv
import math
import time
import logging
from datetime import datetime

import numpy as np

from PySide6.QtCore import QObject, Signal, QThread, QMutex, QMutexLocker, QRunnable

from aset_batt.acquisition.models import BatteryProfile, TestConfig, OperationMode

logger = logging.getLogger(__name__)


class AcquisitionWorker(QObject):
    telemetry = Signal(object)     # dict per sample
    alarm = Signal(str, str)       # (severity, message)
    state = Signal(str)            # RUNNING / PAUSED / STOPPED / ESTOP
    finished = Signal(object)      # results dict for analytics

    def __init__(self, backend, cfg: TestConfig, csv_path: str, estimator=None):
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self.csv_path = csv_path
        self.estimator = estimator     # optional StateEstimator (real SoC/SoH)
        self._io = QMutex()
        self._ctrl = QMutex()
        self._running = True
        self._paused = False
        self._estop = False
        self._warned = set()
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
        """Immediate hardware override — safe to call from the UI thread."""
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
        time_hist, i_hist = [], []          # for 1-RC ECM identification (HPPC)
        hppc_pulses = []
        f = open(self.csv_path, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        # Canonical project schema (matches data_utils.DataHandler / battery_data.csv)
        # so analysis_module and any project tool can read a test CSV directly.
        # Current_A uses the project convention: discharge POSITIVE.
        writer.writerow(["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
                         "SoC_pct", "Temperature_C", "Capacity_Ah", "Mode"])
        try:
            with QMutexLocker(self._io):
                self.backend.start_mode(self.cfg)
            self.state.emit("RUNNING")
            t0 = last = time.monotonic()
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
                    v, i = self.backend.step(dt, elapsed)      # charge +, discharge −
                    temp = self.backend.read_temperature()

                self.cap_ah += abs(i) * dt / 3600.0
                # Live SoC/SoH from the real estimator (i_net is discharge-positive).
                soc = float("nan")
                if self.estimator is not None and dt > 0:
                    try:
                        st = self.estimator.update(v, -i, dt=dt, temp=temp)
                        soc = st.get("soc", float("nan"))
                    except Exception as e:
                        logger.debug("estimator update skipped: %s", e)

                v_hist.append(v); q_hist.append(self.cap_ah); t_hist.append(temp)
                time_hist.append(elapsed); i_hist.append(i)
                if self.cfg.mode == OperationMode.HPPC and abs(i - prev_i) > 0.05:
                    hppc_pulses.append((v, i))
                prev_i = i

                self._check_safety(v, i, temp, p)

                row = {"elapsed": elapsed, "v": v, "i": i, "cap": self.cap_ah,
                       "soc": soc, "temp": temp, "mode": self.cfg.mode.value}
                writer.writerow([datetime.now().isoformat(timespec="milliseconds"),
                                 f"{elapsed:.3f}", f"{v:.4f}", f"{-i:.4f}",  # discharge +
                                 f"{soc:.2f}", f"{temp:.2f}", f"{self.cap_ah:.5f}",
                                 self.cfg.mode.value])
                self.telemetry.emit(row)

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
                try:
                    self.backend.safe_shutdown()
                except Exception:
                    pass
            f.close()

        results = self._post_process(time_hist, i_hist, v_hist, q_hist, t_hist,
                                     hppc_pulses, p)
        self.finished.emit(results)
        if not self._estop:
            self.state.emit("STOPPED")

    # ---- software failsafes (interlocks) ----------------------------------
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

    # ---- post-test analytics ----------------------------------------------
    def _post_process(self, time_hist, i_hist, v_hist, q_hist, t_hist,
                      hppc_pulses, p: BatteryProfile):
        """Delegate to the single application-wide analysis (aset_batt.acquisition.
        analysis). Worker current is discharge-negative → flip to canonical
        discharge-positive; supply the estimator's live SoH when available."""
        from aset_batt.acquisition.analysis import analyze_series
        soh = getattr(self.estimator, "soh", None) if self.estimator is not None else None
        cur_pos = -np.asarray(i_hist, float)
        return analyze_series(time_hist, cur_pos, v_hist, t_hist, q_hist, p,
                              is_hppc=(self.cfg.mode == OperationMode.HPPC), soh=soh)


class ReportTask(QRunnable):
    """Render the PDF report off the UI thread."""

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
        doc = SimpleDocTemplate(self.path, pagesize=A4, topMargin=18 * mm)
        r, p = self.results, self.profile
        story = [
            Paragraph("Battery Test &amp; Sorting Report", styles["Title"]),
            Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), styles["Normal"]),
            Spacer(1, 8 * mm),
        ]
        rows = [["Profile", p.name], ["Chemistry", p.chemistry],
                ["Final capacity", f"{r['capacity_ah']:.3f} Ah"],
                ["State of Health", f"{r['soh']:.1f} %"],
                ["Internal resistance (HPPC)", f"{r['ri_mohm']:.2f} mΩ"],
                ["Sorting grade", r["grade"]]]
        tbl = Table(rows, colWidths=[60 * mm, 100 * mm])
        tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story += [tbl, Spacer(1, 6 * mm),
                  Paragraph(f"Raw telemetry: {os.path.basename(self.csv_path)}", styles["Normal"])]
        doc.build(story)
