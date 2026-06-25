"""
1-RC Thevenin ECM parameter identification for battery cells/packs (LiFePO4).

Given a current-pulse experiment (rest → constant-current pulse → rest), this module
extracts the equivalent-circuit parameters of a first-order Thevenin model:

        Voc ──[ R0 ]──┬───────────── V_terminal
                      │
                   [ R1 ║ C1 ]      (one RC pair, time constant τ = R1·C1)

Terminal-voltage response during a constant-current pulse (current I, t from step):

        V(t) = Voc − I·R0 − I·R1·(1 − exp(−t / (R1·C1)))

Identification strategy
-----------------------
1. **R0 (ohmic):** detected from the *instantaneous* voltage jump at the current
   step. Ohm's law on the step: R0 = |ΔV / ΔI|. This is the fast, non-diffusive
   part of the response and is read directly rather than fitted, which makes it
   robust to the choice of optimiser.
2. **R1, C1 (polarisation):** fitted to the transient that follows the step with
   ``scipy.optimize.curve_fit``. Because the parameters must stay physical
   (R0, R1, C1 > 0), the bounded **Trust Region Reflective** method is used
   (the bounded generalisation of Levenberg–Marquardt; unbounded LM cannot honour
   box constraints). R0 from step 1 seeds the fit and is refined jointly.
3. A moving-average pre-filter removes sensor jitter before fitting.

Sign convention: pass ``current`` with **discharge positive** (terminal voltage
sags below Voc), matching the equation above. Charge pulses (current negative)
also fit correctly as long as the data is internally consistent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import curve_fit

logger = logging.getLogger(__name__)


@dataclass
class FitResult:
    """Structured result of one parameter-identification run."""
    r0_ohm: float
    r1_ohm: float
    c1_farad: float
    tau_s: float
    rmse_v: float
    r_squared: float
    current_a: float
    voc_v: float
    # arrays kept for plotting / inspection (not part of the summary dict)
    _t: np.ndarray = field(default=None, repr=False)
    _v_meas: np.ndarray = field(default=None, repr=False)
    _v_pred: np.ndarray = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Summary dictionary (the public, serialisable result)."""
        return {
            "R0_ohm": self.r0_ohm,
            "R1_ohm": self.r1_ohm,
            "C1_farad": self.c1_farad,
            "tau_s": self.tau_s,
            "rmse_v": self.rmse_v,
            "r_squared": self.r_squared,
            "current_a": self.current_a,
            "voc_v": self.voc_v,
        }


class BatteryParameterIdentifier:
    """Identify 1-RC Thevenin ECM parameters from current-pulse time-series data.

    Parameters
    ----------
    smooth_window : int
        Moving-average window (samples) for the voltage pre-filter. ``<= 1``
        disables filtering. Forced odd.
    step_threshold_a : float
        Minimum |ΔI| between consecutive samples to qualify as a current step.
    max_iterations : int
        ``maxfev`` passed to ``curve_fit``.
    """

    def __init__(self, smooth_window: int = 5, step_threshold_a: float = 0.05,
                 max_iterations: int = 10000):
        self.smooth_window = int(smooth_window)
        self.step_threshold_a = float(step_threshold_a)
        self.max_iterations = int(max_iterations)
        self._last: Optional[FitResult] = None

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------
    def _moving_average(self, y: np.ndarray) -> np.ndarray:
        """Edge-padded moving-average low-pass filter (removes sensor jitter)."""
        w = self.smooth_window
        if w <= 1:
            return y
        w = w if w % 2 == 1 else w + 1          # force odd for symmetric window
        pad = w // 2
        kernel = np.ones(w, dtype=float) / w
        padded = np.pad(y, pad, mode="edge")
        return np.convolve(padded, kernel, mode="valid")

    # ------------------------------------------------------------------
    # Step / R0 extraction
    # ------------------------------------------------------------------
    def _detect_step(self, current: np.ndarray) -> int:
        """Index ``k`` of the largest current transition (step between k and k+1)."""
        di = np.diff(current)
        k = int(np.argmax(np.abs(di)))
        if abs(di[k]) < self.step_threshold_a:
            raise ValueError("No current step detected (|ΔI| below threshold). "
                             "Provide a pulse with a clear current edge.")
        return k

    @staticmethod
    def _extract_r0(current: np.ndarray, voltage_raw: np.ndarray, k: int) -> float:
        """Ohmic resistance from the instantaneous jump at the step: R0 = |ΔV/ΔI|.

        MUST be read from the **raw** (unfiltered) voltage. A moving-average filter
        smears the near-instantaneous ohmic step across its window — blending the
        rested pre-step voltage into the first loaded sample — which underestimates
        R0 and leaks the missing drop into R1. The pre-step baseline uses a *median*
        over a few rested samples (robust to jitter); the first post-step sample
        carries the ohmic jump.
        """
        v_before = float(np.median(voltage_raw[max(0, k - 2):k + 1]))
        v_after = float(voltage_raw[k + 1])
        di = float(current[k + 1] - current[k])
        if abs(di) < 1e-9:
            raise ValueError("Degenerate current step (ΔI ≈ 0).")
        return abs((v_after - v_before) / di)

    @staticmethod
    def _pulse_segment(current: np.ndarray, k: int, threshold: float) -> int:
        """End index (exclusive) of the constant-current region after the step."""
        i_pulse = current[k + 1]
        end = k + 1
        n = len(current)
        while end < n and abs(current[end] - i_pulse) < max(threshold, 1e-6):
            end += 1
        return end

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fit_model(self, time_array, current_array, voltage_array,
                  initial_voc: float) -> dict:
        """Identify R0, R1, C1 (and τ, RMSE) from a current-pulse experiment.

        Parameters
        ----------
        time_array, current_array, voltage_array : array-like
            Equal-length raw time-series (seconds, amps, volts). Current uses the
            discharge-positive convention.
        initial_voc : float
            Open-circuit voltage at the operating SoC (held fixed as ``Voc``).

        Returns
        -------
        dict
            ``{R0_ohm, R1_ohm, C1_farad, tau_s, rmse_v, r_squared, current_a, voc_v}``.

        Raises
        ------
        ValueError
            On malformed input, no detectable current step, or a fit that fails to
            converge.

        Notes
        -----
        The polarisation fit minimises the sum of squared residuals between the
        measured transient and ``V(t) = Voc − I·(R0 + R1·(1 − exp(−t/(R1·C1))))``
        over the bounded domain R0, R1 ∈ [1e-6, 10] Ω, C1 ∈ [1e-3, 1e7] F via the
        Trust Region Reflective optimiser. R0 is seeded from the step jump.
        """
        t = np.asarray(time_array, dtype=float)
        i = np.asarray(current_array, dtype=float)
        v = np.asarray(voltage_array, dtype=float)
        if not (t.shape == i.shape == v.shape) or t.ndim != 1:
            raise ValueError("time, current and voltage must be 1-D arrays of equal length.")
        if t.size < 10:
            raise ValueError("Need at least 10 samples to identify the model.")

        k = self._detect_step(i)
        r0_step = self._extract_r0(i, v, k)          # RAW voltage — never the filtered one
        end = self._pulse_segment(i, k, self.step_threshold_a)
        if end - (k + 1) < 5:
            raise ValueError("Pulse segment too short to fit the RC transient.")

        seg = slice(k + 1, end)
        t_rel = t[seg] - t[k + 1]                    # time from the step edge
        # Filter the pulse segment IN ISOLATION so the edge-padded moving average
        # cannot bleed the rested pre-step voltage into the early (R0/R1-rich) samples.
        v_seg = self._moving_average(v[seg])
        i_pulse = float(np.median(i[seg]))
        voc = float(initial_voc)

        # Closure model with I and Voc fixed → curve_fit varies only R0, R1, C1.
        def model(tt, r0, r1, c1):
            return voc - i_pulse * (r0 + r1 * (1.0 - np.exp(-tt / (r1 * c1))))

        # Initial guesses from the data: steady-state droop gives R0+R1; τ ≈ 1/3 span.
        droop = abs(voc - v_seg[-1]) / max(abs(i_pulse), 1e-9)
        r1_guess = max(droop - r0_step, 1e-4)
        tau_guess = max((t_rel[-1] - t_rel[0]) / 3.0, 1e-3)
        c1_guess = max(tau_guess / r1_guess, 1e-3)
        p0 = [max(r0_step, 1e-5), r1_guess, c1_guess]
        bounds = ([1e-6, 1e-6, 1e-3], [10.0, 10.0, 1e7])

        try:
            popt, _ = curve_fit(
                model, t_rel, v_seg, p0=p0, bounds=bounds,
                method="trf", maxfev=self.max_iterations,
            )
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f"Curve fit did not converge: {exc}") from exc

        r0, r1, c1 = (float(x) for x in popt)
        v_pred = model(t_rel, r0, r1, c1)
        residuals = v_seg - v_pred
        rmse = float(np.sqrt(np.mean(residuals ** 2)))
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((v_seg - np.mean(v_seg)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

        self._last = FitResult(
            r0_ohm=r0, r1_ohm=r1, c1_farad=c1, tau_s=r1 * c1,
            rmse_v=rmse, r_squared=r2, current_a=i_pulse, voc_v=voc,
            _t=t_rel, _v_meas=v_seg, _v_pred=v_pred,
        )
        logger.info("ECM fit: R0=%.4f Ω, R1=%.4f Ω, C1=%.1f F, τ=%.2f s, R²=%.4f",
                    r0, r1, c1, r1 * c1, r2)
        return self._last.to_dict()

    # ------------------------------------------------------------------
    # Visualisation
    # ------------------------------------------------------------------
    def plot_fit(self, save_path: Optional[str] = None, show: bool = False):
        """Plot measured vs. model terminal voltage for the most recent fit.

        Annotates the identified parameters and the R² goodness-of-fit. Returns the
        Matplotlib figure. Call :meth:`fit_model` first.
        """
        if self._last is None:
            raise RuntimeError("No fit available — call fit_model() first.")
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        r = self._last
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
        ax.plot(r._t, r._v_meas, ".", ms=4, color="#888888",
                label="Measured (filtered)")
        ax.plot(r._t, r._v_pred, "-", lw=2, color="#1f4e79",
                label="1-RC model")
        ax.set_xlabel("Time from step (s)")
        ax.set_ylabel("Terminal voltage (V)")
        ax.set_title("Thevenin 1-RC parameter identification")
        ax.grid(True, alpha=0.3)
        txt = (f"R0 = {r.r0_ohm * 1e3:.2f} mΩ\n"
               f"R1 = {r.r1_ohm * 1e3:.2f} mΩ\n"
               f"C1 = {r.c1_farad:.0f} F\n"
               f"τ  = {r.tau_s:.1f} s\n"
               f"RMSE = {r.rmse_v * 1e3:.2f} mV\n"
               f"R²  = {r.r_squared:.4f}")
        ax.text(0.97, 0.05, txt, transform=ax.transAxes, ha="right", va="bottom",
                family="monospace", fontsize=9,
                bbox=dict(boxstyle="round", fc="#f0f0f0", ec="#cccccc"))
        ax.legend(loc="upper right")
        fig.tight_layout()
        if save_path:
            fig.savefig(save_path)
            logger.info("Saved fit plot to %s", save_path)
        if show:
            plt.show()
        return fig
