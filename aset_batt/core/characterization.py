"""
Physics-based parameter identification from battery characterization experiments.

Three analysis functions correspond to the three CHARACTERIZE tab tests:
  fit_peukert_k      → multi-rate discharge data → Peukert exponent k
  compute_coulomb_eta → legacy per-band efficiency (not used by START η)
  integrate_coulomb_ah / evaluate_coulomb_efficiency → whole-cycle START η result
  build_ocv_table    → GITT rest voltages → standard OCV–SoC lookup table
"""
import logging

logger = logging.getLogger(__name__)


def fit_peukert_k(currents, durations_s):
    """Fit Peukert exponent k from multi-rate discharge data.

    Peukert: t · I^k = C_p  →  log(t) = −k·log(I) + log(C_p)

    Args:
        currents:    list of discharge currents [A], at least 2 entries
        durations_s: list of time-to-cutoff [s] at matching currents

    Returns:
        (k, r_squared) — Peukert exponent and linear R² of the log-log fit
    """
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("numpy is required for Peukert fitting")

    if len(currents) < 2:
        raise ValueError("fit_peukert_k needs at least 2 data points")

    log_I = np.log(np.array(currents, dtype=float))
    log_t = np.log(np.array(durations_s, dtype=float))

    coeffs = np.polyfit(log_I, log_t, 1)
    k = float(-coeffs[0])

    log_t_pred = np.polyval(coeffs, log_I)
    ss_res = float(np.sum((log_t - log_t_pred) ** 2))
    ss_tot = float(np.sum((log_t - log_t.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0

    logger.info("Peukert fit: k=%.3f  R²=%.4f  (%d points)", k, r2, len(currents))
    return k, r2


def compute_coulomb_eta(ah_in_by_band, ah_out_by_band):
    """Compute coulomb efficiency per SoC band.

    Args:
        ah_in_by_band:  dict {'bulk': Ah, 'absorb': Ah, 'full': Ah} from charge phase
        ah_out_by_band: dict {'bulk': Ah, 'absorb': Ah, 'full': Ah} from discharge phase

    Returns:
        dict with same keys + 'overall'; values are floats in [0,1] or None if not measured
    """
    result = {}
    total_in  = sum(ah_in_by_band.values())
    total_out = sum(ah_out_by_band.values())

    for band in ('bulk', 'absorb', 'full'):
        ah_in  = ah_in_by_band.get(band, 0.0)
        ah_out = ah_out_by_band.get(band, 0.0)
        if ah_in > 0.001:
            result[band] = min(1.0, ah_out / ah_in)
        else:
            result[band] = None

    result['overall'] = total_out / total_in if total_in > 0.001 else None
    logger.info("Coulomb η: bulk=%.3f  absorb=%.3f  full=%.3f  overall=%.3f",
                result.get('bulk') or 0, result.get('absorb') or 0,
                result.get('full') or 0, result.get('overall') or 0)
    return result


def integrate_coulomb_ah(times_s, currents_a, *, phase: str,
                         expected_dt_s: float = 5.0,
                         max_integrable_gap_s: float = 30.0) -> dict:
    """Integrate measured Coulombs for one phase using actual timestamps.

    The project's characterization reads use discharge-positive and charge-negative.
    ``phase='charge'`` therefore integrates ``max(-I, 0)`` and ``phase='discharge'``
    integrates ``max(I, 0)``. Short sampling gaps are integrated trapezoidally
    from their measured endpoints and reported; gaps longer than 30 s are
    excluded and make the phase invalid rather than silently bridged.
    """
    import math
    ts = [float(x) for x in times_s]
    currents = [float(x) for x in currents_a]
    if len(ts) != len(currents):
        raise ValueError("times_s and currents_a must have equal length")
    if phase not in {"charge", "discharge"}:
        raise ValueError("phase must be 'charge' or 'discharge'")
    sign = -1.0 if phase == "charge" else 1.0
    amps = [max(0.0, sign * i) if math.isfinite(i) else float("nan") for i in currents]
    total_as = 0.0
    positive_duration = 0.0
    largest_dt = 0.0
    gap_count = 0
    gap_duration = 0.0
    excluded_gap_count = 0
    excluded_gap_duration = 0.0
    invalid_interval_count = 0
    for idx in range(1, len(ts)):
        dt = ts[idx] - ts[idx - 1]
        if not math.isfinite(dt) or dt <= 0.0:
            invalid_interval_count += 1
            continue
        largest_dt = max(largest_dt, dt)
        if dt > expected_dt_s * 2.5:
            gap_count += 1
            gap_duration += dt
        if dt > max_integrable_gap_s or not math.isfinite(amps[idx - 1]) or not math.isfinite(amps[idx]):
            excluded_gap_count += 1
            excluded_gap_duration += dt
            continue
        total_as += 0.5 * (amps[idx - 1] + amps[idx]) * dt
        positive_duration += dt
    observed_span = max(0.0, ts[-1] - ts[0]) if len(ts) >= 2 else 0.0
    missed_duration = max(0.0, observed_span - positive_duration)
    gap_fraction = excluded_gap_duration / observed_span if observed_span > 0 else 1.0
    valid = (len(ts) >= 2 and positive_duration > 0.0
             and invalid_interval_count == 0 and gap_fraction <= 0.05)
    return {
        "ah": total_as / 3600.0,
        "integration_duration_s": positive_duration,
        "observed_duration_s": observed_span,
        "sample_count": len(ts),
        "largest_dt_s": largest_dt,
        "gap_count": gap_count,
        "gap_duration_s": gap_duration,
        "missed_duration_s": missed_duration,
        "integration_quality_status": "VALID" if valid else "SAMPLING_INVALID",
        "valid": valid,
    }


def evaluate_coulomb_efficiency(q_in: dict, q_out: dict, *,
                                conditioning_endpoint_valid: bool,
                                full_charge_confirmed: bool,
                                reference_cutoff_reached: bool,
                                aborted: bool = False,
                                abort_reason: str = "") -> dict:
    """Gate whole-cycle Qout/Qin Coulombic efficiency without SoH or clamping."""
    status = "VALID"
    reasons = []
    if aborted:
        why = abort_reason.lower()
        status = ("TEMPERATURE_ABORT" if any(x in why for x in ("temperature", "otp", "thermal"))
                  else "SAFETY_ABORT" if any(x in why for x in ("ovp", "uvp", "safety"))
                  else "CANCELLED" if "cancel" in why else "ERROR")
        reasons.append(abort_reason or "sequence aborted")
    elif not conditioning_endpoint_valid:
        status, reasons = "CONDITIONING_INCOMPLETE", ["conditioning cutoff not reached"]
    elif not full_charge_confirmed:
        status, reasons = "FULL_CHARGE_NOT_CONFIRMED", ["verified taper termination not observed"]
    elif not reference_cutoff_reached:
        status, reasons = "CUTOFF_NOT_REACHED", ["reference discharge cutoff not reached"]
    elif not q_in.get("valid") or not q_out.get("valid"):
        status, reasons = "SAMPLING_INVALID", ["charge or discharge integration quality failed"]
    elif q_in.get("ah", 0.0) <= 0.0 or q_out.get("ah", 0.0) <= 0.0:
        status, reasons = "DATA_INSUFFICIENT", ["Qin and Qout must both be positive"]
    eta = (100.0 * q_out["ah"] / q_in["ah"]
           if q_in.get("ah", 0.0) > 0.0 and q_out.get("ah", 0.0) > 0.0 else None)
    if eta is not None and eta <= 0.0 and status == "VALID":
        status, reasons = "DATA_INSUFFICIENT", ["Coulombic efficiency must be positive"]
    elif eta is not None and eta > 100.0 and status == "VALID":
        status, reasons = "SUSPECT_RESULT", ["Coulombic efficiency exceeds 100%; review cycle boundaries and data"]
    return {
        "q_in_ah": q_in.get("ah"), "q_out_ah": q_out.get("ah"),
        "eta_coulomb_pct": eta if status in {"VALID", "SUSPECT_RESULT"} else None,
        "status": status, "valid": status == "VALID", "reasons": reasons,
    }


def build_ecm_table(soc_pct_list, r0_list, r1_list, c1_list):
    """Build {soc_int: {'r0','r1','c1'}} at 5% SoC steps from HPPC fits at several SoC.

    R0/R1/C1 vary strongly with SoC (they rise sharply toward empty), so feeding the
    EKF a single fixed fit makes its terminal-voltage prediction drift at the SoC
    extremes. Run an HPPC pulse at a few SoC points (e.g. 90/70/50/30/10 %), fit each,
    and pass the parallel lists here; hand the result to StateEstimator.set_ecm_table()
    so the filter uses SoC-appropriate RC dynamics.

    Args:
        soc_pct_list: SoC [%] at each HPPC fit (need not be a regular grid)
        r0_list, r1_list, c1_list: fitted R0 [Ohm], R1 [Ohm], C1 [F] at each SoC

    Returns:
        dict {soc_int: {'r0': Ohm, 'r1': Ohm, 'c1': F}} at 0, 5, …, 100 %
    """
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("numpy is required for ECM table building")

    n = len(soc_pct_list)
    if n < 2 or not (len(r0_list) == len(r1_list) == len(c1_list) == n):
        raise ValueError("build_ecm_table needs >=2 points and equal-length lists")

    soc = np.array(soc_pct_list, dtype=float)
    order = np.argsort(soc)
    soc = soc[order]
    r0 = np.array(r0_list, dtype=float)[order]
    r1 = np.array(r1_list, dtype=float)[order]
    c1 = np.array(c1_list, dtype=float)[order]

    target = np.arange(0, 101, 5, dtype=float)
    r0i = np.interp(target, soc, r0)
    r1i = np.interp(target, soc, r1)
    c1i = np.interp(target, soc, c1)

    table = {int(s): {"r0": round(float(a), 6), "r1": round(float(b), 6),
                      "c1": round(float(c), 2)}
             for s, a, b, c in zip(target, r0i, r1i, c1i)}
    logger.info("Built ECM table: %d SoC points from %d HPPC fits", len(table), n)
    return table


def build_ocv_table(soc_pct_list, ocv_per_cell_list):
    """Build a {soc_int: ocv_per_cell} table from GITT rest measurements.

    Args:
        soc_pct_list:      measured SoC values [%] (need not be on a regular grid)
        ocv_per_cell_list: OCV per cell [V] at each SoC point

    Returns:
        dict {soc_int: ocv_per_cell} at 5% SoC steps: 0, 5, 10, …, 100
    """
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("numpy is required for OCV table building")

    if len(soc_pct_list) < 2:
        raise ValueError("build_ocv_table needs at least 2 measured points")

    soc_arr = np.array(soc_pct_list, dtype=float)
    ocv_arr = np.array(ocv_per_cell_list, dtype=float)

    order = np.argsort(soc_arr)
    soc_arr = soc_arr[order]
    ocv_arr = ocv_arr[order]

    target = np.arange(0, 101, 5, dtype=float)
    ocv_interp = np.interp(target, soc_arr, ocv_arr)

    table = {int(s): round(float(v), 4) for s, v in zip(target, ocv_interp)}
    logger.info("Built OCV table: %d points interpolated from %d measurements",
                len(table), len(soc_pct_list))
    return table
