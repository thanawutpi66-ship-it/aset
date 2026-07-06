"""Regression test for the dual-estimator-feed bug surviving into the CHARGE
phase of every ISA-101 sequence (AUTO/HPPC/Cycle Life).

_seq_common_start() stops the background monitor at sequence start, but
AutoController.start_charge() unconditionally restarts it
(``if not self.monitor_running: self.start_monitor()``) — necessary for a
standalone CHARGE run outside of a sequence (it's the only live-telemetry
feed in that case), but inside a sequence the REST/PULSE/DISCHARGE phases
feed the estimator directly themselves, so a monitor left running after
CHARGE double-feeds coulomb counting for the rest of the run.

Confirmed on a real ~3.75h HPPC test (test_20260706_185655.csv): ~5-10% of
CSV rows shared an identical (rounded, 1-decimal) timestamp with the next
row while reporting a DIFFERENT SoC — proof of two independent
estimator.update() calls landing back-to-back, present from early CHARGE
all the way through the actual HPPC pulses.
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.app.auto_controller import AutoController
from aset_batt.ui.sequences import SequencesMixin


class _FakeHW:
    is_connected = True
    current_temp = 25.0


class _FakeConfig:
    class battery:
        rated_capacity = 5.3
    class system:
        safety_limits = {"max_temperature": 60.0, "min_temperature": -10.0,
                         "max_current": 30.0, "max_voltage": 15.0, "min_voltage": 10.0}


class _FakeData:
    def log_row(self, *a, **kw):
        pass
    def start_logging(self, *a, **kw):
        return True, "ok"
    is_recording = False


class Stub:
    _seq_stop_monitor_after_charge = SequencesMixin._seq_stop_monitor_after_charge


class TestSeqStopMonitorAfterCharge(unittest.TestCase):
    def _controller(self):
        estimator = StateEstimator(5.3, BatteryModel("LeadAcid", 2.0, 6, 1))
        return AutoController(root=None, hw=_FakeHW(), data=_FakeData(),
                              estimator=estimator, config=_FakeConfig())

    def test_stops_a_running_monitor(self):
        c = self._controller()
        c.monitor_running = True
        s = Stub()
        s.controller = c
        s._seq_stop_monitor_after_charge()
        self.assertFalse(c.monitor_running)

    def test_noop_when_already_stopped(self):
        """Must not error (or try to join a thread that was never started) when
        the monitor is already off — e.g. a standalone CHARGE was never run."""
        c = self._controller()
        c.monitor_running = False
        s = Stub()
        s.controller = c
        s._seq_stop_monitor_after_charge()   # should not raise
        self.assertFalse(c.monitor_running)

    def test_noop_when_no_controller_bound(self):
        s = Stub()
        s.controller = None
        s._seq_stop_monitor_after_charge()   # should not raise


if __name__ == "__main__":
    unittest.main()
