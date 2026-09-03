"""Quick Scan must request the configured 1C current unless a safety cap says no."""
import json
from pathlib import Path

from aset_batt.core import battery_profiles


def test_project_b007_config_allows_its_5p3a_one_c_quick_scan():
    config_path = Path(__file__).resolve().parents[1] / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    battery = config["battery"]
    assert battery["rated_capacity"] == 5.3
    assert battery["max_current"] >= battery["rated_capacity"]


def test_5p3ah_product_profiles_preserve_their_1c_current_limit():
    for product_name in (
        "YTZ6V (12V 5.3Ah VRLA)",
        "FB FTZ6V (12V 5.3Ah VRLA AGM)",
    ):
        product = battery_profiles.get_product(product_name)
        assert product is not None
        assert product.max_cont_discharge_a >= product.rated_capacity_ah
