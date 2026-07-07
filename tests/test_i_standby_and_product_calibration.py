"""Regression tests for two bugs found analysing a real HPPC test (a genuinely new,
unused YTZ6V battery graded REJECT):

1. ``_I_STANDBY`` in acquisition.analysis used to be 0.6 A (a leftover PSU-bleed
   compensation from before the SSR fully disconnected the load). On real rig data
   true rest reads 0.000 A, so the 0.6 A +/-0.15 A "rest" window instead caught
   LeadAcid bulk-charge current (~0.1C) and fed its elevated (absorption-stage)
   voltage into the ECM fit's OCV anchor, inflating the fitted R0.

2. A product's ``measured_params`` (battery_profiles.save_measured_params) can now
   carry an ``internal_r_ohm``/``r0_fraction`` calibration that overrides the
   chemistry-generic 60/40 R0/R1 split used by grade_from_ecm — needed because
   BatteryModel.base_rin never scales with rated_capacity_ah/cca_a, so a small pack
   can measure well above the chemistry-generic baseline while genuinely healthy.

3. ``harness_r_ohm`` (BatteryProfile) / ``harness_resistance_ohm`` (BatteryConfig):
   test-rig cabling/contact resistance, purely ohmic, subtracted from DCIR/
   dcir_slope/ECM-R0 (not R1/C1/tau) before grading.
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import _load_metrics, _quality_flags, analyze_series
from aset_batt.acquisition.analytics import Analytics
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core.battery_profiles import ProductProfile


def _profile(internal_r=0.03, r0_fraction=0.0, harness_r_ohm=0.0):
    return BatteryProfile(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
        harness_r_ohm=harness_r_ohm,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0,
        internal_r=internal_r, r0_fraction=r0_fraction,
    )


class TestIStandbyRestDetection(unittest.TestCase):
    def test_rest_ocv_ignores_bulk_charge_current(self):
        # 0.503 A charge current (bulk, ~0.1C of 5.3 Ah) followed by true rest (0.0 A)
        # at a LOWER voltage than the charge tail, then a discharge pulse.
        i = np.array([-0.503] * 5 + [0.0] * 10 + [5.0] * 5, dtype=float)
        v = np.array([14.38] * 5 + [13.34] * 10 + [12.9, 12.6, 12.5, 12.45, 12.4],
                     dtype=float)
        profile = _profile()
        sag, cca, ocv = _load_metrics(i, v, 0.08, profile)
        # OCV must come from the true 0.0 A rest (13.34 V), not the 14.38 V charge tail.
        self.assertAlmostEqual(ocv, 13.34, places=2)

    def test_no_clear_rest_warning_not_fooled_by_bulk_charge(self):
        # First 25 samples are bulk-charge current only (no genuine rest) — the
        # data-quality check must still flag "no clear rest before load".
        i = np.array([-0.503] * 25, dtype=float)
        v = np.linspace(13.0, 13.2, 25)
        temp = np.full(25, 25.0)
        profile = _profile()
        warnings, _ = _quality_flags(i, v, temp, profile, True, 5, True)
        self.assertTrue(any("no clear rest" in w for w in warnings))


class TestProductR0Fraction(unittest.TestCase):
    def test_default_split_used_when_r0_fraction_unset(self):
        profile = _profile(internal_r=0.03, r0_fraction=0.0)
        grade = Analytics.grade_from_ecm(float("nan"), 0.018, 0.012, profile)
        self.assertEqual(grade, "A")   # exactly at the chemistry-generic 60/40 boundary

    def test_product_specific_split_overrides_default(self):
        # A characterised specimen: R0=87.85 mOhm, R1=102.68 mOhm (real HPPC pulse-1
        # after the _I_STANDBY fix) — internal_r/r0_fraction derived so this exact
        # reading grades A (the golden-sample anchor).
        r0, r1 = 0.08784599598693836, 0.10267525178743742
        internal_r = r0 + r1
        r0_fraction = r0 / internal_r
        profile = _profile(internal_r=internal_r, r0_fraction=r0_fraction)
        self.assertEqual(Analytics.grade_from_ecm(float("nan"), r0, r1, profile), "A")

        # The SAME R0/R1 against the chemistry-generic 30 mOhm/60-40 baseline (no
        # product calibration) is REJECT — proves the override is what flips the grade.
        generic_profile = _profile(internal_r=0.03, r0_fraction=0.0)
        self.assertEqual(
            Analytics.grade_from_ecm(float("nan"), r0, r1, generic_profile), "REJECT")


class TestProductProfileAcceptsMeasuredParams(unittest.TestCase):
    def test_measured_params_kwarg_does_not_raise(self):
        # ProductProfile(**d) must not fail when the JSON entry carries a
        # measured_params dict (save_measured_params/get_measured_params) — it used to
        # raise TypeError and silently fall back to the built-in default profile,
        # losing any other JSON overrides (mass, CCA, ...) for that product.
        p = ProductProfile(
            name="x", chemistry="LeadAcid", nominal_voltage_per_cell=2.0,
            cells_series=6, cells_parallel=1, rated_capacity_ah=5.3,
            measured_params={"internal_r_ohm": 0.19, "r0_fraction": 0.46},
        )
        self.assertEqual(p.measured_params["internal_r_ohm"], 0.19)


def _synthetic_hppc_pulse(r0_true=0.010, r1=0.005, c1=3000.0, harness=0.0,
                          current=5.0, voc=13.0, dt=0.2, rest_s=30.0, pulse_s=30.0):
    """Rest -> discharge pulse from a 1-RC ECM, with an optional harness resistor
    (purely ohmic, added on top of r0_true) baked into the simulated voltage —
    mimicking a test rig's own wiring/contact resistance in series with the cell."""
    tau1 = r1 * c1
    t_rest = np.arange(0.0, rest_s, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)
    v_pulse = (voc - current * (r0_true + harness)
               - current * r1 * (1.0 - np.exp(-t_pulse / tau1)))
    i_pulse = np.full_like(t_pulse, current)
    t = np.concatenate([t_rest, t_rest[-1] + dt + t_pulse])
    v = np.concatenate([v_rest, v_pulse])
    i = np.concatenate([i_rest, i_pulse])
    temp = np.full_like(t, 25.0)
    cap = np.cumsum(np.clip(i, 0, None)) * dt / 3600.0
    return t, i, v, temp, cap


class TestHarnessResistanceCompensation(unittest.TestCase):
    R0_TRUE, R1_TRUE, HARNESS = 0.010, 0.005, 0.065

    def test_uncorrected_r0_includes_harness(self):
        t, i, v, temp, cap = _synthetic_hppc_pulse(
            self.R0_TRUE, self.R1_TRUE, harness=self.HARNESS)
        profile = _profile(internal_r=0.03, harness_r_ohm=0.0)   # uncalibrated
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=True)
        self.assertAlmostEqual(res["r0_mohm"] / 1000.0,
                               self.R0_TRUE + self.HARNESS, delta=0.002)

    def test_corrected_r0_recovers_true_value(self):
        t, i, v, temp, cap = _synthetic_hppc_pulse(
            self.R0_TRUE, self.R1_TRUE, harness=self.HARNESS)
        profile = _profile(internal_r=0.03, harness_r_ohm=self.HARNESS)
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=True)
        self.assertAlmostEqual(res["r0_mohm"] / 1000.0, self.R0_TRUE, delta=0.002)
        # R1 must be untouched by the harness correction (it isn't ohmic).
        self.assertAlmostEqual(res["r1_mohm"] / 1000.0, self.R1_TRUE, delta=0.002)

    def test_harness_correction_can_flip_the_grade(self):
        t, i, v, temp, cap = _synthetic_hppc_pulse(
            self.R0_TRUE, self.R1_TRUE, harness=self.HARNESS)
        uncorrected = _profile(internal_r=0.03, harness_r_ohm=0.0)
        corrected = _profile(internal_r=0.03, harness_r_ohm=self.HARNESS)
        grade_before = analyze_series(t, i, v, temp, cap, uncorrected, is_hppc=True)["grade"]
        grade_after = analyze_series(t, i, v, temp, cap, corrected, is_hppc=True)["grade"]
        self.assertEqual(grade_before, "REJECT")
        self.assertEqual(grade_after, "A")

    def test_harness_defaults_to_zero_no_behaviour_change(self):
        profile = _profile()
        self.assertEqual(profile.harness_r_ohm, 0.0)


if __name__ == "__main__":
    unittest.main()
