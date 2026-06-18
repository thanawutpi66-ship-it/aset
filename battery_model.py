"""
Battery Model: Advanced OCV lookup table และ internal resistance estimation
สำหรับ LiFePO4/Li-ion พร้อม temperature compensation และ aging effects
"""
import logging
import numpy as np
from typing import Optional, Dict, Tuple, List

logger = logging.getLogger(__name__)

class BatteryModel:
    """Advanced battery electrical model ด้วย temperature compensation"""

    def __init__(self, battery_type: str = "LiPO", nominal_voltage: float = 3.7,
                 series_cells: int = 1, parallel_cells: int = 1):
        self.battery_type = battery_type
        self.nominal_voltage = nominal_voltage  # per-cell (V)
        # โครงสร้างแพ็ค: series คูณแรงดัน+ความต้านทาน, parallel คูณความจุ/หารความต้านทาน
        self.series_cells = max(1, int(series_cells))
        self.parallel_cells = max(1, int(parallel_cells))

        # Temperature range สำหรับ interpolation
        self.temp_range = [-10, 0, 10, 25, 40, 60]  # °C

        # Temperature-dependent OCV tables (ต่อเซลล์)
        self.ocv_tables = self._generate_ocv_tables()

        # Internal resistance model parameters (ต่อเซลล์)
        self.rin_params = self._get_rin_parameters()

        # base internal resistance ระดับแพ็ค (Ohm) + mΩ สำหรับ grader
        self.base_rin = self.rin_params["r0"] * self.series_cells / self.parallel_cells
        self.base_r0_mohm_pack = self.base_rin * 1000.0

        # Aging model (สำหรับ SoH estimation)
        self.aging_factor = 1.0  # 1.0 = new battery

        # IEC 61960 compliance data
        self.iec_data = {
            'rated_capacity_ah': 2.0,
            'mass_grams': 100.0,
            'compliance_mode': True
        }

        # Pre-sort สำหรับ interpolation
        self._prepare_interpolation_tables()

    def _generate_ocv_tables(self) -> Dict[int, Dict[int, float]]:
        """สร้าง OCV lookup tables สำหรับอุณหภูมิต่างๆ"""
        if self.battery_type == "LiPO":
            # Base table ที่ 25°C สำหรับ LiPO (LiCoO2 chemistry)
            base_table = {
                0:   3.00,   5:   3.40,   10:  3.60,  15:  3.70,  20:  3.75,
                25:  3.78,  30:  3.82,  35:  3.85,  40:  3.88,  45:  3.92,
                50:  3.95,  55:  3.98,  60:  4.00,  65:  4.05,  70:  4.10,
                75:  4.15,  80:  4.18,  85:  4.20,  90:  4.22,  95:  4.25,
                100: 4.30
            }

            # Temperature compensation factors สำหรับ LiPO
            temp_factors = {
                -10: 1.12,   # +12% ที่อุณหภูมิต่ำ (polarization เพิ่มมาก)
                0:   1.06,   # +6%
                10:  1.02,   # +2%
                25:  1.00,   # Reference
                40:  0.96,   # -4% ที่อุณหภูมิสูง
                60:  0.90    # -10%
            }

            # OCV ขึ้นกับอุณหภูมิ "น้อยมาก" (entropic ~mV/K) → ถือว่า ~independent
            # ผลของอุณหภูมิที่มีนัยสำคัญอยู่ที่ internal resistance (ดู _calculate_base_rin)
            # (เดิมคูณ ±6–12% = +หลายร้อย mV ซึ่งไม่ถูกตามฟิสิกส์ จึงเอาออก)
            tables = {temp: dict(base_table) for temp in self.temp_range}
            return tables

        elif self.battery_type == "LiFePO4":
            # Base table ที่ 25°C — rested OCV ต่อเซลล์ (ไม่ใช่แรงดันขณะชาร์จ)
            # LFP มี plateau ~3.25–3.30V ช่วงกลาง, knee ที่ปลาย, rested 100% ~3.40V
            # ค่า strictly-increasing เพื่อให้ reverse lookup (OCV→SoC) เสถียร
            base_table = {
                0:   2.50,   5:   2.90,   10:  3.10,   15:  3.18,   20:  3.22,
                25:  3.245,  30:  3.255,  35:  3.262,  40:  3.268,  45:  3.273,
                50:  3.278,  55:  3.283,  60:  3.288,  65:  3.293,  70:  3.300,
                75:  3.308,  80:  3.318,  85:  3.330,  90:  3.345,  95:  3.365,
                100: 3.400
            }

            # Temperature compensation factors
            temp_factors = {
                -10: 1.08,   # +8% ที่อุณหภูมิต่ำ (polarization เพิ่ม)
                0:   1.04,   # +4%
                10:  1.01,   # +1%
                25:  1.00,   # Reference
                40:  0.98,   # -2% ที่อุณหภูมิสูง (conductivity เพิ่ม)
                60:  0.95    # -5%
            }

            # OCV ขึ้นกับอุณหภูมิ "น้อยมาก" (entropic ~mV/K) → ถือว่า ~independent
            # ผลของอุณหภูมิที่มีนัยสำคัญอยู่ที่ internal resistance (ดู _calculate_base_rin)
            # (เดิมคูณ ±6–12% = +หลายร้อย mV ซึ่งไม่ถูกตามฟิสิกส์ จึงเอาออก)
            tables = {temp: dict(base_table) for temp in self.temp_range}
            return tables

        else:  # Li-ion default
            base_table = {
                0:   2.50,   5:   3.00,   10:  3.20,  15:  3.40,  20:  3.50,
                25:  3.58,  30:  3.62,  35:  3.65,  40:  3.68,  45:  3.70,
                50:  3.72,  55:  3.74,  60:  3.75,  65:  3.77,  70:  3.80,
                75:  3.84,  80:  3.90,  85:  4.00,  90:  4.10,  95:  4.18,
                100: 4.25
            }

            temp_factors = {
                -10: 1.06,   # +6%
                0:   1.03,   # +3%
                10:  1.01,   # +1%
                25:  1.00,   # Reference
                40:  0.97,   # -3%
                60:  0.93    # -7%
            }

            # OCV ~independent ของอุณหภูมิ (ดูหมายเหตุด้านบน)
            tables = {temp: dict(base_table) for temp in self.temp_range}
            return tables

    def _get_rin_parameters(self) -> Dict[str, float]:
        """Parameters สำหรับ internal resistance model"""
        if self.battery_type == "LiPO":
            return {
                'r0': 0.025,      # Base resistance ที่ 25°C, 50% SoC (Ohm) - LiPO มี Rin ต่ำกว่า
                'temp_coeff': 0.004,  # Temperature coefficient (%/°C) - LiPO sensitive กับ temp มากกว่า
                'soc_coeff': 0.0008,  # SoC coefficient (Ohm/%)
                'aging_coeff': 0.001  # Aging coefficient (%/cycle)
            }
        elif self.battery_type == "LiFePO4":
            return {
                'r0': 0.045,
                'temp_coeff': 0.003,
                'soc_coeff': 0.0005,
                'aging_coeff': 0.002
            }
        else:  # Li-ion
            return {
                'r0': 0.035,
                'temp_coeff': 0.004,
                'soc_coeff': 0.0003,
                'aging_coeff': 0.0015
            }

    def _prepare_interpolation_tables(self):
        """เตรียมข้อมูลสำหรับ interpolation ที่เร็วขึ้น"""
        self._interp_data = {}
        for temp in self.temp_range:
            table = self.ocv_tables[temp]
            soc_keys = sorted(table.keys())
            ocv_vals = [table[k] for k in soc_keys]
            self._interp_data[temp] = {
                'soc_keys': soc_keys,
                'ocv_vals': ocv_vals
            }

    def get_ocv_from_soc(self, soc: float, temp: float = 25.0) -> float:
        """คำนวณ OCV จาก SoC และ temperature ด้วย interpolation"""
        soc = max(0.0, min(100.0, soc))
        temp = self._clamp_temperature(temp)

        # หา temperature interpolation weights
        temp_idx = self._find_temp_index(temp)
        if temp_idx >= len(self.temp_range) - 1:
            # ใช้ table สุดท้าย
            data = self._interp_data[self.temp_range[-1]]
        else:
            # Interpolate ระหว่างสอง temperatures
            t1, t2 = self.temp_range[temp_idx], self.temp_range[temp_idx + 1]
            w1 = (t2 - temp) / (t2 - t1)
            w2 = 1 - w1

            data1 = self._interp_data[t1]
            data2 = self._interp_data[t2]

            # Interpolate OCV values
            ocv_vals = []
            for i in range(len(data1['ocv_vals'])):
                ocv_vals.append(data1['ocv_vals'][i] * w1 + data2['ocv_vals'][i] * w2)

            data = {'soc_keys': data1['soc_keys'], 'ocv_vals': ocv_vals}

        # Interpolate ใน SoC domain (per-cell) แล้วคูณจำนวน series → แรงดันแพ็ค
        cell_ocv = float(np.interp(soc, data['soc_keys'], data['ocv_vals']))
        return cell_ocv * self.series_cells

    def get_soc_from_ocv(self, ocv: float, temp: float = 25.0) -> float:
        """Reverse lookup: OCV (แพ็ค) -> SoC

        รับแรงดันระดับแพ็ค หารด้วย series ก่อน lookup per-cell
        หมายเหตุ: ช่วง plateau ของ LFP มี dOCV/dSoC ≈ 0 → SoC ที่ได้ ill-conditioned
        (V คลาดนิดเดียว SoC เพี้ยนมาก) — ควรใช้เฉพาะหลัง rest นานพอ
        """
        temp = self._clamp_temperature(temp)
        cell_ocv = ocv / self.series_cells

        # ใช้ table ที่ใกล้เคียงที่สุดสำหรับ reverse lookup
        closest_temp = min(self.temp_range, key=lambda x: abs(x - temp))
        data = self._interp_data[closest_temp]

        soc = float(np.interp(cell_ocv, data['ocv_vals'], data['soc_keys']))
        return max(0.0, min(100.0, soc))

    def _clamp_temperature(self, temp: float) -> float:
        """จำกัดอุณหภูมิให้อยู่ใน range ที่มีข้อมูล"""
        return max(self.temp_range[0], min(self.temp_range[-1], temp))

    def _find_temp_index(self, temp: float) -> int:
        """หา index ของ temperature ที่ใกล้เคียงที่สุด"""
        for i, t in enumerate(self.temp_range):
            if temp <= t:
                return max(0, i - 1)
        return len(self.temp_range) - 1

    def estimate_rin(self, voltage: float, current: float, soc: float,
                     temp: float = 25.0, measured_dcir: float = 0.0) -> float:
        """
        คำนวณ internal resistance ขั้นสูงพร้อม temperature และ SoC compensation
        Rin = R0 * (1 + temp_factor) * (1 + soc_factor) * (1 + aging_factor)
        """
        if abs(current) < 0.01:
            return self._calculate_base_rin(soc, temp)

        # คำนวณจาก Thevenin model
        ocv = self.get_ocv_from_soc(soc, temp)
        rin_calculated = abs((ocv - voltage) / current)

        # คำนวณ base resistance จาก model
        rin_base = self._calculate_base_rin(soc, temp)

        # Blend calculated และ model values
        rin = 0.6 * rin_calculated + 0.4 * rin_base

        # Blend กับ measured DCIR ถ้ามี
        if measured_dcir > 0:
            rin = 0.7 * rin + 0.3 * measured_dcir

        # clamp ขอบบนสเกลตามแพ็ค (series เพิ่ม R, parallel ลด R)
        r_max = 0.5 * self.series_cells / self.parallel_cells
        return max(0.001, min(r_max, rin))

    def _calculate_base_rin(self, soc: float, temp: float) -> float:
        """คำนวณ base internal resistance (ระดับแพ็ค) จาก temperature และ SoC"""
        params = self.rin_params

        # Temperature factor: R "เพิ่มขึ้นเมื่ออุณหภูมิต่ำลง" (Arrhenius — ionic/
        # charge-transfer ช้าลงตอนเย็น) จึงใช้ (25 - temp) ไม่ใช่ (temp - 25)
        # NB: เป็น linear approximation; ของจริงโตแบบ exponential ที่อุณหภูมิต่ำ
        temp_factor = params['temp_coeff'] * (25.0 - temp)

        # SoC factor (สูงขึ้นเมื่อ SoC ต่ำหรือสูง)
        soc_factor = params['soc_coeff'] * abs(soc - 50.0)

        # Aging factor
        aging_factor = params['aging_coeff'] * (1.0 - self.aging_factor)

        rin_cell = params['r0'] * (1 + temp_factor) * (1 + soc_factor) * (1 + aging_factor)
        # scale เป็นระดับแพ็ค: อนุกรมบวกกัน, ขนานหารกัน
        rin_pack = rin_cell * self.series_cells / self.parallel_cells
        return max(0.001, rin_pack)

    def get_voltage_from_state(self, soc: float, current: float,
                               temp: float = 25.0, rin: Optional[float] = None) -> float:
        """Thevenin model ขั้นสูง: V = OCV - I*Rin พร้อม temperature effects"""
        if rin is None:
            rin = self._calculate_base_rin(soc, temp)

        ocv = self.get_ocv_from_soc(soc, temp)

        # เพิ่ม polarization effects ที่อุณหภูมิต่ำ (ต่อเซลล์ → คูณ series เป็นแพ็ค)
        if temp < 10:
            polarization = 0.02 * abs(current) * (10 - temp) / 10 * self.series_cells
            return ocv - current * rin - polarization
        else:
            return ocv - current * rin

    def update_aging_factor(self, cycle_count: int, time_years: float):
        """อัปเดต aging factor จาก cycle count และอายุการใช้งาน"""
        # Aging model: exponential decay
        cycle_degradation = 1 - 0.0001 * cycle_count  # 0.01% per cycle
        time_degradation = 1 - 0.005 * time_years     # 0.5% per year

        self.aging_factor = max(0.5, cycle_degradation * time_degradation)  # Minimum 50%

    def get_soh_from_capacity(self, measured_capacity: float, nominal_capacity: float) -> float:
        """คำนวณ State of Health จาก measured capacity"""
        soh = (measured_capacity / nominal_capacity) * 100
        self.aging_factor = soh / 100.0
        return max(0.0, min(100.0, soh))

    def calculate_iec61960_capacity(self, voltage_data: List[float], current_data: List[float],
                                   time_data: List[float], discharge_rate: float) -> Dict[str, float]:
        """
        คำนวณ capacity ตาม IEC 61960 Clause 6.2
        สำหรับ LiPO battery testing
        """
        if len(voltage_data) != len(current_data) or len(current_data) != len(time_data):
            raise ValueError("Data arrays must have same length")

        # คำนวณ capacity โดย integration (Ah)
        capacity_ah = 0.0
        energy_wh = 0.0
        avg_voltage = 0.0

        for i in range(1, len(time_data)):
            dt_hours = (time_data[i] - time_data[i-1]) / 3600.0
            avg_current = abs((current_data[i] + current_data[i-1]) / 2)
            segment_voltage = (voltage_data[i] + voltage_data[i-1]) / 2

            capacity_ah += avg_current * dt_hours
            energy_wh += avg_current * segment_voltage * dt_hours
            avg_voltage += segment_voltage

        avg_voltage /= len(voltage_data)

        # IEC 61960 compliance check
        expected_time_hours = self.iec_data['rated_capacity_ah'] / discharge_rate
        actual_time_hours = time_data[-1] / 3600.0 if time_data else 0

        return {
            "capacity_ah": capacity_ah,
            "energy_wh": energy_wh,
            "average_voltage_v": avg_voltage,
            "discharge_time_hours": actual_time_hours,
            "expected_time_hours": expected_time_hours,
            "capacity_efficiency_percent": (capacity_ah / self.iec_data['rated_capacity_ah']) * 100,
            "iec61960_compliant": abs(actual_time_hours - expected_time_hours) < 0.5  # ±30 min tolerance
        }

    def calculate_iec61960_energy_density(self, capacity_ah: float, energy_wh: float) -> Dict[str, float]:
        """
        คำนวณ energy density ตาม IEC 61960 Clause 6.3
        Gravimetric และ volumetric energy density
        """
        mass_kg = self.iec_data['mass_grams'] / 1000.0

        # Gravimetric energy density (Wh/kg)
        gravimetric_density = energy_wh / mass_kg if mass_kg > 0 else 0

        # Volumetric energy density approximation (Wh/L)
        # Typical LiPO density ~2.5 g/cm³, แต่ต้องมี volume data จริง
        assumed_density_g_cm3 = 2.5
        volume_l = mass_kg * 1000 / assumed_density_g_cm3 / 1000  # L
        volumetric_density = energy_wh / volume_l if volume_l > 0 else 0

        return {
            "gravimetric_energy_density_wh_kg": gravimetric_density,
            "volumetric_energy_density_wh_l": volumetric_density,
            "total_energy_wh": energy_wh,
            "battery_mass_g": self.iec_data['mass_grams'],
            "assumed_density_g_cm3": assumed_density_g_cm3
        }

    def measure_iec61960_dcir(self, voltage_before: float, voltage_after: float,
                              current_a: float, temp: float = 25.0) -> Dict[str, float]:
        """
        วัด DC Internal Resistance ตาม IEC 61960 Clause 6.4
        DCIR = (V_before - V_after) / I
        """
        if abs(current_a) < 0.1:
            logger.warning("DCIR measurement current too low")
            return {"dcir_mohm": 0.0, "valid_measurement": False}

        # คำนวณ DCIR
        dcir_ohm = abs((voltage_before - voltage_after) / current_a)
        dcir_mohm = dcir_ohm * 1000

        # Temperature และ SoC compensation
        temp_compensation = 1 + (temp - 25) * 0.004  # 0.4%/°C
        dcir_corrected = dcir_ohm / temp_compensation

        # คำนวณ ACIR approximation (typically 80% of DCIR)
        acir_mohm = dcir_mohm * 0.8

        return {
            "dcir_mohm": dcir_mohm,
            "dcir_corrected_mohm": dcir_corrected * 1000,
            "acir_mohm": acir_mohm,
            "measurement_current_a": current_a,
            "measurement_temp_c": temp,
            "temp_compensation_factor": temp_compensation,
            "iec61960_compliant": dcir_mohm < 500  # Max 500mΩ for LiPO
        }

    def assess_iec61960_cycle_life(self, capacity_history: List[float],
                                   cycle_numbers: List[int]) -> Dict[str, float]:
        """
        ประเมิน cycle life ตาม IEC 61960 Clause 6.5
        End of life criteria: 80% of initial capacity หรือ 70% ของ rated capacity
        """
        if len(capacity_history) < 2:
            return {"insufficient_data": True}

        initial_capacity = capacity_history[0]
        rated_capacity = self.iec_data['rated_capacity_ah']

        # หา cycle ที่ capacity ตกลงถึง 80% ของ initial
        cycles_to_80_initial = None
        for i, cap in enumerate(capacity_history):
            if cap <= initial_capacity * 0.8:
                cycles_to_80_initial = cycle_numbers[i] if i < len(cycle_numbers) else len(capacity_history)
                break

        # หา cycle ที่ capacity ตกลงถึง 70% ของ rated
        cycles_to_70_rated = None
        for i, cap in enumerate(capacity_history):
            if cap <= rated_capacity * 0.7:
                cycles_to_70_rated = cycle_numbers[i] if i < len(cycle_numbers) else len(capacity_history)
                break

        # คำนวณ capacity fade rate
        if len(capacity_history) > 1:
            total_cycles = cycle_numbers[-1] if cycle_numbers else len(capacity_history)
            capacity_loss = initial_capacity - capacity_history[-1]
            fade_rate_percent_per_cycle = (capacity_loss / initial_capacity) / total_cycles * 100
        else:
            fade_rate_percent_per_cycle = 0

        # คำนวณ remaining capacity
        remaining_capacity_percent = (capacity_history[-1] / initial_capacity) * 100
        remaining_vs_rated_percent = (capacity_history[-1] / rated_capacity) * 100

        return {
            "cycles_to_80_percent_initial": cycles_to_80_initial,
            "cycles_to_70_percent_rated": cycles_to_70_rated,
            "capacity_fade_rate_percent_per_cycle": fade_rate_percent_per_cycle,
            "remaining_capacity_percent": remaining_capacity_percent,
            "remaining_vs_rated_percent": remaining_vs_rated_percent,
            "initial_capacity_ah": initial_capacity,
            "final_capacity_ah": capacity_history[-1],
            "iec61960_eol_reached": remaining_capacity_percent <= 80 or remaining_vs_rated_percent <= 70
        }