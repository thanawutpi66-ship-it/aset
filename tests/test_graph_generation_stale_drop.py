"""Regression test (ก.ค. 2026 — real-hardware-only "graph shows 2 overlapping
lines of the same color" report): the monitor loop calls
self.hw.read_vi() — a real, potentially slow SCPI round-trip — then only
re-checks self.monitor_running at the TOP of its next while-loop iteration
(auto_controller.py::_monitor_loop). If stop_monitor() is called while that
read is in flight (e.g. right after a sequence's CHARGE phase ends, or when
switching from Run Test to a Sequence), the loop still finishes its current
iteration and queues one more update_display() call via root.after() —
which can arrive on the GUI thread AFTER a brand-new run has already
cleared buf_t/buf_v and started appending its own fresh samples, landing a
stale (v, i, temp) reading right at the start of the new run's timeline
(elapsed is computed inside _slot_display, at consumption time, against
whatever self._elapsed_t0 is CURRENT then — so a stale sample lands near
elapsed≈0, exactly where the user reported seeing it).

Mock hardware's near-zero read latency almost never opens this window
(matches "only happens with real hardware, not simulation"), but the race
is present in the code regardless of hardware speed.

Fix: sig_display now carries a generation int, captured at the true
send-intent moment (synchronously for direct callers, explicitly passed
through root.after for the monitor loop — see update_display's and
_monitor_loop's own comments). _slot_display drops anything whose
generation doesn't match the window's CURRENT self._run_generation, which
every exclusive "start a run" entry point (_on_run_test, _seq_common_start,
first CHARACTERIZE test) bumps.
"""
import os
import threading
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


def _make_window():
    w = BatteryQtWindow(ConfigManager())
    w.estimator = MagicMock(soh=100.0, soc_std=None)
    return w


def test_stale_generation_sample_is_dropped():
    w = _make_window()
    try:
        w._run_generation = 5
        before_len = len(w.buf_t)
        # Simulate the monitor loop's straggling call: it captured gen=5
        # before being asked to stop, but by the time it actually runs a
        # NEW run (gen=6) has already started.
        w._run_generation = 6
        w._slot_display(12.0, 1.0, 50.0, 0.03, 25.0, 100.0, 5)
        assert len(w.buf_t) == before_len   # nothing appended — dropped as stale
    finally:
        w.close()


def test_current_generation_sample_is_kept():
    w = _make_window()
    try:
        w._run_generation = 3
        before_len = len(w.buf_t)
        w._slot_display(12.0, 1.0, 50.0, 0.03, 25.0, 100.0, 3)
        assert len(w.buf_t) == before_len + 1
        assert w.buf_v[-1] == 12.0
    finally:
        w.close()


def test_update_display_stamps_current_generation_by_default():
    """Synchronous callers (sequences/characterize) don't pass _gen — it
    should default to reading self._run_generation at call time, so their
    own samples are never mistakenly dropped as stale."""
    w = _make_window()
    try:
        w._run_generation = 7
        before_len = len(w.buf_t)
        w.update_display(12.0, 1.0, 50.0, 0.03, 25.0, 100.0)
        assert len(w.buf_t) == before_len + 1
    finally:
        w.close()


def test_update_display_honors_explicit_gen_for_deferred_callers():
    """The monitor loop path: it must capture gen BEFORE scheduling via
    root.after, and update_display must use that explicit value rather than
    re-reading self._run_generation (which may have already moved on by
    the time a root.after-deferred call actually executes)."""
    w = _make_window()
    try:
        w._run_generation = 10
        captured_gen = w._run_generation   # what the monitor loop would capture
        w._run_generation = 11             # a new run started before the deferred call fires
        before_len = len(w.buf_t)
        w.update_display(12.0, 1.0, 50.0, 0.03, 25.0, 100.0, _gen=captured_gen)
        assert len(w.buf_t) == before_len   # correctly recognized as stale and dropped
    finally:
        w.close()


def test_seq_common_start_bumps_generation():
    from aset_batt.ui.sequences.base import BaseSequenceMixin

    class Host(BaseSequenceMixin):
        def __init__(self):
            self.buf_t = []; self.buf_v = []; self.buf_i = []
            self.buf_soc = []; self.buf_rin = []; self.buf_temp = []
            self._elapsed_t0 = None
            self._run_generation = 2
            self.controller = MagicMock(monitor_running=False)
            self.lbl_phase_banner = MagicMock()
            self.cb_workflow_type = MagicMock()
            self.cb_workflow_type.currentText.return_value = "IEC 61960"
            self._seq_running = threading.Event()
            self.btn_seq_cancel = MagicMock()
            self.frm_seq_result = MagicMock()
            self.sig_phase_progress = MagicMock()
            self.sig_loading = MagicMock()
            self.sig_profile_status = MagicMock()

        def _seq_reset_step_leds(self):
            pass

    host = Host()
    host._seq_common_start("btn_auto_seq", "Running…")
    assert host._run_generation == 3


def test_on_run_test_bumps_generation():
    w = _make_window()
    try:
        w.config.hardware.psu_port = "COM1"
        w.config.hardware.load_port = "COM2"
        w.hw = MagicMock(is_connected=True)
        w.controller = MagicMock(is_charging=False, monitor_running=False)
        before = w._run_generation
        w.cb_psu.addItem("COM1"); w.cb_load.addItem("COM2")
        w.cb_psu.setCurrentIndex(w.cb_psu.count() - 1)
        w.cb_load.setCurrentIndex(w.cb_load.count() - 1)
        w._on_run_test()
        assert w._run_generation == before + 1
        if w._test_thread:
            w._test_worker.stop()
            w._test_thread.quit()
            w._test_thread.wait(2000)
    finally:
        w.close()


def test_char_guard_bumps_generation_only_when_nothing_else_running():
    w = _make_window()
    try:
        w.hw = MagicMock(is_connected=True)
        w.controller = MagicMock(monitor_running=False)
        w._test_thread = None
        w._seq_running = threading.Event()
        w._char_running = {}
        before = w._run_generation
        assert w._char_guard() is True
        assert w._run_generation == before + 1

        # A second CHARACTERIZE test joining an already-active one must NOT
        # bump again — that would invalidate the first test's own samples.
        w._char_running["pk"] = threading.Event()
        w._char_running["pk"].set()
        after_first = w._run_generation
        assert w._char_guard() is True
        assert w._run_generation == after_first
    finally:
        w.close()
