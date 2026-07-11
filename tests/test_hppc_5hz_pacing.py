"""Regression test: HPPC relax/pulse legs are paced at ~5 Hz (0.2s), not the old
flat 1 Hz (1.0s) sleep.

identify_ecm_fit()'s own docstring assumes "30s pulse at 5Hz gives ~150
points... R1/C1 are well-resolved at 5Hz" — but the real HPPC relax/pulse loops
in sequences.py only sampled at 1 Hz, a systematic mismatch between what the
analysis was designed for and what data collection actually provided (fewer,
sparser points -> a worse-conditioned R1/C1 fit).

Fixed with the same pacing technique AutoController._monitor_loop already
uses: sleep only the time remaining after the SCPI round-trip (perf_counter-
timed), targeting a 0.2s period, instead of a flat 1.0s added on top of
whatever the read itself took.

The relax/pulse loops span multiple real wall-clock-gated phases (PREPARE's
OCV settle, a fixed 30-min CHARGE-adjacent rest, N pulse/relax cycles) that
are impractical to fast-forward cleanly through mocks without an fragile,
implementation-order-dependent time.time() side_effect chain — so this is
verified at the source level (the exact old/new patterns) rather than by
driving the full multi-hour thread. The loops' *content* (that each iteration
still calls _log_sample()/update_display()) is already covered by
tests/test_graph_feed_during_sequences.py's HPPC relax test, which is
unaffected by this change (it invokes the loop body statements directly, not
through _seq_sleep's timing).
"""
import re
import unittest

from pathlib import Path

_SEQUENCES_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences" / "hppc.py"


class TestHppcPacingSourcePattern(unittest.TestCase):
    def setUp(self):
        self.src = _SEQUENCES_PY.read_text(encoding="utf-8")
        # Isolate the HPPC full-sequence method so this test can't accidentally
        # match some unrelated 1.0s sleep elsewhere in the file.
        start = self.src.index("def _hppc_seq_thread")
        end = self.src.find("\n    def ", start + 1)
        if end == -1: end = len(self.src)
        self.hppc_src = self.src[start:end]

    def test_old_flat_1hz_sleep_is_gone_from_hppc_legs(self):
        self.assertNotIn("self._seq_sleep(1.0)", self.hppc_src,
                         "the old flat-1Hz relax/pulse sleep should have been replaced")

    def test_relax_and_pulse_legs_use_the_5hz_pacing_pattern(self):
        # Target period is now DEFAULT_SAMPLE_HZ (battery_model.py — shared with
        # worker.py's TestConfig.sample_hz) instead of a hardcoded "0.2" literal.
        matches = re.findall(
            r"_elapsed_iter = _t\.perf_counter\(\) - _iter_t0\s*\n\s*"
            r"if not self\._seq_sleep\(max\(0\.0, 1\.0 / DEFAULT_SAMPLE_HZ - _elapsed_iter\)\):",
            self.hppc_src,
        )
        self.assertEqual(len(matches), 2,
                         "expected the shared DEFAULT_SAMPLE_HZ pacing pattern in both the relax leg and the pulse leg")

    def test_iter_t0_is_captured_at_the_top_of_each_loop_iteration(self):
        # _iter_t0 must be (re)stamped every iteration, not once outside the loop,
        # or _elapsed_iter would measure cumulative time instead of per-sample time.
        self.assertEqual(self.hppc_src.count("_iter_t0 = _t.perf_counter()"), 2)


if __name__ == "__main__":
    unittest.main()
