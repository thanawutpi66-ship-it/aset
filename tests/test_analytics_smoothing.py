"""Edge-case tests for Analytics.gaussian_smooth/hampel_filter
(aset_batt/acquisition/analytics.py) — pure numpy, no mocking needed for the
short-array guards, but the scipy-vs-numpy fallback path in gaussian_smooth
is forced by monkeypatching scipy.ndimage.gaussian_filter1d to raise (same
"patch out an import to hit a fallback branch" pattern used elsewhere in this
suite, e.g. tests/test_prepare_phase_rest_logging.py's mocked calibrate_from_
ocv_stable to force an early PREPARE-phase exit).
"""
import unittest
from unittest.mock import patch

import numpy as np

from aset_batt.acquisition.analytics import Analytics


class TestGaussianSmoothShortArrays(unittest.TestCase):
    def test_empty_array_returned_unchanged(self):
        y = np.array([])
        out = Analytics.gaussian_smooth(y)
        self.assertEqual(out.size, 0)

    def test_single_element_returned_unchanged(self):
        y = np.array([5.0])
        out = Analytics.gaussian_smooth(y)
        np.testing.assert_array_equal(out, y)

    def test_two_elements_returned_unchanged(self):
        y = np.array([5.0, 7.0])
        out = Analytics.gaussian_smooth(y)
        np.testing.assert_array_equal(out, y)


class TestGaussianSmoothScipyFallback(unittest.TestCase):
    def test_scipy_path_smooths_a_step(self):
        y = np.concatenate([np.zeros(20), np.ones(20)])
        out = Analytics.gaussian_smooth(y, sigma=2.0)
        self.assertEqual(out.size, y.size)
        # A smoothed step should no longer contain the raw 0/1 discontinuity
        # right at the jump — some intermediate values must appear.
        self.assertTrue(np.any((out > 0.05) & (out < 0.95)))

    def test_falls_back_to_numpy_convolution_when_scipy_raises(self):
        y = np.concatenate([np.zeros(20), np.ones(20)])
        with patch("scipy.ndimage.gaussian_filter1d", side_effect=ImportError("no scipy")):
            out = Analytics.gaussian_smooth(y, sigma=2.0)
        self.assertEqual(out.size, y.size)  # mode="same" convolution
        self.assertTrue(np.any((out > 0.05) & (out < 0.95)))

    def test_fallback_and_scipy_paths_agree_closely(self):
        rng = np.random.default_rng(0)
        y = rng.normal(size=60)
        scipy_out = Analytics.gaussian_smooth(y, sigma=2.0)
        with patch("scipy.ndimage.gaussian_filter1d", side_effect=ImportError("no scipy")):
            fallback_out = Analytics.gaussian_smooth(y, sigma=2.0)
        # Not bit-identical (different edge handling), but should be close in
        # the interior where boundary effects don't dominate.
        interior = slice(10, -10)
        np.testing.assert_allclose(scipy_out[interior], fallback_out[interior], atol=0.15)


class TestHampelFilterShortArrays(unittest.TestCase):
    def test_array_shorter_than_window_returned_unchanged(self):
        x = np.array([1.0, 100.0, 1.0, 1.0, 1.0])  # size 5 < 2*7+1=15
        out = Analytics.hampel_filter(x, k=7)
        np.testing.assert_array_equal(out, x)

    def test_empty_array_returned_unchanged(self):
        out = Analytics.hampel_filter(np.array([]), k=7)
        self.assertEqual(out.size, 0)


class TestHampelFilterOutlierRejection(unittest.TestCase):
    def test_single_spike_replaced_neighbors_untouched(self):
        x = np.full(21, 10.0)
        x[10] = 500.0  # single large spike in the middle
        out = Analytics.hampel_filter(x, k=3, n_sigma=3.0)

        self.assertNotEqual(out[10], 500.0)
        self.assertAlmostEqual(out[10], 10.0)
        # Everything else must be untouched.
        untouched = np.delete(out, 10)
        np.testing.assert_array_equal(untouched, np.full(20, 10.0))

    def test_mad_zero_fallback_still_catches_isolated_spike(self):
        """All neighbours equal -> MAD=0 -> the 1%-of-median noise-floor
        fallback (see the function's own comment) must still fire for a
        clearly-anomalous point."""
        x = np.full(21, 10.0)
        x[10] = 10.0 + 5.0  # deviation (5.0) >> fallback threshold (~0.445)
        out = Analytics.hampel_filter(x, k=3, n_sigma=3.0)
        self.assertAlmostEqual(out[10], 10.0)

    def test_mad_zero_fallback_does_not_over_trigger_on_tiny_deviation(self):
        """A deviation smaller than the noise-floor threshold must survive —
        the fallback exists to catch spikes, not to flatten legitimate
        small variation once MAD happens to be zero in a given window."""
        x = np.full(21, 10.0)
        x[10] = 10.02  # deviation 0.02 << fallback threshold (~0.445)
        out = Analytics.hampel_filter(x, k=3, n_sigma=3.0)
        self.assertAlmostEqual(out[10], 10.02)

    def test_no_outliers_returns_input_unchanged(self):
        # A smooth, monotonic ramp — not random noise, which can legitimately
        # trip the MAD threshold on a handful of points by chance and would
        # make this test flaky rather than testing "no outliers present."
        x = 10.0 + 0.01 * np.arange(30)
        out = Analytics.hampel_filter(x, k=5, n_sigma=3.0)
        np.testing.assert_array_equal(out, x)


if __name__ == "__main__":
    unittest.main()
