"""Regression tests for the frozen-SoC fix: HPPC pulse/relax legs and the
Cycle Life discharge leg must feed estimator.update() per sample.

Root cause (found by cross-referencing two real session CSVs): neither leg
ever called estimator.update(), so coulomb counting froze for the whole
phase — a real HPPC test's 5 pulses removed 0.226 Ah (4.27% of rated) with
the logged SoC pinned at exactly 100.00 for all 2,864 s of the pulse phase
(while the pack's own relax-end OCV declined 13.34→13.15 V, physically
confirming the loss). The very next IEC capacity test then skipped its CHARGE
phase on a surface-charge OCV misread and measured that exact missing charge
as degradation: reported SoH 95.66% vs (100% − 4.27%) = 95.73% — an artifact
matching within 0.07% on a healthy pack.

Feeding update() here is safe now (and wasn't before): the shared monitor loop
is stopped for the whole sequence (no double counting), and the EKF's
uncalibrated-R0 runaway guard + universal step detector are in place.

Unit-level behaviour (SoC tracking under pulsed current with update() called)
is covered by the estimator's own tests; what THIS file locks in is the wiring
— that the sequence legs actually call it — via the same source-pattern
technique tests/test_hppc_5hz_pacing.py established for these multi-hour
thread bodies.
"""
import re
import unittest
from pathlib import Path

_SEQUENCES_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences.py"


def _method_src(src: str, name: str) -> str:
    start = src.index(f"def {name}")
    # The method may be the last one in the file (no following "def") —
    # fall back to end-of-file in that case.
    end = src.find("\n    def ", start + 1)
    return src[start:] if end == -1 else src[start:end]


class TestHppcLegsFeedEstimator(unittest.TestCase):
    def setUp(self):
        self.hppc = _method_src(_SEQUENCES_PY.read_text(encoding="utf-8"),
                                "_hppc_seq_thread")

    def test_relax_and_pulse_legs_both_call_estimator_update(self):
        calls = re.findall(r"self\.controller\.estimator\.update\(", self.hppc)
        self.assertGreaterEqual(len(calls), 2,
                                "expected estimator.update() in BOTH the relax leg "
                                "and the pulse leg of _hppc_seq_thread")

    def test_update_uses_a_real_dt_not_a_hardcoded_one(self):
        # dt must come from perf_counter deltas (one clock across both legs),
        # not a constant — a hardcoded dt corrupts the coulomb integral.
        self.assertIn("_upd_now - _upd_last", self.hppc)
        self.assertIn("_upd_last = _t.perf_counter()", self.hppc)

    def test_voc_for_fit_uses_the_relax_tail_median_not_one_sample(self):
        # Real relax-end voltages are still declining at the end of a practical
        # relax window (13.34→13.15 V across 5 cycles in a real test) — a single
        # last sample carries that residual-relaxation bias straight into the
        # per-cycle fit's R0.
        self.assertIn("sorted(_relax_tail_v)", self.hppc)
        self.assertNotIn("voc_for_fit = v_r\n", self.hppc)

    def test_pulse_rate_instrumentation_present(self):
        # A real rig achieved only ~0.7 Hz against the 5 Hz design with nothing
        # reporting it — the achieved rate must now be logged per pulse and
        # alarmed (once per sequence) when badly under target.
        self.assertIn("sampled at %.1f Hz", self.hppc)
        self.assertIn("_rate_warned", self.hppc)


class TestCycleLifeDischargeFeedsEstimator(unittest.TestCase):
    def test_discharge_loop_calls_estimator_update(self):
        src = _method_src(_SEQUENCES_PY.read_text(encoding="utf-8"),
                          "_cycle_life_thread")
        self.assertIn("self.controller.estimator.update(", src)


if __name__ == "__main__":
    unittest.main()
