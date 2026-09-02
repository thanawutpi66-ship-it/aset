"""Regression test: the 2-RC ECM upgrade's second branch (tau2) had no
plausibility ceiling.

Real run sessions/test_QuickScan_20260717_145413.csv (FB YTZ6V 5.3Ah lead-acid,
sulfated pack -- discharge to cutoff in 45s at 1C) showed identify_ecm_fit()
upgrading a clean 1-RC pulse fit to a spurious 2-RC one: branch 1 stayed sane
but branch 2 fitted tau2=4247s (70 min!) on a ~30s pulse window, inflating
ri_total (R0+R1+R2) 70x (11,346 mOhm instead of ~160 mOhm) and tanking
cca_est_25c_a to 0.46A instead of ~30-35A.

_ECM_MAX_TAU_S (analysis.py) already existed as a ceiling on the 1-RC tau,
added for an earlier bug (tau=91366s on a plain discharge, no pulse at all)
-- but it was never applied to the 2-RC upgrade's tau2_s. Same failure mode,
one branch deeper: a slow SoC/OCV trend across the fit window can pose as a
second RC branch. Fix: reject the 2-RC upgrade when tau2 exceeds the ceiling
and fall back to the 1-RC result, which already passed its own R2/tau gates.
"""
import os
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series, analyze_csv, _ECM_MAX_TAU_S
from aset_batt.acquisition.models import BatteryProfile

_REAL_CSV = os.path.join(os.path.dirname(__file__), "..", "sessions",
                         "test_QuickScan_20260717_145413.csv")


def _make_profile(**overrides):
    kwargs = dict(
        name="FB YTZ6V (12V 5.3Ah VRLA AGM)", chemistry="LeadAcid",
        nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.7, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=5.3,
        ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0, internal_r=0.030,
        peukert_k=1.10, peukert_hr=10.0,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _sulfated_quick_scan_record(i_pulse=5.0, r0=0.030, r1=0.050, tau=4.5,
                                anchor=12.50, cutoff_v=10.15,
                                pulse_s=30.0, relax_s=90.0,
                                short_discharge_s=45.0, hz_fast=5.0):
    """rest -> clean 1-RC mini-pulse -> relax -> a SHORT, steep discharge to
    cutoff (mirrors a sulfated pack's real shape: capacity intact per OCV but
    can't sustain the discharge current -- voltage collapses in under a
    minute, not the many-minutes decline a healthy pack would show). The
    short discharge's near-linear collapse is exactly the "slow trend fits as
    a giant RC branch" shape that broke the 2-RC upgrade."""
    dt_f = 1.0 / hz_fast
    t, i, v = [], [], []
    tt = 0.0
    for _ in range(int(60 * hz_fast)):
        t.append(tt); i.append(0.0); v.append(anchor)
        tt += dt_f
    for k in range(int(pulse_s * hz_fast)):
        ts = k * dt_f
        vk = anchor - i_pulse * (r0 + r1 * (1.0 - np.exp(-ts / tau)))
        t.append(tt); i.append(i_pulse); v.append(vk)
        tt += dt_f
    v_after_pulse = v[-1]
    for k in range(int(relax_s * hz_fast)):
        ts = k * dt_f
        vk = v_after_pulse + (anchor - v_after_pulse) * (1.0 - np.exp(-ts / tau))
        t.append(tt); i.append(0.0); v.append(vk)
        tt += dt_f
    v_start = v[-1] - i_pulse * r0
    n_dis = int(short_discharge_s * hz_fast)
    for k in range(n_dis):
        frac = k / max(1, n_dis - 1)
        vk = v_start - frac * (v_start - cutoff_v)
        t.append(tt); i.append(i_pulse); v.append(vk)
        tt += dt_f
    n = len(t)
    q = np.cumsum(np.clip(np.asarray(i, float), 0, None)
                  * np.diff(np.asarray(t, float), prepend=0.0)) / 3600.0
    return (np.asarray(t), np.asarray(i), np.asarray(v), np.full(n, 25.0), q)


class TestTau2CeilingFallsBackTo1RC(unittest.TestCase):
    def test_ecm_model_stays_1rc_not_spurious_2rc(self):
        t, i, v, temp, q = _sulfated_quick_scan_record()
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             fit_ecm=True)
        self.assertTrue(res["ecm_identified"])
        self.assertEqual(res["ecm_model"], "1RC",
                         "a slow post-pulse trend must not upgrade to a "
                         "spurious 2-RC fit")

    def test_tau2_absent_or_zero_when_1rc_kept(self):
        t, i, v, temp, q = _sulfated_quick_scan_record()
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             fit_ecm=True)
        tau2 = res.get("tau2_s")
        self.assertTrue(tau2 is None or tau2 != tau2 or tau2 == 0.0,
                        f"tau2_s should be absent/NaN/0 for a 1-RC result, got {tau2}")

    def test_ri_total_not_inflated_by_giant_tau2_branch(self):
        t, i, v, temp, q = _sulfated_quick_scan_record()
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             fit_ecm=True)
        # R0+R1 for this synthetic pulse should be in the tens-of-mOhm range,
        # not thousands (the real bug inflated it 70x via a bogus R2 branch).
        self.assertLess(res["ri_mohm"], 500.0)

    def test_cca_proxy_not_collapsed_by_inflated_resistance(self):
        t, i, v, temp, q = _sulfated_quick_scan_record()
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             fit_ecm=True)
        self.assertGreater(res.get("cca_est_25c_a", 0.0), 5.0)


class TestReal2026_0717QuickScanCsv(unittest.TestCase):
    """The exact file that surfaced this bug."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_REAL_CSV):
            raise unittest.SkipTest("real Quick Scan CSV not present")

    def test_ecm_model_is_1rc_not_inflated_2rc(self):
        prof = _make_profile()
        res = analyze_csv(_REAL_CSV, prof, fit_ecm=True)
        self.assertEqual(res["ecm_model"], "1RC")

    def test_ri_total_sane_not_70x_inflated(self):
        prof = _make_profile()
        res = analyze_csv(_REAL_CSV, prof, fit_ecm=True)
        # Real ACIR bench measurement on this pack: 52.48 mOhm (AC, ohmic only).
        # The DC ECM fit legitimately reads higher (sulfation raises DC-branch
        # resistance more than AC), but nowhere near the pre-fix 11,346 mOhm.
        self.assertLess(res["ri_mohm"], 500.0)

    def test_cca_proxy_not_collapsed_to_near_zero(self):
        prof = _make_profile()
        res = analyze_csv(_REAL_CSV, prof, fit_ecm=True)
        self.assertGreater(res.get("cca_est_25c_a", 0.0), 5.0)


if __name__ == "__main__":
    unittest.main()
