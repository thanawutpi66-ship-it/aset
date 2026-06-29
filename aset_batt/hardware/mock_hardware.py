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

        # สถานะภายใน — จำลองแพ็คจริง (default config = lead-acid 6S ~12.4V rest)
        self._load_current = 0.0
        self._psu_voltage = 0.0
        self._sim_v = 12.4          # แรงดันแพ็คจำลอง (V)
        self._charging = False
        self._cccv_v = None         # CV target ขณะชาร์จ (V, ระดับแพ็ค)
        self._charge_i = 0.0        # กระแสชาร์จจำลอง (A) — taper ลงในช่วง CV
        self._t_start = time.time()
        # 1-RC overpotential model so HPPC pulses show a realistic ohmic step + RC
        # relaxation (lets BatteryParameterIdentifier extract non-zero R0/R1 on mock)
        self._mock_r0 = 0.030       # Ω ohmic (instant step under load)
        self._mock_r1 = 0.020       # Ω charge-transfer
        self._mock_tau = 12.0       # s  (R1·C1)
        self._load_on_t = None      # monotonic time the load last turned on
        self._load_off_t = None     # monotonic time the load last turned off
        self._relax_v0 = 0.0        # R1 overpotential at pulse end (decays in rest)
        self.psu_bleed_a: float = 0.0  # mirror HardwareController.psu_bleed_a

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

    def set_psu(self, state, voltage_val="0", current_val="1.0"):
        if state:
            self._psu_voltage = float(voltage_val)
        else:
            self._charging = False

    def set_load(self, state, current_val="0"):
        if state:
            was_on = self._load_current > 0
            # Discharge: no bleed path (PSU disconnected) → set load directly
            self._load_current = float(current_val)
            self._charging = False   # ดิสชาร์จ → หยุดจำลองชาร์จ
            if not was_on:
                self._load_on_t = time.monotonic()   # mark pulse start for RC model
        else:
            self.load_off()

    def set_load_raw(self, target):
        self._load_current = abs(float(target))

    def load_on(self):
        pass

    def load_off(self):
        # Pulse ending → seed the relaxation tail: the R1 overpotential built up
        # during the pulse now decays back toward OCV with τ (no ohmic, no current).
        if self._load_current > 0 and self._load_on_t is not None:
            t_pulse = time.monotonic() - self._load_on_t
            self._relax_v0 = self._load_current * self._mock_r1 * \
                (1.0 - math.exp(-t_pulse / self._mock_tau))
            self._load_off_t = time.monotonic()
        self._load_current = 0.0
        self._load_on_t = None

    def psu_off(self):
        self._psu_voltage = 0.0
        self._charging = False

    # ------------------------------------------------------------------
    # Measurement — จำลองแบตเตอรี่ลดแรงดันตามเวลา
    # ------------------------------------------------------------------

    def read_vi(self):
        """จำลองพฤติกรรมแพ็ค: ชาร์จ → แรงดันไต่ขึ้นจน CV แล้วกระแส taper;
        ดิสชาร์จ → แรงดันค่อยๆ ลด; idle → คงที่ (มี ripple เล็กน้อย)"""
        if self._charging and self._cccv_v:
            gap = self._cccv_v - self._sim_v
            if gap > 0.1:
                # Bulk (CC): ไต่แรงดันขึ้นด้วยกระแสคงที่
                self._sim_v = min(self._cccv_v, self._sim_v + 0.3)
                psu_i = self._charge_i
            else:
                # CV (absorption): แรงดันคงที่, กระแส taper ลงจนถึง tail
                self._sim_v = self._cccv_v
                self._charge_i = max(0.0, self._charge_i - 0.05)
                psu_i = self._charge_i
            load_i = 0.0
        elif self._load_current > 0:
            # Discharge: OCV drifts DOWN slowly with SoC (small, so it doesn't swamp the
            # RC transient during short HPPC pulses); terminal = OCV − I·R0 − I·R1·(1−e^(−t/τ))
            self._sim_v = max(9.5, self._sim_v - self._load_current * 1e-5)
            t_load = time.monotonic() - (self._load_on_t or time.monotonic())
            overpot = self._load_current * (
                self._mock_r0 + self._mock_r1 * (1.0 - math.exp(-t_load / self._mock_tau)))
            ripple = 0.002 * math.sin(time.time() - self._t_start)
            return round(self._sim_v - overpot + ripple, 4), 0.0, round(self._load_current, 4)
        else:
            # Rest: if a pulse just ended, replay the relaxation tail — terminal
            # voltage recovers toward OCV as V = OCV − V_R1·e^(−t_off/τ).
            ripple = 0.002 * math.sin(time.time() - self._t_start)
            if self._load_off_t is not None:
                t_off = time.monotonic() - self._load_off_t
                recovery = self._relax_v0 * math.exp(-t_off / self._mock_tau)
                return round(self._sim_v - recovery + ripple, 4), 0.0, 0.0
            return round(self._sim_v + ripple, 4), 0.0, 0.0
        ripple = 0.005 * math.sin(time.time() - self._t_start)
        return round(self._sim_v + ripple, 4), round(psu_i, 4), round(load_i, 4)

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

    def read_measurements(self, prefer_load_v=False):
        # Convention: discharge = positive. Mirrors HardwareController.read_measurements().
        v, psu_i, load_i = self.read_vi()
        if prefer_load_v:
            return v, load_i          # discharge: PSU disconnected, no bleed
        else:
            return v, -psu_i          # charge/idle: bleed inactive while OUTPUT ON

    def set_charge(self, state, current_val="0"):
        if state:
            self._psu_voltage = min(4.2, self._psu_voltage + 0.01)

    def set_psu_cccv(self, voltage, current):
        """จำลอง CC-CV charge: ตั้ง target + กระแส bulk ให้ read_vi ขับ state machine ได้"""
        self._cccv_v = float(voltage)
        if not self._charging:
            self._charge_i = float(current)   # bleed inactive during charge — no offset
        self._charging = True


class _MockInst:
    """จำลอง VISA instrument object (ใช้ใน profile loop ที่เรียก load_inst.write)"""

    def write(self, cmd):
        pass  # ไม่ทำอะไร

    def query(self, cmd):
        return "0.0"