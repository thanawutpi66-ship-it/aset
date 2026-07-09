"""Regression test: _vi_levels() must reject a "level" whose voltage spans a real
SoC-decline trajectory, not a genuine steady-state plateau.

Root cause of the original bug report: a long, continuous single-rate discharge
(IEC capacity test) only ever shows current at two values (0 = rest, I = discharge),
so it looks exactly like a valid 2-level HPPC-style record to _vi_levels() -- but
the "loaded" level's voltage sweeps volts (from near-full down toward cut-off) as
the pack actually discharges, not the tens-of-mV settle of a real short plateau.
Taking the median of that whole trajectory and fitting a V-I slope against the
rest level mixed real IR-drop with SoC-dependent OCV decline, inflating
dcir_slope_mohm by an order of magnitude (a real case: 400 mOhm reported for a
pack whose own ECM fit -- and the same physical unit's harness-corrected R0 --
implied roughly 90-100 mOhm).
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import _vi_levels, dcir_from_vi_slope


class TestViLevelsRejectsSocDrift(unittest.TestCase):
    def test_long_continuous_discharge_level_is_dropped(self):
        # Rest: tight (~0 A, ~13.15 V). Discharge: constant 2.65 A but voltage
        # sweeps 13.05V -> 10.49V over the whole record, like a real capacity test.
        n = 200
        ia = np.concatenate([np.zeros(20), np.full(n, 2.65)])
        va = np.concatenate([np.full(20, 13.15), np.linspace(13.05, 10.49, n)])
        levels = _vi_levels(ia, va)
        # Only the rest level should survive -- the loaded level's ~2.5 V spread
        # is nowhere near a real steady-state plateau.
        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0][0], 0.0)

    def test_genuine_hppc_style_plateau_is_kept(self):
        # A real short pulse: current steady, voltage settles within ~20 mV.
        n = 30
        ia = np.concatenate([np.zeros(10), np.full(n, 5.0)])
        va = np.concatenate([np.full(10, 13.10),
                             np.linspace(12.60, 12.58, n)])   # 20 mV spread only
        levels = _vi_levels(ia, va)
        self.assertEqual(len(levels), 2)

    def test_end_to_end_slope_dcir_not_inflated_by_soc_drift(self):
        n = 200
        ia = np.concatenate([np.zeros(20), np.full(n, 2.65)])
        va = np.concatenate([np.full(20, 13.15), np.linspace(13.05, 10.49, n)])
        levels = _vi_levels(ia, va)
        r, r2 = dcir_from_vi_slope([p[0] for p in levels], [p[1] for p in levels])
        # Fewer than 2 distinct current levels survived -> not computable, not a
        # wildly inflated number presented as if it were a real measurement.
        self.assertTrue(np.isnan(r))


if __name__ == "__main__":
    unittest.main()
