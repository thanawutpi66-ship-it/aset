"""Regression checks for Chapter 5 evidence integrity.

These are deliberately source/data level checks: a simulation cannot prove a
bench timing result, but it can ensure the application never silently creates
unlabelled HPPC rows or grades an explicitly invalid/gapped input.
"""

import ast
import csv
import tempfile
import unittest
from pathlib import Path

from aset_batt.acquisition.analysis import _read_csv
from aset_batt.storage.data_utils import _sampling_summary


class TestChapter5DataIntegrity(unittest.TestCase):
    def _csv(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                             encoding="utf-8-sig", newline="")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        writer = csv.DictWriter(handle, fieldnames=[
            "Elapsed_s", "Voltage_V", "Current_A", "Temperature_C",
            "Capacity_Ah", "SoC_pct", "Mode", "Phase", "Sample_Quality",
        ])
        writer.writeheader()
        writer.writerows(rows)
        handle.close()
        return handle.name

    def test_invalid_rows_are_excluded_and_critical_gaps_are_reported(self):
        path = self._csv([
            {"Elapsed_s": 0, "Voltage_V": 12.7, "Current_A": 0, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "RELAX", "Phase": "RELAX", "Sample_Quality": "VALID"},
            {"Elapsed_s": 1, "Voltage_V": 0, "Current_A": 0, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "", "Phase": "", "Sample_Quality": "INVALID"},
            {"Elapsed_s": 2, "Voltage_V": 10.5, "Current_A": 5.3, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 5, "Mode": "NEAR_CUTOFF", "Phase": "NEAR_CUTOFF", "Sample_Quality": "GAP"},
        ])
        t, *_series, quality = _read_csv(path)
        self.assertEqual(len(t), 2)
        self.assertEqual(quality["invalid_excluded"], 1)
        self.assertEqual(quality["gap_phases"], ["NEAR_CUTOFF"])

    def test_timing_summary_uses_valid_rows_and_reports_tail_risk(self):
        path = self._csv([
            {"Elapsed_s": 0, "Voltage_V": 12.7, "Current_A": 0, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "MINI_PULSE", "Phase": "MINI_PULSE", "Sample_Quality": "VALID"},
            {"Elapsed_s": 0.1, "Voltage_V": 12.6, "Current_A": 5.3, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "MINI_PULSE", "Phase": "MINI_PULSE", "Sample_Quality": "VALID"},
            {"Elapsed_s": 0.8, "Voltage_V": 12.5, "Current_A": 5.3, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "MINI_PULSE", "Phase": "MINI_PULSE", "Sample_Quality": "VALID"},
            {"Elapsed_s": 0.9, "Voltage_V": 0, "Current_A": 0, "Temperature_C": 25,
             "Capacity_Ah": "", "SoC_pct": 100, "Mode": "", "Phase": "", "Sample_Quality": "INVALID"},
        ])
        info = _sampling_summary(path)["by_phase"]["MINI_PULSE"]
        self.assertEqual(info["samples"], 3)
        self.assertAlmostEqual(info["max_dt_s"], 0.7)
        self.assertEqual(info["dt_over_0p5s"], 1)

    def test_full_hppc_never_logs_an_unlabelled_or_untimed_row(self):
        source = Path("aset_batt/ui/sequences/hppc.py").read_text(encoding="utf-8")
        calls = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_log_sample":
                calls.append({item.arg for item in node.keywords if item.arg})
        self.assertTrue(calls)
        self.assertTrue(all("mode" in call and "expected_dt_s" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
