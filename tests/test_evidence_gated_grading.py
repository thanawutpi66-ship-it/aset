"""Regression tests for evidence-gated Quick/HPPC/C10 grading."""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series
from aset_batt.acquisition.models import BatteryProfile


def _profile():
    return BatteryProfile(
        name="test", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.0, max_charge_v=14.4, cutoff_v=10.5,
        max_charge_a=10.0, max_discharge_a=10.0, ovp=15.0, uvp=9.5,
        otp_warn=45.0, otp_crit=60.0, internal_r=0.030,
        peukert_k=1.10, peukert_hr=10.0,
        capacity_10h_ah=5.0, capacity_rating_basis="C10",
        capacity_rating_validated=True,
    )


def _run(main_current, *, modes=True):
    # A diagnostic 5 A pulse is deliberately included before a full discharge.
    # Its Ah must never leak into the capacity result when phase labels exist.
    # Keep every relevant edge within the Quick MAIN_DISCHARGE 12.5 s
    # timing limit while retaining the hand-calculated 5.3 Ah case.
    duration = 36000.0 if main_current == 0.53 else 3600.0
    main_t = np.arange(3.0, duration + 3.0 + 12.5, 12.5)
    t = np.concatenate(([0.0, 1.0, 2.0, 3.0], main_t[1:]))
    i = np.concatenate(([0.0, 5.0, 0.0, main_current],
                        np.full(main_t.size - 1, main_current)))
    v = np.concatenate(([12.6, 12.4, 12.6, 12.2],
                        np.linspace(12.2, 10.5, main_t.size - 1)))
    temp = np.full(t.size, 25.0)
    q = np.cumsum(np.clip(i, 0.0, None) * np.diff(t, prepend=t[0])) / 3600.0
    phase = (["REST", "MINI_PULSE", "RELAX", "MAIN_DISCHARGE"] +
             ["MAIN_DISCHARGE"] * (main_t.size - 1)) if modes else None
    return analyze_series(t, i, v, temp, q, _profile(), is_hppc=False,
                          soc_start=100.0, modes=phase)


class TestEvidenceGatedGrading(unittest.TestCase):
    def test_phase_scoped_capacity_excludes_quick_diagnostic_pulse(self):
        result = _run(0.53)
        self.assertEqual(result["capacity_basis"], "MAIN_DISCHARGE")
        self.assertAlmostEqual(result["capacity_ah"], 5.3, places=3)
        self.assertGreater(result["capacity_total_ah"], result["capacity_ah"])
        # Without validated start/end OCV metadata, the integrated Ah remains
        # useful Charge Removed evidence but cannot support a Quick estimate.
        self.assertEqual(result["capacity_grade"], "N/A")
        self.assertFalse(result["capacity_gradeable"])
        self.assertFalse(result["quick_capacity_est_valid"])
        self.assertIn(result["quick_capacity_est_status"],
                      {"START_OCV_NOT_VALID", "EXCESSIVE_GAPS"})

    def test_quick_c1_capacity_never_becomes_overall_a(self):
        result = _run(5.3)
        self.assertEqual(result["capacity_grade"], "N/A")
        self.assertEqual(result["grade"], "N/A")
        self.assertEqual(result["quick_grade"], "N/A")
        self.assertFalse(result["quick_gradeable"])
        self.assertFalse(result["capacity_gradeable"])
        self.assertIn("not the C10 reference", " ".join(result["quality_warnings"]))

    def test_legacy_csv_without_phase_provenance_is_withheld(self):
        result = _run(0.53, modes=False)
        self.assertEqual(result["capacity_basis"], "legacy_all_positive")
        self.assertEqual(result["capacity_grade"], "REVIEW")
        self.assertEqual(result["grade"], "REVIEW")


if __name__ == "__main__":
    unittest.main()
