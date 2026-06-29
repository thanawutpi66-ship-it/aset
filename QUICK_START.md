# Quick Start — ASET Battery Test System

> มหาวิทยาลัยอุบลราชธานี · Capstone A19/2568 · อัปเดต 2026-06-29

---

## ติดตั้ง

```bash
python -m venv venv && venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py                                  # เปิด GUI (PySide6 ISA-101)
```

`config.json` → `"simulation_mode": false` เพื่อใช้ฮาร์ดแวร์จริง

---

## เชื่อมต่อฮาร์ดแวร์

1. เปิดโปรแกรม → แถบ **CONNECT**
2. เลือก VISA port ของ PSU + Load (ตัวอย่าง `ASRL3::INSTR`)
3. เลือก COM port ของ ESP32 (temperature)
4. กด **Connect Instruments** — ไฟสีเขียว = เชื่อมต่อสำเร็จ
5. เลือก **battery profile** จาก dropdown (เช่น `FB FTZ6V (12V 5.3Ah VRLA AGM)`)

---

## AUTO SEQUENCE (แนะนำ)

วิธีทดสอบแบบอัตโนมัติ 5 ขั้น: PREPARE → CHARGE → REST → TEST → ANALYZE

### แถบ TEST MODE → AUTO

| ขั้น | ระยะเวลา (lead-acid) | รายละเอียด |
|---|---|---|
| **1 PREPARE** | ~5 นาที | ปิด PSU+Load, รอ OCV settle (ΔV/Δt < 10 mV/60s, ขั้นต่ำ 300s), anchor SoC |
| **2 CHARGE** | 2–5 ชั่วโมง | 3-stage (bulk CC → absorption CV 14.4V → float 13.65V) |
| **3 REST** | 30 นาที | พักหลังชาร์จ + OCV re-anchor |
| **4 TEST** | 1–3 ชั่วโมง | HPPC Full Sequence (discharge pulses + rest + capacity) |
| **5 ANALYZE** | < 1 นาที | ECM fit (R0/R1/C1), SoH, grading A/B/C/Reject |

กด **RUN AUTO SEQUENCE** → ระบบทำงานเอง — กด **CANCEL** หยุดได้ทุกขั้น

> ข้ามการชาร์จได้: เช็ค "Skip charge if SoC ≥ \_\_ %"

---

## MANUAL (ใช้ทดสอบเฉพาะส่วน)

แถบ TEST MODE → MANUAL

- **HPPC tab**: กำหนด pulse/relax duration, กด **START HPPC SEQUENCE**
- **Control tab**: ควบคุม PSU/Load แบบ manual (on/off, voltage, current)
- **Profile tab**: IEC 61960 profiles

---

## อ่านผลลัพธ์

### Live display
| ค่า | หมายความว่า |
|---|---|
| **SoC** | State of Charge %, anchor จาก OCV + Coulomb counting (Peukert-corrected สำหรับ lead-acid) |
| **Rin** | Internal resistance (mΩ) — model-based เมื่อ I < 0.5A, measured จาก DCIR pulse เมื่อ I ≥ 0.5A |
| **SoH** | State of Health % จาก capacity จริงเทียบ rated |
| **Temp** | อุณหภูมิผิวจาก MLX90614 (ESP32) |

### Analytics tab
- เลือก session → วิเคราะห์ทันที
- **Show Circuit** → ดูโมเดล ECM (R₀ + R₁∥C₁)
- ผลลัพธ์: grade badge A/B/C/Reject + R0/R1/C1/τ + SoH + CCA proxy

---

## SoC ไม่ถูกต้อง — ตรวจสอบ

| อาการ | สาเหตุที่พบบ่อย | วิธีแก้ |
|---|---|---|
| SoC ต่ำผิดปกติเมื่อเริ่ม | PREPARE phase รอไม่ถึง 300s → terminal voltage ยังมี surface charge | ปล่อย PREPARE รอจนครบ (อย่ากด CANCEL กลางคัน) |
| SoC ไม่ขึ้นระหว่างชาร์จ CV | η near full = 0.75 (Faraday gassing) — ปกติ | ไม่ใช่ bug; กระแสจริงส่วนใหญ่ไปสร้าง H₂+O₂ |
| Rin ≠ GBM-3080 | GBM วัด ACIR 1kHz; โปรแกรมวัด DCIR ที่ I < 0.5A ใช้ model | คนละนิยาม — ต่างกัน 5–15 mΩ เป็นเรื่องปกติ |
| SoC drift ระหว่าง discharge | Peukert k=1.30 จาก default; ค่าจริงอาจต่าง | ทำ Peukert plot จากการ discharge หลาย C-rate เพื่อ fit k |

---

## Config หลัก (`config.json`)

```json
{
  "battery_type": "LeadAcid",
  "cells_series": 6,
  "rated_capacity": 5.3,
  "simulation_mode": false
}
```

Battery type ที่รองรับ: `LeadAcid` · `LiFePO4` · `Li-ion` · `LiPO`

---

## Reference

- [ARCHITECTURE.md](ARCHITECTURE.md) — สถาปัตยกรรมเชิงลึก + threading + physics algorithms
- [context_summary.md](context_summary.md) — สถานะโปรเจกต์ปัจจุบัน + to-do
- [docs/limitations_future_work.md](docs/limitations_future_work.md) — ข้อจำกัดทางวิทยาศาสตร์ + วรรณกรรม
