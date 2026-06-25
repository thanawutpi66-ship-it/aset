import pyvisa
import pyvisa.constants as const
import serial
import serial.tools.list_ports
import threading
import time
import re
import logging

logger = logging.getLogger(__name__)

class HardwareController:
    def __init__(self):
        self.rm = pyvisa.ResourceManager()
        self.psu_inst = None
        self.load_inst = None
        self.is_connected = False
        self.inst_lock = threading.Lock()

        self.esp_serial = None
        self.is_esp_connected = False
        self.current_temp = 0.0
        self.last_esp_heartbeat = time.time()

        # Combined-measurement capability per instrument (None=unknown, True/False=cached
        # after the first probe). MEAS:SCAL:ALL:DC? returns V,I,P from ONE instantaneous
        # measurement → V and I are simultaneous (no intra-sample skew) and it's one
        # round-trip instead of two. Probed lazily; falls back to separate MEAS queries.
        self._psu_all = None
        self._load_all = None

    def get_visa_ports(self):
        try:
            return self.rm.list_resources()
        except Exception:
            return []

    def get_com_ports(self):
        try:
            return [port.device for port in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def connect_instruments(self, psu_port, load_port):
        self.psu_inst = self.rm.open_resource(psu_port)
        self.load_inst = self.rm.open_resource(load_port)

        for inst in [self.psu_inst, self.load_inst]:
            inst.baud_rate = 9600
            inst.data_bits = 8
            inst.stop_bits = const.StopBits.one
            inst.parity = const.Parity.none
            inst.flow_control = const.ControlFlow.none
            inst.read_termination = '\n'
            inst.write_termination = '\n'
            inst.timeout = 5000

        self.is_connected = True

    def set_psu(self, state, voltage_val="0"):
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    self.psu_inst.write(f":VOLT {voltage_val}")
                    self.psu_inst.write(":CURR 5.0")
                    self.psu_inst.write(":OUTP ON")
                else:
                    self.psu_inst.write(":OUTP OFF")
            except Exception as e:
                logger.error(f"PSU Command Error: {e}")

    def set_load(self, state, current_val="0"):
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    self.load_inst.write(f":CURR {current_val}")
                    self.load_inst.write(":INP ON")
                else:
                    self.load_inst.write(":INP OFF")
            except Exception as e:
                logger.error(f"Load Command Error: {e}")

    def set_load_raw(self, target):
        with self.inst_lock:
            try:
                self.load_inst.write(f":CURR {abs(target)}")
            except Exception as e:
                logger.error(f"set_load_raw error: {e}")

    def load_on(self):
        with self.inst_lock:
            try:
                self.load_inst.write(":INP ON")
            except Exception as e:
                logger.error(f"load_on error: {e}")

    def load_off(self):
        with self.inst_lock:
            try:
                self.load_inst.write(":INP OFF")
            except Exception as e:
                logger.error(f"load_off error: {e}")

    def psu_off(self):
        """ปิด output ของ PSU (ใช้โดย emergency shutdown + ChargeController)"""
        with self.inst_lock:
            try:
                self.psu_inst.write(":OUTP OFF")
            except Exception as e:
                logger.error(f"psu_off error: {e}")

    def _meas_vi(self, inst, which):
        """(voltage, current) from ONE combined measurement when the instrument supports
        it (``MEAS:SCAL:ALL:DC?`` → ``V,I,P`` — measured at the same instant, single
        round-trip), else two separate ``MEAS:VOLT?``/``MEAS:CURR?`` queries. The
        capability is probed once and cached in ``which`` so an unsupported instrument
        isn't retried every sample. Caller must hold ``inst_lock``."""
        cap = getattr(self, which)
        if cap is not False:                       # None (unknown) or True → try combined
            try:
                p = inst.query("MEAS:SCAL:ALL:DC?").strip().split(",")
                v, i = float(p[0]), float(p[1])
                if cap is None:
                    setattr(self, which, True)
                return v, i
            except Exception:
                setattr(self, which, False)        # not supported → stop trying
        v = float(inst.query("MEAS:VOLT?").strip())
        i = float(inst.query("MEAS:CURR?").strip())
        return v, i

    def read_vi(self):
        with self.inst_lock:
            v, i_psu = self._meas_vi(self.psu_inst, "_psu_all")   # combined when supported
            i_load = float(self.load_inst.query("MEAS:CURR?").strip())
            return v, i_psu, i_load

    def read_load_current(self):
        with self.inst_lock:
            try:
                return float(self.load_inst.query("MEAS:CURR?").strip())
            except Exception:
                return 0.0

    def transient_dcir_measure(self, current_target, delta_I):
        """วัด DCIR จาก transient voltage step"""
        with self.inst_lock:
            try:
                v_before = float(self.psu_inst.query("MEAS:VOLT?").strip())
                self.load_inst.write(f":CURR {abs(current_target)}")
                time.sleep(0.02)
                v_after = float(self.psu_inst.query("MEAS:VOLT?").strip())
                dcir_mohm = (abs(v_before - v_after) / abs(delta_I)) * 1000.0
                return dcir_mohm
            except Exception as e:
                logger.error(f"DCIR Transient Error: {e}")
                return 0.0

    def connect_esp32(self, port, callback=None):
        self.esp_serial = serial.Serial(port, 115200, timeout=1)
        self.is_esp_connected = True
        self.last_esp_heartbeat = time.time()
        threading.Thread(
            target=self._esp_monitor_loop, args=(callback,), daemon=True
        ).start()

    def disconnect_esp32(self):
        self.is_esp_connected = False
        if self.esp_serial:
            try:
                self.esp_serial.close()
            except Exception:
                pass

    def _esp_monitor_loop(self, callback):
        self.last_esp_heartbeat = time.time()
        while self.is_esp_connected:
            try:
                if self.esp_serial.in_waiting > 0:
                    line = self.esp_serial.readline().decode('utf-8', errors='ignore').strip()
                    if "Object =" in line and "*C" in line:
                        match = re.search(r"[-+]?\d*\.\d+|\d+", line.split("Object =")[1])
                        if match:
                            self.current_temp = float(match.group())
                            self.last_esp_heartbeat = time.time()
                            if callback:
                                callback(self.current_temp)
            except Exception:
                pass
            time.sleep(0.05)

    def shutdown_all(self):
        self.disconnect_instruments()
        self.disconnect_esp32()

    def disconnect_instruments(self):
        self.is_connected = False
        with self.inst_lock:
            try:
                if self.psu_inst:
                    self.psu_inst.write(":OUTP OFF")
                    self.psu_inst.close()
            except Exception:
                pass
            try:
                if self.load_inst:
                    self.load_inst.write(":INP OFF")
                    self.load_inst.close()
            except Exception:
                pass
            self.psu_inst = None
            self.load_inst = None

    def read_measurements(self, prefer_load_v=False):
        """Return (terminal_voltage, current). Convention: discharge = positive.

        Read V and I from the instrument that is actually ACTIVE, so the terminal
        voltage is always authoritative and the idle instrument is not queried:

          * ``prefer_load_v=True`` (discharge) — V and I from the e-load. The PSU
            output is OFF; a switching PSU's ``MEAS:VOLT?`` may return 0 when off, so
            it must NOT be the voltage source during discharge. i_net = +i_load.
          * ``prefer_load_v=False`` (charge/idle) — V and I from the PSU (it is the
            active source). i_net = −i_psu.

        Each read uses a single combined ``MEAS:SCAL:ALL:DC?`` when the instrument
        supports it (V and I sampled simultaneously — important so the DCIR step isn't
        skewed — and one round-trip instead of two), else falls back to separate queries.

        NB: verify on the bench that the e-load reports the terminal voltage as
        expected (``scripts/bench_check.py``); behaviour of MEAS:VOLT? while an
        output/input is off is instrument-specific.
        """
        with self.inst_lock:
            if prefer_load_v:
                v, i_load = self._meas_vi(self.load_inst, "_load_all")
                return v, i_load
            v, i_psu = self._meas_vi(self.psu_inst, "_psu_all")
            return v, -i_psu

    def set_charge(self, state, current_val="0"):
        """Optional charge control hook for IEC cycle-life tests."""
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    self.psu_inst.write(f":CURR {current_val}")
                    self.psu_inst.write(":OUTP ON")
                else:
                    self.psu_inst.write(":OUTP OFF")
            except Exception as e:
                logger.error(f"Charge control error: {e}")

    def set_psu_cccv(self, voltage, current):
        """ตั้ง PSU เป็น CC-CV: voltage = แรงดันเป้า (CV limit), current = กระแสจำกัด (CC limit)

        PSU ทำ CC↔CV ในฮาร์ดแวร์เอง: ถ้าแบตดึงกระแสถึง limit → CC ที่ current,
        เมื่อแรงดันแตะ voltage → CV ที่ voltage (กระแส taper ลง). ใช้โดย ChargeController
        (3-stage lead-acid / CC-CV lithium) — สั่งทั้งสอง limit พร้อมกันในคำสั่งเดียว
        """
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                self.psu_inst.write(f":VOLT {voltage}")
                self.psu_inst.write(f":CURR {current}")
                self.psu_inst.write(":OUTP ON")
            except Exception as e:
                logger.error(f"set_psu_cccv error: {e}")