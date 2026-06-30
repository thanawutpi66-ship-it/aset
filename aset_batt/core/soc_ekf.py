"""
2-state Extended Kalman Filter for SoC estimation (1-RC Thévenin model).

State  x = [SoC (%), V_RC (V)]
Process (discharge-positive current convention):
    SoC_{k+1}  = SoC_k − η·I·dt/3600 / C_eff · 100
    V_RC_{k+1} = a·V_RC_k + R1·(1−a)·I        with a = exp(−dt/τ), τ = R1·C1
Measurement (terminal voltage):
    V_term = OCV(SoC) − I·R0 − V_RC
    H = [ dOCV/dSoC ,  −1 ]

The Jacobian H[0] = dOCV/dSoC is *naturally small on a flat plateau* (LFP), so the
filter automatically trusts coulomb counting there and the OCV measurement near the
knees — this is exactly the behaviour the old ad-hoc `min_ocv_slope` guard tried to
approximate, but here it falls out of the math (no hard threshold needed).

Literature: EKF on lead-acid achieves SoC error < 2 % vs > several % for plain
coulomb-counting+OCV under leakage / short rest (ResearchGate 318326475; Nature
s41598-025-99931-8). This is a deliberately compact, dependency-light implementation
(NumPy only) suited to the rig's ~5 Hz readback.
"""
import numpy as np


class SoCEKF:
    def __init__(self, soc0: float, r0: float, r1: float, c1: float,
                 q_soc: float = 5.0e-8, q_vrc: float = 1.0e-5,
                 r_volt: float = 5.0e-5, adaptive_r: bool = False):
        # Starting Q/R/P0 follow the derivation in the project research workbook (P5):
        # P0 ≈ ±3% SoC initial uncertainty; Q[0,0] from η/sensor/Peukert error per step
        # (dt-scaled in predict); Q[1,1] from R1/C1 uncertainty; R ≈ 7 mV (5 Hz SCPI +
        # generic-OCV model error — kept above the 1 mV sensor floor until per-cell GITT).
        # These are starting points: tune offline against ground truth via replay.py.
        self.x = np.array([float(soc0), 0.0])      # [SoC %, V_RC V]
        self.P = np.diag([10.0, 0.01])             # ±~3% SoC, ±0.1 V V_RC
        self.R0 = max(1e-4, float(r0))
        self.R1 = max(1e-4, float(r1))
        self.C1 = max(1.0, float(c1))
        self.Q = np.diag([float(q_soc), float(q_vrc)])
        self.R = float(r_volt)                      # measurement variance (V²)
        # Adaptive R (AEKF): blend measured innovation variance into R so the filter
        # de-weights the voltage when the model disagrees (ResearchGate AEKF / arXiv
        # 2304.07748). Off by default; enable for noisy / model-mismatched runs.
        self.adaptive_r = bool(adaptive_r)
        self._innov_var = float(r_volt)
        self._r_min = float(r_volt)

    # -- accessors -----------------------------------------------------------
    @property
    def soc(self) -> float:
        return float(self.x[0])

    @property
    def v_rc(self) -> float:
        return float(self.x[1])

    def set_soc(self, soc: float) -> None:
        """Hard reset (used by endpoint anchors / OCV init). Shrinks covariance."""
        self.x = np.array([min(100.0, max(0.0, float(soc))), 0.0])
        self.P = np.diag([1.0, 0.01])

    def set_rc(self, r0: float, r1: float, c1: float) -> None:
        """Update ECM parameters from a fresh HPPC fit."""
        self.R0 = max(1e-4, float(r0))
        self.R1 = max(1e-4, float(r1))
        self.C1 = max(1.0, float(c1))

    # -- filter steps --------------------------------------------------------
    def predict(self, current: float, dt: float, cap_ah: float, eta: float) -> None:
        dt = max(1e-3, float(dt))
        tau = max(1e-3, self.R1 * self.C1)
        a = float(np.exp(-dt / tau))
        soc, vrc = self.x
        if cap_ah > 1e-6:
            soc = soc - eta * current * (dt / 3600.0) / cap_ah * 100.0
        vrc = a * vrc + self.R1 * (1.0 - a) * current
        self.x = np.array([soc, vrc])

        F = np.array([[1.0, 0.0], [0.0, a]])
        Q = self.Q * dt                              # scale process noise with dt
        self.P = F @ self.P @ F.T + Q

    def update(self, v_meas: float, current: float, ocv_pack: float,
               docv_dsoc_pack: float, r0: float) -> None:
        soc, vrc = self.x
        v_pred = ocv_pack - current * max(1e-4, r0) - vrc
        H = np.array([float(docv_dsoc_pack), -1.0])
        innov = float(v_meas) - v_pred
        if self.adaptive_r:
            # EWMA of innovation² → R ≈ innovation variance (floored at sensor noise)
            self._innov_var = 0.95 * self._innov_var + 0.05 * innov * innov
            self.R = max(self._r_min, self._innov_var)
        S = float(H @ self.P @ H.T + self.R)
        if S <= 0:
            return
        K = (self.P @ H) / S                         # 2-vector gain
        self.x = self.x + K * innov
        self.x[0] = min(100.0, max(0.0, self.x[0]))
        I2 = np.eye(2)
        self.P = (I2 - np.outer(K, H)) @ self.P
        # keep covariance symmetric / positive
        self.P = 0.5 * (self.P + self.P.T)
