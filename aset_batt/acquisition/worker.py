"""Background acquisition worker (QThread).

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

from PySide6.QtCore import QObject, Signal, QThread, QMutex, QMutexLocker

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
            # perf_counter: sub-µs monotonic clock. time.monotonic() on Windows can be
            # quantized to ~15.6 ms, a ~15 % error on a 100 ms (10 Hz) dt → corrupts the
            # coulomb integral. Timestamp is captured AT the measurement, not before it.
            t0 = time.perf_counter()
            last_t = t0
            last_i = 0.0                               # discharge-positive
            while True:
                with QMutexLocker(self._ctrl):
                    if not self._running:
                        break
                    paused, estop = self._paused, self._estop
                if estop:
                    break
                if paused:
                    QThread.msleep(40)
                    last_t = time.perf_counter()       # don't accrue dt across the pause
                    last_i = 0.0
                    continue

                elapsed = time.perf_counter() - t0

                with QMutexLocker(self._io):
                    v, i_raw = self.backend.step(elapsed - (last_t - t0), elapsed)
                    temp = self.backend.read_temperature()
                t_meas = time.perf_counter()           # timestamp AT the sample
                # Normalize sign ONCE at the backend boundary: backend returns charge +,
                # discharge −; the whole worker + analysis speaks discharge-POSITIVE.
                i = -i_raw

                dt = t_meas - last_t
                if dt <= 0:
                    dt = period
                # Trapezoidal coulomb counting on signed current = net Ah removed
                # (this IS the discharge capacity; charge phases correctly subtract).
                self.cap_ah += 0.5 * (i + last_i) * dt / 3600.0
                last_i, last_t = i, t_meas

                # Live SoC from the real estimator (discharge-positive). SoH/R are NOT
                # live quantities — they come from the final analysis, not per-sample.
                soc = float("nan")
                if self.estimator is not None and dt > 0:
                    try:
                        st = self.estimator.update(v, i, dt=dt, temp=temp)
                        soc = st.get("soc", float("nan"))
                    except Exception as e:
                        logger.debug("estimator update skipped: %s", e)

                v_hist.append(v); q_hist.append(self.cap_ah); t_hist.append(temp)
                time_hist.append(elapsed); i_hist.append(i)

                self._check_safety(v, i, temp, p)

                row = {"elapsed": elapsed, "v": v, "i": i, "cap": self.cap_ah,
                       "soc": soc, "temp": temp, "mode": self.cfg.mode.value}
                writer.writerow([datetime.now().isoformat(timespec="milliseconds"),
                                 f"{elapsed:.3f}", f"{v:.4f}", f"{i:.4f}",  # discharge +
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

        results = self._post_process(time_hist, i_hist, v_hist, q_hist, t_hist, p)
        # Feed final analysis back into the estimator so subsequent live estimation is
        # sharper: (a) measured SoH → SoH-adjusted coulomb capacity; (b) HPPC ECM
        # (R0/R1/C1) → the EKF's 1-RC parameters (replaces the rough defaults).
        if self.estimator is not None:
            try:
                soh = results.get("soh")
                if soh is not None and not math.isnan(soh):
                    self.estimator.set_soh(soh)
                if results.get("ecm_identified") and hasattr(self.estimator, "update_ecm"):
                    r0 = results.get("r0_mohm", 0.0) / 1000.0
                    r1 = results.get("r1_mohm", 0.0) / 1000.0
                    c1 = results.get("c1_farad", 0.0)
                    if r0 > 0 and r1 > 0 and c1 > 0:
                        self.estimator.update_ecm(r0, r1, c1)
            except Exception as e:
                logger.debug("estimator feedback skipped: %s", e)
        self.finished.emit(results)
        if not self._estop:
            self.state.emit("STOPPED")

    # ---- software failsafes (interlocks) ----------------------------------
    def _check_safety(self, v, i, temp, p: BatteryProfile):
        if v > p.ovp:
            self._oneshot("OVP", "CRITICAL", f"Over-voltage {v:.2f} V > {p.ovp} V")
            self.emergency_stop()
        elif v < p.uvp and i > 0:    # i discharge-positive → under-voltage while discharging
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
                      p: BatteryProfile):
        """Delegate to the single application-wide analysis (aset_batt.acquisition.
        analysis). Worker current is already discharge-positive (normalized in run()).

        SoH is a FINAL metric: it is computed by analyze_series from the measured
        discharge capacity ÷ rated — NOT taken from the estimator's running value
        (which is not a per-sample quantity and would otherwise override the real
        capacity-based SoH with a stale/placeholder number).

        Runs via analyze_series_mp (separate process), not analyze_series directly:
        this method executes on the worker QThread, which still shares the single
        process-wide GIL with the Qt main thread — the ECM curve-fit inside holds
        that GIL for the whole fit (~5-15s), so the UI would report "Not Responding"
        exactly as it did before analyze_csv's call sites got the same treatment."""
        from aset_batt.acquisition.analysis import analyze_series_mp
        cur_pos = np.asarray(i_hist, float)   # already discharge-positive
        return analyze_series_mp(time_hist, cur_pos, v_hist, t_hist, q_hist, p,
                                 is_hppc=(self.cfg.mode == OperationMode.HPPC), soh=None)
