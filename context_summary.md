# ASET Battery Project — Context Summary

สรุปสถานะโปรเจกต์ ณ **2026-06-21** (architecture ล่าสุด, การ pivot, และ to-do)
ใช้เป็นเอกสาร handoff / ทำความเข้าใจร่วมกัน

---

## 1. โปรเจกต์นี้คืออะไร (หลัง pivot)
**ระบบทดสอบและคัดเกรดแบตเตอรี่อัตโนมัติ (หลายเคมี)** ควบคุมด้วย Python —
มุ่งที่ **แบตมอเตอร์ไซค์ 12V** (lead-acid AGM เป็นหลัก เช่น YTZ7V, Little Bee MV20-12; อาจรวม lithium 4S):
- ควบคุม DC Power Supply + Electronic Load ผ่าน **SCPI/PyVISA**
- อ่านอุณหภูมิจาก **MLX90614 → ESP32 → UART** (ปัจจัยภายนอกร่วมวิเคราะห์)
- สกัด **แบบจำลองวงจรสมมูล 1-RC Thevenin (R0/R1/C1)** จาก HPPC + คำนวณ **SoC/SoH/DCIR**
- **คัดเกรดดี/เสีย (Grade A/B/C/Reject)** อัตโนมัติ + **แยกชนิดเคมี** (ChemistryDetector)
- GUI **PySide6 ISA-101** desktop + บันทึก CSV + PDF report + cloud dashboard

> **มหาวิทยาลัยอุบลราชธานี — Capstone Design วิศวกรรมไฟฟ้า (A19/2568)** ทีม 5 คน
> อาจารย์ที่ปรึกษา: ดร.อาทิตย์ ฤทธิแผลง / ผศ.ดร.บงกช สุขอนันต์

---

## 2. การ Pivot (เปลี่ยนแนวทาง) — สำคัญ

| หัวข้อ | เดิม | ใหม่ (ปัจจุบัน) |
|---|---|---|
| DUT | แพ็ค LiFePO4 **8S 25.6V** | **แบตมอเตอร์ไซค์ 12V** (lead-acid / lithium) |
| จุดขาย | เก็บข้อมูล **75 Hz** จับ Ohmic Drop + ตัดไฟเร็ว ms | **คัดเกรดอัตโนมัติหลายเคมี** (AI แยกชนิด + ดี/เสีย) + **สกัด 1-RC ECM** |
| อุปกรณ์ | ต้องซื้อเพิ่ม (INA226, contactor) | **ใช้ของเดิมล้วน** (เพิ่มแค่ MLX90614 + ESP32) |

**ตัดออก (แจ้งอาจารย์แล้ว/แก้ objective แล้ว):** 75 Hz high-rate, active cutoff เร็ว ms
→ เหลือ **software cutoff (SCPI OFF) + MCB passive backstop** · 📄 [docs/project_pivot.md](docs/project_pivot.md)

---

## 3. Architecture ล่าสุด — แพ็กเกจ `aset_batt/` (single PySide6 app)

> **เปลี่ยนใหญ่จาก session นี้:** จาก flat ~30 ไฟล์ → **package แบบ layered**, GUI เหลือ **PySide6 ตัวเดียว**
> (ลบ Tkinter + retire PyQt6), และรวม acquisition engine เป็นแพ็กเกจเดียว

```
ASET_BATT/
├── main.py                  # shim → aset_batt.app.run  (หรือ python -m aset_batt)
├── config.json              # runtime config (cwd-relative)
├── aset_batt/
│   ├── app/        run.py · app_bootstrapper.py · auto_controller.py
│   ├── core/       battery_model · state_estimator · charge_controller · analysis_module
│   │               iec61960_standard · battery_profiles(+json) · config · parameter_id
│   ├── hardware/   hardware_driver(HAL) · mock_hardware
│   ├── acquisition/  models · backends · analytics · analysis · worker   ← engine รวม
│   ├── ui/         isa101_views.py (ISA-101 HMI) · logos
│   ├── services/   event_system · service_locator · logging_config · exceptions
│   ├── storage/    data_utils(CSV) · report_generator(PDF) · cloud_push
│   └── web/        web_server.py
├── scripts/        generate_sample_data · train_grader · make_training_data
├── tests/ (64 passed) · docs/ · cloud_dashboard/ · pyproject.toml
```

### โมดูล/เลเยอร์หลัก
| โมดูล | หน้าที่ |
|---|---|
| `acquisition/worker.py` | **`AcquisitionWorker` (QThread)** ยึด I/O ฮาร์ดแวร์ (mutex กั้น) + log CSV high-rate; E-Stop ตัดไฟทันทีโดยไม่รอ loop; รับ `StateEstimator` ให้ SoC/SoH สด; เรียก unified analysis ตอนจบ |
| `acquisition/backends.py` | **`HardwareBackend`** (ขับ HAL จริง = SCPI/VISA + ESP32 temp) · `VisaSerialBackend` (อ้างอิงตรง) · *(ลบ SimulatedBackend แล้ว — no-hw ใช้ HardwareBackend+Mock)* |
| `acquisition/analysis.py` | **วิธีวิเคราะห์เดียวทั้งระบบ** `analyze_series()` / `analyze_csv()` + `profile_from_config()` — ใช้ทั้ง worker, ปุ่ม Analyze CSV, และ IEC auto-analyze |
| `acquisition/analytics.py` | HPPC Rᵢ (fallback), ICA dQ/dV, DTV dT/dV (Gaussian), **two-resistance grading** `grade_from_ecm(soh, R0, R1)` |
| `core/parameter_id.py` | **`BatteryParameterIdentifier`** — สกัด 1-RC ECM (R0 จากสเต็ป, R1/C1 ฟิตด้วย scipy curve_fit/Trust-Region-Reflective, bounds>0) + plot R²/RMSE |
| `core/charge_controller.py` | charge state machine: 3-stage (lead-acid) / CC-CV (lithium); strategy override ได้ (Auto/CC-CV/3-Stage) |
| `core/battery_profiles.py(+json)` | ฐานข้อมูลโปรไฟล์ chemistry + **products** (YTZ7V, Generic 4S LiFePO4, **Little Bee MV20-12**) |
| `core/battery_model.py` · `state_estimator.py` | OCV/Rin หลายเคมี + pack scaling · SoC coulomb+OCV+EMA |
| `app/auto_controller.py` | orchestrator: monitor loop, charge, IEC 61960 tests (two-pulse DCIR), auto-analyze |
| `ui/isa101_views.py` | GUI ISA-101 เดียว: cards V/I/SoC/Rin/Temp/SoH · multi-axis trend · Diagnostics(ICA/DTV) · grade badge · E-Stop · charge mode + test mode dropdown |
| `hardware/mock_hardware.py` | HAL mock — มีโมเดล overpotential (R0 step + RC + relaxation tail) ให้ทดสอบ HPPC แบบไม่มีฮาร์ดแวร์ได้ |

---

## 4. Config ปัจจุบัน (`config.json`)
- default: **LiFePO4 4S (12.8V)** — max 3.65/cell, min 2.5/cell, rated 7.0Ah, safety UVP 9.0V
  *(เลือก product อื่นจาก dropdown ได้: YTZ7V lead-acid 6S, Little Bee MV20-12 lead-acid 6S 20Ah)*
- **simulation_mode: true** (ใช้ MockHardwareController) — ตั้ง `false` เพื่อรันฮาร์ดแวร์จริง
- web_server เปิด (8000), auto-push cloud เปิด

---

## 5. ⚠️ สถานะข้อมูล — **ยังเป็น simulation ล้วน ยังไม่เคยรันแบตจริง**
ค่า R0/R1/C1/SoH/grade ที่เห็นในจอ/ที่ผ่านมา **มาจาก MockHardwareController** ที่ตั้งค่าไว้เอง
(`_mock_r0=30 mΩ, _mock_r1=20 mΩ, τ=12 s`) — เป็นการพิสูจน์ว่า **ซอฟต์แวร์คำนวณถูก** (ป้อนค่าเข้า ฟิตได้ค่ากลับ)
**ไม่ใช่ค่าจริงของแบต** จนกว่าจะ `simulation_mode:false` + ต่อ PEL-3111/PSW จริง + กด RUN TEST กับแบตจริง

---

## 6. สิ่งที่ทำเสร็จแล้ว (history โดยย่อ)
- ✅ **Restructure → package `aset_batt/`** (layered) + pyproject.toml; ลบ flat layout
- ✅ **GUI เหลือ PySide6 ISA-101 ตัวเดียว** (ลบ Tkinter, retire PyQt6, ลบ command_center bench)
- ✅ **Unified acquisition engine** (`aset_batt/acquisition/`): worker(QThread) + HardwareBackend จริง + วิธีวิเคราะห์เดียว
- ✅ **1-RC ECM identifier** (`parameter_id.py`) — ฟิต R0/R1/C1/τ (recover ค่าสังเคราะห์แม่นยำ, R²≈0.998)
- ✅ **ต่อ ECM เข้า HPPC** + **two-resistance grading** (R0 ohmic/contact + R1 charge-transfer/SEI)
- ✅ **HPPC pulse/relaxation duration ปรับได้** (per-profile) — แก้ปัญหาพัลส์สั้นทำให้ R1 เพี้ยน
- ✅ Battery profile DB + charge state machine (3-stage/CC-CV) + chemistry-aware charge dropdown
- ✅ ICA/DTV diagnostics + PDF report + Analyze-CSV ใช้ไฟล์ test ล่าสุด + canonical CSV schema
- ✅ DCIR two-pulse (IEC 61960), SoC estimator correctness, Cloud dashboard (Azure) + auto-push
- ✅ **tests: 64 passed**
- ✅ แก้ฟอร์มเสนอหัวข้อ capstone (.docx) หัวข้อ 2/3/4 + เพิ่มหัวข้อ 8 Validation → ตรงกับ pivot
  (ไฟล์: `Downloads/ฟอร์มรายละเอียดหัวข้อโปรเจค (แก้ไข2).docx`)

📄 สถาปัตยกรรมเชิงลึก: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 7. เป้าหมาย & To-do List

### 🔜 งานหลัก (คัดดี/เสีย lead-acid)
- [ ] **แก้ค่า YTZ7V ใน profile/config**: ปัจจุบัน 7Ah/CCA 130 — datasheet Yuasa จริง = **6.3Ah(20HR)/CCA 105A**
- [ ] **SequenceWorker / TestProtocol** (ที่ทีมเสนอ — *ยังไม่ได้ทำ*): ร้อยไซเคิลเดียวอัตโนมัติ
      charge→rest→HPPC→discharge(capacity)→analyze โดย**ดึงแรงดัน/กระแสจาก profile** (ไม่ hardcode)
      + pause/resume + E-Stop ทุกสเตท → แก้ปัญหาแผง OPERATIONS ที่ปุ่มซ้อนทับกัน
- [ ] grader: เพิ่ม feature CCA / voltage-sag + retune เกณฑ์ lead-acid
- [ ] wire `ChemistryDetector` เข้าไปป์ไลน์อัตโนมัติ (detect→เลือก profile/safety)

### 🧪 Validation (มี reference จริงแล้ว — ดู §8)
- [ ] เก็บ **dataset แบตดี/เสียหลายลูก** (lead-acid + lithium)
- [ ] เทียบ **R0 ↔ ACIR ของ GBM-3080/HRM-10**, **capacity ↔ discharge PEL-3111** vs datasheet/IEC
- [ ] R1/C1: ไม่มีเครื่องเทียบตรง → validate ด้วย R²≥0.95 + repeatability (ระบุเป็นข้อจำกัด)

### 📋 บริหาร/รายงาน
- [ ] ตรวจฉลากแบตจริง (โดยเฉพาะ Little Bee MV20-12 — ตอนนี้ใช้ค่ากลางคลาส SLA)
- [ ] อัปเดตหัวข้อ 6 (deliverable: Tkinter→PySide6) + หัวข้อ 7 (ตัด Active Safety) ในฟอร์ม .docx

### 🔐 Security (ค้างจาก session ก่อน)
- [ ] **revoke/rotate Gmail app password** + **rotate cloud ingest token** ที่หลุดในแชต

---

## 8. ฮาร์ดแวร์ + เครื่องอ้างอิง (validation)

### DUT + อุปกรณ์ควบคุม
| อุปกรณ์ | spec |
|---|---|
| แบต DUT | **YTZ7V** (12V, 6.3Ah/20HR, CCA 105A, AGM) · **Little Bee MV20-12** (12V 20Ah SLA) |
| DC Load | GW Instek **PEL-3111** (1.5–150V, 210A, 1050W, SCPI) |
| DC Supply | GW Instek **PSW/PSB-1080L** (0–80V, 0–40.5A, source-only) |
| Temp | **MLX90614 / GY-906** (IR, ±0.5°C, I2C) |
| MCU / Breaker | **ESP32** (temp+UART) · **LUMIRA MCB** (passive overcurrent) |

### เครื่องอ้างอิงสำหรับ validate (ทีมมีอยู่)
| เครื่อง | วัด | ใช้เทียบ |
|---|---|---|
| **GW Instek GBM-3080** | ACIR (AC 1kHz, 4-สาย) + OCV + sorting | **R0 (ohmic, ACIR≈R0)** + OCV + คัดเกรด |
| **FNIRSI HRM-10** | ACIR (AC 1kHz, 4-สาย) ±0.5% + OCV + Pass/Fail | R0 + OCV (consumer-grade, cross-check) |

> ⚠️ ทั้งสองเป็น **AC 1kHz → ได้แค่ R0** ไม่ได้ DCIR(R0+R1)/R1/C1/ความจุ ·
> **ความจุ/SoH** ต้อง discharge จริงด้วย PEL-3111 เทียบ datasheet/IEC 61960 ·
> อย่าเขียน "เทียบ DCIR กับ GBM" (มันวัด ACIR คนละนิยาม)

**ข้อจำกัดที่ต้องจำ:** SCPI readback ~5 Hz · MLX90614 ช้า ~240ms · MCB สั่ง trip ด้วย ESP32 ไม่ได้

---

## 9. การรัน & ทดสอบ
```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt     # PySide6/pyqtgraph/reportlab + scipy/pandas/sklearn/joblib
python main.py                      # GUI (PySide6 ISA-101) — หรือ python -m aset_batt
pytest -q                           # 64 passed
```
ใช้จริง: ตั้ง `config.json` → `"simulation_mode": false` → Connect พอร์ต PSU/Load/ESP32 จริง → RUN TEST

---

## 10. เอกสารอ้างอิงใน repo
- [ARCHITECTURE.md](ARCHITECTURE.md) — สถาปัตยกรรมเชิงลึก + threading + tech-debt
- [docs/project_pivot.md](docs/project_pivot.md) — รายละเอียดการ pivot
- [docs/limitations_future_work.md](docs/limitations_future_work.md) — ข้อจำกัด + future work
- [cloud_dashboard/README.md](cloud_dashboard/README.md) — cloud dashboard
