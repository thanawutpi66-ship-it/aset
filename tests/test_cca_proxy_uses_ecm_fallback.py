"""Regression test: CCA proxy must use the best available resistance measurement.

Root cause of the original bug report: _load_metrics() (which computes cca_est)
runs BEFORE the ECM fit in analyze_series() -- it has to, since its `ocv` output
feeds identify_ecm_fit() as an input. It was given the single-step `dcir` value
unconditionally, so whenever identify_dcir() failed to find a clean step (a real,
common outcome at the rig's achievable sample rate -- see
tests/test_hppc_5hz_pacing.py) and fell back to the profile's generic baseline
resistance, cca_est_a was computed from that generic fallback instead of the ECM
fit's actual measured R0+R1 -- even when the ECM fit succeeded with a good R^2.
For a genuinely healthy pack whose measured resistance is much lower than the
generic baseline, this silently under-reported CCA proxy (a real Feb-2026 field
report: 21 A reported for a battery whose own ECM fit implied ~28 A).
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series
from aset_batt.acquisition.models import BatteryProfile


def _profile():
    return BatteryProfile(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
        harness_r_ohm=0.0,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=0.125,
    )


def _synthetic_hppc_with_stale_first_sample(
        r0=0.010, r1=0.015, c1=2500.0, current=5.0, voc=13.2,
        dt=0.2, rest_s=5.0, pulse_s=200.0, edge_gap_s=0.6, noise_v=0.001, seed=0):
    """Rest -> pulse, with the first post-edge sample delayed by `edge_gap_s`
    (> analysis._DCIR_MAX_STEP_DT) so identify_dcir()'s single-step method drops
    the only candidate step as stale (n_steps=0, measured=False, falls back to
    the profile baseline) while identify_ecm_fit() -- which fits the whole
    curve, not just the first post-edge sample -- still succeeds normally."""
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(0.0, rest_s, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)
    v_pulse = voc - current * (r0 + r1 * (1.0 - np.exp(-t_pulse / tau)))
    i_pulse = np.full_like(t_pulse, current)
    t = np.concatenate([t_rest, t_rest[-1] + edge_gap_s + t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    temp = np.full(t.size, 25.0)
    cap = np.zeros(t.size)
    return t, i, v, temp, cap


class TestCcaProxyPrefersEcmOverStaleDcirFallback(unittest.TestCase):
    def setUp(self):
        self.r0, self.r1 = 0.010, 0.015
        self.t, self.i, self.v, self.temp, self.cap = \
            _synthetic_hppc_with_stale_first_sample(r0=self.r0, r1=self.r1)
        self.profile = _profile()

    def test_dcir_fallback_scenario_is_actually_reproduced(self):
        res = analyze_series(self.t, self.i, self.v, self.temp, self.cap,
                             self.profile, is_hppc=True)
        self.assertFalse(res["dcir_measured"])
        self.assertEqual(res["dcir_n_steps"], 0)

    def test_ecm_fit_succeeds_despite_the_stale_dcir_step(self):
        res = analyze_series(self.t, self.i, self.v, self.temp, self.cap,
                             self.profile, is_hppc=True)
        self.assertTrue(res["ecm_identified"])
        self.assertGreater(res["ecm_r2"], 0.9)

    def test_cca_proxy_uses_ecm_total_resistance_not_the_fallback_baseline(self):
        from aset_batt.acquisition.analysis import _cca_cutoff_v, _ocv_ceiling
        res = analyze_series(self.t, self.i, self.v, self.temp, self.cap,
                             self.profile, is_hppc=True)
        ri_ohm = res["ri_mohm"] / 1000.0
        cutoff = _cca_cutoff_v(self.profile)
        # ocv is surface-charge-clamped to the curve's own 100% point before the
        # CCA arithmetic (see _load_metrics) — mirror that here.
        ceil = _ocv_ceiling(self.profile, 25.0)
        ocv_eff = min(res["ocv_v"], ceil) if ceil else res["ocv_v"]
        expected_cca = (ocv_eff - cutoff) / ri_ohm
        self.assertAlmostEqual(res["cca_est_a"], expected_cca, places=3)

        # The bug: using the generic fallback baseline (much larger than the
        # true measured resistance here) instead would under-report CCA proxy.
        fallback_cca = (ocv_eff - cutoff) / self.profile.internal_r
        self.assertGreater(res["cca_est_a"], fallback_cca)


if __name__ == "__main__":
    unittest.main()
