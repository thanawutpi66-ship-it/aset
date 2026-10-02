"""Read-only check of configured OTP value and supported 45–60 °C control path."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.verification._common import write_result

REQUIRED_OTP_POINTS = (45.0, 50.0, 55.0, 60.0)


def audit(path: str) -> dict:
    config_path = Path(path)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    limits = ((config.get("system") or {}).get("safety_limits") or {})
    current = limits.get("max_temperature")
    accepted = current is not None and 45.0 <= float(current) <= 60.0
    # UI currently permits a wider range. This checks range representability;
    # only a hardware-free app/config test can verify GUI persistence end-to-end.
    return {
        "config_file": str(config_path), "simulation_mode": (config.get("system") or {}).get("simulation_mode"),
        "configured_otp_c": current,
        "configured_value_within_requirement": bool(accepted),
        "required_points": [{"c": p, "representable_by_current_ui_range": -40.0 <= p <= 150.0}
                            for p in REQUIRED_OTP_POINTS],
        "runtime_consumers": ["AutoController.check_safety_limits", "sequence _otp_limit/_seq_check_otp", "AcquisitionWorker profile threshold"],
        "scope": "Static configuration audit only; it does not modify config, create heat, or prove thermal response.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="config JSON (read-only)")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    try:
        write_result(audit(args.config), args.json_out)
        return 0
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
