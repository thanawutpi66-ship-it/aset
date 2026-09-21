"""Repeated same-window GUI lifecycle checks using mocked instrumentation."""
import csv
import gc
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer, Qt
from PySide6.QtWidgets import QApplication

from aset_batt.ui import theme
theme.set_theme("light")
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.ui.views import test_control
from aset_batt.acquisition.models import OperationMode
from aset_batt.acquisition.worker import AcquisitionWorker as RealWorker
from aset_batt.storage.data_utils import DataHandler

_APP = QApplication.instance() or QApplication([])


class _MockBackend:
    def __init__(self, hw):
        self.hw = hw
        self.worker = None
        self.samples = 0
        self.safe_shutdown_calls = 0

    def start_mode(self, _cfg):
        pass

    def step(self, _dt, _elapsed):
        self.samples += 1
        if self.samples >= 5:
            self.worker.stop()
        return 3.8, -1.0

    def temperature_measurement(self):
        return {"temperature_c": 25.0, "temperature_valid": True,
                "temperature_age_s": 0.0, "temperature_source": "mock",
                "temperature_status": "VALID"}

    def safe_shutdown(self):
        self.safe_shutdown_calls += 1

    def emergency_zero(self):
        pass


class TestRepeatedGuiLifecycle(unittest.TestCase):
    def test_fifty_same_window_cycles_do_not_accumulate_workers_signals_or_timers(self):
        with tempfile.TemporaryDirectory(prefix="aset_gui_soak_") as directory:
            window = BatteryQtWindow(ConfigManager())
            window.hw = SimpleNamespace(is_connected=True, last_voltage_source="mock",
                                        last_current_source="mock")
            window.controller = MagicMock(is_charging=False, monitor_running=False,
                                          safety_triggered=False)
            window.controller._monitor_thread = None
            window.controller._live_readback_thread = None
            window.estimator = None
            window._on_test_finished = lambda _results: None
            baseline_timers = len(window.findChildren(QTimer))
            # Let the one-shot startup update check settle before recording the
            # lifecycle baseline; it is unrelated to the 50 acquisition cycles.
            settle_until = time.monotonic() + 0.5
            while time.monotonic() < settle_until:
                _APP.processEvents()
                time.sleep(0.005)
            baseline_threads = threading.active_count()
            baseline_qthreads = sum(isinstance(obj, QThread) for obj in gc.get_objects())
            created_backends = []
            captured_sessions = []
            workers = []
            path_counter = [0]
            max_python_threads = baseline_threads

            def make_worker(backend, cfg, path, estimator=None):
                cfg.sample_hz = 1000.0
                worker = RealWorker(backend, cfg, path, estimator)
                backend.worker = worker
                worker._post_process = lambda *args, **kwargs: {"soh": None}
                emitted = [0]
                worker.telemetry.connect(lambda _row: emitted.__setitem__(0, emitted[0] + 1),
                                         Qt.DirectConnection)
                worker._audit_emitted_count = emitted
                captured_sessions.append(path)
                workers.append(worker)
                return worker

            def make_path(*_args, **_kwargs):
                path_counter[0] += 1
                return os.path.join(directory, f"cycle_{path_counter[0]:02d}.csv")

            def backend_factory(hw):
                backend = _MockBackend(hw)
                created_backends.append(backend)
                return backend

            try:
                with patch.object(test_control, "HardwareBackend", backend_factory), \
                     patch.object(test_control, "AcquisitionWorker", make_worker), \
                     patch.object(DataHandler, "make_session_path", side_effect=make_path):
                    for cycle in range(50):
                        generation = window._run_generation
                        window._on_run_test(OperationMode.CC_DISCHARGE)
                        thread = window._test_thread
                        self.assertIsNotNone(thread, f"cycle {cycle}: worker did not start")
                        deadline = time.monotonic() + 5.0
                        while thread.isRunning() and time.monotonic() < deadline:
                            _APP.processEvents()
                            time.sleep(0.001)
                        self.assertFalse(thread.isRunning(),
                                         f"cycle {cycle}: QThread did not finish")
                        # Deliver all queued telemetry and finished/cleanup slots.
                        for _ in range(5):
                            _APP.processEvents()
                            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                        gc.collect()
                        max_python_threads = max(max_python_threads, threading.active_count())
                        self.assertIsNone(window._test_thread, f"cycle {cycle}: QThread retained")
                        self.assertIsNone(window._test_worker, f"cycle {cycle}: worker retained")
                        self.assertEqual(window._run_generation, generation + 1)
                        self.assertEqual(workers[-1]._audit_emitted_count[0], 5)
                        self.assertEqual(created_backends[-1].samples, 5)
                        self.assertEqual(created_backends[-1].safe_shutdown_calls, 1)
                        self.assertEqual(len(window.buf_t), 5,
                                         f"cycle {cycle}: duplicate/missing GUI telemetry")
                        self.assertEqual(window._test_worker, None)
                        self.assertEqual(len(window.findChildren(QTimer)), baseline_timers)

                        path = captured_sessions[-1]
                        with open(path, encoding="utf-8-sig", newline="") as stream:
                            rows = list(csv.DictReader(stream))
                        self.assertEqual(len(rows), 5, f"cycle {cycle}: CSV session contaminated")
                        self.assertEqual(len({row["Session_ID"] for row in rows}), 1)
                        with open(path + ".meta.json", encoding="utf-8") as stream:
                            meta = json.load(stream)
                        self.assertEqual(meta["status"], "aborted")

                        # Repeated graph page/mode switches must not add crosshair
                        # signal connections or helper graphics items.
                        for idx in (0, 1, 2, 1, 0):
                            window.trend._on_mode_changed(idx)
                        self.assertEqual(len(window.trend._crosshair._wired), 6)
                        self.assertEqual(len(window.trend._crosshair._lines), 6)

                _APP.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                gc.collect()
                # One lazily-started global Qt pool thread may persist after the
                # first widget render; the count must settle and stay bounded.
                self.assertLessEqual(threading.active_count(), baseline_threads + 1)
                self.assertLessEqual(max_python_threads, baseline_threads + 1)
                self.assertLessEqual(
                    sum(isinstance(obj, QThread) for obj in gc.get_objects()),
                    baseline_qthreads + 1)
                self.assertEqual(len(window.findChildren(QTimer)), baseline_timers)
                self.assertEqual(len(captured_sessions), 50)
                self.assertEqual(len(set(captured_sessions)), 50)
            finally:
                window._shutdown_services()
                window.close()
                _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
