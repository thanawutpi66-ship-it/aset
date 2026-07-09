"""Regression test: the passive HPPC "CCA proxy" (analyze_series()'s cca_est_a)
must sag-test against a cranking-appropriate end-voltage, not the pack's
deep-discharge protection cutoff.

Root cause of the original bug report: profile.cutoff_v (1.75 V/cell for the
LeadAcid starter profile used in this project) is a *sustained*, minutes-to-hours
over-discharge protection floor. A crank event is a brief 30 s pulse the pack is
expected to recover from right after, so SAE J537 lets terminal voltage sag much
further (1.2 V/cell) before calling it a failure. Reusing the deep-discharge
cutoff here shrank the "voltage budget" (OCV - cutoff) roughly in half, making a
healthy pack's CCA proxy read far below what its own measured resistance implies
-- a real field report: 21 A shown for a brand-new-battery test whose ECM fit
implied a much higher cranking capability.

Only LeadAcid gets the SAE J537 cutoff -- other chemistries have no equivalent
standard cranking-cutoff convention in this rig's scope, so they keep using
profile.cutoff_v (see _cca_cutoff_v).
"""
import unittest

from aset_batt.acquisition.analysis import _cca_cutoff_v, _CCA_CRANK_CUTOFF_V_PER_CELL
from aset_batt.acquisition.models import BatteryProfile


def _profile(chemistry, series=6, cutoff_v=10.5):
    return BatteryProfile(
        name="t", chemistry=chemistry, nominal_v=2.0 * series, series=series,
        capacity_ah=5.3, max_charge_v=14.4, cutoff_v=cutoff_v,
        max_charge_a=1.0, max_discharge_a=10.0, harness_r_ohm=0.0,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=0.125,
    )


class TestCcaCutoffChemistryAware(unittest.TestCase):
    def test_lead_acid_uses_sae_j537_cranking_cutoff(self):
        p = _profile("LeadAcid", series=6, cutoff_v=10.5)
        self.assertAlmostEqual(_cca_cutoff_v(p), _CCA_CRANK_CUTOFF_V_PER_CELL * 6)
        self.assertNotAlmostEqual(_cca_cutoff_v(p), p.cutoff_v)

    def test_lead_acid_alias_also_resolves(self):
        # profile.chemistry can carry a raw config alias (e.g. "AGM"/"VRLA") rather
        # than the canonical "LeadAcid" string -- must still resolve correctly.
        p = _profile("AGM", series=6, cutoff_v=10.5)
        self.assertAlmostEqual(_cca_cutoff_v(p), _CCA_CRANK_CUTOFF_V_PER_CELL * 6)

    def test_non_lead_acid_chemistry_keeps_deep_discharge_cutoff(self):
        p = _profile("LiPO", series=6, cutoff_v=19.8)
        self.assertAlmostEqual(_cca_cutoff_v(p), p.cutoff_v)

    def test_cranking_cutoff_gives_a_larger_voltage_budget_than_deep_discharge(self):
        """The concrete regression: for the LeadAcid profile this bug was reported
        against, the fix must widen (not narrow) the usable voltage budget."""
        p = _profile("LeadAcid", series=6, cutoff_v=10.5)
        ocv = 13.18
        budget_old = ocv - p.cutoff_v
        budget_new = ocv - _cca_cutoff_v(p)
        self.assertGreater(budget_new, budget_old)


if __name__ == "__main__":
    unittest.main()
