"""
Battery Model: Advanced OCV lookup table และ internal resistance estimation
สำหรับ LiFePO4/Li-ion พร้อม temperature compensation และ aging effects
"""
import logging
import numpy as np
from typing import Optional, Dict, Tuple, List

import aset_batt.core.battery_profiles as battery_profiles
logger = logging.getLogger(__name__)

# Absolute plausible-pack-resistance ceiling — battery_profiles.get_measured_params()
# enforces the same bound independently on manually-entered internal_r_ohm values;
# kept as one named constant so the two checks can't silently drift apart (they
# used to share a bare "5.0" literal in three separate files with no cross-reference).
ABS_R0_CEILING_OHM = 5.0

# Rig-level SCPI/USB latency budget: the max time between a real current-step
# edge and the voltage sample used to compute R=|ΔV/ΔI| before that sample is
# considered relaxed/stale (carrying R1, not just ohmic R0) rather than a clean
# reading. Both the live online step detector (StateEstimator._detect_step_r0,
# via _STEP_MAX_DT_S) and the post-hoc single-step DCIR
# (acquisition.analysis.identify_dcir, via _DCIR_MAX_STEP_DT) read the same
# physical rig, so this budget must be identical for both — they used to each
# hardcode their own "0.5" with no cross-reference.
MAX_STEP_EDGE_LATENCY_S = 0.5

# Max voltage spread (V) to treat a run of samples as a flat/steady rest-or-load
# plateau rather than still settling. Shared by the step detector's rolling
# reference buffer (StateEstimator._STEP_REF_MAX_SPREAD_V) and
# acquisition.analysis._vi_levels' plateau detection (_VI_LEVEL_MAX_SPREAD_V).
STEADY_STATE_MAX_SPREAD_V = 0.15

# Target acquisition rate, shared by every loop that paces itself against real
# hardware readback: acquisition.models.TestConfig.sample_hz (manual TEST MODE,
# worker.py) and ui.sequences.hppc's pulse/relax pacing (used to hardcode its
# own "0.2" independently of the config default — the two drifted out of sync
# by construction). Raised from the old 5.0: measured real SCPI round-trip is
# ~35-40ms for the combined MEAS:SCAL:ALL:DC? query used during discharge/HPPC
# (read_measurements()'s prefer_load_v=True path) — a ~25-28Hz ceiling — and
# ~70-80ms for charge/idle's two separate queries — a ~12-14Hz ceiling. 10Hz
# keeps clear margin under the lower (charge-mode) ceiling while still roughly
# doubling the old target, tightening R0's ΔV/ΔI single-step method (it reads
# whatever sample lands first after the current edge — the sooner that sample
# arrives, the less R1/polarisation has bled into it; see identify_dcir's own
# docstring). This does NOT guarantee HPPC pulses achieve 10Hz in practice —
# that still depends on real per-iteration cost + GIL contention with other
# threads, both separately measured via each loop's own Hz-breakdown logging.
DEFAULT_SAMPLE_HZ = 10.0


def is_plausible_r0(r0: float, base_rin: float,
                    abs_ceiling: float = ABS_R0_CEILING_OHM) -> bool:
    """True if ``r0`` sits within [0.2x, 6x] of ``base_rin`` AND under
    ``abs_ceiling`` — the same relative-plausibility test used by both the live
    online step detector (StateEstimator._detect_step_r0) and the post-hoc
    single-step DCIR (acquisition.analysis.identify_dcir) to reject a ΔV/ΔI
    reading that is a polarisation/quantisation/stale-readback artifact, not a
    real ohmic measurement. A fixed absolute ceiling alone once accepted a
    charge-CV-taper edge as "R0 = 4.83 Ω" (ΔI was just tail current, ΔV was
    collapsing overpotential); a bare relative band alone let a stale-voltage
    readback compute "R0 = 0.00 Ω" (ΔV=0 across a real current edge) — both
    bounds are needed together."""
    base = max(1e-4, float(base_rin))
    return 0.2 * base <= r0 <= min(6.0 * base, abs_ceiling)


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

    def _arrhenius_temp_factor(self, temp: float) -> float:
        """Shared temperature factor for BOTH temp_rin_multiplier() (a pure
        temperature ratio, used to normalize a resistance fitted elsewhere —
        e.g. an ECM pulse fit that's already SoC-dependent but temperature-
        blind) and _calculate_base_rin() (the full temp×SoC×aging model).

        Arrhenius (physically correct) when an Ea/R value is present in the
        chemistry profile — accepts both 'arrhenius_ea_r' (our convention) and
        'arrhenius_ea_k' key names for compatibility:
            R(T) = R0 x exp(Ea/R x (1/T_K - 1/T_ref))
        Falls back to a linear approximation (only valid within +-10 C of
        25 C) if no Ea/R is configured.

        These two call sites used to each reimplement this formula separately
        and drifted apart for months — temp_rin_multiplier() only checked the
        'arrhenius_ea_k' key (which no chemistry profile actually sets) and so
        silently ALWAYS fell back to the linear approximation while
        _calculate_base_rin() (checking both names) correctly used Arrhenius;
        at 10 degC for lead-acid (Ea/R=4000 K) that's x1.075 (linear) vs the
        correct x2.04 (Arrhenius) — nearly 2x off."""
        params = self.rin_params
        ea = params.get('arrhenius_ea_r', 0.0) or params.get('arrhenius_ea_k', 0.0)
        if ea > 0.0:
            t_k, t_ref = temp + 273.15, 298.15
            return float(np.exp(ea * (1.0 / t_k - 1.0 / t_ref))) - 1.0
        return params['temp_coeff'] * (25.0 - temp)

    def temp_rin_multiplier(self, temp: float) -> float:
        """ตัวคูณความต้านทานจากอุณหภูมิล้วน (อ้างอิง 25°C = 1.0) แยกจาก SoC/aging factor
        เพื่อเอาไปคูณกับความต้านทานที่ได้จากแหล่งอื่น (เช่น ECM fit ที่ SoC-dependent
        อยู่แล้วแต่ไม่รู้เรื่องอุณหภูมิ) — ใช้สูตร Arrhenius เดียวกับ _calculate_base_rin"""
        temp = self._clamp_temperature(temp)
        temp_factor = self._arrhenius_temp_factor(temp)
        return max(0.1, 1.0 + temp_factor)

    def _calculate_base_rin(self, soc: float, temp: float) -> float:
        """คำนวณ base internal resistance (ระดับแพ็ค) จาก temperature และ SoC"""
        params = self.rin_params
        temp_factor = self._arrhenius_temp_factor(temp)

        # SoC factor (สูงขึ้นเมื่อ SoC ต่ำหรือสูง)
        soc_factor = params['soc_coeff'] * abs(soc - 50.0)

        # Aging factor
        aging_factor = params['aging_coeff'] * (1.0 - self.aging_factor)

        rin_cell = params['r0'] * (1 + temp_factor) * (1 + soc_factor) * (1 + aging_factor)
        # scale เป็นระดับแพ็ค: อนุกรมบวกกัน, ขนานหารกัน
        rin_pack = rin_cell * self.series_cells / self.parallel_cells
        return max(0.001, rin_pack)


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