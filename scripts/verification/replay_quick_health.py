r"""Replay archived sessions through the app analyzer and Quick Health rules.

Example (PowerShell):
  .venv\Scripts\python.exe scripts\verification\replay_quick_health.py `
    --archive "C:\Users\thana\Downloads\sessions (2).zip" `
    --outdir replay_analysis
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aset_batt.acquisition.analysis import (
    analyze_csv,
    profile_from_config,
)
from aset_batt.acquisition.health_assessment import assess_quick_health, load_config
from aset_batt.core.config import ConfigManager


def _profile(metadata: dict):
    config = ConfigManager()
    battery = config.battery
    battery.battery_type = metadata.get("battery_type") or battery.battery_type
    battery.product_name = metadata.get("product_name") or battery.product_name
    for attr, key, cast in (
        ("cells_series", "cells_series", int),
        ("cells_parallel", "cells_parallel", int),
        ("rated_capacity", "rated_capacity_ah", float),
        ("harness_resistance_ohm", "harness_resistance_ohm", float),
    ):
        try:
            if metadata.get(key) is not None:
                setattr(battery, attr, cast(metadata[key]))
        except (TypeError, ValueError):
            pass
    return profile_from_config(config)


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    fields = fields or (list(rows[0]) if rows else ["status", "reason"])
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _has_valid_grading_case(rows: list[dict]) -> bool:
    """Sensitivity is meaningful only for an existing valid composite grade."""
    for row in rows:
        try:
            if bool(row.get("assessment_valid")) and math.isfinite(float(row.get("health_score_quick"))):
                return True
        except (TypeError, ValueError):
            continue
    return False


def run(archive: Path, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="aset_quick_health_replay_"))
    rows, errors = [], []
    grading_config = load_config()
    try:
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                resolved = (temp / member.filename).resolve()
                if temp.resolve() not in resolved.parents and resolved != temp.resolve():
                    raise ValueError(f"Unsafe archive member: {member.filename}")
            zipped.extractall(temp)
        for csv_path in sorted((temp / "sessions").glob("*.csv")):
            meta_path = csv_path.with_suffix(csv_path.suffix + ".meta.json")
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            except (OSError, ValueError):
                meta = {}
            quick = "quick" in (csv_path.name + str(meta.get("test_type", ""))).lower()
            try:
                profile = _profile(meta)
                # This is the same saved-session route as the UI: current schema
                # is recognized by analyze_csv; older traces remain diagnostic.
                result = analyze_csv(str(csv_path), profile, fit_ecm=True if quick else None,
                                     offline_legacy=True)
                recovery = result.get("quick_recovery_delta_v_candidate", float("nan"))
                hp = grading_config.get("profiles", {}).get(profile.name, {})
                replay_gates = []
                if not result.get("health_assessment_valid", False):
                    replay_gates.append("existing analyzer validity gate failed")
                if not result.get("session_complete", True):
                    replay_gates.append("session is not marked complete")
                new_health = (assess_quick_health(
                    quick_soh_pct=result.get("quick_soh_est_pct"),
                    dcir_ohm=(float(result["dcir_mohm"]) / 1000.0
                              if result.get("dcir_mohm") is not None else None),
                    recovery_metric=recovery,
                    profile_config=hp,
                    temperature_c=result.get("temperature_median_c"),
                    quality_flags=replay_gates,
                    config=grading_config,
                ) if quick else {})
                battery_id = (meta.get("battery_id") or meta.get("serial_number")
                              or meta.get("battery_serial_number") or "")
                mode = meta.get("test_type") or ("QuickScan" if quick else "UNKNOWN")
                row = {
                    "file": csv_path.name,
                    "battery_id": battery_id,
                    "run_id": meta.get("session_id") or csv_path.stem,
                    "mode": mode,
                    "profile_source": ("SESSION_METADATA"
                                      if meta.get("battery_type") or meta.get("product_name")
                                      else "CURRENT_CONFIG_UNVERIFIED_FALLBACK"),
                    "product_name": profile.name,
                    "session_status": meta.get("status", "UNKNOWN"),
                    "assessment_valid": new_health.get("health_assessment_valid", result.get("gradeable", False)),
                    "quick_soh_est_pct": result.get("quick_soh_est_pct"),
                    "soh_c10_pct": result.get("verified_soh_pct"),
                    "dcir_ohm": (float(result["dcir_mohm"]) / 1000.0
                                 if result.get("dcir_mohm") is not None else None),
                    "recovery_metric_delta_v_candidate": recovery,
                    "score_soh": new_health.get("score_soh"),
                    "score_dcir": new_health.get("score_dcir"),
                    "score_recovery": new_health.get("score_recovery"),
                    "weight_soh": new_health.get("weight_soh"),
                    "weight_dcir": new_health.get("weight_dcir"),
                    "weight_recovery": new_health.get("weight_recovery"),
                    "contribution_soh": new_health.get("contribution_soh"),
                    "contribution_dcir": new_health.get("contribution_dcir"),
                    "contribution_recovery": new_health.get("contribution_recovery"),
                    "health_score_quick": new_health.get("health_score_quick"),
                    "condition_grade": new_health.get("condition_grade", result.get("grade")),
                    "recommended_action": new_health.get("recommended_action", ""),
                    "validity_reason": new_health.get("health_assessment_reason", ""),
                    "ecm_r0_mohm": result.get("r0_mohm"),
                    "ecm_r1_mohm": result.get("r1_mohm"),
                    "ecm_tau_s": result.get("tau_s"),
                    "ecm_r2": result.get("ecm_r2"),
                    "temperature_median_c": result.get("temperature_median_c"),
                    "sampling_quality": result.get("integration_quality_status"),
                    "capacity_ah": result.get("capacity_ah"),
                }
                rows.append(row)
            except Exception as exc:  # retain run-level failure as explicit evidence
                errors.append({"file": csv_path.name, "error": repr(exc)})
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    _write_csv(outdir / "new_algorithm_replay.csv", rows)
    _write_csv(outdir / "replay_after_gate_audit.csv", rows)
    quick_rows = [r for r in rows if "quick" in str(r["mode"]).lower()
                  or "quick" in r["file"].lower()]
    recovery_values = []
    for row in quick_rows:
        try:
            value = float(row["recovery_metric_delta_v_candidate"])
            if math.isfinite(value) and str(row["session_status"]).lower() == "completed":
                recovery_values.append(value)
        except (TypeError, ValueError):
            pass
    invalid = [r for r in quick_rows if not r["assessment_valid"]]
    invalid_quick_count = len(invalid)
    invalid.extend({"file": item["file"], "assessment_valid": False,
                    "condition_grade": "INVALID", "validity_reason": item["error"]}
                   for item in errors)
    _write_csv(outdir / "invalid_runs.csv", invalid)

    sensitivity = []
    paired = []
    for row in quick_rows:
        if row.get("battery_id"):
            c10 = [candidate for candidate in rows
                   if candidate.get("battery_id") == row["battery_id"]
                   and candidate.get("soh_c10_pct") not in (None, "")]
        else:
            c10 = []
        if not c10:
            paired.append({"battery_id": row.get("battery_id", ""),
                           "quick_run_id": row["run_id"], "c10_run_id": "",
                           "validation_status": "UNAVAILABLE_NO_PAIRED_C10_EVIDENCE",
                           "absolute_error_pct_points": "",
                           "reason": "No traceable Battery ID and paired verified C10 result in archive"})
        for reference in c10:
            try:
                qsoh, c10soh = float(row["quick_soh_est_pct"]), float(reference["soh_c10_pct"])
                error = qsoh - c10soh if math.isfinite(qsoh) and math.isfinite(c10soh) else None
            except (TypeError, ValueError):
                error = None
            paired.append({"battery_id": row["battery_id"], "quick_run_id": row["run_id"],
                           "c10_run_id": reference["run_id"],
                           "quick_soh_pct": row["quick_soh_est_pct"],
                           "soh_c10_pct": reference["soh_c10_pct"],
                           "absolute_error_pct_points": abs(error) if error is not None else "",
                           "signed_error_pct_points": error if error is not None else "",
                           "validation_status": "PAIRED" if error is not None else "PAIRED_METRICS_UNAVAILABLE"})
    if _has_valid_grading_case(quick_rows):
        for row in quick_rows:
            if not row.get("assessment_valid"):
                continue
            for weight_set in grading_config["sensitivity_weight_sets"]:
                cfg = dict(grading_config)
                cfg["weights"] = {key: float(weight_set[key])
                                  for key in ("soh", "dcir", "recovery")}
                recalculated = assess_quick_health(
                    quick_soh_pct=row["quick_soh_est_pct"], dcir_ohm=row["dcir_ohm"],
                    recovery_metric=row["recovery_metric_delta_v_candidate"],
                    profile_config=grading_config.get("profiles", {}).get(
                        str(row.get("product_name", "")), {}), config=cfg,
                    temperature_c=row.get("temperature_median_c"), quality_flags=[],
                )
                sensitivity.append({"run_id": row["run_id"], "battery_id": row["battery_id"],
                                    "weight_set": weight_set["id"], **recalculated})
    _write_csv(outdir / "weight_sensitivity.csv", sensitivity,
               ["run_id", "battery_id", "weight_set", "health_assessment_valid",
                "health_score_quick", "condition_grade", "health_assessment_reason",
                "score_soh", "score_dcir", "score_recovery", "weight_soh", "weight_dcir",
                "weight_recovery", "contribution_soh", "contribution_dcir",
                "contribution_recovery"])
    _write_csv(outdir / "quick_vs_c10_validation.csv", paired,
               ["battery_id", "quick_run_id", "c10_run_id", "quick_soh_pct",
                "soh_c10_pct", "absolute_error_pct_points", "signed_error_pct_points",
                "validation_status", "reason"])
    (outdir / "selected_configuration.json").write_text(
        json.dumps(grading_config, indent=2, ensure_ascii=True), encoding="utf-8")

    summary = {
        "archive": str(archive), "csv_rows_analyzed": len(rows), "analysis_errors": errors,
        "quick_scan_rows": len(quick_rows),
        "valid_quick_scan_rows": len(quick_rows) - invalid_quick_count,
        "invalid_quick_scan_rows": invalid_quick_count,
        "quick_c10_validation_rows": len(paired),
        "paired_quick_c10_rows": sum(
            row.get("validation_status") == "PAIRED" for row in paired),
        "quick_vs_c10_mae_pct_points": (sum(float(row["absolute_error_pct_points"])
            for row in paired if row.get("validation_status") == "PAIRED") /
            max(1, sum(row.get("validation_status") == "PAIRED" for row in paired))
            if any(row.get("validation_status") == "PAIRED" for row in paired) else None),
        "quick_vs_c10_rmse_pct_points": (math.sqrt(sum(float(row["signed_error_pct_points"]) ** 2
            for row in paired if row.get("validation_status") == "PAIRED") /
            max(1, sum(row.get("validation_status") == "PAIRED" for row in paired)))
            if any(row.get("validation_status") == "PAIRED" for row in paired) else None),
        "battery_ids_available": sum(bool(row["battery_id"]) for row in quick_rows),
        "quick_rows_using_unverified_profile_fallback": sum(
            row["profile_source"] != "SESSION_METADATA" for row in quick_rows),
        "completed_quick_recovery_candidate_min_v": min(recovery_values) if recovery_values else None,
        "completed_quick_recovery_candidate_max_v": max(recovery_values) if recovery_values else None,
        "status_counts": dict(Counter(str(row["session_status"]) for row in quick_rows)),
        "conclusion": "No valid composite Quick Scan score can be reported until profile-specific DCIR and recovery references and valid Quick SoH evidence are available.",
        "recovery_status": grading_config.get("recovery_status"),
    }
    (outdir / "replay_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
    lines = [
        "# Quick Health replay validation",
        "",
        f"- CSV sessions analyzed: {summary['csv_rows_analyzed']}",
        f"- Quick Scan rows: {summary['quick_scan_rows']}",
        f"- Valid composite Quick Scan assessments: {summary['valid_quick_scan_rows']}",
        f"- Invalid/retest Quick Scan rows: {summary['invalid_quick_scan_rows']}",
        f"- Battery IDs available for Quick Scan rows: {summary['battery_ids_available']}",
        f"- Quick Scan rows using current-config profile fallback (unverified): {summary['quick_rows_using_unverified_profile_fallback']}",
        f"- Paired Quick Scan/C10 runs: {summary['paired_quick_c10_rows']}",
        f"- Quick/C10 MAE (percentage points): {summary['quick_vs_c10_mae_pct_points']}",
        f"- Quick/C10 RMSE (percentage points): {summary['quick_vs_c10_rmse_pct_points']}",
        "- Recovery score: experimental/uncalibrated; no reference values are configured.",
        f"- Completed-run candidate delta-V range: {summary['completed_quick_recovery_candidate_min_v']} to {summary['completed_quick_recovery_candidate_max_v']} V.",
        "- DCIR score: withheld because no traceable profile healthy/EOL reference pair is configured.",
        "- Weights are engineering-defined candidates; no fitting or ML was performed.",
        "- Quick SoH basis: Q_main = integral(I dt)/3600; Kp = (I_mean/I_C10)^(k-1); Q_C10_est = Q_main × Kp; Q_full_est = Q_C10_est/(SoC_start − SoC_end), with the SoC span converted from percent to fraction; Quick SoH = 100 × Q_full_est/Q_rated_C10.",
        "- Scores: SoH = clip(Quick SoH, 0, 100); DCIR = 100 × clip((R_EOL − R_test)/(R_EOL − R_healthy), 0, 1); recovery uses higher-is-healthier reference interpolation only when profile references are supplied; Health = Σ(weight × normalized score).",
        "- Existing project boundaries 90/80/70 are reused as configurable A/B/C boundaries; classes are project-defined and not attributed to IEC.",
        "- Temperature remains a separate validity gate. A profile-specific range can be configured; no numeric valid-test range is configured in this dataset, and existing over-temperature safety limits remain separate.",
        "- Baseline is preserved separately in baseline_replay.csv. Sessions without profile metadata use the current app config only as an explicitly unverified diagnostic fallback.",
        "- Capstone evidence readiness: the rule implementation and replay can support the method description, but this archive cannot support performance/accuracy claims, DCIR/recovery calibration, grade validation, or Chapter 4/5 result tables until traceable repeated battery IDs, current Quick Scan OCV anchors, profile references, and paired C10 runs are collected.",
        "",
        "Incomplete runs and absent evidence are retained as invalid/unavailable; no missing value is imputed.",
    ]
    (outdir / "algorithm_validation_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, default=Path("replay_analysis"))
    args = parser.parse_args()
    print(json.dumps(run(args.archive, args.outdir), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
