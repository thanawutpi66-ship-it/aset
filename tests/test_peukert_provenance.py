"""Peukert source, consumer consistency, session traceability and reanalysis tests."""
import csv
import json
import math
from dataclasses import replace
from unittest.mock import patch

from aset_batt.acquisition.analysis import analyze_csv, analyze_series, profile_from_config
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core import battery_profiles
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.config import BatteryConfig, ConfigManager, SystemConfig
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import write_session_metadata


YTZ6 = "YTZ6V (12V 5.3Ah VRLA)"


def _config(product=YTZ6, chemistry="LeadAcid"):
    cfg = ConfigManager.__new__(ConfigManager)
    cfg.battery = BatteryConfig(product_name=product, battery_type=chemistry,
                                rated_capacity=5.0, nominal_voltage=2.0,
                                cells_series=6, cells_parallel=1)
    cfg.system = SystemConfig()
    return cfg


def _profile(k=1.2, source="PRODUCT_OVERRIDE_UNVERIFIED"):
    return BatteryProfile(
        name=YTZ6, chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.0, max_charge_v=14.7, cutoff_v=10.5,
        max_charge_a=5.3, max_discharge_a=5.3, ovp=15.0, uvp=10.0,
        otp_warn=50.0, otp_crit=60.0, internal_r=0.03,
        peukert_k=k, peukert_k_source=source, peukert_hr=10.0,
        peukert_reference_current_a=0.5, capacity_10h_ah=5.0,
        capacity_rating_validated=True,
    )


def _legacy_quick_csv(path):
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
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Elapsed_s", "Voltage_V", "Current_A", "Temperature_C", "Mode"])
        for t, v, i, mode in rows:
            writer.writerow([t, v, i, 25.0, mode])


def test_active_ytz6_profile_and_estimator_share_product_k_and_source():
    profile = profile_from_config(_config())
    model = BatteryModel("LeadAcid", 2.0, 6, 1, product_name=YTZ6)
    assert (profile.peukert_k, profile.peukert_k_source) == (1.16, "PROVISIONAL_EMPIRICAL_UNVERIFIED")
    assert (model.chemistry.peukert_k, model.peukert_k_source) == (1.16, "PROVISIONAL_EMPIRICAL_UNVERIFIED")
    assert profile.peukert_reference_hr == model.peukert_reference_hr == 10.0
    assert profile.peukert_reference_current_a == model.peukert_reference_current_a == 0.5
    estimator = StateEstimator(5.3, model)
    assert math.isclose(estimator._peukert_dah(5.301, 1.0),
                        (5.301 / 0.5) ** 0.16, rel_tol=1e-8)


def test_missing_and_malformed_registry_expose_builtin_fallback(tmp_path):
    original = battery_profiles._PROFILE_FILE
    try:
        battery_profiles._PROFILE_FILE = str(tmp_path / "absent.json")
        battery_profiles.reload()
        missing = battery_profiles.resolve_peukert_parameters(YTZ6, "LeadAcid")
        assert missing["peukert_k"] == 1.16
        assert missing["peukert_k_source"] == "PROVISIONAL_EMPIRICAL_UNVERIFIED"

        malformed = tmp_path / "broken.json"
        malformed.write_text("{bad", encoding="utf-8")
        battery_profiles._PROFILE_FILE = str(malformed)
        battery_profiles.reload()
        broken = battery_profiles.resolve_peukert_parameters(YTZ6, "LeadAcid")
        assert broken["peukert_k"] == 1.16
        assert broken["peukert_k_source"] == "PROVISIONAL_EMPIRICAL_UNVERIFIED"
    finally:
        battery_profiles._PROFILE_FILE = original
        battery_profiles.reload()


def test_no_override_unknown_product_and_unknown_chemistry_resolve_transparently(monkeypatch):
    original = battery_profiles._PRODUCTS[YTZ6]
    try:
        monkeypatch.setitem(battery_profiles._PRODUCTS, YTZ6,
                            replace(original, peukert_k=0.0))
        inherited = profile_from_config(_config())
        assert inherited.peukert_k == 1.1
        assert inherited.peukert_k_source == "CHEMISTRY_DEFAULT_ASSUMPTION"
    finally:
        monkeypatch.setitem(battery_profiles._PRODUCTS, YTZ6, original)

    unknown_product = profile_from_config(_config("missing product", "LeadAcid"))
    assert (unknown_product.peukert_k, unknown_product.peukert_k_source) == (
        1.1, "CHEMISTRY_DEFAULT_ASSUMPTION")
    unknown_chemistry = profile_from_config(_config("missing product", "Unobtainium"))
    fallback_model = BatteryModel("Unobtainium", product_name="missing product")
    assert (unknown_chemistry.peukert_k, unknown_chemistry.peukert_k_source) == (
        1.0, "GENERIC_PROFILE_FALLBACK")
    assert (fallback_model.chemistry.peukert_k, fallback_model.peukert_k_source) == (
        1.0, "GENERIC_PROFILE_FALLBACK")


def test_saved_characterization_is_not_automatically_activated():
    product = battery_profiles.get_product(YTZ6)
    profile = profile_from_config(_config())
    assert product.measured_params.get("peukert_k") is None
    # A characterized candidate, when present, is kept separately from the active override.
    candidate = replace(product, measured_params={"peukert_k": 1.35})
    assert candidate.peukert_k == 1.16
    assert profile.peukert_k == 1.16


def test_saved_characterization_metadata_remains_an_inactive_candidate(tmp_path, monkeypatch):
    product_file = tmp_path / "profiles.json"
    product_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(battery_profiles, "_PROFILE_FILE", str(product_file))
    assert battery_profiles.save_measured_params(YTZ6, {
        "peukert_k": 1.31, "characterized_peukert_k": 1.31,
        "peukert_k_r2": 0.97,
        "peukert_k_source": "CHARACTERIZED_MEASURED",
        "characterization_status": "MEASURED_PENDING_APPROVAL",
        "characterization_timestamp": "2026-09-20T10:00:00+07:00",
    })
    saved = json.loads(product_file.read_text(encoding="utf-8"))
    candidate = saved["products"][YTZ6]["measured_params"]
    assert candidate["characterized_peukert_k"] == 1.31
    assert candidate["characterization_status"] == "MEASURED_PENDING_APPROVAL"
    assert profile_from_config(_config()).peukert_k == 1.16


def test_explicit_activation_persists_and_rolls_back(tmp_path, monkeypatch):
    product_file = tmp_path / "profiles.json"
    product_file.write_text(json.dumps({"products": {YTZ6: {
        "chemistry": "LeadAcid", "nominal_voltage_per_cell": 2.0,
        "cells_series": 6, "cells_parallel": 1, "rated_capacity_ah": 5.0,
        "peukert_k": 1.2, "peukert_hr": 10.0,
        "measured_params": {"characterized_peukert_k": 1.31,
                             "characterization_status": "MEASURED_PENDING_APPROVAL",
                             "characterization_id": "CHAR-001",
                             "characterization_reference_hr": 10.0}
    }} }), encoding="utf-8")
    original_file = battery_profiles._PROFILE_FILE
    monkeypatch.setattr(battery_profiles, "_PROFILE_FILE", str(product_file))
    battery_profiles.reload()
    assert battery_profiles.resolve_peukert_parameters(YTZ6, "LeadAcid")["peukert_k_source"] == "PRODUCT_OVERRIDE_UNVERIFIED"
    assert battery_profiles.approve_measured_peukert(YTZ6)
    active = battery_profiles.resolve_peukert_parameters(YTZ6, "LeadAcid")
    assert (active["peukert_k"], active["peukert_k_source"]) == (1.31, "MEASURED_APPROVED")
    assert json.loads(product_file.read_text(encoding="utf-8"))["products"][YTZ6]["measured_params"]["peukert_activation_history"]
    assert battery_profiles.deactivate_measured_peukert(YTZ6)
    restored = battery_profiles.resolve_peukert_parameters(YTZ6, "LeadAcid")
    assert (restored["peukert_k"], restored["peukert_k_source"]) == (1.2, "PRODUCT_OVERRIDE_UNVERIFIED")
    battery_profiles._PROFILE_FILE = original_file
    battery_profiles.reload()


def test_session_start_metadata_freezes_effective_k_and_reference(tmp_path):
    cfg = _config()
    csv_path = str(tmp_path / "quick.csv")
    write_session_metadata(csv_path, cfg, test_type="QuickScan")
    meta = json.loads((tmp_path / "quick.csv.meta.json").read_text(encoding="utf-8"))
    assert meta["peukert_k"] == 1.16
    assert meta["peukert_k_source"] == "PROVISIONAL_EMPIRICAL_UNVERIFIED"
    assert meta["peukert_reference_hr"] == 10.0
    assert meta["peukert_reference_current_a"] == 0.5
    assert meta["peukert_formula_version"] == "peukert-power-law-v1"


def test_historical_reanalysis_uses_stored_k_and_never_rewrites_csv(tmp_path):
    path = tmp_path / "test_QuickScan_history.csv"
    _legacy_quick_csv(path)
    original_csv = path.read_bytes()
    (tmp_path / "test_QuickScan_history.csv.meta.json").write_text(json.dumps({
        "product_name": YTZ6, "peukert_k": 1.1,
        "peukert_k_source": "CHEMISTRY_DEFAULT_ASSUMPTION",
        "peukert_reference_hr": 10.0,
        "peukert_reference_current_a": 0.5,
        "peukert_reference_capacity_ah": 5.0,
        "peukert_formula_version": "peukert-power-law-v1",
    }), encoding="utf-8")
    changed_current_profile = replace(_profile(1.2), peukert_hr=20.0,
                                     peukert_reference_hr=20.0,
                                     peukert_reference_current_a=0.265,
                                     capacity_10h_ah=5.3, capacity_ah=5.3)
    result = analyze_csv(str(path), changed_current_profile, offline_legacy=True)
    assert result["historical_peukert_k"] == 1.1
    assert result["historical_peukert_k_source"] == "CHEMISTRY_DEFAULT_ASSUMPTION"
    assert result["current_reanalysis_peukert_k"] == 1.2
    assert result["peukert_reanalysis_basis"] == "HISTORICAL_STORED"
    assert math.isclose(result["peukert_factor"], (5.301 / 0.5) ** 0.1, rel_tol=1e-5)
    assert path.read_bytes() == original_csv


def test_stored_historical_k_without_source_is_legacy_unknown(tmp_path):
    path = tmp_path / "test_QuickScan_old.csv"
    _legacy_quick_csv(path)
    (tmp_path / "test_QuickScan_old.csv.meta.json").write_text(
        json.dumps({"peukert_k": 1.1}), encoding="utf-8")
    result = analyze_csv(str(path), _profile(1.2), offline_legacy=True)
    assert result["historical_peukert_k"] == 1.1
    assert result["historical_peukert_k_source"] == "LEGACY_UNKNOWN"
    assert result["peukert_k"] == 1.1


def test_missing_historical_k_is_labeled_current_profile_reanalysis(tmp_path):
    path = tmp_path / "test_QuickScan_unknown.csv"
    _legacy_quick_csv(path)
    original_csv = path.read_bytes()
    result = analyze_csv(str(path), _profile(1.2), offline_legacy=True)
    assert result["historical_peukert_k"] is None
    assert result["historical_peukert_k_source"] == "LEGACY_UNKNOWN"
    assert result["current_reanalysis_peukert_k"] == 1.2
    assert result["peukert_reanalysis_basis"] == "REANALYZED_WITH_CURRENT_PROFILE"
    assert math.isclose(result["peukert_factor"], (5.301 / 0.5) ** 0.2, rel_tol=1e-5)
    assert path.read_bytes() == original_csv


def test_quick_soh_sensitivity_uses_profile_k_and_preserves_values_over_100():
    q_mini, q_main = 0.04405, 2.69893
    i_mini, i_main = 5.302, 5.301
    # Keep the synthetic timeline within the production Quick cadence guard;
    # charge/current are patched below, so elapsed values only model phase
    # timing and must not encode a sparse endpoint gap.
    t = [0.0, 0.1, 0.2, 0.3, 0.4,
         5.4, 10.4, 15.4, 15.5]
    current = [0.0, i_mini, i_mini, 0.0, i_main, i_main, i_main, i_main, i_main]
    voltage = [12.87, 12.86, 12.8, 12.8, 12.7, 11.0, 10.8, 10.6, 10.4]
    modes = ["OCV", "MINI_PULSE", "MINI_PULSE", "RELAX", "MAIN_DISCHARGE",
             "MAIN_DISCHARGE", "MAIN_DISCHARGE", "MAIN_DISCHARGE", "NEAR_CUTOFF"]
    expected = {1.10: 86.84, 1.15: 97.72, 1.20: 109.96}
    for k, expected_soh in expected.items():
        p = _profile(k, "TEST_ASSUMPTION")
        with patch("aset_batt.acquisition.analysis._quick_discharge_interval",
                   return_value=2.74298), patch(
                "aset_batt.acquisition.analysis._quick_mean_discharge_current",
                return_value=5.301):
            result = analyze_series(t, current, voltage, [25.0] * len(t),
                                    [0.0] * len(t), p, False, soc_start=90.0,
                                    soc_end=10.0, ocv_start_valid=True,
                                    ocv_end_valid=True, modes=modes, quick_scan=True)
        ratio = 5.301 / 0.5
        q_interval = 2.74298
        kp = ratio ** (k - 1.0)
        q_full = q_interval * kp / 0.8
        soh = 100.0 * q_full / 5.0
        assert math.isclose(soh, expected_soh, abs_tol=0.02)
        assert p.peukert_k_source == "TEST_ASSUMPTION"
        # Analyzer's Quick k is the profile k; it does not clamp estimates >100%.
        assert result["quick_peukert_k"] == k
        assert math.isclose(result["quick_soh_est_pct"], expected_soh, abs_tol=0.03)
