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
        """Rᵢ = |ΔV / ΔI| measured across the HPPC pulse current step."""
        if len(pulses) >= 2:
            (v1, i1), (v2, i2) = pulses[-2], pulses[-1]
            if abs(i2 - i1) > 1e-3:
                return abs((v2 - v1) / (i2 - i1))
        return p.internal_r

    @staticmethod
    def incremental_capacity(v: np.ndarray, q: np.ndarray):
        """ICA: dQ/dV vs V. Resample onto a monotonic voltage grid, smooth, differentiate."""
        if v.size < 10:
            return np.array([]), np.array([])
        order = np.argsort(v)
        vu, idx = np.unique(v[order], return_index=True)
        qu = q[order][idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        qg = np.interp(grid, vu, qu)
        dqdv = np.gradient(Analytics.gaussian_smooth(qg, 3.0), grid)
        return grid, Analytics.gaussian_smooth(dqdv, 2.0)

    @staticmethod
    def differential_thermal(v: np.ndarray, t: np.ndarray):
        """DTV: dT/dV vs V (thermal fingerprint), same resample/smooth/differentiate."""
        if v.size < 10:
            return np.array([]), np.array([])
        order = np.argsort(v)
        vu, idx = np.unique(v[order], return_index=True)
        tu = t[order][idx]
        if vu.size < 10:
            return np.array([]), np.array([])
        grid = np.linspace(vu.min(), vu.max(), 200)
        tg = np.interp(grid, vu, tu)
        dtdv = np.gradient(Analytics.gaussian_smooth(tg, 3.0), grid)
        return grid, Analytics.gaussian_smooth(dtdv, 2.0)

    @staticmethod
    def grade(soh: float, ri_ohm: float, p: BatteryProfile) -> str:
        """Sort into A/B/C/REJECT from SoH and the internal-resistance growth ratio."""
        ri_ratio = ri_ohm / max(1e-6, p.internal_r)
        if soh >= 90 and ri_ratio <= 1.3:
            return "A"
        if soh >= 80 and ri_ratio <= 1.7:
            return "B"
        if soh >= 70 and ri_ratio <= 2.5:
            return "C"
        return "REJECT"
