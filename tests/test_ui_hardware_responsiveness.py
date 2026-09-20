"""Hardware UI responsiveness tests using delayed fakes only; no VISA or serial devices."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from aset_batt.core.config import ConfigManager
from aset_batt.ui import theme
from aset_batt.ui.isa101_views import BatteryQtWindow

theme.set_theme("light")
APP = QApplication.instance() or QApplication([])


class SlowHardware:
    def __init__(self, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.is_connected = False
        self.is_esp_connected = False
        self.current_temp = 25.0
        self.connect_calls = 0
        self.connect_thread_id = None
        self.serial_error = None
        self.esp_connect_error = ""
        self.ssr_commands = []
        self.disconnect_calls = 0
        self.active_reads = 0
        self.max_active_reads = 0
        self.read_calls = 0
        self.read_thread_id = None
        self._lock = threading.Lock()

    def apply_calibration(self, *args):
        pass

    def connect_instruments(self, psu, load):
        self.connect_calls += 1
        self.connect_thread_id = threading.get_ident()
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        self.is_connected = True

    def apply_default_safety_protection(self, **kwargs):
        return {"warnings": [], "info": {}}

    def disconnect_esp32(self):
        self.is_esp_connected = False

    def connect_esp32(self, port, baudrate=9600):
        if self.serial_error:
            self.esp_connect_error = self.serial_error
            raise RuntimeError(self.serial_error)
        self.is_esp_connected = True
        self.esp_connect_error = ""

    def set_ssr(self, state):
        self.ssr_commands.append(bool(state))
        return True

    def disconnect_instruments(self):
        self.disconnect_calls += 1
        self.is_connected = False

    def get_visa_ports(self):
        return []

    def get_com_ports(self):
        return []

    def read_vi(self):
        with self._lock:
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
            self.read_calls += 1
            self.read_thread_id = threading.get_ident()
        try:
            time.sleep(self.delay)
            return 12.4, 0.3, 0.0
        finally:
            with self._lock:
                self.active_reads -= 1

    def temp_is_stale(self):
        return False


def wait_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.003)
    APP.processEvents()
    return bool(predicate())


def watchdog_during(window, finish, interval_ms=10):
    """Measure timer lateness while a fake hardware task is active."""
    samples = []
    last = [time.perf_counter()]
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    def tick():
        now = time.perf_counter()
        samples.append(max(0.0, (now - last[0]) * 1000.0 - interval_ms))
        last[0] = now
    timer.timeout.connect(tick)
    timer.start()
    started = time.perf_counter()
    while not finish() and time.perf_counter() - started < 6.0:
        APP.processEvents()
        time.sleep(0.001)
    APP.processEvents()
    timer.stop()
    samples.sort()
    if not samples:
        return {"median_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    def pct(p):
        return samples[min(len(samples) - 1, int((len(samples) - 1) * p))]
    return {"median_ms": pct(0.5), "p95_ms": pct(0.95), "max_ms": samples[-1]}


class TestUiHardwareResponsiveness(unittest.TestCase):
    def make_window(self, hw):
        w = BatteryQtWindow(ConfigManager())
        w.hw = hw
        w.config.save_config = lambda: None
        w.cb_psu.addItem("PSU::MOCK")
        w.cb_load.addItem("LOAD::MOCK")
        w.cb_esp.addItem("COM-MOCK")
        w.btn_connect.setEnabled(True)
        return w

    def test_connect_callback_dispatches_and_returns_quickly(self):
        hw = SlowHardware(0.10)
        w = self.make_window(hw)
        try:
            started = time.perf_counter()
            w.btn_connect.click()
            callback_ms = (time.perf_counter() - started) * 1000
            self.assertLess(callback_ms, 50)
            self.assertTrue(wait_until(lambda: not w._hardware_connecting))
            self.assertEqual(hw.connect_calls, 1)
            self.assertNotEqual(hw.connect_thread_id, threading.get_ident())
        finally:
            w._hardware_pool.waitForDone(2000)
            w.close()

    def test_connect_simulated_delays_do_not_block_event_loop(self):
        rows = []
        for delay in (0.1, 0.5, 1.5):
            hw = SlowHardware(delay)
            w = self.make_window(hw)
            try:
                w._on_connect()
                latency = watchdog_during(w, lambda: not w._hardware_connecting)
                rows.append((delay, latency))
                self.assertTrue(wait_until(lambda: hw.is_connected))
                self.assertLess(latency["max_ms"], 100)
            finally:
                w._hardware_pool.waitForDone(3000)
                w.close()
        print("CONNECT_WATCHDOG", rows)

    def test_connect_failure_resets_state_and_cleans_up(self):
        hw = SlowHardware(0.02, "mock VISA timeout")
        w = self.make_window(hw)
        try:
            w._on_connect()
            self.assertTrue(wait_until(lambda: not w._hardware_connecting))
            self.assertEqual(hw.disconnect_calls, 1)
            self.assertFalse(hw.is_connected)
            self.assertIn("mock VISA timeout", hw.connect_error)
            self.assertTrue(w.btn_connect.isEnabled())
        finally:
            w._hardware_pool.waitForDone(2000)
            w.close()

    def test_serial_connect_failure_is_reported_without_stranding_in_connecting(self):
        hw = SlowHardware(0.01)
        hw.serial_error = "mock serial unavailable"
        w = self.make_window(hw)
        alarms = []
        w._log_alarm = alarms.append
        try:
            w.cb_esp.setCurrentText("COM-MOCK")
            w._on_connect()
            self.assertTrue(wait_until(lambda: not w._hardware_connecting))
            self.assertTrue(hw.is_connected)  # ESP32 is an existing non-fatal sub-link.
            self.assertIn("mock serial unavailable", hw.esp_connect_error)
            self.assertTrue(any("mock serial unavailable" in msg for msg in alarms))
        finally:
            w._hardware_pool.waitForDone(2000)
            w.close()

    def test_disconnect_is_worker_owned(self):
        hw = SlowHardware(0.02)
        hw.is_connected = True
        w = self.make_window(hw)
        try:
            w._on_disconnect()
            self.assertTrue(wait_until(lambda: not w._hardware_disconnecting))
            self.assertEqual(hw.disconnect_calls, 1)
            self.assertFalse(hw.is_connected)
        finally:
            w._hardware_pool.waitForDone(2000)
            w.close()

    def test_repeated_connect_disconnect_has_no_pool_or_timer_growth(self):
        hw = SlowHardware(0.005)
        w = self.make_window(hw)
        pool = w._hardware_pool
        timer_ids = tuple(sorted(id(timer) for timer in w.findChildren(QTimer)))
        try:
            for _ in range(3):
                w._on_connect()
                self.assertTrue(wait_until(lambda: not w._hardware_connecting))
                w._on_disconnect()
                self.assertTrue(wait_until(lambda: not w._hardware_disconnecting))
                self.assertEqual(pool.activeThreadCount(), 0)
                self.assertEqual(tuple(sorted(id(timer) for timer in w.findChildren(QTimer))), timer_ids)
            self.assertEqual(hw.connect_calls, 3)
            self.assertEqual(hw.disconnect_calls, 3)
        finally:
            pool.waitForDone(2000)
            w.close()

    def test_app_close_stops_timers_and_runs_shutdown_off_gui(self):
        hw = SlowHardware()
        w = self.make_window(hw)
        shutdown_threads = []
        class Controller:
            def stop_live_readback(self):
                pass
            def shutdown(self):
                shutdown_threads.append(threading.get_ident())
        w.controller = Controller()
        main_thread = threading.get_ident()
        try:
            w.close()
            self.assertTrue(wait_until(lambda: w._close_hardware_shutdown_done))
            self.assertTrue(all(not getattr(w, name).isActive()
                                for name in ("_tick", "_pulse_timer")))
            self.assertTrue(shutdown_threads)
            self.assertNotEqual(shutdown_threads[0], main_thread)
        finally:
            w._hardware_pool.waitForDone(2000)

    def test_direct_poll_delays_keep_event_loop_responsive_and_single_flight(self):
        rows = []
        for delay in (0.02, 0.2, 1.0):
            hw = SlowHardware(delay)
            hw.is_connected = True
            w = self.make_window(hw)
            try:
                w.rb_direct.setChecked(True)
                w._request_direct_poll()
                latency = watchdog_during(w, lambda: not w._direct_poll_inflight)
                rows.append((delay, latency))
                self.assertEqual(hw.max_active_reads, 1)
                self.assertNotEqual(hw.read_thread_id, threading.get_ident())
                self.assertLess(latency["max_ms"], 100)
                self.assertGreaterEqual(w._direct_poll_executed, 1)
                self.assertIsNotNone(w._direct_last_success_at)
            finally:
                w._hardware_pool.waitForDone(2500)
                w.close()
        print("DIRECT_WATCHDOG", rows)

    def test_first_direct_read_over_three_seconds_becomes_unavailable_then_recovers(self):
        hw = SlowHardware(4.5)
        hw.is_connected = True
        w = self.make_window(hw)
        try:
            w.rb_direct.setChecked(True)
            w._request_direct_poll()
            stale_deadline = time.monotonic() + 3.6
            while time.monotonic() < stale_deadline:
                APP.processEvents()
                w._on_heartbeat_tick()
                if "unavailable (>3 s" in w.status_label.text().lower():
                    break
                time.sleep(0.01)
            self.assertIn("unavailable (>3 s", w.status_label.text().lower())
            diag = w.direct_poll_diagnostics()
            self.assertTrue(diag["stale"])
            self.assertTrue(diag["in_flight"])
            self.assertEqual(diag["executed"], 0)
            self.assertTrue(wait_until(lambda: not w._direct_poll_inflight, timeout=2.0))
            self.assertIn("current", w.status_label.text().lower())
            self.assertFalse(w.direct_poll_diagnostics()["stale"])
            self.assertEqual(hw.max_active_reads, 1)
        finally:
            w._hardware_pool.waitForDone(6000)
            w.close()

    def test_slow_direct_requests_are_coalesced_not_queued(self):
        hw = SlowHardware(0.8)
        hw.is_connected = True
        w = self.make_window(hw)
        try:
            w.rb_direct.setChecked(True)
            timer = QTimer(w)
            timer.timeout.connect(w._request_direct_poll)
            timer.start(200)
            self.assertTrue(wait_until(lambda: w._direct_poll_executed >= 2, timeout=4.0))
            timer.stop()
            self.assertEqual(hw.max_active_reads, 1)
            self.assertGreater(w._direct_poll_skipped, 0)
            self.assertLessEqual(len(w._hardware_tasks), 1)
            self.assertIsNotNone(w._direct_last_sample)
            print("POLL_STRESS", {"requests": w._direct_poll_requests,
                                  "executed": w._direct_poll_executed,
                                  "skipped": w._direct_poll_skipped,
                                  "max_in_flight": hw.max_active_reads})
        finally:
            w._hardware_pool.waitForDone(2500)
            w.close()

    def test_estop_handler_remains_callable(self):
        hw = SlowHardware()
        w = self.make_window(hw)
        seen = []
        w._test_worker = None
        w.controller = type("Controller", (), {
            "_trigger_safety": lambda self, reason: seen.append(reason),
            "stop_charge": lambda self: None,
            "stop_monitor": lambda self: None,
            "stop_live_readback": lambda self: None,
        })()
        try:
            w._on_estop()
            self.assertTrue(seen and "E-STOP" in seen[0])
        finally:
            w.controller = None
            w.close()

    def test_estop_during_slow_direct_read_does_not_wait_for_poll_queue(self):
        hw = SlowHardware(1.2)
        hw.is_connected = True
        w = self.make_window(hw)
        seen = []
        w.controller = type("Controller", (), {
            "_trigger_safety": lambda self, reason: seen.append(reason),
            "stop_charge": lambda self: None,
            "stop_monitor": lambda self: None,
            "stop_live_readback": lambda self: None,
        })()
        w._test_worker = None
        try:
            w.rb_direct.setChecked(True)
            w._request_direct_poll()
            self.assertTrue(wait_until(lambda: hw.active_reads == 1, timeout=0.3))
            w._on_estop()
            self.assertEqual(hw.ssr_commands[-1], False)
            self.assertTrue(seen and "E-STOP" in seen[0])
            self.assertGreaterEqual(w._direct_poll_skipped, 0)
            self.assertLessEqual(len(w._hardware_tasks), 1)
            self.assertTrue(wait_until(lambda: not w._direct_poll_inflight, timeout=2.0))
            self.assertEqual(hw.max_active_reads, 1)
        finally:
            w.controller = None
            w._hardware_pool.waitForDone(2500)
            w.close()

    def test_close_during_slow_direct_read_defers_widget_destruction(self):
        hw = SlowHardware(0.4)
        hw.is_connected = True
        w = self.make_window(hw)
        shutdown_called = []
        class Controller:
            def stop_live_readback(self):
                pass
            def shutdown(self):
                shutdown_called.append(threading.get_ident())
        w.controller = Controller()
        try:
            w._request_direct_poll()
            self.assertTrue(wait_until(lambda: hw.active_reads == 1, timeout=0.3))
            w.close()  # ignored/deferred until the poll and async shutdown complete
            self.assertTrue(wait_until(lambda: w._close_hardware_shutdown_done, timeout=3.0))
            self.assertTrue(shutdown_called)
            self.assertFalse(w._tick.isActive())
            self.assertFalse(w._pulse_timer.isActive())
            self.assertFalse(w._hardware_tasks)
            self.assertEqual(w._hardware_pool.activeThreadCount(), 0)
        finally:
            w._hardware_pool.waitForDone(2000)

    def test_idle_event_loop_watchdog(self):
        w = self.make_window(SlowHardware())
        try:
            warm_deadline = time.monotonic() + 0.25
            while time.monotonic() < warm_deadline:
                APP.processEvents()
                time.sleep(0.003)
            done = []
            QTimer.singleShot(1000, lambda: done.append(True))
            latency = watchdog_during(w, lambda: bool(done))
            self.assertIsNotNone(latency)
            print("IDLE_WATCHDOG", latency)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
