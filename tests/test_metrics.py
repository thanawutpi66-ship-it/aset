"""Unit tests for the accuracy-improvement helpers added to acquisition.analysis:
Peukert rate-normalisation, multi-current (V-I slope) DCIR, and MAD outlier rejection."""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import (
    peukert_capacity, dcir_from_vi_slope, _reject_outliers_mad,
)


class TestPeukert(unittest.TestCase):
    def test_no_change_at_reference_rate(self):
        # measured exactly at the reference rate (0.2C of 7 Ah = 1.4 A) → unchanged
        self.assertAlmostEqual(peukert_capacity(6.5, 1.4, 7.0, 1.2, 0.2), 6.5, places=6)

    def test_high_rate_lead_acid_corrected_up(self):
        # 5 Ah measured at 1C (7 A) on lead-acid (k=1.2) → normalised UP toward the rating
        c = peukert_capacity(5.0, 7.0, 7.0, 1.2, 0.2)
        self.assertGreater(c, 5.0)
        self.assertAlmostEqual(c, 5.0 * (7.0 / 1.4) ** 0.2, places=4)

    def test_lithium_barely_changes(self):
        c = peukert_capacity(5.0, 7.0, 7.0, 1.05, 0.2)
        self.assertLess(abs(c - 5.0), 0.5)            # k≈1 → small correction

    def test_guard_zero_current(self):
        self.assertEqual(peukert_capacity(5.0, 0.0, 7.0, 1.2), 5.0)


class TestViSlopeDcir(unittest.TestCase):
    def test_slope_recovers_resistance(self):
        # V = 12.6 - I*0.06  → R = 60 mΩ
        r, r2 = dcir_from_vi_slope([0, 5, 10], [12.6, 12.3, 12.0])
        self.assertAlmostEqual(r, 0.06, places=4)
        self.assertGreater(r2, 0.999)

    def test_needs_two_distinct_levels(self):
        r, r2 = dcir_from_vi_slope([5, 5, 5], [12.0, 12.0, 12.0])
        self.assertTrue(np.isnan(r))


class TestMadRejection(unittest.TestCase):
    def test_drops_single_outlier(self):
        out = _reject_outliers_mad(np.array([60.0, 61.0, 59.0, 60.0, 200.0]))
        self.assertNotIn(200.0, out.tolist())
        self.assertEqual(len(out), 4)

    def test_keeps_clean_data(self):
        x = np.array([60.0, 61.0, 59.0, 60.0])
        self.assertEqual(len(_reject_outliers_mad(x)), 4)


if __name__ == "__main__":
    unittest.main()
