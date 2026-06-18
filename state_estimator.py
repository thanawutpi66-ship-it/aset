"""
Advanced State Estimation: SoC estimation ด้วย Coulomb counting + OCV correction
"""
import time
from battery_model import BatteryModel
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
        self.static_current_threshold = 0.1  # A

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

        # === 2. Update Internal Resistance (forward temp + measured_dcir ให้ถูก) ===
        self.rin = self.battery_model.estimate_rin(
            voltage, current, self.soc, temp=temp, measured_dcir=measured_dcir
        )

        # === 3. OCV-Based Correction (Periodic, เมื่อกระแสน้อย) ===
        now = time.time()
        if abs(current) < self.static_current_threshold:
            if self.last_static_voltage is not None:
                time_since_correction = now - self.last_ocv_correction_time
                if time_since_correction >= self.ocv_correction_interval:
                    ocv_soc = self.battery_model.get_soc_from_ocv(voltage, temp)
                    drift = abs(self.soc_filtered - ocv_soc)
                    # guard: ข้ามถ้าอยู่บน plateau ที่ flat (slope ต่ำ → V คลาดนิดเดียว SoC เพี้ยนมาก)
                    slope = self.battery_model.ocv_slope(ocv_soc, temp)
                    if slope < self.min_ocv_slope:
                        logger.debug(
                            "ข้าม OCV correction: plateau flat (slope=%.4f V/%% < %.4f)",
                            slope, self.min_ocv_slope
                        )
                    elif drift > 3.0:
                        # Blend 80% OCV, 20% Coulomb
                        corrected = 0.8 * ocv_soc + 0.2 * soc_cc
                        logger.info(
                            f"OCV Correction: CC={soc_cc:.1f}% -> "
                            f"OCV={ocv_soc:.1f}% -> Blended={corrected:.1f}%"
                        )
                        # อัปเดต filtered ตรงๆ เพื่อ avoid lag
                        self.soc_filtered = corrected
                        self.last_ocv_correction_time = now
            self.last_static_voltage = voltage
        else:
            self.last_static_voltage = None

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