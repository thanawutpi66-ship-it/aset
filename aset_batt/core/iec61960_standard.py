"""
IEC 61960 Test Profiles and Procedures for LiPO Battery Testing
ตามมาตรฐาน IEC 61960 สำหรับ secondary lithium cells and batteries
"""
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class TestType(Enum):
    """ประเภทการทดสอบตาม IEC 61960"""
    CAPACITY_MEASUREMENT = "capacity_measurement"
    ENERGY_DENSITY = "energy_density"
    INTERNAL_RESISTANCE = "internal_resistance"
    CYCLE_LIFE = "cycle_life"
    SAFETY_TEST = "safety_test"
    PERFORMANCE_TEST = "performance_test"
    ENVIRONMENTAL_TEST = "environmental_test"

class DischargeRate(Enum):
    """อัตราการ discharge ตาม IEC 61960"""
    C_02 = 0.2  # 0.2C สำหรับ rated capacity
    C_05 = 0.5  # 0.5C สำหรับ energy density
    C_1 = 1.0   # 1C สำหรับ cycle life
    C_2 = 2.0   # 2C สำหรับ high rate discharge
    C_5 = 5.0   # 5C สำหรับ power capability

@dataclass
class IEC61960TestProfile:
    """Test profile ตาม IEC 61960"""
    test_type: TestType
    name: str
    description: str
    duration_hours: float
    temperature: float = 25.0  # °C
    discharge_rate: Optional[DischargeRate] = None
    charge_rate: Optional[float] = None  # C-rate
    rest_time_minutes: float = 1.0
    cycles: int = 1
    safety_limits: Dict[str, float] = None

    def __post_init__(self):
        if self.safety_limits is None:
            self.safety_limits = {
                "max_voltage": 4.35,  # LiPO max voltage
                "min_voltage": 2.75,  # LiPO min voltage
                "max_current": 5.0,   # Max discharge current
                "max_temperature": 60.0,
                "min_temperature": 0.0
            }

class IEC61960Standard:
    """IEC 61960 Standard Implementation สำหรับ LiPO Battery Testing"""

    def __init__(self, battery_capacity_ah: float = 2.0,
                 battery_type: str = "Li-ion",
                 nominal_voltage: float = 3.7):
        self.battery_capacity_ah = battery_capacity_ah  # Rated capacity
        self.battery_type = battery_type
        self.nominal_voltage = nominal_voltage
        self.test_profiles = self._create_standard_profiles()
        self.test_results = {}

    def _create_standard_profiles(self) -> Dict[str, IEC61960TestProfile]:
        """สร้าง test profiles มาตรฐานตาม IEC 61960"""

        profiles = {}

        # 1. Capacity Measurement Test (Clause 6.2)
        # Discharge time at a C-rate is 1/C hours (independent of capacity):
        #   t = capacity / current = capacity / (C·capacity) = 1/C.
        # The previous capacity/C form was dimensionally wrong (off by a factor of
        # capacity_ah — e.g. 7 Ah at 0.2C gave 35 h instead of 5 h).
        profiles["capacity_02c"] = IEC61960TestProfile(
            test_type=TestType.CAPACITY_MEASUREMENT,
            name="Rated Capacity (0.2C)",
            description="Measure rated capacity at 0.2C discharge rate",
            duration_hours=1.0 / 0.2,            # 5 h
            discharge_rate=DischargeRate.C_02,
            temperature=25.0
        )

        profiles["capacity_1c"] = IEC61960TestProfile(
            test_type=TestType.CAPACITY_MEASUREMENT,
            name="Capacity at 1C",
            description="Measure capacity at 1C discharge rate",
            duration_hours=1.0 / 1.0,            # 1 h
            discharge_rate=DischargeRate.C_1,
            temperature=25.0
        )

        # 2. Energy Density Test (Clause 6.3)
        profiles["energy_density"] = IEC61960TestProfile(
            test_type=TestType.ENERGY_DENSITY,
            name="Energy Density Measurement",
            description="Measure energy density at 0.5C discharge rate",
            duration_hours=1.0 / 0.5,            # 2 h
            discharge_rate=DischargeRate.C_05,
            temperature=25.0
        )

        # 3. Internal Resistance Test (Clause 6.4)
        profiles["internal_resistance"] = IEC61960TestProfile(
            test_type=TestType.INTERNAL_RESISTANCE,
            name="Internal Resistance Measurement",
            description="Measure DC internal resistance",
            duration_hours=0.1,  # Quick test
            temperature=25.0
        )

        # 4. Cycle Life Test (Clause 6.5)
        profiles["cycle_life_300"] = IEC61960TestProfile(
            test_type=TestType.CYCLE_LIFE,
            name="Cycle Life Test (300 cycles)",
            description="Charge-discharge cycles for life assessment",
            # per cycle: charge (1/C h) + discharge (1/C h) + 0.5 h rest, ×300
            duration_hours=(1.0 / 1.0 * 2 + 0.5) * 300,
            discharge_rate=DischargeRate.C_1,
            charge_rate=1.0,
            cycles=300,
            temperature=25.0
        )

        # 5. Temperature-performance tests (0 / 25 / 45 °C) are intentionally NOT
        # offered: the bench has no thermal chamber (single ambient ~25 °C), so a
        # temperature sweep is not reproducible here. Capacity is characterised at
        # ambient only. See docs/project_pivot.md.

        # 6. Safety Tests (Clause 7)
        profiles["safety_overcharge"] = IEC61960TestProfile(
            test_type=TestType.SAFETY_TEST,
            name="Overcharge Protection Test",
            description="Test overcharge protection mechanism",
            duration_hours=2.0,
            temperature=25.0,
            safety_limits={"max_voltage": 4.5, "max_current": 2.0}
        )

        return profiles

    def get_test_profile(self, profile_name: str) -> Optional[IEC61960TestProfile]:
        """ดึง test profile ตามชื่อ"""
        return self.test_profiles.get(profile_name)

    def get_available_tests(self) -> List[str]:
        """ส่งรายชื่อ test ที่มีให้เลือก"""
        return list(self.test_profiles.keys())

    def calculate_capacity(self, voltage_data: List[float], current_data: List[float],
                          time_data: List[float]) -> Dict[str, float]:
        """
        คำนวณ capacity ตาม IEC 61960
        Capacity = ∫ I dt (Ah)
        """
        if len(voltage_data) != len(current_data) or len(current_data) != len(time_data):
            raise ValueError("Data arrays must have same length")

        # คำนวณ capacity โดย integration
        capacity_ah = 0.0
        energy_wh = 0.0

        for i in range(1, len(time_data)):
            dt = (time_data[i] - time_data[i-1]) / 3600.0  # Convert to hours
            avg_current = (current_data[i] + current_data[i-1]) / 2
            avg_voltage = (voltage_data[i] + voltage_data[i-1]) / 2

            capacity_ah += abs(avg_current) * dt
            energy_wh += abs(avg_current) * avg_voltage * dt

        return {
            "capacity_ah": capacity_ah,
            "energy_wh": energy_wh,
            # Empty on an abort before the first sample — guard the same way
            # discharge_time_hours already does below, instead of a
            # ZeroDivisionError crashing the abort-results step.
            "average_voltage": sum(voltage_data) / len(voltage_data) if voltage_data else 0.0,
            "discharge_time_hours": time_data[-1] / 3600.0 if time_data else 0
        }

    def calculate_energy_density(self, capacity_ah: float, mass_g: float,
                                 avg_voltage: Optional[float] = None) -> Dict[str, float]:
        """
        คำนวณ energy density ตาม IEC 61960
        Energy Density = Energy / Mass (Wh/kg)

        ใช้ avg_voltage จริงจากการ discharge ถ้ามี ไม่งั้น fallback เป็น nominal voltage
        ของ chemistry ที่ตั้งไว้ (ไม่ hardcode 3.7V อีกต่อไป)
        """
        v = avg_voltage if (avg_voltage and avg_voltage > 0) else self.nominal_voltage
        energy_wh = capacity_ah * v

        return {
            "gravimetric_energy_density_wh_kg": energy_wh / (mass_g / 1000) if mass_g > 0 else 0,
            "volumetric_energy_density_wh_l": 0.0,  # Would need volume data
            "total_energy_wh": energy_wh
        }

    def calculate_dcir_two_pulse(self, v1: float, i1: float,
                                 v2: float, i2: float) -> Dict[str, float]:
        """
        DC internal resistance ตาม IEC 61960 Clause 6.4 — **two-pulse method** (ถูกต้องตามมาตรฐาน)
            R = (V1 - V2) / (I2 - I1)
        โดย (V1, I1) วัดหลัง discharge 0.2C นาน 10s, (V2, I2) หลัง 1C นาน 1s
        การใช้ผลต่างของ "สองกระแส" ตัด OCV ออกไป → ลด bias จาก polarization/relaxation
        (ต่างจากวิธี single-pulse V_before−V_after ที่เทียบกับ OCV)
        """
        di = i2 - i1
        if abs(di) < 1e-6:
            logger.warning("DCIR two-pulse: ΔI เล็กเกินไป")
            return {"dcir_mohm": 0.0, "valid_measurement": False}

        dcir_ohm = abs((v1 - v2) / di)
        dcir_mohm = dcir_ohm * 1000.0
        return {
            "dcir_mohm": dcir_mohm,
            "acir_mohm": dcir_mohm * 0.8,  # ACIR ~ ohmic-only < DCIR (approx)
            "v1_v": v1, "i1_a": i1, "v2_v": v2, "i2_a": i2,
            "valid_measurement": True,
            "iec61960_compliant": True,
        }



    def assess_cycle_life(self, capacity_fade_data: List[float]) -> Dict[str, float]:
        """
        ประเมิน cycle life ตาม IEC 61960
        End of life = 80% of initial capacity
        """
        if not capacity_fade_data:
            return {"cycles_to_80_percent": 0, "capacity_fade_rate": 0}

        initial_capacity = capacity_fade_data[0]
        target_capacity = initial_capacity * 0.8

        # หา cycle ที่ capacity ตกลงถึง 80%
        cycles_to_80 = len(capacity_fade_data)
        for i, cap in enumerate(capacity_fade_data):
            if cap <= target_capacity:
                cycles_to_80 = i + 1
                break

        # คำนวณ fade rate (% per cycle)
        if len(capacity_fade_data) > 1:
            fade_rate = (capacity_fade_data[0] - capacity_fade_data[-1]) / (len(capacity_fade_data) - 1) / capacity_fade_data[0] * 100
        else:
            fade_rate = 0

        return {
            "cycles_to_80_percent": cycles_to_80,
            "capacity_fade_rate_percent_per_cycle": fade_rate,
            "remaining_capacity_percent": (capacity_fade_data[-1] / initial_capacity) * 100
        }

    def generate_test_report(self, test_name: str, results: Dict[str, Any]) -> str:
        """
        สร้าง test report ตาม IEC 61960 format
        """
        profile = self.get_test_profile(test_name)
        if not profile:
            return "Test profile not found"

        report = f"""
IEC 61960 TEST REPORT
=====================

Test: {profile.name}
Description: {profile.description}
Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Battery Type: {self.battery_type}
Rated Capacity: {self.battery_capacity_ah} Ah

TEST CONDITIONS:
- Temperature: {profile.temperature}°C
- Discharge Rate: {profile.discharge_rate.value if profile.discharge_rate else 'N/A'}C
- Test Duration: {profile.duration_hours:.1f} hours

RESULTS:
"""

        for key, value in results.items():
            if isinstance(value, float):
                report += f"- {key}: {value:.3f}\n"
            else:
                report += f"- {key}: {value}\n"

        # อย่าฟันธง PASSED เสมอ — อิงผลจริงจาก results['iec61960_compliant'] ถ้ามี
        compliant = results.get("iec61960_compliant")
        if compliant is True:
            report += "\nCOMPLIANCE: PASSED (IEC 61960)\n"
        elif compliant is False:
            report += "\nCOMPLIANCE: FAILED (IEC 61960)\n"
        else:
            report += "\nCOMPLIANCE: NOT EVALUATED (no compliance flag in results)\n"
        return report

    def validate_test_conditions(self, profile: IEC61960TestProfile,
                               actual_conditions: Dict[str, float]) -> List[str]:
        """
        ตรวจสอบว่าการทดสอบเป็นไปตาม IEC 61960 หรือไม่
        """
        violations = []
        limits = profile.safety_limits or {}

        # ตรวจสอบ temperature tolerance (±2°C)
        temp = actual_conditions.get('temperature', profile.temperature)
        if abs(temp - profile.temperature) > 2:
            violations.append(
                f"Temperature {temp:.1f}°C deviates >2°C from target "
                f"{profile.temperature:.1f}°C"
            )

        # ตรวจสอบ voltage limits (ใช้ .get เพื่อไม่ให้ KeyError เมื่อ profile ไม่ระบุ limit)
        max_v = actual_conditions.get('max_voltage')
        max_v_limit = limits.get('max_voltage')
        if max_v is not None and max_v_limit is not None and max_v > max_v_limit:
            violations.append(
                f"Max voltage {max_v:.2f}V exceeds limit {max_v_limit:.2f}V"
            )

        min_v = actual_conditions.get('min_voltage')
        min_v_limit = limits.get('min_voltage')
        if min_v is not None and min_v_limit is not None and min_v < min_v_limit:
            violations.append(
                f"Min voltage {min_v:.2f}V below limit {min_v_limit:.2f}V"
            )

        # ตรวจสอบ current limits
        cur = actual_conditions.get('current')
        max_i_limit = limits.get('max_current')
        if cur is not None and max_i_limit is not None and abs(cur) > max_i_limit:
            violations.append(
                f"Current {abs(cur):.1f}A exceeds limit {max_i_limit:.1f}A"
            )

        return violations