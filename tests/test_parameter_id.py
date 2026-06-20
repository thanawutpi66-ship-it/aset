"""
Tests for the 1-RC Thevenin ECM parameter identifier.

Generate a synthetic current-pulse experiment with KNOWN R0/R1/C1 (plus sensor
noise) and confirm the identifier recovers them and reports a high R².
"""
import unittest

import numpy as np

from aset_batt.core.parameter_id import BatteryParameterIdentifier


def _synthetic_pulse(r0, r1, c1, current, voc, dt=0.1,
                     rest_s=5.0, pulse_s=200.0, noise_v=0.002, seed=0):
    """rest → constant-current discharge pulse → (data ends in pulse)."""
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(0, rest_s, dt)
    t_pulse = np.arange(0, pulse_s, dt)
    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)
    v_pulse = voc - current * (r0 + r1 * (1.0 - np.exp(-t_pulse / tau)))
    i_pulse = np.full_like(t_pulse, current)
    t = np.concatenate([t_rest, t_rest[-1] + dt + t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    return t, i, v


class TestParameterIdentifier(unittest.TestCase):
    def setUp(self):
        self.r0, self.r1, self.c1 = 0.012, 0.018, 2500.0    # τ = 45 s
        self.cur, self.voc = 8.0, 13.2
        self.t, self.i, self.v = _synthetic_pulse(
            self.r0, self.r1, self.c1, self.cur, self.voc)
        self.ident = BatteryParameterIdentifier(smooth_window=5)

    def test_recovers_known_parameters(self):
        res = self.ident.fit_model(self.t, self.i, self.v, self.voc)
        self.assertAlmostEqual(res["R0_ohm"], self.r0, delta=0.15 * self.r0)
        self.assertAlmostEqual(res["R1_ohm"], self.r1, delta=0.25 * self.r1)
        self.assertAlmostEqual(res["tau_s"], self.r1 * self.c1, delta=0.30 * self.r1 * self.c1)
        self.assertGreater(res["r_squared"], 0.95)
        self.assertLess(res["rmse_v"], 0.01)

    def test_result_keys_and_positivity(self):
        res = self.ident.fit_model(self.t, self.i, self.v, self.voc)
        for k in ("R0_ohm", "R1_ohm", "C1_farad", "tau_s", "rmse_v", "r_squared"):
            self.assertIn(k, res)
        self.assertGreater(res["R0_ohm"], 0)
        self.assertGreater(res["R1_ohm"], 0)
        self.assertGreater(res["C1_farad"], 0)

    def test_no_step_raises(self):
        flat_i = np.zeros_like(self.i)
        with self.assertRaises(ValueError):
            self.ident.fit_model(self.t, flat_i, self.v, self.voc)

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            self.ident.fit_model(self.t, self.i[:-3], self.v, self.voc)


if __name__ == "__main__":
    unittest.main()
