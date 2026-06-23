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


# DCIR rises ~0.4 %/°C; normalise every reading to 25 °C so a battery measured at a
# warmer terminal isn't graded as artificially "better" (the bench terminal sits ~4 °C
# above ambient and self-heats during a test).
_DCIR_TEMP_COEFF = 0.004     # per °C
_T_REF = 25.0                # °C


def identify_dcir(current_a, voltage_v, temp_c, profile: BatteryProfile):
    """Repeatable single-step DCIR aggregated over EVERY current step in the record.

    At the rig's ~5 Hz SCPI readback the instantaneous ohmic step cannot be resolved
    from the RC relaxation, so a 1-RC ECM (R0/R1/C1 separation) is not identifiable
    (see ``docs/project_pivot.md``). Instead, ``R = |ΔV/ΔI|`` is read at the first
    sample after each current edge (a consistent ~200 ms readback point), each value
    is normalised to 25 °C, and the **median across all steps** is reported with its
    spread — so an HPPC record's many pulses, or repeated load on/off edges, give a
    repeatable DCIR with a measurable uncertainty instead of a single noisy number.

    Returns ``(dcir_ohm_25C, std_ohm, n_steps, measured)``. ``measured`` is False (and
    DCIR falls back to the profile baseline) when no clear current step is present.
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    tc = np.asarray(temp_c, float)
    if ia.size < 4:
        return profile.internal_r, 0.0, 0, False
    di = np.diff(ia)
    thr = max(1e-3, 0.20 * float(np.max(np.abs(ia))))     # a real load edge, not jitter
    vals = []
    k = 0
    while k < di.size:
        if abs(di[k]) > thr:
            v_before = float(np.median(va[max(0, k - 2):k + 1]))   # rested/level baseline
            v_after = float(va[k + 1])                             # first post-edge sample
            r = abs((v_after - v_before) / di[k])
            T = float(tc[k + 1]) if (k + 1 < tc.size and not np.isnan(tc[k + 1])) else _T_REF
            vals.append(r / (1.0 + _DCIR_TEMP_COEFF * (T - _T_REF)))   # → 25 °C
            k += 2                                                 # skip the paired sample
        else:
            k += 1
    if not vals:
        return profile.internal_r, 0.0, 0, False
    arr = np.asarray(vals, float)
    return float(np.median(arr)), float(np.std(arr)), int(arr.size), True


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


def _quality_flags(current_a, voltage_v, temp_c, profile, is_hppc,
                   n_steps, reached_cutoff):
    """Data-integrity checks — a sorting bench must NOT grade on bad measurements.
    Returns ``(warnings, temp_drift_c)``; an empty warning list means a clean record."""
    ia = np.abs(np.asarray(current_a, float))
    tc = np.asarray(temp_c, float)
    w = []
    # a rest segment is needed for a trustworthy OCV / SoC anchor
    head = ia[:min(ia.size, 25)]
    if head.size and int((head < 0.05).sum()) < 5:
        w.append("no clear rest before load — OCV/SoC anchor uncertain")
    if (not is_hppc) and not reached_cutoff:
        w.append("discharge did not reach cut-off — SoH is partial/under-stated")
    if n_steps == 0:
        w.append("no clear current step — DCIR fell back to profile baseline")
    temp_drift = 0.0
    if tc.size and not np.all(np.isnan(tc)):
        temp_drift = float(np.nanmax(tc) - np.nanmin(tc))
        if temp_drift > 8.0:
            w.append(f"terminal temperature drifted {temp_drift:.1f} °C during the test")
    return w, temp_drift


def _confidence(dcir_ohm, dcir_std, n_steps, profile, n_warnings):
    """0..1 grade confidence from (a) DCIR repeatability across steps, (b) distance to
    the nearest grade boundary, and (c) data-quality warnings. Transparent, not learned."""
    conf = 1.0
    if n_steps >= 2 and dcir_ohm > 0:
        rel = dcir_std / dcir_ohm                     # coefficient of variation
        conf *= max(0.3, 1.0 - min(1.0, 3.0 * rel))   # ~33 % spread → floor
    elif n_steps <= 1:
        conf *= 0.7                                   # single reading — can't assess spread
    ratio = dcir_ohm / max(1e-6, profile.internal_r)  # grade boundaries live at 1.3/1.7/2.5
    d = min(abs(ratio - b) for b in (1.3, 1.7, 2.5))
    conf *= min(1.0, 0.5 + d)                          # sitting on a boundary → less sure
    conf *= max(0.2, 1.0 - 0.15 * n_warnings)
    return float(max(0.0, min(1.0, conf)))


def analyze_series(time_s, current_a, voltage_v, temp_c, capacity_series,
                   profile: BatteryProfile, is_hppc: bool, soh=None) -> dict:
    """Run the unified analysis on raw series → the standard results dict.

    ``current_a`` discharge-positive; ``capacity_series`` is the per-sample cumulative
    Ah; ``soh`` may be supplied (live estimator) else computed from capacity ÷ rated.

    Resistance is a temperature-normalised, multi-step DCIR (mean ± spread); grading
    uses SoH + DCIR + the lead-acid load metrics, with a confidence score and
    data-quality flags so suspect measurements are surfaced rather than silently graded."""
    v = np.asarray(voltage_v, float)
    q = np.asarray(capacity_series, float)
    capacity = float(q[-1]) if q.size else 0.0
    reached_cutoff = bool(v.size and float(np.min(v)) <= profile.cutoff_v * 1.02)
    # SoH = measured capacity ÷ rated is ONLY valid for a FULL discharge (100% → cut-off).
    # HPPC moves little charge, and a discharge stopped early under-states capacity — both
    # would yield a misleading SoH, so SoH is N/A (NaN) unless a full discharge was seen
    # (or the caller supplied a SoH). Grading then falls back to resistance alone.
    if soh is None:
        if (not is_hppc) and reached_cutoff and profile.capacity_ah:
            soh = 100.0 * capacity / profile.capacity_ah
        else:
            soh = float("nan")
    if not np.isnan(soh):
        soh = float(min(120.0, max(0.0, soh)))

    dcir, dcir_std, n_steps, measured = identify_dcir(current_a, voltage_v, temp_c, profile)
    sag, cca_est, ocv = _load_metrics(current_a, voltage_v, dcir, profile)
    warnings, temp_drift = _quality_flags(current_a, voltage_v, temp_c, profile,
                                          is_hppc, n_steps, reached_cutoff)

    gradeable = measured or not np.isnan(soh)
    grade = Analytics.grade(soh, dcir, profile) if gradeable else "REVIEW"
    confidence = _confidence(dcir, dcir_std, n_steps, profile, len(warnings))
    if not gradeable:
        confidence = 0.0

    ica_v, ica = Analytics.incremental_capacity(v, q)
    return {
        "soh": soh, "capacity_ah": capacity,
        "dcir_mohm": dcir * 1000.0, "dcir_std_mohm": dcir_std * 1000.0,
        "dcir_n_steps": n_steps, "dcir_measured": measured, "dcir_temp_normalised": True,
        "ri_mohm": dcir * 1000.0,
        "voltage_sag_v": sag, "cca_est_a": cca_est, "ocv_v": ocv,
        "grade": grade, "gradeable": gradeable,
        "confidence": confidence, "quality_warnings": warnings, "temp_drift_c": temp_drift,
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
