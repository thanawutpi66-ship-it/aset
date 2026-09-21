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
import json
import logging
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from aset_batt.acquisition.analytics import Analytics
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.core.battery_model import (
    is_plausible_r0, MAX_STEP_EDGE_LATENCY_S, STEADY_STATE_MAX_SPREAD_V,
)

logger = logging.getLogger(__name__)


def profile_from_config(config) -> BatteryProfile:
    """Build the analysis profile (pack limits + safety window + baseline Rᵢ) from
    the application config. Shared by the GUI and the controller's auto-analyze."""
    b = config.battery
    product_name = getattr(b, "product_name", "") or ""
    s = config.system.safety_limits or {}
    try:
        from aset_batt.core.battery_model import BatteryModel
        rin_r0_only = BatteryModel(b.battery_type, b.nominal_voltage,
                                   b.cells_series, b.cells_parallel).base_rin
        from aset_batt.acquisition.analytics import Analytics
        rin = rin_r0_only / Analytics.R0_FRACTION
    except Exception:
        rin = 0.05
    otp = float(s.get("max_temperature", 55.0))
    # Peukert exponent: read from the SAME chemistry registry the live estimator
    # uses (aset_batt.core.battery_profiles), with a product-specific override if
    # one is set — NOT a hardcoded 1.20/1.05 split. That hardcode used to silently
    # disagree with the registry's own LeadAcid default (1.10, "AGM 1.05-1.15;
    # flooded 1.2-1.6" — see battery_profiles.json's own comment): live SoC used
    # 1.10 but this post-hoc SoH/grading path used 1.20, a real AGM product's SoH
    # differing by >13 points (80.0% vs 93.4%, potentially the difference between
    # grade A and B) depending purely on which code path computed it.
    from aset_batt.core import battery_profiles
    chemistry_profile = battery_profiles.get_chemistry(b.battery_type)
    peukert = chemistry_profile.peukert_k
    peukert_hr = float(getattr(chemistry_profile, "peukert_hr", 10.0))
    try:
        prod = battery_profiles.get_product(product_name)
        if prod and getattr(prod, "peukert_k", 0.0) > 0.0:
            peukert = prod.peukert_k
        if prod and getattr(prod, "peukert_hr", 0.0) > 0.0:
            peukert_hr = float(prod.peukert_hr)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    try:
        product = battery_profiles.get_product(product_name)
    except Exception:
        product = None
    peukert_settings = battery_profiles.resolve_peukert_parameters(
        product_name, b.battery_type)
    peukert = peukert_settings["peukert_k"]
    peukert_hr = peukert_settings["peukert_reference_hr"]
    # A product with an explicit 10-hour rating has a canonical C10 basis.
    # Do not allow stale saved/UI rated Ah (often the 20-hour display value) to
    # become the denominator for 1C, Peukert, SoH, or capacity grading.
    reference_capacity_ah = (float(product.capacity_10h_ah)
                             if product is not None and product.capacity_10h_ah > 0.0
                             else float(b.rated_capacity))
    peukert_reference_current_a = (
        reference_capacity_ah / peukert_hr if peukert_hr > 0.0 else None)

    # A characterised specimen's R0/R1 (see aset_batt.core.battery_profiles.
    # save_measured_params) overrides the chemistry-generic base_rin/60-40 split for
    # this specific product — the chemistry-level r0 has no capacity/CCA scaling, so a
    # small pack can measure well above it even when genuinely healthy.
    r0_fraction = 0.0
    try:
        mp = battery_profiles.get_measured_params(product_name)
        internal_r_ohm = mp.get("internal_r_ohm")
        if internal_r_ohm and float(internal_r_ohm) > 0:
            rin = float(internal_r_ohm)
            r0_fraction = float(mp.get("r0_fraction", 0.0))
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    product = None
    try:
        product = battery_profiles.get_product(product_name)
    except Exception:
        product = None
    return BatteryProfile(
        name=product_name or b.battery_type, chemistry=b.battery_type,
        nominal_v=b.pack_nominal_voltage, series=b.cells_series,
        capacity_ah=reference_capacity_ah,
        max_charge_v=b.pack_max_voltage, cutoff_v=b.pack_min_voltage,
        max_charge_a=b.max_current, max_discharge_a=b.max_current,
        ovp=float(s.get("max_voltage", b.pack_max_voltage + 1)),
        uvp=float(s.get("min_voltage", b.pack_min_voltage - 1)),
        otp_warn=max(0.0, otp - 10.0), otp_crit=otp, internal_r=float(max(1e-4, rin)),
        peukert_k=peukert, peukert_k_source=peukert_settings["peukert_k_source"],
        peukert_hr=peukert_hr,
        peukert_reference_hr=peukert_hr,
        peukert_reference_current_a=peukert_reference_current_a,
        r0_fraction=r0_fraction,
        harness_r_ohm=max(0.0, float(getattr(b, "harness_resistance_ohm", 0.0))),
        capacity_10h_ah=float(getattr(product, "capacity_10h_ah", 0.0) or 0.0),
        capacity_20h_ah=float(getattr(product, "capacity_20h_ah", 0.0) or 0.0),
        capacity_rating_basis=str(getattr(product, "capacity_rating_basis", "UNKNOWN")),
        capacity_rating_validated=(bool(getattr(product, "capacity_10h_ah", 0.0) > 0.0)
                                   if product is not None else None),
    )


# identify_dcir() normalises every reading to 25 °C (see _dcir_temp_normalizer) so a
# battery measured at a warmer terminal isn't graded as artificially "better" (the
# bench terminal sits ~4 °C above ambient and self-heats during a test). The primary
# path is chemistry-specific Arrhenius (BatteryModel.temp_rin_multiplier, matching the
# Rin baseline's own temperature model); this flat coefficient is now ONLY the
# defensive fallback used if a BatteryModel can't be constructed for the profile's
# chemistry.
_DCIR_TEMP_COEFF = 0.004     # per °C — fallback only, see _dcir_temp_normalizer
_T_REF = 25.0                # °C
# Rest/standby current baseline. Was 0.6 A to compensate for PSU quiescent bleed
# before the SSR (ESP32 GPIO16) fully disconnected the load — now 0.0 A, matching
# StateEstimator.standby_current (aset_batt/core/state_estimator.py). Left at 0.6
# here (this module's only, separate from the live estimator) meant the ±0.15 A
# rest-detection band coincided with a LeadAcid bulk-charge current (~0.1C ≈ 0.5 A
# for a generic ~5.0 Ah pack), so bulk-charge samples were misread as "rest" and their
# elevated (absorption-stage) voltage leaked into the ECM fit's OCV anchor.
_I_STANDBY = 0.0             # A

# DCIR reads R = ΔV/ΔI at the FIRST sample after a current edge, assuming that sample
# lands at the rig's steady ~200 ms readback. If the interval to that sample is much
# longer (USB hiccup, SCPI stall, OS jitter), the voltage has already relaxed into the
# RC region and R would include R1 (polarisation), not just R0 (ohmic) — inflating DCIR.
# Steps whose post-edge dt exceeds this are dropped and flagged rather than trusted.
# Shared with StateEstimator._STEP_MAX_DT_S (the live/online method reading the
# same physical rig) via battery_model.MAX_STEP_EDGE_LATENCY_S.
_DCIR_MAX_STEP_DT = MAX_STEP_EDGE_LATENCY_S      # s
MAX_CAPACITY_EXCLUDED_GAP_S = 30.0


def _integration_gap_metrics(time_s, sample_quality=None, modes=None, *, quick_scan=False):
    """Classify integration intervals using Quick phase cadence and row quality.

    Quick pulse/near-cutoff edges allow at most 0.25 s; main-discharge edges
    allow at most 12.5 s. Other acquisitions retain the existing 30 s bound
    when sample-quality evidence is available. DCIR has its own 0.5 s edge rule.
    """
    t = np.asarray(time_s, float)
    q = [str(x or "VALID").strip().upper() for x in
         ([] if sample_quality is None else list(sample_quality))]
    phase = [str(x or "").strip().upper() for x in (modes or [])]
    dt = np.diff(t)
    excluded_mask = np.zeros(dt.size, dtype=bool)
    gap_count = 0
    excluded_duration = 0.0
    for idx, span in enumerate(dt):
        tagged = len(q) == len(t) and (q[idx] == "GAP" or q[idx + 1] == "GAP")
        invalid = not np.isfinite(span) or span <= 0
        if quick_scan and len(phase) == len(t):
            edge_phases = (phase[idx], phase[idx + 1])
            expected_dt = (0.1 if any(p in {"MINI_PULSE", "NEAR_CUTOFF"}
                                      for p in edge_phases) else 5.0)
            max_dt = min(MAX_CAPACITY_EXCLUDED_GAP_S, 2.5 * expected_dt)
            excessive = np.isfinite(span) and span > max_dt
        else:
            # Non-Quick direct callers retain legacy timing behavior unless
            # row-quality data explicitly identifies a gap.
            excessive = (sample_quality is not None and np.isfinite(span)
                         and span > MAX_CAPACITY_EXCLUDED_GAP_S)
        excluded = invalid or tagged or excessive
        excluded_mask[idx] = excluded
        if excluded:
            gap_count += 1
            if np.isfinite(span) and span > 0:
                excluded_duration += float(span)
    span_total = float(t[-1] - t[0]) if t.size >= 2 and np.isfinite(t[[0, -1]]).all() else 0.0
    fraction = excluded_duration / span_total if span_total > 0 else 1.0
    status = ("INSUFFICIENT_DATA" if t.size < 2 else
              "INVALID_TIMESTAMPS" if (quick_scan or sample_quality is not None)
              and np.any(~np.isfinite(dt) | (dt <= 0)) else
              "EXCESSIVE_GAPS" if excluded_duration > MAX_CAPACITY_EXCLUDED_GAP_S or fraction > 0.05 else
              "VALID_WITH_MINOR_GAPS" if gap_count else "VALID")
    positive = dt[np.isfinite(dt) & (dt > 0)]
    return {"gap_count": gap_count, "excluded_gap_count": gap_count,
            "largest_dt_s": float(np.max(positive)) if positive.size else 0.0,
            "excluded_gap_duration_s": excluded_duration,
            "integration_span_s": span_total,
            "integration_quality_status": status,
            "excluded_interval_mask": excluded_mask}
# SoH = measured discharge Ah ÷ rated ASSUMES the discharge began from a full pack.
# Below this starting SoC the capacity removed only spans SoC_start→0, so a healthy
# pack reads a proportionally LOW SoH (a 50 %-charged healthy pack → SoH ≈ 50 %).
# reached_cutoff can't catch it (it guards the END), so a known-partial start is flagged.
_SOH_MIN_START_SOC = 95.0    # %

# A capacity acceptance result must be measured at the profile's rated duration
# (normally C10), from a full/known-full start, to the configured cut-off.  A
# small tolerance absorbs current-regulator error without allowing a Quick 1C
# discharge to masquerade as a C10 capacity test.
_CAPACITY_RATE_TOLERANCE = 0.15
_CAPACITY_CUTOFF_TOLERANCE_V = 0.10

# D2 runtime guard: refuse a harness-resistance correction that would remove more
# than this fraction of a raw ohmic reading — see _correct_for_harness_r.
_HARNESS_MAX_REMOVAL_FRACTION = 0.5


def _correct_for_harness_r(raw_ohm: float, harness_r: float, label: str,
                           warnings: list) -> tuple:
    """Subtract the rig's harness/contact resistance (BatteryConfig.
    harness_resistance_ohm) from a raw ohmic reading (DCIR / DCIR-slope / ECM R0) —
    but refuse and warn instead if that would remove more than
    ``_HARNESS_MAX_REMOVAL_FRACTION`` of the raw value.

    A harness_resistance_ohm calibrated once against a healthy specimen (or simply
    mis-entered) can be too large relative to a SPECIFIC later reading — e.g. a
    genuinely degraded pack whose true resistance is smaller than the harness value
    calibrated for a healthy one. Applied blindly, ``max(1e-4, raw - harness_r)``
    would floor that reading near zero and grade a bad pack "A" with no indication
    anything was wrong. This is the runtime half of the D2 defense-in-depth pair —
    see ConfigManager.validate_config() for the config-entry-time ceiling.

    Returns ``(corrected_ohm, warnings)`` — ``warnings`` is the input list with a new
    entry appended only when the correction was skipped.
    """
    if harness_r <= 0.0 or raw_ohm <= 0.0:
        return raw_ohm, warnings
    if harness_r >= _HARNESS_MAX_REMOVAL_FRACTION * raw_ohm:
        return raw_ohm, warnings + [
            f"harness_resistance_ohm ({harness_r * 1e3:.1f} mΩ) would remove "
            f"≥{_HARNESS_MAX_REMOVAL_FRACTION * 100:.0f}% of raw {label} "
            f"({raw_ohm * 1e3:.1f} mΩ) — correction SKIPPED (check harness_resistance_ohm "
            f"calibration); grading on the uncorrected value"]
    return max(1e-4, raw_ohm - harness_r), warnings


def _reject_outliers_mad(x, n_sigma=3.0):
    """Drop values that disagree with the median by >n_sigma robust deviations (MAD).
    A bad contact or a pulse caught mid-transient shows up as an outlier DCIR; the
    median already resists it, but removing it first tightens the reported spread."""
    if x.size < 4:
        return x
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    if mad <= 0:
        return x
    keep = np.abs(x - med) <= n_sigma * 1.4826 * mad   # 1.4826: MAD→σ for normal data
    return x[keep] if keep.any() else x


def peukert_capacity(capacity_ah, mean_current_a, rated_ah, k, ref_c_rate=0.1):
    """Normalise a measured discharge capacity to a reference C-rate (Peukert's law).

    Available capacity falls as the discharge rate rises (strongly for lead-acid). With
    ``C = C_p / I^(k-1)``, a capacity measured at current ``I`` maps to the reference
    rate ``I_ref = ref_c_rate·rated`` by ``C_ref = C·(I/I_ref)^(k-1)`` — for YTZ6V
    Quick→C10 this is ``(I_mean/0.500)^(k-1)`` — so a high-rate
    test isn't unfairly graded low. Lithium (k≈1.05) → almost no change."""
    i_ref = float(rated_ah) * float(ref_c_rate)
    if mean_current_a <= 0 or i_ref <= 0 or k <= 0:
        return capacity_ah
    return capacity_ah * (mean_current_a / i_ref) ** (k - 1.0)


def dcir_from_vi_slope(currents, voltages):
    """Robust DCIR from the slope of V vs I across distinct current levels:
    ``V = OCV − I·R`` → ``R = −slope``. Fitting the slope cancels the OCV intercept, so
    it is less sensitive than one ΔV/ΔI step. Returns ``(r_ohm, r2)``; r is NaN when
    fewer than two distinct current levels are present."""
    I = np.asarray(currents, float)
    V = np.asarray(voltages, float)
    if I.size < 2 or float(np.ptp(I)) < 1e-6:
        return float("nan"), 0.0
    slope, intercept = np.polyfit(I, V, 1)
    pred = slope * I + intercept
    ss_res = float(np.sum((V - pred) ** 2))
    ss_tot = float(np.sum((V - V.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return abs(float(slope)), float(r2)


# A "level" is only a valid steady-state DCIR anchor if its own voltage samples
# are tight — a genuine constant-current PLATEAU (HPPC pulse/rest) settles to
# within tens of mV. A long continuous single-rate discharge (IEC capacity test)
# also reads as one "level" by current alone, but its voltage sweeps across the
# WHOLE SoC range (volts, not millivolts) as the pack discharges — the level's
# median would then mix real IR-drop with SoC-dependent OCV decline, inflating
# dcir_slope by an order of magnitude (a real case: 400 mΩ reported vs ~90-100 mΩ
# from the same record's ECM fit). Reject a level whose spread looks like SoC
# drift rather than noise. Shared with
# StateEstimator._STEP_REF_MAX_SPREAD_V via battery_model.STEADY_STATE_MAX_SPREAD_V.
_VI_LEVEL_MAX_SPREAD_V = STEADY_STATE_MAX_SPREAD_V  # V


def _vi_levels(current_a, voltage_v):
    """(current, terminal-voltage) points — one per distinct current level (rest + each
    load level) — for the V–I slope DCIR. Rest gives the (0, OCV) anchor. A level is
    dropped (not just the whole record) if its voltage spread suggests it isn't a real
    steady-state plateau — see _VI_LEVEL_MAX_SPREAD_V."""
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    pts = []
    rest = np.abs(ia - _I_STANDBY) < 0.15      # "rest" = |I| ≈ 0 (SSR fully disconnects load)
    if rest.any() and float(np.ptp(va[rest])) <= _VI_LEVEL_MAX_SPREAD_V:
        pts.append((_I_STANDBY, float(np.median(va[rest]))))
    loaded = np.abs(ia - _I_STANDBY) >= 0.2    # load on top of standby
    if loaded.any():
        keys = np.round(ia, 1)                       # cluster load into 0.1 A levels
        for lvl in np.unique(keys[loaded]):
            m = loaded & (keys == lvl)
            if int(m.sum()) >= 3 and float(np.ptp(va[m])) <= _VI_LEVEL_MAX_SPREAD_V:
                pts.append((float(lvl), float(np.median(va[m]))))
    return pts


def _dcir_temp_normalizer(profile: BatteryProfile):
    """Chemistry-specific Arrhenius temperature multiplier for identify_dcir's 25 °C
    normalization (see BatteryModel.temp_rin_multiplier — the SAME model the Rin
    baseline that graded DCIR is compared against already uses). Replaces the old
    flat ``_DCIR_TEMP_COEFF`` linear approximation, which did not match the
    chemistry-aware baseline and, for some chemistries, even had the wrong sign
    relative to the physically-correct Arrhenius relationship.

    Returns a ``multiplier(temp_c) -> float`` callable; falls back to the legacy
    linear approximation if BatteryModel can't be constructed for this chemistry
    (defensive only — get_chemistry() itself already falls back to a default
    chemistry rather than raising, so this should not normally trigger).
    """
    try:
        from aset_batt.core.battery_model import BatteryModel
        model = BatteryModel(profile.chemistry)
        return model.temp_rin_multiplier
    except Exception:
        return lambda T: 1.0 + _DCIR_TEMP_COEFF * (T - _T_REF)


def identify_dcir(current_a, voltage_v, temp_c, profile: BatteryProfile, time_s=None,
                  modes=None, quick_scan=False, sample_quality=None):
    """Repeatable single-step DCIR aggregated over EVERY current step in the record.

    At the rig's ~5 Hz SCPI readback the instantaneous ohmic step cannot be resolved
    from the RC relaxation, so a 1-RC ECM (R0/R1/C1 separation) is not identifiable
    (see ``docs/project_pivot.md``). Instead, ``R = |ΔV/ΔI|`` is read at the first
    sample after each current edge (a consistent ~200 ms readback point), each value
    is normalised to 25 °C using the same chemistry-specific Arrhenius model as the
    Rin baseline it's graded against (see ``_dcir_temp_normalizer``), and the
    **median across all steps** is reported with its spread — so an HPPC record's
    many pulses, or repeated load on/off edges, give a repeatable DCIR with a
    measurable uncertainty instead of a single noisy number.

    ``time_s`` (optional, elapsed seconds per sample): when supplied, a step whose
    post-edge sample arrives more than ``_DCIR_MAX_STEP_DT`` after the edge is dropped
    (its voltage has relaxed past ohmic) and counted, so a latency-corrupted reading
    can't inflate DCIR — the caller surfaces the drop count as a quality warning.

    Each accepted step must also land within a plausibility band RELATIVE to the
    profile baseline, [0.2×, 6×] internal_r — the same band the live step detector
    (StateEstimator._detect_step_r0) applies. A real session exposed the gap: at a
    charge onset the logger wrote two rows within the same 0.1 s window where the
    CURRENT had refreshed (PSU setpoint applied) but the VOLTAGE readback had not
    (separate SCPI query, still returning the pre-edge value) — ΔV = 0 across a
    1.2 A edge, so R = 0.00 mΩ was accepted as the record's only "measured" DCIR,
    which then zeroed the CCA proxy. The dt-gate above only rejects the too-STALE
    side; this band rejects the too-FRESH/garbage side.

    Returns ``(dcir_ohm_25C, std_ohm, n_steps, measured, n_stale, n_implausible)``.
    ``measured`` is False (and DCIR falls back to the profile baseline) when no clear
    step qualifies; ``n_stale`` is how many otherwise-valid steps were dropped for
    sampling latency; ``n_implausible`` how many for the plausibility band.
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    tc = np.asarray(temp_c, float)
    ta = np.asarray(time_s, float) if time_s is not None else None
    mode_arr = np.asarray([str(m or "").strip().upper() for m in (modes or [])], dtype=object)
    quality_arr = np.asarray([str(x or "VALID").strip().upper()
                              for x in (sample_quality or [])], dtype=object)
    phase_scoped = bool(quick_scan and mode_arr.size == ia.size)
    if ia.size < 4:
        return profile.internal_r, 0.0, 0, False, 0, 0
    temp_mult = _dcir_temp_normalizer(profile)
    di = np.diff(ia)
    pulse_abs = (np.abs(ia[mode_arr == "MINI_PULSE"]) if phase_scoped else np.abs(ia))
    thr = max(1e-3, 0.20 * float(np.max(pulse_abs)))     # a real load edge, not jitter
    r_base = float(profile.internal_r)
    vals = []
    n_stale = 0
    n_implausible = 0
    k = 0
    while k < di.size:
        if abs(di[k]) > thr:
            if quality_arr.size == ia.size and (quality_arr[k] in {"GAP", "INVALID"}
                                                  or quality_arr[k + 1] in {"GAP", "INVALID"}):
                n_stale += 1
                k += 2
                continue
            if phase_scoped:
                # Quick Scan's measured DCIR is the MINI_PULSE STEP_ON only.
                # The preceding sample may be OCV_SETTLE; the post-edge row
                # must explicitly belong to MINI_PULSE and be discharge-positive.
                if (mode_arr[k + 1] != "MINI_PULSE" or di[k] <= 0.0
                        or ia[k + 1] <= 0.0):
                    k += 1
                    continue
            # dt-gate: the post-edge sample must land soon after the edge, or it has
            # relaxed into the RC region and R would carry R1, not just the ohmic R0.
            if ta is not None and (k + 1) < ta.size:
                edge_dt = float(ta[k + 1] - ta[k])
                if not np.isfinite(edge_dt) or edge_dt <= 0.0:
                    n_stale += 1
                    k += 2
                    continue
                if edge_dt > _DCIR_MAX_STEP_DT:
                    n_stale += 1
                    k += 2
                    continue
            v_before = float(np.median(va[max(0, k - 2):k + 1]))   # rested/level baseline
            v_after = float(va[k + 1])                             # first post-edge sample
            r = abs((v_after - v_before) / di[k])
            T = float(tc[k + 1]) if (k + 1 < tc.size and not np.isnan(tc[k + 1])) else _T_REF
            r_norm = r / temp_mult(T)       # → 25 °C, chemistry-specific Arrhenius
            if not is_plausible_r0(r_norm, r_base):
                n_implausible += 1          # stale V readback / quantization, not ohmic
                k += 2
                continue
            vals.append(r_norm)
            k += 2                                                 # skip the paired sample
        else:
            k += 1
    if not vals:
        return profile.internal_r, 0.0, 0, False, n_stale, n_implausible
    arr = _reject_outliers_mad(np.asarray(vals, float))   # drop disagreeing pulses
    return float(np.median(arr)), float(np.std(arr)), int(arr.size), True, n_stale, n_implausible


def _quick_step_on_latency(current_a, modes, time_s, sample_quality=None) -> float:
    """Latency (s) of the first discharge MINI_PULSE STEP_ON candidate."""
    ia = np.asarray(current_a, float)
    ta = np.asarray(time_s, float)
    ma = np.asarray([str(m or "").strip().upper() for m in (modes or [])], dtype=object)
    if ia.size < 2 or ta.size != ia.size or ma.size != ia.size:
        return float("nan")
    pulse = np.abs(ia[ma == "MINI_PULSE"])
    if not pulse.size:
        return float("nan")
    threshold = max(1e-3, 0.20 * float(np.max(pulse)))
    quality = np.asarray([str(x or "VALID").strip().upper()
                          for x in (sample_quality or [])], dtype=object)
    for k in range(ia.size - 1):
        di = ia[k + 1] - ia[k]
        dt = ta[k + 1] - ta[k]
        if ma[k + 1] == "MINI_PULSE" and di > threshold and ia[k + 1] > 0.0:
            if (not np.isfinite(dt) or dt <= 0.0 or dt > _DCIR_MAX_STEP_DT
                    or (quality.size == ia.size
                        and (quality[k] != "VALID" or quality[k + 1] != "VALID"))):
                return float("nan")
            return float(dt)
    return float("nan")


# FreedomCAR/SAE J537-style fixed post-edge timepoints: R@0.1s is closest to pure
# ohmic (R0), R@1s adds fast charge-transfer, R@10s adds diffusion and is closest
# to sustained-load/cranking-relevant resistance — three numbers with an
# unambiguous, standard meaning, comparable across labs/rigs/sample-rates,
# instead of identify_dcir()'s single "whatever the first post-edge sample
# happened to catch" value (rate-dependent: ~100ms at 10Hz, ~200ms at 5Hz).
_DCIR_TIMEPOINTS_S = (0.1, 1.0, 10.0)


def identify_dcir_at_timepoints(current_a, voltage_v, temp_c, profile: BatteryProfile,
                                 time_s, timepoints_s=_DCIR_TIMEPOINTS_S,
                                 modes=None, quick_scan=False, sample_quality=None) -> dict:
    """R = |ΔV/ΔI| at fixed post-edge timepoints, additive alongside identify_dcir()
    (does not replace it — same step detection over current edges, same
    temperature normalisation, plausibility band, and per-timepoint MAD outlier
    rejection across steps).

    Requires ``time_s`` (elapsed seconds per sample); returns ``{}`` if not usable.
    A timepoint with no step landing close enough to it (e.g. the rig's rate is
    too slow, or a pulse is shorter than the timepoint) is simply omitted rather
    than reported from a stale/wrong sample.

    Returns ``{timepoint_s: (r_ohm_25C, std_ohm, n_steps)}``.
    """
    if time_s is None:
        return {}
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    tc = np.asarray(temp_c, float)
    ta = np.asarray(time_s, float)
    if ia.size < 4 or ta.size != ia.size:
        return {}
    mode_arr = np.asarray([str(m or "").strip().upper() for m in (modes or [])], dtype=object)
    phase_scoped = bool(quick_scan and mode_arr.size == ia.size)
    temp_mult = _dcir_temp_normalizer(profile)
    di = np.diff(ia)
    pulse_abs = (np.abs(ia[mode_arr == "MINI_PULSE"]) if phase_scoped else np.abs(ia))
    thr = max(1e-3, 0.20 * float(np.max(pulse_abs)))
    r_base = float(profile.internal_r)

    per_tp_vals = {tp: [] for tp in timepoints_s}
    quality_arr = np.asarray([str(x or "VALID").strip().upper()
                              for x in (sample_quality or [])], dtype=object)
    k = 0
    while k < di.size:
        if abs(di[k]) > thr:
            edge_dt = float(ta[k + 1] - ta[k])
            if not np.isfinite(edge_dt) or edge_dt <= 0.0 or edge_dt > MAX_STEP_EDGE_LATENCY_S:
                k += 2
                continue
            if quality_arr.size == ia.size and (
                    quality_arr[k] != "VALID" or quality_arr[k + 1] != "VALID"):
                k += 2
                continue
            if phase_scoped and (mode_arr[k + 1] != "MINI_PULSE"
                                 or di[k] <= 0.0 or ia[k + 1] <= 0.0):
                k += 1
                continue
            v_before = float(np.median(va[max(0, k - 2):k + 1]))
            # t_edge references the FIRST post-edge sample (ta[k+1]), not the last
            # pre-edge one (ta[k]) — the new current level is only actually present
            # from ta[k+1] onward, so "t=0" of the step response starts there.
            # Using ta[k] made "R@0.1s" land on ta[k+1] itself (t=0, not t=0.1) for
            # any dt<=0.1s rig, silently reporting pure R0 mislabeled as R@0.1s.
            t_edge = float(ta[k + 1])
            i_step = float(ia[k + 1])
            di_step = float(di[k])
            # Plateau end: the next real edge (current settles back down/off), or
            # end of record — timepoints are only searched within this window so
            # a later, unrelated edge can't be mistaken for this one's R@10s.
            j_end = k + 1
            while j_end + 1 < ia.size and abs(ia[j_end + 1] - i_step) <= thr * 0.5:
                j_end += 1
            seg_t = ta[k + 1:j_end + 1]
            for tp in timepoints_s:
                if seg_t.size == 0:
                    continue
                j_rel = int(np.argmin(np.abs(seg_t - (t_edge + tp))))
                j = k + 1 + j_rel
                # Reject if the closest available sample is still far from the
                # target timepoint (rig couldn't actually sample it this run —
                # e.g. a 5s pulse has no real R@10s point) rather than accept a
                # wrong-timepoint sample silently.
                if abs(ta[j] - (t_edge + tp)) > max(0.5 * tp, 0.5):
                    continue
                r = abs((float(va[j]) - v_before) / di_step)
                T = float(tc[j]) if not np.isnan(tc[j]) else _T_REF
                r_norm = r / temp_mult(T)
                if is_plausible_r0(r_norm, r_base):
                    per_tp_vals[tp].append(r_norm)
            k = j_end + 1
        else:
            k += 1

    out = {}
    for tp, vals in per_tp_vals.items():
        if not vals:
            continue
        arr = _reject_outliers_mad(np.asarray(vals, float))
        out[tp] = (float(np.median(arr)), float(np.std(arr)), int(arr.size))
    return out


# SAE J537's cranking end-voltage for a lead-acid CCA test: 1.2 V/cell. A crank pulse
# is brief (30 s) and the pack is expected to recover right after, so the standard lets
# terminal voltage sag much further than profile.cutoff_v (a deep-discharge protection
# floor meant for a sustained, minutes-to-hours discharge — reusing it here shrank the
# "voltage budget" (OCV - cutoff) the proxy divides by roughly in half, under-reporting
# a healthy pack's cranking capability). No equivalent standard cutoff exists for other
# chemistries in this rig's scope, so they keep using profile.cutoff_v as before.
_CCA_CRANK_CUTOFF_V_PER_CELL = 1.2   # V/cell, SAE J537


def _cca_cutoff_v(profile: BatteryProfile) -> float:
    from aset_batt.core import battery_profiles
    chem = battery_profiles.get_chemistry(profile.chemistry).name
    if chem == "LeadAcid":
        return _CCA_CRANK_CUTOFF_V_PER_CELL * profile.series
    return profile.cutoff_v


def _cca_derate_to_cold(cca_25c: float, ocv_eff: float,
                        profile: BatteryProfile) -> float:
    """Cold-temperature cranking-current proxy; not a standardized CCA result."""
    try:
        value = float(cca_25c)
        budget_25 = float(ocv_eff) - _cca_cutoff_v(profile)
        if not np.isfinite(value) or value <= 0.0 or budget_25 <= 0.0:
            return 0.0
        from aset_batt.core.battery_model import BatteryModel
        model = BatteryModel(profile.chemistry)
        factor = model.temp_rin_multiplier(-18.0)
        coeff = float(getattr(model.chemistry, "temp_coeff_mv_per_degc", 0.0))
        cold_ocv = float(ocv_eff) + coeff * (-43.0) * profile.series / 1000.0
        cold_budget = cold_ocv - _cca_cutoff_v(profile)
        return max(0.0, value * cold_budget / (budget_25 * max(1e-9, factor)))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _ocv_ceiling(profile: BatteryProfile, temp_c: float):
    """The chemistry OCV curve's own 100% point (pack-level) at ``temp_c`` — the
    highest voltage that carries any real state-of-charge meaning. A rested
    reading above it is undissipated surface charge (see BatteryModel.
    ocv_out_of_range_mv), not extra capacity. None if the model can't be built."""
    try:
        from aset_batt.core.battery_model import BatteryModel
        model = BatteryModel(profile.chemistry)          # series=1 → per-cell value
        return model.get_ocv_from_soc(100.0, temp_c) * profile.series
    except Exception:
        return None


def _load_metrics(current_a, voltage_v, dcir_ohm, profile: BatteryProfile,
                  ocv_ceiling=None):
    """Lead-acid health features the rig CAN measure at 5 Hz (see project pivot §3,§8.5):

      * ``voltage_sag_v`` — rested OCV minus the lowest terminal voltage seen under
        load. A weak/sulfated battery sags much more for the same current.
      * ``cca_est_a`` — cranking-capability proxy = (OCV − cranking cutoff) / DCIR,
        i.e. the current at which the terminal would sag to the cranking end-voltage
        (see ``_cca_cutoff_v`` — NOT the deep-discharge cutoff). Not a standardised
        CCA (that needs a cold high-rate crank; this is measured at ambient temp from
        a small pulse, linearly extrapolated), but a repeatable, physically grounded
        surrogate for sorting.

    ``ocv_ceiling``: the OCV curve's 100% point (see _ocv_ceiling). The DERIVED
    metrics (sag, CCA proxy) use min(ocv, ceiling): a surface-charge-inflated
    rest voltage (a real fresh-charged test rested at 13.18 V vs the curve's
    12.888 V ceiling) isn't charge the pack can actually deliver, so letting it
    into the arithmetic overstated both the sag baseline and the CCA proxy by
    ~5%. The RAW ocv is still returned unmodified — it is a truthful reading and
    stays what the report displays.
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    rest = np.abs(ia - _I_STANDBY) < 0.15
    if rest.any():
        ocv = float(np.median(va[rest])) + _I_STANDBY * dcir_ohm
    else:
        ocv = float(np.max(va)) if va.size else 0.0
    ocv_eff = min(ocv, ocv_ceiling) if ocv_ceiling else ocv
    under_load = np.abs(ia - _I_STANDBY) >= 0.2
    v_min_load = float(np.min(va[under_load])) if under_load.any() else ocv_eff
    sag = max(0.0, ocv_eff - v_min_load)
    cca_est = (ocv_eff - _cca_cutoff_v(profile)) / dcir_ohm if dcir_ohm > 1e-6 else 0.0
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
    if head.size and int((np.abs(head - _I_STANDBY) < 0.15).sum()) < 5:
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


_ECM_MIN_R2 = 0.90      # accept the 1-RC fit only if it explains the transient this well
# Plausibility ceiling on the fitted RC time constant. Every real pulse this
# codebase has ever measured (HPPC, and now Quick Scan's mini-pulse) has
# τ = R1·C1 in the 4-60 s range (see hppc.py's own "lead-acid τ ≈ 10-60s"
# design comment; real FB FTZ6V data measured τ=4.1-5.1s). fit_model's τ bound
# is intentionally wide ([1e-3, 1e7] s) so it never artificially clips a real
# pulse — but that width also lets it "fit" a multi-hour, slowly-declining
# discharge curve (no fast transient at all) by choosing a huge τ that makes
# the exponential term ≈ linear over the fit window, clearing R²≥0.90 with a
# result that is not a real RC branch. Found via fit_ecm=True on the real
# Quick Scan CSV (a plain 1C discharge, no pulse): τ=91366 s (25 HOURS),
# R1=10.35 Ω — both absurd for the YTZ6V C10 5.0 Ah pack. 600 s (10 min) is a
# generous ceiling well above any real pulse/relax duration this rig uses
# (HPPC's own relax cap is 300 s) while comfortably rejecting an hours-scale
# spurious fit.
_ECM_MAX_TAU_S = 600.0


def identify_ecm_fit(time_s, current_a, voltage_v, voc):
    """1-RC (and optionally 2-RC) Thevenin fit on an HPPC pulse.

    The polarisation/diffusion transient has τ ≈ 10–60 s, so a 30 s pulse at 5 Hz gives
    ~150 points — dense enough for the bounded TRF fit to pin **R1 and C1** precisely.
    **R0** is the fit's intercept at t=0 (backward extrapolation), which removes the slow
    transient's contamination that a single 200 ms step would include. (Sub-200 ms
    dynamics — pure ohmic + fast charge-transfer — are still unresolved at 5 Hz; for that
    a hardware fast-capture would be needed.)

    After the 1-RC fit a 2-RC fit is attempted; it is used only when it is meaningfully
    better (R²(2RC) > R²(1RC) + 0.015 AND R²(2RC) > 0.92).

    Returns ``(fit_dict, "")`` on success (1-RC or 2-RC dict), or ``(None, reason)`` when
    the fit is skipped/rejected — the caller surfaces ``reason`` as a quality warning so
    a blank R1/C1 on the ECM circuit isn't an unexplained dead end.
    """
    try:
        from aset_batt.core.parameter_id import BatteryParameterIdentifier
        identifier = BatteryParameterIdentifier(smooth_window=5)
        res = identifier.fit_model(time_s, current_a, voltage_v, voc)
    except Exception as e:
        logger.info("1-RC ECM fit skipped (%s) — using single-step DCIR", e)
        return None, str(e)
    r2 = res.get("r_squared", 0.0)
    tau = float(res.get("tau1_s", res.get("tau_s", 0.0)))
    if r2 < _ECM_MIN_R2 or res.get("R0_ohm", 0.0) <= 0:
        logger.info("1-RC ECM fit rejected (R²=%.3f) — using single-step DCIR", r2)
        return None, (f"fit quality too low (R²={r2:.2f} < {_ECM_MIN_R2:.2f}) — "
                      f"pulse noisy or too short for the RC tail")
    if tau > _ECM_MAX_TAU_S:
        logger.info("1-RC ECM fit rejected (τ=%.0fs > %.0fs ceiling) — "
                   "likely a slow trend, not a real RC pulse", tau, _ECM_MAX_TAU_S)
        return None, (f"fitted τ={tau:.0f}s exceeds the {_ECM_MAX_TAU_S:.0f}s plausibility "
                      f"ceiling — this looks like a slow SoC/OCV trend, not a real pulse "
                      f"transient")
    # Attempt 2-RC upgrade; reuse same identifier instance (same smoothing/thresholds).
    try:
        res_2rc = identifier.fit_model_2rc(time_s, current_a, voltage_v, voc,
                                           r1rc_result=res)
        if res_2rc is not None:
            res = res_2rc
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    return res, ""



def identify_hppc_pulses(time_s, current_a, voltage_v, temp_c,
                         profile: BatteryProfile, soc_pct=None) -> list:
    """Per-pulse breakdown of an HPPC record — one dict per pulse (discharge
    AND regen/charge-direction, see the G6 fix in hppc.py's sequence thread).

    The aggregated ECM fit that analyze_series reports uses ONE pulse (the first
    current edge fit_model finds) and one whole-record OCV anchor, so a
    systematic drift across the pulse train is invisible in the report. A real
    run (test_HPPC_20260708_152502) rested 190 mV above equilibrium at pulse 1
    and relaxed monotonically across the 5 pulses (anchor 13.34→13.15 V): every
    anchor-referenced estimator showed R0 "declining" 27-37% over 15 minutes —
    an artifact of the moving anchor, not the battery (detrended repeatability
    was ~5%). This table makes that trend visible per pulse so the operator can
    tell a drifting anchor from a genuinely inconsistent pack.

    Per pulse:
      idx, t_edge_s, duration_s, i_pulse_a (signed — negative for a regen/
      charge-direction pulse), leg ("discharge" or "regen", derived from the
      sign of i_pulse_a), anchor_v (median of last-5 rest samples — same
      basis as the live sequence's voc_for_fit), soc_pct (median live-
      estimator SoC during the pulse, NaN if the caller didn't supply
      ``soc_pct``), edge_dt_s (latency of the first post-edge sample; >
      MAX_STEP_EDGE_LATENCY_S means the "edge" reading already contains R1
      relaxation), r0_edge_mohm (ΔV/ΔI at that first post-edge sample, stale
      or not — the edge_stale flag says whether to trust it),
      r0_fit_mohm/r1_fit_mohm/c1_fit_f/tau_fit_s/fit_r2 (per-pulse 1-RC/2-RC fit
      with THIS pulse's own anchor), all resistances normalised to 25 °C and
      harness-corrected on the same basis as the aggregated metrics.

    Returns [] when fewer than 2 qualifying pulses (a single pulse has no trend
    to show and the aggregated fit already covers it).
    """
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    tc = np.asarray(temp_c, float)
    ta = np.asarray(time_s, float)
    if ia.size < 20 or ta.size != ia.size:
        return []
    sa = np.asarray(soc_pct, float) if soc_pct is not None else None
    if sa is not None and sa.size != ia.size:
        sa = None   # mismatched length — treat as not supplied rather than misalign
    temp_mult = _dcir_temp_normalizer(profile)
    harness_r = max(0.0, float(getattr(profile, "harness_r_ohm", 0.0)))

    # Same edge threshold as identify_dcir; np.abs() so a regen (charge-
    # direction, negative) pulse edge is detected too — the discharge-only
    # "ia > thr" used to make every regen pulse invisible here regardless of
    # any other logic, since a negative i_pulse never exceeds a positive
    # threshold.
    thr = max(1e-3, 0.20 * float(np.max(np.abs(ia))))
    on = np.abs(ia) > thr
    starts = np.where(~on[:-1] & on[1:])[0] + 1
    ends = np.where(on[:-1] & ~on[1:])[0] + 1

    def _harness(r):
        if r != r or harness_r <= 0.0:
            return r
        corrected, _ = _correct_for_harness_r(r, harness_r, "", [])
        return corrected

    pulses = []
    for k_on in starts:
        k_off_c = ends[ends > k_on]
        k_off = int(k_off_c[0]) if k_off_c.size else ia.size
        dur = float(ta[k_off - 1] - ta[k_on])
        if dur < 2.0 or (k_off - k_on) < 5:
            continue                       # blip, not an HPPC pulse
        # Anchor needs real rest right before the edge.
        pre = slice(max(0, k_on - 5), k_on)
        if not np.all(np.abs(ia[pre]) < 0.15) or (k_on - pre.start) < 3:
            continue
        anchor = float(np.median(va[pre]))
        i_pulse = float(np.median(ia[k_on:k_off]))
        T = float(np.nanmedian(tc[k_on:k_off])) if tc.size else _T_REF
        if T != T:
            T = _T_REF
        mult = temp_mult(T)

        edge_dt = float(ta[k_on] - ta[k_on - 1])
        di = float(ia[k_on] - ia[k_on - 1])
        v_before = float(np.median(va[max(0, k_on - 3):k_on]))
        r0_edge = abs((float(va[k_on]) - v_before) / di) / mult if abs(di) > thr else float("nan")

        # Per-pulse 1-RC fit with this pulse's own anchor (rest tail at i=0,
        # negative relative time — same seeding as the live sequence's fit buffers).
        n_tail = min(5, k_on)
        ts = np.concatenate([ta[k_on - n_tail:k_on], ta[k_on:k_off]]) - ta[k_on]
        cs = np.concatenate([np.zeros(n_tail), ia[k_on:k_off]])
        vs = np.concatenate([va[k_on - n_tail:k_on], va[k_on:k_off]])
        fit, _reason = identify_ecm_fit(ts, cs, vs, anchor)
        if fit:
            r0_fit = float(fit["R0_ohm"]) / mult
            r1_fit = float(fit["R1_ohm"]) / mult
            c1_fit = float(fit["C1_farad"]) * mult
            tau_fit = float(fit.get("tau1_s", fit.get("tau_s", 0.0)))
            fit_r2 = float(fit["r_squared"])
        else:
            r0_fit = r1_fit = c1_fit = tau_fit = fit_r2 = float("nan")

        soc_at_pulse = (float(np.nanmedian(sa[k_on:k_off]))
                       if sa is not None else float("nan"))
        pulses.append({
            "idx": len(pulses) + 1,
            "t_edge_s": float(ta[k_on]),
            "duration_s": dur,
            "i_pulse_a": i_pulse,
            "leg": "discharge" if i_pulse >= 0 else "regen",
            "anchor_v": anchor,
            "soc_pct": soc_at_pulse,
            "edge_dt_s": edge_dt,
            "edge_stale": edge_dt > _DCIR_MAX_STEP_DT,
            "r0_edge_mohm": _harness(r0_edge) * 1e3,
            "r0_fit_mohm": _harness(r0_fit) * 1e3,
            "r1_fit_mohm": r1_fit * 1e3,
            "c1_fit_f": c1_fit,
            "tau_fit_s": tau_fit,
            "fit_r2": fit_r2,
        })
    return pulses if len(pulses) >= 2 else []


# Per-pulse trend thresholds for the report's drift warning. 50 mV of rest-anchor
# movement across the pulse train ≈ 10 mΩ of apparent R0 change at a 5 A pulse —
# larger than a healthy pack's real pulse-to-pulse variation (detrended CV ~5% on
# the real run above), so past this the R0 TREND is anchor artifact, not battery.
_HPPC_ANCHOR_DRIFT_WARN_V = 0.050
_HPPC_R0_CV_WARN_PCT = 10.0


def _hppc_pulse_summary(pulses: list, warnings: list) -> tuple:
    """(anchor_drift_v, r0_cv_pct, warnings) from identify_hppc_pulses output.
    Prefers the per-pulse fit R0 for the CV (anchor-aware, de-noised); falls back
    to the edge R0 when fits failed. Appends a quality warning when the anchor
    moved or the R0 spread is wide — the exact failure the aggregated single-fit
    report used to hide."""
    if not pulses:
        return float("nan"), float("nan"), warnings
    anchors = np.asarray([p["anchor_v"] for p in pulses], float)
    drift = float(anchors[-1] - anchors[0])
    r0s = np.asarray([p["r0_fit_mohm"] for p in pulses], float)
    if np.all(np.isnan(r0s)):
        r0s = np.asarray([p["r0_edge_mohm"] for p in pulses], float)
    r0s = r0s[~np.isnan(r0s)]
    cv = float(100.0 * np.std(r0s) / np.mean(r0s)) if r0s.size >= 2 and np.mean(r0s) > 0 \
        else float("nan")
    if abs(drift) > _HPPC_ANCHOR_DRIFT_WARN_V:
        warnings.append(
            f"rest anchor drifted {drift * 1e3:+.0f} mV across {len(pulses)} pulses — "
            f"per-pulse R0 trend is an anchor artifact (surface charge still "
            f"dissipating), not the battery; extend REST before pulsing or judge "
            f"repeatability from the detrended spread")
    elif cv == cv and cv > _HPPC_R0_CV_WARN_PCT:
        warnings.append(
            f"per-pulse R0 varies {cv:.0f}% across {len(pulses)} pulses with a "
            f"steady anchor — pulses disagree beyond normal spread; check contact/"
            f"connection stability before trusting the aggregated R0")
    return drift, cv, warnings


def _main_discharge_capacity(time_s, ia, capacity_total: float, modes: list[str] | None,
                            sample_quality=None):
    """Return Ah belonging to MAIN_DISCHARGE only, plus its provenance.

    Logged cumulative Ah includes Quick Scan's diagnostic pulse and any other
    positive-current phase.  Integrating only intervals whose two endpoints are
    MAIN_DISCHARGE keeps that charge out of the capacity claim.  Legacy CSVs
    have no phase labels, so their total remains displayable but is explicitly
    marked as unsuitable for a verified capacity grade.
    """
    t = np.asarray(time_s, float)
    i = np.asarray(ia, float)
    normalised = [str(m or "").strip().upper() for m in (modes or [])]
    if len(normalised) == len(i) and "MAIN_DISCHARGE" in normalised and len(i) >= 2:
        # NEAR_CUTOFF is a sampling-rate subphase of the same discharge. Counting
        # intervals by adjacent rows (rather than summing named phase totals)
        # includes it exactly once and never double-counts the transition.
        mask = np.asarray([m in {"MAIN_DISCHARGE", "NEAR_CUTOFF"}
                           for m in normalised], bool)
        dt = np.diff(t)
        interval_mask = mask[:-1] & mask[1:] & np.isfinite(dt) & (dt >= 0.0)
        gap_info = _integration_gap_metrics(t, sample_quality, normalised)
        interval_mask &= ~gap_info["excluded_interval_mask"]
        current_mid = 0.5 * (np.clip(i[:-1], 0.0, None) + np.clip(i[1:], 0.0, None))
        return float(np.sum(current_mid[interval_mask] * dt[interval_mask]) / 3600.0), \
            "MAIN_DISCHARGE"
    return float(capacity_total), "legacy_all_positive"


def _quick_discharge_interval(time_s, ia, capacity_total: float,
                              modes: list[str] | None, sample_quality=None):
    """Integrate the single Quick Scan discharge from its pre-pulse anchor.

    Include pulse, main discharge and near-cutoff rows.  Each physical interval
    is integrated once when either endpoint is in an active discharge phase;
    this captures the load-on/off boundary trapezoids without summing phases.
    """
    t = np.asarray(time_s, float)
    i = np.asarray(ia, float)
    phase = [str(m or "").strip().upper() for m in (modes or [])]
    active = {"MINI_PULSE", "MAIN_DISCHARGE", "NEAR_CUTOFF"}
    if len(phase) != len(i) or len(i) < 2 or "MAIN_DISCHARGE" not in phase:
        return float(capacity_total)
    dt = np.diff(t)
    interval_mask = np.asarray([(a in active or b in active)
                                for a, b in zip(phase[:-1], phase[1:])])
    interval_mask &= np.isfinite(dt) & (dt >= 0.0)
    gap_info = _integration_gap_metrics(t, sample_quality, phase, quick_scan=True)
    interval_mask &= ~gap_info["excluded_interval_mask"]
    current_mid = 0.5 * (np.clip(i[:-1], 0.0, None) + np.clip(i[1:], 0.0, None))
    return float(np.sum(current_mid[interval_mask] * dt[interval_mask]) / 3600.0)


def _quick_mean_discharge_current(time_s, ia, modes, sample_quality=None):
    """Time-weighted mean over the exact intervals used by Quick charge integration."""
    t = np.asarray(time_s, float)
    i = np.asarray(ia, float)
    phase = [str(m or "").strip().upper() for m in (modes or [])]
    active = {"MINI_PULSE", "MAIN_DISCHARGE", "NEAR_CUTOFF"}
    if len(phase) != len(i) or len(i) < 2:
        return 0.0
    dt = np.diff(t)
    interval_mask = np.asarray([a in active or b in active
                                for a, b in zip(phase[:-1], phase[1:])])
    interval_mask &= np.isfinite(dt) & (dt >= 0.0)
    gap_info = _integration_gap_metrics(t, sample_quality, phase, quick_scan=True)
    interval_mask &= ~gap_info["excluded_interval_mask"]
    duration = float(np.sum(dt[interval_mask]))
    if duration <= 0.0:
        return 0.0
    current_mid = 0.5 * (np.clip(i[:-1], 0.0, None) + np.clip(i[1:], 0.0, None))
    return float(np.sum(current_mid[interval_mask] * dt[interval_mask]) / duration)


def _calc_capacity_and_soh(
        time_s, capacity_total: float, ia: np.ndarray, profile: "BatteryProfile",
        is_hppc: bool, reached_cutoff: bool, soh: float | None,
        modes: list[str] | None = None, soc_start: float | None = None,
        sample_quality=None):
    """Calculate raw and rate/SoC-normalised capacity separately.

    ``soh`` is always the raw, observed capacity fraction. ``soh_est`` is a
    clearly-labelled estimate that applies Peukert and (when supplied) start-SoC
    normalisation.  The estimate is useful for diagnosis, but never by itself a
    capacity acceptance grade.
    """
    capacity, capacity_basis = _main_discharge_capacity(time_s, ia, capacity_total, modes, sample_quality)
    normalised_modes = [str(m or "").strip().upper() for m in (modes or [])]
    if len(normalised_modes) == len(ia) and "MAIN_DISCHARGE" in normalised_modes:
        dis = ia[np.asarray([m == "MAIN_DISCHARGE" for m in normalised_modes], bool)]
    else:
        dis = ia[ia > 0.05]
    mean_dis = float(np.mean(dis)) if dis.size else 0.0
    ref_c_rate = 1.0 / max(1e-9, float(getattr(profile, "peukert_hr", 10.0)))
    reference_current_a = (float(profile.capacity_10h_ah) /
                           max(1.0, float(getattr(profile, "peukert_hr", 10.0)))
                           if getattr(profile, "capacity_10h_ah", 0.0) > 0.0
                           else ref_c_rate * profile.capacity_ah)
    try:
        _k = float(getattr(profile, "peukert_k", 1.1))
    except (TypeError, ValueError):
        _k = float("nan")
    cap_norm = (capacity * (mean_dis / reference_current_a) ** (_k - 1.0)
                if mean_dis > 0.0 and reference_current_a > 0.0
                and np.isfinite(_k) else capacity)
    # ``capacity`` is measured removed charge.  It becomes a direct SoH only
    # when the protocol proves a full-charge start; a partial/unknown start is
    # reported through ``observed_capacity_fraction_pct`` and may only produce
    # ``soh_est`` after independent SoC-span normalization.
    verified_full_start = (soc_start is not None and np.isfinite(float(soc_start))
                           and float(soc_start) >= _SOH_MIN_START_SOC)
    if soh is None:
        raw_soh = (100.0 * capacity / profile.capacity_ah
                   if (not is_hppc) and reached_cutoff and profile.capacity_ah
                   and verified_full_start else float("nan"))
    else:
        raw_soh = float(soh)
    soh_est = (100.0 * cap_norm / profile.capacity_ah
               if (not is_hppc) and reached_cutoff and profile.capacity_ah else raw_soh)
    if soc_start is not None and soc_start == soc_start and 30.0 <= soc_start < 98.0 \
            and not np.isnan(soh_est):
        soh_est /= soc_start / 100.0
        soh_basis = (f"rate/SoC-normalised estimate from {soc_start:.0f}% start SoC; "
                     "not a full-charge measurement")
    elif soc_start is not None and soc_start == soc_start and soc_start >= 98.0:
        soh_basis = "full-charge measurement"
    else:
        soh_basis = "raw observed capacity; start SoC is unknown"
    if not np.isnan(raw_soh):
        raw_soh = float(min(100.0, max(0.0, raw_soh)))
    if not np.isnan(soh_est):
        soh_est = float(min(100.0, max(0.0, soh_est)))
    return capacity, capacity_basis, mean_dis, cap_norm, raw_soh, soh_est, soh_basis


def _grade_from_soh(soh: float) -> str:
    if np.isnan(soh):
        return "REVIEW"
    if soh >= 90.0:
        return "A"
    if soh >= 80.0:
        return "B"
    if soh >= 70.0:
        return "C"
    return "REJECT"


def _worst_grade(*grades: str) -> str:
    """Worst valid grade; REVIEW is handled by the evidence gate before use."""
    rank = {"A": 0, "B": 1, "C": 2, "REJECT": 3}
    return max(grades, key=lambda g: rank[g])

def _check_soh_start_soc(
        warnings: list, is_hppc: bool, reached_cutoff: bool, soh: float, 
        soc_start: float | None, current_a, voltage_v, ocv_ceil: float, 
        t_med: float, profile: "BatteryProfile") -> list:
    if (not is_hppc) and reached_cutoff and not np.isnan(soh) \
            and soc_start is not None and soc_start == soc_start:
        if soc_start < _SOH_MIN_START_SOC:
            warnings.append(
                f"discharge started at {soc_start:.0f}% SoC (not full) — SoH is under-stated; "
                f"charge fully before a capacity test")
        elif soc_start >= _SOH_MIN_START_SOC:
            try:
                from aset_batt.core.battery_model import BatteryModel
                ia_h = np.asarray(current_a, float)[:25]
                va_h = np.asarray(voltage_v, float)[:25]
                head_rest = va_h[np.abs(ia_h - _I_STANDBY) < 0.15]
                if head_rest.size >= 3:
                    v_head = float(np.median(head_rest))
                    if not (ocv_ceil and v_head > ocv_ceil):
                        model = BatteryModel(profile.chemistry)
                        soc_ocv = model.get_soc_from_ocv(v_head / profile.series, t_med) \
                            if model.series_cells == 1 else None
                        if soc_ocv is not None and soc_start - soc_ocv > 15.0:
                            warnings.append(
                                f"logged start SoC ({soc_start:.0f}%) is not corroborated by "
                                f"the rested head voltage ({v_head:.3f} V → ~{soc_ocv:.0f}%) — "
                                f"SoH may be under-stated (pack likely not full at start)")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    return warnings

# FreedomCAR/USABC-style DC resistance at fixed pulse timepoints (G5). The
# single "R0" the report shows is the first-sample-after-edge value (≈R@100ms at
# this rig's ~10 Hz), which already carries part of R1 — not a clean ohmic number
# and not directly comparable across rigs/labs sampling at different rates. The
# standard answer is to report ΔV/ΔI at DEFINED times instead: R@0.1s (ohmic-
# dominated), R@1s, R@10s (the closest surrogate to a cranking/high-rate pull).
# With a 1-RC/2-RC ECM already fitted, R(t) = R0 + Σ Ri·(1 − e^(−t/τi)) is exactly
# those timepoints read off the de-noised model of the same pulse — no change to
# how the pulse is measured, only what's reported. NaN when no ECM was identified
# (a single-step DCIR can't be resolved into a time-resolved R(t)).
_R_TIMEPOINTS_S = (0.1, 1.0, 10.0)


def ecm_r_at(t: float, r0: float, r1: float, tau1: float,
             r2: float = 0.0, tau2: float = 0.0) -> float:
    """1-RC/2-RC model DC resistance at pulse time ``t`` seconds:
    ``R0 + R1·(1−e^(−t/τ1)) [+ R2·(1−e^(−t/τ2))]``. Each RC term is only added
    when its (Ri, τi) are physical (>0), so a 1-RC fit (R2=τ2=0) contributes
    nothing from the second branch instead of dividing by a zero τ2."""
    r = r0
    if tau1 > 1e-9 and r1 > 0.0:
        r += r1 * (1.0 - float(np.exp(-t / tau1)))
    if tau2 > 1e-9 and r2 > 0.0:
        r += r2 * (1.0 - float(np.exp(-t / tau2)))
    return r


# A pulse release edge provides a second, independent view of the ohmic jump.
# Older records often reach their FIRST *load-on* sample 0.3–3 s after the
# command, by which time the rising RC branch has already inflated ΔV/ΔI.  At
# load release the observed recovery is R0 PLUS the known RC relaxation during
# that sampled interval.  Subtracting the latter makes the remaining value a
# better ACIR/ECM-R0 surrogate without inventing a calibration from the GBM.
_R0_RELEASE_MAX_PULSE_S = 120.0
# Legacy HPPC captures have a 1.2–1.3 s release readback gap.  The analytical
# RC recovery correction remains valid there; longer gaps are increasingly
# vulnerable to OCV drift and are excluded.
_R0_RELEASE_MAX_EDGE_DT_S = 1.5
# A release correction is recovery for an otherwise unresolved *load-on* edge,
# not a replacement for a genuinely well-sampled 10 Hz edge.  Applying it to a
# clean 100 ms trace would subtract RC relaxation twice and bias R0 low.
_R0_RELEASE_MIN_LOAD_ON_DT_S = 0.25


def identify_release_r0(time_s, current_a, voltage_v, temp_c,
                        profile: BatteryProfile, r1_ohm: float,
                        tau_s: float) -> tuple[float, float, int]:
    """Estimate R0 from load-release edges, compensating the fitted RC recovery.

    For a constant-current pulse of duration ``tp`` followed by a sampled
    release interval ``dt``:

    ``ΔV_release / |I| = R0 + R1*(1-exp(-tp/τ))*(1-exp(-dt/τ))``.

    This deliberately applies only to short, bounded pulses.  A capacity
    discharge lasting minutes/hours has changing OCV and is not valid evidence
    for this calculation.  The function returns a 25 °C-normalised
    ``(median_ohm, std_ohm, n_edges)`` or ``(nan, nan, 0)`` when the data cannot
    support it.  It does not apply harness correction; the caller owns the one
    consistent correction point for every reported R0 metric.
    """
    if not (np.isfinite(r1_ohm) and np.isfinite(tau_s)
            and r1_ohm > 0.0 and tau_s > 1e-6):
        return float("nan"), float("nan"), 0
    ta = np.asarray(time_s, float)
    ia = np.asarray(current_a, float)
    va = np.asarray(voltage_v, float)
    tc = np.asarray(temp_c, float)
    if ia.size < 6 or not (ta.size == ia.size == va.size):
        return float("nan"), float("nan"), 0
    threshold = max(1e-3, 0.20 * float(np.nanmax(np.abs(ia))))
    on = np.abs(ia) > threshold
    releases = np.where(on[:-1] & ~on[1:])[0] + 1
    normalizer = _dcir_temp_normalizer(profile)
    values = []
    for k_off in releases:
        k_on = k_off - 1
        while k_on > 0 and on[k_on - 1]:
            k_on -= 1
        if k_on <= 0:
            continue
        pulse_s = float(ta[k_off - 1] - ta[k_on])
        release_dt = float(ta[k_off] - ta[k_off - 1])
        load_on_dt = float(ta[k_on] - ta[k_on - 1])
        if (load_on_dt <= _R0_RELEASE_MIN_LOAD_ON_DT_S
                or pulse_s < 2.0 or pulse_s > _R0_RELEASE_MAX_PULSE_S
                or release_dt <= 0.0 or release_dt > _R0_RELEASE_MAX_EDGE_DT_S):
            continue
        i_pulse = float(np.nanmedian(np.abs(ia[k_on:k_off])))
        if i_pulse <= threshold:
            continue
        measured = abs(float(va[k_off]) - float(va[k_off - 1])) / i_pulse
        rc_recovery = (r1_ohm * (1.0 - float(np.exp(-pulse_s / tau_s)))
                       * (1.0 - float(np.exp(-release_dt / tau_s))))
        r0 = measured - rc_recovery
        if not np.isfinite(r0) or r0 <= 0.0:
            continue
        T = float(tc[k_off]) if k_off < tc.size and np.isfinite(tc[k_off]) else _T_REF
        values.append(r0 / normalizer(T))
    if not values:
        return float("nan"), float("nan"), 0
    arr = _reject_outliers_mad(np.asarray(values, float))
    return float(np.median(arr)), float(np.std(arr)), int(arr.size)


def _release_r0_is_consistent(release_r0: float, load_on_r0: float) -> bool:
    """Whether a release estimate can replace the load-on ECM result.

    Delayed load-on sampling includes positive RC polarisation, so a valid
    release-edge correction should normally be lower than (or at most very
    slightly above) the load-on fit.  Reject a contradictory single/mis-timed
    release edge rather than silently making the headline R0 worse.
    """
    return bool(np.isfinite(release_r0) and np.isfinite(load_on_r0)
                and 0.25 * load_on_r0 <= release_r0 <= 1.05 * load_on_r0)


def _extract_ecm_metrics(
        time_s, current_a, voltage_v, temp_c, ocv: float, ocv_ceil: float, fit_ecm: bool,
        harness_r: float, profile: "BatteryProfile", t_med: float, dcir: float, measured: bool,
        warnings: list) -> tuple[dict, list, float]:
    """``fit_ecm`` gates whether a whole-record 1-RC/2-RC fit is attempted at
    all — historically this was always ``is_hppc`` (only HPPC records have a
    pulse transient), but a non-HPPC record can now carry one too (e.g. Quick
    Scan's mini-pulse leg), so the caller resolves the gate and passes it in
    directly rather than this function assuming is_hppc."""
    ecm, ecm_reason = identify_ecm_fit(time_s, current_a, voltage_v, ocv) if fit_ecm else (None, "")
    is_2rc = bool(ecm and "R2_ohm" in ecm)

    if fit_ecm and ecm is None and ecm_reason:
        warnings.append(f"ECM R1/C1 not identified — {ecm_reason}; showing DCIR/R0 only")
        
    out = {
        "r0": dcir, "r1": 0.0, "c1": 0.0, "tau": 0.0, "r2_ecm_fit": 0.0,
        "rmse_v": float("nan"),
        "r2_rc": 0.0, "c2": 0.0, "tau2": 0.0, "ri_total": dcir,
        "ecm_fit_t_s": float("nan"), "is_2rc": False, "ecm_identified": False,
        "r_0p1s": float("nan"), "r_1s": float("nan"), "r_10s": float("nan"),
        "r0_fit": dcir, "r0_release": float("nan"), "r0_release_n": 0,
        "r0_method": "dcir_fallback",
    }
    
    if ecm:
        r0, r1 = float(ecm["R0_ohm"]), float(ecm["R1_ohm"])
        if harness_r > 0.0:
            r0, warnings = _correct_for_harness_r(r0, harness_r, "ECM R0", warnings)
        
        c1 = float(ecm["C1_farad"])
        tau = float(ecm.get("tau1_s", ecm.get("tau_s", 0.0)))
        r2_ecm_fit = float(ecm["r_squared"])
        rmse_v = float(ecm.get("rmse_v", float("nan")))
        r2_rc = float(ecm.get("R2_ohm", 0.0))
        c2 = float(ecm.get("C2_farad", 0.0))
        tau2 = float(ecm.get("tau2_s", 0.0))
        
        # Keep the conventional load-on fit for auditability, then refine the
        # headline R0 from release edges when the record contains enough valid
        # short pulses.  The raw fit remains available in the result/report so
        # this is a correction of the observable, not hidden re-labelling.
        _ecm_mult = _dcir_temp_normalizer(profile)(t_med)
        r0 /= _ecm_mult
        r1 /= _ecm_mult
        r2_rc /= _ecm_mult
        c1 *= _ecm_mult
        c2 *= _ecm_mult
        r0_fit = r0
        r0_release, r0_release_std, r0_release_n = identify_release_r0(
            time_s, current_a, voltage_v, temp_c, profile,
            float(ecm["R1_ohm"]), tau)
        r0_release_corrected = r0_release
        if r0_release_n and harness_r > 0.0:
            r0_release_corrected, warnings = _correct_for_harness_r(
                r0_release, harness_r, "release-edge R0", warnings)
        if r0_release_n and _release_r0_is_consistent(r0_release_corrected, r0_fit):
            r0_release = r0_release_corrected
            r0 = r0_release_corrected
            r0_method = "release_edge_rc_compensated"
        else:
            r0_method = "load_on_ecm_fit"
            if r0_release_n:
                warnings.append(
                    "release-edge R0 disagrees with the load-on ECM fit; "
                    "keeping the load-on value and retaining the release value for audit")
        ri_total = r0 + r1 + r2_rc
        
        try:
            ia_ = np.asarray(current_a, float)
            va_ = np.asarray(voltage_v, float)
            edges = np.where((np.abs(ia_[:-1]) < 0.15) & (np.abs(ia_[1:]) >= 1.0))[0]
            if edges.size:
                k0 = int(edges[0])
                local_rest = va_[max(0, k0 - 10):k0 + 1]
                local_rest = local_rest[np.abs(ia_[max(0, k0 - 10):k0 + 1]) < 0.15]
                if local_rest.size >= 3:
                    voc_local = float(np.median(local_rest))
                    if abs(voc_local - ocv) > 0.05:
                        warnings.append(
                            f"rest voltage right before the first pulse "
                            f"({voc_local:.3f} V) differs from the whole-record rest "
                            f"median ({ocv:.3f} V) — rest history inconsistent "
                            f"(surface charge?); R0 is sensitive to this anchor")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            
        if measured and dcir > 0 and not (0.5 * r0 <= dcir <= 1.5 * ri_total):
            warnings.append(f"DCIR@250ms ({dcir*1e3:.0f} mΩ) disagrees with fit "
                                   f"R0+R1 ({ri_total*1e3:.0f} mΩ) — check the pulse")
                                   
        # DC resistance at the standard 0.1/1/10 s pulse timepoints, read off the
        # fitted model (G5). Temp-normalised R0/R1/R2 + τ1/τ2 are used so these are
        # on the same 25 °C basis as the rest of the reported resistances.
        _rt = {tp: ecm_r_at(tp, r0, r1, tau, r2_rc, tau2) for tp in _R_TIMEPOINTS_S}

        out.update({
            "r0": r0, "r1": r1, "c1": c1, "tau": tau, "r2_ecm_fit": r2_ecm_fit,
            "rmse_v": rmse_v,
            "r2_rc": r2_rc, "c2": c2, "tau2": tau2, "ri_total": ri_total,
            "ecm_fit_t_s": float(ecm.get("t_edge_s", float("nan"))),
            "is_2rc": is_2rc, "ecm_identified": True,
            "r_0p1s": _rt[0.1], "r_1s": _rt[1.0], "r_10s": _rt[10.0],
            "r0_fit": r0_fit, "r0_release": r0_release,
            "r0_release_n": r0_release_n, "r0_method": r0_method,
        })
        
    _ocv_eff = min(ocv, ocv_ceil) if ocv_ceil else ocv
    cca_est = max(0.0, (_ocv_eff - _cca_cutoff_v(profile)) / out["ri_total"]) if out["ri_total"] > 1e-6 else 0.0
    return out, warnings, cca_est

def analyze_series(time_s, current_a, voltage_v, temp_c, capacity_series,
                   profile: "BatteryProfile", is_hppc: bool, soh=None,
                   soc_start=None, soc_series=None, fit_ecm=None, modes=None,
                   quick_scan: bool | None = None, ocv_start_valid=None,
                   soc_end=None, ocv_end_valid=False, sample_quality=None) -> dict:
    """Run the unified analysis on raw series → the standard results dict.

    ``fit_ecm``: whether to attempt a 1-RC/2-RC pulse fit at all. ``None``
    (default) resolves to ``is_hppc`` — every existing caller is unaffected.
    A non-HPPC record can still carry an analyzable pulse (e.g. Quick Scan's
    mini-pulse leg) — pass ``fit_ecm=True`` explicitly for those WITHOUT also
    setting ``is_hppc=True``, which would incorrectly suppress SoH
    (``_calc_capacity_and_soh`` only computes SoH when ``not is_hppc``) and the
    "did not reach cut-off" quality warning."""
    fit_ecm = is_hppc if fit_ecm is None else bool(fit_ecm)
    v = Analytics.hampel_filter(np.asarray(voltage_v, float))
    ia = Analytics.hampel_filter(np.asarray(current_a, float))
    q = np.asarray(capacity_series, float)
    capacity_total = float(q[-1]) if q.size else 0.0
    reached_cutoff = bool(v.size and float(np.min(v)) <= profile.cutoff_v * 1.02)
    mode_set = {str(m or "").strip().upper() for m in (modes or [])}
    is_quick_scan = (not is_hppc and (bool(quick_scan)
                     or {"MINI_PULSE", "MAIN_DISCHARGE"}.issubset(mode_set)))
    integration_quality = _integration_gap_metrics(
        time_s, sample_quality, modes, quick_scan=is_quick_scan)
    gaps_excessive = integration_quality["integration_quality_status"] in {
        "EXCESSIVE_GAPS", "INVALID_TIMESTAMPS"}
    capacity, capacity_basis, mean_dis, cap_norm, soh, soh_est, soh_basis = \
        _calc_capacity_and_soh(time_s, capacity_total, ia, profile, is_hppc,
                                reached_cutoff, soh, modes, soc_start, sample_quality)
    if gaps_excessive:
        # Preserve the integrated measurement as raw evidence even when timing
        # quality prevents normalized capacity/SoH certification.
        cap_norm = soh = soh_est = float("nan")
        soh_basis = "unavailable: excessive excluded sampling gaps"

    quick_capacity = None
    quick_capacity_status = "NOT_QUICK_SCAN"
    quick_soh_est = float("nan")
    q_interval_removed = (_quick_discharge_interval(time_s, ia, capacity_total, modes, sample_quality)
                          if is_quick_scan else capacity)
    quick_mean_current = (_quick_mean_discharge_current(time_s, ia, modes, sample_quality)
                          if is_quick_scan else mean_dis)
    if gaps_excessive and is_quick_scan:
        q_interval_removed = quick_mean_current = float("nan")
        quick_capacity = None
        quick_capacity_status = integration_quality["integration_quality_status"]
    try:
        _peukert_hr = float(getattr(profile, "peukert_hr", 10.0))
    except (TypeError, ValueError):
        _peukert_hr = float("nan")
    try:
        _capacity_10h = float(getattr(profile, "capacity_10h_ah", 0.0))
    except (TypeError, ValueError):
        _capacity_10h = float("nan")
    _reference_capacity = (_capacity_10h if np.isfinite(_capacity_10h) and _capacity_10h > 0.0
                          else float(profile.capacity_ah))
    try:
        i_ref_quick = float(getattr(profile, "peukert_reference_current_a", None))
    except (TypeError, ValueError):
        i_ref_quick = float("nan")
    if not np.isfinite(i_ref_quick) or i_ref_quick <= 0.0:
        i_ref_quick = (_reference_capacity / _peukert_hr
                       if np.isfinite(_peukert_hr) and _peukert_hr > 0.0
                       else float("nan"))
    try:
        peukert_k = float(getattr(profile, "peukert_k", float("nan")))
    except (TypeError, ValueError):
        peukert_k = float("nan")
    peukert_inputs_valid = (np.isfinite(quick_mean_current) and quick_mean_current > 0.0
                             and np.isfinite(i_ref_quick) and i_ref_quick > 0.0
                             and np.isfinite(peukert_k))
    peukert_factor = ((quick_mean_current / i_ref_quick) ** (peukert_k - 1.0)
                      if peukert_inputs_valid else
                      float("nan") if is_quick_scan else 1.0)
    q_c10_interval = (q_interval_removed * peukert_factor
                      if np.isfinite(peukert_factor) else float("nan"))
    if is_quick_scan:
        from aset_batt.acquisition.ocv_validation import estimate_full_capacity
        if gaps_excessive:
            quick_capacity_status = "EXCESSIVE_GAPS"
        elif not peukert_inputs_valid:
            quick_capacity_status = "PEUKERT_INPUT_INVALID"
        elif getattr(profile, "capacity_rating_validated", None) is False:
            quick_capacity_status = "PROFILE_NOT_RATE_VALIDATED"
        else:
            estimate = estimate_full_capacity(
                max(0.0, float(q_c10_interval)), soc_start, soc_end,
                start_valid=bool(ocv_start_valid), end_valid=bool(ocv_end_valid),
                rated_capacity_ah=profile.capacity_ah)
            quick_capacity, quick_capacity_status = estimate["capacity_ah"], estimate["status"]
            if estimate["valid"]:
                quick_soh_est = 100.0 * quick_capacity / profile.capacity_ah
        soh_est = quick_soh_est
        soh_basis = ("OCV-normalized Quick screening estimate" if np.isfinite(quick_soh_est)
                     else quick_capacity_status)

    # Polarity guard: reject a charge-only record, but do not misclassify an
    # HPPC sequence just because its regen leg has more samples than its
    # discharge pulses.  Throughput, rather than median current, preserves the
    # direction evidence that an electrical-characterisation record contains.
    ta = np.asarray(time_s, float)
    if ta.size == ia.size and ta.size >= 2:
        _dt = np.diff(ta)
        _dt = np.where(np.isfinite(_dt) & (_dt >= 0.0), _dt, 0.0)
        _i_mid = 0.5 * (ia[:-1] + ia[1:])
        _pos_as = float(np.sum(np.clip(_i_mid, 0.0, None) * _dt))
        _neg_as = float(np.sum(np.clip(-_i_mid, 0.0, None) * _dt))
        is_charge_record = _neg_as > 0.0 and _pos_as < 0.05 * _neg_as
    else:
        _active_i = ia[np.abs(ia) > 0.2]
        is_charge_record = bool(_active_i.size and float(np.median(_active_i)) < -0.2)
    if is_charge_record:
        soh = float("nan")

    dcir, dcir_std, n_steps, measured, n_stale, n_implausible = identify_dcir(
        current_a, voltage_v, temp_c, profile, time_s=time_s,
        modes=modes, quick_scan=is_quick_scan, sample_quality=sample_quality)
    dcir_latency = (_quick_step_on_latency(current_a, modes, time_s, sample_quality)
                    if is_quick_scan else float("nan"))
    dcir_timepoints = identify_dcir_at_timepoints(
        current_a, voltage_v, temp_c, profile, time_s=time_s,
        modes=modes, quick_scan=is_quick_scan, sample_quality=sample_quality)
    levels = _vi_levels(current_a, voltage_v)
    if len(levels) >= 2:
        dcir_slope, dcir_slope_r2 = dcir_from_vi_slope(
            [p[0] for p in levels], [p[1] for p in levels])
    else:
        dcir_slope, dcir_slope_r2 = float("nan"), 0.0

    harness_r = max(0.0, float(getattr(profile, "harness_r_ohm", 0.0)))
    harness_warnings: list = []
    if harness_r > 0.0:
        if measured:
            dcir, harness_warnings = _correct_for_harness_r(
                dcir, harness_r, "DCIR", harness_warnings)
        if dcir_slope == dcir_slope:
            dcir_slope, harness_warnings = _correct_for_harness_r(
                dcir_slope, harness_r, "DCIR slope", harness_warnings)
        for _tp in list(dcir_timepoints):
            _r, _std, _n = dcir_timepoints[_tp]
            _r, harness_warnings = _correct_for_harness_r(
                _r, harness_r, f"DCIR@{_tp:g}s", harness_warnings)
            dcir_timepoints[_tp] = (_r, _std, _n)

    _tc_arr = np.asarray(temp_c, float)
    t_med = float(np.nanmedian(_tc_arr)) if _tc_arr.size and not np.all(np.isnan(_tc_arr)) else _T_REF
    ocv_ceil = _ocv_ceiling(profile, t_med)
    sag, cca_est_25c, ocv = _load_metrics(
        current_a, voltage_v, dcir, profile, ocv_ceiling=ocv_ceil)
    warnings, temp_drift = _quality_flags(current_a, voltage_v, temp_c, profile,
                                          is_hppc, n_steps, reached_cutoff)
    warnings = warnings + harness_warnings
    
    if n_stale > 0:
        warnings.append(f"{n_stale} DCIR step(s) dropped — sampling latency >{_DCIR_MAX_STEP_DT:.1f}s "
                        f"(USB/SCPI stall); R0 would read inflated")
    if n_implausible > 0:
        warnings.append(f"{n_implausible} DCIR step(s) rejected — ΔV/ΔI outside the plausible band "
                        f"(stale voltage readback at the edge or quantization); not ohmic resistance")

    warnings = _check_soh_start_soc(warnings, is_hppc, reached_cutoff, soh, soc_start, current_a, voltage_v, ocv_ceil, t_med, profile)

    if is_charge_record:
        warnings.append("test was a CHARGE (negative median current) — grading requires a DISCHARGE; SoH is invalid")

    fit_time, fit_current, fit_voltage, fit_temp = time_s, current_a, voltage_v, temp_c
    fit_ocv = ocv
    if is_quick_scan and fit_ecm and len(mode_set) > 0:
        _mode_arr = np.asarray([str(m or "").strip().upper() for m in modes], dtype=object)
        _pulse_idxs = np.where(_mode_arr == "MINI_PULSE")[0]
        if _pulse_idxs.size:
            _first = int(_pulse_idxs[0])
            _last = int(_pulse_idxs[-1])
            _start = max(0, _first - 5)
            _pre_v = np.asarray(voltage_v, float)[_start:_first]
            _pre_i = np.asarray(current_a, float)[_start:_first]
            _quiet = _pre_v[np.abs(_pre_i) < 0.15]
            if _quiet.size:
                fit_ocv = float(np.median(_quiet))
            _fit_indices = np.arange(_start, _last + 1)
            fit_time = np.asarray(time_s, float)[_fit_indices]
            fit_time = fit_time - fit_time[0]
            fit_current = np.asarray(current_a, float)[_fit_indices]
            fit_voltage = np.asarray(voltage_v, float)[_fit_indices]
            fit_temp = np.asarray(temp_c, float)[_fit_indices]
        else:
            fit_ecm = False
    ecm, warnings, cca_est = _extract_ecm_metrics(
        fit_time, fit_current, fit_voltage, fit_temp, fit_ocv, ocv_ceil, fit_ecm,
        harness_r, profile, t_med, dcir, measured, warnings)
    _ocv_eff = min(ocv, ocv_ceil) if ocv_ceil else ocv
    if ecm["ecm_identified"]:
        # Prefer fitted ECM resistance over a profile/DCIR fallback for the
        # proxy. Keep the raw-at-25°C and cold-derated values distinct.
        cca_est_25c = cca_est
    cca_est = _cca_derate_to_cold(cca_est_25c, _ocv_eff, profile)

    # Per-pulse breakdown — the aggregated ECM above fits ONE pulse (whichever
    # edge fit_model's _detect_step finds first); this exposes the pulse-to-
    # pulse trend the single fit hides (see identify_hppc_pulses' docstring for
    # the real HPPC anchor-drift incident) AND, for a non-HPPC record with
    # exactly one analyzable pulse (e.g. Quick Scan's mini-pulse leg followed by
    # a long discharge), gives the promotion fallback below a per-pulse fit to
    # promote even when the aggregated whole-record fit above landed on the
    # WRONG edge (see promotion comment).
    hppc_pulses = identify_hppc_pulses(
        time_s, current_a, voltage_v, temp_c, profile,
        soc_pct=soc_series) if fit_ecm else []
    hppc_anchor_drift_v, hppc_r0_cv_pct, warnings = _hppc_pulse_summary(
        hppc_pulses, warnings)

    # Promotion fallback: fit_model's _detect_step() picks the single LARGEST
    # |ΔI| edge in the whole record — for a record with two same-magnitude
    # edges (Quick Scan's ~1C mini-pulse AND its ~1C main discharge), that can
    # land on the main discharge's "pulse" instead of the real one. The main
    # discharge's own current stays flat but its voltage sweeps the pack's
    # whole SoC range, so fit_model's R² gate correctly rejects it (ecm stays
    # not-identified) — but identify_hppc_pulses() above already fits EACH
    # edge independently with its own local anchor, so the real short pulse's
    # fit is sitting right there even when the whole-record fit missed it.
    # Promote the best-R² per-pulse fit into the headline ECM fields instead of
    # reporting "not identified" when a real, already-corrected/normalised fit
    # is available. Only fires when the aggregated fit above found nothing.
    if fit_ecm and not ecm["ecm_identified"] and hppc_pulses:
        _candidates = [p for p in hppc_pulses if p["fit_r2"] == p["fit_r2"]]  # non-NaN
        if _candidates:
            _best = max(_candidates, key=lambda p: p["fit_r2"])
            _p_r0 = _best["r0_fit_mohm"] / 1000.0
            _p_r1 = _best["r1_fit_mohm"] / 1000.0
            _p_tau = _best["tau_fit_s"]
            # Apply the same optional release-edge correction as the
            # whole-record path.  The per-pulse values above are already on a
            # 25 °C basis; convert R1 approximately back to the run's median
            # temperature because identify_release_r0() owns the per-edge
            # normalisation.  This only changes old, delayed load-on records
            # (the helper rejects a clean <=250 ms load-on edge).
            _p_r0_fit = _p_r0
            _p_r0_release, _p_r0_release_std, _p_r0_release_n = identify_release_r0(
                time_s, current_a, voltage_v, temp_c, profile,
                _p_r1 * _dcir_temp_normalizer(profile)(t_med), _p_tau)
            _p_r0_release_corrected = _p_r0_release
            if _p_r0_release_n and harness_r > 0.0:
                _p_r0_release_corrected, warnings = _correct_for_harness_r(
                    _p_r0_release, harness_r, "per-pulse release-edge R0", warnings)
            if _p_r0_release_n and _release_r0_is_consistent(
                    _p_r0_release_corrected, _p_r0_fit):
                _p_r0_release = _p_r0_release_corrected
                _p_r0 = _p_r0_release_corrected
                _p_r0_method = "release_edge_rc_compensated"
            else:
                _p_r0_method = "load_on_ecm_fit_per_pulse"
                if _p_r0_release_n:
                    warnings.append(
                        "per-pulse release-edge R0 disagrees with the load-on ECM fit; "
                        "keeping the load-on value and retaining the release value for audit")
            _p_rt = {tp: ecm_r_at(tp, _p_r0, _p_r1, _p_tau) for tp in _R_TIMEPOINTS_S}
            ecm = dict(ecm)
            ecm.update({
                "r0": _p_r0, "r1": _p_r1, "c1": _best["c1_fit_f"], "tau": _p_tau,
                "r2_ecm_fit": _best["fit_r2"], "ri_total": _p_r0 + _p_r1,
                "ecm_fit_t_s": _best["t_edge_s"], "is_2rc": False,
                "ecm_identified": True,
                "r_0p1s": _p_rt[0.1], "r_1s": _p_rt[1.0], "r_10s": _p_rt[10.0],
                "r0_fit": _p_r0_fit, "r0_release": _p_r0_release,
                "r0_release_n": _p_r0_release_n, "r0_method": _p_r0_method,
            })
            cca_est_25c = (max(0.0, _ocv_eff - _cca_cutoff_v(profile)) / ecm["ri_total"]
                           if ecm["ri_total"] > 1e-6 else 0.0)
            cca_est = _cca_derate_to_cold(cca_est_25c, _ocv_eff, profile)

    # A C10 capacity acceptance needs explicit phase provenance, a full/known-full
    # start, the profile cut-off, and a current close to C10.  Quick Scan's
    # Peukert-corrected number remains a useful estimate (soh_est) but cannot
    # supply this evidence.  This prevents a 1C scan, or a legacy CSV whose Ah
    # includes diagnostic pulses, from being promoted into an Overall A/B/C.
    expected_current = profile.capacity_ah / max(1e-9, float(getattr(profile, "peukert_hr", 10.0)))
    rate_ok = expected_current > 0.0 and abs(mean_dis - expected_current) <= \
        expected_current * _CAPACITY_RATE_TOLERANCE
    full_start = soc_start is not None and soc_start == soc_start and soc_start >= _SOH_MIN_START_SOC
    cutoff_ok = bool(v.size and float(np.min(v)) <= profile.cutoff_v + _CAPACITY_CUTOFF_TOLERANCE_V)
    capacity_reasons = []
    if is_hppc:
        capacity_reasons.append("HPPC is an electrical-characterisation test, not a capacity test")
    if is_charge_record:
        capacity_reasons.append("record is a charge, not a discharge")
    if capacity_basis != "MAIN_DISCHARGE":
        capacity_reasons.append("CSV has no MAIN_DISCHARGE phase provenance")
    if getattr(profile, "capacity_rating_validated", None) is not True:
        capacity_reasons.append("profile C10 rating basis is not validated")
    if not full_start:
        capacity_reasons.append("discharge did not start from verified full SoC")
    if not bool(ocv_start_valid):
        capacity_reasons.append("starting OCV condition is not verified")
    if not cutoff_ok:
        capacity_reasons.append("configured cut-off was not reached")
    if not rate_ok:
        capacity_reasons.append(
            f"mean discharge {mean_dis:.3f} A is not the C{getattr(profile, 'peukert_hr', 10.0):g} "
            f"reference {expected_current:.3f} A")
    capacity_gradeable = not capacity_reasons and not np.isnan(soh)
    capacity_grade = _grade_from_soh(soh) if capacity_gradeable else "REVIEW"
    if not capacity_gradeable:
        warnings.append("capacity grade withheld — " + "; ".join(capacity_reasons or ["no valid capacity result"]))

    # Electrical evidence is limited to a resolved pulse resistance.  A stale or
    # implausible edge is a data-quality failure, not a low-resistance battery.
    electrical_source = measured or ecm["ecm_identified"]
    electrical_reasons = []
    if is_charge_record:
        electrical_reasons.append("record is a charge, not a discharge")
    if not electrical_source:
        electrical_reasons.append("no valid DCIR or ECM pulse measurement")
    # A rejected generic DCIR edge is not grounds to discard a separate,
    # high-quality ECM pulse fit.  This happens in Quick Scan when the long
    # main-discharge edge is stale but its diagnostic mini-pulse remains fit
    # capable.  If no ECM is available, the rejected edge still blocks grade.
    if (n_stale > 0 or n_implausible > 0) and not ecm["ecm_identified"]:
        electrical_reasons.append("pulse sampling contains stale or implausible edge data")
    if ecm["ecm_identified"] and (np.isnan(ecm["r2_ecm_fit"]) or ecm["r2_ecm_fit"] < 0.90):
        electrical_reasons.append("ECM fit quality is below R² 0.90")
    electrical_gradeable = not electrical_reasons
    if electrical_gradeable:
        if ecm["ecm_identified"]:
            electrical_grade = Analytics.grade_from_ecm(float("nan"), ecm["r0"], ecm["r1"], profile)
        else:
            electrical_grade = Analytics.grade(float("nan"), dcir, profile)
    else:
        electrical_grade = "REVIEW"
        warnings.append("electrical grade withheld — " + "; ".join(electrical_reasons))

    # Quick Scan is deliberately a 1C screen.  It can still make an immediate,
    # explicitly model-based decision from Peukert-normalised capacity; that is
    # useful to the operator and must not be confused with the verified C10
    # grade below.  The phase signature keeps generic 1C data from receiving a
    # Quick Scan label accidentally.
    quick_reasons = []
    if not is_quick_scan:
        quick_reasons.append("not a phase-labelled Quick Scan record")
    if capacity_basis != "MAIN_DISCHARGE":
        quick_reasons.append("missing MAIN_DISCHARGE capacity provenance")
    if not np.isfinite(quick_soh_est):
        quick_reasons.append(quick_capacity_status)
    if not cutoff_ok:
        quick_reasons.append("configured cut-off was not reached")
    if is_charge_record:
        quick_reasons.append("record is a charge, not a discharge")
    if not np.isfinite(soh_est):
        quick_reasons.append("Peukert-normalised SoH is unavailable")
    quick_gradeable = not quick_reasons
    quick_grade = _grade_from_soh(soh_est) if quick_gradeable else "REVIEW"
    quick_grade_basis = (
        "Peukert-corrected C10-equivalent SoH screening grade; not a measured C10 result"
        if quick_gradeable else "; ".join(quick_reasons)
    )

    # ``grade`` remains the verified C10 headline for a phase-labelled Quick
    # Scan.  Preserve the historical safety behaviour for other test records:
    # a resolved electrical REJECT remains decisive there.
    if is_quick_scan:
        # Missing endpoint OCV means the Quick capacity assessment is incomplete,
        # not contradictory evidence. Keep the electrical result and report N/A.
        if not np.isfinite(quick_soh_est):
            quick_grade = "N/A"
            capacity_grade = "N/A"
            grade = "N/A"
        else:
            grade = (_worst_grade(capacity_grade, electrical_grade)
                     if capacity_gradeable and electrical_gradeable else "REVIEW")
    elif "REJECT" in (capacity_grade, electrical_grade):
        grade = "REJECT"
    elif capacity_gradeable and electrical_gradeable:
        grade = _worst_grade(capacity_grade, electrical_grade)
    else:
        grade = "REVIEW"
    gradeable = capacity_gradeable and electrical_gradeable

    confidence = _confidence(dcir, dcir_std, n_steps, profile, len(warnings))
    if ecm["ecm_identified"]:
        confidence *= 0.7 + 0.3 * max(0.0, min(1.0, ecm["r2_ecm_fit"]))

    # Penalize confidence for severe anchor drift
    if hppc_anchor_drift_v and hppc_anchor_drift_v > 0.3:
        confidence *= max(0.0, 1.0 - (hppc_anchor_drift_v - 0.3))

    if n_stale > 0:
        confidence *= 0.8

    quick_confidence = confidence
    if not gradeable:
        confidence = 0.0

    import logging
    logger = logging.getLogger(__name__)
    logger.info(
        "GRADE DECISION product=%s chemistry=%s grade=%s confidence=%.2f "
        "soh=%s dcir_mohm=%.2f r0_mohm=%.2f r1_mohm=%.2f harness_r_mohm=%.2f "
        "n_steps=%d measured=%s ecm_identified=%s gradeable=%s "
        "capacity_grade=%s electrical_grade=%s quick_grade=%s capacity_basis=%s warnings=%d",
        getattr(profile, "name", "?"), getattr(profile, "chemistry", "?"),
        grade, confidence, "nan" if np.isnan(soh) else f"{soh:.1f}",
        dcir * 1000.0, ecm["r0"] * 1000.0, ecm["r1"] * 1000.0, harness_r * 1000.0,
        n_steps, measured, ecm["ecm_identified"], gradeable,
        capacity_grade, electrical_grade, quick_grade, capacity_basis, len(warnings),
    )

    ica_v, ica = Analytics.incremental_capacity(v, q)
    return {
        "soh": (float("nan") if is_quick_scan else soh),
        "observed_capacity_fraction_pct": soh,
        "soh_est": soh_est, "soh_basis": soh_basis,
        "capacity_ah": capacity, "capacity_total_ah": capacity_total,
        **{k: value for k, value in integration_quality.items()
           if k != "excluded_interval_mask"},
        "q_removed_ah": capacity,
        "q_interval_removed_ah": q_interval_removed,
        "peukert_factor": peukert_factor,
        "quick_mean_discharge_a": quick_mean_current,
        "q_c10_interval_equivalent_ah": q_c10_interval,
        "reference_current_c10_a": i_ref_quick,
        "quick_peukert_k": peukert_k,
        "peukert_k": peukert_k,
        "peukert_k_source": getattr(profile, "peukert_k_source", "GENERIC_PROFILE_FALLBACK"),
        "quick_soc_start_pct": soc_start,
        "quick_soc_end_pct": soc_end,
        "quick_ocv_start_valid": bool(ocv_start_valid),
        "quick_ocv_end_valid": bool(ocv_end_valid),
        "quick_capacity_est_status": quick_capacity_status,
        "quick_capacity_est_ah": quick_capacity,
        "quick_full_capacity_est_ah": quick_capacity,
        "quick_capacity_est_valid": quick_capacity is not None,
        "quick_capacity_est_status": quick_capacity_status,
        "quick_soh_est_pct": quick_soh_est,
        "quick_soh_status": ("AVAILABLE" if np.isfinite(quick_soh_est)
                             else quick_capacity_status if is_quick_scan else "NOT_QUICK_SCAN"),
        "capacity_assessment_status": ("AVAILABLE" if capacity_gradeable else
                                       "NOT_AVAILABLE" if is_quick_scan and not np.isfinite(quick_soh_est)
                                       else "REVIEW"),
        "evidence_status": (quick_capacity_status if is_quick_scan and not np.isfinite(quick_soh_est)
                            else "AVAILABLE"),
        "quick_soh_est_valid": bool(np.isfinite(quick_soh_est)),
        "verified_capacity_ah": capacity if capacity_gradeable else None,
        "verified_soh_pct": soh if capacity_gradeable else None,
        "capacity_basis": capacity_basis, "capacity_norm_ah": cap_norm,
        "capacity_rate_normalized_ah": cap_norm, "mean_discharge_a": mean_dis,
        "peukert_k": getattr(profile, "peukert_k", 1.1),
        "capacity_reference_rate_hr": getattr(profile, "peukert_hr", 10.0),
        "capacity_reference_ah": profile.capacity_ah,
        "capacity_reference_current_a": (
            float(profile.capacity_10h_ah) / max(1.0, float(profile.peukert_hr))
            if profile.capacity_10h_ah > 0.0 else
            profile.capacity_ah / max(1e-9, getattr(profile, "peukert_hr", 10.0))),
        "quick_scan_reference_capacity_ah": profile.capacity_ah,
        "quick_scan_discharge_rate_c": 1.0 if is_quick_scan else float("nan"),
        "peukert_reference_capacity_ah": profile.capacity_ah,
        "peukert_reference_rate_hr": getattr(profile, "peukert_hr", 10.0),
        "peukert_reference_hr": getattr(profile, "peukert_hr", 10.0),
        "peukert_reference_current_a": i_ref_quick,
        "peukert_formula_version": "peukert-power-law-v1",
        "capacity_rating_basis": getattr(profile, "capacity_rating_basis", "UNKNOWN"),
        "capacity_basis_status": "CURRENT_PROFILE_BASIS",
        "capacity_basis_legacy": False,
        "dcir_mohm": dcir * 1000.0, "dcir_std_mohm": dcir_std * 1000.0,
        "dcir_n_steps": n_steps, "dcir_measured": measured, "dcir_temp_normalised": True,
        "dcir_source": ("MEASURED_MINI_PULSE_STEP_ON" if measured and is_quick_scan
                        else "MEASURED_RECORD" if measured else "PROFILE_FALLBACK"),
        "dcir_phase": "MINI_PULSE" if measured and is_quick_scan else None,
        "dcir_edge_type": "STEP_ON" if measured and is_quick_scan else None,
        "dcir_latency_s": dcir_latency,
        "dcir_timing_status": ("VALID" if np.isfinite(dcir_latency) and 0.0 < dcir_latency <= 0.5
                               else "INVALID_TIMESTAMP" if is_quick_scan else "NOT_APPLICABLE"),
        "ecm_temp_normalised": True,
        "dcir_slope_mohm": dcir_slope * 1000.0, "dcir_slope_r2": dcir_slope_r2,
        "ri_mohm": ecm["ri_total"] * 1000.0,
        "voltage_sag_v": sag, "cca_est_a": cca_est,
        "cca_est_25c_a": cca_est_25c,
        "cca_proxy_source": ("ECM_R0_PLUS_R1" if ecm["ecm_identified"]
                             else "DCIR_MEASURED" if measured
                             else "PROFILE_FALLBACK"),
        "cca_basis": "−18°C estimated cranking-current proxy; not SAE J537 CCA",
        "temperature_median_c": t_med,
        "ocv_v": ocv,
        "grade": grade, "overall_grade": grade, "gradeable": gradeable,
        "overall_gradeable": gradeable,
        "quick_grade": quick_grade, "quick_gradeable": quick_gradeable,
        "is_quick_scan": is_quick_scan,
        "quick_grade_basis": quick_grade_basis, "quick_grade_confidence": quick_confidence,
        "capacity_grade": capacity_grade, "capacity_gradeable": capacity_gradeable,
        "electrical_grade": electrical_grade, "electrical_gradeable": electrical_gradeable,
        "confidence": confidence, "quality_warnings": warnings, "temp_drift_c": temp_drift,
        "r0_mohm": ecm["r0"] * 1000.0,
        "r0_fit_mohm": ecm["r0_fit"] * 1000.0,
        "r0_release_mohm": ecm["r0_release"] * 1000.0,
        "r0_release_n": ecm["r0_release_n"],
        "r0_method": ecm["r0_method"],
        "r1_mohm": ecm["r1"] * 1000.0, "c1_farad": ecm["c1"], "tau_s": ecm["tau"],
        "ecm_identified": ecm["ecm_identified"], "ecm_r2": ecm["r2_ecm_fit"], "ecm_fit_t_s": ecm["ecm_fit_t_s"],
        "ecm_rmse_mv": ecm["rmse_v"] * 1000.0,
        "ecm_model": "2RC" if ecm["is_2rc"] else "1RC",
        "r2_mohm": ecm["r2_rc"] * 1000.0, "c2_farad": ecm["c2"], "tau2_s": ecm["tau2"],
        # FreedomCAR-style DC resistance at 0.1/1/10 s (G5) — NaN when no ECM fit.
        "r_at_0p1s_mohm": ecm["r_0p1s"] * 1000.0, "r_at_1s_mohm": ecm["r_1s"] * 1000.0,
        "r_at_10s_mohm": ecm["r_10s"] * 1000.0,
        "ica": (ica_v, ica),
        # FreedomCAR/SAE J537-style R@0.1s/1s/10s — see identify_dcir_at_timepoints.
        # {timepoint_s: {"r_mohm", "std_mohm", "n_steps"}}; a timepoint no pulse
        # in this record was long enough to reach is simply absent, not zeroed.
        "dcir_timepoints_mohm": {
            tp: {"r_mohm": r * 1000.0, "std_mohm": std * 1000.0, "n_steps": n}
            for tp, (r, std, n) in dcir_timepoints.items()
        },
        # Per-pulse HPPC breakdown — [] for non-HPPC or <2 qualifying pulses.
        "hppc_pulses": hppc_pulses,
        "hppc_anchor_drift_v": hppc_anchor_drift_v,
        "hppc_r0_cv_pct": hppc_r0_cv_pct,
    }



def _read_csv(path):
    """Read usable telemetry rows plus their acquisition-quality evidence.

    INVALID rows are retained in the raw CSV for auditability but must never
    influence electrical/capacity metrics.  GAP rows remain measurable data;
    their phase is returned so the caller can withhold a grade if the gap is at
    a pulse edge or near the discharge cut-off.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        # Skip metadata header lines starting with '#' (e.g. provenance metadata)
        lines = (line for line in f if not line.lstrip().startswith('#'))
        reader = csv.DictReader(lines)
        hdr = {h.strip().lower(): h for h in (reader.fieldnames or [])}

        def col(name):
            return hdr.get(name.lower())

        c_t, c_v, c_i = col("Elapsed_s"), col("Voltage_V"), col("Current_A")
        c_temp, c_cap, c_mode = col("Temperature_C"), col("Capacity_Ah"), col("Mode")
        c_soc, c_quality, c_phase = col("SoC_pct"), col("Sample_Quality"), col("Phase")
        T, V, I, TEMP, CAP, SOC, modes, sample_quality = [], [], [], [], [], [], [], []
        quality = {"invalid_excluded": 0, "gap_phases": [], "row_quality": []}
        for r in reader:
            def num(c, default=float("nan")):
                try:
                    return float(r[c]) if c else default
                except (ValueError, TypeError, KeyError):
                    return default
            row_quality = str(r.get(c_quality) or "VALID").strip().upper() if c_quality else "VALID"
            if row_quality == "INVALID":
                quality["invalid_excluded"] += 1
                if quality["row_quality"]:
                    quality["row_quality"][-1] = "GAP"
                quality["pending_invalid"] = True
                continue
            mode = r[c_mode] if c_mode else ""
            phase = r[c_phase] if c_phase else mode
            if row_quality == "GAP":
                quality["gap_phases"].append(str(phase or mode or "UNLABELLED").strip().upper())
            T.append(num(c_t)); V.append(num(c_v)); I.append(num(c_i))
            TEMP.append(num(c_temp, 25.0)); CAP.append(num(c_cap)); SOC.append(num(c_soc))
            modes.append(mode)
            sample_quality.append("GAP" if quality.pop("pending_invalid", False) else row_quality)
            quality["row_quality"].append(sample_quality[-1])
    return (np.asarray(T, float), np.asarray(V, float), np.asarray(I, float),
            np.asarray(TEMP, float), np.asarray(CAP, float), np.asarray(SOC, float), modes,
            quality)


def _apply_session_outcome(csv_path: str, result: dict) -> dict:
    """Expose a terminal session outcome and withhold a grade for failed runs.

    The CSV still deserves analysis after an interlock/fault — it can explain
    why a battery failed — but a partial trace is not evidence of a completed
    acceptance test.  Old CSVs without the new metadata sidecar remain
    analyzable as before; a still-running session remains provisional until it
    is closed.
    """
    try:
        with open(csv_path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return result
    outcome = str(meta.get("status", "")).strip().lower()
    if not outcome:
        return result
    result["session_outcome"] = outcome
    result["session_end_reason"] = meta.get("end_reason", "")
    result["session_complete"] = outcome == "completed"
    campaign = meta.get("validation_campaign")
    if isinstance(campaign, dict) and campaign.get("enabled"):
        evidence = meta.get("validation_evidence") or {}
        try:
            from aset_batt.core.validation_campaign import validation_verdict
            result["validation_campaign"] = campaign
            result["validation_verdict"] = validation_verdict(
                campaign, evidence.get("ambient") or {}, evidence.get("sampling") or {},
                bool(result.get("capacity_gradeable")),
            )
        except Exception as exc:
            result.setdefault("quality_warnings", []).append(
                f"validation evidence unavailable: {exc}")
    if outcome in {"aborted", "cancelled", "safety_tripped", "fault", "interrupted"}:
        warning = (f"session outcome is {outcome.replace('_', ' ')}"
                   + (f": {result['session_end_reason']}" if result["session_end_reason"] else ""))
        warnings = result.setdefault("quality_warnings", [])
        if warning not in warnings:
            warnings.append(warning)
        # Keep calculated resistance/capacity fields for diagnosis, but prevent
        # a report or dashboard from presenting the session as an accepted A/B/C
        # result.  A later complete re-run is the only way to restore grading.
        result.update({
            "grade": "REVIEW", "overall_grade": "REVIEW",
            "gradeable": False, "overall_gradeable": False,
        })
    return result


def _quick_start_soc_from_metadata(fallback_soc: float | None, *,
                                   quick_scan: bool, session_meta: dict):
    """Prefer the valid pre-MINI_PULSE OCV anchor recorded by Quick Scan."""
    if quick_scan and session_meta.get("ocv_start_valid"):
        try:
            return float(session_meta["ocv_start_soc_pct"])
        except (KeyError, TypeError, ValueError):
            return None
    return fallback_soc


def _offline_legacy_quick_metrics(t, i, v, modes, profile):
    """Recover independently useful Quick Scan metrics from historical raw rows."""
    t, i, v = (np.asarray(x, float) for x in (t, i, v))
    labels = [str(x or "").strip().upper() for x in modes]
    recorded = any(labels)
    phase_source = "RECORDED_PHASE" if recorded else "LEGACY_INFERRED_PHASE"
    dt = np.diff(t)
    valid_dt = np.isfinite(dt) & (dt > 0)
    charge = np.clip(i, 0.0, None)
    q_total = float(np.sum((charge[:-1] + charge[1:]) * 0.5 * np.where(valid_dt, dt, 0.0)) / 3600.0)

    if recorded and len(labels) == len(i):
        mini = np.asarray([x == "MINI_PULSE" for x in labels])
        main = np.asarray([x == "MAIN_DISCHARGE" for x in labels])
        active = mini | main
    else:
        # Legacy Quick Scan has a short diagnostic pulse followed by the longer
        # continuous discharge. Identify pulse regions from current changes.
        active_i = np.where(i > max(0.5, 0.15 * float(np.nanmax(i))))[0]
        mini = np.zeros(i.size, bool)
        main = np.zeros(i.size, bool)
        active = np.zeros(i.size, bool)
        if active_i.size:
            starts = np.r_[active_i[0], active_i[1:][np.diff(active_i) > 1]]
            ends = np.r_[starts[1:] - 1, active_i[-1]]
            if starts.size:
                mini[starts[0]:ends[0] + 1] = True
                for a, b in zip(starts[1:], ends[1:]):
                    main[a:b + 1] = True
                active = mini | main

    def integrate(mask):
        if mask.size != i.size or np.count_nonzero(mask) < 2:
            return float("nan")
        idx = np.where(mask)[0]
        edges = np.arange(idx[0], idx[-1])
        use = mask[edges] & mask[edges + 1] & valid_dt[edges]
        selected_edges = edges[use]
        return float(np.sum((charge[selected_edges] + charge[selected_edges + 1]) * 0.5 * dt[selected_edges]) / 3600.0)

    q_mini, q_main = integrate(mini), integrate(main)
    q_interval = q_total if not np.any(active) else float(np.nansum([q_mini, q_main]))
    loaded = active & np.isfinite(i) & (i > 0.05)
    loaded_edges = loaded[:-1] & loaded[1:] & valid_dt
    loaded_dt = float(np.sum(dt[loaded_edges]))
    loaded_ah = (float(np.sum((charge[:-1][loaded_edges] + charge[1:][loaded_edges])
                              * 0.5 * dt[loaded_edges]) / 3600.0)
                 if loaded_edges.any() else float("nan"))
    mean_i = loaded_ah * 3600.0 / loaded_dt if loaded_dt > 0.0 else float("nan")
    k = float(getattr(profile, "peukert_k", 0.0) or 0.0)
    c10_capacity = float(getattr(profile, "capacity_10h_ah", 0.0) or 0.0)
    rating_valid = getattr(profile, "capacity_rating_validated", None) is True
    rated_capacity = float(getattr(profile, "capacity_ah", 0.0) or 0.0)
    rated_hours = float(getattr(profile, "peukert_hr", 0.0) or 0.0)
    if c10_capacity > 0.0 and rating_valid:
        i_ref = c10_capacity / 10.0
    elif rating_valid and str(getattr(profile, "capacity_rating_basis", "")).upper() == "C10" \
            and rated_capacity > 0.0 and rated_hours > 0.0:
        i_ref = rated_capacity / rated_hours
    else:
        i_ref = float("nan")
    q_c10 = (q_interval * (mean_i / i_ref) ** (k - 1.0)
             if np.isfinite(q_interval) and np.isfinite(mean_i) and mean_i > 0
             and np.isfinite(i_ref) and i_ref > 0.0 and k > 0.0 else float("nan"))

    # Use the actual first pulse-on edge. Its first sample must be within 0.5s.
    candidates = np.where((i[1:] - i[:-1]) > max(0.05, 0.2 * float(np.nanmax(i))))[0]
    dcir = latency = float("nan")
    for edge in candidates:
        if recorded and (edge + 1 >= len(labels) or labels[edge + 1] != "MINI_PULSE"):
            continue
        latency = float(t[edge + 1] - t[edge])
        if latency <= 0.5 and i[edge + 1] > 0:
            dcir = abs(float(v[edge + 1] - v[edge]) / float(i[edge + 1] - i[edge])) * 1000.0
            break
    return {
        "phase_detection_source": phase_source,
        "charge_removed_ah": q_interval,
        "q_interval_removed_ah": q_interval,
        "charge_removed_source": "REANALYZED_FROM_RAW_DATA",
        "q_mini_ah": q_mini, "q_main_ah": q_main,
        "mean_discharge_current_a": mean_i,
        "quick_mean_discharge_a": mean_i,
        "reference_current_c10_a": i_ref,
        "peukert_k": k,
        "c10_equivalent_interval_charge_ah": q_c10,
        "q_c10_interval_equivalent_ah": q_c10,
        "peukert_factor_legacy_reanalysis": (q_c10 / q_interval if np.isfinite(q_c10) and q_interval > 0 else float("nan")),
        "peukert_factor": (q_c10 / q_interval if np.isfinite(q_c10) and q_interval > 0 else float("nan")),
        "capacity_basis_status": "VALIDATED_C10_PROFILE" if np.isfinite(q_c10) else "C10_BASIS_UNAVAILABLE",
        "dcir_reanalyzed_mohm": dcir if np.isfinite(dcir) and latency <= 0.5 else float("nan"),
        "dcir_reanalyzed_latency_s": latency,
        "dcir_reanalyzed_source": "REANALYZED_FROM_LEGACY_RAW_DATA" if np.isfinite(dcir) and latency <= 0.5 else "UNAVAILABLE",
        "recorded_rest_voltage_v": float(v[0]) if v.size else float("nan"),
        "validated_start_ocv_v": float("nan"), "validated_end_ocv_v": float("nan"),
        "quick_soh_current_method_pct": float("nan"),
        "quick_full_capacity_est_ah": float("nan"),
        "quick_soh_est_pct": float("nan"),
        "quick_soh_current_method_reason": "Historical end OCV does not satisfy current Quick OCV validation",
        "start_ocv_status": "RECORDED_REST_VOLTAGE_UNVALIDATED",
        "end_ocv_status": "LEGACY_UNVERIFIED",
    }


def _read_historical_csv_result(csv_path):
    """Read optional historical grade/capacity columns without using them as current results."""
    out = {}
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            lines = (line for line in handle if not line.lstrip().startswith("#"))
            reader = csv.DictReader(lines)
            rows = list(reader)
        if not rows:
            return out
        aliases = {
            "grade": ("Historical_Grade", "Grade"),
            "soh": ("Historical_SoH_pct", "SoH_pct", "SoH"),
            "capacity_ah": ("Historical_Capacity_Ah", "Capacity_Ah"),
            "analysis_version": ("Analysis_Version",),
        }
        lookup = {str(k).strip().lower(): k for k in (reader.fieldnames or [])}
        for target, names in aliases.items():
            col_name = next((lookup[n.lower()] for n in names if n.lower() in lookup), None)
            if col_name is None:
                continue
            value = next((row.get(col_name) for row in reversed(rows)
                          if row.get(col_name) not in (None, "")), None)
            if value is not None:
                if target == "grade":
                    out[target] = value
                elif target == "analysis_version":
                    out[target] = value
                else:
                    try:
                        out[target] = float(value)
                    except (TypeError, ValueError):
                        pass
    except (OSError, csv.Error, UnicodeError):
        return out
    return out


def _has_current_quick_schema(csv_path):
    """Whether a CSV carries the production schema and Quick procedure record."""
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            lines = (line for line in handle if not line.lstrip().startswith("#"))
            reader = csv.DictReader(lines)
            headers = {str(name).strip().lower() for name in (reader.fieldnames or [])}
        required = {"schema_version", "session_id", "test_type", "phase",
                    "sample_quality", "temperature_c", "elapsed_s",
                    "voltage_v", "current_a"}
        if not required.issubset(headers):
            return False
        with open(csv_path + ".meta.json", encoding="utf-8") as handle:
            metadata = json.load(handle)
        protocol = metadata.get("protocol") or {}
        protocol_id = str(protocol.get("id", "")).strip().lower()
        test_type = str(metadata.get("test_type", "")).strip().lower()
        return (str(metadata.get("schema_version", "")) == "2.3"
                and protocol_id == "quick-scan-v2"
                and "quick" in test_type)
    except (OSError, csv.Error, UnicodeError, ValueError, TypeError):
        return False


def analyze_csv(csv_path: str, profile: BatteryProfile, force_hppc: bool = False,
                fit_ecm=None, offline_legacy: bool = False) -> dict:
    """Parse a telemetry CSV and run the unified analysis. HPPC is inferred from
    the ``Mode`` column; capacity is integrated from current if not logged.

    ``fit_ecm``: see ``analyze_series`` — pass ``True`` to attempt a pulse fit
    on a non-HPPC record (e.g. Quick Scan's mini-pulse leg) without also
    setting ``force_hppc`` (which would incorrectly suppress SoH)."""
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path or "(no CSV)")
    try:
        with open(csv_path + ".meta.json", encoding="utf-8") as handle:
            session_meta = json.load(handle)
    except (OSError, ValueError, TypeError):
        session_meta = {}
    current_peukert_k = float(getattr(profile, "peukert_k", 1.1))
    current_peukert_source = getattr(profile, "peukert_k_source", "GENERIC_PROFILE_FALLBACK")
    is_quick_scan = ("quickscan" in os.path.basename(csv_path).lower()
                     or "quick" in str(session_meta.get("test_type", "")).lower())
    historical_peukert_k = None
    historical_peukert_source = "LEGACY_UNKNOWN"
    try:
        saved_k = session_meta.get("peukert_k") if is_quick_scan else None
        if saved_k is not None and np.isfinite(float(saved_k)) and float(saved_k) > 0.0:
            historical_peukert_k = float(saved_k)
            historical_peukert_source = str(
                session_meta.get("peukert_k_source") or "LEGACY_UNKNOWN")
    except (TypeError, ValueError):
        pass
    # Stored session k is the reproducible assumption for reanalysis. A file
    # without it is explicitly a current-profile reanalysis. Replace only this
    # immutable value object; do not mutate today's selected profile/config.
    if historical_peukert_k is not None:
        from dataclasses import replace as _replace
        historical_values = {
            "peukert_k": historical_peukert_k,
            "peukert_k_source": historical_peukert_source,
        }
        for field, meta_key in (("peukert_hr", "peukert_reference_hr"),
                                ("peukert_reference_hr", "peukert_reference_hr"),
                                ("peukert_reference_current_a", "peukert_reference_current_a"),
                                ("capacity_10h_ah", "peukert_reference_capacity_ah"),
                                ("capacity_ah", "peukert_reference_capacity_ah")):
            value = session_meta.get(meta_key)
            if value is not None:
                try:
                    historical_values[field] = float(value)
                except (TypeError, ValueError):
                    pass
        profile = _replace(profile, **historical_values)
    historical_basis_complete = (historical_peukert_k is not None and
                                  session_meta.get("peukert_reference_hr") is not None and
                                  session_meta.get("peukert_reference_current_a") is not None and
                                  session_meta.get("peukert_reference_capacity_ah") is not None and
                                  session_meta.get("peukert_formula_version") is not None)
    t, v, i, temp, cap, soc, modes, acquisition_quality = _read_csv(csv_path)
    sample_quality = acquisition_quality.get("row_quality", [])
    if t.size < 2:
        raise ValueError("CSV has too few samples to analyse.")
    is_hppc = force_hppc or any("hppc" in (m or "").lower() for m in modes)
    # New sessions expose the protocol through Mode/Phase.  Filename detection
    # retains correct grading semantics when replaying legacy QuickScan files
    # that predate phase provenance.
    is_quick_scan = ("quickscan" in os.path.basename(csv_path).lower()
                     or "quick" in str(session_meta.get("test_type", "")).lower())
    phase_recorded = any(str(m or "").strip() for m in modes)
    # Select File uses offline_legacy=True for every saved file.  Current
    # session files must still use the unified analyzer so their recorded
    # timing quality, OCV evidence, and protocol basis are honored.  Require
    # both the current row schema and an explicit Quick protocol: filenames
    # and phase labels alone can also occur in historical/exported CSVs.
    offline_legacy = bool(offline_legacy and not _has_current_quick_schema(csv_path))
    if offline_legacy and is_quick_scan and not any(str(m or "").strip() for m in modes):
        # Give the unified analyzer conservative inferred labels so legacy Quick
        # traces use the same pulse-scoped gates without requiring Phase/Mode.
        raw_mask = np.asarray(i, float) > max(0.5, 0.15 * float(np.nanmax(i)))
        starts = np.where(raw_mask & ~np.r_[False, raw_mask[:-1]])[0]
        ends = np.where(raw_mask & ~np.r_[raw_mask[1:], False])[0]
        inferred = np.full(len(i), "", dtype=object)
        if starts.size:
            inferred[starts[0]:ends[0] + 1] = "MINI_PULSE"
            for a, b in zip(starts[1:], ends[1:]):
                inferred[a:b + 1] = "MAIN_DISCHARGE"
        modes = inferred.tolist()
    if np.all(np.isnan(cap)):                       # no capacity column → integrate
        dt = np.diff(t, prepend=t[0])
        cap = np.cumsum(np.clip(i, 0, None) * dt) / 3600.0
    if offline_legacy:
        # Offline legacy inspection is independent from current certification
        # gates. Reconstruct each metric from raw columns and keep historical
        # result fields only as separate provenance.
        raw = _offline_legacy_quick_metrics(t, i, v, modes, profile)
        if not phase_recorded:
            raw["phase_detection_source"] = "LEGACY_INFERRED_PHASE"
        try:
            from aset_batt.storage.data_utils import DataHandler
            integrity = DataHandler.verify_integrity(csv_path)
        except Exception:
            integrity = None
        integrity_status = ("HASH_VERIFIED" if integrity is True else
                            "HASH_MISMATCH" if integrity is False else
                            "INTEGRITY_HASH_UNAVAILABLE")
        historical = session_meta
        first_row = {}
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
                lines = (line for line in handle if not line.lstrip().startswith("#"))
                first_reader = csv.DictReader(lines)
                first_row = next(first_reader, {}) or {}
        except (OSError, csv.Error, UnicodeError):
            pass
        hist_result = {k: historical[k] for k in
                       ("analysis_version", "capacity_basis_version", "rated_capacity_ah",
                        "capacity_ah", "soh", "grade", "quick_soh_est_pct",
                        "historical_grade", "historical_soh_pct", "historical_capacity_ah",
                        "peukert_k", "peukert_k_source", "peukert_factor",
                        "q_c10_interval_equivalent_ah", "quick_full_capacity_est_ah")
                       if historical.get(k) is not None}
        for key, value in _read_historical_csv_result(csv_path).items():
            hist_result.setdefault(key, value)
        protocol = historical.get("protocol") or {}
        if protocol.get("analysis_version"):
            hist_result.setdefault("analysis_version", protocol["analysis_version"])
        if historical.get("historical_grade") is not None:
            hist_result.setdefault("grade", historical["historical_grade"])
        if historical.get("historical_soh_pct") is not None:
            hist_result.setdefault("quick_soh_est_pct", historical["historical_soh_pct"])
        if historical.get("historical_capacity_ah") is not None:
            hist_result.setdefault("capacity_ah", historical["historical_capacity_ah"])
        return {
            **raw,
            "historical_peukert_k": historical_peukert_k,
            "historical_peukert_k_source": historical_peukert_source,
            "current_reanalysis_peukert_k": current_peukert_k,
            "current_reanalysis_peukert_k_source": current_peukert_source,
            "peukert_reanalysis_basis": ("HISTORICAL_BASIS_INCOMPLETE" if historical_peukert_k is not None and not historical_basis_complete
                                          else "HISTORICAL_STORED" if historical_peukert_k is not None
                                          else "REANALYZED_WITH_CURRENT_PROFILE"),
            "peukert_k": float(profile.peukert_k),
            "peukert_k_source": getattr(profile, "peukert_k_source", "LEGACY_UNKNOWN"),
            "peukert_reference_hr": float(profile.peukert_hr),
            "peukert_reference_current_a": raw.get("reference_current_c10_a"),
            "analysis_layer": "OFFLINE_CURRENT_REANALYSIS",
            "dataset_status": ("CORRUPT" if integrity_status == "HASH_MISMATCH" else
                               "LEGACY_COMPATIBLE" if np.isfinite(raw["charge_removed_ah"])
                               and raw["charge_removed_ah"] > 0.0 else "LEGACY_LIMITED"),
            "compatibility_status": ("RAW_ONLY_LEGACY" if not historical else "LEGACY_COMPATIBLE"),
            "current_reanalysis_status": ("PARTIAL_METRICS" if not np.isfinite(raw["c10_equivalent_interval_charge_ah"])
                                          or not np.isfinite(raw["dcir_reanalyzed_mohm"])
                                          else "COMPLETE_METRICS"),
            "electrical_status": ("MEASURED_DCIR_GRADE_UNVERIFIED"
                                  if np.isfinite(raw["dcir_reanalyzed_mohm"])
                                  else "DCIR_UNAVAILABLE"),
            "overall_displayed_status": "N/A",
            "integrity_status": integrity_status,
            "historical_result": hist_result,
            "file_information": {
                "filename": os.path.basename(csv_path),
                "size_bytes": os.path.getsize(csv_path),
                "session_id": historical.get("session_id") or first_row.get("Session_ID") or "N/A",
                "test_type": (historical.get("test_type") or first_row.get("Test_Type")
                              or ("Quick Scan (filename inferred)" if "quick" in os.path.basename(csv_path).lower()
                                  else first_row.get("Mode") or "N/A")),
                "acquisition_date": first_row.get("Timestamp") or historical.get("started_at") or "N/A",
                "app_version": historical.get("app_version") or "N/A",
                "analysis_version": historical.get("analysis_version")
                                   or (historical.get("protocol") or {}).get("analysis_version") or "N/A",
            },
            "dataset_notes": (["Historical CSV reanalyzed from its recorded samples"]
                              + ([] if historical else ["No metadata sidecar; analyzed raw CSV only"])),
            "grade": "N/A", "overall_grade": "N/A", "gradeable": False,
            "overall_gradeable": False, "capacity_grade": "N/A", "quick_grade": "N/A",
            "electrical_grade": "N/A", "quality_warnings": [],
            "capacity_ah": raw["charge_removed_ah"],
            "dcir_mohm": raw["dcir_reanalyzed_mohm"],
            "dcir_measured": np.isfinite(raw["dcir_reanalyzed_mohm"]),
            "soh": float("nan"), "soh_est": float("nan"),
            "quick_soh_est_pct": float("nan"),
        }
    # Quick Scan's SoC anchor is the validated OCV-derived value before MINI_PULSE.
    # Other analyses retain the historical pre-main-discharge anchoring behavior.
    # This bypasses artificially high initial SoC caused by surface charge.
    soc_start = None
    if soc.size and not np.all(np.isnan(soc)):
        _modes_upper = [str(m).strip().upper() for m in modes]
        if "MAIN_DISCHARGE" in _modes_upper:
            first_md_idx = _modes_upper.index("MAIN_DISCHARGE")
            if first_md_idx > 0:
                for idx in range(first_md_idx - 1, -1, -1):
                    if not np.isnan(soc[idx]):
                        soc_start = float(soc[idx])
                        break

        # Fallback if no MAIN_DISCHARGE mode is found (e.g. older files without Mode col)
        if soc_start is None:
            active_idx = np.where(i > 0.5)[0]
            if active_idx.size > 0:
                edge_idx = active_idx[0]
                if edge_idx > 0:
                    for idx in range(edge_idx - 1, -1, -1):
                        if not np.isnan(soc[idx]):
                            soc_start = float(soc[idx])
                            break

        # Ultimate fallback (e.g. no discharge at all or very start)
        if soc_start is None:
            soc_start = float(np.nanmax(soc))
    # Validation HPPC/GITT sessions use a C10-calibrated Ah ruler if the
    # campaign already captured one.  Routine sessions retain their estimator
    # SoC exactly as before.
    soc_series = soc if soc.size and not np.all(np.isnan(soc)) else None
    reference_capacity_ah = None
    try:
        campaign = (session_meta.get("validation_campaign") or {})
        candidate = float(campaign.get("reference_capacity_ah"))
        if campaign.get("enabled") and candidate > 0.0:
            from aset_batt.core.validation_campaign import reference_soc_from_capacity
            soc_series = np.asarray(reference_soc_from_capacity(cap, candidate), float)
            soc_start = float(soc_series[0]) if soc_series.size else soc_start
            reference_capacity_ah = candidate
    except (ValueError, TypeError):
        pass
    soc_start = _quick_start_soc_from_metadata(
        soc_start, quick_scan=is_quick_scan, session_meta=session_meta)
    result = analyze_series(t, i, v, temp, cap, profile, is_hppc,
                            soc_start=soc_start, soc_series=soc_series,
                            fit_ecm=fit_ecm, modes=modes, quick_scan=is_quick_scan,
                            ocv_start_valid=session_meta.get("ocv_start_valid"),
                            soc_end=session_meta.get("ocv_end_soc_pct"),
                            ocv_end_valid=session_meta.get("ocv_end_valid", False),
                            sample_quality=sample_quality)
    if is_quick_scan:
        result.update({
            "historical_peukert_k": historical_peukert_k,
            "historical_peukert_k_source": historical_peukert_source,
            "current_reanalysis_peukert_k": current_peukert_k,
            "current_reanalysis_peukert_k_source": current_peukert_source,
            "peukert_reanalysis_basis": ("HISTORICAL_BASIS_INCOMPLETE" if historical_peukert_k is not None and not historical_basis_complete
                                          else "HISTORICAL_STORED" if historical_peukert_k is not None
                                          else "REANALYZED_WITH_CURRENT_PROFILE"),
        })
    # Keep the worker's historical EKF feedback anchor without retaining a
    # second per-sample SoC history during acquisition. This is a pure nearest-
    # timestamp lookup over the same validated series used by the fit.
    fit_t = result.get("ecm_fit_t_s")
    fit_soc_values = soc_series if soc_series is not None else soc
    if (fit_t is not None and np.isfinite(fit_t) and fit_soc_values is not None
            and len(fit_soc_values) == len(t) and len(t)):
        fit_soc_idx = int(np.argmin(np.abs(t - fit_t)))
        fit_soc = float(fit_soc_values[fit_soc_idx])
        if np.isfinite(fit_soc):
            result["ecm_fit_soc_pct"] = fit_soc
    # Never relabel a historical YTZ6V measurement using today's corrected
    # product profile. Old CSVs and sidecars remain untouched; their report
    # records that the capacity basis predates the explicit C10/C20 split.
    recorded_product = str(session_meta.get("product_name") or
                           session_meta.get("battery_product") or
                           getattr(profile, "name", "") or "").lower()
    is_ytz6v = "ytz6v" in recorded_product
    if is_ytz6v and session_meta.get("capacity_basis_version") != "ytz6v-c10-c20-v1" and not offline_legacy:
        result["capacity_basis_status"] = "LEGACY_HISTORICAL_CAPACITY_BASIS"
        result["capacity_basis_legacy"] = True
        warning = ("YTZ6V session uses a historical capacity basis; current C10/C20 "
                   "profile values were not applied to this recorded measurement")
        result.setdefault("quality_warnings", []).append(warning)
        # The measured Ah remains visible for traceability, but historical
        # metadata cannot support a current C10 SoH denominator or grade.
        result.update({"capacity_grade": "REVIEW", "capacity_gradeable": False,
                       "verified_capacity_ah": None, "verified_soh_pct": None,
                       "grade": "REVIEW", "overall_grade": "REVIEW",
                       "gradeable": False, "overall_gradeable": False})
    if reference_capacity_ah is not None:
        result["reference_soc_source"] = "c10_capacity"
        result["reference_capacity_ah"] = reference_capacity_ah
    invalid_n = acquisition_quality["invalid_excluded"]
    if invalid_n:
        result.setdefault("quality_warnings", []).append(
            f"{invalid_n} INVALID CSV row(s) excluded before analysis")
    critical_gap_phases = {"MINI_PULSE", "DISCHARGE_PULSE", "REGEN_PULSE",
                           "NEAR_CUTOFF", "MAIN_DISCHARGE"}
    critical_gaps = sorted(set(acquisition_quality["gap_phases"]) & critical_gap_phases)
    if critical_gaps:
        result.setdefault("quality_warnings", []).append(
            "sampling GAP in critical phase(s): " + ", ".join(critical_gaps))
        # A gap over a pulse edge corrupts DCIR/ECM; a gap around the cut-off
        # corrupts capacity.  Preserve diagnostics, but never certify a grade.
        result.update({"grade": "REVIEW", "overall_grade": "REVIEW",
                       "gradeable": False, "overall_gradeable": False})
    if is_quick_scan and "MINI_PULSE" in acquisition_quality["gap_phases"]:
        result.update({"dcir_measured": False, "dcir_source": "PROFILE_FALLBACK",
                       "dcir_n_steps": 0, "dcir_timepoints_mohm": {},
                       "ecm_identified": False, "electrical_grade": "REVIEW",
                       "electrical_gradeable": False, "grade": "REVIEW",
                       "overall_grade": "REVIEW", "gradeable": False,
                       "overall_gradeable": False})
        result.setdefault("quality_warnings", []).append(
            "Quick Scan MINI_PULSE contains GAP samples; measured DCIR/ECM evidence withheld")
    # Offline inspection is diagnostic. An old terminal status must not erase
    # independently reconstructed electrical metrics or turn the page into a
    # generic REVIEW result.
    return _apply_session_outcome(csv_path, result)


_analysis_pool: ProcessPoolExecutor | None = None


def _get_analysis_pool() -> ProcessPoolExecutor:
    """Lazily-started, process-wide worker pool for analyze_csv_mp()."""
    global _analysis_pool
    if _analysis_pool is None:
        _analysis_pool = ProcessPoolExecutor(max_workers=1)
    return _analysis_pool


def shutdown_analysis_pool():
    """Tear down the analysis worker pool, cancelling any queued fits.

    Called from the GUI's closeEvent. Without this, a long scipy curve_fit still
    running when the user quits keeps the child process (and its CPU) alive, and the
    interpreter blocks on atexit joining it — the app appears to hang after close.
    cancel_futures drops anything still queued; the one in-flight fit can't be killed
    mid-C-call but is short, and wait=False means we don't block the UI teardown on it."""
    global _analysis_pool
    if _analysis_pool is not None:
        try:
            _analysis_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:                       # cancel_futures added in Python 3.9
            _analysis_pool.shutdown(wait=False)
        _analysis_pool = None


def analyze_csv_mp(csv_path: str, profile: BatteryProfile, force_hppc: bool = False,
                   fit_ecm=None, offline_legacy: bool = False) -> dict:
    """Same result as analyze_csv(), but the ECM curve-fit (scipy.optimize.curve_fit,
    up to ~10k iterations, run from a background thread after every auto sequence)
    executes in a separate worker process instead of a thread.

    curve_fit's Python-level callback holds the GIL for the whole fit, so even
    though the caller is already off the Qt main thread, a plain threading.Thread
    still starves the UI event loop of the GIL and Windows reports "Not Responding"
    for the ~5-15s the fit takes. A separate process has its own GIL, so the UI
    thread keeps pumping events while this call blocks on the subprocess result.
    """
    future = _get_analysis_pool().submit(analyze_csv, csv_path, profile, force_hppc, fit_ecm, offline_legacy)
    return future.result()


def analyze_series_mp(time_s, current_a, voltage_v, temp_c, capacity_series,
                      profile: BatteryProfile, is_hppc: bool, soh=None,
                      soc_start=None, soc_series=None, fit_ecm=None, modes=None,
                      quick_scan: bool | None = None) -> dict:
    """Same result as analyze_series(), but off the calling thread's GIL — see
    analyze_csv_mp's docstring. AcquisitionWorker.run() (the Characterization /
    RUN TEST / HPPC-via-RUN-TEST QThread) calls this directly with its in-memory
    sample arrays instead of round-tripping through a CSV, so it needed its own
    process-pool twin; routing it through analyze_csv_mp would have meant writing
    an extra throwaway CSV just to satisfy that wrapper's file-path signature."""
    future = _get_analysis_pool().submit(
        analyze_series, time_s, current_a, voltage_v, temp_c, capacity_series,
        profile, is_hppc, soh, soc_start, soc_series, fit_ecm, modes, quick_scan)
    return future.result()
