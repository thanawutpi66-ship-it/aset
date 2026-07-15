# Method 3 — High-resolution R₀ capture with the existing ESP32 + PEL-3111 (design)

> สถานะ: **phase-2 / ยังไม่ทดสอบจริง** — เอกสารออกแบบ + ESP32 firmware sketch
> (`firmware/esp32_fast_r0/`). ใช้ "ของที่มีอยู่แล้ว" ไม่ต้องซื้อเครื่องเพิ่ม.

## 1. ทำไมต้องมี (ปัญหาที่ 10 Hz แก้ไม่ได้)

ที่ ~10 Hz (SCPI readback ~100 ms/จุด) เรา fit ได้ **R₁/C₁** แม่น และได้ **R₀ จากการ
extrapolate ไป t=0** — แต่ **R₀ ล้วน (ohmic, <1 ms) + charge-transfer เร็ว (τ<200 ms)**
ยัง "มองไม่เห็น" เพราะ sample แรกมาช้าเกินไป (ดู `analysis.py`, `docs/project_pivot.md`).

Method 3 จับ transient ในจังหวะสเต็ปด้วย **ADC เร็วของ ESP32** (kHz) แทน SCPI — โดย:
- ใช้ **PEL Dynamic mode** สร้างสเต็ปกระแสคม (slew ระดับ µs)
- ใช้ **TRIG OUT** ของ PEL บอก ESP32 ว่า "t=0 เดี๋ยวนี้"
- ใช้ **IMON (current monitor analog)** ของ PEL เป็นสัญญาณกระแส
- แตะ **แรงดันขั้วแบต** ผ่าน divider เข้า ADC อีกช่อง

> ทีมเคยตัด "75 Hz" ทิ้งเพราะคิดว่าต้องซื้อ INA226 — แต่ **ESP32 มีอยู่แล้ว** (ตอนนี้อ่านแค่
> อุณหภูมิ) และ IMON/TRIG OUT มากับ PEL ฟรี → ฟื้นความสามารถนี้ได้โดยไม่ซื้อเพิ่ม.

## 2. สัญญาณ + การต่อสาย (อ้างอิง PEL-3000 J1 connector)

| สัญญาณจาก PEL | ขั้ว | สเปก | ต่อเข้า ESP32 |
|---|---|---|---|
| **IMON** (กระแส analog) | J1 pin 2–3 | 0–10 V (high range) / **0–1 V (low range)** ∝ I_full | ADC ช่อง I |
| **TRIG OUT** | BNC หลังเครื่อง | pulse 4.5 V, ≥2 µs ทุกครั้งที่ Dynamic สลับ | GPIO interrupt |
| **A COM** | ขั้วลบโหลด | ground ร่วม | GND |
| แรงดันขั้วแบต | ที่แบตโดยตรง | 0–15 V | ADC ช่อง V (ผ่าน divider) |

**ข้อควรระวังระดับสัญญาณ (ESP32 ADC = 0–3.3 V):**
- **IMON:** ใช้ **low range (0–1 V)** ตรงเข้า ADC ได้เลย (ปลอดภัยสุด) หรือ high range (0–10 V)
  ต้องหาร ~1:4 (เช่น 33k/10k) ให้เหลือ ≤3.3 V
- **แรงดันแบต 12–15 V → ≤3.3 V:** divider ~1:5 (เช่น 47k/10k) + ตามด้วย op-amp buffer
  (กัน ADC ของ ESP32 โหลด divider จนเพี้ยน) — **อย่าต่อ 12V เข้า ADC ตรงๆ เด็ดขาด**
- **TRIG OUT 4.5 V → 3.3 V:** divider 2:1 หรือ clamp ด้วย Zener/level-shifter
- ใช้ **ADC1 ของ ESP32 เท่านั้น** (GPIO32–39) เพราะ ADC2 ใช้ไม่ได้ตอน Wi-Fi เปิด;
  ถ้าทำได้ปิด Wi-Fi ระหว่างจับเพื่อลด noise

## 3. ขั้นตอนการวัด (sequence)

```
PC (Python)                         PEL-3111                       ESP32
  │  ตั้ง Dynamic: L1=0A, L2=I_pulse,                                  │
  │  slew เร็ว, Timer ~1s, TRIG OUT on  ───►  พร้อม                    │
  │  "ARM" ───────────────────────────────────────────────────────►  พร้อมรอ trigger
  │  สั่ง Dynamic ON  ──────────────►  สเต็ป 0→I_pulse (µs)            │
  │                                    └─ TRIG OUT pulse ───────────►  interrupt: t0=now
  │                                                                     จับ V,IMON เร็ว ~1–5 kHz
  │                                                                     นาน ~1 s ลง buffer
  │  อ่าน buffer (UART)  ◄───────────────────────────────────────────  ส่ง CSV: t_us,adc_v,adc_i
  │  คำนวณ R0/R1/C1 + เทียบกับ SCPI 1-RC fit
```

## 4. คำนวณ R₀ (และ cross-check)

จาก buffer ความเร็วสูง:
- **R₀ (ohmic):** ใช้จุดที่ t เล็กมาก (เช่น 1–5 ms แรกหลัง trigger) — `R₀ = ΔV/ΔI`
  หรือ fit ช่วงต้นแล้ว extrapolate t→0 (ตอนนี้เห็น sub-200 ms แล้ว ต่างจาก SCPI)
- **R₁/C₁:** fit ช่วง tail เหมือนเดิม → **เทียบกับค่าจาก SCPI 1-RC fit** (ควรตรงกัน = ยืนยันความถูกต้อง)
- ป้อน **R₀(fast)** เป็น feature เพิ่มให้ grader (ดี/เสีย แยกคมขึ้น)

## 5. Calibration (สำคัญ — ADC ของ ESP32 ดิบ/ไม่ linear)

ก่อนแต่ละ session ทำ 2-point linear cal เทียบกับ **ค่า GW Instek (reference ที่ calibrated)**:
- **ช่อง V:** อ่าน `MEAS:VOLT?` ที่ 2 จุด (เช่น OCV และใต้โหลด) จับคู่กับ ADC counts → หา gain/offset
- **ช่อง I:** อ่าน `MEAS:CURR?` ที่ 2 ระดับโหลด จับคู่กับ IMON ADC counts → gain/offset
- เก็บค่า cal ไว้ใน PC; แปลง ADC counts → V/A ก่อนคำนวณ R
- สำหรับ "คัดเกรดเชิงเปรียบเทียบ" ความ linear ไม่ต้องเป๊ะ แต่ **ต้องทำซ้ำได้** (เทียบก้อนต่อก้อนที่ cal เดียวกัน)

## 6. ข้อจำกัด / ความเสี่ยง (พูดตรง)

- ESP32 ADC: ENOB จริง ~9–10 bit, noisy → ต้อง oversample/เฉลี่ย + cal; R₀ absolute สู้เครื่อง lab ไม่ได้
- ground loop / divider loading / สาย IMON ยาว → noise; เดินสายสั้น, shield, GND ร่วมจุดเดียว
- ต้อง **ยืนยัน J1 pinout + TRIG OUT + IMON range** กับ manual ของ **PEL-3111 ตัวจริง** (ผมอ้างจาก PEL-3000E)
- เป็นงาน hardware+firmware+cal — **phase 2**, ยังไม่ทดสอบ
- ความปลอดภัย: 12 V พลังงานต่ำ แต่ระวังลัดวงจรขณะต่อ divider; ใส่ฟิวส์/ต่อตอนปิดโหลด

## 7. BOM (เกือบฟรี)
- ESP32 ✅ มีแล้ว · ตัวต้านทาน divider 4–6 ตัว · op-amp buffer 1 ตัว (เช่น MCP6002) ·
  สาย BNC→jumper สำหรับ TRIG OUT · (option) สายต่อ J1 connector ของ PEL

---
ดู `firmware/esp32_fast_r0/esp32_fast_r0.ino` สำหรับ sketch ฝั่ง ESP32
และ `aset_batt/hardware/pel_batt_test.py` สำหรับการสั่ง PEL ทำ capacity/SoH (Method ข, ผ่าน SCPI).
