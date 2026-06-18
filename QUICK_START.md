# 🚀 Quick Start Guide - SoC Calibration & Accuracy

## โจทย์ที่แก้ไข
❌ **ปัญหา**: ข้อมูล SoC ที่คำนวณไม่ตรงกับข้อมูลจริงของแบต
✅ **วิธีแก้**: Advanced state estimation + OCV calibration

---

## การใช้งานอย่างถูกต้อง 📋

### Step 1: เตรียมแบต (5-10 min) 🔌
```
1. เสียบ PSU + Electronic Load
2. เสียบ ESP32 (optional)
3. ให้แบต **rest** (หยุดการชาร์จ/ดิสชาร์จ) ≥ 30 นาที
   → เพื่อให้ terminal voltage = OCV
```

### Step 2: Connect & Calibrate ⚡
```
1. Run main.py
2. เลือก COM ports → กด "Connect Instruments"
3. ใส่ Battery Capacity: 50.0 Ah (ของจริง)
4. **กด "📊 Calibrate from OCV"**
   → ระบบจะอ่าน voltage แล้ว estimate SoC จากตาราง OCV
5. ผลลัพธ์:
   ✅ SoC = 45.2% (calibrated from 3.19V)
   ✅ Temperature: 25.3°C (compensated)
```

### Step 3: Run Test ▶️
```
1. กำหนด Load Profile หรือ Manual Control
2. กด "Start Test" / "Run Profile"
3. เฝ้าดู SoC บน display:
   - ถ้า discharging 1A × 1h → ลด 2%
   - ถ้าไม่ลด = มีปัญหา​ hardware
4. ข้อมูลถูกบันทึกใน CSV → download
```

### Step 4: Verify & Plot 📊
```python
# อ่านข้อมูลที่บันทึก
import pandas as pd
df = pd.read_csv('battery_data.csv')

# Plot SoC over time
import matplotlib.pyplot as plt
plt.plot(df['Elapsed_s'], df['SoC_pct'], label='SoC')
plt.xlabel('Time (s)')
plt.ylabel('SoC (%)')
plt.grid()
plt.show()

# ตรวจสอบ: ควรลดลงอย่างสม่ำเสมอ ไม่ jump
```

---

## ตัวอย่างผลลัพธ์ที่คาดหวัง 📈

### Test: 50Ah LiFePO4, Discharging 5A, 2 hours

| Time | Current | SoC (Old) | SoC (New) | Rin | Status |
|------|---------|-----------|-----------|-----|--------|
| 0 min | 0 A | 50.0% ✓ | 50.0% ✓ | 0.05Ω | Calibrated |
| 30 min | 5 A | 47.0% | 47.1% | 0.047Ω | Discharging |
| 60 min | 5 A | 44.0% | 44.2% | 0.048Ω | Discharging |
| 90 min | 5 A | 41.0% | 41.1% | 0.049Ω | Discharging |
| 120 min | 5 A | 38.0% ⚠️ drift | 38.0% ✅ | 0.050Ω | Rest & correct |

✔️ **Difference**: โดยทั่วไป < 1% ต่อชั่วโมง

---

## Config ที่ดี (Best Practice) 🎯

### `config.json`
```json
{
    "battery_type": "LiFePO4",
    "nominal_voltage": 3.2,
    "rated_capacity": 50.0,
    "max_points": 100,
    "simulation_mode": false
}
```

### Battery Type ที่สนับสนุน
- `"LiFePO4"` ← ค่า default สำหรับ
  - Voltage range: 2.5V - 3.8V
  - ใช้กับ 24/48V Solar system
  
- `"Li-ion"` ← สำหรับ 18650 type
  - Voltage range: 2.5V - 4.3V

---

## Troubleshooting 🔧

### ปัญหา: SoC ไม่ลดลง
```
✓ เช็ค:
  1. Current sensor connected?
     → Load ON?
  2. Data recording?
     → Check CSV file
  3. Coulomb efficiency:
     → Default 99%, ปรับได้ใน state_estimator.py
```

### ปัญหา: SoC jump ขึ้นลง
```
✓ เช็ค:
  1. Current noise?
     → บ่อย = sensor problem
  2. Smooth factor (alpha)?
     → ปัจจุบัน 0.05, ↑ = ↓ responsive
  3. OCV correction?
     → Interval = 300s, ปรับได้
```

### ปัญหา: Calibration แปลก
```
✓ เช็ค:
  1. System rest > 30 min?
     → Voltage = OCV ต้องพอ
  2. Temperature sensor OK?
     → ESP32 connected?
  3. Voltage reading:
     → Multimeter ตรวจ vs display
```

---

## Logging & Debug 📝

View logs:
```python
import logging
logging.basicConfig(level=logging.DEBUG)  # ใน main.py
```

Example output:
```
INFO:root:SoC synchronized: 50.0% | Capacity: 50.0 Ah
INFO:root:SoC calibrated from OCV: 3.225V -> 50.1%
INFO:root:OCV Correction: CC=48.5% -> OCV=49.8% -> Blended=49.2%
```

---

## สำคัญ! ⚠️
1. **ไม่ต้อง restart test ทั้งหมด** เพื่อ calibrate
   → เพียง rest + "📊 Calibrate from OCV"
   
2. **Temperature matters!**
   → HoT (50°C): SoC -2% ถึง -5%
   → COLD (0°C): SoC +2% ถึง +5%

3. **OCV table ต้อง match battery type**
   → ใช้ LiFePO4 table กับ Li-ion → ผิด!

---

## Reference 📚
- Paper: "Battery management systems review"
- Standard: IEEE 1188 (Battery guide)
- LiFePO4 specs: typical 3.2V nominal

**Happy Testing! 🎓**
