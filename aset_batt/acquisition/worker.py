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
import gc
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

    # Consecutive samples the CC-CV charge termination current must hold for
    # before actually ending the test — same reasoning as charge_controller.py's
    # ChargeParams.tail_confirm_samples: a single sample below the threshold
    # (regulation blip, sensor noise) used to end the test immediately with no
    # confirmation at all.
    _CV_TAIL_CONFIRM_SAMPLES = 5

    # Same reasoning, for CC_DISCHARGE's cutoff-voltage test-complete check below —
    # a single sample at/below cutoff_v (ADC noise, a brief sag under a load step)
    # used to end the test immediately with no confirmation, same bug class as the
    # CV tail-current check above and the estimator's endpoint-anchor sustain gate.
    _CUTOFF_CONFIRM_SAMPLES = 5

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
        soc_hist = []                       # parallel to time_hist — see update_ecm() feedback below
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
            last_flush_t = t0
            last_i = 0.0                               # discharge-positive
            # Per-substep timing breakdown, same idea as sequences/hppc.py's HPPC
            # Full Sequence pulse-rate alarm — this manual TEST MODE run (including
            # HPPC via "Run Test") is a SEPARATE code path from that automated
            # sequence and didn't have any rate diagnostics at all. Logged every
            # _RATE_LOG_EVERY samples so a slow rig is visible without waiting for
            # the whole test to finish.
            _t_scpi = _t_log = _t_est = _t_emit = _t_safety = _t_flush = 0.0
            _t_sleep_req = _t_sleep_actual = 0.0
            _t_ctrl = 0.0
            _rate_n = 0
            _rate_t0 = t0
            _cv_tail_confirm_n = 0
            _cutoff_confirm_n = 0

            # Python's cyclic GC fires transparently on allocation-count thresholds
            # (not time), runs INLINE holding the GIL, and can be triggered by ANY
            # thread's allocation (a dict, a list.append) — invisible to every
            # perf_counter() wrapper above since it can preempt mid-statement. Real
            # hardware runs showed a suspiciously clean, near-perfect alternating
            # fast/slow-block pattern once "other(unexplained)" was isolated with
            # everything else measured — too regular for external CPU contention,
            # consistent with GC generation thresholds. gc.callbacks fires around
            # every collection process-wide; accumulate its duration directly to
            # test this rather than guess.
            _t_gc_accum = [0.0]
            _gc_t0 = [None]
            def _gc_cb(phase, info):
                if phase == "start":
                    _gc_t0[0] = time.perf_counter()
                elif _gc_t0[0] is not None:
                    _t_gc_accum[0] += time.perf_counter() - _gc_t0[0]
                    _gc_t0[0] = None
            gc.callbacks.append(_gc_cb)
            _RATE_LOG_EVERY = 25
            while True:
                # Time to acquire self._ctrl and evaluate the pause/estop branch —
                # the one remaining unmeasured piece of a normal (non-paused)
                # iteration. Contended if the UI thread is mid-call to pause()/
                # stop()/emergency_stop() (or blocked on the GIL waiting to get
                # there). flush%% explained SOME slow blocks fully (other=0%% once
                # flush was measured) but real hardware runs still showed 22-49%%
                # unexplained in OTHER slow blocks with flush=0%% — this isolates
                # whether ctrl-mutex contention is that remainder.
                _c0 = time.perf_counter()
                with QMutexLocker(self._ctrl):
                    if not self._running:
                        break
                    paused, estop = self._paused, self._estop
                _t_ctrl += time.perf_counter() - _c0
                if estop:
                    break
                if paused:
                    QThread.msleep(40)
                    last_t = time.perf_counter()       # don't accrue dt across the pause
                    last_i = 0.0
                    continue

                _iter_t0 = time.perf_counter()
                elapsed = _iter_t0 - t0

                _s0 = time.perf_counter()
                with QMutexLocker(self._io):
                    v, i_raw = self.backend.step(elapsed - (last_t - t0), elapsed)
                    temp = self.backend.read_temperature()
                _t_scpi += time.perf_counter() - _s0
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
                    _s2 = time.perf_counter()
                    try:
                        st = self.estimator.update(v, i, dt=dt, temp=temp)
                        soc = st.get("soc", float("nan"))
                    except Exception as e:
                        logger.debug("estimator update skipped: %s", e)
                    _t_est += time.perf_counter() - _s2

                v_hist.append(v); q_hist.append(self.cap_ah); t_hist.append(temp)
                time_hist.append(elapsed); i_hist.append(i); soc_hist.append(soc)

                _s3 = time.perf_counter()
                self._check_safety(v, i, temp, p)
                _t_safety += time.perf_counter() - _s3

                row = {"elapsed": elapsed, "v": v, "i": i, "cap": self.cap_ah,
                       "soc": soc, "temp": temp, "mode": self.cfg.mode.value}
                _s1 = time.perf_counter()
                writer.writerow([datetime.now().isoformat(timespec="milliseconds"),
                                 f"{elapsed:.3f}", f"{v:.4f}", f"{i:.4f}",  # discharge +
                                 f"{soc:.2f}", f"{temp:.2f}", f"{self.cap_ah:.5f}",
                                 self.cfg.mode.value])
                _t_log += time.perf_counter() - _s1
                # Cross-thread Qt signal emit to the GUI thread — previously left
                # unmeasured (folded into "other"). Fires once per sample, same
                # cadence as the unexplained ~65ms/sample gap found by comparing
                # "other" against the now-measured actual msleep() duration (see
                # worker Hz breakdown investigation, 2026-07-11) — timing this
                # separately will show directly whether GIL/event-queue contention
                # on this emit() is the hidden cost, instead of it staying invisible.
                _s4 = time.perf_counter()
                self.telemetry.emit(row)
                _t_emit += time.perf_counter() - _s4

                now = time.perf_counter()
                if now - last_flush_t >= 1.0:
                    # Disk sync — the one remaining untimed piece of the loop body.
                    # emit/safety were measured on a real run and came back flat 0%,
                    # ruling both out, yet "other(unexplained)" was still 28-65% in
                    # slow blocks — this fires only once/sec so it wouldn't show up
                    # in every block, matching that unevenness. A stalled disk write
                    # (Windows Defender / antivirus scanning the CSV, slow storage)
                    # would land here and nowhere else already measured.
                    _s5 = time.perf_counter()
                    f.flush()
                    _t_flush += time.perf_counter() - _s5
                    last_flush_t = now

                _rate_n += 1
                if _rate_n >= _RATE_LOG_EVERY:
                    _rate_span = now - _rate_t0
                    if _rate_span > 0.5:
                        _hz = _rate_n / _rate_span
                        _target_hz = 1.0 / period
                        # Root-caused (Windows USB selective suspend — see CLAUDE.md)
                        # and instrumentation kept as a fallback for machines where it
                        # can't be disabled, but the full per-substep breakdown logging
                        # every _RATE_LOG_EVERY samples (~2.5s at 10Hz) is too verbose
                        # for routine, healthy runs. Only pay for the full breakdown
                        # string + %-of-total divisions when a block is genuinely
                        # degraded; a healthy block gets one terse DEBUG line.
                        if _hz < 0.8 * _target_hz:
                            # msleep() overshoot, emit(), safety, and ctrl-mutex were all
                            # measured and ruled out on real hardware runs. f.flush()
                            # explained SOME slow blocks fully but not others. A near-
                            # perfect alternating fast/slow-block pattern (too regular for
                            # external CPU contention) then pointed at the cyclic GC,
                            # which fires on allocation thresholds and can preempt ANY
                            # thread mid-statement, invisible to every timer above — now
                            # measured directly via gc.callbacks (_t_gc_accum).
                            _t_gc = _t_gc_accum[0]
                            _t_accounted = (_t_scpi + _t_log + _t_est + _t_emit + _t_safety
                                           + _t_flush + _t_ctrl + _t_gc + _t_sleep_actual)
                            _t_total = max(_t_accounted, _rate_span)
                            _t_other = max(0.0, _t_total - _t_accounted)
                            # Every explicit Python-level operation in the loop is now
                            # measured and ruled out (SCPI/estimator/log/emit/safety/
                            # flush/ctrl-mutex/GC all flat 0% in the affected blocks) yet
                            # "other(unexplained)" alternates ~0%/~38% in a near-perfect
                            # pattern with a ~6.8s fast+slow period — matching HPPC's own
                            # PULSE<->RELAX cycle (backends.py step()) rather than
                            # anything external. Logging the phase directly instead of
                            # guessing further.
                            _hppc_phase = ("PULSE" if getattr(self.backend, "_hppc_loaded", None)
                                          else "RELAX") if self.cfg.mode == OperationMode.HPPC else "-"
                            logger.info(
                                "%s worker sampled at %.1f Hz (%d samples / %.1fs, target %.1f Hz, "
                                "hppc_phase=%s) — "
                                "breakdown: SCPI %.0f%%  estimator %.0f%%  log %.0f%%  emit %.0f%%  "
                                "safety %.0f%%  flush %.0f%%  ctrl %.0f%%  gc %.0f%%  sleep %.0f%%  "
                                "other(unexplained) %.0f%%  "
                                "(sleep requested %.0fms actual %.0fms over %d samples)",
                                self.cfg.mode.value, _hz, _rate_n, _rate_span, _target_hz, _hppc_phase,
                                100 * _t_scpi / _t_total, 100 * _t_est / _t_total,
                                100 * _t_log / _t_total, 100 * _t_emit / _t_total,
                                100 * _t_safety / _t_total, 100 * _t_flush / _t_total,
                                100 * _t_ctrl / _t_total, 100 * _t_gc / _t_total,
                                100 * _t_sleep_actual / _t_total, 100 * _t_other / _t_total,
                                _t_sleep_req * 1000, _t_sleep_actual * 1000, _rate_n)
                        else:
                            logger.debug(
                                "%s worker sampled at %.1f Hz (%d samples / %.1fs, target %.1f Hz) — healthy",
                                self.cfg.mode.value, _hz, _rate_n, _rate_span, _target_hz)
                    _rate_n = 0
                    _t_scpi = _t_log = _t_est = _t_emit = _t_safety = _t_flush = _t_ctrl = 0.0
                    _t_sleep_req = _t_sleep_actual = 0.0
                    _t_gc_accum[0] = 0.0
                    _rate_t0 = now

                if self.cfg.mode == OperationMode.CC_DISCHARGE:
                    # Require _CUTOFF_CONFIRM_SAMPLES consecutive samples at/below
                    # cutoff before ending — same debounce as the CV tail-current
                    # check above, guarding against a single noisy/sagging sample
                    # ending the test early and understating measured capacity.
                    _cutoff_confirm_n = (_cutoff_confirm_n + 1) if v <= p.cutoff_v else 0
                    if _cutoff_confirm_n >= self._CUTOFF_CONFIRM_SAMPLES:
                        self.alarm.emit("INFO", "Discharge reached cut-off voltage — test complete")
                        break
                if self.cfg.mode == OperationMode.CC_CV_CHARGE:
                    # abs(i), not i: i is discharge-positive (see the sign-flip comment
                    # above), so i is NEGATIVE the entire time this mode is genuinely
                    # charging — "i < 0.02*max_charge_a" was comparing a negative number
                    # against a positive threshold, which is true on EVERY sample from
                    # the first one (not just once tapered), so this check would have
                    # ended every manual CC-CV charge run within a couple seconds
                    # regardless of real current. abs(i) correctly reads the charging
                    # current's magnitude, tapering toward 0 as the battery fills.
                    #
                    # Require _CV_TAIL_CONFIRM_SAMPLES consecutive samples below the
                    # termination current before actually ending — a single sample
                    # (regulation blip, sensor noise near the threshold) used to end
                    # the test immediately with no confirmation, same failure mode
                    # already fixed in charge_controller.py's decide().
                    _cv_tail_confirm_n = (_cv_tail_confirm_n + 1) if (
                        abs(i) < 0.02 * p.max_charge_a and elapsed > 2) else 0
                    if _cv_tail_confirm_n >= self._CV_TAIL_CONFIRM_SAMPLES:
                        self.alarm.emit("INFO", "Charge tapered to termination current — test complete")
                        break

                # Sleep only the time REMAINING in this period, not the full period —
                # a flat msleep(200ms) here regardless of how long SCPI/estimator/log
                # already took meant every iteration cost (real work) + 200ms, capping
                # the achieved rate at ~4 Hz even when the real work was only 30-40ms
                # (confirmed on the real rig: SCPI 9-15%, estimator/log ~0%, "other"
                # 84-91% — "other" WAS this fixed sleep, not hidden overhead). Same
                # self-correcting pacing sequences/hppc.py's pulse loop already uses.
                _iter_elapsed = time.perf_counter() - _iter_t0
                _sleep_ms = max(0, int((period - _iter_elapsed) * 1000))
                _sl0 = time.perf_counter()
                QThread.msleep(_sleep_ms)
                _t_sleep_req += _sleep_ms / 1000.0
                _t_sleep_actual += time.perf_counter() - _sl0
        except Exception as e:
            logger.exception("worker loop error")
            self.alarm.emit("CRITICAL", f"Acquisition fault: {e}")
        finally:
            try:
                gc.callbacks.remove(_gc_cb)
            except ValueError:
                pass
            with QMutexLocker(self._io):
                try:
                    self.backend.safe_shutdown()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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
                        # This callback fires after the WHOLE record's sample loop has
                        # already finished, so self.estimator.soc has moved on well past
                        # the SoC the pulse actually happened at (update_ecm()'s default
                        # anchor). Look up the SoC AT the fitted pulse's own timestamp
                        # from the per-sample history captured above instead.
                        fit_soc = None
                        fit_t = results.get("ecm_fit_t_s")
                        if fit_t is not None and not math.isnan(fit_t) and time_hist:
                            idx = int(np.argmin(np.abs(np.asarray(time_hist) - fit_t)))
                            if not math.isnan(soc_hist[idx]):
                                fit_soc = soc_hist[idx]
                        self.estimator.update_ecm(r0, r1, c1, fit_soc=fit_soc)
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
            # Used to be WARNING-only and never actually stopped anything — the ONLY
            # safety net for HPPC mode specifically (it has no voltage-based test-
            # complete check of its own, unlike CC_DISCHARGE's p.cutoff_v below), so a
            # weak pack under repeated pulses could be driven arbitrarily far past uvp
            # with just one warning ever (_oneshot fires once per run) until the
            # e-load's own hardware UVP trip was the last line of defense. Same
            # emergency_stop() pattern as OVP/OTC/OCP above — this call site already
            # runs outside any lock (see those three), so it's the same proven-safe
            # reentrant call, not a new pattern.
            self._oneshot("UVP", "CRITICAL", f"Under-voltage {v:.2f} V < {p.uvp} V")
            self.emergency_stop()
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
