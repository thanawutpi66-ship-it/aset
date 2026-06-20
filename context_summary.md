# ASET Battery Project — Context Summary

สรุปสถานะโปรเจกต์ ณ ปัจจุบัน (architecture ล่าสุด, การ pivot, และ to-do)
ใช้เป็นเอกสาร handoff / ทำความเข้าใจร่วมกัน

---

## 1. โปรเจกต์นี้คืออะไร (หลัง pivot)
**ระบบทดสอบและคัดเกรดแบตเตอรี่อัตโนมัติ (หลายเคมี)** ควบคุมด้วย Python —
ปัจจุบันมุ่งไปที่ **แบตมอเตอร์ไซค์ 12V** (lead-acid AGM เป็นหลัก เช่น RB Battery YTZ7V 12V 7Ah,
อาจรวม lithium 4S) โดย:
- ควบคุม DC Power Supply + Electronic Load ผ่าน **SCPI/PyVISA**
- อ่านอุณหภูมิจาก **MLX90614 → ESP32 → UART**
- คำนวณ **DCIR / SoC / SoH** + **แยกชนิดเคมี (AI ด่าน 1)** + **คัดเกรดดี/เสีย (AI ด่าน 2)**
- แสดงผล GUI (PySide6 desktop) + บันทึก CSV + dashboard (local + cloud)

> **มหาวิทยาลัยอุบลราชธานี — Capstone Design วิศวกรรมไฟฟ้า (A19/2568)** ทีม 5 คน
> อาจารย์ที่ปรึกษา: ดร.อาทิตย์ ฤทธิแผลง / ผศ.ดร.บงกช สุขอนันต์

---

## 2. การ Pivot (เปลี่ยนแนวทาง) — สำคัญ

| หัวข้อ | เดิม | ใหม่ (ปัจจุบัน) |
|---|---|---|
| DUT | แพ็ค LiFePO4 **8S 25.6V** | **แบตมอเตอร์ไซค์ 12V** (lead-acid / lithium) |
| จุดขาย | เก็บข้อมูล **75 Hz** จับ Ohmic Drop + ตัดไฟเร็ว ms | **คัดเกรดอัตโนมัติหลายเคมี** (AI แยกชนิด + ดี/เสีย) |
| อุปกรณ์ | ต้องซื้อเพิ่ม (INA226, contactor) | **ใช้ของเดิมล้วน ไม่ซื้อเพิ่ม** |

**เหตุผลที่ pivot:**
1. **75 Hz ผ่าน SCPI ทำไม่ได้จริง** (ได้ ~5 Hz) — ต้องซื้อ INA226+ESP32 high-rate ถึงจะถึง → ทีมเลือกไม่ซื้อ
2. อาจารย์เสนอทดสอบ**แบตมอเตอร์ไซค์ปกติ** — เล็ก/ปลอดภัยกว่า, หาแบตดี/เสียหลายลูกได้ถูก (= dataset)
3. **แนวคัดเกรดไม่ต้องการ 75 Hz** (ใช้ SoH+DCIR+OCV+CCA ที่วัด 5 Hz ได้) → ตัด 75 Hz แล้วสอดคล้อง
4. lead-acid **OCV ลาดชัน** → SoC จาก OCV ง่าย/แม่นกว่า LFP

**สิ่งที่ตัดออก (ต้องแจ้งอาจารย์ + แก้ objective):**
- 75 Hz high-rate / แยก R0–Rp ละเอียดด้วย 1RC
- Active cutoff เร็ว ms → เหลือ **software cutoff (SCPI OFF) + MCB เป็น passive backstop**

📄 รายละเอียด: [docs/project_pivot.md](docs/project_pivot.md)

---

## 3. Architecture ล่าสุด (อุปกรณ์เดิม)
```
[แบตมอไซค์ 12V (DUT)]
  ├─ charge    ← GW Instek PSW/PSB-1080L (SCPI/PyVISA)
  ├─ discharge ← GW Instek PEL-3111 (SCPI) + อ่าน V/I (~5 Hz, calibrated)
  ├─ temp      ← MLX90614 → ESP32 → UART → PC
  └─ ตัดวงจร   ← software (SCPI OFF) + MCB LUMIRA (passive)
                   │
            [PC / Python]
   System A: GUI (PySide6)            ← ui/qt_views.py
   System B: Central Control          ← auto_controller.py
   System C: Acquisition + HAL        ← hardware_driver.py / mock_hardware.py
   System D: Processing+Logging+Safety+Web ← state_estimator, data_utils,
                                            web_server, cloud_push
```
- **Cross-cutting:** event_system, service_locator, config, logging_config, exceptions
- บทบาท **ESP32 เหลือแค่อ่านอุณหภูมิ** (ตัดงาน high-rate ออกแล้ว)

### โมดูลหลัก
| ไฟล์ | หน้าที่ |
|---|---|
| `battery_profiles.py` / `battery_profiles.json` | **ฐานข้อมูลโปรไฟล์แบต** (chemistry: OCV/Rin/charge-strategy + products เช่น YTZ7V) โหลดจาก JSON + built-in fallback; แทน hardcode if/elif เดิม |
| `charge_controller.py` | **state machine การชาร์จ**: 3-stage (lead-acid Bulk→Absorption→Float) / CC-CV (lithium); `decide()` pure + `run()` loop; PSU ทำ CC↔CV เองผ่าน `set_psu_cccv` |
| `battery_model.py` | OCV/Rin หลายเคมี: LiPO, LiFePO4, Li-ion, **LeadAcid** (ดึงจาก `battery_profiles`); pack scaling (series/parallel); แก้ทฤษฎีแล้ว (OCV temp-independent, Rin Arrhenius, plateau guard); expose `charge_profile` |
| `state_estimator.py` | SoC = coulomb counting + OCV correction + EMA (coulombic charge-only, plateau guard, dt จริง) |
| `analysis_module.py` | `RandlesModelExtractor` (1RC fit), `BatteryGrader` (heuristic+ML auto-load), `BatteryAnalyzer`, **`ChemistryDetector`** |
| `auto_controller.py` | orchestrator: monitor loop, profile, IEC tests (**two-pulse DCIR**), auto-analyze |
| `iec61960_standard.py` | DCIR two-pulse, capacity, energy density, cycle-life |
| `hardware_driver.py` / `mock_hardware.py` | HAL — SCPI/VISA + ESP32 temp (สลับด้วย simulation_mode) |
| Local web dashboard removed; cloud_dashboard + cloud_push used for remote viewing |
| `cloud_dashboard/` + `cloud_push.py` | cloud service (Azure) + auto-push จากเครื่องแล็บ |
| `train_grader.py` / `make_training_data.py` | เทรน ML grader (RandomForest) |
| `generate_sample_data.py` | สร้างข้อมูลจำลอง (config-driven) |

---

## 4. Config ปัจจุบัน (`config.json`)
- **battery_type: LeadAcid**, 6S×2V → **pack 12.0V nominal** (max 14.7V / min 10.5V), rated 7.0Ah
- **safety_limits (pack): OVP 15.0V / UVP 10.0V / 60°C / 30A**
- simulation_mode: true, web_server: เปิด (port 8000), **auto-push cloud: เปิด**

---

## 5. สิ่งที่ทำเสร็จแล้ว (history โดยย่อ)
- ✅ โมเดล/ทฤษฎีถูกต้อง: pack scaling, OCV temp, Rin Arrhenius, plateau guard, safety window
- ✅ DCIR **two-pulse ตาม IEC 61960** Clause 6.4
- ✅ SoC estimator แก้ correctness (coulombic, temp forward, dt, plateau)
- ✅ AI grader: heuristic + **RandomForest** (auto-load `.joblib`) + auto-analyze หลัง test
- ✅ Cloud dashboard 24/7 บน **Azure** (`aset-batt-dashboard.azurewebsites.net`) + **auto-push** + Tailscale Funnel
- ✅ ตรวจความถูกต้องเทียบงานวิจัย + เอกสาร [docs/limitations_future_work.md](docs/limitations_future_work.md)
- ✅ **Pivot → lead-acid 12V + ChemistryDetector** (multi-chemistry)
- ✅ **tests: 26 passed**

📄 สถาปัตยกรรมเชิงลึก: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 6. เป้าหมาย & To-do List (กำลังจะทำต่อ)

### 🔜 Batch ถัดไป — งานคัดดี/เสีย (lead-acid)
- [ ] **grader: เพิ่ม feature CCA / voltage-sag under load** + retune เกณฑ์ดี/เสียสำหรับ lead-acid (SoH + IR)
- [ ] **wire `ChemistryDetector` เข้าไปป์ไลน์**: detect ชนิด → เลือก profile/safety อัตโนมัติ
- [ ] **regenerate sample data เป็น 12V** + ปรับ DCIR step สำหรับมอเตอร์ไซค์
- [ ] map `read_vi()`/HAL เข้ากับ **SCPI จริงของ PEL-3111 / PSW-1080L** ที่ 12V

### 🧪 Validation
- [ ] เก็บ **dataset แบตมอไซค์ดี/เสีย หลายลูก** (lead-acid + lithium)
- [ ] เทียบ DCIR/SoH กับ **GW Instek (reference ฟรี)** + datasheet YTZ7V (CCA, 7Ah) + (option) Hioki BT355x
- [ ] ตัวชี้วัด: แยกชนิดถูก > X%, คัดดี/เสียตรง > X%, DCIR/SoH error < Y%

### 📋 งานบริหาร/รายงาน
- [ ] **คุยอาจารย์**: แก้ objective (ตัด 75 Hz/active-cutoff, ใส่ multi-chemistry grading)
- [ ] **ตรวจฉลากแบต** → กำหนด scope (acid อย่างเดียว หรือ acid+lithium)
- [ ] firmware ESP32 เหลือแค่อ่าน temp (งานเบาลง)

### 🔐 Security (ค้างจาก session ก่อน)
- [ ] **revoke/rotate Gmail app password** ที่หลุดในแชต
- [ ] **rotate cloud ingest token** ที่โผล่ในแชต

### 🔮 Future work (ตามงานวิจัย — ใส่ในรายงาน)
- OCV hysteresis (LFP), adaptive SoC (EKF/UKF), Rin Arrhenius เต็มรูป, เทรน grader ด้วยข้อมูลจริง

---

## 7. ฮาร์ดแวร์จริง (ที่มีอยู่)
| อุปกรณ์ | spec |
|---|---|
| แบต DUT | มอเตอร์ไซค์ 12V (เช่น RB Battery YTZ7V 12V 7Ah, lead-acid AGM) |
| DC Load | GW Instek **PEL-3111** (1.5–150V, 210A, 1050W, slew 16A/µs, SCPI) |
| DC Supply | GW Instek **PSW/PSB-1080L** (0–80V, 0–40.5A, 1080W, source-only) |
| Temp | **MLX90614 / GY-906** (IR, ±0.5°C, I2C, response ~240ms) |
| MCU | **ESP32** (อ่าน temp + UART) |
| Breaker | **LUMIRA 2P 1000V 100A** (MCB — passive overcurrent) |

**ข้อจำกัดที่ต้องจำ:** SCPI readback ~5 Hz · MLX90614 ช้า · MCB สั่ง trip ด้วย ESP32 ไม่ได้

---

## 8. การรัน & ทดสอบ
```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt          # + scipy/pandas/scikit-learn/joblib (optional, ติดตั้งแล้ว)
python main.py                           # รันแอป (GUI) ตรงบน host
python generate_sample_data.py           # สร้างข้อมูลจำลอง (ตอนนี้ = lead-acid 12V)
pytest                                   # 26 passed
```
- Cloud: ดู [cloud_dashboard/DEPLOY_AZURE.md](cloud_dashboard/DEPLOY_AZURE.md)

---

## 9. เอกสารอ้างอิงใน repo
- [ARCHITECTURE.md](ARCHITECTURE.md) — สถาปัตยกรรมเชิงลึก + threading + tech-debt
- [docs/project_pivot.md](docs/project_pivot.md) — รายละเอียดการ pivot
- [docs/limitations_future_work.md](docs/limitations_future_work.md) — ข้อจำกัด + future work (อิงงานวิจัย)
- [cloud_dashboard/README.md](cloud_dashboard/README.md) / [DEPLOY_AZURE.md](cloud_dashboard/DEPLOY_AZURE.md) — cloud
