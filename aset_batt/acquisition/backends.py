"""Instrument backends behind the acquisition worker.

All three implement the same interface and are called ONLY from the worker thread
(serialized by the worker's I/O mutex):

  * ``HardwareBackend``  — drives the project's real HAL (``HardwareController`` /
    ``MockHardwareController``): SCPI to PSU + e-load, MLX90614 temp via ESP32.
    Use it with ``MockHardwareController`` for no-hardware development.
  * ``VisaSerialBackend`` — direct PyVISA/pyserial reference (placeholders).

Sign convention returned by ``step``: charge current positive, discharge negative.
"""
from __future__ import annotations

import logging
from typing import Optional

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
        self._hppc_pulse = 30.0
        self._hppc_relax = 30.0

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
            self._hppc_pulse = max(1.0, float(p.hppc_pulse_duration))
            self._hppc_relax = max(1.0, float(p.hppc_relaxation_duration))

    def step(self, dt, elapsed):
        # HPPC cycle = relax (rest) → pulse. The leading rest establishes the OCV
        # baseline; the trailing rest of each cycle is the relaxation tail. Load is
        # toggled only on edges. Durations come from the battery profile.
        if self._cfg.mode == OperationMode.HPPC:
            cycle = self._hppc_relax + self._hppc_pulse
            want_load = (elapsed % cycle) >= self._hppc_relax
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
