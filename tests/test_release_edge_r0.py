"""Regression proof for the RC-compensated R0 release-edge estimator."""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import identify_release_r0, _release_r0_is_consistent
from aset_batt.acquisition.models import BatteryProfile


def _profile():
    return BatteryProfile(
        name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.3, max_charge_v=14.4, cutoff_v=10.5,
        max_charge_a=10.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
        otp_warn=45.0, otp_crit=60.0, internal_r=0.03,
    )


class TestReleaseEdgeR0(unittest.TestCase):
    def test_recovers_ohmic_r0_from_a_sampled_release(self):
        """At 10 Hz the load-on reading contains RC polarisation; accounting for
        RC recovery at the release edge recovers the known 12 mΩ R0."""
        r0_true, r1_true, tau, current, voc = 0.012, 0.090, 3.5, 5.0, 13.0
        t = np.arange(0.0, 42.1, 0.1)
        i = np.where((t >= 5.0) & (t < 35.0), current, 0.0)
        v = np.full_like(t, voc)
        on = (t >= 5.0) & (t < 35.0)
        off = t >= 35.0
        v[on] = voc - current * (r0_true + r1_true *
                                  (1.0 - np.exp(-(t[on] - 5.0) / tau)))
        # The first logged rest sample is captured one acquisition interval
        # after the load-off command, matching the release-edge estimator's
        # explicit ``release_dt`` correction.
        v[off] = voc - current * r1_true * (1.0 - np.exp(-30.0 / tau)) * \
                 np.exp(-(t[off] - 34.9) / tau)
        # Simulate the old CSV's 0.3 s gap at the load-on edge.  That is the
        # only situation in which release correction should replace a clean
        # 10 Hz load-on ECM intercept.
        keep = ~((t >= 5.0) & (t < 5.2))
        t, i, v = t[keep], i[keep], v[keep]
        r0, std, n = identify_release_r0(t, i, v, np.full_like(t, 25.0),
                                         _profile(), r1_true, tau)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(r0, r0_true, delta=0.0002)
        self.assertAlmostEqual(std, 0.0, delta=1e-12)

    def test_rejects_long_capacity_discharge_as_r0_evidence(self):
        t = np.arange(0.0, 220.1, 0.1)
        i = np.where((t >= 5.0) & (t < 205.0), 5.0, 0.0)
        v = np.full_like(t, 13.0)
        r0, _, n = identify_release_r0(t, i, v, np.full_like(t, 25.0),
                                       _profile(), 0.09, 3.5)
        self.assertEqual(n, 0)
        self.assertTrue(np.isnan(r0))

    def test_does_not_replace_load_on_fit_with_contradictory_release_value(self):
        self.assertTrue(_release_r0_is_consistent(0.012, 0.025))
        self.assertFalse(_release_r0_is_consistent(0.148, 0.100))


if __name__ == "__main__":
    unittest.main()
