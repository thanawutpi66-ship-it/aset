"""
Analysis Module: System Identification + AI/heuristic battery grading
สำหรับ ASET Battery Characterization System

โครงสร้าง:
    RCParameters / AnalysisFeatures / AnalysisResult  — dataclasses ส่งผ่าน event
    RandlesModelExtractor   — System Identification (curve_fit 1RC)
    BatteryGrader           — AI / heuristic (pluggable, โหลด .joblib ถ้ามี)
    BatteryAnalyzer         — orchestrator (เรียกใน background thread)

หมายเหตุ dependency:
    โมดูลนี้ทำงานด้วย numpy อย่างเดียวได้ (มี fallback ในตัว)
    ถ้ามี scipy  -> ใช้ curve_fit ฟิต 1RC แม่นยำขึ้น
    ถ้ามี pandas -> โหลด CSV เร็วขึ้น (ไม่มีก็ใช้ csv ของ stdlib)
    ถ้ามี joblib + scikit-learn -> ใช้โมเดล ML, ไม่งั้น fallback เป็น heuristic
"""
import csv
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies — โหลดแบบ lazy เพื่อให้โมดูลรันได้แม้ไม่มี package เหล่านี้
# ---------------------------------------------------------------------------
try:
    from scipy.optimize import curve_fit as _scipy_curve_fit  # type: ignore
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - depends on environment
    _scipy_curve_fit = None
    _HAS_SCIPY = False

try:
    import pandas as _pd  # type: ignore
    _HAS_PANDAS = True
except Exception:  # pragma: no cover
    _pd = None
    _HAS_PANDAS = False

try:
    import joblib as _joblib  # type: ignore
    _HAS_JOBLIB = True
except Exception:  # pragma: no cover
    _joblib = None
    _HAS_JOBLIB = False


# ลำดับ feature ที่ใช้ทั้งตอน grade และตอน train (ต้องตรงกันเสมอ)
FEATURE_NAMES: Tuple[str, ...] = (
    "r0_mohm",
    "rp_mohm",
    "tau_s",
    "capacity_ah",
    "soh_pct",
    "avg_temp_c",
    "temp_rise_c",
    "energy_wh",
    "avg_voltage_v",
    "num_pulses",
)


# ===========================================================================
# Dataclasses ส่งผ่าน event
# ===========================================================================
@dataclass
class RCParameters:
    """พารามิเตอร์ 1RC Thevenin ของ pulse เดียว"""
    r0_ohm: float          # ohmic resistance (Ω)
    rp_ohm: float          # polarization resistance (Ω)
    cp_farad: float        # polarization capacitance (F)
    tau_s: float           # time constant = Rp * Cp (s)
    fit_rmse: float        # RMSE ของการฟิต (Ω)
    current_a: float       # กระแสเฉลี่ยของ pulse (A, signed)
    soc_pct: float = -1.0  # SoC ขณะเกิด pulse (%) ถ้าไม่มีข้อมูล = -1
    temp_c: float = 25.0   # อุณหภูมิเฉลี่ยของ pulse (°C)
    method: str = "numpy"  # "scipy" หรือ "numpy"


@dataclass
class AnalysisFeatures:
    """Features ที่สกัดได้จากทั้ง test ใช้ป้อนเข้า grader"""
    r0_mohm: float = 0.0       # ohmic resistance เฉลี่ย (mΩ)
    rp_mohm: float = 0.0       # polarization resistance เฉลี่ย (mΩ)
    tau_s: float = 0.0         # time constant เฉลี่ย (s)
    capacity_ah: float = 0.0   # ความจุที่วัดได้ (Ah)
    soh_pct: float = 0.0       # State of Health (%)
    avg_temp_c: float = 25.0   # อุณหภูมิเฉลี่ย (°C)
    temp_rise_c: float = 0.0   # อุณหภูมิเพิ่มขึ้นสูงสุด (°C)
    energy_wh: float = 0.0     # พลังงาน discharge (Wh)
    avg_voltage_v: float = 0.0 # แรงดันเฉลี่ย (V)
    num_pulses: int = 0        # จำนวน pulse ที่ตรวจพบ

    def to_vector(self) -> np.ndarray:
        """แปลงเป็น feature vector ตามลำดับ FEATURE_NAMES"""
        return np.array([float(getattr(self, name)) for name in FEATURE_NAMES],
                        dtype=float)


@dataclass
class AnalysisResult:
    """ผลลัพธ์รวมที่ส่งผ่าน event ไปยัง UI"""
    success: bool
    grade: str = "N/A"                 # เกรด เช่น "A", "B", "C", "D"
    confidence: float = 0.0            # ความเชื่อมั่น 0..1
    method: str = "heuristic"          # "ml" หรือ "heuristic"
    features: Optional[AnalysisFeatures] = None
    rc_params: List[RCParameters] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""
    csv_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """แปลงเป็น dict สำหรับ logging / web / serialization"""
        d = asdict(self)
        return d


# ===========================================================================
# System Identification — Randles / 1RC model extraction
# ===========================================================================
class RandlesModelExtractor:
    """สกัดพารามิเตอร์ 1RC Thevenin จาก current-step (pulse) response"""

    def __init__(self, min_current_a: float = 0.05, min_pulse_samples: int = 5):
        """
        Args:
            min_current_a    : กระแสขั้นต่ำที่ถือว่าเป็น load/charge (A)
            min_pulse_samples: จำนวน sample ขั้นต่ำต่อ pulse ที่ฟิตได้
        """
        self.min_current_a = min_current_a
        self.min_pulse_samples = min_pulse_samples

    # ---- pulse detection ---------------------------------------------------
    def detect_pulses(self, t: np.ndarray, v: np.ndarray, i: np.ndarray
                      ) -> List[Tuple[int, int]]:
        """
        ตรวจหา current step (pulse) จาก time series

        คืน list ของ (start_idx, end_idx) — index ครอบช่วงที่กระแสคงที่และไม่เป็นศูนย์
        โดย start_idx คือ sample แรกหลัง step (end_idx แบบ inclusive)
        """
        n = len(i)
        if n < self.min_pulse_samples + 1:
            return []

        abs_i = np.abs(i)
        # threshold แบบ adaptive: อย่างน้อย min_current_a หรือ 20% ของกระแสสูงสุด
        threshold = max(self.min_current_a, 0.20 * float(np.max(abs_i)))
        active = abs_i >= threshold

        pulses: List[Tuple[int, int]] = []
        idx = 1  # เริ่มที่ 1 เพื่อให้มี sample ก่อนหน้า (v_pre)
        while idx < n:
            if active[idx] and not active[idx - 1]:
                # พบ rising edge -> เริ่ม pulse
                start = idx
                i_ref = i[idx]
                end = idx
                while end + 1 < n and active[end + 1]:
                    # หยุดเมื่อกระแสเปลี่ยนทิศหรือเปลี่ยนขนาดเกิน 30%
                    if i_ref != 0 and abs(i[end + 1] - i_ref) > 0.30 * abs(i_ref):
                        break
                    end += 1
                if end - start + 1 >= self.min_pulse_samples:
                    pulses.append((start, end))
                idx = end + 1
            else:
                idx += 1

        logger.debug("detect_pulses: พบ %d pulse (threshold=%.3f A)",
                     len(pulses), threshold)
        return pulses

    # ---- single-pulse fit --------------------------------------------------
    @staticmethod
    def _model(t_rel: np.ndarray, r0: float, rp: float, tau: float) -> np.ndarray:
        """y(t) = R0 + Rp * (1 - exp(-t/tau))  (overpotential / |I|)"""
        tau = max(tau, 1e-6)
        return r0 + rp * (1.0 - np.exp(-t_rel / tau))

    def fit_pulse(self, t: np.ndarray, v: np.ndarray, i: np.ndarray,
                  start: int, end: int) -> Optional[RCParameters]:
        """
        ฟิต 1RC สำหรับ pulse ช่วง [start, end]

        ใช้แรงดันก่อน step (v_pre) เป็น baseline แล้วฟิต
            (v_pre - v(t)) / I = R0 + Rp*(1 - exp(-t/tau))
        """
        v_pre = float(v[start - 1])
        seg_t = t[start:end + 1].astype(float)
        seg_v = v[start:end + 1].astype(float)
        seg_i = i[start:end + 1].astype(float)

        i_mean = float(np.mean(seg_i))
        if abs(i_mean) < 1e-6:
            return None

        t_rel = seg_t - seg_t[0]
        if t_rel[-1] <= 0:
            return None

        # overpotential ต่อหน่วยกระแส (signed current ทำให้ R เป็นบวกทั้ง charge/discharge)
        y = (v_pre - seg_v) / i_mean

        # initial guess
        r0_guess = max(float(y[0]), 1e-4)
        rp_guess = max(float(y[-1]) - r0_guess, 1e-4)
        tau_guess = max(float(t_rel[-1]) / 3.0, 1e-3)

        method = "numpy"
        if _HAS_SCIPY:
            try:
                popt, _ = _scipy_curve_fit(
                    self._model, t_rel, y,
                    p0=[r0_guess, rp_guess, tau_guess],
                    bounds=([0.0, 0.0, 1e-3], [np.inf, np.inf, np.inf]),
                    maxfev=10000,
                )
                r0, rp, tau = (float(popt[0]), float(popt[1]), float(popt[2]))
                method = "scipy"
            except Exception as e:  # pragma: no cover - numerical edge
                logger.debug("scipy curve_fit ล้มเหลว ใช้ fallback: %s", e)
                r0, rp, tau = self._fit_numpy(t_rel, y, r0_guess, rp_guess)
        else:
            r0, rp, tau = self._fit_numpy(t_rel, y, r0_guess, rp_guess)

        # คำนวณ RMSE
        resid = y - self._model(t_rel, r0, rp, tau)
        rmse = float(np.sqrt(np.mean(resid ** 2)))

        cp = tau / rp if rp > 1e-9 else 0.0

        # อุณหภูมิเฉลี่ยของ pulse (ถ้ามี caller ส่ง temp มาแยก จะ override ภายหลัง)
        return RCParameters(
            r0_ohm=max(r0, 0.0),
            rp_ohm=max(rp, 0.0),
            cp_farad=max(cp, 0.0),
            tau_s=max(tau, 0.0),
            fit_rmse=rmse,
            current_a=i_mean,
            method=method,
        )

    @staticmethod
    def _fit_numpy(t_rel: np.ndarray, y: np.ndarray,
                   r0_guess: float, rp_guess: float) -> Tuple[float, float, float]:
        """
        Fallback fit เมื่อไม่มี scipy:
          R0  = overpotential ที่ t เล็กสุด (ohmic ทันที)
          Rp  = steady-state - R0
          tau = เวลาที่ y ถึง R0 + 0.632*Rp  (นิยาม time constant)
        """
        r0 = float(np.min(y[:max(1, len(y) // 5)]))  # ใช้ช่วงต้นเป็น ohmic
        r0 = max(r0, 0.0)
        y_ss = float(np.mean(y[-max(1, len(y) // 5):]))  # steady-state เฉลี่ยช่วงท้าย
        rp = max(y_ss - r0, 1e-4)

        target = r0 + 0.632 * rp
        tau = None
        for k in range(len(y)):
            if y[k] >= target:
                if k == 0:
                    tau = float(t_rel[0]) or 1e-3
                else:
                    # linear interpolate ระหว่างจุด k-1 กับ k
                    y0, y1 = y[k - 1], y[k]
                    t0, t1 = t_rel[k - 1], t_rel[k]
                    frac = (target - y0) / (y1 - y0) if y1 != y0 else 0.0
                    tau = float(t0 + frac * (t1 - t0))
                break
        if tau is None or tau <= 0:
            tau = float(t_rel[-1]) / 3.0 or 1e-3
        return r0, rp, tau

    def extract(self, t: np.ndarray, v: np.ndarray, i: np.ndarray,
                temp: Optional[np.ndarray] = None,
                soc: Optional[np.ndarray] = None) -> List[RCParameters]:
        """ตรวจหา pulse ทั้งหมดแล้วฟิตทีละตัว คืน list ของ RCParameters"""
        results: List[RCParameters] = []
        for (start, end) in self.detect_pulses(t, v, i):
            rc = self.fit_pulse(t, v, i, start, end)
            if rc is None:
                continue
            if temp is not None and len(temp) > end:
                rc.temp_c = float(np.mean(temp[start:end + 1]))
            if soc is not None and len(soc) > end:
                rc.soc_pct = float(np.mean(soc[start:end + 1]))
            results.append(rc)
        return results


# ===========================================================================
# Grader — ML ถ้ามีโมเดล, ไม่งั้น heuristic
# ===========================================================================
class BatteryGrader:
    """ให้เกรดแบตเตอรี่จาก features — pluggable (ML หรือ heuristic)"""

    def __init__(self, model_path: Optional[str] = None,
                 base_r0_mohm: float = 25.0):
        """
        Args:
            model_path  : path ของ .joblib (ถ้ามีและโหลดได้ จะใช้ ML)
            base_r0_mohm: R0 อ้างอิงของแบตใหม่ (mΩ) ใช้ใน heuristic
        """
        self.base_r0_mohm = base_r0_mohm
        self._model = None
        self._classes: Optional[List[str]] = None
        # auto-discover โมเดลที่เทรนไว้ ถ้าไม่ได้ระบุ path มา
        if model_path is None:
            for cand in ("grader_model.joblib",
                         os.path.join(os.path.dirname(__file__), "grader_model.joblib")):
                if os.path.exists(cand):
                    model_path = cand
                    break
        if model_path:
            self.load_model(model_path)

    def load_model(self, path: str) -> bool:
        """โหลดโมเดล RandomForest (.joblib) — คืน True ถ้าสำเร็จ"""
        if not _HAS_JOBLIB:
            logger.warning("ไม่มี joblib ติดตั้ง — ใช้ heuristic แทน")
            return False
        if not os.path.exists(path):
            logger.warning("ไม่พบไฟล์โมเดล: %s — ใช้ heuristic แทน", path)
            return False
        try:
            self._model = _joblib.load(path)
            self._classes = list(getattr(self._model, "classes_", []))
            logger.info("โหลดโมเดล grader สำเร็จจาก %s (classes=%s)",
                        path, self._classes)
            return True
        except Exception as e:
            logger.error("โหลดโมเดลล้มเหลว: %s — ใช้ heuristic แทน", e)
            self._model = None
            return False

    @property
    def has_model(self) -> bool:
        return self._model is not None

    def predict(self, features: AnalysisFeatures) -> Tuple[str, float, str]:
        """
        คืน (grade, confidence, method)
        ใช้ ML ถ้ามีโมเดล ไม่งั้น heuristic
        """
        if self.has_model:
            try:
                return self._predict_ml(features)
            except Exception as e:  # pragma: no cover
                logger.error("ML predict ล้มเหลว ใช้ heuristic: %s", e)
        return self._predict_heuristic(features)

    def _predict_ml(self, features: AnalysisFeatures) -> Tuple[str, float, str]:
        x = features.to_vector().reshape(1, -1)
        grade = str(self._model.predict(x)[0])
        confidence = 0.0
        if hasattr(self._model, "predict_proba"):
            proba = self._model.predict_proba(x)[0]
            confidence = float(np.max(proba))
        return grade, confidence, "ml"

    def _predict_heuristic(self, features: AnalysisFeatures
                           ) -> Tuple[str, float, str]:
        """
        Heuristic grading:
          - SoH เป็นตัวหลัก
          - R0 ที่สูงกว่าค่าอ้างอิง = หักคะแนน
        """
        soh = features.soh_pct
        r0 = features.r0_mohm
        base = max(self.base_r0_mohm, 1e-3)
        r0_ratio = r0 / base if r0 > 0 else 1.0

        if soh >= 90 and r0_ratio <= 1.3:
            grade = "A"
        elif soh >= 80 and r0_ratio <= 1.8:
            grade = "B"
        elif soh >= 70 and r0_ratio <= 2.5:
            grade = "C"
        else:
            grade = "D"

        # confidence: ยิ่งใกล้ขอบเขตยิ่งมั่นใจน้อย (ค่าหยาบ)
        confidence = 0.6
        return grade, confidence, "heuristic"


# ===========================================================================
# Chemistry detection (acid / lithium) — ด่านแรกของแนวคัดเกรดมอเตอร์ไซค์แบต
# ===========================================================================
@dataclass
class ChemistryResult:
    """ผลการจำแนกชนิดเคมีของแบต (12V class)"""
    chemistry: str            # "LeadAcid" | "LiFePO4" | "Li-ion" | "Unknown"
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


# ===========================================================================
# Orchestrator
# ===========================================================================
class BatteryAnalyzer:
    """รวม pulse fit + capacity + temperature -> features -> grade"""

    # ชื่อคอลัมน์มาตรฐานจาก data_utils.DataHandler
    COL_TIME = "Elapsed_s"
    COL_VOLTAGE = "Voltage_V"
    COL_CURRENT = "Current_A"
    COL_SOC = "SoC_pct"
    COL_TEMP = "Temperature_C"

    def __init__(self, rated_capacity_ah: float = 2.0,
                 base_r0_mohm: float = 25.0,
                 model_path: Optional[str] = None,
                 event_bus: Optional[Any] = None):
        """
        Args:
            rated_capacity_ah: ความจุที่ rate ไว้ ใช้คำนวณ SoH
            base_r0_mohm     : R0 อ้างอิงแบตใหม่ (mΩ) สำหรับ heuristic
            model_path       : path โมเดล .joblib (optional)
            event_bus        : EventBus สำหรับ post ANALYSIS_COMPLETED (optional)
        """
        self.rated_capacity_ah = rated_capacity_ah
        self.extractor = RandlesModelExtractor()
        self.grader = BatteryGrader(model_path=model_path,
                                    base_r0_mohm=base_r0_mohm)
        self.event_bus = event_bus

    # canonicalise header names so either schema works (DataHandler's "Voltage_V"
    # or the acquisition worker's lowercase "voltage_v"); maps lowercased → canonical.
    _CANON = {
        "elapsed_s": "Elapsed_s", "voltage_v": "Voltage_V", "current_a": "Current_A",
        "soc_pct": "SoC_pct", "temperature_c": "Temperature_C",
        "resistance_mohm": "Resistance_mOhm",
    }

    @classmethod
    def _canon_col(cls, name: str) -> str:
        return cls._CANON.get(name.strip().lower(), name)

    # ---- CSV loading -------------------------------------------------------
    def _load_csv(self, csv_path: str) -> Dict[str, np.ndarray]:
        """โหลด CSV เป็น dict ของ numpy arrays (รองรับทั้ง pandas และ stdlib);
        normalise ชื่อคอลัมน์ + coerce ค่าที่ไม่ใช่ตัวเลข (เช่นคอลัมน์ 'mode') เป็น NaN"""
        if _HAS_PANDAS:
            df = _pd.read_csv(csv_path, encoding="utf-8-sig")
            return {self._canon_col(col): _pd.to_numeric(df[col], errors="coerce")
                    .to_numpy(dtype=float)
                    for col in df.columns if col.strip().lower() != "timestamp"}

        # fallback: stdlib csv
        cols: Dict[str, List[float]] = {}
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = [h for h in (reader.fieldnames or []) if h.strip().lower() != "timestamp"]
            keys = {h: self._canon_col(h) for h in headers}
            for h in headers:
                cols[keys[h]] = []
            for row in reader:
                for h in headers:
                    try:
                        cols[keys[h]].append(float(row[h]))
                    except (ValueError, TypeError, KeyError):
                        cols[keys[h]].append(np.nan)
        return {k: np.asarray(v, dtype=float) for k, v in cols.items()}

    # ---- feature computation ----------------------------------------------
    @staticmethod
    def _capacity_and_energy(t: np.ndarray, v: np.ndarray, i: np.ndarray
                             ) -> Tuple[float, float]:
        """
        coulomb counting ของช่วง discharge (กระแสเป็นบวก = discharge ตาม convention โปรเจกต์)
        คืน (capacity_ah, energy_wh)
        """
        if len(t) < 2:
            return 0.0, 0.0
        dt = np.diff(t)  # วินาที
        i_mid = (i[1:] + i[:-1]) / 2.0
        v_mid = (v[1:] + v[:-1]) / 2.0
        # นับเฉพาะ discharge (i > 0)
        discharge = i_mid > 0
        ah = float(np.sum(i_mid[discharge] * dt[discharge]) / 3600.0)
        wh = float(np.sum(i_mid[discharge] * v_mid[discharge] * dt[discharge]) / 3600.0)
        return max(ah, 0.0), max(wh, 0.0)

    def _build_features(self, data: Dict[str, np.ndarray],
                        rc_params: List[RCParameters]) -> AnalysisFeatures:
        t = data.get(self.COL_TIME)
        v = data.get(self.COL_VOLTAGE)
        i = data.get(self.COL_CURRENT)
        temp = data.get(self.COL_TEMP)

        feats = AnalysisFeatures(num_pulses=len(rc_params))

        if rc_params:
            feats.r0_mohm = float(np.median([rc.r0_ohm for rc in rc_params])) * 1000.0
            feats.rp_mohm = float(np.median([rc.rp_ohm for rc in rc_params])) * 1000.0
            feats.tau_s = float(np.median([rc.tau_s for rc in rc_params]))

        if t is not None and v is not None and i is not None:
            cap, energy = self._capacity_and_energy(t, v, i)
            feats.capacity_ah = cap
            feats.energy_wh = energy
            feats.avg_voltage_v = float(np.nanmean(v)) if len(v) else 0.0
            feats.soh_pct = (cap / self.rated_capacity_ah * 100.0
                             if self.rated_capacity_ah > 0 else 0.0)
            feats.soh_pct = float(max(0.0, min(120.0, feats.soh_pct)))

        if temp is not None and len(temp) and not np.all(np.isnan(temp)):
            feats.avg_temp_c = float(np.nanmean(temp))
            feats.temp_rise_c = float(np.nanmax(temp) - np.nanmin(temp))

        return feats

    # ---- main entry --------------------------------------------------------
    def analyze(self, csv_path: str) -> AnalysisResult:
        """
        วิเคราะห์ไฟล์ CSV หนึ่งไฟล์ -> AnalysisResult
        ออกแบบให้เรียกใน background thread ได้ (ไม่ throw, คืน result เสมอ)
        """
        notes: List[str] = []
        if not _HAS_SCIPY:
            notes.append("ไม่มี scipy — ใช้ตัวฟิต 1RC แบบ numpy fallback")
        if not self.grader.has_model:
            notes.append("ไม่มีโมเดล ML — ใช้ heuristic grading")

        try:
            if not os.path.exists(csv_path):
                raise FileNotFoundError(csv_path)

            data = self._load_csv(csv_path)

            required = (self.COL_TIME, self.COL_VOLTAGE, self.COL_CURRENT)
            missing = [c for c in required if c not in data]
            if missing:
                raise ValueError(f"CSV ขาดคอลัมน์: {', '.join(missing)}")

            t = data[self.COL_TIME]
            v = data[self.COL_VOLTAGE]
            i = data[self.COL_CURRENT]

            if len(t) < self.extractor.min_pulse_samples + 1:
                raise ValueError("ข้อมูลน้อยเกินไปสำหรับการวิเคราะห์")

            rc_params = self.extractor.extract(
                t, v, i,
                temp=data.get(self.COL_TEMP),
                soc=data.get(self.COL_SOC),
            )
            if not rc_params:
                notes.append("ไม่พบ current pulse — R0/Rp/tau จะเป็น 0")

            features = self._build_features(data, rc_params)
            grade, confidence, method = self.grader.predict(features)

            result = AnalysisResult(
                success=True,
                grade=grade,
                confidence=confidence,
                method=method,
                features=features,
                rc_params=rc_params,
                notes=notes,
                csv_path=csv_path,
            )
            logger.info("วิเคราะห์เสร็จ: grade=%s (%.0f%%, %s), pulses=%d, SoH=%.1f%%",
                        grade, confidence * 100, method,
                        features.num_pulses, features.soh_pct)
        except Exception as e:
            logger.error("วิเคราะห์ล้มเหลว (%s): %s", csv_path, e)
            result = AnalysisResult(
                success=False,
                error=str(e),
                notes=notes,
                csv_path=csv_path,
            )

        self._post_event(result)
        return result

    def _post_event(self, result: AnalysisResult) -> None:
        """ส่งผลผ่าน EventBus ถ้ามี (ไม่ทำให้ analyze ล้มถ้า post พัง)"""
        if self.event_bus is None:
            return
        try:
            # import แบบ lazy เพื่อตัด circular import
            from aset_batt.services.event_system import Event, EventType
            self.event_bus.post_event(
                Event(EventType.ANALYSIS_COMPLETED, data=result)
            )
        except Exception as e:  # pragma: no cover
            logger.debug("post ANALYSIS_COMPLETED ล้มเหลว: %s", e)
