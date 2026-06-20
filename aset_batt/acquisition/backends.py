"""Instrument backends behind the acquisition worker.

All three implement the same interface and are called ONLY from the worker thread
(serialized by the worker's I/O mutex):

  * ``HardwareBackend``  — drives the project's real HAL (``HardwareController`` /
    ``MockHardwareController``): SCPI to PSU + e-load, MLX90614 temp via ESP32.
  * ``VisaSerialBackend`` — direct PyVISA/pyserial reference (placeholders).
  * ``SimulatedBackend``  — physics-lite model so the pipeline runs with no rig.

Sign convention returned by ``step``: charge current positive, discharge negative.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from aset_batt.acquisition.models import TestConfig, OperationMode

logger = logging.getLogger(__name__)


class InstrumentBackend:
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def start_mode(self, cfg: TestConfig) -> None: ...
    def step(self, dt: float, elapsed: float) -> tuple[float, float]:
        raise NotImplementedError
    def read_temperature(self) -> float: ...
    def emergency_zero(self) -> None: ...
    def safe_shutdown(self) -> None: ...


class HardwareBackend(InstrumentBackend):
    """Adapter wiring the real instrument HAL into the QThread worker.

    ``hw`` is a ``HardwareController`` (real SCPI/VISA) or ``MockHardwareController``;
    both expose the same interface, so simulation_mode is honoured transparently.
    """

    def __init__(self, hw):
        self.hw = hw
        self._cfg: Optional[TestConfig] = None
        self._hppc_loaded = False

    def connect(self):
        pass  # HAL is connected by the application bootstrapper

    def start_mode(self, cfg: TestConfig):
        self._cfg = cfg
        p = cfg.profile
        if cfg.mode == OperationMode.CC_CV_CHARGE:
            self.hw.load_off()
            # PSU does CC↔CV in hardware: set the CV ceiling + CC limit together.
            self.hw.set_psu_cccv(p.max_charge_v, p.max_charge_a)
        elif cfg.mode == OperationMode.CC_DISCHARGE:
            self.hw.psu_off()
            self.hw.set_load(True, str(p.max_discharge_a))
        else:  # HPPC — start at rest; step() sequences the pulses
            self.hw.psu_off()
            self.hw.load_off()
            self._hppc_loaded = False

    def step(self, dt, elapsed):
        # HPPC: 10 s rest / 10 s discharge pulse, toggling the load only on edges.
        if self._cfg.mode == OperationMode.HPPC:
            want_load = (elapsed % 20.0) >= 10.0
            if want_load != self._hppc_loaded:
                if want_load:
                    self.hw.set_load(True, str(self._cfg.profile.max_discharge_a * 0.6))
                else:
                    self.hw.load_off()
                self._hppc_loaded = want_load
        v, i_net = self.hw.read_measurements()   # HAL convention: discharge = +
        return v, -i_net                          # worker convention: discharge = −

    def read_temperature(self):
        return float(getattr(self.hw, "current_temp", float("nan")))

    def emergency_zero(self):
        # Two independent calls so one failing instrument can't block the other.
        for fn in (self.hw.load_off, self.hw.psu_off):
            try:
                fn()
            except Exception as e:
                logger.error("emergency_zero step failed: %s", e)

    def safe_shutdown(self):
        self.emergency_zero()

    def disconnect(self):
        pass


class VisaSerialBackend(InstrumentBackend):
    """Direct PyVISA/pyserial reference backend (SCPI placeholders). Prefer
    ``HardwareBackend`` in the integrated app, which reuses the project HAL."""

    def __init__(self, psu_addr: str, load_addr: str, esp_port: str):
        self.psu_addr, self.load_addr, self.esp_port = psu_addr, load_addr, esp_port
        self.psu = self.load = self.ser = None
        self._cfg: Optional[TestConfig] = None

    def connect(self):
        import pyvisa, serial
        rm = pyvisa.ResourceManager()
        self.psu = rm.open_resource(self.psu_addr)
        self.load = rm.open_resource(self.load_addr)
        self.ser = serial.Serial(self.esp_port, 115200, timeout=0.2)
        self.psu.write("*RST"); self.load.write("*RST")

    def start_mode(self, cfg: TestConfig):
        self._cfg = cfg
        p = cfg.profile
        if cfg.mode == OperationMode.CC_CV_CHARGE:
            self.load.write(":INP OFF")
            self.psu.write(f":VOLT {p.max_charge_v}")
            self.psu.write(f":CURR {p.max_charge_a}")
            self.psu.write(":OUTP ON")
        elif cfg.mode == OperationMode.CC_DISCHARGE:
            self.psu.write(":OUTP OFF")
            self.load.write(":MODE CC")
            self.load.write(f":CURR {p.max_discharge_a}")
            self.load.write(":INP ON")
        else:
            self.psu.write(":OUTP OFF"); self.load.write(":INP OFF")

    def step(self, dt, elapsed):
        v = float(self.psu.query("MEAS:VOLT?"))
        i_src = float(self.psu.query("MEAS:CURR?"))
        i_load = float(self.load.query("MEAS:CURR?"))
        return v, (i_src - i_load)

    def read_temperature(self):
        try:
            line = self.ser.readline().decode(errors="ignore")
            if "=" in line:
                return float(line.split("=")[1].split("*")[0])
        except Exception:
            pass
        return float("nan")

    def emergency_zero(self):
        for inst, cmd in ((self.psu, ":OUTP OFF"), (self.load, ":INP OFF")):
            try:
                inst.write(":VOLT 0"); inst.write(":CURR 0"); inst.write(cmd)
            except Exception:
                pass

    def safe_shutdown(self):
        self.emergency_zero()

    def disconnect(self):
        for h in (self.psu, self.load, self.ser):
            try: h.close()
            except Exception: pass


class SimulatedBackend(InstrumentBackend):
    """Physics-lite battery + instrument simulation (no hardware required)."""

    def __init__(self, soh_factor: float = 0.93):
        self._cfg: Optional[TestConfig] = None
        self.soc = 0.2
        self.soh = soh_factor
        self.r = 0.03
        self.temp = 28.0
        self._i = 0.0

    def connect(self): pass
    def disconnect(self): pass

    def start_mode(self, cfg: TestConfig):
        self._cfg = cfg
        self.r = cfg.profile.internal_r / max(0.5, self.soh)
        self.soc = 0.15 if cfg.mode == OperationMode.CC_CV_CHARGE else 0.95

    def _ocv(self, soc: float) -> float:
        p = self._cfg.profile
        soc = min(1.0, max(0.0, soc))
        if p.chemistry == "Lead-Acid":
            cell = 1.95 + 0.18 * soc
        elif p.chemistry == "LiFePO4":
            cell = 3.0 + 0.25 * soc + 0.15 * (soc ** 6)
        else:
            cell = 3.4 + 0.8 * soc
        return cell * p.series

    def step(self, dt, elapsed):
        p = self._cfg.profile
        cap = p.capacity_ah * self.soh
        mode = self._cfg.mode
        if mode == OperationMode.CC_CV_CHARGE:
            ocv = self._ocv(self.soc)
            i = p.max_charge_a
            v = ocv + i * self.r
            if v >= p.max_charge_v:
                v = p.max_charge_v
                i = max(0.0, (p.max_charge_v - ocv) / self.r)
            self.soc = min(1.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        elif mode == OperationMode.CC_DISCHARGE:
            i = -p.max_discharge_a
            v = self._ocv(self.soc) + i * self.r
            self.soc = max(0.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        else:
            i = 0.0 if (elapsed % 20.0) < 10.0 else -p.max_discharge_a * 0.6
            v = self._ocv(self.soc) + i * self.r
            self.soc = max(0.0, self.soc + i * dt / 3600.0 / cap)
            self._i = i
        self.temp += (abs(self._i) ** 2 * self.r * 0.02 - (self.temp - 28.0) * 0.02) * dt
        self.temp += np.random.normal(0, 0.03)
        return v + np.random.normal(0, 0.002), self._i

    def read_temperature(self):
        return self.temp

    def emergency_zero(self):
        self._i = 0.0

    def safe_shutdown(self):
        self._i = 0.0
