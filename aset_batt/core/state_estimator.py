"""
Advanced State Estimation: SoC estimation ด้วย Coulomb counting + OCV correction
"""
import time
from aset_batt.core.battery_model import BatteryModel
import logging

logger = logging.getLogger(__name__)

class StateEstimator:
    """Adaptive & robust SoC estimator"""

    def __init__(self, rated_capacity: float, battery_model: BatteryModel = None):
        self.rated_capacity = rated_capacity  # Ah
        self.battery_model = battery_model or BatteryModel()

        # State variables
        self.soc = 50.0          # % (initial assumption)
        self.soc_initial = 50.0  # % ใช้เป็น reference ของ Coulomb counting
        self.soh = 100.0         # %
        self.rin = self.battery_model.base_rin  # Ohm

        # Coulomb counting
        self.ah_accumulated = 0.0   # Ah นับจาก initial SoC
        self.coulomb_efficiency = 0.99

        # OCV correction
        self.last_ocv_correction_time = time.time()
        self.ocv_correction_interval = 300  # วินาที (5 นาที)
        self.last_static_voltage = None
        self.standby_current = 0.6            # A — PSU quiescent draw even when OUTP OFF
        self.static_current_threshold = 0.15  # A — window around standby
        self._rested_s = 0.0                  # accumulated rest time (s) for endpoint anchor

        # Exponential smoothing
        self.alpha = 0.05
        self.soc_filtered = 50.0

        # ข้าม OCV correction เมื่ออยู่บน plateau ที่ flat (slope ต่ำ → SoC ill-conditioned)
        self.min_ocv_slope = 0.003  # V ต่อ %SoC (ต่อเซลล์)

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def init_from_voltage(self, voltage: float, temp: float = 25.0) -> None:
        """Initialize SoC จาก measured OCV (voltage) หลัง rest"""
        soc = self.battery_model.get_soc_from_ocv(voltage, temp)
        self._reset_to_soc(soc)
        logger.info(f"SoC initialized from voltage: {voltage:.3f}V -> {self.soc:.1f}%")

    def set_initial_soc(self, soc: float) -> None:
        """Set initial SoC ด้วยตนเอง"""
        self._reset_to_soc(max(0.0, min(100.0, soc)))
        logger.info(f"Initial SoC set to {self.soc:.1f}%")

    def sync_with_ocv(self, voltage: float, temp: float = 25.0) -> float:
        """Force synchronize SoC กับ OCV (ใช้หลัง rest period)"""
        soc = self.battery_model.get_soc_from_ocv(voltage, temp)
        self._reset_to_soc(soc)
        logger.info(f"SoC synced with OCV: {voltage:.3f}V -> {self.soc:.1f}%")
        return self.soc

    def _reset_to_soc(self, soc: float) -> None:
        """Reset state ทั้งหมดให้ตรงกับ soc ที่กำหนด"""
        self.soc = soc
        self.soc_initial = soc
        self.soc_filtered = soc
        self.ah_accumulated = 0.0
        self.last_ocv_correction_time = time.time()

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    def update(self, voltage: float, current: float, dt: float,
               temp: float = 25.0, measured_dcir: float = 0.0) -> dict:
        """
        Update state estimation

        Args:
            voltage: Terminal voltage (V)
            current: Net current (A, positive = discharge)
            dt: Time step (seconds)
            temp: Temperature (°C)
            measured_dcir: Measured DCIR จาก transient test (Ohm)

        Returns:
            dict: {soc, soh, rin, ah_accumulated}
        """

        # === 1. Coulomb Counting ===
        # current > 0 = discharge → SoC ลดลง ; current < 0 = charge → SoC เพิ่มขึ้น
        # coulombic efficiency ใช้กับ "charge" เท่านั้น (ประจุที่ใส่เข้าไม่ได้เก็บหมด);
        # ตอน discharge ประจุที่จ่ายออกนับเต็ม
        dah = current * (dt / 3600.0)
        if current < 0:  # charge
            dah *= self.coulomb_efficiency
        self.ah_accumulated += dah

        # ใช้ soc_initial เป็นฐาน ไม่ hardcode 50%
        # ลบ ah_accumulated เพราะ discharge (positive current) ทำให้ SoC ลดลง
        soc_cc = self.soc_initial - (self.ah_accumulated / self.rated_capacity) * 100.0
        soc_cc = max(0.0, min(100.0, soc_cc))

        # === 1b. Endpoint Anchors (SoC Restoring Points) ===
        # Hard reset ที่ขอบบน/ล่าง — แก้ Coulomb counting drift บน flat LFP plateau
        # ทำงานระหว่าง active charge/discharge (ต่างจาก OCV correction ที่ต้องการกระแส ≈ 0)
        cp = self.battery_model.charge_profile
        s  = self.battery_model.series_cells
        # 100% anchor: ชาร์จ + V ≥ 98.6% ของ CV (~3.60V/cell × 8 = 28.8V สำหรับ LFP 8S)
        #              AND กระแสชาร์จ taper ถึง tail threshold (แบตใกล้เต็มแล้ว)
        full_v_cell = cp.cv_voltage_per_cell or cp.absorption_voltage_per_cell
        if full_v_cell > 0:
            anchor_v_full = full_v_cell * s * 0.986      # 3.65 × 0.986 × 8 = 28.8V (LFP 8S)
            anchor_i_tail = self.rated_capacity * cp.tail_current_c_rate * 1.2
            if (current < 0 and voltage >= anchor_v_full
                    and abs(current) <= anchor_i_tail and self.soc < 98.0):
                logger.info("Endpoint anchor → 100%%: %.3fV (≥%.3f) I=%.3fA tail=%.3fA",
                            voltage, anchor_v_full, current, anchor_i_tail)
                self._reset_to_soc(100.0)
                soc_cc = 100.0
        # 0% anchor: discharge + V ≤ OCV ที่ 0% ของแพ็ค (+ 1% hysteresis)
        #            2.50V/cell × 8 = 20.0V สำหรับ LFP 8S
        anchor_v_empty = self.battery_model.get_ocv_from_soc(0.0)
        if (current > 0 and voltage <= anchor_v_empty * 1.01 and self.soc > 2.0):
            logger.info("Endpoint anchor → 0%%: %.3fV (≤%.3f)", voltage, anchor_v_empty)
            self._reset_to_soc(0.0)
            soc_cc = 0.0

        # === 2. Update Internal Resistance (forward temp + measured_dcir ให้ถูก) ===
        self.rin = self.battery_model.estimate_rin(
            voltage, current, self.soc, temp=temp, measured_dcir=measured_dcir
        )

        # === 3. OCV-Based Correction + ENDPOINT RESET (เมื่อกระแสน้อย) ===
        # หลักการ (จาก literature ของ LFP): coulomb counting drift ได้ → ต้อง re-anchor
        # ด้วย OCV "เฉพาะตรงที่ OCV เชื่อถือได้" คือบริเวณ knee/ปลาย (slope ชัน) หลัง full
        # charge / full discharge. ตรง plateau ที่ flat (slope ต่ำ) ห้ามแก้ (V คลาดนิด SoC
        # เพี้ยนมาก). ปลายที่ steep → anchor ทันที (ไม่รอ 300s) และ re-anchor coulomb counter.
        now = time.time()
        if abs(current - self.standby_current) < self.static_current_threshold:
            self._rested_s += dt
            self.last_static_voltage = voltage
            ocv_voltage = voltage + self.standby_current * self.rin
            ocv_soc = self.battery_model.get_soc_from_ocv(ocv_voltage, temp)
            slope = self.battery_model.ocv_slope(ocv_soc, temp)
            drift = abs(self.soc_filtered - ocv_soc)
            steep = slope >= 2.0 * self.min_ocv_slope          # ปลาย/knee ที่ OCV เชื่อได้มาก
            periodic = (now - self.last_ocv_correction_time) >= self.ocv_correction_interval
            # เงื่อนไข: พักนานพอ (กัน transient) + ไม่ใช่ plateau แบน + (อยู่ปลาย หรือ ถึงรอบ+drift)
            if (self._rested_s >= 5.0 and slope >= self.min_ocv_slope
                    and (steep or (periodic and drift > 3.0))):
                w = 0.9 if steep else 0.8                       # ปลาย anchor หนักกว่า
                corrected = w * ocv_soc + (1.0 - w) * soc_cc
                logger.info("OCV %s: CC=%.1f%% OCV=%.1f%% slope=%.4f -> %.1f%%",
                            "endpoint-reset" if steep else "correction",
                            soc_cc, ocv_soc, slope, corrected)
                self.soc_filtered = corrected
                # re-anchor coulomb counting to the corrected SoC (ไม่งั้น smoothing ดึงกลับ)
                self.soc_initial = corrected
                self.ah_accumulated = 0.0
                soc_cc = corrected
                self.last_ocv_correction_time = now
                self._rested_s = 0.0
        else:
            self.last_static_voltage = None
            self._rested_s = 0.0

        # === 4. Exponential Smoothing ===
        self.soc_filtered = (1 - self.alpha) * self.soc_filtered + self.alpha * soc_cc
        self.soc = max(0.0, min(100.0, self.soc_filtered))

        return {
            "soc": self.soc,
            "soh": self.soh,
            "rin": self.rin,
            "ah_accumulated": self.ah_accumulated
        }

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        return {
            "soc": self.soc,
            "soh": self.soh,
            "rin": self.rin,
            "ah_accumulated": self.ah_accumulated,
            "coulomb_efficiency": self.coulomb_efficiency
        }