from scripts.verification.replay_quick_health import _has_valid_grading_case


def test_sensitivity_requires_at_least_one_valid_composite_grade():
    assert not _has_valid_grading_case([
        {"assessment_valid": False, "health_score_quick": None},
        {"assessment_valid": False, "health_score_quick": 91.0},
    ])
    assert _has_valid_grading_case([
        {"assessment_valid": True, "health_score_quick": 81.5},
    ])
    assert not _has_valid_grading_case([
        {"assessment_valid": True, "health_score_quick": "nan"},
    ])
