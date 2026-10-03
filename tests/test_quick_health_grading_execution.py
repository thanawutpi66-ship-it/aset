from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "verification"))

import verify_quick_health_grading as verification


def test_all_deterministic_vectors_match_independent_expected_outputs():
    for case in verification.make_cases():
        result = verification.assess(case)
        assert result["health_assessment_valid"] is case["expected_valid"], case["case_id"]
        assert result["condition_grade"] == case["expected_grade"], case["case_id"]
        if case["expected_score"] is None:
            assert result["health_score_quick"] is None, case["case_id"]
        else:
            assert abs(result["health_score_quick"] - case["expected_score"]) <= 1e-9, case["case_id"]
        if case["expected_reason"]:
            assert case["expected_reason"] in result["health_assessment_reason"], case["case_id"]


def test_boundaries_are_evaluated_by_real_assessment_engine():
    outcomes = {}
    for case in verification.make_cases():
        if case["case_type"] == "boundary":
            result = verification.assess(case)
            outcomes[case["case_id"]] = result["condition_grade"]
    assert outcomes == {
        "BOUNDARY_90": "A", "BOUNDARY_89_999": "B",
        "BOUNDARY_80": "B", "BOUNDARY_79_999": "C",
        "BOUNDARY_70": "C", "BOUNDARY_69_999": "REJECT",
    }


def test_real_html_renderer_and_gui_grade_selection_preserve_assessment():
    from aset_batt.ui.report_html import build_results_html
    from aset_batt.ui.views import test_control

    source = Path(test_control.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    on_test_finished = next(node for node in ast.walk(tree)
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name == "_on_test_finished")
    gui_source = ast.get_source_segment(source, on_test_finished)
    assert 'results.get("condition_grade", "INVALID")' in gui_source
    assert '"Screening: INVALID / RETEST · "' in gui_source

    for grade in ("A", "B", "C", "REJECT", "INVALID"):
        case = next(c for c in verification.make_cases() if c["expected_grade"] == grade)
        result = verification.assess(case)
        report_data = {
            "grade": "REVIEW", "overall_grade": "REVIEW", "soh": 85.0,
            "capacity_ah": 1.0, "is_quick_scan": True,
            "quick_grade": result["condition_grade"],
            "quick_grade_basis": result["health_assessment_reason"], **result,
        }
        html = build_results_html(report_data)
        assert result["condition_grade"] in html
        if grade == "INVALID":
            assert "INVALID / RETEST" in html
            assert result["health_assessment_reason"] in html
        else:
            assert f'{result["health_score_quick"]:.1f} / 100' in html


def test_battery_id_is_not_an_assessment_input_or_gate():
    import inspect
    params = inspect.signature(verification.assess_quick_health).parameters
    assert "battery_id" not in params


def test_artifact_generation_outputs_expected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "OUT", tmp_path)
    verification.main()
    expected = {
        "grading_test_cases.csv", "grading_boundary_tests.csv",
        "grading_invalid_gate_tests.csv", "grading_gui_report_consistency.csv",
        "grading_execution_summary.md", "grading_test_configuration.json",
    }
    assert expected == {p.name for p in tmp_path.iterdir()}
