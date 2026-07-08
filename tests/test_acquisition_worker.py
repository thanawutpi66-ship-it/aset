"""Tests for AcquisitionWorker.run() (aset_batt/acquisition/worker.py) — the
command-center's background acquisition loop. Previously only `_post_process`
had any test coverage; the loop itself (telemetry emission, CSV writing,
safety-triggered emergency_stop, natural cutoff termination, pause/resume)
had none.

Happy-path and safety-trip tests call run() directly on the test thread (same
"call the thread target directly" philosophy as tests/test_graph_feed_
during_sequences.py) with a mocked InstrumentBackend and _post_process
stubbed out (the real one runs a multiprocessing ECM fit — orthogonal to what
this file is testing). The pause/resume test genuinely needs two threads
since pause/resume is inherently a cross-thread interaction, so it runs
worker.run() on a real background thread and drives pause()/stop() from the
main thread.
"""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from aset_batt.acquisition.worker import AcquisitionWorker
from aset_batt.acquisition.models import BatteryProfile, TestConfig, OperationMode

_app = QApplication.instance() or QApplication([])


def _make_profile(**overrides):
    kwargs = dict(
        name="Test Profile", chemistry="LiFePO4", nominal_v=25.6, series=8,
        capacity_ah=50.0, max_charge_v=29.2, cutoff_v=10.0, max_charge_a=25.0,
        max_discharge_a=50.0, ovp=30.0, uvp=5.0, otp_warn=45.0, otp_crit=55.0,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _make_worker(profile, mode=OperationMode.CC_DISCHARGE, sample_hz=1000.0):
    cfg = TestConfig(profile=profile, mode=mode, sample_hz=sample_hz)
    backend = MagicMock()
    backend.read_temperature.return_value = 25.0
    csv_path = os.path.join(tempfile.mkdtemp(prefix="aset_worker_test_"), "run.csv")
    worker = AcquisitionWorker(backend, cfg, csv_path)
    worker._post_process = MagicMock(return_value={"soh": None})
    return worker, backend, csv_path


def _collect_signals(worker):
    """Direct connection: run() may execute on a background Python thread with
    no Qt event loop pumping this process, so a queued (thread-affinity-based)
    connection would never deliver — force synchronous delivery instead."""
    telemetry, alarms, states, finished = [], [], [], []
    worker.telemetry.connect(lambda row: telemetry.append(row), Qt.DirectConnection)
    worker.alarm.connect(lambda sev, msg: alarms.append((sev, msg)), Qt.DirectConnection)
    worker.state.connect(lambda s: states.append(s), Qt.DirectConnection)
    worker.finished.connect(lambda r: finished.append(r), Qt.DirectConnection)
    return telemetry, alarms, states, finished


class TestHappyPath(unittest.TestCase):
    def test_discharge_loop_runs_to_cutoff_and_emits_telemetry(self):
        profile = _make_profile(cutoff_v=10.0)
        worker, backend, csv_path = _make_worker(profile)
        telemetry, alarms, states, finished = _collect_signals(worker)

        backend.step.side_effect = [(12.0, 1.0), (11.0, 1.0), (9.5, 1.0)]

        worker.run()

        self.assertEqual(len(telemetry), 3)
        self.assertAlmostEqual(telemetry[-1]["v"], 9.5)
        self.assertIn("RUNNING", states)
        self.assertEqual(states[-1], "STOPPED")
        self.assertTrue(any("cut-off" in msg for _, msg in alarms))
        self.assertEqual(len(finished), 1)
        backend.safe_shutdown.assert_called_once()

        with open(csv_path, encoding="utf-8") as f:
            rows = f.readlines()
        self.assertEqual(len(rows), 4)  # header + 3 samples

    def test_current_sign_normalized_to_discharge_positive(self):
        """backend.step returns (v, i_raw) with charge +/discharge − per its own
        convention; run() must flip it once at the boundary so downstream
        telemetry speaks discharge-positive (see the comment in run())."""
        profile = _make_profile(cutoff_v=-100.0)  # never trips naturally
        worker, backend, csv_path = _make_worker(profile)
        telemetry, *_ = _collect_signals(worker)

        # stop() flips _running False, but the loop only checks it at the TOP
        # of the next pass, so calling it inside step() still lets this first
        # sample run to completion before the loop exits.
        def _one_shot(dt, elapsed):
            worker.stop()
            return (12.0, -5.0)
        backend.step.side_effect = _one_shot

        worker.run()

        self.assertEqual(len(telemetry), 1)
        self.assertAlmostEqual(telemetry[0]["i"], 5.0)


class TestSafetyTrip(unittest.TestCase):
    def test_overvoltage_triggers_emergency_stop_and_halts_loop(self):
        profile = _make_profile(ovp=30.0, cutoff_v=-100.0)
        worker, backend, csv_path = _make_worker(profile)
        telemetry, alarms, states, finished = _collect_signals(worker)

        backend.step.side_effect = [(12.0, 1.0), (31.0, 1.0), (12.0, 1.0)]

        worker.run()

        # OVP sample itself still gets logged; loop halts before a 3rd sample.
        self.assertEqual(len(telemetry), 2)
        self.assertTrue(any(sev == "CRITICAL" and "Over-voltage" in msg for sev, msg in alarms))
        self.assertIn("ESTOP", states)
        self.assertNotIn("STOPPED", states)  # estop path skips the final STOPPED emit
        backend.emergency_zero.assert_called_once()
        self.assertEqual(len(finished), 1)


class TestPauseResume(unittest.TestCase):
    def test_pause_halts_sampling_and_resume_continues_it(self):
        profile = _make_profile(cutoff_v=-100.0)  # never trips naturally
        worker, backend, csv_path = _make_worker(profile, sample_hz=1000.0)
        telemetry, alarms, states, finished = _collect_signals(worker)
        backend.step.return_value = (12.0, 1.0)

        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            time.sleep(0.1)
            count_before_pause = len(telemetry)
            self.assertGreater(count_before_pause, 0)

            worker.pause(True)
            time.sleep(0.05)
            count_during_pause = len(telemetry)
            time.sleep(0.1)
            # Paused loop just busy-waits on QThread.msleep(40) — no new samples.
            self.assertEqual(len(telemetry), count_during_pause)
            self.assertIn("PAUSED", states)

            worker.pause(False)
            time.sleep(0.1)
            self.assertGreater(len(telemetry), count_during_pause)
            self.assertIn("RUNNING", states)
        finally:
            worker.stop()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
