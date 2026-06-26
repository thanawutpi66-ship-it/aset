"""Post-test analytics: HPPC internal resistance, Incremental Capacity Analysis
(ICA, dQ/dV) and Differential Thermal Voltammetry (DTV, dT/dV) with Gaussian
smoothing, plus the sorting-grade decision."""
from __future__ import annotations

import numpy as np

from aset_batt.acquisition.models import BatteryProfile


class Analytics:
    @staticmethod
    def gaussian_smooth(y: np.ndarray, sigma: float = 2.0) -> np.ndarray:
        """Gaussian low-pass; scipy if available, else a numpy kernel convolution.
        Smoothing is essential before differentiation — dQ/dV amplifies measurement
        noise, so we filter both the integral and its derivative."""
        if y.size < 3:
            return y
        try:
            from scipy.ndimage import gaussian_filter1d
            return gaussian_filter1d(y, sigma)
        except Exception:
            radius = int(3 * sigma)
            x = np.arange(-radius, radius + 1)
            k = np.exp(-(x ** 2) / (2 * sigma ** 2)); k /= k.sum()
            return np.convolve(y, k, mode="same")

    @staticmethod
    def internal_resistance_hppc(pulses, p: BatteryProfile) -> float:
        """[fallback] Single-point total resistance Rᵢ = |ΔV / ΔI| across the pulse
        step. Superseded by the full 1-RC ECM fit (``parameter_id``) which separates
        R0 and R1; retained only for non-HPPC modes where no RC transient exists."""
        if len(pulses) >= 2:
            (v1, i1), (v2, i2) = pulses[-2], pulses[-1]
            if abs(i2 - i1) > 1e-3:
                return abs((v2 - v1) / (i2 - i1))
        return p.internal_r

    @staticmethod
    def hampel_filter(x: np.ndarray, k: int = 7, n_sigma: float = 3.0) -> np.ndarray:
        """Hampel identifier: replace spikes with the local median.

        For each sample, if it deviates from the local median (window 2k+1) by more
        than n_sigma robust standard deviations (1.4826·MAD), it is replaced with that
        median.  k=7 (window=15) at 1 Hz gives ~15-second outlier context, which is
        appropriate for SCPI measurement glitches."""
        x = np.asarray(x, float)
        n = x.size
        if n < 2 * k + 1:
            return x.copy()
        out = x.copy()
        for i in range(n):
            lo, hi = max(0, i - k), min(n, i + k + 1)
            win = x[lo:hi]
            med = float(np.median(win))
            mad = float(np.median(np.abs(win - med)))
            # When MAD=0 (all neighbours equal) use a noise floor of 1% of |median|
            # or 1e-6 (whichever is larger) so an isolated spike is still caught.
            if mad == 0:
                mad = max(1e-6, 0.01 * abs(med))
            if abs(x[i] - med) > n_sigma * 1.4826 * mad:
                out[i] = med
        return out

    @staticmethod
    def _savgol_deriv(y: np.ndarray, grid: np.ndarray) -> np.ndarray:
        """dy/dx via a Savitzky-Golay filter — it fits a local polynomial, so it smooths
        AND differentiates in one pass and **preserves peak height/position** far better
        than Gaussian-smoothing then np.gradient (which rounds ICA peaks off). Falls back
        to the Gaussian path when SciPy isn't available."""
        n = y.size
        if n >= 11:
            try:
                from scipy.signal import savgol_filter
                win = min(n if n % 2 == 1 else n - 1, 21)   # odd window ≤ n
                if win >= 5:
                    delta = (grid[-1] - grid[0]) / (n - 1)
                    return savgol_filter(y, win, 3, deriv=1, delta=delta)
            except Exception:
                pass
        return Analytics.gaussian_smooth(np.gradient(Analytics.gaussian_smooth(y, 3.0), grid), 2.0)

    @staticmethod
    def incremental_capacity(v: np.ndarray, q: np.ndarray):
        """ICA: dQ/dV vs V. Resample onto a monotonic voltage grid, then take a peak-
        preserving Savitzky-Golay derivative.

        The voltage axis is de-jittered FIRST: q is single-valued in V only on a clean
        monotonic sweep, so raw sensor jitter would make V non-monotonic and scramble
        the q-ordering during the sort (spiking dQ/dV). Smoothing the axis before the
        sort/unique is what keeps the curve physical."""
        if v.size < 10:
            return np.array([]), np.array([])
        v_s = Analytics.gaussian_smooth(Analytics.hampel_filter(v), 2.0)
        q_s = Analytics.gaussian_smooth(Analytics.hampel_filter(q), 2.0)
        order = np.argsort(v_s)
        vu, idx = np.unique(v_s[order], return_index=True)
        qu = q_s[order][idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        qg = np.interp(grid, vu, qu)
        return grid, Analytics._savgol_deriv(qg, grid)

    @staticmethod
    def differential_thermal(v: np.ndarray, t: np.ndarray):
        """DTV: dT/dV vs V (thermal fingerprint). Same axis-first de-jitter as ICA."""
        if v.size < 10:
            return np.array([]), np.array([])
        v_s = Analytics.gaussian_smooth(Analytics.hampel_filter(v), 2.0)
        t_s = Analytics.gaussian_smooth(Analytics.hampel_filter(t), 2.0)
        order = np.argsort(v_s)
        vu, idx = np.unique(v_s[order], return_index=True)
        tu = t_s[order][idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        tg = np.interp(grid, vu, tu)
        dtdv = np.gradient(Analytics.gaussian_smooth(tg, 3.0), grid)
        return grid, Analytics.gaussian_smooth(dtdv, 2.0)

    # baseline split of the profile's DC internal resistance into ohmic (R0) and
    # charge-transfer (R1) parts — typical for LiFePO4 (~60 % ohmic, ~40 % CT).
    R0_FRACTION = 0.6
    R1_FRACTION = 0.4

    @staticmethod
    def grade_from_ecm(soh: float, r0_ohm: float, r1_ohm: float,
                       p: BatteryProfile) -> str:
        """Sort A/B/C/REJECT from SoH plus **independent** growth of R0 and R1.

        Physical rationale:
          * **R0** (ohmic) rising → contact degradation / electrolyte conductivity loss.
          * **R1** (charge-transfer) rising → SEI-layer growth / active-material loss.

        A cell is downgraded if *either* resistance has grown, so a cell that still
        holds capacity (high SoH) but has a degraded interface (high R1) is correctly
        rejected — which the old single total-Rᵢ metric could miss.

        ``soh`` may be NaN when it is not measurable from this test (e.g. an HPPC
        pulse test, where partial throughput is NOT a capacity measurement). In that
        case the cell is graded on resistance growth alone rather than fabricating a
        capacity-based SoH gate.
        """
        r0_base = max(1e-9, Analytics.R0_FRACTION * p.internal_r)
        r1_base = max(1e-9, Analytics.R1_FRACTION * p.internal_r)
        r0_ratio = r0_ohm / r0_base
        r1_ratio = r1_ohm / r1_base
        soh_unknown = soh is None or np.isnan(soh)
        if (soh_unknown or soh >= 90) and r0_ratio <= 1.3 and r1_ratio <= 1.4:
            return "A"
        if (soh_unknown or soh >= 80) and r0_ratio <= 1.7 and r1_ratio <= 1.8:
            return "B"
        if (soh_unknown or soh >= 70) and r0_ratio <= 2.5 and r1_ratio <= 2.8:
            return "C"
        return "REJECT"

    @staticmethod
    def grade(soh: float, ri_ohm: float, p: BatteryProfile) -> str:
        """[fallback] Single total-resistance grading for non-HPPC modes (no RC fit).
        Prefer :meth:`grade_from_ecm` when R0/R1 are available. ``soh`` may be NaN
        (not measurable) → grade on resistance alone."""
        ri_ratio = ri_ohm / max(1e-6, p.internal_r)
        soh_unknown = soh is None or np.isnan(soh)
        if (soh_unknown or soh >= 90) and ri_ratio <= 1.3:
            return "A"
        if (soh_unknown or soh >= 80) and ri_ratio <= 1.7:
            return "B"
        if (soh_unknown or soh >= 70) and ri_ratio <= 2.5:
            return "C"
        return "REJECT"
