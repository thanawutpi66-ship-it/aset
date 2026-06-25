# สรุปการปรับแนวทางโครงงาน (Project Pivot) — สำหรับทีม

เอกสารนี้สรุปว่า "โปรเจคเปลี่ยนไปทางไหน เพราะอะไร และใครต้องทำอะไรต่อ"
เพื่อให้ทีมเข้าใจตรงกันก่อนเดินต่อ

---

## 1. สรุปสั้น (TL;DR)
- **เดิม:** ระบบทดสอบแพ็ค **LiFePO4 8S (25.6V)** เน้น **เก็บข้อมูลเร็ว 75 Hz** เพื่อจับ Ohmic Drop → DCIR/SOH แม่น + **ตัดไฟเร็วระดับ ms**
- **ใหม่:** ระบบ **คัดเกรดแบตเตอรี่อัตโนมัติหลายเคมี** ทดสอบ **แบตมอเตอร์ไซค์ทั่วไป** (เช่น RB Battery YTZ7V 12V 7Ah — lead-acid AGM) โดย **AI แยกชนิด (acid/lithium) + คัดดี/เสีย** จาก DCIR/SOH/CCA
- **ใช้อุปกรณ์เดิมล้วน ไม่ซื้อเพิ่ม**

---

## 2. ทำไมต้องเปลี่ยน (เหตุผลให้ทีมเข้าใจ)
1. **75 Hz ผ่าน SCPI ทำไม่ได้จริง** — การอ่าน `MEAS:VOLT?`/`MEAS:CURR?` จาก GW Instek ได้จริง ~5 Hz (ตามที่รายงานเองระบุ) การจะได้ 75 Hz ต้อง **ซื้อ INA226 + ESP32 วัดเอง + contactor** เพิ่ม → **ทีมตัดสินใจไม่ซื้อเพิ่ม**
2. **อาจารย์เสนอให้ทดสอบแบตปกติ (มอเตอร์ไซค์)** — เล็ก ปลอดภัยกว่ามาก (12V/7Ah), หาแบต **ดี/เสีย หลายลูกได้ง่ายและถูก** (= แก้ปัญหาขาด dataset)
3. **แนว "คัดเกรด" ไม่ต้องการ 75 Hz** — good/bad ใช้ SOH + DCIR(step) + OCV + CCA ซึ่งวัดที่ 5 Hz ได้หมด → การตัด 75 Hz จึง **สอดคล้องกับทิศใหม่ ไม่ใช่จุดอ่อน**
4. ถ้าเป็น **lead-acid → เส้น OCV–SoC ลาดชัน** (ไม่ flat แบบ LFP) → ประมาณ SoC ง่ายและแม่นกว่า

---

## 3. ขอบเขตใหม่: เก็บ / ตัด / เพิ่ม

### ✅ เก็บไว้ (ใช้ของเดิมได้)
- GUI (Tkinter) + dynamic plot, CSV logging, web dashboard
- ควบคุม charge/discharge ผ่าน **SCPI/PyVISA** (PEL-3111 + PSW)
- **อ่าน V/I จาก GW Instek เอง** (calibrated — ไม่ต้องมี INA226)
- Coulomb counting (SoC), capacity/SOH, DCIR (วิธี step/two-pulse)
- โครงสร้าง grader (rule-based / RandomForest), แนวคิด 1RC

### ❌ ตัด / ลดเป้า (ต้องแก้ objective + แจ้งอาจารย์)
- **75 Hz / จับ Ohmic Drop คม (R0 ล้วน)** → ทำไม่ได้ที่ ~5 Hz (สเต็ปแรกมาช้า ~200 ms)
- **Active cutoff เร็วระดับ ms** → เหลือ **software cutoff ผ่าน SCPI (`:INP OFF`/`:OUTP OFF`) + MCB LUMIRA เป็น passive backstop** (ช้ากว่า แต่แบต 12V พลังงานต่ำ รับได้)
- **ไม่ซื้อ INA226 / DC contactor / isolation / NTC** (ใช้ของเดิม)

> **แก้ความเข้าใจ (สำคัญ):** ข้อความเดิม "แยก R0/Rp ด้วย 1RC ทำไม่ได้ที่ 5Hz" **เหมารวมเกินไป**
> ความจริง: τ ของ diffusion = 10–60 s → ที่ 5Hz เก็บ 150 จุด/30s **ฟิต R1/C1 ได้แม่นมาก**
> (พิสูจน์แล้ว: synthetic 5Hz → R0/R1/C1/τ ตรง, R²=1.00). **R0** ใช้ "ลากเส้นย้อนกลับไป t=0"
> ในการ fit (extrapolation) ได้ดีกว่า single-step ส่วน R0 ล้วน (τ<200ms) เท่านั้นที่ยังต้อง
> hardware fast-capture (วิธี 3). → โค้ดจึง **เปิด 1-RC fit ไว้สำหรับ HPPC** และรายงานคู่กับ
> **DCIR@~250 ms** (cross-check/fallback). ดู `aset_batt/acquisition/analysis.py`.

### ➕ เพิ่มใหม่ (งานหลักของแนวนี้)
- **Chemistry detector** — แยก lead-acid ↔ lithium จาก features: OCV เต็ม, ความชันเส้น OCV, IR, การคืนแรงดันหลังปลดโหลด
- **เลือก profile + ขีดปลอดภัยอัตโนมัติตามชนิด** (กันชาร์จผิดชนิด)
- **feature สำหรับ lead-acid:** CCA / voltage-sag under load
- **OCV table ของ lead-acid 12V** (และ/หรือ lithium 12V)

---

## 4. สถาปัตยกรรมใหม่ (อุปกรณ์เดิมทั้งหมด)
```
[แบตมอไซค์ 12V (DUT)]
   ├─ charge ← GW Instek PSW/PSB-1080L (SCPI)
   ├─ discharge + วัด V/I ← GW Instek PEL-3111 (SCPI, ~5 Hz)
   ├─ อุณหภูมิ ← MLX90614 → ESP32 → UART → PC
   └─ ตัดวงจร ← software (SCPI OFF) + MCB LUMIRA (passive backstop)
                         │
                  [PC / Python]  control + คำนวณ SoC/SOH/DCIR + chemistry detect + คัดเกรด + CSV + dashboard
```
> หมายเหตุ: บทบาท **ESP32 ลดลงเหลือแค่อ่านอุณหภูมิ** (เพราะตัดงาน high-rate sensing ออก)

---

## 5. บทบาทของ "AI" ในแนวใหม่ (มีจริง 2 ชั้น)
1. **ด่าน 1 — แยกชนิดเคมี** (acid/lithium) จากลายเซ็นไฟฟ้า
2. **ด่าน 2 — เลือกโปรไฟล์ทดสอบ + ขีดปลอดภัยให้ถูกชนิด** อัตโนมัติ
3. **ด่าน 3 — คัดดี/เสีย** จาก SOH + DCIR + CCA (rule-based หรือ RandomForest)

> ⚠️ สิ่งที่ AI **ทำไม่ได้** และต้องสื่อสารให้ชัด: "บอกรุ่นเป๊ะ (เช่น YTZ7V)" จากไฟฟ้าล้วน — ต้องอ่านฉลาก

---

## 6. ผลต่อ Objective / รายงาน (ต้องคุยอาจารย์)
- เปลี่ยน DUT: 8S LiFePO4 25.6V → **แบตมอเตอร์ไซค์ 12V (acid/lithium)**
- เอา **"75 Hz" และ "active cutoff เร็ว"** ออกจาก objective
- ใส่ **"แยกชนิดเคมีอัตโนมัติ + คัดเกรดดี/เสีย"** เป็นวัตถุประสงค์หลัก
- จุดขายใหม่: *automation + multi-chemistry grading* (ไม่ใช่ *high-speed acquisition*)

---

## 7. แผน Validation (ไม่ต้องซื้อเพิ่ม)
- **GW Instek = reference V/I** (calibrated, ฟรี)
- **datasheet YTZ7V** (CCA, 7Ah) = ค่าอ้างอิง SOH/IR
- **เก็บแบตมอไซค์หลายลูก ดี/เสีย** → พิสูจน์การคัดเกรด A/B/C
- (option) **Hioki BT355x** ถ้ายืมแล็บได้ = ground-truth DCIR

**ตัวชี้วัดความสำเร็จใหม่:**
1. แยกชนิด acid/lithium ถูก > X%
2. คัดดี/เสีย ตรงกับ reference/datasheet > X%
3. DCIR/SOH คลาดเคลื่อน < Y% เทียบ reference

---

## 8. สิ่งที่ต้องแก้ในโค้ด (สำหรับทีม dev)
1. `read_vi()` → อ่าน V/I จาก **SCPI GW Instek** ให้เหมาะแบต 12V (ปัจจุบันผูกกับ psu/load 8S)
2. **OCV table → lead-acid 12V** (sloped) + เพิ่ม **chemistry detector**
3. **DCIR → step method timing** ผ่าน SCPI
4. **Safety thresholds → 12V** (lead-acid ~15V OVP / ~10.5V UVP / 60°C) + software cutoff
5. **Grader → เพิ่ม feature CCA / voltage-sag**

---

## 9. Action items (แบ่งงานทีม)
- [ ] **คุยอาจารย์** ยืนยันแนวใหม่ + แก้ objective (ตัด 75 Hz/active-cutoff, ใส่ multi-chemistry grading)
- [ ] **เก็บ dataset** แบตมอไซค์ดี/เสีย หลายลูก (lead-acid + lithium ถ้ามี)
- [ ] **retune โค้ด** (ข้อ 8)
- [ ] **firmware ESP32** เหลือแค่อ่าน temp (MLX90614) — งานเบาลง
- [ ] **ตรวจฉลากแบต** ว่าจะรับ acid อย่างเดียว หรือ acid+lithium

---

## 10. ข้อแลกเปลี่ยน (ให้ทีมเข้าใจตรงกัน)
| เสียไป | ได้มา |
|---|---|
| จุดขาย "ความเร็วสูง 75 Hz" | **ทำได้จริงด้วยของเดิม ไม่ต้องซื้อเพิ่ม** |
| แยก R0/Rp ละเอียด | **ปลอดภัยขึ้นมาก** (12V vs 25.6V pack) |
| active cutoff เร็ว ms | **หา dataset ดี/เสียได้ง่าย/ถูก** |
| | **AI มีบทบาทจริง** (แยกชนิด + คัดเกรด) |
