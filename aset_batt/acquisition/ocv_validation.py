"""Shared validity rules for rested OCV observations.

OCV is only a valid SoC anchor when outputs are off, measured current is near
zero, temperature and samples are valid, rest duration is sufficient, and the
recent voltage window is stable.  This module is intentionally hardware-free.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

OCV_MAX_ABS_CURRENT_A = 0.10  # tolerate instrument zero/noise; reject meaningful residual current
OCV_STATUS_VALID = "VALID_OCV"
OCV_STATUS_NOT_RESTED = "NOT_RESTED"
OCV_STATUS_CURRENT_NOT_ZERO = "CURRENT_NOT_ZERO"
OCV_STATUS_VOLTAGE_UNSTABLE = "VOLTAGE_UNSTABLE"
OCV_STATUS_TIMEOUT = "TIMEOUT"
OCV_STATUS_OUT_OF_RANGE = "OUT_OF_RANGE"
OCV_STATUS_MEASUREMENT_INVALID = "MEASUREMENT_INVALID"
QUICK_OCV_MIN_REST_S = 180.0
QUICK_OCV_MAX_REST_S = 600.0
QUICK_OCV_WINDOW_S = 60.0
QUICK_OCV_MAX_SPREAD_V = 0.010


@dataclass(frozen=True)
class OCVAnchor:
    """Traceable rested-voltage SoC anchor.

    ``valid`` is deliberately explicit: a voltage value is not an OCV anchor
    until the rest/current/stability policy has accepted the observation.
    """
    voltage: float | None
    temperature: float | None
    soc_fraction: float | None
    valid: bool
    reason: str
    rest_duration_s: float = 0.0
    window_duration_s: float = 0.0
    voltage_spread_v: float | None = None
    measured_current_a: float | None = None
    source: str = "RESTED_OCV"


@dataclass(frozen=True)
class C10ReferenceResult:
    measured_capacity_ah: float | None
    verified_c10_capacity_ah: float | None
    soh_pct: float | None
    valid: bool
    status: str
    reason: str


def evaluate_c10_reference(*, measured_capacity_ah: float | None,
                           full_charge_confirmed: bool,
                           reference_current_a: float,
                           mean_discharge_current_a: float,
                           current_tolerance_a: float,
                           reached_cutoff: bool,
                           data_valid: bool,
                           rated_c10_capacity_ah: float | None) -> C10ReferenceResult:
    """Gate direct C10 capacity/SoH; never repairs an invalid test."""
    try:
        q = float(measured_capacity_ah)
        mean_i = float(mean_discharge_current_a)
        rated = float(rated_c10_capacity_ah)
    except (TypeError, ValueError):
        q = mean_i = rated = float("nan")
    reasons = []
    if not math.isfinite(q) or q < 0: reasons.append("MEASUREMENT_INVALID")
    if not full_charge_confirmed: reasons.append("C10_REFERENCE_INVALID_START_CONDITION")
    if not math.isfinite(mean_i) or abs(mean_i - reference_current_a) > abs(current_tolerance_a):
        reasons.append("REFERENCE_CURRENT_INVALID")
    if not reached_cutoff: reasons.append("CUTOFF_NOT_REACHED")
    if not data_valid: reasons.append("DATA_INVALID")
    if not math.isfinite(rated) or rated <= 0: reasons.append("RATED_CAPACITY_UNAVAILABLE")
    valid = not reasons
    return C10ReferenceResult(q, q if valid else None,
                              100.0 * q / rated if valid else None,
                              valid, "VALID_C10_REFERENCE" if valid else reasons[0],
                              "OK" if valid else "; ".join(reasons))


def evaluate_quick_ocv_window(samples, *, outputs_off: bool,
                              max_abs_current_a: float = OCV_MAX_ABS_CURRENT_A,
                              now_s: float | None = None) -> dict:
    """Apply Quick Scan's bounded 180–600 s, 60 s stable OCV policy."""
    elapsed = float(now_s if now_s is not None else
                    (samples[-1][0] if samples else 0.0))
    if elapsed >= QUICK_OCV_MAX_REST_S:
        result = evaluate_ocv_window(
            samples, outputs_off=outputs_off,
            min_rest_s=QUICK_OCV_MIN_REST_S, window_s=QUICK_OCV_WINDOW_S,
            max_spread_v=QUICK_OCV_MAX_SPREAD_V,
            max_abs_current_a=max_abs_current_a, now_s=elapsed)
        if not result["valid"]:
            result["status"] = "OCV_TIMEOUT"
        return result
    return evaluate_ocv_window(
        samples, outputs_off=outputs_off,
        min_rest_s=QUICK_OCV_MIN_REST_S, window_s=QUICK_OCV_WINDOW_S,
        max_spread_v=QUICK_OCV_MAX_SPREAD_V,
        max_abs_current_a=max_abs_current_a, now_s=elapsed)


def evaluate_ocv_window(samples, *, outputs_off: bool, min_rest_s: float,
                         window_s: float, max_spread_v: float,
                         max_abs_current_a: float = OCV_MAX_ABS_CURRENT_A,
                         now_s: float | None = None) -> dict:
    """Evaluate ``(elapsed_s, voltage_v, current_a, temperature_c, valid)`` rows."""
    rows = list(samples or [])
    if not outputs_off or not rows:
        return {"valid": False, "status": OCV_STATUS_MEASUREMENT_INVALID,
                "voltage_v": None, "rest_s": 0.0, "voltage_window_v": None,
                "max_abs_current_a": None, "temperature_c": None}
    try:
        end_s = float(rows[-1][0] if now_s is None else now_s)
        tail = [r for r in rows if end_s - float(r[0]) <= window_s]
        vals = [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), bool(r[4])) for r in tail]
    except (ValueError, TypeError, IndexError):
        vals = []
        end_s = 0.0
    if not vals or any(not r[4] or not all(math.isfinite(x) for x in r[:4]) for r in vals):
        return {"valid": False, "status": OCV_STATUS_MEASUREMENT_INVALID,
                "voltage_v": None, "rest_s": 0.0, "voltage_window_v": None,
                "max_abs_current_a": None, "temperature_c": None}
    first_s = float(rows[0][0])
    rest_s = max(0.0, end_s - first_s)
    vs = [r[1] for r in vals]
    max_i = max(abs(r[2]) for r in vals)
    out = {"valid": False, "status": OCV_STATUS_VALID, "voltage_v": vs[-1],
           "rest_s": rest_s, "voltage_window_v": max(vs) - min(vs),
           "max_abs_current_a": max_i, "temperature_c": vals[-1][3]}
    if max_i > max_abs_current_a:
        out["status"] = OCV_STATUS_CURRENT_NOT_ZERO
    elif rest_s < min_rest_s or len(vals) < 3 or end_s - vals[0][0] < window_s:
        out["status"] = OCV_STATUS_NOT_RESTED
    elif out["voltage_window_v"] >= max_spread_v:
        out["status"] = OCV_STATUS_VOLTAGE_UNSTABLE
    else:
        out["valid"] = True
    return out


def estimate_full_capacity(q_removed_ah: float, soc_start_pct: float | None,
                           soc_end_pct: float | None, *, start_valid: bool,
                           end_valid: bool, min_soc_span: float = 0.20,
                           rated_capacity_ah: float | None = None) -> dict:
    """Conservative OCV-normalized estimate; never substitutes a missing endpoint."""
    if not start_valid or soc_start_pct is None:
        return {"capacity_ah": None, "status": "START_OCV_NOT_VALID", "valid": False}
    if not end_valid or soc_end_pct is None:
        return {"capacity_ah": None, "status": "END_OCV_NOT_VALID", "valid": False}
    try:
        q = float(q_removed_ah)
        start, end = float(soc_start_pct) / 100.0, float(soc_end_pct) / 100.0
    except (TypeError, ValueError):
        return {"capacity_ah": None, "status": "MEASUREMENT_INVALID", "valid": False}
    span = start - end
    if not all(math.isfinite(x) for x in (q, start, end)) or q < 0.0:
        return {"capacity_ah": None, "status": "MEASUREMENT_INVALID", "valid": False}
    if span <= 0.0:
        return {"capacity_ah": None, "status": "INVALID_SOC_ORDER", "valid": False}
    if span < min_soc_span:
        return {"capacity_ah": None, "status": "SOC_SPAN_TOO_SMALL", "valid": False}
    estimate = q / span
    if rated_capacity_ah and (estimate > rated_capacity_ah * 1.5 or estimate < rated_capacity_ah * 0.2):
        return {"capacity_ah": None, "status": "ESTIMATE_OUT_OF_BOUNDS", "valid": False}
    return {"capacity_ah": estimate, "status": "ESTIMATED", "valid": True}
