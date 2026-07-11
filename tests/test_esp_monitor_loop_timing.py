"""Regression test: HardwareController._esp_monitor_loop() reports its own
per-iteration "working" percentage every _RATE_LOG_EVERY iterations.

Context: a real-rig HPPC run showed the acquisition worker's achieved rate
step down from ~4.8 Hz to a stable ~3.7 Hz partway through a long test, with
SCPI/estimator/log all unchanged in absolute cost — pointing at GIL
contention from a background thread. The live trend-graph redraw was ruled
out (0.4-2.9 ms, confirmed on the rig). The ESP32 monitor thread (a separate
daemon thread polling at 20 Hz for the whole app session) is the next
candidate; this instrumentation lets that be confirmed or ruled out with
real data instead of more guessing.
"""
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.hardware.hardware_driver import HardwareController


def _make_hw():
    hw = HardwareController.__new__(HardwareController)
    hw.is_esp_connected = True
    hw.current_temp = 25.0
    hw.last_esp_heartbeat = 0.0
    return hw


class TestEspMonitorLoopTiming(unittest.TestCase):
    def test_logs_working_percentage_every_rate_log_every_iterations(self):
        hw = _make_hw()
        calls = {"n": 0}

        hw.esp_serial = MagicMock()
        hw.esp_serial.in_waiting = 1
        hw.esp_serial.readline.side_effect = lambda: b"Object = 25.0*C\r\n"

        n_iterations = 205   # > _RATE_LOG_EVERY (200) so the log fires once

        # Shared fake clock: perf_counter() advances a small fixed step on
        # every call (matching the ~20 Hz nominal cadence over 205 iterations
        # once the intended sleep is folded in), so _span naturally exceeds
        # the 0.5s reporting threshold without a real multi-second test.
        clock = {"t": 0.0}
        def fake_perf_counter():
            clock["t"] += 0.001
            return clock["t"]

        def fake_sleep(_s):
            clock["t"] += 0.05   # simulate the real ~20 Hz poll interval
            calls["n"] += 1
            if calls["n"] >= n_iterations:
                hw.is_esp_connected = False

        with patch("aset_batt.hardware.hardware_driver.time.sleep", side_effect=fake_sleep), \
             patch("aset_batt.hardware.hardware_driver.time.perf_counter", side_effect=fake_perf_counter), \
             patch("aset_batt.hardware.hardware_driver.logger") as mock_logger:
            hw._esp_monitor_loop(callback=None)

        working_pct_calls = [
            c for c in mock_logger.info.call_args_list
            if c.args and "working" in c.args[0]
        ]
        self.assertEqual(len(working_pct_calls), 1,
                         "must log exactly once for 205 iterations at _RATE_LOG_EVERY=200")
        # args: (msg, pct, n_iterations, span)
        pct = working_pct_calls[0].args[1]
        self.assertGreaterEqual(pct, 0.0)
        self.assertLessEqual(pct, 100.0)


if __name__ == "__main__":
    unittest.main()
