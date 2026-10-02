"""Read-only acquisition/logging timing analysis by Mode and Phase."""
from __future__ import annotations

import argparse
import math
import statistics
from collections import defaultdict

from scripts.verification._common import number, read_csv, write_result


def metrics(rows: list[dict[str, str]]) -> dict:
    usable = []
    for row in rows:
        t = number(row.get("Elapsed_s"))
        if t is not None and (row.get("Sample_Quality") or "VALID").upper() == "VALID":
            usable.append((t, row))
    usable.sort(key=lambda pair: pair[0])
    times = [pair[0] for pair in usable]
    dts = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not dts:
        return {"n_samples": len(usable), "elapsed_duration_s": 0.0,
                "n_positive_intervals": 0, "status": "insufficient valid increasing timestamps"}
    duration = times[-1] - times[0]
    ordered = sorted(dts)
    # Nearest-rank percentile is deterministic for short engineering traces.
    p95 = ordered[max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))]
    rates = [1.0 / dt for dt in dts]
    return {
        "n_samples": len(usable), "elapsed_duration_s": duration,
        "n_positive_intervals": len(dts), "median_dt_s": statistics.median(dts),
        "mean_dt_s": statistics.mean(dts), "p95_dt_s": p95, "max_dt_s": max(dts),
        "rate_from_median_dt_hz": 1.0 / statistics.median(dts),
        "rate_from_mean_dt_hz": 1.0 / statistics.mean(dts),
        "intervals_over_elapsed_rate_hz": len(dts) / duration if duration > 0 else None,
        "median_instantaneous_rate_hz": statistics.median(rates),
        "minimum_3_hz_met_for_logged_samples": (1.0 / statistics.mean(dts)) >= 3.0,
        "design_target_10_hz_met_for_logged_samples": (1.0 / statistics.mean(dts)) >= 10.0,
    }


def audit(path: str) -> dict:
    rows, fields = read_csv(path)
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[((row.get("Mode") or "UNLABELLED").strip(),
                 (row.get("Phase") or "UNLABELLED").strip())].append(row)
    by_mode_phase = {f"{mode} / {phase}": metrics(group)
                     for (mode, phase), group in sorted(groups.items())}
    all_result = metrics(rows)
    return {
        "input_csv": path, "timestamp_basis": "Elapsed_s from logged CSV rows; this measures logging cadence, not independently proven instrument acquisition cadence.",
        "fields_present": fields, "by_mode_phase": by_mode_phase,
        "all_rows_summary": all_result,
        "criteria": {"minimum_acceptance_hz": 3.0, "design_target_hz": 10.0,
                     "interpretation": "3–9.99 Hz meets the stated minimum but is below the design target."},
        "limitations": ["CSV timestamps cannot establish samples omitted before logging/throttling.",
                        "Elapsed_s basis and acquisition-vs-logging cadence must be confirmed from the selected acquisition path."],
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
