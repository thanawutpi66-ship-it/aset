"""
Mock Hardware Controller — ใช้สำหรับ simulation_mode และ unit testing
ต้องมี interface เดียวกับ HardwareController ทุก attribute และ method
"""
import threading
import math
import time


class MockHardwareController:
    def __init__(self):
        self.is_connected = True
        self.is_esp_connected = False
        self.current_temp = 25.0         # °C จำลอง
        self.inst_lock = threading.Lock()  # ต้องมีเหมือน HardwareController

        # จำลอง instruments (ไม่ใช้จริง แต่ต้องไม่ให้ AttributeError)
        self.psu_inst = None
        self.load_inst = _MockInst()

        # สถานะภายใน
        self._load_current = 0.0
        self._psu_voltage = 3.3
        self._t_start = time.time()

    # ------------------------------------------------------------------
    # Port enumeration
    # ------------------------------------------------------------------

    def get_visa_ports(self):
        return ["MOCK::PSU::INSTR", "MOCK::LOAD::INSTR"]

    def get_com_ports(self):
        return ["COM_MOCK"]

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_instruments(self, psu_port, load_port):
        self.is_connected = True

    def connect_esp32(self, port, callback=None):
        self.is_esp_connected = True

    def disconnect_esp32(self):
        self.is_esp_connected = False

    # ------------------------------------------------------------------
    # PSU / Load control
    # ------------------------------------------------------------------

    def set_psu(self, state, voltage_val="0"):
        if state:
            self._psu_voltage = float(voltage_val)

    def set_load(self, state, current_val="0"):
        self._load_current = float(current_val) if state else 0.0

    def set_load_raw(self, target):
        self._load_current = abs(float(target))

    def load_on(self):
        pass

    def load_off(self):
        self._load_current = 0.0

    def psu_off(self):
        self._psu_voltage = 0.0

    # ------------------------------------------------------------------
    # Measurement — จำลองแบตเตอรี่ลดแรงดันตามเวลา
    # ------------------------------------------------------------------

    def read_vi(self):
        elapsed = time.time() - self._t_start
        # แรงดันลดลงช้าๆ จาก 3.3V → 3.0V ใน 3600 วินาที
        v = max(3.0, 3.3 - elapsed * 0.0001 + 0.01 * math.sin(elapsed))
        psu_i = 0.5
        load_i = self._load_current if self._load_current > 0 else 0.5
        return round(v, 4), round(psu_i, 4), round(load_i, 4)

    def read_load_current(self):
        return self._load_current

    def transient_dcir_measure(self, current_target, delta_I):
        return 0.05  # mock DCIR (Ohm)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown_all(self):
        self.disconnect_instruments()
        self.disconnect_esp32()

    def disconnect_instruments(self):
        self.is_connected = False
        self.psu_inst = None
        self.load_inst = _MockInst()

    def read_measurements(self):
        # Convention: discharge = positive (load_i − psu_i)
        v, psu_i, load_i = self.read_vi()
        return v, load_i - psu_i

    def set_charge(self, state, current_val="0"):
        if state:
            self._psu_voltage = min(4.2, self._psu_voltage + 0.01)

    def set_psu_cccv(self, voltage, current):
        """จำลอง CC-CV charge: เก็บ setpoint ให้ read_vi สะท้อนการชาร์จแบบหยาบ"""
        self._cccv_v = float(voltage)
        self._cccv_i = float(current)


class _MockInst:
    """จำลอง VISA instrument object (ใช้ใน profile loop ที่เรียก load_inst.write)"""

    def write(self, cmd):
        pass  # ไม่ทำอะไร

    def query(self, cmd):
        return "0.0"