"""
Tests for the dict-returning analyze_csv() in aset_batt.acquisition.analysis.

Verifies:
1. Returns a dict with the expected result keys for a minimal synthetic CSV.
2. force_hppc=True does not crash (ecm_identified may be False for non-pulse data).
3. All numeric fields are float (not NaN) for a full constant-current discharge.
4. Grade is one of: "A", "B", "C", "REJECT", "REVIEW".
"""
import csv
import math
import os
import tempfile

import numpy as np
import pytest

from aset_batt.acquisition.analysis import analyze_csv
from aset_batt.acquisition.models import BatteryProfile


# ---------------------------------------------------------------------------
# Minimal test profile matching the synthetic CSV pack (25.6 V / 8S LiFePO4)
# ---------------------------------------------------------------------------

def _test_profile() -> BatteryProfile:
    return BatteryProfile(
        name="test_pack",
        chemistry="LiFePO4",
        nominal_v=25.6,
        series=8,
        capacity_ah=10.0,
        max_charge_v=29.2,
        cutoff_v=20.0,
        max_charge_a=10.0,
        max_discharge_a=20.0,
        ovp=30.0,
        uvp=19.0,
        otp_warn=45.0,
        otp_crit=55.0,
        internal_r=0.03,
    )


# ---------------------------------------------------------------------------
# CSV writer helpers
# ---------------------------------------------------------------------------

def _write_discharge_csv(path: str, n: int = 300) -> None:
    """Write a minimal full-discharge CSV (300 samples, 0->300 s).

    Voltage drops linearly from 25.6 V to 20.0 V; current = 5 A constant.
    The final voltage reaches the cutoff (20.0 V) so SoH is calculable.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Elapsed_s", "Voltage_V", "Current_A", "Temperature_C", "SoC_pct",
        ])
        for k in range(n):
            t = float(k)
            v = 25.6 - (25.6 - 20.0) * k / max(n - 1, 1)
            i = 5.0
            temp = 25.0
            soc = 100.0 - 100.0 * k / max(n - 1, 1)
            writer.writerow([f"{t:.1f}", f"{v:.4f}", f"{i:.1f}", f"{temp:.1f}", f"{soc:.2f}"])


def _write_discharge_csv_with_mode(path: str, n: int = 300) -> None:
    """Same as above but includes a Mode column (no 'hppc' in values)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Elapsed_s", "Voltage_V", "Current_A", "Temperature_C", "SoC_pct", "Mode",
        ])
        for k in range(n):
            t = float(k)
            v = 25.6 - (25.6 - 20.0) * k / max(n - 1, 1)
            i = 5.0
            temp = 25.0
            soc = 100.0 - 100.0 * k / max(n - 1, 1)
            writer.writerow([f"{t:.1f}", f"{v:.4f}", f"{i:.1f}", f"{temp:.1f}", f"{soc:.2f}", "DISCHARGE"])


# ---------------------------------------------------------------------------
# Expected result keys
# ---------------------------------------------------------------------------

EXPECTED_KEYS = [
    "grade",
    "soh",
    "capacity_ah",
    "dcir_mohm",
    "r0_mohm",
    "r1_mohm",
    "ecm_identified",
    "confidence",
]


# ---------------------------------------------------------------------------
# Test 1: returns a dict with expected keys
# ---------------------------------------------------------------------------

class TestAnalyzeCsvReturnsDict:

    def test_returns_dict(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        assert isinstance(result, dict), (
            f"analyze_csv should return a dict, got {type(result).__name__}"
        )

    def test_expected_keys_present(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        for key in EXPECTED_KEYS:
            assert key in result, (
                f"Expected key '{key}' not found in analyze_csv result. "
                f"Available keys: {sorted(result.keys())}"
            )


# ---------------------------------------------------------------------------
# Test 2: force_hppc=True does not crash
# ---------------------------------------------------------------------------

class TestAnalyzeCsvForceHPPC:

    def test_force_hppc_does_not_crash(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile(), force_hppc=True)
        except Exception as exc:
            pytest.fail(
                f"analyze_csv raised {type(exc).__name__} with force_hppc=True: {exc}"
            )
        finally:
            os.remove(path)
        assert isinstance(result, dict)

    def test_force_hppc_ecm_identified_is_bool(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile(), force_hppc=True)
        finally:
            os.remove(path)
        assert isinstance(result.get("ecm_identified"), bool), (
            f"ecm_identified should be bool, got {type(result.get('ecm_identified')).__name__}"
        )

    def test_force_hppc_returns_expected_keys(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile(), force_hppc=True)
        finally:
            os.remove(path)
        for key in EXPECTED_KEYS:
            assert key in result, (
                f"Key '{key}' missing from force_hppc=True result"
            )


# ---------------------------------------------------------------------------
# Test 3: numeric fields are float and not NaN for a full discharge
# ---------------------------------------------------------------------------

class TestAnalyzeCsvNumericFields:

    NUMERIC_KEYS = ["capacity_ah", "dcir_mohm", "r0_mohm", "r1_mohm", "confidence"]

    def test_numeric_fields_are_float(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        for key in self.NUMERIC_KEYS:
            val = result.get(key)
            assert isinstance(val, (int, float)), (
                f"Field '{key}' should be numeric, got {type(val).__name__}: {val!r}"
            )

    def test_numeric_fields_not_nan_for_full_discharge(self):
        """For a full constant-current discharge these fields must be finite numbers."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        for key in self.NUMERIC_KEYS:
            val = result.get(key)
            assert not (isinstance(val, float) and math.isnan(val)), (
                f"Field '{key}' should not be NaN for a full-discharge CSV, got NaN"
            )

    def test_capacity_ah_is_positive(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        cap = result.get("capacity_ah", 0.0)
        assert cap > 0.0, f"capacity_ah should be positive, got {cap}"

    def test_confidence_in_range(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        conf = result.get("confidence", -1.0)
        assert 0.0 <= conf <= 1.0, f"confidence should be in [0, 1], got {conf}"


# ---------------------------------------------------------------------------
# Test 4: grade is one of the expected values
# ---------------------------------------------------------------------------

VALID_GRADES = {"A", "B", "C", "REJECT", "REVIEW"}


class TestAnalyzeCsvGrade:

    def test_grade_is_valid_string(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        grade = result.get("grade")
        assert isinstance(grade, str), (
            f"grade should be a string, got {type(grade).__name__}: {grade!r}"
        )

    def test_grade_in_allowed_set(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        grade = result.get("grade")
        assert grade in VALID_GRADES, (
            f"grade '{grade}' is not one of the expected values: {VALID_GRADES}"
        )

    def test_grade_with_mode_column(self):
        """CSV with a Mode column (non-HPPC) should still return a valid grade."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv_with_mode(path)
        try:
            result = analyze_csv(path, _test_profile())
        finally:
            os.remove(path)
        grade = result.get("grade")
        assert grade in VALID_GRADES, (
            f"grade '{grade}' not in {VALID_GRADES} (Mode-column CSV)"
        )

    def test_ecm_identified_false_for_non_hppc(self):
        """Plain discharge CSV without HPPC pulses should not trigger ECM identification."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            result = analyze_csv(path, _test_profile(), force_hppc=False)
        finally:
            os.remove(path)
        assert result.get("ecm_identified") is False, (
            "ecm_identified should be False for plain discharge data without HPPC"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
