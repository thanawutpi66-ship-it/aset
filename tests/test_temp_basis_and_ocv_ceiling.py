"""Regression tests for the temperature-basis and OCV-ceiling theory fixes.

Three related defects found by checking the pipeline against electrochemistry
first principles and two real session CSVs:

  1. LeadAcid Arrhenius Ea/R was 4000 K (4.5 %/degC at 25 degC) -- that's
     low-temperature charge-transfer territory, not the H2SO4 electrolyte
     conductivity (~0.17-0.20 eV -> Ea/R ~2000-2300 K) that dominates near room
     temp. A reading at just 30 degC was inflated x1.25 on normalization when
     ~x1.12 is physical. Corrected to 2200 K.
  2. Mixed normalization basis: the single-step DCIR has always been
     normalized to 25 degC, but the ECM R0/R1 never were -- the report showed
     both under one "norm. 25 degC" header and the DCIR-vs-fit cross-check
     compared a normalized number against a raw one (a built-in ~12% wedge at
     this bench's ~30 degC). ECM parameters now share the DCIR's basis; C1
     scales inversely so the fitted tau = R1*C1 is preserved.
  3. Surface-charge OCV leaked into derived metrics: a fresh-charged pack's
     rest median (13.18 V, above the curve's own 12.888 V 100% point) inflated
     the sag baseline and the CCA proxy by charge the pack cannot actually
     deliver. Derived metrics now clamp to the curve ceiling; the raw reading
     is still reported untouched.
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series, _ocv_ceiling
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core.battery_model import BatteryModel


def _profile(internal_r=0.113):
    return BatteryProfile(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
        harness_r_ohm=0.0,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=internal_r,
    )


def _hppc(r0, r1, c1, current=5.3, voc=12.80, temp=25.0, dt=0.2,
          rest_s=5.0, pulse_s=30.0, noise_v=0.0005, seed=0):
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(-rest_s, 0.0, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    v = np.concatenate([np.full_like(t_rest, voc),
                        voc - current * (r0 + r1 * (1.0 - np.exp(-t_pulse / tau)))])
    v = v + rng.normal(0, noise_v, v.size)
    i = np.concatenate([np.zeros_like(t_rest), np.full_like(t_pulse, current)])
    t = np.concatenate([t_rest, t_pulse]) + rest_s
    temp_arr = np.full(t.size, temp)
    cap = np.zeros(t.size)
    return t, i, v, temp_arr, cap


class TestArrheniusLiteratureBand(unittest.TestCase):
    def test_lead_acid_temp_sensitivity_is_electrolyte_not_charge_transfer(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        # 30 -> 25 degC normalization factor: physical band is ~1.10-1.15
        # (electrolyte conductivity), NOT the old 1.25 (charge-transfer Ea).
        factor = 1.0 / m.temp_rin_multiplier(30.0)
        self.assertGreater(factor, 1.08)
        self.assertLess(factor, 1.16)


class TestEcmSharesDcirNormalizationBasis(unittest.TestCase):
    def test_ecm_r0_r1_normalized_and_tau_preserved(self):
        r0_true, r1_true, c1_true = 0.025, 0.068, 73.5
        t, i, v, temp, cap = _hppc(r0_true, r1_true, c1_true, temp=30.0)
        prof = _profile()
        res = analyze_series(t, i, v, temp, cap, prof, is_hppc=True)
        self.assertTrue(res["ecm_identified"])
        self.assertTrue(res["ecm_temp_normalised"])
        mult = BatteryModel(prof.chemistry).temp_rin_multiplier(30.0)
        # R0/R1 land on the 25 degC basis (raw / mult), same as the DCIR path.
        self.assertAlmostEqual(res["r0_mohm"], r0_true / mult * 1000, delta=0.15 * r0_true * 1000)
        self.assertAlmostEqual(res["r1_mohm"], r1_true / mult * 1000, delta=0.25 * r1_true * 1000)
        # tau is a directly-fitted observable -- normalization must not move it.
        self.assertAlmostEqual(res["tau_s"], r1_true * c1_true, delta=0.3 * r1_true * c1_true)

    def test_dcir_vs_fit_cross_check_shares_the_basis(self):
        """Both sides normalized -> a clean synthetic pulse at 30 degC must NOT
        trip the 'DCIR disagrees with fit' warning that the old mixed basis
        manufactured out of thin air."""
        t, i, v, temp, cap = _hppc(0.025, 0.068, 73.5, temp=30.0)
        res = analyze_series(t, i, v, temp, cap, _profile(), is_hppc=True)
        self.assertFalse(any("disagrees with fit" in w for w in res["quality_warnings"]))


class TestOcvCeilingClamp(unittest.TestCase):
    def test_surface_charged_voc_is_clamped_out_of_cca_and_sag(self):
        # voc 13.30 V: physically impossible as a true rested OCV (curve tops out
        # ~12.9 V at 30 degC) -- classic fresh-off-charge surface charge.
        t, i, v, temp, cap = _hppc(0.025, 0.068, 73.5, voc=13.30, temp=30.0)
        prof = _profile()
        res = analyze_series(t, i, v, temp, cap, prof, is_hppc=True)
        ceil = _ocv_ceiling(prof, 30.0)
        self.assertIsNotNone(ceil)
        # raw reading still reported truthfully...
        self.assertGreater(res["ocv_v"], ceil)
        # ...but the CCA proxy budget uses the ceiling, not the inflated value.
        expected_cca = (ceil - 1.2 * prof.series) / (res["ri_mohm"] / 1000.0)
        self.assertAlmostEqual(res["cca_est_a"], expected_cca, delta=0.02 * expected_cca)

    def test_voc_divergence_warning_fires_on_inconsistent_rest_history(self):
        """Local pre-pulse rest far from the whole-record rest median (surface
        charge relaxing between them) must be surfaced -- R0 shifts by dV/I with
        the anchor choice (a real file showed a 2.2x spread)."""
        r0, r1, c1 = 0.025, 0.068, 73.5
        t1, i1, v1, temp1, cap1 = _hppc(r0, r1, c1, voc=13.30, temp=30.0)
        # append a long, much lower rest tail so the global median sits ~200 mV
        # below the local pre-pulse rest
        n_tail = 400
        t2 = np.arange(t1[-1] + 0.2, t1[-1] + 0.2 + n_tail * 0.2, 0.2)
        t = np.concatenate([t1, t2])
        i = np.concatenate([i1, np.zeros(n_tail)])
        v = np.concatenate([v1, np.full(n_tail, 13.05)])
        temp = np.full(t.size, 30.0)
        cap = np.zeros(t.size)
        res = analyze_series(t, i, v, temp, cap, _profile(), is_hppc=True)
        self.assertTrue(any("rest history inconsistent" in w
                            for w in res["quality_warnings"]))


class TestSocStartCorroboration(unittest.TestCase):
    def test_uncorroborated_full_claim_is_flagged(self):
        """The circular-trust hole: the logged SoC column claimed 100% (frozen
        estimator) while the pack's own rested head voltage said ~81% -- the
        'started full' gate passed and healthy capacity was reported as
        degradation. The head voltage is an independent witness; a big
        disagreement must be flagged."""
        prof = _profile()
        n_rest, n_dis = 25, 600
        v_rest = 12.66            # rested head: OCV curve says ~81%, NOT full
        t = np.arange(n_rest + n_dis) * 5.0
        i = np.concatenate([np.zeros(n_rest), np.full(n_dis, 2.65)])
        v = np.concatenate([np.full(n_rest, v_rest),
                            np.linspace(12.55, 10.45, n_dis)])   # reaches cutoff
        temp = np.full(t.size, 25.0)
        cap = np.cumsum(np.clip(i, 0, None)) * 5.0 / 3600.0
        res = analyze_series(t, i, v, temp, cap, prof, is_hppc=False,
                             soc_start=100.0)                    # the (false) claim
        self.assertTrue(any("not corroborated" in w for w in res["quality_warnings"]))

    def test_corroborated_full_claim_is_not_flagged(self):
        prof = _profile()
        n_rest, n_dis = 25, 600
        t = np.arange(n_rest + n_dis) * 5.0
        i = np.concatenate([np.zeros(n_rest), np.full(n_dis, 2.65)])
        v = np.concatenate([np.full(n_rest, 12.87),                # genuinely ~99%
                            np.linspace(12.70, 10.45, n_dis)])
        temp = np.full(t.size, 25.0)
        cap = np.cumsum(np.clip(i, 0, None)) * 5.0 / 3600.0
        res = analyze_series(t, i, v, temp, cap, prof, is_hppc=False,
                             soc_start=100.0)
        self.assertFalse(any("not corroborated" in w for w in res["quality_warnings"]))


class TestLiveFitFeedsEstimatorOn25CBasis(unittest.TestCase):
    def test_sequences_normalizes_before_update_ecm(self):
        """StateEstimator treats stored R0/R1 as 25 degC-basis (its live rin is
        (R0+R1)*temp_rin_multiplier) -- the HPPC live fit must divide by the
        multiplier before feeding, or displayed rin under-states by ~12% at
        this bench's ~30 degC."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "aset_batt" / "ui" / "sequences.py").read_text(encoding="utf-8")
        start = src.index("def _hppc_seq_thread")
        end = src.index("\n    def ", start + 1)
        hppc = src[start:end]
        self.assertIn("temp_rin_multiplier", hppc)
        # normalization must sit between the fit and the feed
        self.assertLess(hppc.index("identify_ecm_fit(_fit_t"),
                        hppc.index("temp_rin_multiplier"))
        self.assertLess(hppc.index("temp_rin_multiplier"),
                        hppc.index("estimator.update_ecm("))


if __name__ == "__main__":
    unittest.main()
