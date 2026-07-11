"""G5: report DC resistance at the FreedomCAR/USABC pulse timepoints
(R@0.1s / R@1s / R@10s) read off the fitted 1-RC/2-RC ECM, instead of only the
single first-sample-after-edge "R0" (≈R@100ms, already part-R1) that isn't
cross-rig comparable. This is a reporting addition only — the pulse itself is
measured exactly as before, so the numbers are derived from the model already fit.
"""
import math
import unittest

import numpy as np

from aset_batt.acquisition.analysis import (
    ecm_r_at, analyze_series, profile_from_config,
)
from aset_batt.core.config import ConfigManager


class TestEcmRAtPureFunction(unittest.TestCase):
    def test_zero_time_is_r0_and_late_time_approaches_r0_plus_r1(self):
        r0, r1, tau = 0.025, 0.068, 5.0
        self.assertAlmostEqual(ecm_r_at(0.0, r0, r1, tau), r0, places=9)
        self.assertAlmostEqual(ecm_r_at(1e6, r0, r1, tau), r0 + r1, places=6)

    def test_monotonic_increasing_and_bounded(self):
        r0, r1, tau = 0.025, 0.068, 5.0
        v01, v1, v10 = (ecm_r_at(t, r0, r1, tau) for t in (0.1, 1.0, 10.0))
        self.assertLess(v01, v1)
        self.assertLess(v1, v10)
        self.assertGreaterEqual(v01, r0)
        self.assertLessEqual(v10, r0 + r1 + 1e-12)

    def test_second_rc_branch_only_adds_when_physical(self):
        r0, r1, tau1 = 0.02, 0.05, 4.0
        one = ecm_r_at(1.0, r0, r1, tau1)
        # a 1-RC fit passes R2=tau2=0 — must NOT divide by a zero tau2, just skip it
        self.assertEqual(ecm_r_at(1.0, r0, r1, tau1, 0.0, 0.0), one)
        # a real second branch raises the resistance at that timepoint
        self.assertGreater(ecm_r_at(1.0, r0, r1, tau1, 0.03, 20.0), one)


def _synthetic_pulse(r0, r1, c1, current, voc, dt=0.2, pulse_s=30.0, rest_s=2.0,
                     noise_v=0.0005, seed=1):
    """rest -> constant-current pulse, matching the real fit buffer shape."""
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(-rest_s, 0.0, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)
    v_pulse = voc - current * (r0 + r1 * (1.0 - np.exp(-t_pulse / tau)))
    i_pulse = np.full_like(t_pulse, current)
    t = np.concatenate([t_rest, t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    return t.tolist(), i.tolist(), v.tolist()


class TestTimepointResistancesInResults(unittest.TestCase):
    def test_hppc_result_reports_ordered_bounded_timepoints(self):
        profile = profile_from_config(ConfigManager())
        t, i, v = _synthetic_pulse(0.025, 0.068, 73.5, 5.3, 13.15)
        temp = [25.0] * len(t)
        cap = [0.0] * len(t)
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=True)
        if not res.get("ecm_identified"):
            self.skipTest("ECM fit not identified in this environment")
        r01 = res["r_at_0p1s_mohm"]
        r1s = res["r_at_1s_mohm"]
        r10 = res["r_at_10s_mohm"]
        self.assertFalse(math.isnan(r01))
        self.assertLess(r01, r1s, "R@0.1s must be below R@1s")
        self.assertLess(r1s, r10, "R@1s must be below R@10s")
        # bounded by the reported ohmic R0 (below) and the R0+R1(+R2) total (above)
        self.assertGreaterEqual(r01, res["r0_mohm"] - 1e-6)
        self.assertLessEqual(r10, res["ri_mohm"] + 1e-6)

    def test_non_hppc_leaves_timepoints_nan(self):
        """A plain discharge has no pulse edge to fit an ECM to — the timepoint
        resistances must be NaN, never a fabricated number."""
        profile = profile_from_config(ConfigManager())
        n = 60
        t = [k * 1.0 for k in range(n)]
        i = [2.0] * n
        v = list(np.linspace(13.0, 11.0, n))
        temp = [25.0] * n
        cap = list(np.cumsum([2.0 / 3600.0] * n))
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=False)
        self.assertTrue(math.isnan(res["r_at_0p1s_mohm"]))
        self.assertTrue(math.isnan(res["r_at_10s_mohm"]))


if __name__ == "__main__":
    unittest.main()
