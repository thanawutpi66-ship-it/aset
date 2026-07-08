"""Industrial-grade audit follow-up R5.

analyze_series()'s grading decision used to produce NO structured log line —
only ECM-fit-rejection reasons were logged, never the actual soh/dcir/r0/r1/
harness_r/grade/confidence values a grade was decided from. Post-hoc
investigation of a mis-graded batch had to re-run analysis on the archived CSV
and hope config/battery_profiles.json hadn't since changed. One INFO-level log
line now makes the decision itself reconstructable from the log alone.
"""
import logging
import unittest

import numpy as np

from aset_batt.acquisition.analysis import analyze_series
from aset_batt.acquisition.models import BatteryProfile


def _profile(**overrides):
    kwargs = dict(
        name="Test Lead-Acid 12V", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.3, max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0,
        max_discharge_a=10.0, ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0,
        internal_r=0.03,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


class TestGradeDecisionIsLogged(unittest.TestCase):
    def test_grading_decision_produces_one_info_line_with_the_real_values(self):
        n = 20
        t = np.arange(n, dtype=float) * 0.2
        i = np.full(n, 1.0)
        v = np.linspace(12.6, 11.5, n)
        temp = np.full(n, 25.0)
        cap = np.cumsum(i) * 0.2 / 3600.0
        profile = _profile(harness_r_ohm=0.02)

        with self.assertLogs("aset_batt.acquisition.analysis", level="INFO") as cm:
            res = analyze_series(t, i, v, temp, cap, profile, is_hppc=False)

        decision_lines = [line for line in cm.output if "GRADE DECISION" in line]
        self.assertEqual(len(decision_lines), 1)
        line = decision_lines[0]

        # The values in the log line must match what the function actually
        # returned, not just be present — a log line with stale/wrong numbers
        # would be worse than no log line (false confidence during an
        # investigation).
        self.assertIn(f"grade={res['grade']}", line)
        self.assertIn(f"confidence={res['confidence']:.2f}", line)
        self.assertIn(f"dcir_mohm={res['dcir_mohm']:.2f}", line)
        self.assertIn(f"r0_mohm={res['r0_mohm']:.2f}", line)
        self.assertIn(f"r1_mohm={res['r1_mohm']:.2f}", line)
        self.assertIn("harness_r_mohm=20.00", line)   # 0.02 Ω -> 20 mΩ
        self.assertIn(f"n_steps={res['dcir_n_steps']}", line)
        self.assertIn(f"gradeable={res['gradeable']}", line)

    def test_nan_soh_is_logged_as_the_literal_string_nan_not_a_crash(self):
        n = 20
        t = np.arange(n, dtype=float) * 0.2
        i = np.full(n, 1.0)
        v = np.full(n, 12.0)   # HPPC-style, never reaches cutoff -> soh stays NaN
        temp = np.full(n, 25.0)
        cap = np.zeros(n)
        profile = _profile()

        with self.assertLogs("aset_batt.acquisition.analysis", level="INFO") as cm:
            analyze_series(t, i, v, temp, cap, profile, is_hppc=False)

        decision_lines = [line for line in cm.output if "GRADE DECISION" in line]
        self.assertEqual(len(decision_lines), 1)
        self.assertIn("soh=nan", decision_lines[0])


if __name__ == "__main__":
    unittest.main()
