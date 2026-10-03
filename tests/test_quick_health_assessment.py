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
    assert result["contribution_soh"] == pytest.approx(60.0)
    assert result["contribution_dcir"] == pytest.approx(15.0)
    assert result["contribution_recovery"] == pytest.approx(5.0)
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


def test_missing_components_do_not_withhold_best_effort_grade():
    result = assess_quick_health(
        quick_soh_pct=90,
        dcir_ohm=0.020,
        recovery_metric=0.3,
        profile_config={},
    )
    assert result["health_assessment_valid"] is True
    assert result["strict_validation_passed"] is False
    assert result["health_score_quick"] == pytest.approx(90.0)
    assert result["condition_grade"] == "A"
    assert result["weight_soh"] == pytest.approx(1.0)
    assert result["score_soh"] == 90.0
    assert result["score_dcir"] is None
    assert result["score_recovery"] is None


def test_quality_gates_remain_strict_diagnostics_but_do_not_block_screening():
    cfg = _calibrated_config()
    result = assess_quick_health(
        quick_soh_pct=95,
        dcir_ohm=0.012,
        recovery_metric=0.45,
        profile_config=cfg["profiles"]["test"],
        quality_flags=["sample timing invalid"],
        config=cfg,
    )
    assert result["health_assessment_valid"]
    assert not result["strict_validation_passed"]
    assert result["condition_grade"] == "A"
    assert result["health_score_quick"] == pytest.approx(92.75)
    assert "sample timing invalid" in result["health_assessment_reason"]


def test_temperature_range_affects_strict_status_not_available_component_grade():
    cfg = _calibrated_config()
    cfg["profiles"]["test"]["valid_test_temperature_min_c"] = 10.0
    cfg["profiles"]["test"]["valid_test_temperature_max_c"] = 35.0
    result = assess_quick_health(
        quick_soh_pct=95, dcir_ohm=0.012, recovery_metric=0.45,
        temperature_c=40.0, profile_config=cfg["profiles"]["test"], config=cfg,
    )
    assert result["health_assessment_valid"] is True
    assert result["strict_validation_passed"] is False
    assert result["condition_grade"] == "A"
    assert result["health_score_quick"] == pytest.approx(92.75)
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


def test_report_displays_screening_grade_and_limited_validation():
    from aset_batt.ui.report_html import build_results_html

    html = build_results_html({
        "is_quick_scan": True,
        "grade": "N/A",
        "soh": float("nan"),
        "capacity_ah": 0.0,
        "quick_grade": "A",
        "quick_grade_basis": "DCIR reference missing",
        "health_assessment_valid": True,
        "strict_validation_passed": False,
        "health_score_quick": 90.0,
        "score_soh": 90.0,
        "score_dcir": None,
        "score_recovery": None,
        "weight_soh": 1.0,
        "base_weight_coverage": 0.6,
        "available_components": "soh",
        "confidence_level": "LOW",
        "health_assessment_reason": "DCIR reference missing",
        "grading_algorithm_version": "available-evidence-screening-v2",
    })
    assert "Quick Screening Grade" in html
    assert "Limited evidence" in html
    assert "90.0 / 100" in html
    assert "DCIR reference missing" in html


@pytest.mark.parametrize(("inputs", "expected_score", "expected_weights"), [
    ((90, 0.020, 0.30), 74.0, (0.6, 0.3, 0.1)),
    ((90, None, None), 90.0, (1.0, 0.0, 0.0)),
    ((90, 0.020, None), 76.6666666667, (2/3, 1/3, 0.0)),
    ((90, None, 0.30), 84.2857142857, (6/7, 0.0, 1/7)),
    ((None, 0.020, 0.30), 50.0, (0.0, 0.75, 0.25)),
])
def test_available_evidence_weights_renormalize(inputs, expected_score, expected_weights):
    cfg = _calibrated_config()
    soh, dcir, recovery = inputs
    result = assess_quick_health(quick_soh_pct=soh, dcir_ohm=dcir,
                                 recovery_metric=recovery,
                                 profile_config=cfg["profiles"]["test"], config=cfg)
    assert result["health_score_quick"] == pytest.approx(expected_score)
    actual = (result["weight_soh"], result["weight_dcir"], result["weight_recovery"])
    assert actual == pytest.approx(expected_weights)
    assert sum(actual) == pytest.approx(1.0)


def test_no_score_component_is_explicit_and_does_not_fabricate_score():
    result = assess_quick_health(quick_soh_pct=float("nan"), dcir_ohm=None,
                                 recovery_metric=None, profile_config={})
    assert result["condition_grade"] == "NO_SCORE_COMPONENT"
    assert result["health_score_quick"] is None
    assert result["available_weight_sum"] == 0.0


def test_config_weights_are_explicit_and_sum_to_one():
    assert load_config()["weights"] == {"soh": 0.60, "dcir": 0.30, "recovery": 0.10}
    cfg = _calibrated_config()
    cfg["weights"]["soh"] = 0.61
    with pytest.raises(ValueError, match="sum to 1.0"):
        assess_quick_health(quick_soh_pct=90, dcir_ohm=0.01,
                            recovery_metric=0.2,
                            profile_config=cfg["profiles"]["test"], config=cfg)
