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
    def incremental_capacity(v: np.ndarray, q: np.ndarray):
        """ICA: dQ/dV vs V. Resample onto a monotonic voltage grid, smooth, differentiate.

        The voltage axis is de-jittered FIRST: q is single-valued in V only on a clean
        monotonic sweep, so raw sensor jitter would make V non-monotonic and scramble
        the q-ordering during the sort (spiking dQ/dV). Smoothing the axis before the
        sort/unique is what keeps the curve physical."""
        if v.size < 10:
            return np.array([]), np.array([])
        v_s = Analytics.gaussian_smooth(v, 2.0)      # de-jitter the independent axis first
        q_s = Analytics.gaussian_smooth(q, 2.0)
        order = np.argsort(v_s)
        vu, idx = np.unique(v_s[order], return_index=True)
        qu = q_s[order][idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        qg = np.interp(grid, vu, qu)
        dqdv = np.gradient(Analytics.gaussian_smooth(qg, 3.0), grid)
        return grid, Analytics.gaussian_smooth(dqdv, 2.0)

    @staticmethod
    def differential_thermal(v: np.ndarray, t: np.ndarray):
        """DTV: dT/dV vs V (thermal fingerprint). Same axis-first de-jitter as ICA."""
        if v.size < 10:
            return np.array([]), np.array([])
        v_s = Analytics.gaussian_smooth(v, 2.0)      # de-jitter the independent axis first
        t_s = Analytics.gaussian_smooth(t, 2.0)
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
        """
        r0_base = max(1e-9, Analytics.R0_FRACTION * p.internal_r)
        r1_base = max(1e-9, Analytics.R1_FRACTION * p.internal_r)
        r0_ratio = r0_ohm / r0_base
        r1_ratio = r1_ohm / r1_base
        if soh >= 90 and r0_ratio <= 1.3 and r1_ratio <= 1.4:
            return "A"
        if soh >= 80 and r0_ratio <= 1.7 and r1_ratio <= 1.8:
            return "B"
        if soh >= 70 and r0_ratio <= 2.5 and r1_ratio <= 2.8:
            return "C"
        return "REJECT"

    @staticmethod
    def grade(soh: float, ri_ohm: float, p: BatteryProfile) -> str:
        """[fallback] Single total-resistance grading for non-HPPC modes (no RC fit).
        Prefer :meth:`grade_from_ecm` when R0/R1 are available."""
        ri_ratio = ri_ohm / max(1e-6, p.internal_r)
        if soh >= 90 and ri_ratio <= 1.3:
            return "A"
        if soh >= 80 and ri_ratio <= 1.7:
            return "B"
        if soh >= 70 and ri_ratio <= 2.5:
            return "C"
        return "REJECT"
