"""Offline EKF SoC error analysis against an independent capacity reference."""
from __future__ import annotations

import argparse

from aset_batt.core.validation_campaign import ekf_metrics, reference_soc_from_capacity
from scripts.verification._common import number, read_csv, write_result


def audit(path: str, reference_capacity_ah: float | None,
          start_soc_pct: float | None, reference_column: str | None = None,
          convergence_pct: float = 5.0, sustain_s: float = 60.0) -> dict:
    rows, fields = read_csv(path)
    if not reference_column and (reference_capacity_ah is None or start_soc_pct is None):
        raise ValueError("supply --reference-column or both --reference-capacity-ah and --start-soc-pct from qualifying independent evidence")
    estimated, reference, elapsed = [], [], []
    for row in rows:
        e = number(row.get("SoC_pct")); t = number(row.get("Elapsed_s"))
        r = number(row.get(reference_column)) if reference_column else None
        if reference_column:
            if e is not None and r is not None:
                estimated.append(e); reference.append(r); elapsed.append(t if t is not None else float(len(elapsed)))
        else:
            cap = number(row.get("Capacity_Ah"))
            if e is not None and cap is not None:
                estimated.append(e); elapsed.append(t if t is not None else float(len(elapsed)))
                reference.append(reference_soc_from_capacity([cap], reference_capacity_ah, start_soc_pct)[0])
    result = ekf_metrics(estimated, reference, elapsed, convergence_pct, sustain_s)
    conv = result.get("convergence_s")
    after = []
    if conv is not None and elapsed:
        t0 = elapsed[0] + conv
        after = [abs(a - b) for a, b, t in zip(estimated, reference, elapsed) if t >= t0]
    return {
        "input_csv": path, "reference_source": reference_column or "Capacity_Ah normalized by explicitly supplied independent capacity reference",
        "reference_capacity_ah": reference_capacity_ah, "start_soc_pct": start_soc_pct,
        "metric": result, "post_convergence_n": len(after),
        "post_convergence_max_abs_error_pp": max(after) if after else None,
        "acceptance_limit_pp": 5.0,
        "criterion_status": ("NOT_EVALUABLE" if not after else
                             "MEETS_NUMERIC_LIMIT" if max(after) <= 5.0 else "EXCEEDS_NUMERIC_LIMIT"),
        "note": "Numeric status is not Final evidence eligibility; reference capacity and convergence method must be qualified and frozen before the run.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--reference-capacity-ah", type=float)
    parser.add_argument("--start-soc-pct", type=float)
    parser.add_argument("--reference-column")
    parser.add_argument("--convergence-pct", type=float, default=5.0)
    parser.add_argument("--sustain-s", type=float, default=60.0)
    parser.add_argument("--json-out")
    args = parser.parse_args()
    try:
        write_result(audit(args.csv, args.reference_capacity_ah, args.start_soc_pct,
                           args.reference_column, args.convergence_pct, args.sustain_s), args.json_out)
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
