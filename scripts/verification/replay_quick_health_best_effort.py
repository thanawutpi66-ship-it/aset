"""Replay deduplicated real sessions using best-effort Quick Scan screening.

This is production-code replay of recorded CSV files only. It never creates
synthetic measurements and refuses to overwrite an existing output directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.verification.replay_quick_health import _profile, _write_csv
from aset_batt.acquisition.analysis import analyze_csv
from aset_batt.acquisition.health_assessment import load_config


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _meta_for(path: Path) -> dict:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except (OSError, ValueError):
        return {}


def run(archive: Path, session_dir: Path, outdir: Path, old_results: Path):
    if outdir.exists() and any(outdir.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty replay output: {outdir}")
    outdir.mkdir(parents=True, exist_ok=True)
    archive_sha_before = sha256(archive)
    tmp = Path(tempfile.mkdtemp(prefix="aset_best_effort_replay_"))
    config = load_config()
    cfg_path = Path(__file__).resolve().parents[2] / "quick_health_config.json"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    sources = []
    input_manifest = []
    seen = {}
    sidecar_seen = {}
    try:
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                target = (tmp / member.filename).resolve()
                if tmp.resolve() not in target.parents and target != tmp.resolve():
                    raise ValueError(f"Unsafe archive member: {member.filename}")
            zipped.extractall(tmp)
        candidates = [(path, f"{archive}::{path.relative_to(tmp).as_posix()}", "archive")
                      for path in sorted((tmp / "sessions").rglob("*.csv"))]
        candidates += [(path, str(path.resolve()), "project_sessions")
                       for path in sorted(session_dir.rglob("*.csv"))]
        for path, source_path, group in candidates:
            digest = sha256(path)
            duplicate = digest in seen
            input_manifest.append({
                "source_path": source_path, "file_name": path.name,
                "file_type": "csv", "sha256": digest, "size": path.stat().st_size,
                "source_group": group, "used_in_replay": not duplicate,
                "duplicate_status": f"DUPLICATE_OF:{seen[digest]}" if duplicate else "UNIQUE",
                "sha256_after": None, "unchanged": None,
            })
            sidecar_path = path.with_suffix(path.suffix + ".meta.json")
            if sidecar_path.exists():
                sidecar_hash = sha256(sidecar_path)
                sidecar_duplicate = sidecar_hash in sidecar_seen
                input_manifest.append({
                    "source_path": source_path + ".meta.json",
                    "file_name": sidecar_path.name, "file_type": "metadata_sidecar",
                    "sha256": sidecar_hash, "size": sidecar_path.stat().st_size,
                    "source_group": group, "used_in_replay": False,
                    "duplicate_status": (f"DUPLICATE_OF:{sidecar_seen[sidecar_hash]}"
                                         if sidecar_duplicate else "METADATA_ONLY"),
                    "sha256_after": None, "unchanged": None,
                })
                if not sidecar_duplicate:
                    sidecar_seen[sidecar_hash] = source_path + ".meta.json"
            if duplicate:
                continue
            seen[digest] = source_path
            sources.append((path, source_path, digest, group, _meta_for(path)))

        old_rows = {}
        old_path = old_results / "quickscan_results.csv"
        if old_path.exists():
            with old_path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if row.get("source_sha256"):
                        old_rows[row["source_sha256"]] = row

        rows, errors = [], []
        for path, source_path, digest, group, meta in sources:
            test_type = str(meta.get("test_type") or "")
            quick = "quick" in (path.name + test_type).lower()
            if not quick:
                continue
            try:
                profile = _profile(meta)
                result = analyze_csv(str(path), profile, fit_ecm=True, offline_legacy=True)
                health = result
                old = old_rows.get(digest, {})
                status = str(meta.get("status") or "UNKNOWN").upper()
                row = {
                    "source_path": source_path, "source_group": group,
                    "source_file": path.name, "source_sha256": digest,
                    "session_id": meta.get("session_id") or path.stem,
                    "started_at": meta.get("started_at") or meta.get("start_time"),
                    "application_version": meta.get("app_version") or meta.get("application_version"),
                    "battery_id": meta.get("battery_id") or meta.get("serial_number") or "",
                    "battery_profile": profile.name,
                    "completion_status": status,
                    "sample_count": result.get("sample_count"),
                    "detected_phases": result.get("detected_phases"),
                    "strict_validation_passed": health.get("strict_validation_passed", False),
                    "strict_validation_reason": health.get("strict_validation_reason", ""),
                    "strict_ocv_start_valid": result.get("quick_ocv_start_valid", False),
                    "strict_ocv_end_valid": result.get("quick_ocv_end_valid", False),
                    "best_effort_ocv_start_v": result.get("best_effort_ocv_start_v"),
                    "best_effort_ocv_end_v": result.get("best_effort_ocv_end_v"),
                    "best_effort_soc_start_pct": result.get("best_effort_soc_start_pct"),
                    "best_effort_soc_end_pct": result.get("best_effort_soc_end_pct"),
                    "best_effort_ocv_source": result.get("best_effort_ocv_source"),
                    "quick_soh_strict_pct": result.get("quick_soh_est_pct"),
                    "quick_soh_estimate_pct": result.get("best_effort_quick_soh_pct"),
                    "quick_soh_estimate_status": result.get("best_effort_quick_soh_status"),
                    "quick_soh_score_valid": result.get("best_effort_quick_soh_scoring_valid"),
                    "q_main_ah": result.get("q_interval_removed_ah"),
                    "mean_discharge_a": result.get("quick_mean_discharge_a"),
                    "peukert_k": result.get("quick_peukert_k"),
                    "q_c10_est_ah": result.get("q_c10_interval_equivalent_ah"),
                    "dcir_pulse_mohm": result.get("dcir_mohm") if result.get("dcir_measured") else None,
                    "dcir_method": result.get("dcir_source"),
                    "ecm_r0_mohm": result.get("r0_mohm") if result.get("ecm_identified") else None,
                    "ecm_r2": result.get("ecm_r2") if result.get("ecm_identified") else None,
                    "recovery_delta_v": result.get("quick_recovery_delta_v_candidate"),
                    "score_soh": health.get("score_soh"),
                    "score_dcir": health.get("score_dcir"),
                    "score_recovery": health.get("score_recovery"),
                    "base_weight_soh": health.get("base_weight_soh"),
                    "base_weight_dcir": health.get("base_weight_dcir"),
                    "base_weight_recovery": health.get("base_weight_recovery"),
                    "effective_weight_soh": health.get("weight_soh"),
                    "effective_weight_dcir": health.get("weight_dcir"),
                    "effective_weight_recovery": health.get("weight_recovery"),
                    "contribution_soh": health.get("contribution_soh"),
                    "contribution_dcir": health.get("contribution_dcir"),
                    "contribution_recovery": health.get("contribution_recovery"),
                    "available_weight_sum": health.get("available_weight_sum"),
                    "available_components": health.get("available_components"),
                    "missing_components": health.get("missing_components"),
                    "health_score": health.get("health_score_quick"),
                    "grade": health.get("condition_grade"),
                    "confidence_level": health.get("confidence_level"),
                    "confidence_reason": health.get("confidence_reason"),
                    "assessment_reason": health.get("health_assessment_reason"),
                    "old_strict_status": old.get("assessment_status", "NOT_MATCHED"),
                    "old_strict_grade": old.get("condition_grade", ""),
                    "git_commit": commit,
                    "grading_algorithm_version": health.get("grading_algorithm_version"),
                }
                rows.append(row)
            except Exception as exc:
                errors.append({"source_path": source_path, "source_file": path.name,
                               "source_sha256": digest, "git_commit": commit,
                               "error": repr(exc)})
        # Record post-read hashes for every CSV and metadata sidecar.
        for item in input_manifest:
            base_source = item["source_path"].removesuffix(".meta.json") \
                if item["file_type"] == "metadata_sidecar" else item["source_path"]
            matches = [entry[0] for entry in candidates if entry[1] == base_source]
            if matches:
                check_path = (matches[0].with_suffix(matches[0].suffix + ".meta.json")
                              if item["file_type"] == "metadata_sidecar" else matches[0])
                item["sha256_after"] = sha256(check_path)
                item["unchanged"] = item["sha256_after"] == item["sha256"]

        quick = rows
        gradeable = [r for r in quick if r["grade"] in {"A", "B", "C", "REJECT"}]
        no_score = [r for r in quick if r["grade"] == "NO_SCORE_COMPONENT"]
        by_grade = Counter(r["grade"] for r in quick)
        by_status = Counter(r["completion_status"] for r in quick)
        basis = Counter("+".join(x for x in (r["available_components"] or "").split(",") if x)
                        or "NO_SCORE_COMPONENT" for r in quick)
        confidence = Counter(r["confidence_level"] for r in quick)
        _write_csv(outdir / "quickscan_results.csv", quick)
        _write_csv(outdir / "final_replay.csv", rows + errors)
        _write_csv(outdir / "grading_results.csv", [{k: r[k] for k in (
            "session_id", "source_file", "source_sha256", "score_soh", "score_dcir",
            "score_recovery", "base_weight_soh", "base_weight_dcir", "base_weight_recovery",
            "effective_weight_soh", "effective_weight_dcir", "effective_weight_recovery",
            "contribution_soh", "contribution_dcir", "contribution_recovery",
            "available_weight_sum", "health_score", "grade", "available_components",
            "missing_components", "strict_validation_passed", "confidence_level",
            "confidence_reason", "grading_algorithm_version", "git_commit")}
            for r in quick])
        _write_csv(outdir / "quick_health_components.csv", [{k: r[k] for k in (
            "session_id", "source_file", "source_sha256", "score_soh", "score_dcir",
            "score_recovery", "effective_weight_soh", "effective_weight_dcir",
            "effective_weight_recovery", "contribution_soh", "contribution_dcir",
            "contribution_recovery", "available_weight_sum", "available_components",
            "missing_components", "git_commit")}
            for r in quick])
        _write_csv(outdir / "strict_vs_best_effort.csv", [{
            "session": r["session_id"], "source_file": r["source_file"],
            "source_sha256": r["source_sha256"], "old_strict_status": r["old_strict_status"],
            "old_grade": r["old_strict_grade"], "new_screening_score": r["health_score"],
            "new_screening_grade": r["grade"], "components_used": r["available_components"],
            "effective_weights": ";".join(f"{name}={r['effective_weight_' + name]}"
                for name in ("soh", "dcir", "recovery") if r.get("effective_weight_" + name)),
            "confidence": r["confidence_level"], "reason": r["assessment_reason"],
            "git_commit": commit,
        } for r in quick])
        _write_csv(outdir / "valid_runs.csv", gradeable)
        _write_csv(outdir / "invalid_runs.csv", no_score + errors)
        _write_csv(outdir / "source_dataset_manifest.csv", input_manifest)
        _write_csv(outdir / "dcir_results.csv", [{k: r[k] for k in (
            "source_file", "session_id", "dcir_pulse_mohm", "dcir_method", "ecm_r0_mohm", "ecm_r2", "git_commit")}
            for r in quick])
        _write_csv(outdir / "recovery_results.csv", [{k: r[k] for k in (
            "source_file", "session_id", "recovery_delta_v", "score_recovery", "git_commit")}
            for r in quick])
        (outdir / "final_configuration.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary = {
            "git_commit": commit, "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
            "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(), "config_sha256": sha256(cfg_path),
            "replay_script": str(Path(__file__).resolve()),
            "replay_script_sha256": sha256(Path(__file__).resolve()),
            "archive": str(archive), "archive_sha256_before": archive_sha_before,
            "archive_sha256_after": sha256(archive),
            "archive_unchanged": archive_sha_before == sha256(archive),
            "csv_paths_found": len(input_manifest), "unique_csv_contents": len(sources),
            "quick_scan_total": len(quick), "gradeable_runs": len(gradeable),
            "no_score_runs": len(no_score), "grade_distribution": dict(by_grade),
            "completion_status": dict(by_status), "evidence_basis": dict(basis),
            "confidence": dict(confidence), "analysis_errors": len(errors),
            "source_hashes_unchanged": all(row["unchanged"] for row in input_manifest),
            "conclusion": "Project-defined best-effort screening grades only; not physical validation, IEC grades, or Quick-C10 equivalence.",
        }
        (outdir / "analysis_provenance.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        lines = ["# Best-effort Quick Scan replay", "",
                 f"- Frozen commit: `{commit}`", f"- Unique CSV contents: {len(sources)}",
                 f"- Quick Scan runs: {len(quick)}; gradeable: {len(gradeable)}; no score component: {len(no_score)}",
                 f"- Grade distribution: {dict(by_grade)}", f"- Completion status: {dict(by_status)}",
                 f"- Evidence basis: {dict(basis)}", f"- Evidence confidence: {dict(confidence)}",
                 f"- Analysis errors: {len(errors)}; source hashes unchanged: {summary['source_hashes_unchanged']}",
                 "- Grades use only available normalized components with base weights 0.60/0.30/0.10 renormalized across available scores.",
                 "- Missing DCIR/Recovery references remain unavailable; those metrics are diagnostic only.",
                 "- Screening thresholds are project-defined (90/80/70), not IEC-defined or validated.",
                 "- No physical validation, grade accuracy, or Quick-C10 equivalence is claimed.", ""]
        (outdir / "final_replay_summary.md").write_text("\n".join(lines), encoding="utf-8")
        return summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--old-results", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.archive, args.session_dir, args.outdir, args.old_results), indent=2))


if __name__ == "__main__":
    main()
