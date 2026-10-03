import copy

import pytest

from aset_batt.acquisition.health_assessment import (
    assess_quick_health,
    classify_health,
    load_config,
)


def _calibrated_config():
    config = copy.deepcopy(load_config())
    config["profiles"]["test"] = {
        "dcir_healthy_ohm": 0.010,
        "dcir_eol_ohm": 0.030,
        "recovery_healthy": 0.50,
        "recovery_eol": 0.10,
    }
    return config


def test_component_normalization_weighting_and_traceability():
    result = assess_quick_health(
        quick_soh_pct=120.0,
        dcir_ohm=0.020,
        recovery_metric=0.30,
        profile_config=_calibrated_config()["profiles"]["test"],
        config=_calibrated_config(),
    )
    assert result["health_assessment_valid"] is True
    assert result["score_soh"] == 100.0
    assert result["score_dcir"] == 50.0
    assert result["score_recovery"] == pytest.approx(50.0)
    assert result["contribution_soh"] == 60.0
    assert result["contribution_dcir"] == 15.0
    assert result["contribution_recovery"] == 5.0
    assert result["health_score_quick"] == pytest.approx(80.0)
    assert result["condition_grade"] == "B"
    assert result["dcir_ratio"] == 2.0
    assert result["dcir_delta_pct"] == 100.0


def test_dcir_clipping_and_reference_formula():
    cfg = _calibrated_config()
    profile = cfg["profiles"]["test"]
    low = assess_quick_health(quick_soh_pct=50, dcir_ohm=0.001,
                              recovery_metric=0.5, profile_config=profile, config=cfg)
    high = assess_quick_health(quick_soh_pct=50, dcir_ohm=0.050,
                               recovery_metric=0.5, profile_config=profile, config=cfg)
    assert low["score_dcir"] == 100.0
    assert high["score_dcir"] == 0.0
    assert low["score_soh"] == 50.0


def test_missing_references_or_measurements_withhold_grade_and_request_retest():
    result = assess_quick_health(
        quick_soh_pct=90,
        dcir_ohm=0.020,
        recovery_metric=0.3,
        profile_config={},
    )
    assert result["health_assessment_valid"] is False
    assert result["health_score_quick"] is None
    assert result["condition_grade"] == "INVALID"
    assert result["recommended_action"].startswith("RETEST:")
    assert result["score_soh"] == 90.0
    assert result["score_dcir"] is None
    assert result["score_recovery"] is None


def test_quality_gates_prevent_any_condition_grade():
    cfg = _calibrated_config()
    result = assess_quick_health(
        quick_soh_pct=95,
        dcir_ohm=0.012,
        recovery_metric=0.45,
        profile_config=cfg["profiles"]["test"],
        quality_flags=["sample timing invalid"],
        config=cfg,
    )
    assert not result["health_assessment_valid"]
    assert result["condition_grade"] == "INVALID"
    assert result["health_score_quick"] is None
    assert "sample timing invalid" in result["health_assessment_reason"]


def test_configured_temperature_range_is_a_validity_gate_not_a_health_score():
    cfg = _calibrated_config()
    cfg["profiles"]["test"]["valid_test_temperature_min_c"] = 10.0
    cfg["profiles"]["test"]["valid_test_temperature_max_c"] = 35.0
    result = assess_quick_health(
        quick_soh_pct=95, dcir_ohm=0.012, recovery_metric=0.45,
        temperature_c=40.0, profile_config=cfg["profiles"]["test"], config=cfg,
    )
    assert result["health_assessment_valid"] is False
    assert result["condition_grade"] == "INVALID"
    assert result["health_score_quick"] is None
    assert result["score_soh"] == 95.0
    assert "outside the configured validity range" in result["health_assessment_reason"]


@pytest.mark.parametrize(
    ("score", "expected"),
    [(90, "A"), (89.999, "B"), (80, "B"), (79.999, "C"),
     (70, "C"), (69.999, "REJECT")],
)
def test_project_grade_boundaries(score, expected):
    assert classify_health(score, load_config()["grade_thresholds"]) == expected


def test_replay_is_deterministic_for_fixed_inputs():
    cfg = _calibrated_config()
    kwargs = dict(quick_soh_pct=85.2, dcir_ohm=0.018,
                  recovery_metric=0.27, profile_config=cfg["profiles"]["test"], config=cfg)
    assert assess_quick_health(**kwargs) == assess_quick_health(**kwargs)


def test_report_displays_invalid_retest_instead_of_a_condition_grade():
    from aset_batt.ui.report_html import build_results_html

    html = build_results_html({
        "is_quick_scan": True,
        "grade": "N/A",
        "soh": float("nan"),
        "capacity_ah": 0.0,
        "quick_grade": "INVALID",
        "quick_grade_basis": "DCIR reference missing",
        "health_assessment_valid": False,
        "health_assessment_reason": "DCIR reference missing",
        "grading_algorithm_version": "multi-parameter-rule-v1",
    })
    assert "INVALID / RETEST" in html
    assert "DCIR reference missing" in html


def test_config_weights_are_explicit_and_sum_to_one():
    assert load_config()["weights"] == {"soh": 0.60, "dcir": 0.30, "recovery": 0.10}
    cfg = _calibrated_config()
    cfg["weights"]["soh"] = 0.61
    with pytest.raises(ValueError, match="sum to 1.0"):
        assess_quick_health(quick_soh_pct=90, dcir_ohm=0.01,
                            recovery_metric=0.2,
                            profile_config=cfg["profiles"]["test"], config=cfg)
