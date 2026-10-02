"""Inventory existing repository evidence without promoting historical data."""
from __future__ import annotations

import argparse
import csv
import json
import re
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

from scripts.verification._common import load_sidecar


def inventory(roots: list[str]) -> list[dict]:
    records = []
    for root_name in roots:
        root = Path(root_name)
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*.csv"))
        for path in files:
            if not path.is_file():
                continue
            meta = load_sidecar(path) if path.suffix.lower() == ".csv" else {}
            row_count = None
            headers = []
            vit_valid = 0
            vit_complete = 0
            stale_temperature = 0
            quality_counts: Counter[str] = Counter()
            temperature_statuses: Counter[str] = Counter()
            if path.suffix.lower() == ".csv":
                try:
                    with path.open(encoding="utf-8-sig", newline="") as handle:
                        reader = csv.DictReader(handle); headers = reader.fieldnames or []
                        row_count = 0
                        for row in reader:
                            row_count += 1
                            quality = (row.get("Sample_Quality") or "VALID").strip().upper()
                            quality_counts[quality or "UNSPECIFIED"] += 1
                            temp_status = (row.get("Temperature_Status") or "").strip().upper()
                            if temp_status:
                                temperature_statuses[temp_status] += 1
                            if quality != "VALID":
                                continue
                            vit_valid += 1
                            try:
                                values = [float(row.get(k, "")) for k in
                                          ("Voltage_V", "Current_A", "Temperature_C")]
                                complete = all(math.isfinite(v) for v in values)
                            except (TypeError, ValueError):
                                complete = False
                            vit_complete += int(complete)
                            if temp_status == "STALE":
                                stale_temperature += 1
                except (OSError, UnicodeError, csv.Error):
                    pass
            campaign = meta.get("validation_campaign") or {}
            complete = bool(meta.get("status") == "completed" and campaign.get("enabled")
                            and campaign.get("campaign_id") and campaign.get("specimen_id")
                            and meta.get("protocol") and meta.get("app_version") not in (None, "", "unknown"))
            if complete:
                evidence_class = "A. FINAL-ELIGIBLE CANDIDATE — requires human protocol/data review"
            elif not meta:
                evidence_class = "E. UNKNOWN — NEEDS REVIEW (legacy record without sidecar)"
            elif meta.get("status") in ("running", "aborted", "safety_trip", "cancelled"):
                evidence_class = "D. INVALID / INCOMPLETE or legacy without terminal metadata"
            else:
                evidence_class = "B. HISTORICAL / DEVELOPMENT — not Final-promoted"
            date = meta.get("started_at")
            if not date:
                match = re.search(r"(20\d{6})", path.name)
                date = (datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
                        if match else "UNKNOWN — filesystem mtime may be a copy time")
            records.append({"file": str(path), "date": date, "session_id": meta.get("session_id"),
                            "battery": meta.get("product_name") or meta.get("battery_product") or "",
                            "test_type": meta.get("test_type"), "data_type": path.suffix,
                            "metadata": bool(meta), "rows": row_count, "columns": headers,
                            "completeness": (f"V/I/T {vit_complete}/{vit_valid} VALID; quality {dict(quality_counts)}; temp_status {dict(temperature_statuses) or 'not recorded'}"
                                             if row_count is not None else "not a CSV"),
                            "valid_rows": vit_valid, "valid_rows_with_complete_vit": vit_complete,
                            "valid_rows_with_stale_temperature": stale_temperature,
                            "quality_counts": dict(quality_counts),
                            "temperature_status_counts": dict(temperature_statuses),
                            "evidence_class": evidence_class,
                            "possible_requirement": "Req.2/3/4/5/7/9/10 only after requirement-specific audit",
                            "reason": "No current complete Final campaign identity and full frozen protocol evidence" if not complete else "Candidate only; requires manual qualification",
                            "action_needed": "Review source/protocol, data quality, campaign identity, and physical provenance"})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", default=["sessions", "test_data.csv"],
                        help="explicit repository evidence paths (default: sessions/ and test_data.csv)")
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    args = parser.parse_args()
    try:
        records = inventory(args.roots)
        payload = {"inventory_count": len(records), "records": records,
                   "policy": "Historical/development evidence is never promoted to Final evidence automatically."}
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
        print(rendered)
        if args.json_out:
            Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
        if args.markdown_out:
            lines = ["# Existing Evidence Inventory", "",
                     "Historical/development records are not Final evidence. Class A means candidate only, never automatic qualification. The completeness field counts V/I/T-present VALID rows; it is not requirement eligibility.", "",
                     "| File | Date | Session ID | Battery | Test Type | Data Type | Metadata? | Completeness | Evidence Class | Possible Requirement | Reason | Action Needed |",
                     "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for r in records:
                vals = [r[k] for k in ("file", "date", "session_id", "battery", "test_type", "data_type", "metadata", "completeness", "evidence_class", "possible_requirement", "reason", "action_needed")]
                lines.append("| " + " | ".join(str(v or "").replace("|", "\\|").replace("\n", " ") for v in vals) + " |")
            Path(args.markdown_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
