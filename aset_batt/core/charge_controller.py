"""
Charge Controller — สเตทแมชชีนการชาร์จตามชนิดเคมี

- Lead-acid (VRLA):  3-stage  →  Bulk (CC) → Absorption (CV) → Float
- Lithium (cc_cv):   CC → CV → จบ (ไม่ float)

แนวคิดสำคัญ: **PSU ทำ CC↔CV ในฮาร์ดแวร์เอง** เมื่อสั่ง :VOLT + :CURR limit พร้อมกัน
(ดู HardwareController.set_psu_cccv). ซอฟต์แวร์มีหน้าที่แค่ "เปลี่ยน stage"
(ลดแรงดันลง float / จบการชาร์จ) ตามกระแสที่ taper ลง + timeout — ไม่ได้ bit-bang
CC/CV เอง จึงทนต่อ readback ช้า (~5 Hz) ได้

แยก decision logic (`decide`, pure) ออกจาก I/O loop (`run`) เพื่อให้ unit-test ได้
โดยไม่ต้องมีฮาร์ดแวร์
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import aset_batt.core.battery_profiles as battery_profiles
logger = logging.getLogger(__name__)

# Stages
IDLE = "idle"
BULK = "bulk"            # three_stage: CC อัดกระแสคงที่
ABSORPTION = "absorption"  # three_stage: CV เลี้ยงแรงดัน, กระแส taper
FLOAT = "float"          # three_stage: เลี้ยง float voltage (แบตเต็มแล้ว)
CC = "cc"                # cc_cv: constant current
CV = "cv"                # cc_cv: constant voltage, กระแส taper
DONE = "done"
ERROR = "error"


@dataclass
class ChargeParams:
    """Setpoint ระดับแพ็ค (คำนวณจาก per-cell profile × series + C-rate × capacity)"""
    strategy: str
    bulk_current_a: float
    tail_current_a: float
    absorption_v: float      # three_stage
    float_v: float           # three_stage
    cv_v: float              # cc_cv
    v_margin: float          # ระยะเผื่อที่ถือว่า "แตะ" แรงดันเป้า (CV เริ่ม)
    stage_timeout_s: float
    # จำนวน "ครั้งติดต่อกัน" ที่ i_charge ต้อง <= tail_current_a ก่อนจะยอมรับว่าแบตเต็มจริง
    # (ดูเหตุผลเต็มที่ decide()'s docstring) — นับเป็นจำนวนครั้ง ไม่ใช่วินาที เพื่อไม่ผูกกับ
    # poll_interval_s (poll_interval_s=0 ใน unit test ก็ยังทำงานถูกต้อง)
    tail_confirm_samples: int = 5

    @classmethod
    def from_config(cls, charge_profile, series_cells: int,
                    rated_capacity_ah: float,
                    strategy: str = None,
                    bulk_c_rate_override: float = None) -> "ChargeParams":
        """strategy=None → ใช้ตามเคมีของแบต (profile); ส่งค่ามาเพื่อ override
        (เช่นผู้ใช้เลือก 'cc_cv' / 'three_stage' จาก dropdown ใน GUI)
        bulk_c_rate_override: ผู้ใช้เลือก C-rate เอง (None = ใช้ค่าจาก profile)"""
        cp = charge_profile
        series = max(1, int(series_cells))
        cap = max(1e-6, float(rated_capacity_ah))
        bulk_c = bulk_c_rate_override if bulk_c_rate_override is not None else cp.bulk_c_rate
        return cls(
            strategy=strategy or cp.strategy,
            bulk_current_a=bulk_c * cap,
            tail_current_a=cp.tail_current_c_rate * cap,
            absorption_v=cp.absorption_voltage_per_cell * series,
            float_v=cp.float_voltage_per_cell * series,
            cv_v=cp.cv_voltage_per_cell * series,
            v_margin=0.05 * series,
            stage_timeout_s=cp.stage_timeout_min * 60.0,
        )


@dataclass
class ChargeDecision:
    """ผลการตัดสินใจหนึ่งสเต็ป: stage ใหม่ + setpoint ที่จะสั่ง PSU"""
    stage: str
    set_voltage: float
    set_current: float
    done: bool = False        # แบตเต็ม/จบการชาร์จแล้ว
    output_on: bool = True     # False = สั่ง PSU OFF
    note: str = ""


def decide(params: ChargeParams, stage: str, v_pack: float, i_charge: float,
           t_in_stage: float, tail_confirm_n: int = 0) -> ChargeDecision:
    """ตัดสินสเตทถัดไป (pure) — ไม่มี side-effect

    v_pack        = แรงดันแพ็คที่วัดได้ (V)
    i_charge      = กระแสที่ไหลเข้าแบต (A, บวก = กำลังชาร์จ)
    t_in_stage    = เวลาที่อยู่ใน stage ปัจจุบัน (s) — ใช้ตัด timeout
    tail_confirm_n= จำนวนครั้งติดต่อกัน (นับโดย caller) ที่ i_charge <= tail_current_a
                    มาแล้ว — ใช้ยืนยันก่อนจบการชาร์จจริง (ABSORPTION→FLOAT, CV→DONE)

    ทำไมต้อง tail_confirm_n: ก่อนหน้านี้กระแส "ต่ำกว่า tail" แค่ sample เดียว
    (กระแสกระตุกลงชั่วขณะระหว่าง PSU ปรับ regulation, sensor noise ใกล้ threshold)
    ก็จบการชาร์จเงียบๆ ทันที ทั้งที่แบตยังไม่เต็มจริง — ไม่มี warning ด้วย เพราะ
    decide() คืนค่า FLOAT/DONE ตรงๆ ให้ caller เชื่อทันที การจบชาร์จก่อนเวลาแบบเงียบ
    เป็นผลลัพธ์ที่ร้ายแรงกว่าการหน่วงจบไม่กี่ sample เพื่อยืนยันซ้ำ (asymmetric risk)
    """
    p = params
    timed_out = t_in_stage >= p.stage_timeout_s
    tail_confirmed = i_charge <= p.tail_current_a and tail_confirm_n >= p.tail_confirm_samples

    if p.strategy == "three_stage":
        if stage in (IDLE, BULK):
            # CC อัดกระแส; PSU จะเข้า CV เองเมื่อแรงดันแตะ absorption
            if v_pack >= p.absorption_v - p.v_margin:
                return ChargeDecision(ABSORPTION, p.absorption_v, p.bulk_current_a,
                                      note="แรงดันแตะ absorption → เข้า CV")
            if timed_out:
                return ChargeDecision(ABSORPTION, p.absorption_v, p.bulk_current_a,
                                      note="bulk timeout → ข้ามไป absorption")
            return ChargeDecision(BULK, p.absorption_v, p.bulk_current_a)
        if stage == ABSORPTION:
            if tail_confirmed or timed_out:
                why = "กระแส taper ถึง tail (ยืนยันแล้ว)" if tail_confirmed else "absorption timeout"
                return ChargeDecision(FLOAT, p.float_v, p.bulk_current_a,
                                      note=f"{why} → float")
            return ChargeDecision(ABSORPTION, p.absorption_v, p.bulk_current_a)
        if stage == FLOAT:
            # แบตเต็มแล้ว — เลี้ยง float ต่อ (done=True ให้ caller เลือกหยุด/เลี้ยงต่อ)
            return ChargeDecision(FLOAT, p.float_v, p.bulk_current_a, done=True,
                                  note="float (แบตเต็ม)")

    else:  # cc_cv (lithium)
        if stage in (IDLE, CC):
            if v_pack >= p.cv_v - p.v_margin:
                return ChargeDecision(CV, p.cv_v, p.bulk_current_a,
                                      note="แรงดันแตะ CV")
            if timed_out:
                return ChargeDecision(CV, p.cv_v, p.bulk_current_a,
                                      note="cc timeout → cv")
            return ChargeDecision(CC, p.cv_v, p.bulk_current_a)
        if stage == CV:
            if tail_confirmed or timed_out:
                why = "กระแส taper ถึง tail (ยืนยันแล้ว)" if tail_confirmed else "cv timeout"
                return ChargeDecision(DONE, p.cv_v, 0.0, done=True, output_on=False,
                                      note=f"{why} → จบการชาร์จ")
            return ChargeDecision(CV, p.cv_v, p.bulk_current_a)

    # stage แปลก ๆ — ปลอดภัยไว้ก่อน
    return ChargeDecision(DONE, 0.0, 0.0, done=True, output_on=False, note="unknown stage")


class ChargeController:
    """ขับ state machine การชาร์จกับฮาร์ดแวร์จริง (เรียกจาก thread แยกใน AutoController)"""

    def __init__(self, hw, config, battery_model,
                 on_update: Optional[Callable[[str, float, float, str], None]] = None,
                 poll_interval_s: float = 1.0, strategy: str = None,
                 bulk_c_rate_override: float = None):
        self.hw = hw
        self.config = config
        self.params = ChargeParams.from_config(
            battery_model.charge_profile,
            config.battery.cells_series,
            config.battery.rated_capacity,
            strategy=strategy,
            bulk_c_rate_override=bulk_c_rate_override,
        )
        self.on_update = on_update           # callback(stage, v, i_charge, note)
        self.poll_interval_s = poll_interval_s
        self.stage = IDLE
        self._running = False

    def stop(self):
        self._running = False

    def run(self, should_stop: Optional[Callable[[], bool]] = None,
            float_hold_s: float = 0.0) -> str:
        """รัน loop การชาร์จจนจบ/ถูกสั่งหยุด คืนค่า stage สุดท้าย

        should_stop()  = callback คืน True เมื่อต้องหยุด (safety/ผู้ใช้กด stop)
        float_hold_s   = lead-acid: เลี้ยง float ต่ออีกกี่วินาทีหลังเต็มก่อนหยุด (0 = ไม่เลี้ยง)
        """
        if not getattr(self.hw, "is_connected", False):
            logger.error("ChargeController: hardware ไม่ได้เชื่อมต่อ")
            return ERROR

        self._running = True
        self.stage = IDLE
        stage_start = time.time()
        float_entered_at: Optional[float] = None
        # Consecutive-sample counter for decide()'s tail_confirm_n — counts SAMPLES,
        # not elapsed seconds, so it works correctly even at poll_interval_s=0 (unit
        # tests) where wall-clock time barely advances per iteration.
        tail_confirm_n = 0
        logger.info(f"เริ่มชาร์จ strategy={self.params.strategy} "
                    f"bulk={self.params.bulk_current_a:.2f}A")

        try:
            while self._running:
                if should_stop and should_stop():
                    logger.info("ChargeController: ได้รับสัญญาณหยุด")
                    break

                v_pack, psu_i, _load_i = self.hw.read_vi()
                i_charge = max(0.0, psu_i)   # กระแสเข้าแบต (psu_i = charge)
                t_in_stage = time.time() - stage_start
                tail_confirm_n = (tail_confirm_n + 1) if i_charge <= self.params.tail_current_a else 0

                d = decide(self.params, self.stage, v_pack, i_charge, t_in_stage, tail_confirm_n)

                if d.stage != self.stage:
                    logger.info(f"charge stage {self.stage} → {d.stage}: {d.note}")
                    stage_start = time.time()
                    self.stage = d.stage
                    if d.stage == FLOAT:
                        float_entered_at = time.time()

                # re-check abort ทันทีก่อน "จ่ายไฟ" — กัน race กับ emergency shutdown
                # (ถ้า safety ทริกเกอร์หลังเช็คตอนต้นรอบ จะได้ไม่สั่ง OUTP ON ซ้ำหลังตัดไฟ)
                if should_stop and should_stop():
                    logger.info("ChargeController: abort ก่อนจ่ายไฟ (safety/stop)")
                    break

                if d.output_on:
                    self.hw.set_psu_cccv(d.set_voltage, d.set_current)
                else:
                    self.hw.psu_off()

                if self.on_update:
                    self.on_update(self.stage, v_pack, i_charge, d.note)

                # cc_cv: จบเมื่อ DONE. three_stage: จบเมื่อเลี้ยง float ครบ float_hold_s
                if d.stage == DONE:
                    break
                if d.stage == FLOAT and float_entered_at is not None:
                    if time.time() - float_entered_at >= float_hold_s:
                        logger.info("ชาร์จเสร็จ (lead-acid เข้า float แล้ว)")
                        break

                time.sleep(self.poll_interval_s)
        finally:
            self._running = False
            try:
                self.hw.psu_off()
            except Exception as e:
                logger.error(f"ปิด PSU หลังชาร์จไม่สำเร็จ: {e}")

        return self.stage
