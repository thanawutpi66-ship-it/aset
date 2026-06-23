"""
Single, unified post-test analysis for the whole application.

Every grade in the **running app** — the live characterization test, the "Analyze
CSV" button, and the IEC-profile auto-analyze — goes through :func:`analyze_series` /
:func:`analyze_csv`, so the live grading path is exactly ONE method.

(``aset_batt.core.analysis_module`` contains a *separate* ML/heuristic grader with
its own A/B/C/D scale; it is NOT wired into live grading — it backs the offline
training scripts, report_generator, and its own tests. See that module's header.)

The single live method below provides:

  * R0/R1/C1/τ via the 1-RC Thevenin ECM identifier (on HPPC pulses);
    single-point ohmic fallback otherwise.
  * ICA (dQ/dV) and DTV (dT/dV), Gaussian-smoothed.
  * Two-resistance grading (R0 ohmic + R1 charge-transfer).

Current convention: **discharge POSITIVE** (project canonical), matching
``data_utils.DataHandler`` / the worker's CSV ``Current_A`` column.
"""
from __future__ import annotations

import os
import csv
import logging

import numpy as np

from aset_batt.acquisition.analytics import Analytics
from aset_batt.acquisition.models import BatteryProfile

logger = logging.getLogger(__name__)


def profile_from_config(config) -> BatteryProfile:
    """Build the analysis profile (pack limits + safety window + baseline Rᵢ) from
    the application config. Shared by the GUI and the controller's auto-analyze."""
    b = config.battery
    s = config.system.safety_limits or {}
    try:
        from aset_batt.core.battery_model import BatteryModel
        rin = BatteryModel(b.battery_type, b.nominal_voltage,
                           b.cells_series, b.cells_parallel).base_rin
    except Exception:
        rin = 0.03
    otp = float(s.get("max_temperature", 55.0))
    return BatteryProfile(
        name=b.battery_type, chemistry=b.battery_type,
        nominal_v=b.pack_nominal_voltage, series=b.cells_series,
        capacity_ah=b.rated_capacity,
        max_charge_v=b.pack_max_voltage, cutoff_v=b.pack_min_voltage,
        max_charge_a=b.max_current, max_discharge_a=b.max_current,
        ovp=float(s.get("max_voltage", b.pack_max_voltage + 1)),
        uvp=float(s.get("min_voltage", b.pack_min_voltage - 1)),
        otp_warn=max(0.0, otp - 10.0), otp_crit=otp, internal_r=float(max(1e-4, rin)),
    )


def identify_dcir(current_a, voltage_v, profile: BatteryProfile):
    """Single-step DC internal resistance: ``R = |ΔV / ΔI|`` at the largest current step.

    At the rig's ~5 Hz SCPI readback the instantaneous ohmic step cannot be resolved
    from the RC relaxation, so a 1-RC ECM (separating R0 / R1 / C1) is **not
    identifiable** on this hardware — see ``docs/project_pivot.md``. A single-step
    DCIR is the correct, repeatable resistance metric at this sample rate and is what
    grading uses.

    Returns ``(dcir_ohm, measured)``. ``measured`` is False (and DCIR falls back to the
    profile baseline) when no clear current step is present.
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    if ia.size > 3:
        di = np.diff(ia)
        k = int(np.argmax(np.abs(di)))
        if abs(di[k]) > 1e-3:
            # median baseline before the step (jitter-robust); first sample after it
            v_before = float(np.median(va[max(0, k - 2):k + 1]))
            v_after = float(va[k + 1])
            return abs((v_after - v_before) / di[k]), True
    return profile.internal_r, False


def _load_metrics(current_a, voltage_v, dcir_ohm, profile: BatteryProfile):
    """Lead-acid health features the rig CAN measure at 5 Hz (see project pivot §3,§8.5):

      * ``voltage_sag_v`` — rested OCV minus the lowest terminal voltage seen under
        load. A weak/sulfated battery sags much more for the same current.
      * ``cca_est_a`` — cranking-capability proxy = (OCV − cutoff) / DCIR, i.e. the
        current at which the terminal would sag to the discharge cutoff. Not a
        standardised CCA (that needs a cold high-rate crank), but a repeatable,
        physically grounded surrogate for sorting.
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    rest = np.abs(ia) < 0.05
    ocv = float(np.median(va[rest])) if rest.any() else float(np.max(va)) if va.size else 0.0
    under_load = np.abs(ia) >= max(0.1, 0.05 * float(np.max(np.abs(ia))) if ia.size else 0.1)
    v_min_load = float(np.min(va[under_load])) if under_load.any() else ocv
    sag = max(0.0, ocv - v_min_load)
    cca_est = (ocv - profile.cutoff_v) / dcir_ohm if dcir_ohm > 1e-6 else 0.0
    return sag, max(0.0, cca_est), ocv


def analyze_series(time_s, current_a, voltage_v, temp_c, capacity_series,
                   profile: BatteryProfile, is_hppc: bool, soh=None) -> dict:
    """Run the unified analysis on raw series → the standard results dict.

    ``current_a`` discharge-positive; ``capacity_series`` is the per-sample cumulative
    Ah; ``soh`` may be supplied (live estimator) else computed from capacity ÷ rated.

    Resistance comes from a single-step DCIR (not a 1-RC ECM — unidentifiable at 5 Hz);
    grading uses SoH + DCIR + the lead-acid load metrics (voltage-sag, CCA proxy)."""
    v = np.asarray(voltage_v, float)
    q = np.asarray(capacity_series, float)
    capacity = float(q[-1]) if q.size else 0.0
    # SoH = measured capacity ÷ rated is ONLY valid for a full discharge (100% → cutoff).
    # An HPPC pulse test moves only a little charge, so its throughput is NOT a capacity
    # measurement — reporting capacity/rated there yields a meaningless ~0% SoH. When the
    # caller does not supply a SoH and the test is not a full discharge, SoH is N/A (NaN)
    # and grading falls back to resistance alone.
    if soh is None:
        if is_hppc or not profile.capacity_ah:
            soh = float("nan")
        else:
            soh = 100.0 * capacity / profile.capacity_ah
    if not np.isnan(soh):
        soh = float(min(120.0, max(0.0, soh)))

    dcir, measured = identify_dcir(current_a, voltage_v, profile)
    sag, cca_est, ocv = _load_metrics(current_a, voltage_v, dcir, profile)
    grade = Analytics.grade(soh, dcir, profile)

    ica_v, ica = Analytics.incremental_capacity(v, q)
    return {
        "soh": soh, "capacity_ah": capacity,
        "dcir_mohm": dcir * 1000.0, "ri_mohm": dcir * 1000.0,
        "dcir_measured": measured,
        "voltage_sag_v": sag, "cca_est_a": cca_est, "ocv_v": ocv,
        "grade": grade,
        # legacy keys kept for consumers; 1-RC ECM is no longer identified at 5 Hz
        "r0_mohm": dcir * 1000.0, "r1_mohm": 0.0, "c1_farad": 0.0, "tau_s": 0.0,
        "ecm_identified": False,
        "ica": (ica_v, ica),
    }


def _read_csv(path):
    """Read a canonical (or lowercase) telemetry CSV → arrays + mode strings."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        hdr = {h.strip().lower(): h for h in (reader.fieldnames or [])}

        def col(name):
            return hdr.get(name.lower())

        c_t, c_v, c_i = col("Elapsed_s"), col("Voltage_V"), col("Current_A")
        c_temp, c_cap, c_mode = col("Temperature_C"), col("Capacity_Ah"), col("Mode")
        T, V, I, TEMP, CAP, modes = [], [], [], [], [], []
        for r in reader:
            def num(c, default=float("nan")):
                try:
                    return float(r[c]) if c else default
                except (ValueError, TypeError, KeyError):
                    return default
            T.append(num(c_t)); V.append(num(c_v)); I.append(num(c_i))
            TEMP.append(num(c_temp, 25.0)); CAP.append(num(c_cap))
            modes.append(r[c_mode] if c_mode else "")
    return (np.asarray(T, float), np.asarray(V, float), np.asarray(I, float),
            np.asarray(TEMP, float), np.asarray(CAP, float), modes)


def analyze_csv(csv_path: str, profile: BatteryProfile) -> dict:
    """Parse a telemetry CSV and run the unified analysis. HPPC is inferred from
    the ``Mode`` column; capacity is integrated from current if not logged."""
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path or "(no CSV)")
    t, v, i, temp, cap, modes = _read_csv(csv_path)
    if t.size < 2:
        raise ValueError("CSV has too few samples to analyse.")
    is_hppc = any("hppc" in (m or "").lower() for m in modes)
    if np.all(np.isnan(cap)):                       # no capacity column → integrate
        dt = np.diff(t, prepend=t[0])
        cap = np.cumsum(np.clip(i, 0, None) * dt) / 3600.0
    return analyze_series(t, i, v, temp, cap, profile, is_hppc)
