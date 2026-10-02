"""Read-only V/I/T and sample-quality completeness audit for a session CSV."""
from __future__ import annotations

import argparse
from collections import Counter

from scripts.verification._common import number, read_csv, write_result


def audit(path: str) -> dict:
    rows, fields = read_csv(path)
    quality_counts: Counter[str] = Counter()
    valid = []
    missing: Counter[str] = Counter()
    stale_temperature = 0
    unavailable_temperature = 0
    temperature_status_counts: Counter[str] = Counter()
    for row in rows:
        quality = (row.get("Sample_Quality") or "VALID").strip().upper()
        quality_counts[quality or "UNSPECIFIED"] += 1
        if quality != "VALID":
            continue
        valid.append(row)
        for field in ("Voltage_V", "Current_A", "Temperature_C"):
            if number(row.get(field)) is None:
                missing[field] += 1
        status = (row.get("Temperature_Status") or "").strip().upper()
        if status:
            temperature_status_counts[status] += 1
        if status == "STALE":
            stale_temperature += 1
        elif status in {"INVALID", "MISSING", "UNAVAILABLE", "NOT_AVAILABLE", "DISCONNECTED", "NONFINITE"}:
            unavailable_temperature += 1
    n = len(valid)
    availability = {
        key: {"available": n - missing[key], "missing": missing[key],
              "completeness_pct": ((n - missing[key]) / n * 100.0 if n else None)}
        for key in ("Voltage_V", "Current_A", "Temperature_C")
    }
    all_vit = sum(all(number(row.get(k)) is not None for k in
                      ("Voltage_V", "Current_A", "Temperature_C")) for row in valid)
    return {
        "input_csv": path, "raw_rows": len(rows), "valid_samples": n,
        "quality_counts": dict(quality_counts), "vit_availability": availability,
        "all_vit_valid_samples": all_vit,
        "all_vit_completeness_pct": all_vit / n * 100.0 if n else None,
        "valid_rows_with_explicit_stale_temperature_status": stale_temperature,
        "valid_rows_with_explicit_unavailable_or_invalid_temperature_status": unavailable_temperature,
        "temperature_status_counts": dict(temperature_status_counts),
        "temperature_age_column_present": "Temperature_Age_s" in fields,
        "temperature_source_column_present": "Temperature_Source" in fields,
        "interpretation": "GAP/INVALID rows are counted separately and excluded from the valid-sample denominator; no raw rows are changed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="session CSV (read-only)")
    parser.add_argument("--json-out", help="optional JSON report path")
    args = parser.parse_args()
    try:
        write_result(audit(args.csv), args.json_out)
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
