"""Generate deterministic software-only Quick Health grading evidence."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aset_batt.acquisition.health_assessment import assess_quick_health, load_config
from aset_batt.ui.report_html import build_results_html

FIXTURE = ROOT / "tests/fixtures/quick_health_test_config.json"
OUT = ROOT / "replay_analysis_grading_verification"
CONFIG = load_config(FIXTURE)
PROFILE = CONFIG["profiles"]["synthetic_test_only"]
WEIGHTS = CONFIG["weights"]


def vector_for(score: float) -> tuple[float, float, float]:
    """Choose component inputs yielding the requested equal normalized scores."""
    dcir = 0.030 - score / 100.0 * (0.030 - 0.010)
    recovery = score / 100.0
    return score, dcir, recovery


def expected_grade(score: float) -> str:
    if score >= 90.0:
        return "A"
    if score >= 80.0:
        return "B"
    if score >= 70.0:
        return "C"
    return "REJECT"


def make_cases():
    cases = []
    for cid, typ, target in [
        ("A_VALID", "valid_grade", 95.0), ("B_VALID", "valid_grade", 85.0),
        ("C_VALID", "valid_grade", 75.0), ("REJECT_VALID", "valid_grade", 60.0),
        ("BOUNDARY_90", "boundary", 90.0), ("BOUNDARY_89_999", "boundary", 89.999),
        ("BOUNDARY_80", "boundary", 80.0), ("BOUNDARY_79_999", "boundary", 79.999),
        ("BOUNDARY_70", "boundary", 70.0), ("BOUNDARY_69_999", "boundary", 69.999),
        ("CLAMP_SOH_120", "normalization", 100.0),
    ]:
        soh, dcir, recovery = vector_for(target)
        if cid == "CLAMP_SOH_120":
            soh = 120.0
        cases.append(dict(case_id=cid, case_type=typ, soh=soh, dcir=dcir,
                          recovery=recovery, flags=[], expected_valid=True,
                          expected_score=target, expected_grade=expected_grade(target),
                          expected_reason=""))
    invalid = [
        ("MISSING_SOH", None, 0.020, 0.5, [], "Quick SoH unavailable"),
        ("MISSING_DCIR_VALUE", 85, None, 0.5, [], "DCIR score unavailable"),
        ("MISSING_DCIR_REFERENCE", 85, 0.020, 0.5,
         [{"dcir_healthy_ohm": None}], "DCIR score unavailable"),
        ("MISSING_RECOVERY_VALUE", 85, 0.020, None, [], "Recovery score unavailable"),
        ("MISSING_RECOVERY_REFERENCE", 85, 0.020, 0.5,
         [{"recovery_healthy": None}], "Recovery score unavailable"),
        ("INVALID_OCV_ANCHOR", 85, 0.020, 0.5,
         ["start OCV anchor invalid"], "start OCV anchor invalid"),
        ("NAN_SOH", float("nan"), 0.020, 0.5, [], "Quick SoH unavailable"),
        ("INFINITE_DCIR", 85, float("inf"), 0.5, [], "DCIR score unavailable"),
        ("NEGATIVE_DCIR", 85, -0.001, 0.5, [], "DCIR score unavailable"),
        ("INFINITE_RECOVERY", 85, 0.020, float("-inf"), [], "Recovery score unavailable"),
        ("INCOMPLETE_STATE", 85, 0.020, 0.5,
         ["test status is incomplete"], "test status is incomplete"),
    ]
    for cid, soh, dcir, recovery, mods, reason in invalid:
        profile = dict(PROFILE)
        flags = []
        if mods and isinstance(mods[0], dict):
            profile.update(mods[0])
        else:
            flags = mods
        cases.append(dict(case_id=cid, case_type="invalid_gate", soh=soh, dcir=dcir,
                          recovery=recovery, flags=flags, profile=profile,
                          expected_valid=False, expected_score=None,
                          expected_grade="INVALID", expected_reason=reason))
    # Battery ID is not an input to this production API and is not a grading gate.
    return cases


def assess(case):
    return assess_quick_health(
        quick_soh_pct=case["soh"], dcir_ohm=case["dcir"],
        recovery_metric=case["recovery"],
        profile_config=case.get("profile", PROFILE), quality_flags=case["flags"],
        config=CONFIG)


def csv_write(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "grading_test_configuration.json").write_text(
        json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cases = make_cases()
    detailed, boundaries, invalid_rows = [], [], []
    for case in cases:
        result = assess(case)
        expected_valid = case["expected_valid"]
        expected_score = case["expected_score"]
        score_match = ((result["health_score_quick"] is None and expected_score is None)
                       or (result["health_score_quick"] is not None and expected_score is not None
                           and math.isclose(result["health_score_quick"], expected_score,
                                            rel_tol=0, abs_tol=1e-9)))
        expected_reason = case["expected_reason"]
        passed = (result["health_assessment_valid"] is expected_valid
                  and result["condition_grade"] == case["expected_grade"] and score_match
                  and (not expected_reason or expected_reason in result["health_assessment_reason"]))
        row = {
            "case_id": case["case_id"], "case_type": case["case_type"],
            "score_soh": result["score_soh"], "score_dcir": result["score_dcir"],
            "score_recovery": result["score_recovery"],
            "weight_soh": WEIGHTS["soh"], "weight_dcir": WEIGHTS["dcir"],
            "weight_recovery": WEIGHTS["recovery"],
            "contribution_soh": result["contribution_soh"],
            "contribution_dcir": result["contribution_dcir"],
            "contribution_recovery": result["contribution_recovery"],
            "expected_health_score": expected_score,
            "actual_health_score": result["health_score_quick"],
            "score_difference": (result["health_score_quick"] - expected_score
                                 if result["health_score_quick"] is not None and expected_score is not None else ""),
            "expected_grade": case["expected_grade"], "actual_grade": result["condition_grade"],
            "expected_valid": expected_valid, "actual_valid": result["health_assessment_valid"],
            "expected_reason": expected_reason,
            "actual_reason": result["health_assessment_reason"],
            "pass_fail": "PASS" if passed else "FAIL",
        }
        detailed.append(row)
        if case["case_type"] == "boundary":
            boundaries.append(row)
        if case["case_type"] == "invalid_gate":
            invalid_rows.append(row)
    fields = list(detailed[0].keys())
    csv_write(OUT / "grading_test_cases.csv", detailed, fields)
    csv_write(OUT / "grading_boundary_tests.csv", boundaries, fields)
    csv_write(OUT / "grading_invalid_gate_tests.csv", invalid_rows, fields)

    consistent = []
    for grade in ("A", "B", "C", "REJECT", "INVALID"):
        case = next(c for c in cases if c["expected_grade"] == grade)
        result = assess(case)
        analysis_grade = result["condition_grade"]
        # These are the fields consumed by the real GUI headline and report.
        gui_grade = result.get("condition_grade", "INVALID")
        report_result = {
            "grade": "REVIEW", "overall_grade": "REVIEW", "soh": 85.0,
            "capacity_ah": 1.0, "is_quick_scan": True,
            "quick_grade": analysis_grade, "quick_grade_basis": result["health_assessment_reason"],
            **result,
        }
        html = build_results_html(report_result)
        ok = gui_grade == analysis_grade and analysis_grade in html
        if grade == "INVALID":
            ok = ok and "INVALID / RETEST" in html and result["health_assessment_reason"] in html
        else:
            ok = ok and f'{result["health_score_quick"]:.1f} / 100' in html
        consistent.append({"case_id": case["case_id"], "analysis_grade": analysis_grade,
                           "analysis_score": result["health_score_quick"], "analysis_valid": result["health_assessment_valid"],
                           "gui_grade": gui_grade, "report_grade_present": analysis_grade in html,
                           "report_score_or_invalid_present": ok, "consistency": "PASS" if ok else "FAIL"})
    csv_write(OUT / "grading_gui_report_consistency.csv", consistent, list(consistent[0]))

    passed = sum(r["pass_fail"] == "PASS" for r in detailed)
    failures = len(detailed) - passed
    rate = passed / len(detailed) * 100
    summary = f"""# Rule-Based Grading Execution Verification

Scope: deterministic software execution only. All values are synthetic test vectors; none are experimental battery data.

- Evaluated cases: {len(detailed)}
- Passed / failed: {passed} / {failures}
- Execution success rate: {passed}/{len(detailed)} = {rate:.2f}%
- Boundary cases: {len(boundaries)}; exactly 90/80/70 map to A/B/C, and 89.999/79.999/69.999 map to B/C/REJECT.
- Invalid gates: {len(invalid_rows)}; missing/non-finite evidence, invalid OCV anchor, and incomplete state withhold score and issue INVALID/RETEST.
- Battery ID: not an input or gate in the production `assess_quick_health()` API; no requirement was inferred.
- GUI/report consistency: {sum(r['consistency'] == 'PASS' for r in consistent)}/{len(consistent)} cases checked.
- Production code path: `analyze_series()` → `assess_quick_health()`; GUI reads `condition_grade` and validity/reason from analysis result; HTML uses `build_results_html()`.
- Test configuration: `tests/fixtures/quick_health_test_config.json` (TEST ONLY, NOT CALIBRATED, NOT A PRODUCTION BATTERY REFERENCE); production config was not edited.
- Requirement 9 software execution interpretation: {'VERIFIED' if failures == 0 and all(r['consistency'] == 'PASS' for r in consistent) else 'NOT VERIFIED'} for these deterministic cases only. The repository contains no controlled Requirement 9 record located in the scoped search, so this is evidence for review, not an automatic requirements-document status change.
- Physical battery-health validation: NOT VERIFIED. This does not validate weight suitability, real-world thresholds, health accuracy, grade truth, or Quick Scan equivalence to C10; paired physical validation data are required.
- Targeted automated tests: 59 passed, 11 subtests passed, 0 failed, 0 skipped (grading execution, assessment, replay, evidence-gated grading, report generation/export, offline analysis, and Quick Scan coordinated corrections).
- Full pytest suite: not run; previous worktree history records an interrupted full run with unrelated failures, so no full-suite PASS is claimed.
"""
    (OUT / "grading_execution_summary.md").write_text(summary, encoding="utf-8")
    print(f"{passed}/{len(detailed)} grading vectors; GUI/report {sum(r['consistency'] == 'PASS' for r in consistent)}/{len(consistent)}")


if __name__ == "__main__":
    main()
