import math

from aset_batt.acquisition.ocv_validation import (
    evaluate_c10_reference, estimate_full_capacity,
)
from aset_batt.hardware.pel_batt_test import soh_from_capacity
from aset_batt.ui.sequences.cycle_life import cycle_retention_summary


def test_partial_charge_is_not_direct_soh():
    assert math.isnan(soh_from_capacity(3.6, 5.0))


def test_partial_capacity_requires_two_valid_soc_anchors():
    out = estimate_full_capacity(3.6, 80.0, 0.0,
                                 start_valid=True, end_valid=True,
                                 rated_capacity_ah=5.0)
    assert out["valid"]
    assert out["capacity_ah"] == 4.5

    missing = estimate_full_capacity(3.6, None, 0.0,
                                     start_valid=False, end_valid=True)
    assert not missing["valid"]
    assert missing["capacity_ah"] is None


def test_c10_direct_capacity_is_gated_by_protocol_evidence():
    good = evaluate_c10_reference(
        measured_capacity_ah=4.8, full_charge_confirmed=True,
        reference_current_a=0.5, mean_discharge_current_a=0.502,
        current_tolerance_a=0.03, reached_cutoff=True, data_valid=True,
        rated_c10_capacity_ah=5.0)
    assert good.valid
    assert good.verified_c10_capacity_ah == 4.8
    assert good.soh_pct == 96.0

    bad = evaluate_c10_reference(
        measured_capacity_ah=3.6, full_charge_confirmed=False,
        reference_current_a=0.5, mean_discharge_current_a=0.53,
        current_tolerance_a=0.01, reached_cutoff=True, data_valid=True,
        rated_c10_capacity_ah=5.0)
    assert not bad.valid
    assert bad.verified_c10_capacity_ah is None
    assert bad.soh_pct is None


def test_cycle_measured_only_retention_is_not_soh():
    out = cycle_retention_summary([4.0, 3.8], basis="MEASURED_ONLY")
    assert out["valid"]
    assert out["retention_pct"] == 95.0
    assert math.isnan(out["soh_pct"])


def test_cycle_incompatible_basis_is_withheld():
    out = cycle_retention_summary([4.0, 3.8], basis="UNKNOWN")
    assert not out["valid"]
    assert math.isnan(out["retention_pct"])
