"""Regen (charge) pulse leg in HPPC (G6 fix).

FreedomCAR's real HPPC profile is discharge-pulse -> rest -> regen(charge)-
pulse -> rest, but this sequence only ever pulsed discharge. Independent
toggle: off leaves the discharge-only behavior byte-for-byte unchanged.

The timing/current-magnitude logic lives in module-level helpers
(regen_pulse_current / regen_allowed) precisely so it can be unit-tested
without driving the multi-hour sequence thread; a source-pattern guard pins
that the thread actually calls them in the right place (same pattern as
test_hppc_adaptive_relax.py's own guard).
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from aset_batt.ui.sequences.hppc import regen_pulse_current, regen_allowed

_HPPC_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences" / "hppc.py"


class TestRegenPulseCurrent(unittest.TestCase):
    def test_75pct_of_discharge_pulse(self):
        self.assertAlmostEqual(regen_pulse_current(10.0), 7.5)

    def test_custom_fraction(self):
        self.assertAlmostEqual(regen_pulse_current(10.0, 0.5), 5.0)

    def test_negative_pulse_current_clamped_to_zero(self):
        self.assertEqual(regen_pulse_current(-5.0), 0.0)

    def test_negative_fraction_clamped_to_zero(self):
        self.assertEqual(regen_pulse_current(10.0, -0.5), 0.0)


class TestRegenAllowed(unittest.TestCase):
    def test_below_ceiling_allowed(self):
        self.assertTrue(regen_allowed(85.0, 90.0))

    def test_at_or_above_ceiling_blocked(self):
        self.assertFalse(regen_allowed(90.0, 90.0))
        self.assertFalse(regen_allowed(95.0, 90.0))


class TestSourcePatternWiresRegenIntoThread(unittest.TestCase):
    """Pin the thread actually using the helpers in the right place — the unit
    tests above are worthless if the sequence quietly stops calling them."""

    def setUp(self):
        src = _HPPC_PY.read_text(encoding="utf-8")
        start = src.index("def _hppc_seq_thread")
        end = src.find("\n    def ", start + 1)
        self.hppc_src = src[start:end if end != -1 else len(src)]

    def test_regen_gated_behind_opt(self):
        self.assertIn('opts.get("regen_enabled"', self.hppc_src)
        self.assertIn("if regen_enabled and self._seq_running.is_set():", self.hppc_src)

    def test_regen_current_computed_via_helper(self):
        self.assertIn("i_regen = regen_pulse_current(i_pulse)", self.hppc_src)

    def test_regen_gated_by_soc_ceiling_helper(self):
        self.assertIn("regen_allowed(soc_now,", self.hppc_src)

    def test_regen_uses_set_psu_not_set_charge(self):
        self.assertIn("self.hw.set_psu(True,", self.hppc_src)
        # set_charge() is confirmed dead code (zero callers anywhere in the
        # codebase) — it must never appear in the regen block.
        regen_idx = self.hppc_src.index("Regen (charge) pulse leg (G6)")
        next_leg_idx = self.hppc_src.index("if not self._seq_running.is_set():\n                    break\n                if not soc_sweep_enabled:")
        regen_block = self.hppc_src[regen_idx:next_leg_idx]
        self.assertNotIn("self.hw.set_charge(", regen_block)
        self.assertNotIn("set_psu_cccv(", regen_block)

    def test_regen_current_read_back_not_renegated(self):
        regen_idx = self.hppc_src.index("Regen (charge) pulse leg (G6)")
        window = self.hppc_src[regen_idx:regen_idx + 6000]
        self.assertIn("read_measurements(prefer_load_v=False)", window)
        self.assertNotIn("-i_rp", window)
        self.assertNotIn("-i_regen_read", window)

    def test_regen_ovp_check_logs_before_clearing_running_flag(self):
        regen_idx = self.hppc_src.index("Regen (charge) pulse leg (G6)")
        window = self.hppc_src[regen_idx:regen_idx + 8000]
        ovp_idx = window.index("Over-voltage during HPPC regen pulse")
        alarm_idx = window.index("sig_alarm.emit", ovp_idx)
        clear_idx = window.index("self._seq_running.clear()", ovp_idx - 400)
        self.assertLess(clear_idx, alarm_idx,
                        "self._seq_running.clear() should be set before the "
                        "sig_alarm.emit — matches the discharge pulse's own "
                        "UVP-abort ordering in this file")

    def test_regen_not_fed_into_live_ecm_update(self):
        # Guard against silently blending regen into the discharge pulse's
        # live/aggregated fit — see the decision documented in the plan: regen
        # gets its own post-hoc analysis via identify_hppc_pulses(), never
        # update_ecm().
        regen_idx = self.hppc_src.index("Regen (charge) pulse leg (G6)")
        next_leg_idx = self.hppc_src.index("if not self._seq_running.is_set():\n                    break\n                if not soc_sweep_enabled:")
        regen_block = self.hppc_src[regen_idx:next_leg_idx]
        # Check the actual call pattern, not the substring "update_ecm(" alone —
        # the regen block's own explanatory comment legitimately mentions the
        # function name to say NOT to call it.
        self.assertNotIn("self.controller.estimator.update_ecm(", regen_block)

    def test_regen_block_placed_after_discharge_live_fit(self):
        fit_idx = self.hppc_src.index("Live per-cycle ECM fit failed")
        regen_idx = self.hppc_src.index("Regen (charge) pulse leg (G6)")
        self.assertGreater(regen_idx, fit_idx)

    def test_teardown_covers_both_load_and_psu(self):
        # _seq_hw_safe_off() (base.py) already calls both load_off() and
        # psu_off() unconditionally in finally — confirm the regen leg doesn't
        # need (and doesn't add) its own duplicate cleanup path.
        import aset_batt.ui.sequences.base as base_mod
        base_src = Path(base_mod.__file__).read_text(encoding="utf-8")
        teardown_idx = base_src.index("_seq_hw_safe_off")
        window = base_src[teardown_idx:teardown_idx + 800]
        self.assertIn("load_off", window)
        self.assertIn("psu_off", window)


if __name__ == "__main__":
    unittest.main()
