# ข้อจำกัดและงานในอนาคต (Limitations and Future Work)

ส่วนนี้สรุปข้อจำกัดเชิงวิธีการของระบบ ASET Battery Characterization ในเวอร์ชันปัจจุบัน
และแนวทางพัฒนาต่อ โดยอ้างอิงการตรวจสอบความถูกต้องเทียบกับวรรณกรรมวิชาการ
(หมายเลข [n] อ้างถึงรายการอ้างอิงท้ายเอกสาร)

---

## อัปเดตล่าสุด (2026-06-29) — สถานะข้อจำกัดปัจจุบัน

- **ทดสอบกับ hardware จริงแล้ว:** FB FTZ6V (12V 5.3Ah VRLA AGM) — ยืนยัน OCV/CV/taper current ถูกต้อง
- **R0 reference:** GBM-3080 วัด ACIR 38 mΩ; โปรแกรม model-based 30 mΩ (new) — ต่างกัน 8 mΩ จาก aging + AC/DC difference ไม่ใช่ bug
- **ไม่มี reference สำหรับ R1/C1:** เครื่องที่ทีมมี (GW Instek GBM-3080, FNIRSI HRM-10) วัด **ACIR 1kHz**
  → เทียบได้แค่ **R0** + OCV; ส่วน **R1/C1/τ** validate ด้วย R²≥0.95 + repeatability
- **HPPC พัลส์สั้นทำให้ R1/C1 under-resolved:** แก้แล้วโดยทำ pulse/relaxation duration ปรับได้ (ควร pulse ≳ 3·τ)
- **AUTO SEQUENCE ทำงานได้แล้ว:** PREPARE→CHARGE→REST→TEST→ANALYZE เป็น workflow อัตโนมัติ
- **YTZ7V profile ค่ายังผิด:** ใส่ 7Ah/CCA130 แต่ datasheet = 6.3Ah(20HR)/CCA105A → ต้องแก้
- ⚠️ การ characterize เต็มไซเคิลใช้เวลา **หลายชั่วโมง** (PREPARE 5 min + ชาร์จ 2–5h + พัก 30 min + discharge)

📄 สถานะ/architecture ล่าสุดทั้งหมด: [../context_summary.md](../context_summary.md)

---

## 1. ข้อจำกัด (Limitations)

### 1.1 แบบจำลอง OCV–SoC (ไม่ครอบคลุม hysteresis)
ระบบใช้ตาราง rested OCV–SoC ต่อเซลล์เพียงชุดเดียวสำหรับทั้งการประจุและคายประจุ และถือว่า
OCV ขึ้นกับอุณหภูมิเพียงเล็กน้อย (entropic term) อย่างไรก็ตาม แบตเตอรี่ LiFePO₄ มี
**voltage hysteresis ที่เด่นชัด** — ที่ SoC เดียวกัน แรงดันขณะประจุต่างจากขณะคายประจุ —
ซึ่งเป็นสาเหตุหลักของความคลาดเคลื่อนในการประมาณ SoC [1][2] ระบบปัจจุบันยังไม่ได้
สร้างแบบจำลอง hysteresis (เช่น OCV-H-SoC map หรือ hysteresis state)

### 1.2 ช่วง plateau ที่ราบของ LiFePO₄ (ill-conditioned)
ในช่วง SoC กลาง (≈20–90%) เส้น OCV–SoC ของ LFP ราบมาก (dOCV/dSoC ≈ 0) ทำให้การ
ย้อนหา SoC จากแรงดัน (OCV→SoC) ไวต่อสัญญาณรบกวนสูง [3] ระบบ "บรรเทา" ปัญหาโดย
**ข้าม OCV correction เมื่อ slope ต่ำ** (`ocv_slope` guard) แต่ยัง **ไม่ได้แก้** ด้วยเทคนิคขั้นสูง

### 1.3 การประมาณ SoC — ปรับปรุงแล้วบางส่วน (2026-06-29)
ระบบใช้ Coulomb counting + OCV correction + EMA (α=0.05) ซึ่งเป็นโครงสร้าง hybrid ที่ถูกต้องตามหลัก [4]
ปรับปรุงล่าสุด:
- **State-dependent coulomb efficiency (Faraday):** η=0.97/0.92/0.75 ตาม SoC < 75%/75–90%/>90% สำหรับ Lead-Acid
  แทน fixed η=0.99 — ลด error ช่วง absorption/near-full
- **Peukert correction:** (I/I_rated)^(k−1), k=1.30, C10 สำหรับ Lead-Acid — ลด error ที่ discharge rate ต่างจาก rated
- **OCV anchor via ΔV/Δt:** PREPARE ใช้ `calibrate_from_ocv_stable()` ≥ 300s + convergence แทน fixed sleep

ยังไม่มี: adaptive filter (EKF/UKF), covariance estimation, หรือ hysteresis model

### 1.4 แบบจำลองความต้านทานภายใน (Rin) และ OCV — ปรับปรุงแล้วบางส่วน
ทิศทางของผลอุณหภูมิถูกต้อง (R เพิ่มเมื่ออุณหภูมิต่ำ ตาม Arrhenius) [5][6]
ปรับปรุงล่าสุด (2026-06-29):
- **Nernst OCV temperature compensation:** `_generate_ocv_tables()` shift ทุก OCV +0.40 mV/°C/cell
  สำหรับ Lead-Acid จาก 25°C reference — ลด error จากอุณหภูมิห้อง (25–28°C ส่งผล ~1–1.2 mV/cell)

ยังคงใช้ **ความสัมพันธ์เชิงเส้น** สำหรับ Rin vs. T (ของจริง exponential ชันมาก < 15°C) [5]
พารามิเตอร์ Rin ยังเป็นค่า heuristic — ยังไม่ fit จากข้อมูลวัดจริงหลายอุณหภูมิ

### 1.5 ความสอดคล้องกับ IEC 61960
- **DCIR**: ใช้วิธี two-pulse `(V₁−V₂)/(I₂−I₁)` ตาม Clause 6.4 แล้ว [7][8] แต่กระแส 1C
  ถูก **clamp ตามขีดจำกัดของ rig** (เซลล์ 50 Ah → 1C = 50 A เกิน load 15 A) จึงไม่ใช่ 1C จริง
- การทดสอบ cycle-life / safety ยัง implement บางส่วน และยัง **ไม่ได้ตรวจสอบเทียบ
  เครื่องมืออ้างอิง** (reference instrument)

### 1.6 ตัวจำแนกเกรดด้วย ML (ข้อมูลสังเคราะห์)
RandomForest grader ถูกเทรนด้วย **ข้อมูลสังเคราะห์ที่ติดป้ายด้วยกฎ** (rule-based labels)
ทำให้ความแม่นยำที่รายงาน (≈100%) **ไม่สะท้อนสมรรถนะจริง** เพราะข้อมูลแยกชั้นด้วยกฎอยู่แล้ว
งานวิจัยใช้ข้อมูลวัดจริง (cycling / EIS / incremental-capacity features) [9][10][11]
ยังขาด dataset จริง, cross-validation บนเซลล์จริง, และการเทียบกับโมเดลอื่น (LSBoost/GRU)

### 1.7 การตรวจสอบเชิงฮาร์ดแวร์
- **sign convention: verified แล้ว** — ทดสอบกับ FB FTZ6V จริง, กระแสชาร์จ/discharge ตรง convention
- ทดสอบ Lead-Acid 6S (FB FTZ6V) แล้ว; lithium 4S ยังเป็น simulation
- SoC/SoH ยังไม่ได้เทียบกับเครื่องมืออ้างอิงแบบ full discharge capacity test (PEL-3111 0.2C ถึง UVP)
- ACIR (GBM-3080) = 38 mΩ vs. model R0 = 30 mΩ — ยืนยัน: ต่างกันจาก aging + AC/DC frequency effect ไม่ใช่ software bug

### 1.8 ระบบและข้อมูล
- cloud dashboard เก็บ snapshot แบบ in-memory → **ไม่มีฐานข้อมูลประวัติถาวร**
- การเข้าดู dashboard ยัง **ไม่มีระบบยืนยันตัวตน** (อ่านอย่างเดียว/สาธารณะ)

---

## 2. งานในอนาคต (Future Work)

จัดลำดับตามผลกระทบต่อความถูกต้องเชิงวิชาการ

1. **โมเดล hysteresis ของ OCV–SoC** — สร้าง 3D OCV-H-SoC map หรือ dual-polarization
   equivalent-circuit ที่มี hysteresis state, ใช้ dQ/dV หรือ pseudo-OCV และ/หรือ EIS
   เพื่อแยกช่วง flat ของ LFP [1][2][3]
2. **การประมาณ SoC แบบ adaptive** — เปลี่ยนจาก EMA เป็น **EKF/UKF**, adaptive sliding-mode
   observer หรือ H-infinity filter; พิจารณา hybrid ML + relaxation reset [2][4]
3. **โมเดล Rin จากข้อมูลจริง** — fit พารามิเตอร์ Arrhenius (exponential) จาก DCIR ที่วัด
   หลายอุณหภูมิ; ใช้ multi-factor dynamic internal resistance model พร้อม error compensation [5][6]
4. **ML grading ด้วยข้อมูลจริง** — เก็บ labeled dataset จากการ cycling/EIS จริง,
   สกัด incremental-capacity features, ปรับ hyperparameter (Bayesian optimization),
   ทำ cross-validation และเทียบ RandomForest กับ LSBoost/GRU [9][10][11]
5. **ตรวจสอบ IEC 61960 เต็มรูป** — ใช้ rig ที่จ่าย 0.2C/1C ได้จริง, ทดสอบ cycle-life/
   safety/energy-density ครบ clause, และ **เทียบกับเครื่องมืออ้างอิง** [7][8]
6. **Hardware-in-the-loop validation** — verify sign convention กับสายจริง, ตรวจ
   SoC/SoH/DCIR เทียบ reference instrument, ขยายไปหลาย chemistry/topology
7. **Productionization** — ฐานข้อมูล time-series สำหรับประวัติ, ระบบ auth ของ dashboard, CI

---

## เอกสารอ้างอิง (References)

[1] Slope-adaptive SoC for LFP with temperature-aware hysteresis modeling, ScienceDirect.
https://www.sciencedirect.com/science/article/pii/S2590116825000803

[2] Enhanced SoC for LFP: Coulomb counting reset + machine learning + relaxation, ACS Energy Letters.
https://pubs.acs.org/doi/10.1021/acsenergylett.4c03223

[3] Addressing the OCV–SOC flat region in LFP cells using EIS, IOPscience (J. Electrochem. Soc.).
https://iopscience.iop.org/article/10.1149/1945-7111/ae33fc

[4] Enhanced coulomb counting method for SoC/SoH estimation, ScienceDirect (Applied Energy).
https://www.sciencedirect.com/science/article/abs/pii/S0306261908003061

[5] Measurement of the temperature influence on current distribution in Li-ion batteries (Arrhenius), Wiley Energy Technology.
https://onlinelibrary.wiley.com/doi/full/10.1002/ente.202000862

[6] Multi-factor dynamic internal resistance model with error compensation, ScienceDirect (J. Energy Storage).
https://www.sciencedirect.com/science/article/pii/S235248472100305X

[7] How to perform internal resistance measurement according to IEC 61960, Arbin Instruments.
https://www.arbin.com/how-to-perform-internal-resistance-measurement-accroding-to-iec-61960-with-arbin.html

[8] Internal Resistance: DCIR and ACIR, Battery Design.
https://www.batterydesign.net/dcir-acir/

[9] An optimized Random Forest regression model for Li-ion prognostics and health management, MDPI Batteries.
https://www.mdpi.com/2313-0105/9/6/332

[10] State of Health estimation for Li-ion batteries using Random Forest and GRU, ScienceDirect (J. Energy Storage).
https://www.sciencedirect.com/science/article/abs/pii/S2352152X23031948

[11] Machine learning pipeline for battery state of health estimation, arXiv.
https://arxiv.org/pdf/2102.00837
