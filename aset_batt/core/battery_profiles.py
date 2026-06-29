"""
Battery Profile Registry — ฐานข้อมูลโปรไฟล์แบตเตอรี่ (chemistry + charging strategy)

แทนที่การ hardcode พารามิเตอร์เคมีแบบ if/elif ใน battery_model.py
- "chemistries" = พารามิเตอร์เชิงฟิสิกส์ต่อเซลล์ (OCV curve, Rin) + กลยุทธ์การชาร์จ
- "products"    = แบตรุ่นจริง (เช่น YTZ7V) ที่อ้างอิง chemistry + ขนาดแพ็ค (สำหรับ dropdown)

อ่านจาก battery_profiles.json ถ้ามี (merge ทับ default) — ถ้าไฟล์หาย/พัง จะ fallback
ไปใช้ built-in default ซึ่ง "ค่าเดียวกับที่เคย hardcode" เป๊ะ เพื่อกัน test/runtime พัง
(แนวเดียวกับ config.py ที่ fallback ไป dataclass default)
"""
import json
import os
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "battery_profiles.json")


@dataclass
class ChargeProfile:
    """กลยุทธ์การชาร์จต่อเคมี (ค่าแรงดันเป็น 'ต่อเซลล์' — คูณ series เป็นแพ็คตอนใช้งาน)

    strategy:
      - "cc_cv"        : Li-ion/LiPO/LiFePO4 (CC จน cv_voltage แล้ว CV จนกระแส tail)
      - "three_stage"  : Lead-acid (Bulk CC -> Absorption CV -> Float)
    """
    strategy: str = "cc_cv"
    bulk_c_rate: float = 0.5            # กระแส bulk เป็นสัดส่วนของ C (0.5C)
    cv_voltage_per_cell: float = 4.20   # แรงดัน CV ต่อเซลล์ (cc_cv)
    absorption_voltage_per_cell: float = 0.0  # แรงดัน absorption ต่อเซลล์ (three_stage)
    float_voltage_per_cell: float = 0.0       # แรงดัน float ต่อเซลล์ (three_stage)
    tail_current_c_rate: float = 0.05   # เกณฑ์กระแสจบ CV/absorption (สัดส่วน C)
    stage_timeout_min: float = 240.0    # กันค้าง: timeout ต่อ stage (นาที)


@dataclass
class ChemistryProfile:
    """พารามิเตอร์เชิงฟิสิกส์ต่อเซลล์ของเคมีหนึ่งชนิด"""
    name: str
    ocv_curve: Dict[int, float]          # {soc_pct: ocv_per_cell}
    rin: Dict[str, float]                # r0/temp_coeff/soc_coeff/aging_coeff
    charge: ChargeProfile = field(default_factory=ChargeProfile)
    # Nernst temperature coefficient: mV/°C/cell shift of OCV from 25°C reference.
    # Lead-acid H₂SO₄ electrolyte: ~+0.40 mV/°C/cell (OCV rises slightly with T).
    # Li-ion / LFP: ≈0 (temp effect small; separate per-temp tables used instead).
    temp_coeff_mv_per_degc: float = 0.0
    # Peukert parameters for real-time SoC during discharge.
    # k=1.0 → no correction (Li-ion k≈1.0–1.05).  Lead-acid k≈1.25–1.35.
    # peukert_hr = hour-rate at which rated_capacity is specified (10HR or 20HR).
    peukert_k: float = 1.0
    peukert_hr: float = 20.0


@dataclass
class ProductProfile:
    """แบตรุ่นจริงที่ผู้ใช้เลือกจาก dropdown — map ไป chemistry + ขนาดแพ็ค

    max/min_voltage_per_cell + safety_*_pack จำเป็นเพื่อให้การสลับรุ่นตั้ง "หน้าต่าง
    แรงดันให้สอดคล้องกับเคมีใหม่" (ไม่งั้น pack_max/min_voltage + safety window จะค้าง
    ค่าของรุ่นเดิม ทำให้ IEC test / OCV init / safety ผิด)
    """
    name: str
    chemistry: str
    nominal_voltage_per_cell: float
    cells_series: int
    cells_parallel: int
    rated_capacity_ah: float
    max_voltage_per_cell: float = 0.0    # 0 = ไม่ระบุ (ผู้เรียกจะไม่แก้ค่าเดิม)
    min_voltage_per_cell: float = 0.0
    safety_ovp_pack: float = 0.0         # over-voltage protection ระดับแพ็ค (V)
    safety_uvp_pack: float = 0.0         # under-voltage protection ระดับแพ็ค (V)
    mass_grams: float = 0.0
    cca_a: float = 0.0                        # Cold Cranking Amps (0 = ไม่มี/ไม่ใช่ starter)
    max_cont_discharge_a: float = 0.0         # กระแส discharge ต่อเนื่องสูงสุด (A); 0 = ไม่ระบุ
    max_peak_discharge_a: float = 0.0         # กระแส discharge peak สูงสุด (A); 0 = ไม่ระบุ
    notes: str = ""


# ---------------------------------------------------------------------------
# Built-in defaults — ค่าเดียวกับที่เคย hardcode ใน battery_model.py (ห้ามเปลี่ยนตัวเลข)
# ---------------------------------------------------------------------------
_DEFAULT_CHEMISTRIES: Dict[str, ChemistryProfile] = {
    "LiPO": ChemistryProfile(
        name="LiPO",
        ocv_curve={
            0: 3.00, 5: 3.45, 10: 3.55, 15: 3.62, 20: 3.67,
            25: 3.71, 30: 3.75, 35: 3.78, 40: 3.81, 45: 3.84,
            50: 3.87, 55: 3.90, 60: 3.93, 65: 3.96, 70: 3.99,
            75: 4.03, 80: 4.07, 85: 4.11, 90: 4.15, 95: 4.18,
            100: 4.20,
        },
        rin={"r0": 0.025, "temp_coeff": 0.004, "soc_coeff": 0.0008,
             "aging_coeff": 0.001},
        charge=ChargeProfile(strategy="cc_cv", bulk_c_rate=0.5,
                             cv_voltage_per_cell=4.20, tail_current_c_rate=0.05,
                             stage_timeout_min=120.0),
    ),
    "LiFePO4": ChemistryProfile(
        name="LiFePO4",
        ocv_curve={
            0: 2.50, 5: 2.90, 10: 3.10, 15: 3.18, 20: 3.22,
            25: 3.245, 30: 3.255, 35: 3.262, 40: 3.268, 45: 3.273,
            50: 3.278, 55: 3.283, 60: 3.288, 65: 3.293, 70: 3.300,
            75: 3.308, 80: 3.318, 85: 3.330, 90: 3.345, 95: 3.365,
            100: 3.400,
        },
        rin={"r0": 0.045, "temp_coeff": 0.003, "soc_coeff": 0.0005,
             "aging_coeff": 0.002},
        charge=ChargeProfile(strategy="cc_cv", bulk_c_rate=0.5,
                             cv_voltage_per_cell=3.65, tail_current_c_rate=0.05,
                             stage_timeout_min=120.0),
    ),
    "LeadAcid": ChemistryProfile(
        name="LeadAcid",
        ocv_curve={
            0: 1.960, 5: 1.980, 10: 1.995, 15: 2.005, 20: 2.015,
            25: 2.025, 30: 2.033, 35: 2.040, 40: 2.047, 45: 2.053,
            50: 2.060, 55: 2.066, 60: 2.072, 65: 2.078, 70: 2.085,
            75: 2.092, 80: 2.100, 85: 2.108, 90: 2.115, 95: 2.122,
            100: 2.130,
        },
        rin={"r0": 0.005, "temp_coeff": 0.005, "soc_coeff": 0.0010,
             "aging_coeff": 0.003},
        # VRLA 3-stage: absorption 2.40V/cell (14.4V@6S), float 2.275V/cell (13.65V@6S)
        charge=ChargeProfile(strategy="three_stage", bulk_c_rate=0.10,
                             absorption_voltage_per_cell=2.40,
                             float_voltage_per_cell=2.275,
                             tail_current_c_rate=0.02, stage_timeout_min=240.0),
        # Nernst: H₂SO₄ electrolyte OCV rises ~+0.40 mV/°C/cell from 25°C reference
        temp_coeff_mv_per_degc=0.40,
        # Peukert k≈1.3 for VRLA/AGM; capacity rated at 10-hour rate (C10)
        peukert_k=1.30,
        peukert_hr=10.0,
    ),
    "Li-ion": ChemistryProfile(
        name="Li-ion",
        ocv_curve={
            0: 3.00, 5: 3.40, 10: 3.50, 15: 3.58, 20: 3.63,
            25: 3.67, 30: 3.70, 35: 3.73, 40: 3.76, 45: 3.79,
            50: 3.82, 55: 3.85, 60: 3.88, 65: 3.92, 70: 3.96,
            75: 4.00, 80: 4.05, 85: 4.10, 90: 4.14, 95: 4.17,
            100: 4.20,
        },
        rin={"r0": 0.035, "temp_coeff": 0.004, "soc_coeff": 0.0003,
             "aging_coeff": 0.0015},
        charge=ChargeProfile(strategy="cc_cv", bulk_c_rate=0.5,
                             cv_voltage_per_cell=4.20, tail_current_c_rate=0.05,
                             stage_timeout_min=120.0),
    ),
}

# เคมีที่ใช้แทนเมื่อ battery_type ไม่รู้จัก (ตรงกับ else-branch เดิมใน battery_model)
_FALLBACK_CHEMISTRY = "Li-ion"

_DEFAULT_PRODUCTS: Dict[str, ProductProfile] = {
    "YTZ6V (12V 5Ah VRLA)": ProductProfile(
        name="YTZ6V (12V 5Ah VRLA)", chemistry="LeadAcid",
        nominal_voltage_per_cell=2.0, cells_series=6, cells_parallel=1,
        rated_capacity_ah=5.0, max_voltage_per_cell=2.45, min_voltage_per_cell=1.75,
        safety_ovp_pack=15.0, safety_uvp_pack=10.5,
        mass_grams=900.0, cca_a=100.0,
        notes="Yuasa YTZ6V มอเตอร์ไซค์ lead-acid AGM 12V 5Ah (10HR)",
    ),
    "YTZ7V (12V 7Ah VRLA)": ProductProfile(
        name="YTZ7V (12V 7Ah VRLA)", chemistry="LeadAcid",
        nominal_voltage_per_cell=2.0, cells_series=6, cells_parallel=1,
        rated_capacity_ah=7.0, max_voltage_per_cell=2.45, min_voltage_per_cell=1.75,
        safety_ovp_pack=15.0, safety_uvp_pack=10.0,
        mass_grams=2400.0, cca_a=130.0,
        notes="RB Battery YTZ7V มอเตอร์ไซค์ lead-acid AGM",
    ),
    "Generic 4S LiFePO4 (12.8V)": ProductProfile(
        name="Generic 4S LiFePO4 (12.8V)", chemistry="LiFePO4",
        nominal_voltage_per_cell=3.2, cells_series=4, cells_parallel=1,
        rated_capacity_ah=7.0, max_voltage_per_cell=3.65, min_voltage_per_cell=2.50,
        safety_ovp_pack=15.0, safety_uvp_pack=9.0,
        mass_grams=1100.0, cca_a=0.0,
        notes="แบตมอเตอร์ไซค์ lithium 4S (drop-in replacement)",
    ),
}


# ---------------------------------------------------------------------------
# Loading / merge
# ---------------------------------------------------------------------------
def _charge_from_dict(d: dict, base: ChargeProfile) -> ChargeProfile:
    """สร้าง ChargeProfile จาก dict โดยเริ่มจาก base (เติมเฉพาะ key ที่ให้มา)"""
    merged = ChargeProfile(**vars(base))
    for k, v in d.items():
        if hasattr(merged, k):
            setattr(merged, k, v)
    return merged


def _chemistry_from_dict(name: str, d: dict,
                         base: Optional[ChemistryProfile]) -> ChemistryProfile:
    base_charge = base.charge if base else ChargeProfile()
    ocv = base.ocv_curve.copy() if base else {}
    if "ocv_curve" in d:
        # รับได้ทั้ง list-of-pairs [[soc,ocv],...] และ object {"0":1.96,...}
        raw = d["ocv_curve"]
        if isinstance(raw, dict):
            ocv = {int(k): float(v) for k, v in raw.items()}
        else:
            ocv = {int(soc): float(v) for soc, v in raw}
    rin = (base.rin.copy() if base else {})
    rin.update(d.get("rin", {}))
    charge = _charge_from_dict(d.get("charge", {}), base_charge)
    tc  = d.get("temp_coeff_mv_per_degc", base.temp_coeff_mv_per_degc if base else 0.0)
    pk  = d.get("peukert_k",              base.peukert_k              if base else 1.0)
    phr = d.get("peukert_hr",             base.peukert_hr             if base else 20.0)
    return ChemistryProfile(name=name, ocv_curve=ocv, rin=rin, charge=charge,
                            temp_coeff_mv_per_degc=tc, peukert_k=pk, peukert_hr=phr)


def _load_registry() -> Tuple[Dict[str, ChemistryProfile], Dict[str, ProductProfile]]:
    """โหลด registry: เริ่มจาก default แล้ว merge ทับด้วยไฟล์ JSON ถ้ามี"""
    chemistries = {k: v for k, v in _DEFAULT_CHEMISTRIES.items()}
    products = {k: v for k, v in _DEFAULT_PRODUCTS.items()}

    if not os.path.exists(_PROFILE_FILE):
        logger.info("battery_profiles.json not found — ใช้ built-in defaults")
        return chemistries, products

    try:
        with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"โหลด battery_profiles.json ไม่ได้ ({e}) — ใช้ built-in defaults")
        return chemistries, products

    for name, d in data.get("chemistries", {}).items():
        try:
            prof = _chemistry_from_dict(name, d, chemistries.get(name))
            # validate: chemistry ใหม่ใน JSON ต้องมี ocv_curve >=2 จุด + rin.r0
            # (ไม่งั้น BatteryModel ทำ np.interp บน array ว่าง → crash)
            if len(prof.ocv_curve) < 2:
                raise ValueError("ocv_curve ต้องมีอย่างน้อย 2 จุด")
            if "r0" not in prof.rin:
                raise ValueError("rin ต้องมีคีย์ 'r0'")
            chemistries[name] = prof
        except (ValueError, TypeError) as e:
            if name in chemistries:
                logger.error(f"chemistry '{name}' ใน JSON ไม่ถูกต้อง ({e}) — ใช้ค่า built-in เดิม")
            else:
                logger.error(f"chemistry '{name}' ใน JSON ไม่ถูกต้อง ({e}) — ข้าม")

    for name, d in data.get("products", {}).items():
        try:
            products[name] = ProductProfile(name=name, **d)
        except TypeError as e:
            logger.error(f"product '{name}' ใน profile ไม่ถูกต้อง ({e}) — ข้าม")

    logger.info(f"โหลด battery profiles: {len(chemistries)} chemistries, "
                f"{len(products)} products จาก {_PROFILE_FILE}")
    return chemistries, products


_CHEMISTRIES, _PRODUCTS = _load_registry()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_CHEMISTRY_ALIASES: Dict[str, str] = {
    "Lead-Acid": "LeadAcid",
    "lead-acid": "LeadAcid",
    "lead_acid": "LeadAcid",
    "VRLA": "LeadAcid",
    "SLA": "LeadAcid",
    "AGM": "LeadAcid",
}


def get_chemistry(name: str) -> ChemistryProfile:
    """คืน ChemistryProfile ตามชื่อ; รองรับ alias เช่น 'Lead-Acid' → 'LeadAcid'"""
    resolved = _CHEMISTRY_ALIASES.get(name, name)
    prof = _CHEMISTRIES.get(resolved)
    if prof is None:
        logger.warning(f"chemistry '{name}' ไม่รู้จัก — fallback เป็น {_FALLBACK_CHEMISTRY}")
        return _CHEMISTRIES[_FALLBACK_CHEMISTRY]
    return prof


def list_chemistries() -> List[str]:
    return list(_CHEMISTRIES.keys())


def get_product(name: str) -> Optional[ProductProfile]:
    return _PRODUCTS.get(name)


def list_products() -> List[str]:
    return list(_PRODUCTS.keys())


def reload() -> None:
    """โหลด registry ใหม่จากดิสก์ (ใช้ตอนผู้ใช้แก้ไฟล์ profile ระหว่างรัน)"""
    global _CHEMISTRIES, _PRODUCTS
    _CHEMISTRIES, _PRODUCTS = _load_registry()


def save_measured_params(product_name: str, params: dict) -> bool:
    """Persist characterization results for a product in battery_profiles.json.

    params keys (all optional):
      peukert_k, peukert_k_r2, peukert_hr
      coulomb_eta_bulk, coulomb_eta_absorb, coulomb_eta_full
      ocv_curve_measured  (dict {soc_int: v_per_cell})
      measured_date       (ISO string — auto-filled if absent)

    Returns True on success.
    """
    import datetime

    data: dict = {}
    if os.path.exists(_PROFILE_FILE):
        try:
            with open(_PROFILE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            pass

    products_section = data.setdefault("products", {})
    entry = products_section.setdefault(product_name, {})
    mp = entry.setdefault("measured_params", {})
    mp.update(params)
    if "measured_date" not in mp:
        mp["measured_date"] = datetime.date.today().isoformat()

    try:
        with open(_PROFILE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        logger.info("Saved measured_params for '%s' → %s", product_name, _PROFILE_FILE)
        return True
    except Exception as exc:
        logger.error("Failed to save measured_params for '%s': %s", product_name, exc)
        return False


def get_measured_params(product_name: str) -> dict:
    """Return measured_params dict for a product entry, or {} if none stored."""
    if not os.path.exists(_PROFILE_FILE):
        return {}
    try:
        with open(_PROFILE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return (data.get("products", {})
                    .get(product_name, {})
                    .get("measured_params", {}))
    except Exception:
        return {}
