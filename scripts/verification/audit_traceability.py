"""Audit a CSV/sidecar bundle against Final-run traceability fields."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path

from scripts.verification._common import load_sidecar, read_csv, write_result


REQUIRED_TOP = ("session_id", "app_version", "operator", "battery_profile", "test_type",
                "started_at", "ended_at", "status", "protocol", "instruments",
                "safety_limits", "effective_safety_limits", "sha256")
REQUIRED_PROTOCOL = ("id",)
REQUIRED_CAMPAIGN = ("campaign_id", "specimen_id", "run_index", "protocol_revision")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def audit(csv_path: str, sidecar: str | None = None) -> dict:
    rows, fields = read_csv(csv_path)
    meta = load_sidecar(csv_path, sidecar)
    missing = [key for key in REQUIRED_TOP if meta.get(key) in (None, "", {})]
    if meta.get("app_version") == "unknown":
        missing.append("app_version.known")
    campaign = meta.get("validation_campaign")
    if not isinstance(campaign, dict) or not campaign.get("enabled"):
        missing.append("validation_campaign.enabled")
        campaign = {}
    missing.extend(f"validation_campaign.{k}" for k in REQUIRED_CAMPAIGN
                   if campaign.get(k) in (None, "", {}))
    protocol = meta.get("protocol") if isinstance(meta.get("protocol"), dict) else {}
    missing.extend(f"protocol.{k}" for k in REQUIRED_PROTOCOL
                   if protocol.get(k) in (None, "", {}))
    csv_ids = {r.get("Session_ID", "").strip() for r in rows if r.get("Session_ID", "").strip()}
    csv_id = meta.get("session_id")
    id_consistent = ("Session_ID" in fields and bool(csv_id) and len(csv_ids) == 1
                     and csv_ids == {str(csv_id)})
    if not id_consistent:
        missing.append("session_id_consistency")
    if not _valid_timestamp(meta.get("started_at")):
        missing.append("started_at.valid")
    if not _valid_timestamp(meta.get("ended_at")):
        missing.append("ended_at.valid")
    for nested, keys, prefix in ((meta.get("instruments"), ("psu", "electronic_load", "esp32", "mlx90614"), "instruments"),
                                 (meta.get("safety_limits"), ("max_voltage", "min_voltage", "max_current", "max_temperature", "min_temperature"), "safety_limits")):
        if not isinstance(nested, dict):
            missing.extend(f"{prefix}.{key}" for key in keys)
        else:
            missing.extend(f"{prefix}.{key}" for key in keys if nested.get(key) in (None, "", {}))
    instruments = meta.get("instruments") if isinstance(meta.get("instruments"), dict) else {}
    for instrument in ("psu", "electronic_load", "esp32", "mlx90614"):
        identity = instruments.get(instrument)
        if isinstance(identity, dict) and not any(identity.get(k) for k in ("idn", "resource", "serial", "firmware")):
            missing.append(f"instruments.{instrument}.identity")
    effective = meta.get("effective_safety_limits") if isinstance(meta.get("effective_safety_limits"), dict) else {}
    for key in ("ovp_v", "uvp_v", "ocp_a", "otp_c", "utp_c"):
        if effective.get(key) in (None, "", {}):
            missing.append(f"effective_safety_limits.{key}")
    csv_hash = hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()
    hash_match = meta.get("sha256") == csv_hash
    if not hash_match:
        missing.append("sha256.matches_csv")
    hash_sidecar = Path(csv_path + ".sha256")
    side_hash_match = None
    if hash_sidecar.is_file():
        declared = hash_sidecar.read_text(encoding="utf-8").strip().split()[0].lower()
        side_hash_match = declared == csv_hash
        if not side_hash_match:
            missing.append(".sha256.matches_csv")
    if not fields:
        missing.append("CSV header")
    total = len(REQUIRED_TOP) + len(REQUIRED_CAMPAIGN) + len(REQUIRED_PROTOCOL) + 1 + 4 + 5 + 5 + 1
    present = max(0, total - len(set(missing)))
    return {
        "csv_exists": True, "sidecar_exists": bool(meta), "csv_rows": len(rows),
        "session_id": csv_id, "csv_session_ids": sorted(csv_ids),
        "session_id_consistent": id_consistent, "metadata_fields_checked": total,
        "csv_sha256": csv_hash, "metadata_hash_matches_csv": hash_match,
        "sha256_sidecar_matches_csv": side_hash_match,
        "missing_fields": sorted(set(missing)),
        "completeness_pct": present / total * 100.0,
        "final_eligibility": "NOT_ELIGIBLE" if missing else "CANDIDATE_ONLY_REQUIRES_REVIEW",
        "note": "No Final eligibility is granted by this structural check; actual protocol and evidence qualification remain reviewer decisions.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="session CSV (read-only)")
    parser.add_argument("--sidecar", help="sidecar path; defaults to CSV.meta.json")
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
