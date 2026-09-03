import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aset_batt.acquisition.analysis import analyze_csv, profile_from_config
from aset_batt.core.config import ConfigManager


cfg = ConfigManager("config.json")
profile = profile_from_config(cfg)
keys = [
    "capacity_ah", "capacity_norm_ah", "mean_discharge_a", "soh", "grade",
    "capacity_grade", "electrical_grade", "gradeable", "dcir_mohm",
    "dcir_n_steps", "dcir_measured", "r0_mohm", "r1_mohm", "tau_s",
    "ecm_identified", "ecm_r2", "confidence", "ocv_v", "voltage_sag_v",
    "cca_est_a", "quality_warnings",
]

for path in sys.argv[1:]:
    result = analyze_csv(path, profile, fit_ecm=True)
    print(path)
    print(json.dumps({key: result.get(key) for key in keys}, ensure_ascii=False, indent=2))
