"""
pel_batt_test.py — drive the GW Instek PEL-3111 for a capacity / SoH discharge test.

Two paths:

  * ``run_pc_discharge()`` — **reliable, uses only VERIFIED SCPI** that the project
    already drives on the load (``:MODE CC``, ``:CURR``, ``:INP ON/OFF``,
    ``MEAS:VOLT?``, ``MEAS:CURR?``). The PC does trapezoidal coulomb counting with a
    ``perf_counter`` clock (sub-µs), so capacity is accurate regardless of the ~5 Hz
    readback. This is the recommended path.

  * ``run_native_batt_test()`` — **DISABLED (always falls back to run_pc_discharge)**:
    the instrument's built-in BATT Test Automation + Datalog would let it discharge and
    log Ah/Wh into its own memory with no PC-side timing at all — but per the real
    PEL-3000H Programming Manual, there is **no SCPI command that returns the
    accumulated Ah/Wh** from a finished BATT test (``:BATT:RESult?`` only returns the
    *instantaneous* current/voltage at query time, not accumulated capacity/energy).
    Starting the native test would therefore discharge the real battery with no way to
    retrieve a usable result afterwards — worse than never engaging it at all. The
    ``NATIVE_BATT_SCPI`` strings below are corrected against the manual (an earlier
    version had 5 of 8 wrong — see ``docs/rig_investigation_findings.md``) so they're
    ready to use once accumulated-Ah/Wh retrieval is solved (most likely via
    downloading the instrument's datalog file, a separate feature), but
    ``native_supported()`` is hardcoded to return ``False`` until then.

Standalone: ``pyvisa`` is imported lazily so importing this module never needs hardware.
Run it from ``scripts/pel_capacity_test.py``.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (no hardware) — easy to reason about / unit-test
# ---------------------------------------------------------------------------
def integrate_capacity(t_s: List[float], i_a: List[float], v_v: List[float]):
    """Trapezoidal coulomb + energy integration over a discharge.

    ``i_a`` discharge-positive. Returns ``(capacity_ah, energy_wh)`` counting only the
    discharge (i>0) portion."""
    ah = wh = 0.0
    for k in range(1, len(t_s)):
        dt = t_s[k] - t_s[k - 1]
        if dt <= 0:
            continue
        i_mid = 0.5 * (max(0.0, i_a[k]) + max(0.0, i_a[k - 1]))
        v_mid = 0.5 * (v_v[k] + v_v[k - 1])
        ah += i_mid * dt / 3600.0
        wh += i_mid * v_mid * dt / 3600.0
    return ah, wh


def soh_from_capacity(capacity_ah: float, rated_ah: float) -> float:
    """SoH % = measured discharge capacity ÷ rated, clamped to [0, 120]."""
    if rated_ah <= 0:
        return float("nan")
    return max(0.0, min(120.0, 100.0 * capacity_ah / rated_ah))


@dataclass
class DischargeResult:
    capacity_ah: float = 0.0
    energy_wh: float = 0.0
    soh_pct: float = float("nan")
    duration_s: float = 0.0
    stopped_reason: str = ""
    n_samples: int = 0
    t_s: List[float] = field(default_factory=list)
    v_v: List[float] = field(default_factory=list)
    i_a: List[float] = field(default_factory=list)
    source: str = "pc_coulomb"     # or "native_datalog"





class PelBattTest:
    """Capacity / SoH driver for the PEL-3111 e-load."""

    def __init__(self, load_resource, rated_capacity_ah: float):
        """``load_resource`` is an open PyVISA resource for the e-load."""
        self.load = load_resource
        self.rated_ah = float(rated_capacity_ah)

    # -- verified low-level (same SCPI the app's HAL uses) ------------------
    def _w(self, cmd: str):
        self.load.write(cmd)

    def _qf(self, cmd: str) -> float:
        try:
            return float(self.load.query(cmd).strip())
        except Exception:
            return float("nan")

    def _read_vi(self):
        """Terminal V + I straight from the load (it is the active instrument while
        discharging — the authoritative terminal voltage). Discharge-positive."""
        v = self._qf("MEAS:VOLT?")
        i = self._qf("MEAS:CURR?")
        return v, (i if i == i else 0.0)

    def safe_off(self):
        try:
            self._w(":INP OFF")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    # -- recommended path: PC-driven CC discharge + coulomb counting -------
    def run_pc_discharge(self, current_a: float, stop_voltage: float,
                         max_seconds: float = 8 * 3600,
                         sample_period: float = 0.2,
                         should_stop=None) -> DischargeResult:
        """CC discharge until the terminal hits ``stop_voltage`` (or timeout / stop).
        Uses only verified SCPI; capacity from perf_counter trapezoidal integration."""
        self._w(":MODE CC")
        self._w(f":CURR {current_a}")
        t_s: List[float] = []
        v_v: List[float] = []
        i_a: List[float] = []
        reason = "completed"
        self._w(":INP ON")
        t0 = time.perf_counter()
        try:
            while True:
                v, i = self._read_vi()
                now = time.perf_counter() - t0
                t_s.append(now); v_v.append(v); i_a.append(i)
                if should_stop and should_stop():
                    reason = "stopped by caller"; break
                if v == v and v <= stop_voltage:
                    reason = "reached stop voltage"; break
                if now >= max_seconds:
                    reason = "timeout"; break
                time.sleep(sample_period)
        finally:
            self.safe_off()
        ah, wh = integrate_capacity(t_s, i_a, v_v)
        return DischargeResult(
            capacity_ah=ah, energy_wh=wh, soh_pct=soh_from_capacity(ah, self.rated_ah),
            duration_s=(t_s[-1] if t_s else 0.0), stopped_reason=reason,
            n_samples=len(t_s), t_s=t_s, v_v=v_v, i_a=i_a, source="pc_coulomb")


