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
        for attr in ("psu_inst", "load_inst"):
            inst = getattr(self, attr, None)
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.is_connected = False

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

    def read_vi(self):
        with self.inst_lock:
            v = float(self.psu_inst.query("MEAS:VOLT?").strip())
            i_psu = float(self.psu_inst.query("MEAS:CURR?").strip())
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
        logger.info("Connecting ESP32 on %s at 115200 baud", port)
        self.esp_serial = serial.Serial(port, 115200, timeout=1)
        self.is_esp_connected = True
        self.last_esp_heartbeat = time.time()
        logger.info("ESP32 serial opened on %s", port)
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

    # Ordered list of patterns tried against each serial line.
    # Each pattern must have one capture group returning the numeric temperature.
    _ESP_TEMP_PATTERNS = [
        re.compile(r"Object\s*=\s*([-+]?\d+\.?\d*)\s*\*?°?C", re.IGNORECASE),
        re.compile(r"Object\s+Temp[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
        re.compile(r"T_?obj[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
        re.compile(r"temp[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
    ]

    def _parse_esp_temp(self, line: str):
        """Return float temperature from a serial line, or None if not recognised."""
        for pat in self._ESP_TEMP_PATTERNS:
            m = pat.search(line)
            if m:
                return float(m.group(1))
        return None

    def _esp_monitor_loop(self, callback):
        self.last_esp_heartbeat = time.time()
        _unmatched_logged = set()   # avoid log-spamming the same unknown format
        while self.is_esp_connected:
            try:
                if self.esp_serial.in_waiting > 0:
                    line = self.esp_serial.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    temp = self._parse_esp_temp(line)
                    if temp is not None:
                        self.current_temp = temp
                        self.last_esp_heartbeat = time.time()
                        if callback:
                            callback(temp)
                    else:
                        # Log unrecognised lines once so the format can be diagnosed
                        key = line[:40]
                        if key not in _unmatched_logged:
                            logger.debug("ESP32 unmatched line: %r", line)
                            _unmatched_logged.add(key)
            except Exception as exc:
                logger.warning("ESP32 serial error: %s", exc)
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

    def read_measurements(self):
        """Return (voltage, current) for IEC test routines.

        Convention: discharge = positive (load_i − psu_i)."""
        v, psu_i, load_i = self.read_vi()
        return v, load_i - psu_i

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