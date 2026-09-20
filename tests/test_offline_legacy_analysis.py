"""Offline legacy CSV analysis stays useful without weakening live grading."""
import csv
import json
import math
from dataclasses import replace

import numpy as np

from aset_batt.acquisition.analysis import analyze_csv, profile_from_config
from aset_batt.core.config import ConfigManager
from aset_batt.ui.report_html import build_results_html


def _profile():
    return profile_from_config(ConfigManager())


def _write_quick(path, phase=True):
    # First point is recorded rest; then a 0.294 s mini pulse and a sustained
    # main discharge sampled at nonuniform intervals.
    rows = [
        (0.0, 12.87, 0.0, "REST"),
        (0.294, 12.69, 5.302, "MINI_PULSE"),
        (30.0, 12.68, 5.302, "MINI_PULSE"),
        (31.0, 12.85, 0.0, "REST"),
        (32.3, 12.66, 5.301, "MAIN_DISCHARGE"),
        (1800.0, 11.7, 5.301, "MAIN_DISCHARGE"),
        (1820.0, 10.7, 5.301, "MAIN_DISCHARGE"),
        (1880.0, 12.5, 0.0, "REST"),
    ]
    cols = ["Elapsed_s", "Voltage_V", "Current_A", "Temperature_C"]
    if phase:
        cols += ["Phase", "Mode"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for t, v, i, p in rows:
            row = [t, v, i, 25.0]
            if phase:
                row += [p, p]
            writer.writerow(row)


def test_legacy_basis_does_not_become_review_and_old_result_is_separate(tmp_path):
    path = tmp_path / "test_QuickScan_legacy.csv"
    _write_quick(path)
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps({
        "product_name": "YTZ6V", "rated_capacity_ah": 5.3,
        "analysis_version": "quick-screen-v3", "historical_grade": "B",
        "historical_soh_pct": 72.3,
    }), encoding="utf-8")
    result = analyze_csv(str(path), _profile(), fit_ecm=True, offline_legacy=True)
    assert result["dataset_status"] == "LEGACY_COMPATIBLE"
    assert result["grade"] == "N/A"
    assert math.isclose(result["dcir_reanalyzed_mohm"], 33.95, abs_tol=0.1)
    assert math.isclose(result["dcir_reanalyzed_latency_s"], 0.294, abs_tol=1e-6)
    assert math.isclose(result["charge_removed_ah"], 2.67614, abs_tol=0.001)
    assert result["historical_result"]["grade"] == "B"
    assert "72.3" in build_results_html(result)
    assert "Current Quick SoH" in build_results_html(result)
    assert "REVIEW" not in build_results_html(result)


def test_legacy_without_phase_or_metadata_uses_raw_waveform_and_reports_missing_hash(tmp_path):
    path = tmp_path / "quickscan_old.csv"
    _write_quick(path, phase=False)
    result = analyze_csv(str(path), _profile(), fit_ecm=True, offline_legacy=True)
    assert result["dataset_status"] == "LEGACY_COMPATIBLE"
    assert result["phase_detection_source"] == "LEGACY_INFERRED_PHASE"
    assert result["integrity_status"] == "INTEGRITY_HASH_UNAVAILABLE"
    assert any("No metadata sidecar" in note for note in result["dataset_notes"])
    assert result["charge_removed_source"] == "REANALYZED_FROM_RAW_DATA"


def test_bad_hash_is_reported_as_corrupt_without_destroying_raw_metrics(tmp_path):
    path = tmp_path / "quickscan.csv"
    _write_quick(path)
    path.with_suffix(path.suffix + ".sha256").write_text("0" * 64 + "  quickscan.csv\n")
    result = analyze_csv(str(path), _profile(), offline_legacy=True)
    assert result["dataset_status"] == "CORRUPT"
    assert result["charge_removed_ah"] > 0


def test_current_format_quick_csv_is_analyzed_without_hardware(tmp_path):
    path = tmp_path / "test_QuickScan_current.csv"
    rows = [
        (0.0, 12.87, 0.0, "OCV", "VALID"),
        (0.286, 12.69, 5.302, "MINI_PULSE", "VALID"),
        (0.386, 12.68, 5.302, "MINI_PULSE", "VALID"),
        (0.486, 12.85, 0.0, "RELAX", "VALID"),
        (0.586, 12.66, 5.301, "MAIN_DISCHARGE", "VALID"),
        (5.586, 12.50, 5.301, "MAIN_DISCHARGE", "VALID"),
        (10.586, 12.30, 5.301, "MAIN_DISCHARGE", "VALID"),
        (10.686, 12.25, 5.301, "NEAR_CUTOFF", "VALID"),
    ]
    from aset_batt.storage.data_utils import SESSION_COLUMNS, SESSION_SCHEMA_VERSION
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SESSION_COLUMNS)
        writer.writeheader()
        for index, (elapsed, voltage, current, phase, quality) in enumerate(rows):
            writer.writerow({
                "Timestamp": "2026-09-20T10:00:00+07:00",
                "Timestamp_ISO": "2026-09-20T10:00:00+07:00",
                "Elapsed_s": elapsed, "Voltage_V": voltage,
                "Current_A": current, "SoC_pct": 98.0 - index,
                "Temperature_C": 25.0, "Capacity_Ah": "",
                "Mode": phase, "Schema_Version": SESSION_SCHEMA_VERSION,
                "Session_ID": "synthetic-current-quick", "Test_Type": "QuickScan",
                "Phase": phase, "Step_Index": index,
                "Voltage_Source": "synthetic", "Current_Source": "synthetic",
                "Sample_Quality": quality, "Sample_Note": "",
                "Temperature_Status": "VALID", "Temperature_Age_s": 0.0,
                "Temperature_Source": "synthetic",
            })
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps({
        "product_name": "YTZ6V", "battery_product": "YTZ6V",
        "capacity_basis_version": "ytz6v-c10-c20-v1",
        "schema_version": SESSION_SCHEMA_VERSION, "test_type": "QuickScan",
        "status": "completed", "ocv_start_valid": True,
        "ocv_start_soc_pct": 98.0, "ocv_end_valid": False,
        "protocol": {"id": "quick-scan-v2", "analysis_version": "quick-screen-v5",
                     "phases": ["OCV_SETTLE", "MINI_PULSE", "RELAX",
                                "MAIN_DISCHARGE", "TAIL_REST"]},
    }), encoding="utf-8")
    result = analyze_csv(str(path), _profile(), offline_legacy=True)
    assert result["is_quick_scan"] is True
    assert "analysis_layer" not in result
    assert result["quick_capacity_est_status"] == "END_OCV_NOT_VALID"
    assert bool(result["dcir_measured"]) is True
    assert result["quick_soh_est_pct"] != result["quick_soh_est_pct"]
    assert result["quick_soh_est_valid"] is False


def test_current_format_quick_with_excessive_gap_stays_current_analyzer(tmp_path):
    path = tmp_path / "test_QuickScan_current_gap.csv"
    _write_quick(path)
    # Add the complete current schema/protocol markers while retaining a
    # deliberately excessive elapsed-time gap in the raw trace.
    contents = path.read_text(encoding="utf-8").replace(
        "Elapsed_s,Voltage_V,Current_A,Temperature_C,Phase,Mode",
        "Elapsed_s,Voltage_V,Current_A,Temperature_C,Phase,Mode,Schema_Version,Session_ID,Test_Type,Sample_Quality")
    lines = contents.splitlines()
    lines[0] = lines[0]
    rows = [line + ",2.3,current-gap,QuickScan,VALID" for line in lines[1:]]
    path.write_text("\n".join([lines[0]] + rows) + "\n", encoding="utf-8")
    contents = path.read_text(encoding="utf-8")
    contents = (contents.replace("32.3,12.66", "9000.0,12.66")
                        .replace("1800.0,11.7", "9005.0,11.7")
                        .replace("1820.0,10.7", "9010.0,10.7")
                        .replace("1880.0,12.5", "9015.0,12.5"))
    path.write_text(contents, encoding="utf-8")
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps({
        "schema_version": "2.3", "test_type": "QuickScan",
        "protocol": {"id": "quick-scan-v2"}, "status": "completed",
    }), encoding="utf-8")
    result = analyze_csv(str(path), _profile(), offline_legacy=True)
    assert result["is_quick_scan"] is True
    assert result["integration_quality_status"] == "EXCESSIVE_GAPS"
    assert result["quick_capacity_est_status"] == "EXCESSIVE_GAPS"


def test_historical_dcir_over_half_second_is_unavailable(tmp_path):
    path = tmp_path / "test_QuickScan_late_edge.csv"
    _write_quick(path)
    contents = path.read_text(encoding="utf-8")
    contents = contents.replace("0.294,12.69", "0.8,12.69")
    path.write_text(contents, encoding="utf-8")
    result = analyze_csv(str(path), _profile(), offline_legacy=True)
    assert math.isnan(result["dcir_reanalyzed_mohm"])
    assert result["dcir_reanalyzed_source"] == "UNAVAILABLE"


def test_new_acquisition_analysis_default_is_unchanged_for_legacy_basis(tmp_path):
    path = tmp_path / "test_QuickScan_legacy.csv"
    _write_quick(path)
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps({
        "product_name": "YTZ6V", "rated_capacity_ah": 5.3,
    }), encoding="utf-8")
    result = analyze_csv(str(path), _profile(), fit_ecm=True)
    assert result["capacity_basis_status"] == "LEGACY_HISTORICAL_CAPACITY_BASIS"
    assert result["grade"] == "REVIEW"


def test_c10_equivalent_requires_validated_basis_and_uses_measured_loaded_current(tmp_path):
    path = tmp_path / "test_QuickScan_basis.csv"
    _write_quick(path)
    profile = _profile()
    result_unknown = analyze_csv(str(path), profile, offline_legacy=True)
    assert result_unknown["capacity_basis_status"] == "C10_BASIS_UNAVAILABLE"
    assert math.isnan(result_unknown["c10_equivalent_interval_charge_ah"])

    ytz = replace(profile, name="YTZ6V", capacity_ah=5.0,
                  capacity_10h_ah=5.0, capacity_rating_basis="C10",
                  capacity_rating_validated=True, peukert_k=1.10,
                  peukert_hr=10.0)
    result = analyze_csv(str(path), ytz, offline_legacy=True)
    assert result["capacity_basis_status"] == "VALIDATED_C10_PROFILE"
    assert math.isclose(result["mean_discharge_current_a"], 5.301, abs_tol=0.01)
    assert math.isclose(result["peukert_factor_legacy_reanalysis"],
                        (5.301 / 0.5) ** 0.1, rel_tol=0.01)


def test_html_omits_empty_historical_result_section():
    from aset_batt.ui.report_html import build_results_html
    html = build_results_html({
        "analysis_layer": "OFFLINE_CURRENT_REANALYSIS",
        "file_information": {"filename": "quick.csv", "test_type": "Quick Scan"},
        "historical_result": {"grade": None, "capacity_ah": None},
        "dataset_status": "LEGACY_COMPATIBLE", "dataset_notes": [],
    })
    assert "FILE INFORMATION" in html
    assert "CURRENT REANALYSIS" in html
    assert "HISTORICAL RESULT" not in html
