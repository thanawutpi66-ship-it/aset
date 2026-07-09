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
from concurrent.futures import ProcessPoolExecutor

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
    # Peukert exponent by chemistry: lead-acid capacity is markedly rate-dependent,
    # lithium almost not.
    peukert = 1.20 if "lead" in (b.battery_type or "").lower() else 1.05

    # A characterised specimen's R0/R1 (see aset_batt.core.battery_profiles.
    # save_measured_params) overrides the chemistry-generic base_rin/60-40 split for
    # this specific product — the chemistry-level r0 has no capacity/CCA scaling, so a
    # small pack can measure well above it even when genuinely healthy.
    r0_fraction = 0.0
    try:
        from aset_batt.core import battery_profiles
        mp = battery_profiles.get_measured_params(b.product_name)
        internal_r_ohm = mp.get("internal_r_ohm")
        if internal_r_ohm and float(internal_r_ohm) > 0:
            rin = float(internal_r_ohm)
            r0_fraction = float(mp.get("r0_fraction", 0.0))
    except Exception:
        pass

    return BatteryProfile(
        name=b.battery_type, chemistry=b.battery_type,
        nominal_v=b.pack_nominal_voltage, series=b.cells_series,
        capacity_ah=b.rated_capacity,
        max_charge_v=b.pack_max_voltage, cutoff_v=b.pack_min_voltage,
        max_charge_a=b.max_current, max_discharge_a=b.max_current,
        ovp=float(s.get("max_voltage", b.pack_max_voltage + 1)),
        uvp=float(s.get("min_voltage", b.pack_min_voltage - 1)),
        otp_warn=max(0.0, otp - 10.0), otp_crit=otp, internal_r=float(max(1e-4, rin)),
        peukert_k=peukert, r0_fraction=r0_fraction,
        harness_r_ohm=max(0.0, float(getattr(b, "harness_resistance_ohm", 0.0))),
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
# for a 5.3 Ah pack), so bulk-charge samples were misread as "rest" and their
# elevated (absorption-stage) voltage leaked into the ECM fit's OCV anchor.
_I_STANDBY = 0.0             # A

# DCIR reads R = ΔV/ΔI at the FIRST sample after a current edge, assuming that sample
# lands at the rig's steady ~200 ms readback. If the interval to that sample is much
# longer (USB hiccup, SCPI stall, OS jitter), the voltage has already relaxed into the
# RC region and R would include R1 (polarisation), not just R0 (ohmic) — inflating DCIR.
# Steps whose post-edge dt exceeds this are dropped and flagged rather than trusted.
_DCIR_MAX_STEP_DT = 0.5      # s

# SoH = measured discharge Ah ÷ rated ASSUMES the discharge began from a full pack.
# Below this starting SoC the capacity removed only spans SoC_start→0, so a healthy
# pack reads a proportionally LOW SoH (a 50 %-charged healthy pack → SoH ≈ 50 %).
# reached_cutoff can't catch it (it guards the END), so a known-partial start is flagged.
_SOH_MIN_START_SOC = 95.0    # %

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


def peukert_capacity(capacity_ah, mean_current_a, rated_ah, k, ref_c_rate=0.2):
    """Normalise a measured discharge capacity to a reference C-rate (Peukert's law).

    Available capacity falls as the discharge rate rises (strongly for lead-acid). With
    ``C = C_p / I^(k-1)``, a capacity measured at current ``I`` maps to the reference
    rate ``I_ref = ref_c_rate·rated`` by ``C_ref = C·(I/I_ref)^(k-1)`` — so a high-rate
    test isn't unfairly graded low. Lithium (k≈1.05) → almost no change."""
    i_ref = ref_c_rate * rated_ah
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
# drift rather than noise.
_VI_LEVEL_MAX_SPREAD_V = 0.15  # V


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


def identify_dcir(current_a, voltage_v, temp_c, profile: BatteryProfile, time_s=None):
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
    if ia.size < 4:
        return profile.internal_r, 0.0, 0, False, 0, 0
    temp_mult = _dcir_temp_normalizer(profile)
    di = np.diff(ia)
    thr = max(1e-3, 0.20 * float(np.max(np.abs(ia))))     # a real load edge, not jitter
    r_base = max(1e-4, float(profile.internal_r))
    vals = []
    n_stale = 0
    n_implausible = 0
    k = 0
    while k < di.size:
        if abs(di[k]) > thr:
            # dt-gate: the post-edge sample must land soon after the edge, or it has
            # relaxed into the RC region and R would carry R1, not just the ohmic R0.
            if ta is not None and (k + 1) < ta.size and (ta[k + 1] - ta[k]) > _DCIR_MAX_STEP_DT:
                n_stale += 1
                k += 2
                continue
            v_before = float(np.median(va[max(0, k - 2):k + 1]))   # rested/level baseline
            v_after = float(va[k + 1])                             # first post-edge sample
            r = abs((v_after - v_before) / di[k])
            T = float(tc[k + 1]) if (k + 1 < tc.size and not np.isnan(tc[k + 1])) else _T_REF
            r_norm = r / temp_mult(T)       # → 25 °C, chemistry-specific Arrhenius
            if not (0.2 * r_base <= r_norm <= 6.0 * r_base):
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
    if r2 < _ECM_MIN_R2 or res.get("R0_ohm", 0.0) <= 0:
        logger.info("1-RC ECM fit rejected (R²=%.3f) — using single-step DCIR", r2)
        return None, (f"fit quality too low (R²={r2:.2f} < {_ECM_MIN_R2:.2f}) — "
                      f"pulse noisy or too short for the RC tail")
    # Attempt 2-RC upgrade; reuse same identifier instance (same smoothing/thresholds).
    try:
        res_2rc = identifier.fit_model_2rc(time_s, current_a, voltage_v, voc,
                                           r1rc_result=res)
        if res_2rc is not None:
            res = res_2rc
    except Exception:
        pass
    return res, ""


def analyze_series(time_s, current_a, voltage_v, temp_c, capacity_series,
                   profile: BatteryProfile, is_hppc: bool, soh=None,
                   soc_start=None) -> dict:
    """Run the unified analysis on raw series → the standard results dict.

    ``current_a`` discharge-positive; ``capacity_series`` is the per-sample cumulative
    Ah; ``soh`` may be supplied (live estimator) else computed from capacity ÷ rated.

    Resistance: a temperature-normalised multi-step **DCIR@~250 ms** is always reported
    (robust, repeatable). For HPPC, a **1-RC ECM fit** additionally extracts R0/R1/C1/τ
    (R1/C1 are well-resolved at 5 Hz; R0 by t=0 extrapolation), used for two-resistance
    grading when its R² is good — with the DCIR kept as a cross-check and fallback.
    A confidence score and data-quality flags surface suspect measurements."""
    from aset_batt.acquisition.analytics import Analytics
    v = Analytics.hampel_filter(np.asarray(voltage_v, float))
    ia = Analytics.hampel_filter(np.asarray(current_a, float))
    q = np.asarray(capacity_series, float)
    capacity = float(q[-1]) if q.size else 0.0
    reached_cutoff = bool(v.size and float(np.min(v)) <= profile.cutoff_v * 1.02)
    # Mean discharge current (for Peukert rate-normalisation of capacity).
    dis = ia[ia > 0.05]
    mean_dis = float(np.mean(dis)) if dis.size else 0.0
    # Rate-normalised capacity: a fast discharge under-reads capacity (esp. lead-acid),
    # so normalise to the reference rate before SoH (Peukert). Lithium ≈ unchanged.
    cap_norm = peukert_capacity(capacity, mean_dis, profile.capacity_ah,
                                getattr(profile, "peukert_k", 1.1))
    # SoH = rate-normalised capacity ÷ rated, ONLY valid for a FULL discharge
    # (100% → cut-off). HPPC moves little charge, and a discharge stopped early
    # under-states capacity — both would mislead, so SoH is N/A (NaN) unless a full
    # discharge was seen (or the caller supplied a SoH).
    if soh is None:
        if (not is_hppc) and reached_cutoff and profile.capacity_ah:
            soh = 100.0 * cap_norm / profile.capacity_ah
        else:
            soh = float("nan")
    if not np.isnan(soh):
        soh = float(min(120.0, max(0.0, soh)))

    dcir, dcir_std, n_steps, measured, n_stale, n_implausible = identify_dcir(
        current_a, voltage_v, temp_c, profile, time_s=time_s)
    # Multi-current DCIR: when the record spans ≥2 distinct current levels, the slope of
    # V vs I gives an OCV-cancelling DCIR (more robust than a single step).
    levels = _vi_levels(current_a, voltage_v)
    if len(levels) >= 2:
        dcir_slope, dcir_slope_r2 = dcir_from_vi_slope(
            [p[0] for p in levels], [p[1] for p in levels])
    else:
        dcir_slope, dcir_slope_r2 = float("nan"), 0.0

    # Harness (test-rig wiring/contact) resistance: purely ohmic and in series with
    # everything the rig measures, so it inflates every ohmic reading by the same
    # fixed amount regardless of the pack's real health. Only ohmic readings (DCIR,
    # dcir_slope, ECM R0 below) are corrected; R1/C1/τ characterise the cell's own RC
    # relaxation and aren't affected by simple wiring resistance.
    #
    # Defense-in-depth pair (D2): ConfigManager.validate_config() rejects an
    # implausible harness_resistance_ohm at config-entry time; _correct_for_harness_r
    # below is the runtime backstop — a value that survived entry validation but is
    # still too large RELATIVE TO A GIVEN READING (e.g. calibrated against a healthy
    # pack's DCIR, then applied to a genuinely degraded one whose true resistance is
    # smaller) would otherwise silently floor that reading and grade a bad pack "A".
    harness_r = max(0.0, float(getattr(profile, "harness_r_ohm", 0.0)))
    harness_warnings: list = []
    if harness_r > 0.0:
        if measured:
            dcir, harness_warnings = _correct_for_harness_r(
                dcir, harness_r, "DCIR", harness_warnings)
        if dcir_slope == dcir_slope:   # not NaN
            dcir_slope, harness_warnings = _correct_for_harness_r(
                dcir_slope, harness_r, "DCIR slope", harness_warnings)

    # Median record temperature — shared 25 °C-normalization basis for the ECM
    # parameters below (same chemistry Arrhenius model identify_dcir already
    # uses per-step), and the temp at which the OCV ceiling is evaluated.
    _tc_arr = np.asarray(temp_c, float)
    t_med = float(np.nanmedian(_tc_arr)) if _tc_arr.size and not np.all(np.isnan(_tc_arr)) \
        else _T_REF
    ocv_ceil = _ocv_ceiling(profile, t_med)
    sag, cca_est, ocv = _load_metrics(current_a, voltage_v, dcir, profile,
                                      ocv_ceiling=ocv_ceil)
    warnings, temp_drift = _quality_flags(current_a, voltage_v, temp_c, profile,
                                          is_hppc, n_steps, reached_cutoff)
    warnings = warnings + harness_warnings
    # A latency-corrupted step reads too high (relaxed past ohmic) — those were dropped
    # in identify_dcir; flag it so the operator knows the rig's sampling stalled and the
    # DCIR rests on fewer (or, if all were stale, zero → baseline) steps than it looks.
    if n_stale > 0:
        warnings = warnings + [
            f"{n_stale} DCIR step(s) dropped — sampling latency >{_DCIR_MAX_STEP_DT:.1f}s "
            f"(USB/SCPI stall); R0 would read inflated"]
    # A too-fresh pair reads too LOW: the current column refreshed but the voltage
    # readback (separate SCPI query) hadn't — ΔV≈0 across a real edge computes a
    # near-zero R (a real file reported "DCIR 0.00 mΩ measured" from exactly this
    # and zeroed the CCA proxy). Those are now rejected by the plausibility band.
    if n_implausible > 0:
        warnings = warnings + [
            f"{n_implausible} DCIR step(s) rejected — ΔV/ΔI outside the plausible band "
            f"(stale voltage readback at the edge or quantization); not ohmic resistance"]
    # SoH under-statement guard: an SoH was computed (full discharge to cut-off) but the
    # pack didn't START near-full, so the Ah removed under-counts true capacity and the
    # SoH/grade read low even for a healthy cell. reached_cutoff guards only the END;
    # this flags the START. (Warning only — the number isn't corrected, since that would
    # amplify OCV→SoC error; charge fully for an accurate capacity test.) Lowers
    # confidence automatically via the warning count fed to _confidence below.
    if (not is_hppc) and reached_cutoff and not np.isnan(soh) \
            and soc_start is not None and soc_start == soc_start \
            and soc_start < _SOH_MIN_START_SOC:
        warnings = warnings + [
            f"discharge started at {soc_start:.0f}% SoC (not full) — SoH is under-stated; "
            f"charge fully before a capacity test"]
    # Circular-trust guard on the guard itself: soc_start comes from the logged SoC
    # column — the very estimator this pipeline exists to check. A real case slipped
    # straight through: the logged SoC said 100% (frozen by an estimator bug) while
    # the pack was really ~95.7% full, so the "started full" gate passed and the
    # missing 4.3% was reported as SoH degradation on a healthy battery. Corroborate
    # the logged start SoC against the record's own rested head voltage — an
    # independent physical witness — and flag a large disagreement instead of
    # silently trusting either side. (BELOW-ceiling voltages only: an above-ceiling
    # rest is surface charge and can't distinguish 90% from 100% — see _ocv_ceiling.)
    if (not is_hppc) and not np.isnan(soh) and soc_start is not None \
            and soc_start == soc_start and soc_start >= _SOH_MIN_START_SOC:
        try:
            from aset_batt.core.battery_model import BatteryModel
            ia_h = np.asarray(current_a, float)[:25]
            va_h = np.asarray(voltage_v, float)[:25]
            head_rest = va_h[np.abs(ia_h - _I_STANDBY) < 0.15]
            if head_rest.size >= 3:
                v_head = float(np.median(head_rest))
                if not (ocv_ceil and v_head > ocv_ceil):   # surface charge → no info
                    model = BatteryModel(profile.chemistry)
                    soc_ocv = model.get_soc_from_ocv(v_head / profile.series, t_med) \
                        if model.series_cells == 1 else None
                    if soc_ocv is not None and soc_start - soc_ocv > 15.0:
                        warnings = warnings + [
                            f"logged start SoC ({soc_start:.0f}%) is not corroborated by "
                            f"the rested head voltage ({v_head:.3f} V → ~{soc_ocv:.0f}%) — "
                            f"SoH may be under-stated (pack likely not full at start)"]
        except Exception:
            pass

    # 1-RC / 2-RC ECM for HPPC (reported ALONGSIDE the DCIR, not instead of it).
    ecm, ecm_reason = identify_ecm_fit(time_s, current_a, voltage_v, ocv) if is_hppc \
        else (None, "")
    # HPPC but no ECM → R1/C1 come out blank on the circuit with no reason shown. Surface
    # WHY (no clear pulse edge / pulse too short / poor fit) so a blank isn't a silent
    # dead end — the operator can lengthen the pulse or check the load edge and re-run.
    if is_hppc and ecm is None and ecm_reason:
        warnings = warnings + [f"ECM R1/C1 not identified — {ecm_reason}; showing DCIR/R0 only"]
    is_2rc = bool(ecm and "R2_ohm" in ecm)
    if ecm:
        r0, r1 = float(ecm["R0_ohm"]), float(ecm["R1_ohm"])
        if harness_r > 0.0:
            # ohmic only — R1/C1/τ are the cell's own RC, untouched by wiring resistance
            r0, warnings = _correct_for_harness_r(r0, harness_r, "ECM R0", warnings)
        # 2-RC dict uses tau1_s; 1-RC uses tau_s
        c1 = float(ecm["C1_farad"])
        tau = float(ecm.get("tau1_s", ecm.get("tau_s", 0.0)))
        r2_ecm_fit = float(ecm["r_squared"])
        # 2-RC extra parameters (zero when only 1-RC was accepted)
        r2_rc = float(ecm.get("R2_ohm", 0.0))
        c2 = float(ecm.get("C2_farad", 0.0))
        tau2 = float(ecm.get("tau2_s", 0.0))
        # 25 °C normalization — the SAME chemistry Arrhenius basis identify_dcir
        # has always applied per step. The ECM parameters used to skip this
        # entirely, so the report showed a normalized DCIR next to raw-at-
        # -bench-temp R0/R1 under one "norm. 25 °C" header, and the DCIR-vs-fit
        # cross-check below compared a normalized value against a raw one — at
        # this bench's typical ~30 °C that was a built-in ~12% wedge between two
        # numbers that measure the same physics. C1 scales INVERSELY so the
        # fitted time constant τ = R1·C1 (a directly-observed quantity) is
        # preserved, not silently shifted by the normalization.
        _ecm_mult = _dcir_temp_normalizer(profile)(t_med)
        r0 /= _ecm_mult
        r1 /= _ecm_mult
        r2_rc /= _ecm_mult
        c1 *= _ecm_mult
        c2 *= _ecm_mult
        ri_total = r0 + r1 + r2_rc
        # voc-anchor sensitivity check: the fit's Voc came from the GLOBAL rest
        # median; if the LOCAL rest right before the first pulse sits far from
        # it (classic cause: surface charge still relaxing between them), R0
        # shifts by ΔV/I — a real file showed a 2.2× R0 spread from exactly
        # this. The grade stays comparable only because the baseline specimen
        # was measured with the same procedure; surface the divergence so an
        # inconsistent rest history is visible instead of silently biasing R0.
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
                        warnings = warnings + [
                            f"rest voltage right before the first pulse "
                            f"({voc_local:.3f} V) differs from the whole-record rest "
                            f"median ({ocv:.3f} V) — rest history inconsistent "
                            f"(surface charge?); R0 is sensitive to this anchor"]
        except Exception:
            pass
        # cross-check: the DCIR@~250 ms should sit between R0 and R0+R1+R2; a big gap
        # means the fit and the step disagree → surface it.
        if measured and dcir > 0 and not (0.5 * r0 <= dcir <= 1.5 * ri_total):
            warnings = warnings + [f"DCIR@250ms ({dcir*1e3:.0f} mΩ) disagrees with fit "
                                   f"R0+R1 ({ri_total*1e3:.0f} mΩ) — check the pulse"]
        # Absolute timestamp of the pulse edge this fit anchored to (same clock as
        # time_s) — lets a caller with a parallel SoC history (e.g. worker.py) look
        # up the SoC AT the pulse instead of whatever SoC is current when the
        # post-hoc result is consumed.
        ecm_fit_t_s = float(ecm.get("t_edge_s", float("nan")))
    else:
        r0, r1, c1, tau, r2_ecm_fit = dcir, 0.0, 0.0, 0.0, 0.0
        r2_rc, c2, tau2 = 0.0, 0.0, 0.0
        ri_total = dcir
        ecm_fit_t_s = float("nan")

    # cca_est above was derived from `dcir` alone, computed by _load_metrics() before
    # the ECM fit even ran (ocv is needed as ECM fit input, so that call can't simply
    # move later). When the single-step DCIR fails (measured=False, falls back to the
    # profile's generic baseline) but the ECM fit succeeds, ri_total already carries
    # the better, actually-measured resistance (see the if/else above — it equals
    # dcir only when there's no ECM) — re-deriving cca_est from it instead avoids
    # reporting a CCA proxy suppressed by a resistance the pack never actually showed.
    _ocv_eff = min(ocv, ocv_ceil) if ocv_ceil else ocv   # surface-charge clamp, as in _load_metrics
    cca_est = max(0.0, (_ocv_eff - _cca_cutoff_v(profile)) / ri_total) if ri_total > 1e-6 else 0.0

    gradeable = measured or bool(ecm) or not np.isnan(soh)
    if not gradeable:
        grade = "REVIEW"
    elif ecm:
        grade = Analytics.grade_from_ecm(soh, r0, r1, profile)   # two-resistance grading
    else:
        grade = Analytics.grade(soh, dcir, profile)

    confidence = _confidence(dcir, dcir_std, n_steps, profile, len(warnings))
    if ecm:
        confidence *= 0.7 + 0.3 * max(0.0, min(1.0, r2_ecm_fit))  # reward a good fit
    if not gradeable:
        confidence = 0.0

    # R5 (industrial-grade audit): the grading decision itself used to produce NO
    # structured log line — only ECM-fit-rejection reasons were logged (see the
    # logger.info calls in identify_ecm_fit above), never the actual soh/dcir/r0/
    # r1/harness_r/grade/confidence values a grade was decided from. Post-hoc
    # investigation of a mis-graded batch had to re-run analysis on the archived
    # CSV and hope config/battery_profiles.json hadn't since changed (they aren't
    # versioned — see the R3 audit-trail work). One line here makes the decision
    # itself reconstructable from the log alone.
    logger.info(
        "GRADE DECISION product=%s chemistry=%s grade=%s confidence=%.2f "
        "soh=%s dcir_mohm=%.2f r0_mohm=%.2f r1_mohm=%.2f harness_r_mohm=%.2f "
        "n_steps=%d measured=%s ecm_identified=%s gradeable=%s warnings=%d",
        getattr(profile, "name", "?"), getattr(profile, "chemistry", "?"),
        grade, confidence,
        "nan" if np.isnan(soh) else f"{soh:.1f}",
        dcir * 1000.0, r0 * 1000.0, r1 * 1000.0, harness_r * 1000.0,
        n_steps, measured, bool(ecm), gradeable, len(warnings),
    )

    ica_v, ica = Analytics.incremental_capacity(v, q)
    return {
        "soh": soh, "capacity_ah": capacity,
        "capacity_norm_ah": cap_norm, "mean_discharge_a": mean_dis,
        "peukert_k": getattr(profile, "peukert_k", 1.1),
        "dcir_mohm": dcir * 1000.0, "dcir_std_mohm": dcir_std * 1000.0,
        "dcir_n_steps": n_steps, "dcir_measured": measured, "dcir_temp_normalised": True,
        "ecm_temp_normalised": True,   # R0/R1(/R2) on the same 25 °C basis as DCIR
        "dcir_slope_mohm": dcir_slope * 1000.0, "dcir_slope_r2": dcir_slope_r2,
        "ri_mohm": ri_total * 1000.0,
        "voltage_sag_v": sag, "cca_est_a": cca_est, "ocv_v": ocv,
        "grade": grade, "gradeable": gradeable,
        "confidence": confidence, "quality_warnings": warnings, "temp_drift_c": temp_drift,
        # ECM (HPPC): R0 extrapolated, R1/C1/τ fitted; zeros + ecm=False when only DCIR.
        # When 2-RC is accepted, R2/C2/τ2 carry the second RC branch; otherwise zeros.
        "r0_mohm": r0 * 1000.0, "r1_mohm": r1 * 1000.0, "c1_farad": c1, "tau_s": tau,
        "ecm_identified": bool(ecm), "ecm_r2": r2_ecm_fit, "ecm_fit_t_s": ecm_fit_t_s,
        "ecm_model": "2RC" if is_2rc else "1RC",
        "r2_mohm": r2_rc * 1000.0, "c2_farad": c2, "tau2_s": tau2,
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
        c_soc = col("SoC_pct")
        T, V, I, TEMP, CAP, SOC, modes = [], [], [], [], [], [], []
        for r in reader:
            def num(c, default=float("nan")):
                try:
                    return float(r[c]) if c else default
                except (ValueError, TypeError, KeyError):
                    return default
            T.append(num(c_t)); V.append(num(c_v)); I.append(num(c_i))
            TEMP.append(num(c_temp, 25.0)); CAP.append(num(c_cap)); SOC.append(num(c_soc))
            modes.append(r[c_mode] if c_mode else "")
    return (np.asarray(T, float), np.asarray(V, float), np.asarray(I, float),
            np.asarray(TEMP, float), np.asarray(CAP, float), np.asarray(SOC, float), modes)


def analyze_csv(csv_path: str, profile: BatteryProfile, force_hppc: bool = False) -> dict:
    """Parse a telemetry CSV and run the unified analysis. HPPC is inferred from
    the ``Mode`` column; capacity is integrated from current if not logged."""
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path or "(no CSV)")
    t, v, i, temp, cap, soc, modes = _read_csv(csv_path)
    if t.size < 2:
        raise ValueError("CSV has too few samples to analyse.")
    is_hppc = force_hppc or any("hppc" in (m or "").lower() for m in modes)
    if np.all(np.isnan(cap)):                       # no capacity column → integrate
        dt = np.diff(t, prepend=t[0])
        cap = np.cumsum(np.clip(i, 0, None) * dt) / 3600.0
    # Starting SoC = the peak logged SoC (start of a discharge) — used to flag an SoH
    # that's under-stated because the pack wasn't full when the capacity test began.
    soc_start = float(np.nanmax(soc)) if soc.size and not np.all(np.isnan(soc)) else None
    return analyze_series(t, i, v, temp, cap, profile, is_hppc, soc_start=soc_start)


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


def analyze_csv_mp(csv_path: str, profile: BatteryProfile, force_hppc: bool = False) -> dict:
    """Same result as analyze_csv(), but the ECM curve-fit (scipy.optimize.curve_fit,
    up to ~10k iterations, run from a background thread after every auto sequence)
    executes in a separate worker process instead of a thread.

    curve_fit's Python-level callback holds the GIL for the whole fit, so even
    though the caller is already off the Qt main thread, a plain threading.Thread
    still starves the UI event loop of the GIL and Windows reports "Not Responding"
    for the ~5-15s the fit takes. A separate process has its own GIL, so the UI
    thread keeps pumping events while this call blocks on the subprocess result.
    """
    future = _get_analysis_pool().submit(analyze_csv, csv_path, profile, force_hppc)
    return future.result()


def analyze_series_mp(time_s, current_a, voltage_v, temp_c, capacity_series,
                      profile: BatteryProfile, is_hppc: bool, soh=None,
                      soc_start=None) -> dict:
    """Same result as analyze_series(), but off the calling thread's GIL — see
    analyze_csv_mp's docstring. AcquisitionWorker.run() (the Characterization /
    RUN TEST / HPPC-via-RUN-TEST QThread) calls this directly with its in-memory
    sample arrays instead of round-tripping through a CSV, so it needed its own
    process-pool twin; routing it through analyze_csv_mp would have meant writing
    an extra throwaway CSV just to satisfy that wrapper's file-path signature."""
    future = _get_analysis_pool().submit(
        analyze_series, time_s, current_a, voltage_v, temp_c, capacity_series,
        profile, is_hppc, soh, soc_start)
    return future.result()
