"""SoC-sweep HPPC (G1/G2 fix).

HPPC used to pulse only once, at 100% SoC right after the mandatory full
charge. FreedomCAR's real HPPC profile sweeps pulse sets across SoC levels
(typically every 10%). Independent toggle: off preserves the exact
single-level behavior byte-for-byte (the outer level loop runs once).

The timing/stop-condition logic lives in module-level helpers
(soc_sweep_done / discharge_step_ah_target) precisely so it can be
unit-tested without driving the multi-hour sequence thread; a source-pattern
guard pins that the thread actually calls them (same pattern as
test_hppc_adaptive_relax.py's own guard).
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from aset_batt.ui.sequences.hppc import soc_sweep_done, discharge_step_ah_target

_HPPC_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences" / "hppc.py"


class TestSocSweepDone(unittest.TestCase):
    def test_above_floor_not_done(self):
        self.assertFalse(soc_sweep_done(45.0, 20.0))

    def test_at_or_below_floor_done(self):
        self.assertTrue(soc_sweep_done(20.0, 20.0))
        self.assertTrue(soc_sweep_done(15.0, 20.0))

    def test_negative_soc_clamped_by_caller_not_here(self):
        # soc_sweep_done is a pure comparison — it doesn't clamp inputs itself,
        # the estimator's own soc property already clamps to [0, 100].
        self.assertTrue(soc_sweep_done(-5.0, 20.0))


class TestDischargeStepAhTarget(unittest.TestCase):
    def test_10pct_of_50ah(self):
        self.assertAlmostEqual(discharge_step_ah_target(50.0, 10.0), 5.0)

    def test_zero_step_is_zero_ah(self):
        self.assertAlmostEqual(discharge_step_ah_target(50.0, 0.0), 0.0)

    def test_negative_inputs_clamped_to_zero(self):
        self.assertAlmostEqual(discharge_step_ah_target(-10.0, 10.0), 0.0)
        self.assertAlmostEqual(discharge_step_ah_target(50.0, -10.0), 0.0)


class TestSourcePatternWiresSocSweepIntoThread(unittest.TestCase):
    """Pin the thread actually using the helpers — the unit tests above are
    worthless if the sequence quietly goes back to a fixed single-level run."""

    def setUp(self):
        src = _HPPC_PY.read_text(encoding="utf-8")
        start = src.index("def _hppc_seq_thread")
        end = src.find("\n    def ", start + 1)
        self.hppc_src = src[start:end if end != -1 else len(src)]

    def test_outer_level_loop_wraps_inner_cycle_loop(self):
        level_idx = self.hppc_src.index("while self._seq_running.is_set():")
        cyc_idx = self.hppc_src.index("for cyc in range(1, n_cyc + 1):")
        self.assertGreater(cyc_idx, level_idx,
                           "the outer level loop must be declared BEFORE the "
                           "inner per-cycle loop it wraps")

    def test_single_level_path_preserved_when_disabled(self):
        self.assertIn('if not soc_sweep_enabled:', self.hppc_src)
        self.assertIn("break   # preserves the exact single-level behavior",
                      self.hppc_src)

    def test_tau_fit_reset_per_level(self):
        # appears at least twice: the initial pre-loop reset, and the
        # per-level reset after the re-anchor
        self.assertEqual(self.hppc_src.count("_tau_fit = 0.0"), 2)

    def test_reanchor_calls_calibrate_from_ocv_stable_with_no_bleed(self):
        reanchor_idx = self.hppc_src.index("Re-anchor after the step's rest")
        window = self.hppc_src[reanchor_idx:reanchor_idx + 1200]
        self.assertIn("calibrate_from_ocv_stable(", window)
        self.assertIn("allow_bleed_off=False", window)

    def test_stop_condition_uses_live_estimator_soc_not_ah_accumulator(self):
        self.assertIn('state_s["soc"] <= target_level_soc', self.hppc_src)
        # discharge_step_ah_target is exported for the ETA display only — it
        # must never be used as the runtime stop condition (see its own
        # docstring) — guard against that regressing silently.
        step_block_start = self.hppc_src.index("SoC-sweep DISCHARGE step")
        step_block_end = self.hppc_src.index("Re-anchor after the step's rest")
        self.assertNotIn("discharge_step_ah_target(",
                         self.hppc_src[step_block_start:step_block_end])

    def test_discharge_step_uvp_check_present(self):
        self.assertIn("Under-voltage during HPPC SoC-sweep discharge", self.hppc_src)

    def test_regen_composes_inside_the_wrapped_inner_loop(self):
        # Feature 1 (regen) must sit inside the inner "for cyc" loop so it
        # fires at every SoC level the sweep visits, not just once globally.
        cyc_idx = self.hppc_src.index("for cyc in range(1, n_cyc + 1):")
        fit_idx = self.hppc_src.index("Live per-cycle ECM fit failed")
        regen_idx = self.hppc_src.find("regen_enabled and", cyc_idx)
        if regen_idx != -1:
            self.assertGreater(regen_idx, cyc_idx)
            self.assertLess(regen_idx, self.hppc_src.index(
                "if not soc_sweep_enabled:"))


if __name__ == "__main__":
    unittest.main()
