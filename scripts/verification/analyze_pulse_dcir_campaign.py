"""Analyze step-paired DCIR from independent run CSVs; never auto-selects a method."""
from __future__ import annotations

import argparse
import math
import statistics

from scripts.verification._common import load_sidecar, number, read_csv, write_result


def analyze_run(path: str, phase: str | None = None, max_dt_s: float = 0.5) -> dict:
    rows, _ = read_csv(path)
    usable = []
    for row in rows:
        t, i, v = number(row.get("Elapsed_s")), number(row.get("Current_A")), number(row.get("Voltage_V"))
        if t is None or i is None or v is None:
            continue
        if (row.get("Sample_Quality") or "VALID").strip().upper() != "VALID":
            continue
        usable.append((t, i, v, row))
    usable.sort(key=lambda x: x[0])
    pulse_rows = [r for r in usable if not phase or (r[3].get("Phase") or "").strip().upper() == phase.upper()]
    threshold = max(1e-3, 0.20 * max((abs(x[1]) for x in pulse_rows), default=0.0))
    pulses = []
    idx = 0
    while idx < len(usable) - 1:
        pre, post = usable[idx], usable[idx + 1]
        delta_i = post[1] - pre[1]
        if abs(delta_i) <= threshold or (phase and (post[3].get("Phase") or "").strip().upper() != phase.upper()):
            idx += 1
            continue
        dt = post[0] - pre[0]
        pre_set = usable[max(0, idx - 2):idx + 1]
        v_pre = statistics.median(x[2] for x in pre_set)
        delta_v = post[2] - v_pre
        reason = None
        if not (0.0 < dt <= max_dt_s): reason = "PAIR_INTERVAL_OUT_OF_RANGE"
        elif not math.isfinite(delta_v) or not math.isfinite(delta_i) or delta_i == 0: reason = "NONFINITE_OR_ZERO_DELTA"
        dcir = abs(delta_v / delta_i) if reason is None else None
        pulses.append({"pulse_index": len(pulses) + 1, "t_pre_s": pre[0], "t_post_s": post[0],
                       "dt_s": dt, "v_pre_v": v_pre, "v_post_v": post[2],
                       "i_pre_a": pre[1], "i_post_a": post[1], "delta_v_v": delta_v,
                       "delta_i_a": delta_i, "dcir_ohm": dcir,
                       "quality_pre": pre[3].get("Sample_Quality", "VALID"),
                       "quality_post": post[3].get("Sample_Quality", "VALID"),
                       "accepted": reason is None, "reject_reason": reason})
        idx += 2
    accepted = [p["dcir_ohm"] for p in pulses if p["accepted"]]
    within = {"n": len(accepted)}
    if accepted:
        within.update({"mean_ohm": statistics.mean(accepted), "median_ohm": statistics.median(accepted)})
    if len(accepted) > 1:
        within["sample_sd_ohm"] = statistics.stdev(accepted)
        within["within_run_cv_pct"] = (within["sample_sd_ohm"] / abs(within["mean_ohm"])*100
                                        if within["mean_ohm"] else None)
    meta = load_sidecar(path)
    return {"csv": path, "run_id": meta.get("session_id"), "test_type": meta.get("test_type"),
            "pulses": pulses, "accepted_pulses": len(accepted), "rejected_pulses": len(pulses)-len(accepted),
            "within_run": within, "threshold_delta_i_a": threshold}


def analyze(paths: list[str], aggregation: str | None, phase: str | None,
            max_dt_s: float = 0.5) -> dict:
    runs = [analyze_run(path, phase, max_dt_s) for path in paths]
    result = {"runs": runs, "max_pair_interval_s": max_dt_s,
              "pairing_method": "adjacent VALID samples at rising/falling current edges; ΔI threshold follows current identify_dcir relative 20%-of-maximum rule",
              "run_level_method": aggregation, "repeatability_scope": "between independent CSV runs; within-run pulse variation is reported separately"}
    if aggregation is None:
        result["between_run"] = {"status": "NEEDS METHOD DECISION: Final run-level Pulse-DCIR aggregation rule"}
        return result
    if aggregation not in {"mean", "median"}:
        raise ValueError("aggregation must be mean or median")
    representatives = []
    for run in runs:
        values = [p["dcir_ohm"] for p in run["pulses"] if p["accepted"]]
        if values:
            representative = statistics.mean(values) if aggregation == "mean" else statistics.median(values)
            representatives.append({"run_id": run["run_id"], "representative_ohm": representative,
                                     "valid_pulses": len(values)})
    between = {"representatives": representatives, "n_independent_runs": len(representatives),
               "acceptance_cv_pct": 15.0}
    vals = [r["representative_ohm"] for r in representatives]
    if len(vals) >= 2:
        mean = statistics.mean(vals); sd = statistics.stdev(vals)
        between.update({"mean_ohm": mean, "sample_sd_ohm": sd,
                        "cv_pct": sd / abs(mean) * 100 if mean else None,
                        "numeric_criterion": "MEETS" if mean and sd / abs(mean) * 100 <= 15.0 else "EXCEEDS"})
    else:
        between["numeric_criterion"] = "NOT_EVALUABLE: at least two independent run representatives required"
    result["between_run"] = between
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="+", help="one CSV per independent run; inputs are read-only")
    parser.add_argument("--phase", help="restrict pulse edges to a phase, e.g. PULSE")
    parser.add_argument("--max-dt-s", type=float, default=0.5)
    parser.add_argument("--aggregation", choices=("mean", "median"),
                        help="explicitly selected run-level aggregation; omitted until method is approved")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    try:
        write_result(analyze(args.csv, args.aggregation, args.phase, args.max_dt_s), args.json_out)
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
