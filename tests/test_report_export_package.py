"""Regression proof for the single Word + PDF experiment-report export."""
import csv
import os
import tempfile
from types import SimpleNamespace

from aset_batt.storage.report_export import generate_report_package


def _config():
    return SimpleNamespace(battery=SimpleNamespace(
        battery_type="LeadAcid", product_name="B007", cells_series=6,
        cells_parallel=1, pack_nominal_voltage=12.0, rated_capacity=5.3,
        mass_grams=0,
    ))


def test_report_package_creates_matching_editable_and_submission_files():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "session.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "Elapsed_s", "Voltage_V", "Current_A", "SoC_pct",
                "Temperature_C", "Mode"])
            writer.writeheader()
            writer.writerows([
                {"Elapsed_s": 0, "Voltage_V": 12.7, "Current_A": 0,
                 "SoC_pct": 100, "Temperature_C": 25, "Mode": "OCV"},
                {"Elapsed_s": 1, "Voltage_V": 12.4, "Current_A": 5.3,
                 "SoC_pct": 99, "Temperature_C": 25, "Mode": "MINI_PULSE"},
                {"Elapsed_s": 2, "Voltage_V": 12.6, "Current_A": 0,
                 "SoC_pct": 99, "Temperature_C": 25, "Mode": "RELAX"},
            ])
        result = generate_report_package(
            os.path.join(tmp, "B007_quick"), _config(),
            analysis={"quick_grade": "A", "soh_est": 97.4, "grade": "REVIEW",
                      "soh": 62.2, "capacity_ah": 3.295, "dcir_mohm": 12.4,
                      "r0_mohm": 9.9, "r1_mohm": 88.6, "tau_s": 3.4,
                      "confidence": 0.6, "quality_warnings": []},
            csv_path=csv_path)
        assert os.path.exists(result["docx_path"])
        assert os.path.exists(result["pdf_path"])
        assert not result["warnings"]
