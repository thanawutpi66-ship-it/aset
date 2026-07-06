"""Regression tests for harness (test-rig wiring/contact) resistance compensation.

Real-world trigger: a brand-new battery (independently measured at 12.2 mOhm on a
bench ACIR meter, at the battery terminals directly) came back graded REJECT from
an HPPC Full Sequence test — DCIR measured 77.4 mOhm, just over the REJECT
threshold (ratio > 2.5 vs the 30 mOhm pack baseline). The ~65 mOhm gap between the
ACIR reading and the rig's own DCIR is the rig's cabling/contact resistance, which
is purely ohmic and in series with everything the rig measures — it inflates every
reading by the same fixed amount regardless of the pack's real health, so a
genuinely healthy battery was being graded on its wiring, not itself.

harness_r_ohm (BatteryProfile) / harness_resistance_ohm (BatteryConfig) subtracts
a calibrated constant from the measured DCIR/R0 before grading. R1/C1/tau (the
cell's own RC relaxation) are deliberately NOT touched — wiring resistance is a
simple series resistor, not a charge-transfer/polarization effect.
"""
import numpy as np
import unittest

from aset_batt.acquisition.analysis import analyze_series
from aset_batt.acquisition.models import BatteryProfile


R0_TRUE = 0.010    # Ohm — the pack's real ohmic resistance
R1_TRUE = 0.006
C1_TRUE = 2500.0   # tau = 15 s
I_TRUE = 5.0
VOC_TRUE = 13.30   # 6S lead-acid rested


def _make_profile(harness_r_ohm: float = 0.0) -> BatteryProfile:
    return BatteryProfile(
        name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.3, max_charge_v=14.4, cutoff_v=10.5,
        max_charge_a=1.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
        otp_warn=45.0, otp_crit=60.0, internal_r=0.005 * 6,  # 30 mOhm pack baseline
        harness_r_ohm=harness_r_ohm,
    )


def _synthetic_hppc_pulse(r0_seen: float, r1=R1_TRUE, c1=C1_TRUE, current=I_TRUE,
                          voc=VOC_TRUE, dt=0.2, rest_s=20.0, pulse_s=30.0,
                          relax_s=30.0, noise_v=0.001, seed=1):
    """One HPPC-style rest -> pulse -> relax cycle. r0_seen is whatever ohmic step
    the rig actually measures (true battery R0, optionally inflated by a harness
    resistor in series — electrically indistinguishable from a bigger R0 at the
    fitting stage, which is exactly the real-world failure mode this guards
    against)."""
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(0.0, rest_s, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    t_relax = np.arange(0.0, relax_s, dt)

    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)

    v_pulse = voc - current * r0_seen - current * r1 * (1.0 - np.exp(-t_pulse / tau))
    i_pulse = np.full_like(t_pulse, current)

    v_relax_start = v_pulse[-1]
    vrc_at_release = current * r1 * (1.0 - np.exp(-pulse_s / tau))
    v_relax = voc - vrc_at_release * np.exp(-t_relax / tau)
    i_relax = np.zeros_like(t_relax)

    t = np.concatenate([t_rest, t_rest[-1] + dt + t_pulse,
                       t_rest[-1] + dt + t_pulse[-1] + dt + t_relax])
    v = np.concatenate([v_rest, v_pulse, v_relax]) + rng.normal(0.0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse, i_relax])
    cap = np.cumsum(np.clip(i, 0, None)) * dt / 3600.0
    temp = np.full(t.size, 25.0)
    return t, i, v, temp, cap


class TestHarnessResistanceSubtractedFromDCIRAndR0(unittest.TestCase):
    def test_uncorrected_r0_includes_harness(self):
        """Baseline: with harness_r_ohm=0 (old behaviour), the fitted R0 reflects
        the true battery R0 PLUS whatever harness resistance is baked into the
        synthetic pulse — proving the uncorrected measurement really is inflated."""
        harness = 0.065   # 65 mOhm, matching the real CSV's rig
        t, i, v, temp, cap = _synthetic_hppc_pulse(r0_seen=R0_TRUE + harness)
        profile = _make_profile(harness_r_ohm=0.0)
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=True)
        self.assertTrue(res["ecm_identified"])
        r0_ohm = res["r0_mohm"] / 1000.0
        self.assertAlmostEqual(r0_ohm, R0_TRUE + harness, delta=0.002)

    def test_corrected_r0_matches_true_battery_r0(self):
        """With harness_r_ohm set to the calibrated rig value, the reported R0
        comes back down to (approximately) the pack's real ohmic resistance."""
        harness = 0.065
        t, i, v, temp, cap = _synthetic_hppc_pulse(r0_seen=R0_TRUE + harness)
        profile = _make_profile(harness_r_ohm=harness)
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=True)
        self.assertTrue(res["ecm_identified"])
        r0_ohm = res["r0_mohm"] / 1000.0
        self.assertAlmostEqual(r0_ohm, R0_TRUE, delta=0.002)

    def test_r1_is_not_touched_by_harness_correction(self):
        """Harness resistance is pure ohmic (R0) — the cell's own charge-transfer
        resistance R1 must be identical with and without the correction."""
        harness = 0.065
        t, i, v, temp, cap = _synthetic_hppc_pulse(r0_seen=R0_TRUE + harness)
        res_raw = analyze_series(t, i, v, temp, cap, _make_profile(0.0), is_hppc=True)
        res_corrected = analyze_series(t, i, v, temp, cap, _make_profile(harness), is_hppc=True)
        self.assertAlmostEqual(res_raw["r1_mohm"], res_corrected["r1_mohm"], delta=0.5)

    def test_grade_improves_from_reject_to_a_with_correction(self):
        """The real-world trigger case: a genuinely healthy pack (small true R0,
        well within Grade-A resistance ratios) reads as REJECT when harness
        resistance is left uncorrected, and Grade A once it's compensated."""
        harness = 0.065
        t, i, v, temp, cap = _synthetic_hppc_pulse(r0_seen=R0_TRUE + harness)
        res_raw = analyze_series(t, i, v, temp, cap, _make_profile(0.0), is_hppc=True)
        res_corrected = analyze_series(t, i, v, temp, cap, _make_profile(harness), is_hppc=True)
        self.assertEqual(res_raw["grade"], "REJECT")
        self.assertEqual(res_corrected["grade"], "A")

    def test_default_harness_is_zero_no_behavior_change(self):
        """BatteryProfile.harness_r_ohm defaults to 0.0 — existing callers that
        don't know about this field see identical behaviour to before."""
        profile = BatteryProfile(
            name="t", chemistry="LeadAcid", nominal_v=12.0, series=6,
            capacity_ah=5.3, max_charge_v=14.4, cutoff_v=10.5,
            max_charge_a=1.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
            otp_warn=45.0, otp_crit=60.0,
        )
        self.assertEqual(profile.harness_r_ohm, 0.0)

    def test_profile_from_config_reads_harness_resistance_ohm(self):
        from aset_batt.acquisition.analysis import profile_from_config
        from aset_batt.core.config import ConfigManager
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = 0.042
        profile = profile_from_config(cfg)
        self.assertAlmostEqual(profile.harness_r_ohm, 0.042, places=6)


if __name__ == "__main__":
    unittest.main()
