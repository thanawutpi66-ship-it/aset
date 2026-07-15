"""identify_dcir_at_timepoints() (aset_batt/acquisition/analysis.py) — additive
alongside identify_dcir(): reports R at fixed post-edge timepoints (FreedomCAR/
SAE J537-style R@0.1s/1s/10s) instead of a single "whatever the first post-edge
sample happened to catch" value. Synthetic data uses a known 1-RC decay so the
three timepoints are checked against a closed-form expected R, not just
"some number came out".
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import identify_dcir_at_timepoints
from aset_batt.acquisition.models import BatteryProfile


def _lead_acid_profile():
    return BatteryProfile(
        name="Test Lead-Acid 12V", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=7.0, max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.4,
        max_discharge_a=7.0, ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0,
        internal_r=0.03,
    )


def _synthetic_pulse(r0, r1, tau_s, i_pulse, dt=0.1, rest_n=5, pulse_s=15.0, v_rest=12.0):
    """Rest (I=0) then a clean current step held for pulse_s seconds, terminal
    voltage following V = v_rest - I*(R0 + R1*(1 - exp(-t/tau))) — the standard
    1-RC step response, discharge-positive convention."""
    t, i, v = [], [], []
    for k in range(rest_n):
        t.append(-(rest_n - k) * dt)
        i.append(0.0)
        v.append(v_rest)
    n_pulse = int(pulse_s / dt) + 1
    for k in range(n_pulse):
        tk = k * dt
        t.append(tk)
        i.append(i_pulse)
        v.append(v_rest - i_pulse * (r0 + r1 * (1.0 - np.exp(-tk / tau_s))))
    return (np.asarray(t, float), np.asarray(i, float), np.asarray(v, float),
            np.full(len(t), 25.0))


class TestDcirAtTimepoints(unittest.TestCase):
    def test_r_grows_with_timepoint_matching_rc_decay(self):
        profile = _lead_acid_profile()
        r0, r1, tau = 0.03, 0.02, 2.0
        t, i, v, temp = _synthetic_pulse(r0, r1, tau, i_pulse=5.0)

        out = identify_dcir_at_timepoints(i, v, temp, profile, time_s=t)

        self.assertEqual(set(out.keys()), {0.1, 1.0, 10.0})
        r01, r1s, r10 = out[0.1][0], out[1.0][0], out[10.0][0]
        # Monotonically increasing as more of the RC branch has charged.
        self.assertLess(r01, r1s)
        self.assertLess(r1s, r10)

        expected = lambda tk: r0 + r1 * (1.0 - np.exp(-tk / tau))
        self.assertAlmostEqual(r01, expected(0.1), places=4)
        self.assertAlmostEqual(r1s, expected(1.0), places=4)
        self.assertAlmostEqual(r10, expected(10.0), places=4)
        # R@10s should be close to the full R0+R1 (tau=2s, 10s = 5*tau).
        self.assertAlmostEqual(r10, r0 + r1, places=2)

    def test_short_pulse_omits_unreachable_timepoint(self):
        """A pulse shorter than a requested timepoint must not silently borrow
        a sample from whatever comes after it (e.g. the relax leg) — that
        timepoint should simply be absent."""
        profile = _lead_acid_profile()
        t, i, v, temp = _synthetic_pulse(0.03, 0.02, 2.0, i_pulse=5.0, pulse_s=2.0)

        out = identify_dcir_at_timepoints(i, v, temp, profile, time_s=t)

        self.assertIn(0.1, out)
        self.assertIn(1.0, out)
        self.assertNotIn(10.0, out)

    def test_no_time_s_returns_empty(self):
        profile = _lead_acid_profile()
        _, i, v, temp = _synthetic_pulse(0.03, 0.02, 2.0, i_pulse=5.0)
        out = identify_dcir_at_timepoints(i, v, temp, profile, time_s=None)
        self.assertEqual(out, {})

    def test_reference_temperature_is_a_no_op(self):
        """At exactly 25 C the Arrhenius multiplier is 1.0 (same normalizer as
        identify_dcir), so R@0.1s should equal the raw synthetic R0 unchanged."""
        profile = _lead_acid_profile()
        t, i, v, temp = _synthetic_pulse(0.04, 0.0, 2.0, i_pulse=5.0)  # r1=0 -> flat step
        out = identify_dcir_at_timepoints(i, v, temp, profile, time_s=t)
        self.assertAlmostEqual(out[0.1][0], 0.04, places=4)
        self.assertAlmostEqual(out[10.0][0], 0.04, places=4)


if __name__ == "__main__":
    unittest.main()
