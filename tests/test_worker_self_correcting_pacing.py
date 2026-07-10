"""Regression test: AcquisitionWorker.run()'s per-sample pacing must sleep
only the time REMAINING in the target period, not the full period on top of
whatever the real work (SCPI/estimator/log) already took.

Real-rig evidence (2026-07-10, manual "Run Test" HPPC via TEST MODE): the
per-substep breakdown showed SCPI 9-15%, estimator ~0%, log ~0%, "other"
84-91% — and the achieved rate (~3.6-4.2 Hz) was suspiciously close to
1/(period + small_work), not 1/period. The loop's OLD pacing called
QThread.msleep(int(period * 1000)) unconditionally every iteration — a flat
sleep regardless of how long the iteration's own work took, so real
per-iteration cost was ALWAYS work_time + period, capping the achieved rate
well under the 5 Hz target even when the real work was only 30-40ms. "other"
in the breakdown was this un-timed fixed sleep, not hidden overhead.
sequences/hppc.py's pulse loop already did this correctly (sleep only
max(0, period - elapsed)); worker.py's loop now matches it.
"""
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aset_batt.acquisition.models import BatteryProfile, TestConfig, OperationMode

_app = QApplication.instance() or QApplication([])


def _profile():
    return BatteryProfile("Test 12V", "Lead-Acid", 12.0, 6, 7.0,
                          14.4, 10.5, 1.4, 7.0, 15.0, 10.0, 45.0, 55.0, 0.03)


class _SlowStepBackend:
    """Every step() call itself blocks for `step_cost_s` — simulates a real
    SCPI round-trip that eats into the per-sample time budget. Records the
    perf_counter() timestamp of each call so the test can measure the
    SAMPLING LOOP's own duration directly, independent of whatever the
    worker does after the loop ends (_post_process() spins up a real
    ProcessPoolExecutor for analyze_series_mp — first-use subprocess startup
    overhead that has nothing to do with per-sample pacing but would
    otherwise swamp a naive "time the whole run() call" measurement)."""

    def __init__(self, n_samples: int, step_cost_s: float):
        self.n = n_samples
        self.k = 0
        self.step_cost_s = step_cost_s
        self.call_times = []

    def start_mode(self, cfg):
        pass

    def step(self, dt_since, elapsed):
        self.call_times.append(time.perf_counter())
        if self.k >= self.n:
            raise IndexError("sample budget exhausted — ends the worker loop")
        self.k += 1
        time.sleep(self.step_cost_s)
        return 12.5, -1.0   # arbitrary steady reading

    def read_temperature(self):
        return 25.0

    def safe_shutdown(self):
        pass

    def emergency_zero(self):
        pass


class TestSelfCorrectingPacing(unittest.TestCase):
    def test_iteration_cost_is_capped_at_the_target_period_not_work_plus_period(self):
        from aset_batt.acquisition.worker import AcquisitionWorker

        n_samples = 15
        step_cost = 0.05     # simulate a 50 ms SCPI round-trip
        cfg = TestConfig(_profile(), OperationMode.CC_DISCHARGE)
        cfg.sample_hz = 5.0  # period = 200 ms

        csv_path = os.path.join(tempfile.mkdtemp(), "pacing.csv")
        backend = _SlowStepBackend(n_samples, step_cost)
        w = AcquisitionWorker(backend=backend, cfg=cfg, csv_path=csv_path, estimator=None)
        w.run()

        # Measure only the sampling loop's own span: from the first step() call
        # to the (n_samples+1)th attempt that raises IndexError and ends the
        # loop — excludes _post_process()'s one-time subprocess-pool startup
        # entirely. n_samples intervals: one msleep still happens after the
        # last successful sample, before the loop even attempts call n+1.
        loop_span = backend.call_times[-1] - backend.call_times[0]
        n_intervals = n_samples

        period = 1.0 / cfg.sample_hz
        # OLD (broken) behavior: each interval costs (period + step_cost) ~= 250ms.
        # FIXED behavior: each interval costs max(period, step_cost) ~= 200ms.
        # The midpoint cleanly separates the two without being so loose either
        # regime would pass.
        broken_per_interval = period + step_cost
        fixed_per_interval = max(period, step_cost)
        threshold = n_intervals * (broken_per_interval + fixed_per_interval) / 2
        self.assertLess(loop_span, threshold,
                        f"pacing still adds work time on top of the full period "
                        f"(loop_span={loop_span:.2f}s over {n_intervals} intervals, "
                        f"broken~{n_intervals * broken_per_interval:.2f}s, "
                        f"fixed~{n_intervals * fixed_per_interval:.2f}s)")


if __name__ == "__main__":
    unittest.main()
