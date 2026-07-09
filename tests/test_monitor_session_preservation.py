"""Regression test: start_monitor(reuse_session=True) must NOT clobber a
session a sequence already opened.

Root cause (finally explains three long-standing mysteries at once): every
sequence's PREPARE phase opens a labelled session (_ensure_logging(
label="HPPC") etc.) and logs the multi-minute OCV-settle rest into it — but
start_charge() -> start_monitor() then unconditionally created a NEW unlabelled
session file and reset _start_time, abandoning the labelled file mid-sequence.
Confirmed against a real test's artifacts (test_20260708_152502.csv):

  1. the file had NO "HPPC" label in its name (the labelled file was orphaned),
  2. it starts at t=0 with only ~3 rest rows before charge current flows,
     even though PREPARE genuinely waited minutes of OCV settle (logged to the
     abandoned file), and
  3. _quality_flags therefore flagged "no clear rest before load" on every
     sequence run — a systematic false positive with a real rest behind it.

Follow-up fix: the original reuse-if-already-recording guard alone caused a
SECOND regression — a manual "Start Charge" run left is_recording permanently
True (no manual Stop button ever calls stop_logging()), so a second unrelated
manual test silently appended into the first one's file. start_monitor() now
takes an explicit reuse_session flag: sequences pass True (this test's
scenario), while a plain manual start (reuse_session=False, the default)
force-closes any stale recording and always opens fresh — see
test_fresh_start_monitor_still_creates_a_session below, which now covers that
"stale recording left open" case too, not just the never-recorded-before one.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.hardware.mock_hardware import MockHardwareController


def _make_controller():
    cfg = ConfigManager()
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    return AutoController(None, hw, data, estimator, cfg), data


class TestStartMonitorPreservesOpenSession(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ctrl, self.data = _make_controller()

    def tearDown(self):
        self.ctrl.monitor_running = False
        self.data.stop_logging()
        for f in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, f))
            except OSError:
                pass
        os.rmdir(self.dir)

    def test_existing_labelled_session_survives_start_monitor(self):
        """The exact PREPARE -> start_charge(reuse_session=True) -> start_monitor
        flow (sequences.py's 3 CHARGE-phase call sites all pass reuse_session=True)."""
        labelled = os.path.join(self.dir, "test_HPPC_20260101_000000.csv")
        ok, _ = self.data.start_logging(labelled)
        self.assertTrue(ok)
        self.ctrl._start_time = time.time() - 300.0   # PREPARE began 5 min ago
        self.ctrl._start_mono = time.perf_counter() - 300.0

        with patch("threading.Thread"):               # don't spawn the real loop
            self.ctrl.start_monitor(reuse_session=True)

        self.assertEqual(self.data.current_path, labelled,
                         "start_monitor(reuse_session=True) replaced the sequence's open session")
        # elapsed continuity: the 5 minutes of PREPARE must still be on the clock
        self.assertGreater(time.perf_counter() - self.ctrl._start_mono, 299.0)

    def test_fresh_start_monitor_still_creates_a_session(self):
        """Manual Start Monitor (reuse_session=False, the default) with nothing
        recording keeps its old behaviour."""
        self.assertFalse(self.data.is_recording)
        with patch("threading.Thread"), \
             patch.object(DataHandler, "make_session_path",
                          return_value=os.path.join(self.dir, "test_fresh.csv")), \
             patch("aset_batt.storage.data_utils.write_session_metadata"):
            self.ctrl.start_monitor()
        self.assertTrue(self.data.is_recording)
        self.assertTrue(self.data.current_path.endswith("test_fresh.csv"))
        self.assertIsNotNone(self.ctrl._start_time)
        self.assertIsNotNone(self.ctrl._start_mono)

    def test_manual_restart_closes_stale_recording_and_opens_fresh(self):
        """The regression this follow-up fix closes: a first manual test leaves
        is_recording True forever (no Stop button calls stop_logging()) — a
        second manual start_monitor() (reuse_session=False, the default) must
        NOT silently append into the first test's file."""
        stale = os.path.join(self.dir, "test_stale_20260101_000000.csv")
        ok, _ = self.data.start_logging(stale)
        self.assertTrue(ok)
        self.ctrl._start_time = time.time() - 600.0
        self.ctrl._start_mono = time.perf_counter() - 600.0

        with patch("threading.Thread"), \
             patch.object(DataHandler, "make_session_path",
                          return_value=os.path.join(self.dir, "test_second.csv")), \
             patch("aset_batt.storage.data_utils.write_session_metadata"):
            self.ctrl.start_monitor()

        self.assertTrue(self.data.current_path.endswith("test_second.csv"),
                        "manual restart must open a fresh session, not reuse the stale one")
        # clock must NOT carry over from the stale session
        self.assertLess(time.perf_counter() - self.ctrl._start_mono, 5.0)


if __name__ == "__main__":
    unittest.main()
