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
    # ±1σ parameter uncertainties from covariance (nan when pcov is ill-conditioned)
    r0_std_ohm: float = float("nan")
    r1_std_ohm: float = float("nan")
    c1_std_farad: float = float("nan")
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
            "R0_std_ohm": self.r0_std_ohm,
            "R1_std_ohm": self.r1_std_ohm,
            "C1_std_farad": self.c1_std_farad,
        }


@dataclass
class FitResult2RC:
    """Structured result of a 2-RC Thevenin ECM identification run.

    The model is:
        V(t) = Voc - I*R0 - I*R1*(1-exp(-t/τ1)) - I*R2*(1-exp(-t/τ2))
    where τ1 = R1*C1 and τ2 = R2*C2.
    """
    R0_ohm: float
    R1_ohm: float
    C1_farad: float
    tau1_s: float
    R2_ohm: float
    C2_farad: float
    tau2_s: float
    r_squared: float
    rmse_v: float
    current_a: float
    voc_v: float
    # arrays kept for plotting / inspection (not part of the summary dict)
    _t: np.ndarray = field(default=None, repr=False)
    _v_meas: np.ndarray = field(default=None, repr=False)
    _v_pred: np.ndarray = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Summary dictionary (the public, serialisable result)."""
        return {
            "R0_ohm": self.R0_ohm,
            "R1_ohm": self.R1_ohm,
            "C1_farad": self.C1_farad,
            "tau1_s": self.tau1_s,
            "R2_ohm": self.R2_ohm,
            "C2_farad": self.C2_farad,
            "tau2_s": self.tau2_s,
            "r_squared": self.r_squared,
            "rmse_v": self.rmse_v,
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

        droop = abs(voc - v_seg[-1]) / max(abs(i_pulse), 1e-9)
        r_rc_base = max(droop - r0_step, 1e-4)
        span = max(t_rel[-1] - t_rel[0], 1e-3)
        bounds = ([1e-6, 1e-6, 1e-3], [10.0, 10.0, 1e7])

        # Multi-start: try 4 sets of (R1, τ) guesses spread across physically plausible
        # ranges. Single-guess TRF can trap in a local minimum when the initial τ is far
        # from the true time constant; the best result (lowest RMSE) is kept.
        candidates = [
            (r_rc_base * 0.5, span / 5.0),   # fast τ, moderate R1
            (r_rc_base * 0.6, span / 3.0),   # original default
            (r_rc_base * 0.8, span / 2.0),   # slow τ, higher R1
            (r_rc_base * 1.0, span * 0.75),  # very slow τ
        ]

        best_popt = best_pcov = None
        best_rmse = float("inf")

        for r1_g, tau_g in candidates:
            r1_g = max(r1_g, 1e-4)
            tau_g = max(tau_g, 1e-3)
            c1_g = max(tau_g / r1_g, 1e-3)
            p0 = [max(r0_step, 1e-5), r1_g, c1_g]
            try:
                popt_c, pcov_c = curve_fit(
                    model, t_rel, v_seg, p0=p0, bounds=bounds,
                    method="trf", maxfev=self.max_iterations,
                )
                rmse_c = float(np.sqrt(np.mean((v_seg - model(t_rel, *popt_c)) ** 2)))
                if rmse_c < best_rmse:
                    best_rmse = rmse_c
                    best_popt = popt_c
                    best_pcov = pcov_c
            except (RuntimeError, ValueError):
                continue

        if best_popt is None:
            raise ValueError("ECM fit: all initial guesses failed to converge. "
                             "Check that the pulse segment is long enough.")

        r0, r1, c1 = (float(x) for x in best_popt)

        # ±1σ parameter uncertainties from the covariance diagonal.
        # These quantify how well the transient data constrains each parameter —
        # large σ relative to the value means the parameter is poorly identified.
        try:
            perr = np.sqrt(np.diag(best_pcov))
            r0_std, r1_std, c1_std = float(perr[0]), float(perr[1]), float(perr[2])
            if not (np.isfinite(r0_std) and np.isfinite(r1_std) and np.isfinite(c1_std)):
                r0_std = r1_std = c1_std = float("nan")
        except Exception:
            r0_std = r1_std = c1_std = float("nan")

        v_pred = model(t_rel, r0, r1, c1)
        residuals = v_seg - v_pred
        rmse = float(np.sqrt(np.mean(residuals ** 2)))
        ss_res = float(np.sum(residuals ** 2))
        ss_tot = float(np.sum((v_seg - np.mean(v_seg)) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

        self._last = FitResult(
            r0_ohm=r0, r1_ohm=r1, c1_farad=c1, tau_s=r1 * c1,
            rmse_v=rmse, r_squared=r2, current_a=i_pulse, voc_v=voc,
            r0_std_ohm=r0_std, r1_std_ohm=r1_std, c1_std_farad=c1_std,
            _t=t_rel, _v_meas=v_seg, _v_pred=v_pred,
        )
        logger.info("ECM fit: R0=%.4f±%.4f Ω, R1=%.4f±%.4f Ω, C1=%.1f F, τ=%.2f s, R²=%.4f",
                    r0, r0_std, r1, r1_std, c1, r1 * c1, r2)
        return self._last.to_dict()

    # ------------------------------------------------------------------
    # 2-RC Thevenin fit
    # ------------------------------------------------------------------
    def fit_model_2rc(self, time_array, current_array, voltage_array,
                      initial_voc: float, r1rc_result: Optional[dict] = None
                      ) -> Optional[dict]:
        """Identify R0, R1, C1, R2, C2 (2-RC Thevenin ECM) from a current-pulse.

        Model:
            V(t) = Voc - I*R0 - I*R1*(1-exp(-t/τ1)) - I*R2*(1-exp(-t/τ2))

        The fit is only accepted when ALL of the following hold:

        * scipy.optimize.curve_fit converges within bounds.
        * The two time constants differ by at least a factor of 5
          (τ_fast / τ_slow <= 0.2) — ensures the two RC branches represent
          genuinely different dynamics rather than a degenerate duplicate.
        * R²(2-RC) > R²(1-RC) + 0.015 — 2-RC must be meaningfully better.
        * R²(2-RC) > 0.92 — the fit must explain the transient well.

        Parameters
        ----------
        time_array, current_array, voltage_array : array-like
            Equal-length raw time-series (seconds, amps, volts).
        initial_voc : float
            Open-circuit voltage (held fixed).
        r1rc_result : dict, optional
            Previously computed 1-RC result dict (keys ``r_squared``, ``R0_ohm``
            etc.).  When supplied the acceptance threshold is applied; when None
            the function still attempts the fit but skips the 1-RC comparison.

        Returns
        -------
        dict or None
            Keys: R0_ohm, R1_ohm, C1_farad, tau1_s, R2_ohm, C2_farad, tau2_s,
            r_squared, rmse_v, current_a, voc_v.  Returns None on any failure.
        """
        try:
            t = np.asarray(time_array, dtype=float)
            i = np.asarray(current_array, dtype=float)
            v = np.asarray(voltage_array, dtype=float)
            if not (t.shape == i.shape == v.shape) or t.ndim != 1:
                return None
            if t.size < 10:
                return None

            k = self._detect_step(i)
            r0_step = self._extract_r0(i, v, k)
            end = self._pulse_segment(i, k, self.step_threshold_a)
            if end - (k + 1) < 5:
                return None

            seg = slice(k + 1, end)
            t_rel = t[seg] - t[k + 1]
            v_seg = self._moving_average(v[seg])
            i_pulse = float(np.median(i[seg]))
            voc = float(initial_voc)

            def model_2rc(tt, r0, r1, c1, r2, c2):
                tau1 = r1 * c1
                tau2 = r2 * c2
                return (voc
                        - i_pulse * r0
                        - i_pulse * r1 * (1.0 - np.exp(-tt / tau1))
                        - i_pulse * r2 * (1.0 - np.exp(-tt / tau2)))

            # Initial guesses: split the total polarisation droop evenly across
            # two RC branches with a ~10x spread in time constants.
            droop = abs(voc - v_seg[-1]) / max(abs(i_pulse), 1e-9)
            r_rc_total = max(droop - r0_step, 1e-4)
            r1_guess = r_rc_total * 0.6
            r2_guess = r_rc_total * 0.4
            span = max(t_rel[-1] - t_rel[0], 1e-3)
            tau_fast = max(span / 10.0, 1e-3)
            tau_slow = max(span / 1.5, tau_fast * 5.0)
            c1_guess = max(tau_fast / r1_guess, 1e-3)
            c2_guess = max(tau_slow / r2_guess, 1e-3)

            p0 = [max(r0_step, 1e-5), r1_guess, c1_guess, r2_guess, c2_guess]
            bounds = (
                [1e-6, 1e-6, 1e-3, 1e-6, 1e-3],
                [10.0, 10.0, 1e7,  10.0, 1e7],
            )

            popt, _ = curve_fit(
                model_2rc, t_rel, v_seg, p0=p0, bounds=bounds,
                method="trf", maxfev=self.max_iterations,
            )
            r0, r1, c1, r2, c2 = (float(x) for x in popt)
            tau1 = r1 * c1
            tau2 = r2 * c2

            # Reject degenerate fit: time constants must differ by at least 5x.
            tau_min, tau_max = min(tau1, tau2), max(tau1, tau2)
            if tau_max < 1e-12 or (tau_min / tau_max) > (1.0 / 5.0):
                logger.info("2-RC fit rejected: τ1=%.3f s, τ2=%.3f s (< 5x ratio)", tau1, tau2)
                return None

            v_pred = model_2rc(t_rel, r0, r1, c1, r2, c2)
            residuals = v_seg - v_pred
            rmse = float(np.sqrt(np.mean(residuals ** 2)))
            ss_res = float(np.sum(residuals ** 2))
            ss_tot = float(np.sum((v_seg - np.mean(v_seg)) ** 2))
            r2_val = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

            # Acceptance: must beat 1-RC by margin AND reach absolute quality floor.
            if r2_val <= 0.92:
                logger.info("2-RC fit rejected: R²=%.4f < 0.92", r2_val)
                return None
            if r1rc_result is not None:
                r2_1rc = float(r1rc_result.get("r_squared", 0.0))
                if r2_val <= r2_1rc + 0.015:
                    logger.info("2-RC fit rejected: R²=%.4f not > 1-RC R²=%.4f + 0.015",
                                r2_val, r2_1rc)
                    return None

            result = FitResult2RC(
                R0_ohm=r0, R1_ohm=r1, C1_farad=c1, tau1_s=tau1,
                R2_ohm=r2, C2_farad=c2, tau2_s=tau2,
                r_squared=r2_val, rmse_v=rmse,
                current_a=i_pulse, voc_v=voc,
                _t=t_rel, _v_meas=v_seg, _v_pred=v_pred,
            )
            logger.info(
                "2-RC ECM fit: R0=%.4f Ω, R1=%.4f Ω τ1=%.2f s, R2=%.4f Ω τ2=%.2f s, R²=%.4f",
                r0, r1, tau1, r2, tau2, r2_val,
            )
            return result.to_dict()
        except Exception as exc:
            logger.debug("2-RC fit failed: %s", exc)
            return None

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
