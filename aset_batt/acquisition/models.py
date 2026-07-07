"""Acquisition value objects — operation modes, battery test profile, test config,
and dynamic profile loading. Shared by the worker, backends, and the command-center UI."""
from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class OperationMode(Enum):
    CC_CV_CHARGE = "CC-CV Charge"
    CC_DISCHARGE = "Constant Current Discharge"
    HPPC = "HPPC Pulse Test"


@dataclass
class BatteryProfile:
    """Test profile for one battery model — pack-level limits + safety interlocks."""
    name: str
    chemistry: str
    nominal_v: float
    series: int
    capacity_ah: float
    max_charge_v: float
    cutoff_v: float
    max_charge_a: float
    max_discharge_a: float
    ovp: float           # over-voltage trip
    uvp: float           # under-voltage trip
    otp_warn: float      # over-temperature warning
    otp_crit: float      # over-temperature critical (triggers E-Stop)
    internal_r: float = 0.03
    # Per-product override of the R0/R1 split used by grade_from_ecm (see Analytics.
    # R0_FRACTION/R1_FRACTION). 0.0 = not calibrated for this product → fall back to the
    # chemistry-agnostic 0.6/0.4 default. Set via a product's measured_params
    # (battery_profiles.json) once a confirmed-good specimen has been characterised.
    r0_fraction: float = 0.0
    # Test-rig cabling/contact resistance (Ω, pack-level), subtracted from the
    # measured DCIR/dcir_slope/ECM-R0 before grading — see BatteryConfig
    # .harness_resistance_ohm. R1/C1/τ are NOT touched: wiring resistance is a simple
    # series resistor, not the cell's own charge-transfer/polarization dynamics.
    harness_r_ohm: float = 0.0
    # HPPC timing — pulse should be ≳ 3·τ and the relaxation long enough to capture
    # the full RC tail, so R1/C1 are not truncated/under-resolved by a short pulse.
    hppc_pulse_duration: float = 30.0       # seconds of constant-current load
    hppc_relaxation_duration: float = 30.0  # seconds of rest (relaxation tail) per cycle
    hppc_pulse_crate: float = 1.0           # C-rate for pulse current (× capacity_ah)
    # Peukert exponent — how strongly available capacity falls with discharge rate.
    # ~1.0–1.05 for lithium (almost rate-independent), ~1.15–1.30 for lead-acid.
    # Used to normalise measured capacity to a reference C-rate before SoH.
    peukert_k: float = 1.10


@dataclass
class TestConfig:
    profile: BatteryProfile
    mode: OperationMode
    # Target loop rate. The real ceiling is the instruments' SCPI readback (~5 Hz on
    # the GW Instek PSW/PEL over USB: ~100 ms per MEAS query, and we query two per
    # sample), so the effective rate is ~3–5 Hz regardless of a higher setting here.
    # Timing uses the measured per-sample dt (perf_counter), so this is only a target.
    sample_hz: float = 5.0


# Not a pytest test class (name starts with "Test") — tell the collector to skip it.
TestConfig.__test__ = False


_FALLBACK = {
    "LiFePO4 25.6V (8S, 50Ah)": BatteryProfile(
        "LiFePO4 25.6V (8S, 50Ah)", "LiFePO4", 25.6, 8, 50.0,
        29.2, 20.0, 25.0, 50.0, 30.0, 18.0, 45.0, 55.0, 0.030),
    "Lead-Acid 12V (6S, 7Ah)": BatteryProfile(
        "Lead-Acid 12V (6S, 7Ah)", "Lead-Acid", 12.0, 6, 7.0,
        14.4, 10.5, 1.4, 7.0, 15.0, 10.0, 45.0, 55.0, 0.030),
}


def load_profiles(path: str = "command_center_profiles.json") -> dict[str, BatteryProfile]:
    """Load test profiles from an external JSON structure (cwd-relative) with a
    built-in fallback so the bench always has at least two profiles."""
    if not os.path.exists(path):
        logger.warning("profiles file '%s' missing — using built-in fallback", path)
        return dict(_FALLBACK)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = {name: BatteryProfile(name=name, **d)
               for name, d in data.get("profiles", {}).items()}
        return out or dict(_FALLBACK)
    except Exception as e:
        logger.error("profile load failed (%s) — fallback", e)
        return dict(_FALLBACK)
