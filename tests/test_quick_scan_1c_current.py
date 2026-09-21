"""Quick Scan must request the configured 1C current unless a safety cap says no."""
from aset_batt.core import battery_profiles
from aset_batt.ui.sequences.quick_scan import quick_scan_1c_current
from aset_batt.acquisition.analysis import peukert_capacity, profile_from_config
from aset_batt.core.config import ConfigManager


def test_ytz6v_quick_scan_uses_c10_reference_current():
    product = battery_profiles.get_product("YTZ6V (12V 5.3Ah VRLA)")
    assert product.capacity_10h_ah == 5.0
    assert product.capacity_20h_ah == 5.3
    assert product.rated_capacity_ah == 5.0
    assert product.rated_capacity_ah / 10 == 0.50
    assert product.capacity_20h_ah / 20 == 0.265
    assert quick_scan_1c_current(product.rated_capacity_ah,
                                 product.max_cont_discharge_a) == 5.0


def test_ytz6v_profile_and_peukert_use_c10_reference():
    cfg = ConfigManager.__new__(ConfigManager)
    from aset_batt.core.config import BatteryConfig, SystemConfig
    cfg.battery = BatteryConfig(product_name="YTZ6V (12V 5.3Ah VRLA)",
                                battery_type="LeadAcid", rated_capacity=5.0)
    cfg.system = SystemConfig()
    profile = profile_from_config(cfg)
    assert profile.capacity_ah == 5.0
    assert profile.name == "YTZ6V (12V 5.3Ah VRLA)"
    assert profile.capacity_10h_ah == 5.0
    assert profile.capacity_20h_ah == 5.3
    assert profile.capacity_ah / profile.peukert_hr == 0.500
    normalized = peukert_capacity(1.0, 5.0, profile.capacity_ah,
                                  profile.peukert_k,
                                  ref_c_rate=1.0 / profile.peukert_hr)
    assert normalized == 1.0 * (5.0 / 0.5) ** (profile.peukert_k - 1.0)
    assert profile.capacity_10h_ah / profile.peukert_hr == 0.5


def test_legacy_ytz6v_session_is_marked_and_not_graded_as_current_basis(tmp_path):
    import csv
    import numpy as np
    from aset_batt.acquisition.analysis import analyze_csv
    cfg = ConfigManager.__new__(ConfigManager)
    from aset_batt.core.config import BatteryConfig, SystemConfig
    cfg.battery = BatteryConfig(product_name="YTZ6V (12V 5.3Ah VRLA)",
                                battery_type="LeadAcid", rated_capacity=5.0)
    cfg.system = SystemConfig()
    profile = profile_from_config(cfg)
    csv_path = tmp_path / "test_QuickScan_legacy.csv"
    rows = [(0, 12.7, 0.0, 0.0), (1, 12.6, 0.5, 0.001),
            (3601, 10.4, 0.5, 0.501)]
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Elapsed_s", "Voltage_V", "Current_A", "Capacity_Ah"])
        writer.writerows(rows)
    original_bytes = csv_path.read_bytes()
    result = analyze_csv(str(csv_path), profile)
    assert csv_path.read_bytes() == original_bytes
    assert result["capacity_basis_legacy"] is True
    assert result["capacity_basis_status"] == "LEGACY_HISTORICAL_CAPACITY_BASIS"
    assert result["capacity_grade"] == "REVIEW"
    assert any("historical capacity basis" in w for w in result["quality_warnings"])


def test_ftz6v_does_not_inherit_unconfirmed_capacity_rate_basis():
    product = battery_profiles.get_product("FB FTZ6V (12V 5.3Ah VRLA AGM)")
    assert product is not None
    assert product.capacity_rating_basis == "UNKNOWN"
    assert product.capacity_10h_ah == 0
