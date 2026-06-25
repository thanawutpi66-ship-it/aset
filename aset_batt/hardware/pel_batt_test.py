"""
pel_batt_test.py — drive the GW Instek PEL-3111 for a capacity / SoH discharge test.

Two paths:

  * ``run_pc_discharge()`` — **reliable, uses only VERIFIED SCPI** that the project
    already drives on the load (``:MODE CC``, ``:CURR``, ``:INP ON/OFF``,
    ``MEAS:VOLT?``, ``MEAS:CURR?``). The PC does trapezoidal coulomb counting with a
    ``perf_counter`` clock (sub-µs), so capacity is accurate regardless of the ~5 Hz
    readback. This is the recommended path.

  * ``run_native_batt_test()`` — **OPTIONAL**: lets the *instrument* run its built-in
    BATT Test Automation + Datalog (it discharges and logs Ah/Wh into its own memory,
    then you read it back — no PC-side timing at all). The exact remote SCPI for that
    function is **NOT in the panel user manual** and must be confirmed in the PEL-3111
    *programming* manual; the strings below are best-effort (the PEL-3000 is documented
    as Kikusui PLZ-4W command-compatible) and are marked VERIFY. The method probes them
    and falls back to ``run_pc_discharge`` if the instrument rejects them.

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


# ---------------------------------------------------------------------------
# Native BATT-test SCPI — VERIFY against the PEL-3111 programming manual.
# These let the load run + log the discharge itself. If any is rejected, the driver
# falls back to the PC-side path. Edit here once you confirm the real commands.
# ---------------------------------------------------------------------------
NATIVE_BATT_SCPI = {
    "select_mode": ":BATT:MODE CC",         # VERIFY: discharge mode (CC/CR/CP)
    "set_current": ":BATT:CURR {a}",        # VERIFY: discharge current
    "stop_volt":   ":BATT:STOP:VOLT {v}",   # VERIFY: stop voltage
    "datalog_int": ":BATT:DLOG:TIM {s}",    # VERIFY: datalog interval (s)
    "start":       ":BATT:STAR ON",         # VERIFY: start the BATT test
    "running?":    ":BATT:STAT?",           # VERIFY: 1 while running
    "fetch_ah":    ":BATT:FETC:AH?",        # VERIFY: logged amp-hours
    "fetch_wh":    ":BATT:FETC:WH?",        # VERIFY: logged watt-hours
    "abort":       ":BATT:STAR OFF",        # VERIFY: stop the BATT test
}


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
        except Exception:
            pass

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

    # -- optional path: instrument-native BATT test + datalog --------------
    def native_supported(self) -> bool:
        """Probe whether the native BATT-test SCPI is accepted (best-effort)."""
        try:
            self.load.query(NATIVE_BATT_SCPI["running?"])
            return True
        except Exception as e:
            logger.info("native BATT test not available (%s) — use run_pc_discharge", e)
            return False

    def run_native_batt_test(self, current_a: float, stop_voltage: float,
                             datalog_interval_s: float = 1.0,
                             poll_s: float = 2.0,
                             max_seconds: float = 8 * 3600) -> Optional[DischargeResult]:
        """Run the load's built-in BATT Test + Datalog, then read back Ah/Wh.

        Returns a DischargeResult on success, or None if the instrument doesn't accept
        the (VERIFY-marked) commands — caller should then use ``run_pc_discharge``."""
        if not self.native_supported():
            return None
        s = NATIVE_BATT_SCPI
        try:
            self._w(s["select_mode"])
            self._w(s["set_current"].format(a=current_a))
            self._w(s["stop_volt"].format(v=stop_voltage))
            self._w(s["datalog_int"].format(s=datalog_interval_s))
            self._w(s["start"])
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < max_seconds:
                if self._qf(s["running?"]) < 0.5:
                    break
                time.sleep(poll_s)
            else:
                self._w(s["abort"])
            ah = self._qf(s["fetch_ah"])
            wh = self._qf(s["fetch_wh"])
            if ah != ah:
                logger.warning("native datalog returned no Ah — fall back to PC path")
                return None
            return DischargeResult(
                capacity_ah=ah, energy_wh=(wh if wh == wh else 0.0),
                soh_pct=soh_from_capacity(ah, self.rated_ah),
                stopped_reason="native batt test", source="native_datalog")
        except Exception as e:
            logger.warning("native BATT test failed (%s) — fall back to PC path", e)
            self.safe_off()
            return None
