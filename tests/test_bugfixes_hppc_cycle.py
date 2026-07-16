"""Regression tests for the workflow-stopping bugs fixed in this session:

1. AutoController._monitor_loop must survive a bounded number of consecutive
   transient hardware-read errors (retry with backoff) instead of dying on the
   very first exception — and if it DOES give up, it must reset monitor_running
   so the operator can restart monitoring, and post a clear alarm.

2. Logging the HPPC-pulse / cycle-life-discharge current with the WRONG sign
   (a bug that existed in isa101_views.py's sequence threads) silently breaks
   the 1-RC ECM fit. This test locks the analysis pipeline's behavior on
   discharge-positive vs. negated data so that mistake can't quietly return in
   any thread that logs HPPC/discharge samples.

These tests avoid Qt entirely (no QApplication/BatteryQtWindow), matching the
project's existing test style — they exercise AutoController and the analysis
pipeline directly with lightweight fakes.
"""
import csv
import math
import os
import tempfile
import time
import unittest

from aset_batt.app.auto_controller import AutoController
from aset_batt.acquisition.analysis import analyze_csv, profile_from_config
from aset_batt.acquisition.models import BatteryProfile


# ---------------------------------------------------------------------------
# Lightweight fakes (no Qt, no real hardware)
# ---------------------------------------------------------------------------
class _FakeEventHandler:
    def __init__(self):
        self.events = []

    def post_event(self, etype, data):
        self.events.append((etype, data))


class _FakeEstimator:
    def update(self, v, i, dt, temp):
        return {"soc": 50.0, "rin": 0.03, "soh": 100.0}


class _FakeData:
    def __init__(self):
        self.rows = []

    def log_row(self, *args, **kwargs):
        self.rows.append(args)


class _FakeSystemConfig:
    safety_limits = {
        "max_temperature": 60.0, "min_temperature": -10.0,
        "max_current": 30.0, "max_voltage": 15.0, "min_voltage": 10.0,
    }


class _FakeConfig:
    def __init__(self):
        self.system = _FakeSystemConfig()


class _FakeHW:
    def __init__(self, read_vi_fn):
        self.is_connected = True
        self.current_temp = 25.0
        self._psu_output_on = False
        self.read_vi = read_vi_fn


def _controller(read_vi_fn):
    c = AutoController(root=None, hw=_FakeHW(read_vi_fn), data=_FakeData(),
                       estimator=_FakeEstimator(), config=_FakeConfig())
    c.event_handler = _FakeEventHandler()
    c.monitor_running = True
    c._start_time = time.time()
    return c


class TestMonitorLoopSurvivesTransientErrors(unittest.TestCase):
    """A single VISA/USB hiccup used to kill the whole monitor loop on the spot
    (old code: `except Exception: break`), and monitor_running was never reset,
    so start_monitor()'s guard meant the operator could never restart it either."""

    def test_retries_past_a_couple_of_transient_errors(self):
        calls = {"n": 0}

        def flaky_then_ok():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("transient VISA timeout")
            if calls["n"] >= 4:
                # stop the test loop once we've proven it kept going past the errors
                controller.monitor_running = False
            return (12.0, 0.0, 0.0)

        controller = _controller(flaky_then_ok)
        controller._monitor_loop()   # runs to completion synchronously

        # Old code would have broken (and stopped calling read_vi) after the very
        # first exception, i.e. calls["n"] == 1. New code retries past it.
        self.assertGreaterEqual(calls["n"], 4)
        # Two transient errors must NOT be treated as fatal.
        self.assertEqual(controller.event_handler.events, [])

    def test_gives_up_after_max_consecutive_errors_and_is_restartable(self):
        def always_fails():
            raise RuntimeError("permanent VISA failure")

        controller = _controller(always_fails)
        controller._MONITOR_MAX_CONSEC_ERRORS = 2   # speed up the test
        controller._monitor_loop()   # must exit on its own (no manual stop needed)

        # monitor_running MUST be reset by the loop itself — otherwise
        # start_monitor()'s "if not running" guard permanently no-ops and the
        # operator can never restart monitoring short of an app restart.
        self.assertFalse(controller.monitor_running)
        self.assertEqual(len(controller.event_handler.events), 1)
        _etype, payload = controller.event_handler.events[0]
        self.assertEqual(payload[0], "Monitor Stopped")


# ---------------------------------------------------------------------------
# Sign-convention regression: a bugged HPPC/cycle-life sequence thread logged
# `-i` instead of `i` for the discharge-positive current, silently breaking the
# 1-RC ECM fit for that entire test run.
# ---------------------------------------------------------------------------
def _write_hppc_csv(path: str, sign: float):
    """3 relax/pulse cycles at ~5 Hz, current logged with the given sign
    (``sign=+1`` = correct discharge-positive convention, ``sign=-1`` reproduces
    the bug)."""
    r0, r1, tau, cur, voc = 0.012, 0.018, 18.0, 7.0, 12.8
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
                    "SoC_pct", "Temperature_C", "Capacity_Ah", "Mode"])
        t = 0.0
        for _cyc in range(3):
            for _k in range(30):    # 6 s relax at 5 Hz
                w.writerow(["00:00:00", f"{t:.2f}", "12.8000", "0.0000",
                           "80.0", "25.0", "0.0", "HPPC"])
                t += 0.2
            for k in range(30):     # 6 s pulse at 5 Hz
                v = voc - cur * (r0 + r1 * (1 - math.exp(-(k * 0.2) / tau)))
                w.writerow(["00:00:00", f"{t:.2f}", f"{v:.4f}",
                           f"{sign * cur:.4f}", "79.0", "25.0", "0.01", "HPPC"])
                t += 0.2


class TestHppcSignConventionRegression(unittest.TestCase):
    def setUp(self):
        from aset_batt.core.config import config_manager
        self.profile = profile_from_config(config_manager)
        fd, self.path_ok = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd, self.path_bug = tempfile.mkstemp(suffix=".csv")
        os.close(fd)

    def tearDown(self):
        for p in (self.path_ok, self.path_bug):
            if os.path.exists(p):
                os.remove(p)

    def test_discharge_positive_current_identifies_ecm(self):
        _write_hppc_csv(self.path_ok, sign=+1.0)
        res = analyze_csv(self.path_ok, self.profile, force_hppc=True)
        self.assertTrue(res["ecm_identified"])
        self.assertGreater(res["r1_mohm"], 0.0)

    def test_negated_current_breaks_ecm_identification(self):
        """Locks the exact failure mode of the bug that was fixed: if any
        sequence thread ever logs the HPPC/discharge current with the wrong
        sign again, this test catches it — the fit silently stops converging."""
        _write_hppc_csv(self.path_bug, sign=-1.0)
        res = analyze_csv(self.path_bug, self.profile, force_hppc=True)
        self.assertFalse(res["ecm_identified"])


# ---------------------------------------------------------------------------
# G6 regen pulse: identify_hppc_pulses()'s edge-mask fix (ia > thr -> abs(ia)
# > thr) must detect a legitimate negative-current (regen) pulse alongside a
# positive-current (discharge) pulse — both correctly signed, i.e. the "good"
# convention this file's own sign-bug tests above guard, not the injected bug.
# ---------------------------------------------------------------------------
def _write_hppc_csv_with_regen(path: str):
    """One rest -> discharge-pulse -> rest -> regen-pulse -> rest cycle, both
    pulses correctly signed (discharge positive, regen negative)."""
    r0, r1, tau, cur_dis, cur_regen, voc = 0.012, 0.018, 18.0, 7.0, -5.25, 12.8
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
                    "SoC_pct", "Temperature_C", "Capacity_Ah", "Mode"])
        t = 0.0

        def rest(n, soc):
            nonlocal t
            for _ in range(n):
                w.writerow(["00:00:00", f"{t:.2f}", "12.8000", "0.0000",
                           f"{soc}", "25.0", "0.0", "HPPC"])
                t += 0.2

        def pulse(n, cur, soc):
            nonlocal t
            for k in range(n):
                v = voc - cur * (r0 + r1 * (1 - math.exp(-(k * 0.2) / tau)))
                w.writerow(["00:00:00", f"{t:.2f}", f"{v:.4f}",
                           f"{cur:.4f}", f"{soc}", "25.0", "0.01", "HPPC"])
                t += 0.2

        rest(30, "80.0")
        pulse(30, cur_dis, "79.0")
        rest(30, "79.0")
        pulse(30, cur_regen, "82.0")
        rest(30, "82.0")


class TestHppcRegenPulseDetection(unittest.TestCase):
    def setUp(self):
        from aset_batt.core.config import config_manager
        self.profile = profile_from_config(config_manager)
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_discharge_and_regen_pulses_both_detected_and_tagged(self):
        _write_hppc_csv_with_regen(self.path)
        res = analyze_csv(self.path, self.profile, force_hppc=True)
        pulses = res["hppc_pulses"]
        self.assertEqual(len(pulses), 2,
                         "sign-mask fix must detect BOTH the discharge pulse "
                         "and the regen (negative-current) pulse")
        self.assertEqual(pulses[0]["leg"], "discharge")
        self.assertGreater(pulses[0]["i_pulse_a"], 0)
        self.assertEqual(pulses[1]["leg"], "regen")
        self.assertLess(pulses[1]["i_pulse_a"], 0)


if __name__ == "__main__":
    unittest.main()
