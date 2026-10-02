"""Unit tests for offline verification tools; fixture measurements are not evidence."""
from __future__ import annotations

import csv
import json
import math
import os
import inspect
from types import SimpleNamespace

from scripts.verification.analyze_pulse_dcir_campaign import analyze
from scripts.verification.analyze_sampling import metrics
from scripts.verification.audit_final_grade import audit as audit_grade
from scripts.verification.audit_otp_configuration import audit as audit_otp
from scripts.verification.audit_traceability import audit as audit_traceability
from scripts.verification.build_evidence_manifest import inventory
from scripts.verification.validate_ekf import audit as audit_ekf
from scripts.verification.verify_c10_run import audit as audit_c10
from scripts.verification.verify_vit_completeness import audit as audit_vit


def _csv(path, rows, fields=None):
    fields = fields or list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def test_vit_checker_counts_missing_and_stale_without_rewriting(tmp_path):
    path = tmp_path / "fixture.csv"
    _csv(path, [
        {"Voltage_V": "12.4", "Current_A": "0.5", "Temperature_C": "25", "Sample_Quality": "VALID", "Temperature_Status": "VALID"},
        {"Voltage_V": "12.3", "Current_A": "0.5", "Temperature_C": "", "Sample_Quality": "VALID", "Temperature_Status": "STALE"},
        {"Voltage_V": "", "Current_A": "", "Temperature_C": "", "Sample_Quality": "INVALID", "Temperature_Status": "MISSING"},
    ])
    before = path.read_bytes()
    result = audit_vit(str(path))
    assert result["raw_rows"] == 3
    assert result["valid_samples"] == 2
    assert result["vit_availability"]["Temperature_C"]["missing"] == 1
    assert result["valid_rows_with_explicit_stale_temperature_status"] == 1
    assert path.read_bytes() == before


def test_sampling_reports_both_minimum_and_design_target():
    result = metrics([{"Elapsed_s": str(t), "Sample_Quality": "VALID"}
                      for t in (0.0, 0.2, 0.4, 0.6)])
    assert result["rate_from_mean_dt_hz"] == 5.0
    assert result["minimum_3_hz_met_for_logged_samples"] is True
    assert result["design_target_10_hz_met_for_logged_samples"] is False
    assert result["p95_dt_s"] == 0.2


def test_dcir_between_run_requires_explicit_method(tmp_path):
    fields = ["Elapsed_s", "Current_A", "Voltage_V", "Sample_Quality", "Phase"]
    files = []
    for i, resistance in enumerate((0.010, 0.011)):
        path = tmp_path / f"run{i}.csv"
        _csv(path, [
            {"Elapsed_s": "0.0", "Current_A": "0", "Voltage_V": "12.6", "Sample_Quality": "VALID", "Phase": "PULSE"},
            {"Elapsed_s": "0.2", "Current_A": "1", "Voltage_V": str(12.6-resistance), "Sample_Quality": "VALID", "Phase": "PULSE"},
        ], fields)
        files.append(str(path))
    pending = analyze(files, None, "PULSE")
    assert pending["between_run"]["status"].startswith("NEEDS METHOD DECISION")
    selected = analyze(files, "mean", "PULSE")
    assert selected["between_run"]["n_independent_runs"] == 2
    assert selected["between_run"]["sample_sd_ohm"] > 0
    assert not math.isclose(selected["between_run"]["sample_sd_ohm"], 0.0005)


def test_traceability_does_not_call_legacy_bundle_final(tmp_path):
    path = tmp_path / "legacy.csv"
    _csv(path, [{"Timestamp": "0", "Voltage_V": "12", "Current_A": "0.2", "Temperature_C": "25"}])
    result = audit_traceability(str(path))
    assert result["sidecar_exists"] is False
    assert result["final_eligibility"] == "NOT_ELIGIBLE"
    assert "validation_campaign.enabled" in result["missing_fields"]


def test_c10_checker_does_not_promote_unqualified_csv(tmp_path):
    path = tmp_path / "capacity.csv"
    _csv(path, [{"Voltage_V": "10.5", "Current_A": "0.5", "Temperature_C": "25"}])
    result = audit_c10(str(path))
    assert result["status"] == "INCOMPLETE_OR_UNVERIFIED"
    assert "sidecar_present" in result["missing_or_unverified"]


def test_ekf_checker_requires_independent_reference(tmp_path):
    path = tmp_path / "ekf.csv"
    _csv(path, [{"Elapsed_s": "0", "SoC_pct": "90", "Capacity_Ah": "0"}])
    try:
        audit_ekf(str(path), None, None)
    except ValueError as exc:
        assert "qualifying independent evidence" in str(exc)
    else:
        raise AssertionError("the analyzer must not invent a capacity reference")


def test_grade_checker_does_not_infer_an_issued_grade(tmp_path):
    path = tmp_path / "grade.csv"
    _csv(path, [{"Voltage_V": "12", "Current_A": "0", "Temperature_C": "25"}])
    result = audit_grade(str(path))
    assert result["status"] == "NO_ISSUED_GRADE_FOUND"
    assert result["traceability_candidate"] is False


def test_evidence_inventory_never_promotes_unqualified_run(tmp_path):
    path = tmp_path / "unlabelled.csv"
    _csv(path, [{"Voltage_V": "12", "Current_A": "0", "Temperature_C": "25"}])
    item, = inventory([str(path)])
    assert item["evidence_class"].startswith("E.")
    assert item["valid_rows_with_complete_vit"] == 1


def test_otp_configuration_audit_is_read_only_and_marks_ui_range(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"system": {"simulation_mode": True,
                                            "safety_limits": {"max_temperature": 55}}}), encoding="utf-8")
    before = path.read_bytes()
    result = audit_otp(str(path))
    assert result["simulation_mode"] is True
    assert result["configured_value_within_requirement"] is True
    assert all(point["representable_by_current_ui_range"] for point in result["required_points"])
    assert path.read_bytes() == before


def test_sequence_otp_setting_persists_and_is_used_in_simulation(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from aset_batt.ui import theme
    theme.set_theme("light")
    from aset_batt.core.config import ConfigManager
    from aset_batt.ui.isa101_views import BatteryQtWindow
    from aset_batt.acquisition.models import OperationMode, TestConfig
    from aset_batt.acquisition.worker import AcquisitionWorker

    app = QApplication.instance() or QApplication([])
    config_path = tmp_path / "simulation-config.json"
    cfg = ConfigManager(str(config_path))
    cfg.system.simulation_mode = True
    cfg.system.safety_limits = {"max_voltage": 15.0, "min_voltage": 10.0,
                                "max_current": 5.0, "max_temperature": 55.0,
                                "min_temperature": -10.0}
    window = BatteryQtWindow(cfg)
    # The sequence safety helper resolves through controller.config.
    window.controller = SimpleNamespace(config=cfg)
    try:
        for threshold in (45.0, 50.0, 55.0, 60.0):
            window.spn_otp.setValue(threshold)
            window._on_save_safety_limits()
            reloaded = ConfigManager(str(config_path))
            assert reloaded.system.safety_limits["max_temperature"] == threshold
            assert window._otp_limit() == threshold
            profile = window._acq_profile()
            assert profile.otp_crit == threshold
            backend = SimpleNamespace(emergency_zero=lambda: None)
            worker = AcquisitionWorker(backend, TestConfig(profile, OperationMode.CC_DISCHARGE),
                                       str(tmp_path / "unused.csv"))
            worker._check_safety(12.0, 0.0, threshold - 0.1, profile)
            assert not worker._estop
            worker._check_safety(12.0, 0.0, threshold, profile)
            assert worker._estop
        worker_run_source = inspect.getsource(AcquisitionWorker.run)
        assert '"otp_critical_c": p.otp_crit' in worker_run_source
    finally:
        window.close()
        del app
