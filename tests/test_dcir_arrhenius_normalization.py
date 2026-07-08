"""Phase D1 regression: identify_dcir() (aset_batt/acquisition/analysis.py) now
normalizes measured DCIR to 25 C using the SAME chemistry-specific Arrhenius
model (BatteryModel.temp_rin_multiplier) as the Rin baseline it is graded
against, instead of a flat linear _DCIR_TEMP_COEFF approximation that did not
match the baseline model at all (see _dcir_temp_normalizer's docstring).

measure_iec61960_dcir (aset_batt/core/battery_model.py) intentionally keeps
the flat linear coefficient — that path is scoped to strict IEC 61960
reporting, not the live grading path — so this file only covers identify_dcir.
"""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import identify_dcir, _dcir_temp_normalizer
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core.battery_model import BatteryModel


def _lead_acid_profile():
    return BatteryProfile(
        name="Test Lead-Acid 12V", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=7.0, max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.4,
        max_discharge_a=7.0, ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0,
        internal_r=0.03,
    )


class TestDcirUsesArrheniusNormalization(unittest.TestCase):
    def test_normalized_dcir_matches_battery_model_arrhenius_multiplier(self):
        profile = _lead_acid_profile()
        # Single clean current step (0A rest -> 5A load) measured at 10 C.
        ia = np.array([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])
        raw_r = 0.05  # ohm, the TRUE (uncorrected) resistance at the 10 C measurement
        v_rest = 12.0
        va = np.array([v_rest, v_rest, v_rest, v_rest - 5.0 * raw_r,
                        v_rest - 5.0 * raw_r, v_rest - 5.0 * raw_r])
        temp_c = np.full(6, 10.0)

        dcir_25, std, n_steps, measured, n_stale = identify_dcir(ia, va, temp_c, profile)

        self.assertTrue(measured)
        self.assertEqual(n_steps, 1)

        model = BatteryModel("LeadAcid")
        expected = raw_r / model.temp_rin_multiplier(10.0)
        self.assertAlmostEqual(dcir_25, expected, places=6)

    def test_arrhenius_normalization_differs_from_old_flat_linear_result(self):
        """Proves this is a real behavioral change, not a no-op refactor."""
        profile = _lead_acid_profile()
        ia = np.array([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])
        raw_r = 0.05
        v_rest = 12.0
        va = np.array([v_rest, v_rest, v_rest, v_rest - 5.0 * raw_r,
                        v_rest - 5.0 * raw_r, v_rest - 5.0 * raw_r])
        temp_c = np.full(6, 10.0)

        dcir_25, *_ = identify_dcir(ia, va, temp_c, profile)

        old_flat_linear = raw_r / (1.0 + 0.004 * (10.0 - 25.0))
        self.assertNotAlmostEqual(dcir_25, old_flat_linear, places=3)

    def test_reference_temperature_is_a_no_op(self):
        """At exactly 25 C the Arrhenius multiplier is 1.0, so DCIR should equal
        the raw measured resistance unchanged."""
        profile = _lead_acid_profile()
        ia = np.array([0.0, 0.0, 0.0, 5.0, 5.0, 5.0])
        raw_r = 0.04
        v_rest = 12.0
        va = np.array([v_rest, v_rest, v_rest, v_rest - 5.0 * raw_r,
                        v_rest - 5.0 * raw_r, v_rest - 5.0 * raw_r])
        temp_c = np.full(6, 25.0)

        dcir_25, *_ = identify_dcir(ia, va, temp_c, profile)
        self.assertAlmostEqual(dcir_25, raw_r, places=6)

    def test_falls_back_to_linear_for_unconstructible_chemistry(self):
        """_dcir_temp_normalizer must degrade gracefully (not raise) if
        BatteryModel construction ever fails for a profile's chemistry."""
        from unittest.mock import patch
        profile = _lead_acid_profile()
        with patch("aset_batt.core.battery_model.BatteryModel",
                   side_effect=RuntimeError("boom")):
            mult = _dcir_temp_normalizer(profile)
        self.assertAlmostEqual(mult(25.0), 1.0, places=9)
        self.assertAlmostEqual(mult(10.0), 1.0 + 0.004 * (10.0 - 25.0), places=9)


if __name__ == "__main__":
    unittest.main()
