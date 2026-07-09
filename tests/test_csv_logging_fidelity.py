"""Regression tests for DataHandler.log_row fidelity fixes, found by deep
inspection of two real session CSVs (a 4.8 h HPPC run and a 2 h IEC run):

  * Elapsed_s was quantised to 0.1 s (f"{elapsed_s:.1f}") — at the monitor
    loop's ~10 Hz that produced 5,988 duplicate timestamps in one real file,
    corrupting every dt-based consumer (identify_dcir's staleness gate, the
    ECM fit's time axis, replay dt=0 divisions). Now 1 ms resolution.
  * Timestamp column carried only HH:MM:SS — a 4-5 h session crossing
    midnight wraps 23:59→00:00 with no way to disambiguate the day. Now
    includes the date.
  * Long steady phases wrote thousands of rows whose every measured value was
    identical to the previous row (3,542 in one real file) — zero information,
    real disk/OneDrive churn. Now throttled, BUT only up to 0.25 s: the cap
    exists because identify_dcir's staleness gate (_DCIR_MAX_STEP_DT=0.5 s)
    measures the recorded gap from the last pre-edge row to the first
    post-edge row; a 1 s throttle would push that past the gate and get every
    real current-step edge dropped as stale. A changed row always writes
    immediately.
"""
import csv
import os
import tempfile
import unittest

from aset_batt.storage.data_utils import DataHandler


class _LoggingCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.csv")
        self.dh = DataHandler()
        ok, msg = self.dh.start_logging(self.path)
        self.assertTrue(ok, msg)

    def tearDown(self):
        self.dh.stop_logging()
        for f in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, f))
            except OSError:
                pass
        os.rmdir(self.dir)

    def _rows(self):
        self.dh.csv_file.flush()
        with open(self.path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))


class TestElapsedResolutionAndTimestampDate(_LoggingCase):
    def test_elapsed_keeps_millisecond_resolution(self):
        self.dh.log_row(1.234, 12.5, 1.0, 80.0, 60.0, 25.0)
        self.dh.log_row(1.334, 12.6, 1.1, 80.0, 60.0, 25.0)
        rows = self._rows()
        self.assertEqual(rows[0]["Elapsed_s"], "1.234")
        self.assertEqual(rows[1]["Elapsed_s"], "1.334")

    def test_timestamp_includes_the_date(self):
        self.dh.log_row(0.1, 12.5, 1.0, 80.0, 60.0, 25.0)
        ts = self._rows()[0]["Timestamp"]
        # "YYYY-MM-DD HH:MM:SS" — a bare "HH:MM:SS" has no dashes at all.
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestRedundantRowThrottle(_LoggingCase):
    def test_identical_rows_within_window_are_skipped(self):
        for k in range(5):
            self.dh.log_row(0.1 + 0.05 * k, 12.5, 1.0, 80.0, 60.0, 25.0)
        # 5 identical-value rows spanning 0.2s -> only the first should land.
        self.assertEqual(len(self._rows()), 1)

    def test_identical_row_writes_again_after_the_window(self):
        self.dh.log_row(0.1, 12.5, 1.0, 80.0, 60.0, 25.0)
        self.dh.log_row(0.4, 12.5, 1.0, 80.0, 60.0, 25.0)   # 0.3s later >= 0.25s
        self.assertEqual(len(self._rows()), 2)

    def test_any_changed_value_writes_immediately(self):
        """The current-step edge case the 0.25s cap protects: a changed value
        (here the current) must never be delayed by the throttle."""
        self.dh.log_row(0.10, 12.50, 0.0, 80.0, 60.0, 25.0)
        self.dh.log_row(0.20, 12.50, 0.0, 80.0, 60.0, 25.0)   # identical -> skipped
        self.dh.log_row(0.30, 12.35, 5.3, 80.0, 60.0, 25.0)   # the edge -> writes
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["Current_A"], "5.3000")
        self.assertEqual(rows[1]["Elapsed_s"], "0.300")

    def test_rin_calibrated_transition_writes_immediately(self):
        self.dh.log_row(0.10, 12.5, 1.0, 80.0, 60.0, 25.0, rin_calibrated=False)
        self.dh.log_row(0.15, 12.5, 1.0, 80.0, 60.0, 25.0, rin_calibrated=True)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["Rin_Calibrated"], "1")

    def test_throttle_resets_between_sessions(self):
        self.dh.log_row(0.10, 12.5, 1.0, 80.0, 60.0, 25.0)
        self.dh.stop_logging()
        path2 = os.path.join(self.dir, "t2.csv")
        ok, _ = self.dh.start_logging(path2)
        self.assertTrue(ok)
        # Same values, tiny elapsed — but a brand-new session's first row must
        # always write.
        self.dh.log_row(0.12, 12.5, 1.0, 80.0, 60.0, 25.0)
        self.dh.csv_file.flush()
        with open(path2, encoding="utf-8-sig") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)


if __name__ == "__main__":
    unittest.main()
