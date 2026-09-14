"""Evidence helpers for repeatable battery-validation campaigns.

This module deliberately contains no Qt or hardware dependencies.  A campaign is
an immutable description of *why* a session was run; session CSVs remain the
source of measured values.  Keeping the calculations here makes the same
evidence usable in the desktop report and cloud payload.
"""
from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any, Iterable


VALIDATION_CAMPAIGN_SCHEMA = "1.0"
DEFAULT_AMBIENT_TARGET_C = 25.0
DEFAULT_AMBIENT_TOLERANCE_C = 3.0


def normalize_campaign(value: Any) -> dict:
    """Return a backwards-compatible, JSON-safe campaign snapshot.

    Empty/legacy config values deliberately mean "not a validation session";
    normal application runs must never be relabelled as research evidence.
    """
    raw = value if isinstance(value, dict) else {}
    enabled = bool(raw.get("enabled", False))
    return {
        "schema_version": str(raw.get("schema_version") or VALIDATION_CAMPAIGN_SCHEMA),
        "enabled": enabled,
        "campaign_id": str(raw.get("campaign_id") or "").strip(),
        "specimen_id": str(raw.get("specimen_id") or "").strip(),
        "expected_condition": str(raw.get("expected_condition") or "").strip(),
        "run_index": max(1, int(raw.get("run_index") or 1)),
        "ambient_target_c": float(raw.get("ambient_target_c", DEFAULT_AMBIENT_TARGET_C)),
        "ambient_tolerance_c": max(0.0, float(raw.get("ambient_tolerance_c", DEFAULT_AMBIENT_TOLERANCE_C))),
        "preconditioning": str(raw.get("preconditioning") or "Full charge → settled 60 min rest").strip(),
        "protocol_revision": str(raw.get("protocol_revision") or "validation-v1").strip(),
        "related_sessions": dict(raw.get("related_sessions") or {}),
    }


def campaign_is_complete(campaign: dict) -> bool:
    return bool(campaign.get("enabled") and campaign.get("campaign_id")
                and campaign.get("specimen_id"))


def ambient_summary(temperatures_c: Iterable[float], campaign: dict) -> dict:
    values = [float(v) for v in temperatures_c if _finite(v)]
    target = float(campaign["ambient_target_c"])
    tolerance = float(campaign["ambient_tolerance_c"])
    if not values:
        return {"available": False, "in_band": False, "target_c": target,
                "tolerance_c": tolerance}
    low, high = min(values), max(values)
    return {
        "available": True,
        "target_c": target,
        "tolerance_c": tolerance,
        "min_c": low,
        "max_c": high,
        "mean_c": mean(values),
        "drift_c": high - low,
        "in_band": low >= target - tolerance and high <= target + tolerance,
    }


def replicate_statistics(values: Iterable[float]) -> dict:
    """Return small-sample repeatability statistics without inventing data."""
    data = [float(v) for v in values if _finite(v)]
    n = len(data)
    if not n:
        return {"n": 0}
    result = {"n": n, "mean": mean(data), "min": min(data), "max": max(data)}
    if n < 2:
        return result
    sd = stdev(data)
    result["std_dev"] = sd
    result["cv_pct"] = (sd / abs(result["mean"]) * 100.0
                        if abs(result["mean"]) > 1e-12 else None)
    # Student-t 95% two-sided critical values for the planned n=3 repeats and
    # small adjacent counts.  Do not use a normal approximation for n=3.
    t95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}.get(n, 1.96)
    result["ci95_half_width"] = t95 * sd / math.sqrt(n)
    return result


def reference_soc_from_capacity(capacity_ah: Iterable[float], reference_capacity_ah: float,
                                start_soc_pct: float = 100.0) -> list[float]:
    """Independent SoC ruler from discharged Ah and a qualifying C10 capacity."""
    cap = float(reference_capacity_ah)
    if not _finite(cap) or cap <= 0.0:
        raise ValueError("reference_capacity_ah must be positive")
    start = float(start_soc_pct)
    return [max(0.0, min(100.0, start - float(ah) / cap * 100.0))
            for ah in capacity_ah]


def ekf_metrics(estimated_soc: Iterable[float], reference_soc: Iterable[float],
                elapsed_s: Iterable[float] | None = None,
                convergence_pct: float = 5.0, sustain_s: float = 60.0) -> dict:
    """Compute transparent EKF error and sustained-convergence metrics."""
    pairs = [(float(e), float(r)) for e, r in zip(estimated_soc, reference_soc)
             if _finite(e) and _finite(r)]
    if not pairs:
        return {"n": 0}
    errors = [e - r for e, r in pairs]
    absolute = [abs(e) for e in errors]
    result = {
        "n": len(errors),
        "mae_pct": mean(absolute),
        "rmse_pct": math.sqrt(mean(e * e for e in errors)),
        "max_error_pct": max(absolute),
        "error_pct": errors,
    }
    times = list(elapsed_s or [])
    if len(times) >= len(errors):
        times = [float(t) for t in times[:len(errors)]]
        for idx, value in enumerate(absolute):
            if value > convergence_pct:
                continue
            end = next((j for j in range(idx, len(absolute))
                        if times[j] - times[idx] >= sustain_s), None)
            if end is not None and max(absolute[idx:end + 1]) <= convergence_pct:
                result["convergence_s"] = max(0.0, times[idx] - times[0])
                break
    return result


def ssr_interruption_evidence(command_monotonic_s: float | None,
                              samples: Iterable[tuple[float, float]],
                              zero_current_a: float = 0.05) -> dict:
    """Return a sampling-bounded SSR interruption observation.

    It intentionally never claims relay switching latency: the first observed
    near-zero sample is only an upper bound at the acquisition cadence.
    """
    if command_monotonic_s is None or not _finite(command_monotonic_s):
        return {"available": False, "method": "telemetry_bounded"}
    command = float(command_monotonic_s)
    observed = next(((float(t), float(i)) for t, i in samples
                     if _finite(t) and _finite(i) and float(t) >= command
                     and abs(float(i)) <= zero_current_a), None)
    result = {"available": observed is not None, "method": "telemetry_bounded",
              "command_monotonic_s": command, "zero_current_threshold_a": zero_current_a,
              "claim": "Observed command-to-next-near-zero-current upper bound; not relay switching time."}
    if observed:
        result.update({"first_zero_sample_s": observed[0], "current_a": observed[1],
                       "upper_bound_s": max(0.0, observed[0] - command)})
    return result


def validation_verdict(campaign: dict, ambient: dict, sampling: dict,
                       c10_qualified: bool) -> dict:
    """Evidence gate used for presentation, never a battery-health grade."""
    reasons: list[str] = []
    if not campaign_is_complete(campaign):
        reasons.append("campaign identity incomplete")
    if not ambient.get("in_band", False):
        reasons.append("ambient temperature outside validation band or unavailable")
    if not c10_qualified:
        reasons.append("no qualifying C10 capacity reference")
    quality = sampling.get("quality_counts") or {}
    if quality.get("INVALID", 0):
        reasons.append("invalid telemetry samples present")
    return {"validation_ready": not reasons, "reasons": reasons}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
