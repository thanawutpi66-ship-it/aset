"""
Physics-based parameter identification from battery characterization experiments.

Three analysis functions correspond to the three CHARACTERIZE tab tests:
  fit_peukert_k      → multi-rate discharge data → Peukert exponent k
  compute_coulomb_eta → charge/discharge Ah accounting → per-band efficiency
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
