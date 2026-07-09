"""
Analysis Module: Chemistry Detection
สำหรับ ASET Battery Characterization System

Note: The advanced AI grading features (BatteryAnalyzer, BatteryGrader, 
RandlesModelExtractor) have been removed as they were not wired into the main 
application pipeline.

โมดูลนี้เหลือเพียง `ChemistryDetector` ซึ่งถูกใช้จริงโดย UI (isa101_views).
"""
import logging
from dataclasses import dataclass, field
from typing import List, Tuple

logger = logging.getLogger(__name__)

@dataclass
class ChemistryResult:
    chemistry: str
    confidence: float
    rested_ocv_full: float
    mid_slope_v_per_pct: float
    notes: List[str] = field(default_factory=list)

class ChemistryDetector:
    """แยกชนิดเคมีของแบตคลาส 12V จากลายเซ็นไฟฟ้า (rule-based heuristic)

    ตัวแยกหลัก = rested OCV ตอนชาร์จเต็ม (วัดหลัง full charge + พักให้ relax):
        LiFePO4 (4S)  → ~13.3–13.6V  (เส้นกลาง flat มาก)
        Lead-acid(6S) → ~12.6–12.9V  (sloped)
        Li-ion (3S)   → ~12.0–12.5V  (sloped)
    เสริมด้วย mid-slope (LFP flatter) เพื่อเพิ่มความมั่นใจ

    NB: heuristic — ควร calibrate ด้วยข้อมูลจริง; กรณี Li-ion(3S) กับ lead-acid
    ที่คายประจุแล้วแรงดันใกล้กัน → ก้ำกึ่ง (กรณีจริงของมอเตอร์ไซค์มักเป็น
    lead-acid vs LiFePO4-4S ซึ่งแยกกันชัดด้วย rested OCV)
    """

    def detect(self, rested_ocv_full: float,
               mid_slope_v_per_pct: float) -> ChemistryResult:
        notes: List[str] = []
        if rested_ocv_full >= 13.1:
            chemistry = "LiFePO4"
            confidence = 0.90 if mid_slope_v_per_pct < 0.012 else 0.75
            notes.append("OCV เต็มสูง (~4S) + เส้น flat → LiFePO4")
        elif rested_ocv_full >= 12.55:
            chemistry = "LeadAcid"
            confidence = 0.80
            notes.append("OCV เต็ม ~12.6–12.9V (6S×2V) → lead-acid")
        elif rested_ocv_full >= 11.5:
            chemistry = "Li-ion"
            confidence = 0.55
            notes.append("OCV เต็ม ~12.0–12.5V (3S) → Li-ion (ก้ำกึ่งกับ lead-acid ที่คายแล้ว)")
        else:
            chemistry = "Unknown"
            confidence = 0.30
            notes.append("OCV ต่ำผิดปกติ — อาจคายหมดหรือไม่ใช่คลาส 12V")
        return ChemistryResult(chemistry, confidence,
                               rested_ocv_full, mid_slope_v_per_pct, notes)

    @staticmethod
    def features_from_model(model) -> Tuple[float, float]:
        """ดึง (rested_ocv_full, mid_slope) ระดับแพ็คจาก BatteryModel (สำหรับจำลอง/ทดสอบ)"""
        v_full = model.get_ocv_from_soc(100.0)
        slope = abs(model.get_ocv_from_soc(80.0) - model.get_ocv_from_soc(20.0)) / 60.0
        return v_full, slope
