"""Regression test: Quick Scan's SoH is silently wrong when the discharge
doesn't start near 100% SoC.

Quick Scan has no charge phase by design (tests the battery as-received) --
but _calc_capacity_and_soh computed SoH as 100*cap_norm/rated_capacity
regardless of starting SoC, so a HEALTHY battery starting at 50% read as
~50% "SoH". A real run (sessions/test_QuickScan_20260717_145413.csv, started
at 68% SoC) headlined "State of Health 2.6%" and fed a REJECT grade off it,
even though a quality-warning already correctly flagged "SoH is
under-stated" -- the system knew, but still headlined a misleading number.

soh_est = soh / (soc_start/100) is a first-order estimate (SoC-normalized),
not a full-charge measurement -- exposed alongside the raw soh (unchanged,
kept honest) with a soh_basis string explaining the caveat: it assumes
capacity fades uniformly with SoC, which under-corrects for a pack (like the
real one above) that fails specifically at high-rate/low-SoC (sulfation)
rather than uniformly. It is diagnostic only: Overall Grade requires a
phase-labelled full C10 capacity reference and valid electrical evidence.
"""
import os
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series, analyze_csv
from aset_batt.acquisition.models import BatteryProfile

_REAL_CSV = os.path.join(os.path.dirname(__file__), "..", "sessions",
                         "test_QuickScan_20260717_145413.csv")


def _make_profile(**overrides):
    kwargs = dict(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.0, max_charge_v=14.4, cutoff_v=10.5,
        max_charge_a=1.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
        otp_warn=45.0, otp_crit=60.0, internal_r=0.030,
        peukert_k=1.10, peukert_hr=10.0,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _partial_discharge_record(soc_start_frac, rated=5.0, i_dis=0.50,
                              anchor=12.60, cutoff_v=10.45, hz=1.0):
    """A HEALTHY pack (no sulfation, no anomaly) discharged starting from
    soc_start_frac of its rated capacity -- e.g. soc_start_frac=0.5 means the
    discharge only removes half of what a full-to-cutoff run would remove,
    simulating a battery that wasn't charged before Quick Scan ran."""
    dt = 1.0 / hz
    discharge_s = (rated * soc_start_frac) / i_dis * 3600.0
    t, i, v = [], [], []
    tt = 0.0
    for _ in range(int(60 * 10)):
        t.append(tt); i.append(0.0); v.append(anchor)
        tt += 0.1
    n = int(discharge_s * hz)
    v0 = anchor - i_dis * 0.030
    for k in range(n):
        frac = k / max(1, n - 1)
        t.append(tt); i.append(i_dis); v.append(v0 - frac * (v0 - cutoff_v))
        tt += dt
    t = np.asarray(t); i = np.asarray(i); v = np.asarray(v)
    q = np.cumsum(np.clip(i, 0, None) * np.diff(t, prepend=t[0])) / 3600.0
    return t, i, v, np.full(t.size, 25.0), q


class TestSohEstNormalization(unittest.TestCase):
    def test_healthy_pack_starting_at_50pct_soh_est_recovers_true_value(self):
        t, i, v, temp, q = _partial_discharge_record(soc_start_frac=0.5)
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             soc_start=50.0)
        # Raw soh reads ~50% of true (only half the capacity was ever removed).
        self.assertLess(res["soh"], 60.0)
        self.assertGreater(res["soh"], 40.0)
        # soh_est extrapolates back toward the true ~100% figure.
        self.assertGreater(res["soh_est"], 85.0)
        self.assertLessEqual(res["soh_est"], 120.0)

    def test_soc_start_100_is_noop_backward_compat(self):
        t, i, v, temp, q = _partial_discharge_record(soc_start_frac=1.0)
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             soc_start=100.0)
        self.assertAlmostEqual(res["soh_est"], res["soh"], delta=1e-6)
        self.assertEqual(res["soh_basis"], "full-charge measurement")

    def test_soc_start_missing_is_noop_backward_compat(self):
        t, i, v, temp, q = _partial_discharge_record(soc_start_frac=0.5)
        res_with_none = analyze_series(t, i, v, temp, q, _make_profile(),
                                       is_hppc=False, soc_start=None)
        res_without = analyze_series(t, i, v, temp, q, _make_profile(),
                                     is_hppc=False)
        self.assertAlmostEqual(res_with_none["soh_est"], res_with_none["soh"], delta=1e-6)
        self.assertAlmostEqual(res_without["soh_est"], res_without["soh"], delta=1e-6)

    def test_partial_and_full_unverified_runs_do_not_get_false_grades(self):
        """A healthy pack starting at 50% SoC must not be graded as if it
        were a genuinely degraded ~50%-SoH pack. Two INDEPENDENT records:
        the same healthy pack either fully charged (delivers 100% of rated
        to cutoff) or not charged first (delivers only the 50% it had)."""
        t1, i1, v1, temp1, q1 = _partial_discharge_record(soc_start_frac=0.5)
        res_partial = analyze_series(t1, i1, v1, temp1, q1, _make_profile(),
                                     is_hppc=False, soc_start=50.0)
        t2, i2, v2, temp2, q2 = _partial_discharge_record(soc_start_frac=1.0)
        res_full = analyze_series(t2, i2, v2, temp2, q2, _make_profile(),
                                  is_hppc=False, soc_start=100.0)
        # Neither synthetic record carries MAIN_DISCHARGE provenance, so the
        # headline must be REVIEW rather than silently promoting an estimate.
        self.assertEqual(res_partial["grade"], res_full["grade"])
        self.assertEqual(res_partial["grade"], "REVIEW")

    def test_soh_basis_explains_the_estimate(self):
        t, i, v, temp, q = _partial_discharge_record(soc_start_frac=0.5)
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             soc_start=50.0)
        self.assertIn("50%", res["soh_basis"])
        self.assertIn("not a full-charge", res["soh_basis"])


class TestReal2026_0717QuickScanCsvSohEst(unittest.TestCase):
    """The exact file that surfaced this bug -- a genuinely sulfated pack,
    so soh_est must NOT rescue the grade (unlike the healthy-pack case
    above, this one really is bad)."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_REAL_CSV):
            raise unittest.SkipTest("real Quick Scan CSV not present")

    def test_soh_est_present_and_higher_than_raw(self):
        prof = _make_profile()
        res = analyze_csv(_REAL_CSV, prof, fit_ecm=True)
        self.assertIn("soh_est", res)
        self.assertGreater(res["soh_est"], res["soh"])

    def test_quick_scan_does_not_produce_an_overall_grade_from_soh_estimate(self):
        prof = _make_profile()
        res = analyze_csv(_REAL_CSV, prof, fit_ecm=True)
        # This is Quick data, not a phase-labelled full C10 capacity reference.
        # The result may expose an electrical grade, but cannot claim an
        # overall health grade from its normalised capacity estimate.
        self.assertEqual(res["grade"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
