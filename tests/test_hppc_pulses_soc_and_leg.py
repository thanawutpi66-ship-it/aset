"""Direct unit tests for identify_hppc_pulses()'s sign-mask fix and new
leg/soc_pct fields (G6 regen pulse + G1/G2 SoC-sweep support).

Before the fix, the edge detector was ``on = ia > thr`` — sign-aware, so a
regen (charge-direction, negative-current) pulse edge never crossed a
POSITIVE threshold and was invisible to this function regardless of any other
logic. Fixed to ``on = np.abs(ia) > thr``. These tests exercise the function
directly with synthetic arrays (no CSV, no hardware) since no dedicated
direct-unit-test file existed for it before.
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import identify_hppc_pulses
from aset_batt.acquisition.models import BatteryProfile


def _make_profile(**overrides):
    kwargs = dict(
        name="FB FTZ6V (12V 5.3Ah VRLA AGM)", chemistry="LeadAcid",
        nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.7, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=5.3,
        ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0, internal_r=0.030,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _rc_pulse_segment(anchor, i_pulse, r0, r1, tau, pulse_s, hz):
    """One rest-anchored 1-RC pulse response segment. i_pulse may be negative
    (regen/charge-direction) — the model already handles either sign, same as
    the live sequence's own fit (V = anchor − I·(R0+R1·(1−e^(−t/τ))))."""
    dt = 1.0 / hz
    t, i, v = [], [], []
    for k in range(int(pulse_s * hz)):
        ts = k * dt
        vk = anchor - i_pulse * (r0 + r1 * (1.0 - np.exp(-ts / tau)))
        t.append(ts); i.append(i_pulse); v.append(vk)
    return t, i, v


def _rest_segment(anchor, dur_s, hz):
    dt = 1.0 / hz
    n = int(dur_s * hz)
    return [anchor] * n, [0.0] * n


class TestDischargeOnlySeries(unittest.TestCase):
    def test_all_pulses_tagged_discharge_soc_nan_when_not_supplied(self):
        hz = 10.0
        anchor = 12.80
        t, i, v = [], [], []
        tt = 0.0
        for _ in range(3):
            rv, _ = _rest_segment(anchor, 60.0, hz)
            for x in rv:
                t.append(tt); i.append(0.0); v.append(x); tt += 1.0 / hz
            pt, pi, pv = _rc_pulse_segment(anchor, 5.3, 0.030, 0.060, 5.0, 30.0, hz)
            for x in pv:
                t.append(tt); i.append(5.3); v.append(x); tt += 1.0 / hz
        rv, _ = _rest_segment(anchor, 60.0, hz)
        for x in rv:
            t.append(tt); i.append(0.0); v.append(x); tt += 1.0 / hz
        n = len(t)
        temp = np.full(n, 25.0)

        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        self.assertEqual(len(pulses), 3)
        for p in pulses:
            self.assertEqual(p["leg"], "discharge")
            self.assertGreater(p["i_pulse_a"], 0)
            self.assertNotEqual(p["soc_pct"], p["soc_pct"])   # NaN


class TestDischargeAndRegenSeries(unittest.TestCase):
    def _build(self, hz=10.0, anchor=12.80):
        t, i, v = [], [], []
        tt = 0.0

        def _extend(vals, cur):
            nonlocal tt
            for x in vals:
                t.append(tt); i.append(cur); v.append(x); tt += 1.0 / hz

        rv, _ = _rest_segment(anchor, 60.0, hz)
        _extend(rv, 0.0)
        _, _, pv = _rc_pulse_segment(anchor, 5.3, 0.030, 0.060, 5.0, 30.0, hz)
        for x in pv:
            t.append(tt); i.append(5.3); v.append(x); tt += 1.0 / hz
        rv, _ = _rest_segment(anchor, 60.0, hz)
        _extend(rv, 0.0)
        _, _, pv = _rc_pulse_segment(anchor, -3.975, 0.030, 0.060, 5.0, 30.0, hz)
        for x in pv:
            t.append(tt); i.append(-3.975); v.append(x); tt += 1.0 / hz
        rv, _ = _rest_segment(anchor, 60.0, hz)
        _extend(rv, 0.0)
        n = len(t)
        return t, i, v, np.full(n, 25.0)

    def test_two_pulses_correct_legs_and_signs(self):
        t, i, v, temp = self._build()
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        self.assertEqual(len(pulses), 2,
                         "sign-mask fix: a regen (negative-current) pulse must "
                         "be detected — the old ia > thr mask made it invisible")
        p_dis, p_regen = pulses[0], pulses[1]
        self.assertEqual(p_dis["leg"], "discharge")
        self.assertGreater(p_dis["i_pulse_a"], 0)
        self.assertEqual(p_regen["leg"], "regen")
        self.assertLess(p_regen["i_pulse_a"], 0)
        self.assertAlmostEqual(p_regen["i_pulse_a"], -3.975, delta=0.05)

    def test_regen_pulse_fit_recovers_injected_r0(self):
        t, i, v, temp = self._build()
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        p_regen = pulses[1]
        self.assertAlmostEqual(p_regen["r0_fit_mohm"], 30.0, delta=8.0)


class TestSocPctThreading(unittest.TestCase):
    def test_soc_pct_median_matches_each_pulse_window(self):
        hz = 10.0
        anchor = 12.80
        t, i, v, soc = [], [], [], []
        tt = 0.0
        soc_levels = [90.0, 80.0, 70.0]
        for lvl in soc_levels:
            rv, _ = _rest_segment(anchor, 60.0, hz)
            for x in rv:
                t.append(tt); i.append(0.0); v.append(x); soc.append(lvl); tt += 1.0 / hz
            _, _, pv = _rc_pulse_segment(anchor, 5.3, 0.030, 0.060, 5.0, 30.0, hz)
            for x in pv:
                t.append(tt); i.append(5.3); v.append(x); soc.append(lvl); tt += 1.0 / hz
        rv, _ = _rest_segment(anchor, 60.0, hz)
        for x in rv:
            t.append(tt); i.append(0.0); v.append(x); soc.append(soc_levels[-1]); tt += 1.0 / hz
        n = len(t)
        temp = np.full(n, 25.0)

        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile(), soc_pct=soc)
        self.assertEqual(len(pulses), 3)
        for p, expected_lvl in zip(pulses, soc_levels):
            self.assertAlmostEqual(p["soc_pct"], expected_lvl, delta=0.5)

    def test_mismatched_length_soc_array_treated_as_not_supplied(self):
        hz = 10.0
        anchor = 12.80
        t, i, v = [], [], []
        tt = 0.0
        for _ in range(3):
            rv, _ = _rest_segment(anchor, 60.0, hz)
            for x in rv:
                t.append(tt); i.append(0.0); v.append(x); tt += 1.0 / hz
            _, _, pv = _rc_pulse_segment(anchor, 5.3, 0.030, 0.060, 5.0, 30.0, hz)
            for x in pv:
                t.append(tt); i.append(5.3); v.append(x); tt += 1.0 / hz
        n = len(t)
        temp = np.full(n, 25.0)
        # deliberately wrong length
        bad_soc = [50.0, 50.0, 50.0]

        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile(), soc_pct=bad_soc)
        self.assertEqual(len(pulses), 3)
        for p in pulses:
            self.assertNotEqual(p["soc_pct"], p["soc_pct"])   # NaN, not crashed


if __name__ == "__main__":
    unittest.main()
