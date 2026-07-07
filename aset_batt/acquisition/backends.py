"""Instrument backends behind the acquisition worker.

Called ONLY from the worker thread (serialized by the worker's I/O mutex):

  * ``HardwareBackend``  — drives the project's real HAL (``HardwareController`` /
    ``MockHardwareController``): SCPI to PSU + e-load, MLX90614 temp via ESP32.
    Use it with ``MockHardwareController`` for no-hardware development.

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
                    p = self._cfg.profile
                    i_pulse = min(p.hppc_pulse_crate * p.capacity_ah, p.max_discharge_a)
                    self.hw.set_load(True, str(i_pulse))
                else:
                    self.hw.load_off()
                self._hppc_loaded = want_load
        # Read the terminal voltage from the ACTIVE instrument: the e-load during
        # discharge/HPPC (PSU off), the PSU during charge. Avoids trusting a PSU
        # MEAS:VOLT? while its output is off.
        discharge = self._cfg.mode in (OperationMode.CC_DISCHARGE, OperationMode.HPPC)
        v, i_net = self.hw.read_measurements(prefer_load_v=discharge)  # HAL: discharge = +
        return v, -i_net                          # worker convention: discharge = −

    def read_temperature(self):
        return float(getattr(self.hw, "current_temp", float("nan")))

    def emergency_zero(self):
        # Independent calls so one failing instrument can't block the other.
        # psu_off() also cuts the SSR relay (GPIO16) — see HardwareController.psu_off.
        for fn in (self.hw.load_off, self.hw.psu_off):
            try:
                fn()
            except Exception as e:
                logger.error("emergency_zero step failed: %s", e)

    def safe_shutdown(self):
        self.emergency_zero()

    def disconnect(self):
        pass
