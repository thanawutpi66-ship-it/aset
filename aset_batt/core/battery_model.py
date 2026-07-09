"""
Battery Model: Advanced OCV lookup table และ internal resistance estimation
สำหรับ LiFePO4/Li-ion พร้อม temperature compensation และ aging effects
"""
import logging
import numpy as np
from typing import Optional, Dict, Tuple, List

import aset_batt.core.battery_profiles as battery_profiles
logger = logging.getLogger(__name__)

class BatteryModel:
    """Advanced battery electrical model ด้วย temperature compensation"""

    def __init__(self, battery_type: str = "LiPO", nominal_voltage: float = 3.7,
                 series_cells: int = 1, parallel_cells: int = 1, product_name: str = ""):
        self.battery_type = battery_type
        self.product_name = product_name
        self.nominal_voltage = nominal_voltage  # per-cell (V)
        # โครงสร้างแพ็ค: series คูณแรงดัน+ความต้านทาน, parallel คูณความจุ/หารความต้านทาน
        self.series_cells = max(1, int(series_cells))
        self.parallel_cells = max(1, int(parallel_cells))

        # Temperature range สำหรับ interpolation
        self.temp_range = [-10, 0, 10, 25, 40, 60]  # °C

        # ดึงโปรไฟล์เคมีจาก registry (battery_profiles.json + built-in fallback)
        # — แทนการ hardcode พารามิเตอร์แบบ if/elif เดิม
        self.chemistry = battery_profiles.get_chemistry(battery_type)

        # Temperature-dependent OCV tables (ต่อเซลล์)
        self.ocv_tables = self._generate_ocv_tables()

        # Internal resistance model parameters (ต่อเซลล์)
        self.rin_params = self._get_rin_parameters()

        # กลยุทธ์การชาร์จ (ต่อเซลล์) — ใช้โดย 3-stage / CC-CV charger ใน auto_controller
        self.charge_profile = self.chemistry.charge

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
        """สร้าง OCV lookup tables สำหรับอุณหภูมิต่างๆ จาก chemistry profile

        Base table = rested OCV ต่อเซลล์ ณ 25°C (จาก battery_profiles).
        ถ้าเคมีมี temp_coeff_mv_per_degc != 0 (เช่น Lead-Acid Nernst ~+0.40 mV/°C/cell)
        จะ shift ทุกจุดใน OCV table ตามอุณหภูมิ relative จาก 25°C reference.
        Li-ion/LFP: tc=0 → ใช้ table เดิมทุกอุณหภูมิ (ผลเล็กน้อย vs. Rin ที่เปลี่ยนมาก)
        """
        base_table = self.chemistry.ocv_curve
        tc = getattr(self.chemistry, "temp_coeff_mv_per_degc", 0.0)
        if tc == 0.0:
            return {temp: dict(base_table) for temp in self.temp_range}
        # Nernst shift: delta_v = tc[mV/°C/cell] × (T − 25°C) / 1000 → V/cell
        result = {}
        for t in self.temp_range:
            delta_v = tc * (t - 25.0) * 1e-3
            result[t] = {soc: ocv + delta_v for soc, ocv in base_table.items()}
        return result

    def _get_rin_parameters(self) -> Dict[str, float]:
        """Parameters สำหรับ internal resistance model (จาก chemistry profile)

        r0 = base resistance ต่อเซลล์ ที่ 25°C/50% SoC (Ohm);
        temp_coeff (Arrhenius, R สูงเมื่อเย็น), soc_coeff (U-shape), aging_coeff
        """
        return dict(self.chemistry.rin)

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

    def get_ocv_from_soc(self, soc: float, temp: float = 25.0,
                         direction: int = 0) -> float:
        """คำนวณ OCV จาก SoC และ temperature ด้วย interpolation

        direction: +1 = charging, −1 = discharging, 0 = rest (default).
        ถ้า chemistry.hysteresis_v_per_cell > 0 จะบวก/ลบครึ่งของ hysteresis ตามทิศทาง
        (charge OCV สูงกว่า discharge OCV ที่ SoC เดียวกัน). default 0 → ไม่มีผล.
        """
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
        # OCV hysteresis (direction-dependent half-offset; 0 when not characterised)
        if direction != 0:
            hyst = getattr(self.chemistry, "hysteresis_v_per_cell", 0.0)
            if hyst > 0.0:
                cell_ocv += 0.5 * hyst * (1 if direction > 0 else -1)
        return cell_ocv * self.series_cells

    def ocv_slope(self, soc: float, temp: float = 25.0, dsoc: float = 1.0) -> float:
        """|dOCV/dSoC| ต่อเซลล์ (V ต่อ %SoC) ที่ SoC ที่กำหนด

        ใช้ตรวจช่วง plateau ที่ flat (slope ต่ำ) ของ LFP ซึ่ง OCV→SoC ill-conditioned
        คืนค่า "ต่อเซลล์" (หาร series) เพื่อให้ threshold ไม่ขึ้นกับขนาดแพ็ค
        """
        s1 = max(0.0, soc - dsoc)
        s2 = min(100.0, soc + dsoc)
        if s2 <= s1:
            return 0.0
        d_pack = abs(self.get_ocv_from_soc(s2, temp) - self.get_ocv_from_soc(s1, temp))
        return d_pack / (s2 - s1) / self.series_cells

    def get_soc_from_ocv(self, ocv: float, temp: float = 25.0) -> float:
        """Reverse lookup: OCV (แพ็ค) -> SoC

        รับแรงดันระดับแพ็ค หารด้วย series ก่อน lookup per-cell
        หมายเหตุ: ช่วง plateau ของ LFP มี dOCV/dSoC ≈ 0 → SoC ที่ได้ ill-conditioned
        (V คลาดนิดเดียว SoC เพี้ยนมาก) — ควรใช้เฉพาะหลัง rest นานพอ

        ใช้ temperature interpolation (เหมือน get_ocv_from_soc) เพื่อให้ symmetric —
        การ snap ไป nearest table เดิมทำให้ SoC กระโดดเมื่ออุณหภูมิข้ามจุดกึ่งกลาง
        """
        temp = self._clamp_temperature(temp)
        cell_ocv = ocv / self.series_cells

        temp_idx = self._find_temp_index(temp)
        if temp_idx >= len(self.temp_range) - 1:
            data = self._interp_data[self.temp_range[-1]]
            soc = float(np.interp(cell_ocv, data['ocv_vals'], data['soc_keys']))
        else:
            t1, t2 = self.temp_range[temp_idx], self.temp_range[temp_idx + 1]
            w2 = (temp - t1) / (t2 - t1)   # weight toward upper temp
            w1 = 1.0 - w2
            d1 = self._interp_data[t1]
            d2 = self._interp_data[t2]
            soc1 = float(np.interp(cell_ocv, d1['ocv_vals'], d1['soc_keys']))
            soc2 = float(np.interp(cell_ocv, d2['ocv_vals'], d2['soc_keys']))
            soc = w1 * soc1 + w2 * soc2

        return max(0.0, min(100.0, soc))

    def ocv_out_of_range_mv(self, ocv_pack: float, temp: float = 25.0) -> float:
        """How far (mV, pack-level) ``ocv_pack`` sits outside the calibrated OCV
        curve's own defined range — positive above the 100% point, negative below
        the 0% point, 0.0 if within range.

        get_soc_from_ocv() silently clamps an out-of-range reading to 0/100 %
        (np.interp's normal behaviour) with no signal that it happened. A rested
        terminal voltage genuinely ABOVE the curve's own 100% point is not
        physically a "more than full" charge — for lead-acid specifically it is
        the classic *surface charge* symptom (a temporary post-charge voltage
        elevation from concentrated electrolyte near the plates that has not yet
        diffused into the bulk, taking hours to relax — far longer than a rest
        window sized for coulomb-counting drift). Caught on a real pack: a 300 s
        rest read 13.15 V, 260 mV above this chemistry's own calibrated 100 %
        point (12.888 V for a 6S pack) — flat/stable within the settle window,
        but not actually at equilibrium.
        """
        temp = self._clamp_temperature(temp)
        cell_ocv = ocv_pack / self.series_cells
        # Same temperature-blend as get_soc_from_ocv/get_ocv_from_soc — using only
        # self.temp_range[temp_idx] (no blend) picks the WRONG table whenever temp
        # lands exactly on a grid point other than the first (_find_temp_index
        # returns i-1, meant to be paired with i, not used alone).
        temp_idx = self._find_temp_index(temp)
        if temp_idx >= len(self.temp_range) - 1:
            data = self._interp_data[self.temp_range[-1]]
            v_min, v_max = min(data['ocv_vals']), max(data['ocv_vals'])
        else:
            t1, t2 = self.temp_range[temp_idx], self.temp_range[temp_idx + 1]
            w2 = (temp - t1) / (t2 - t1)
            w1 = 1.0 - w2
            d1, d2 = self._interp_data[t1], self._interp_data[t2]
            v_min = w1 * min(d1['ocv_vals']) + w2 * min(d2['ocv_vals'])
            v_max = w1 * max(d1['ocv_vals']) + w2 * max(d2['ocv_vals'])
        if cell_ocv > v_max:
            return (cell_ocv - v_max) * self.series_cells * 1000.0
        if cell_ocv < v_min:
            return (cell_ocv - v_min) * self.series_cells * 1000.0
        return 0.0

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

        # คำนวณ base resistance จาก model ก่อน (ใช้เป็น sanity bound)
        rin_base = self._calculate_base_rin(soc, temp)

        # Thevenin model: rin = (OCV − V) / I.  Only reliable when |I| is large enough
        # that the voltage drop dominates measurement noise.  At low currents (< 0.5A)
        # a ±5 mV noise floor causes >10 mΩ error, so fall back to model entirely.
        if abs(current) >= 0.5:
            ocv = self.get_ocv_from_soc(soc, temp)
            rin_raw = abs((ocv - voltage) / current)
            # Reject physically unreasonable values (> 10× base or negative)
            if 0 < rin_raw <= 10.0 * rin_base:
                rin_calculated = rin_raw
            else:
                rin_calculated = rin_base
        else:
            rin_calculated = rin_base

        # Blend calculated และ model values
        rin = 0.6 * rin_calculated + 0.4 * rin_base

        # Blend กับ measured DCIR ถ้ามี
        if measured_dcir > 0:
            rin = 0.7 * rin + 0.3 * measured_dcir

        # clamp ขอบบนสเกลตามแพ็ค (series เพิ่ม R, parallel ลด R)
        r_max = 0.5 * self.series_cells / self.parallel_cells
        return max(0.001, min(r_max, rin))

    def temp_rin_multiplier(self, temp: float) -> float:
        """ตัวคูณความต้านทานจากอุณหภูมิล้วน (อ้างอิง 25°C = 1.0) แยกจาก SoC/aging factor
        เพื่อเอาไปคูณกับความต้านทานที่ได้จากแหล่งอื่น (เช่น ECM fit ที่ SoC-dependent
        อยู่แล้วแต่ไม่รู้เรื่องอุณหภูมิ) — ใช้สูตร Arrhenius เดียวกับ _calculate_base_rin"""
        temp = self._clamp_temperature(temp)
        params = self.rin_params
        # BUG FIX: this used to look up only 'arrhenius_ea_k', a key no chemistry
        # profile actually sets (battery_profiles.py sets 'arrhenius_ea_r') — so this
        # method silently ALWAYS fell back to the linear approximation, while
        # _calculate_base_rin (which checks both names) correctly used Arrhenius. The
        # discrepancy was large: at 10°C for lead-acid (Ea/R=4000 K) the linear model
        # gives ×1.075 vs the correct Arrhenius ×2.04 — nearly 2× off. Check both key
        # names, same as _calculate_base_rin, so the two stay consistent.
        ea_k = params.get('arrhenius_ea_r', 0.0) or params.get('arrhenius_ea_k', 0.0)
        if ea_k > 0:
            t_k, t_ref = temp + 273.15, 298.15
            temp_factor = float(np.exp(ea_k * (1.0 / t_k - 1.0 / t_ref))) - 1.0
        else:
            temp_factor = params['temp_coeff'] * (25.0 - temp)
        return max(0.1, 1.0 + temp_factor)

    def _calculate_base_rin(self, soc: float, temp: float) -> float:
        """คำนวณ base internal resistance (ระดับแพ็ค) จาก temperature และ SoC"""
        params = self.rin_params

        # Temperature compensation: Arrhenius (physically correct) when an Ea/R value
        # is present in the chemistry profile. Accepts both key names for compatibility:
        #   'arrhenius_ea_r' (Ea/R in K, our convention) or 'arrhenius_ea_k' (same).
        # R(T) = R₀ × exp(Ea/R × (1/T_K − 1/T_ref)); falls back to linear if unset.
        ea_r = params.get('arrhenius_ea_r', 0.0) or params.get('arrhenius_ea_k', 0.0)
        if ea_r > 0.0:
            t_k = temp + 273.15
            t_ref = 298.15
            temp_factor = float(np.exp(ea_r * (1.0 / t_k - 1.0 / t_ref))) - 1.0
        else:
            # Legacy linear: only valid within ±10 °C of 25 °C
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

    def set_aging_from_soh(self, soh: Optional[float]) -> None:
        """Wire a capacity-based SoH (aset_batt.core.state_estimator's live ``soh`` —
        itself sourced from acquisition.analysis's full-discharge measurement, or a
        prior test in the same session/state) into the aging factor that
        ``_calculate_base_rin`` blends into the Rin baseline — so a pack's OWN
        measured health, not just its chemistry, shapes what "healthy" DCIR/R0 mean
        for grading (Phase D3: was previously always exactly 1.0 in production, since
        nothing called this or ``update_aging_factor``/``get_soh_from_capacity``).

        ``soh=None`` (or NaN — e.g. a quick HPPC-only test with no full discharge
        yet in this session) resets aging_factor to 1.0: a safe "assume new/unknown"
        default rather than carrying over a stale value that could belong to a
        previously-tested, different physical battery.
        """
        if soh is None or soh != soh:   # None or NaN
            self.aging_factor = 1.0
            return
        # Same floor as update_aging_factor(): never assume more than 50% extra
        # resistance from aging alone. Capped at 1.0 — a SoH reading above 100% (measurement
        # noise/calibration) should not further REDUCE the baseline below chemistry-generic.
        self.aging_factor = max(0.5, min(1.0, float(soh) / 100.0))

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

        # Empty arrays reach here on an abort before the first sample (e.g. the
        # operator stops a profile test immediately) — guard both divisions the
        # same way actual_time_hours already does below, instead of letting a
        # ZeroDivisionError crash the abort-results step.
        avg_voltage = avg_voltage / len(voltage_data) if voltage_data else 0.0

        # IEC 61960 compliance check
        expected_time_hours = (
            self.iec_data['rated_capacity_ah'] / discharge_rate if discharge_rate else 0.0
        )
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