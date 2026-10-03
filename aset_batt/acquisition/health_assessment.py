"""Transparent, configurable Quick Scan health assessment.

References and grade thresholds are project-defined configuration. Missing
reference evidence withholds the composite score and condition grade.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "quick_health_config.json"
DEFAULT_WEIGHTS = {"soh": 0.60, "dcir": 0.30, "recovery": 0.10}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate project grading config without silently repairing it."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    weights = config.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError("weights must define exactly soh, dcir, and recovery")
    values = [float(weights[name]) for name in DEFAULT_WEIGHTS]
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("weights must be finite and non-negative")
    if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("Quick Scan health weights must sum to 1.0")
    for candidate in config.get("sensitivity_weight_sets", []):
        candidate_values = [float(candidate[name]) for name in DEFAULT_WEIGHTS]
        if (any(not math.isfinite(value) or value < 0.0 for value in candidate_values)
                or not math.isclose(sum(candidate_values), 1.0,
                                    rel_tol=0.0, abs_tol=1e-9)):
            raise ValueError("every sensitivity weight set must be non-negative and sum to 1.0")
    thresholds = config.get("grade_thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("grade_thresholds must be an object")
    try:
        a, b, c = (float(thresholds[name]) for name in ("A", "B", "C"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("grade_thresholds must define A, B, and C") from exc
    if not (100.0 >= a >= b >= c >= 0.0):
        raise ValueError("grade thresholds must satisfy 100 >= A >= B >= C >= 0")
    return config


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def classify_health(score: float, thresholds: dict[str, Any]) -> str:
    """Map a valid composite score to project-defined condition classes."""
    if score >= float(thresholds["A"]):
        return "A"
    if score >= float(thresholds["B"]):
        return "B"
    if score >= float(thresholds["C"]):
        return "C"
    return "REJECT"


def assess_quick_health(
    *,
    quick_soh_pct: Any,
    dcir_ohm: Any,
    recovery_metric: Any,
    profile_config: dict[str, Any] | None,
    temperature_c: Any = None,
    quality_flags: list[str] | tuple[str, ...] = (),
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate component scores and a weighted result, or withhold on any gate.

    Recovery is treated as higher-is-healthier only when profile config explicitly
    supplies healthy/EOL references for the configured metric.
    """
    config = config or load_config()
    profile_config = profile_config or {}
    weights = {key: float(value) for key, value in config["weights"].items()}
    if (set(weights) != set(DEFAULT_WEIGHTS)
            or any(not math.isfinite(value) or value < 0.0 for value in weights.values())
            or not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9)):
        raise ValueError("Quick Scan health weights must be non-negative and sum to 1.0")
    raw_soh = _number(quick_soh_pct)
    raw_dcir = _number(dcir_ohm)
    raw_recovery = _number(recovery_metric)
    temperature = _number(temperature_c)
    soh_score = min(100.0, max(0.0, raw_soh)) if raw_soh is not None else None
    dcir_healthy = _number(profile_config.get("dcir_healthy_ohm"))
    dcir_eol = _number(profile_config.get("dcir_eol_ohm"))
    recovery_healthy = _number(profile_config.get("recovery_healthy"))
    recovery_eol = _number(profile_config.get("recovery_eol"))
    temperature_min = _number(profile_config.get("valid_test_temperature_min_c"))
    temperature_max = _number(profile_config.get("valid_test_temperature_max_c"))
    dcir_score = None
    if (raw_dcir is not None and raw_dcir >= 0.0 and dcir_healthy is not None
            and dcir_eol is not None and dcir_eol > dcir_healthy):
        dcir_score = 100.0 * min(1.0, max(0.0,
            (dcir_eol - raw_dcir) / (dcir_eol - dcir_healthy)))
    recovery_score = None
    if (raw_recovery is not None and recovery_healthy is not None
            and recovery_eol is not None and recovery_healthy > recovery_eol):
        recovery_score = 100.0 * min(1.0, max(0.0,
            (raw_recovery - recovery_eol) / (recovery_healthy - recovery_eol)))

    reasons = [str(flag) for flag in quality_flags]
    if temperature_min is not None or temperature_max is not None:
        if temperature is None:
            reasons.append("test temperature unavailable for configured validity range")
        elif ((temperature_min is not None and temperature < temperature_min)
              or (temperature_max is not None and temperature > temperature_max)):
            reasons.append("test temperature is outside the configured validity range")
    if raw_soh is None:
        reasons.append("Quick SoH unavailable or invalid")
    if dcir_score is None:
        reasons.append("DCIR score unavailable: measured value or profile references missing")
    if recovery_score is None:
        reasons.append("Recovery score unavailable: metric or profile references missing")
    scores = {"soh": soh_score, "dcir": dcir_score, "recovery": recovery_score}
    contributions = {key: (weights[key] * score if score is not None else None)
                     for key, score in scores.items()}
    valid = not reasons and all(value is not None for value in scores.values())
    health = sum(contributions.values()) if valid else None
    grade = classify_health(health, config["grade_thresholds"]) if health is not None else "INVALID"
    return {
        "health_assessment_valid": valid,
        "health_assessment_status": "VALID" if valid else "INVALID",
        "health_assessment_reason": "; ".join(reasons) if reasons else "All required components and quality gates passed",
        "health_score_quick": health,
        "condition_grade": grade,
        "recommended_action": ("Full C10 Capacity Test recommended for verification"
                               if grade in {"C", "REJECT"} else
                               "Quick Scan screening only; reference capacity not yet verified"
                               if valid else "RETEST: required measurement/reference or validity gate unavailable"),
        "score_soh": soh_score,
        "score_dcir": dcir_score,
        "score_recovery": recovery_score,
        "weight_soh": weights["soh"],
        "weight_dcir": weights["dcir"],
        "weight_recovery": weights["recovery"],
        "contribution_soh": contributions["soh"],
        "contribution_dcir": contributions["dcir"],
        "contribution_recovery": contributions["recovery"],
        "dcir_ratio": raw_dcir / dcir_healthy if raw_dcir is not None and dcir_healthy else None,
        "dcir_delta_pct": ((raw_dcir - dcir_healthy) / dcir_healthy * 100.0
                           if raw_dcir is not None and dcir_healthy else None),
        "grading_algorithm_version": config.get("algorithm_version", "multi-parameter-rule-v1"),
    }
