"""Regression tests for evidence-gated Quick/HPPC/C10 grading."""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series
from aset_batt.acquisition.models import BatteryProfile


def _profile():
    return BatteryProfile(
        name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.3, max_charge_v=14.4, cutoff_v=10.5,
        max_charge_a=10.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
        otp_warn=45.0, otp_crit=60.0, internal_r=0.030,
        peukert_k=1.10, peukert_hr=10.0,
    )


def _run(main_current, *, modes=True):
    # A diagnostic 5 A pulse is deliberately included before a full discharge.
    # Its Ah must never leak into the capacity result when phase labels exist.
    t = np.array([0.0, 1.0, 2.0, 3.0, 36003.0 if main_current == 0.53 else 3603.0])
    i = np.array([0.0, 5.0, 0.0, main_current, main_current])
    v = np.array([12.6, 12.4, 12.6, 12.2, 10.5])
    temp = np.full(t.size, 25.0)
    q = np.cumsum(np.clip(i, 0.0, None) * np.diff(t, prepend=t[0])) / 3600.0
    phase = ["REST", "MINI_PULSE", "RELAX", "MAIN_DISCHARGE", "MAIN_DISCHARGE"] if modes else None
    return analyze_series(t, i, v, temp, q, _profile(), is_hppc=False,
                          soc_start=100.0, modes=phase)


class TestEvidenceGatedGrading(unittest.TestCase):
    def test_phase_scoped_capacity_excludes_quick_diagnostic_pulse(self):
        result = _run(0.53)
        self.assertEqual(result["capacity_basis"], "MAIN_DISCHARGE")
        self.assertAlmostEqual(result["capacity_ah"], 5.3, places=3)
        self.assertGreater(result["capacity_total_ah"], result["capacity_ah"])
        self.assertEqual(result["capacity_grade"], "A")

    def test_quick_c1_capacity_never_becomes_overall_a(self):
        result = _run(5.3)
        self.assertEqual(result["capacity_grade"], "REVIEW")
        self.assertEqual(result["grade"], "REVIEW")
        self.assertFalse(result["capacity_gradeable"])
        self.assertIn("not the C10 reference", " ".join(result["quality_warnings"]))

    def test_legacy_csv_without_phase_provenance_is_withheld(self):
        result = _run(0.53, modes=False)
        self.assertEqual(result["capacity_basis"], "legacy_all_positive")
        self.assertEqual(result["capacity_grade"], "REVIEW")
        self.assertEqual(result["grade"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
