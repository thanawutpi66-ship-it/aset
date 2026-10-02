"""Read-only audit of issued grade declarations and evidence gating."""
from __future__ import annotations

import argparse

from scripts.verification._common import load_sidecar, read_csv, write_result


def audit(csv_path: str, sidecar: str | None = None) -> dict:
    rows, _ = read_csv(csv_path)
    meta = load_sidecar(csv_path, sidecar)
    grade = meta.get("grade") or meta.get("overall_grade")
    measured_fields = [k for k in ("dcir_mohm", "measured_dcir_mohm", "capacity_ah", "verified_capacity_ah") if meta.get(k) is not None]
    warnings = list(meta.get("quality_warnings") or [])
    checkpoint = meta.get("checkpoint") or {}
    eligible = bool(grade and meta.get("gradeable") is True and meta.get("status") == "completed"
                    and meta.get("app_version") not in (None, "", "unknown") and meta.get("session_id"))
    return {"csv": csv_path, "rows": len(rows), "declared_grade": grade,
            "declared_gradeable": meta.get("gradeable"), "session_status": meta.get("status"),
            "measured_result_fields_in_sidecar": measured_fields,
            "profile_or_fallback_resistance_is_not_measured_dcir": "dcir_mohm" not in measured_fields and "measured_dcir_mohm" not in measured_fields,
            "charge_checkpoint": checkpoint, "quality_warnings": warnings,
            "traceability_candidate": eligible,
            "status": "REVIEW_REQUIRED" if grade else "NO_ISSUED_GRADE_FOUND",
            "note": "A number in a CSV/profile fallback is not treated as measured Final DCIR; verify grade basis against analysis output and raw pulse evidence."}


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
