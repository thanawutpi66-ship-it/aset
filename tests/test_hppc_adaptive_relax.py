"""Adaptive HPPC relax duration (G3 fix).

A relax leg shorter than ~3τ truncates the RC tail, biasing the next pulse's
fitted R1/C1/τ systematically low (the old code only WARNED about it and kept
the configured timing). From cycle 2 the relax leg now extends to ≥3× the
previous cycle's own fitted τ — per-unit, not the chemistry guess: lead-acid's
generic τ says 10-60 s but the real FB FTZ6V measured τ≈4.1-5.1 s across all
5 pulses of test_HPPC_20260708_152502 — extend-only (configured relax_s is a
floor), capped at 300 s, with a settle early-exit once the floor has elapsed.

The timing logic lives in module-level helpers (effective_relax_s /
relax_settled) precisely so it can be unit-tested without driving the
multi-hour sequence thread; a source-pattern guard pins that the thread
actually calls them (same pattern as test_hppc_live_ecm_feed.py's own guard).
"""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from aset_batt.ui.sequences.hppc import (
    effective_relax_s, relax_settled, _RELAX_CAP_S, _SETTLE_DV_V, _SETTLE_WIN_S,
)

_HPPC_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences" / "hppc.py"


class TestEffectiveRelax(unittest.TestCase):
    def test_cycle1_no_fit_uses_configured(self):
        self.assertEqual(effective_relax_s(30.0, 0.0), 30.0)

    def test_extends_to_3tau_when_fit_is_longer(self):
        # cycle-1 fit τ=45s → next relax 135s (> configured 30s)
        self.assertAlmostEqual(effective_relax_s(30.0, 45.0), 135.0)

    def test_never_shortens_below_configured(self):
        # real FB FTZ6V τ≈4.5s → 3τ=13.5s < configured 30s → stay at 30s
        self.assertEqual(effective_relax_s(30.0, 4.5), 30.0)

    def test_capped_at_300s(self):
        # pathological fit (τ=200s from a noisy pulse) must not stall the
        # sequence: 3×200=600s → capped
        self.assertEqual(effective_relax_s(30.0, 200.0), _RELAX_CAP_S)

    def test_configured_above_cap_is_also_capped(self):
        self.assertEqual(effective_relax_s(400.0, 0.0), _RELAX_CAP_S)


class TestRelaxSettled(unittest.TestCase):
    def _flat_window(self, t0, span, v=12.80, dt=1.0):
        import numpy as np
        return [(t0 + k * dt, v) for k in np.arange(0.0, span + 1e-9, dt)]

    def test_no_extension_never_settles_early(self):
        # relax_eff == relax_s → the early-exit must not shorten the
        # CONFIGURED relax, only the adaptive extension
        win = self._flat_window(30.0, 2 * _SETTLE_WIN_S)
        self.assertFalse(relax_settled(win, 50.0, 0.0, 50.0, 50.0))

    def test_before_configured_floor_never_settles(self):
        win = self._flat_window(0.0, 2 * _SETTLE_WIN_S)
        self.assertFalse(relax_settled(win, 20.0, 0.0, 30.0, 135.0))

    def test_flat_voltage_after_floor_settles(self):
        t_now = 55.0
        win = self._flat_window(t_now - 2 * _SETTLE_WIN_S, 2 * _SETTLE_WIN_S)
        self.assertTrue(relax_settled(win, t_now, 0.0, 30.0, 135.0))

    def test_still_relaxing_voltage_does_not_settle(self):
        # 2 mV of movement inside the newest window > _SETTLE_DV_V (1 mV)
        t_now = 55.0
        t0 = t_now - 2 * _SETTLE_WIN_S
        win = [(t0 + k, 12.80 + (0.002 if k >= _SETTLE_WIN_S else 0.0) * (k % 2))
               for k in range(int(2 * _SETTLE_WIN_S) + 1)]
        self.assertFalse(relax_settled(win, t_now, 0.0, 30.0, 135.0))

    def test_window_not_full_yet_does_not_settle(self):
        t_now = 35.0
        win = self._flat_window(t_now - 5.0, 5.0)   # only 5s of history
        self.assertFalse(relax_settled(win, t_now, 0.0, 30.0, 135.0))


class TestSourcePatternWiresAdaptiveRelaxIntoThread(unittest.TestCase):
    """Pin the thread actually using the helpers — the unit tests above are
    worthless if the sequence quietly goes back to a fixed t_phase."""

    def setUp(self):
        src = _HPPC_PY.read_text(encoding="utf-8")
        start = src.index("def _hppc_seq_thread")
        end = src.find("\n    def ", start + 1)
        self.hppc_src = src[start:end if end != -1 else len(src)]

    def test_relax_duration_comes_from_effective_relax_s(self):
        self.assertIn("relax_eff = effective_relax_s(relax_s, _tau_fit)",
                      self.hppc_src)
        self.assertIn("t_phase = _t_relax0 + relax_eff", self.hppc_src)

    def test_settle_early_exit_is_checked_after_rest_uvp(self):
        self.assertIn("relax_settled(", self.hppc_src)
        # settled-but-empty pack must abort, not pulse: UVP check first
        uvp_idx = self.hppc_src.index("Under-voltage during HPPC rest")
        settle_idx = self.hppc_src.index("relax_settled(")
        self.assertGreater(settle_idx, uvp_idx)

    def test_fitted_tau_feeds_next_cycle(self):
        # the per-cycle ECM fit must capture τ for the NEXT cycle's extension
        self.assertIn("_tau_fit = _tau_new", self.hppc_src)
        fit_idx = self.hppc_src.index("identify_ecm_fit(_fit_t, _fit_i, _fit_v")
        tau_idx = self.hppc_src.index("_tau_fit = _tau_new")
        self.assertGreater(tau_idx, fit_idx)


if __name__ == "__main__":
    unittest.main()
