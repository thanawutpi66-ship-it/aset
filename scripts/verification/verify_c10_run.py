"""Read-only structural/protocol audit for a purported YTZ6V C10 run."""
from __future__ import annotations

import argparse

from scripts.verification._common import load_sidecar, number, read_csv, write_result


def audit(csv_path: str, sidecar: str | None = None) -> dict:
    rows, fields = read_csv(csv_path)
    meta = load_sidecar(csv_path, sidecar)
    protocol = meta.get("protocol") if isinstance(meta.get("protocol"), dict) else {}
    campaign = meta.get("validation_campaign") if isinstance(meta.get("validation_campaign"), dict) else {}
    tests = {
        "sidecar_present": bool(meta),
        "terminal_status_completed": meta.get("status") == "completed",
        "validation_campaign_enabled": campaign.get("enabled") is True,
        "specimen_identified": bool(campaign.get("specimen_id")),
        "product_is_YTZ6V": "YTZ6V" in str(meta.get("product_name") or meta.get("battery_product") or ""),
        "protocol_identified": bool(protocol.get("id")),
        "target_c10_current_recorded": any(k in protocol for k in ("current_a", "test_current_a", "discharge_current_a", "c_rate")),
        "10_50V_cutoff_recorded": any(number(protocol.get(k)) == 10.5 for k in ("cutoff_v", "pack_min_voltage_v", "end_voltage_v")),
        "charge_completed": (meta.get("checkpoint") or {}).get("charge_completed") is True,
        "csv_has_voltage_current_temperature": all(c in fields for c in ("Voltage_V", "Current_A", "Temperature_C")),
        "rows_present": bool(rows),
    }
    return {"input_csv": csv_path, "checks": tests, "missing_or_unverified": [k for k, v in tests.items() if not v],
            "status": "INCOMPLETE_OR_UNVERIFIED" if not all(tests.values()) else "STRUCTURAL_CANDIDATE_ONLY",
            "interpretation": "This does not certify a Final C10 result; source/protocol, data quality, full charge/rest, cutoff event, and engineering review must also be verified."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--sidecar")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    try:
        write_result(audit(args.csv, args.sidecar), args.json_out)
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
