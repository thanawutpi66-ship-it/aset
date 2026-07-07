# Rig Investigation Findings

บันทึกการสืบสวน harness-resistance/OCV-anchor bug (ก.ค. 2026) — เก็บ **ที่มาและเหตุผล** ของแต่ละข้อค้นพบไว้ที่นี่
(สเปก/SCPI/ขั้นตอน wiring แบบ lookup อยู่ที่ [`pel3111_psw_hardware_reference.md`](pel3111_psw_hardware_reference.md),
สิ่งที่ต้องทำต่ออยู่ที่ [`rig_status_action_items.md`](rig_status_action_items.md))

## ที่มาของค่า `_I_STANDBY = 0.6` (aset_batt/acquisition/analysis.py)

ค่านี้คือกระแส bleed จริงของ PSW (ไม่ใช่แค่ของเก่าค้างมา) — PSU ดูดกระแสกลับ ~0.6A เองตอน CV mode ที่แรงดันแบต
มากกว่าแรงดันที่ตั้งไว้ (ไม่ได้ชาร์จจริง) SSR (FOTEK SSR-50DD) คือของที่ซื้อมาแก้ปัญหานี้โดยเฉพาะ โดยตัดกระแสไม่ให้
ไหลถึงแบตตอนไม่ได้ชาร์จจริง

แต่ `_I_STANDBY=0.6` ในโค้ด **วิเคราะห์ข้อมูลหลัง-การทดสอบ** (ใช้ตรวจจับ "ช่วงพัก" จาก log) เจอปัญหาว่ามันไปจับกระแส
ชาร์จ bulk (~0.5A) ที่ใกล้เคียง 0.6A แทน — แก้เป็น `_I_STANDBY=0.0` แล้ว (สอดคล้องกับ `StateEstimator.standby_current
=0.0` ที่แก้ไปก่อนหน้าตอนติด SSR) เพราะกระแสที่ log ตอนพักจริง (SSR OFF) คือ 0.000A เป๊ะ ไม่ใช่ 0.6A — bleed 0.6A
ไม่เคยไปถึงจุดวัดกระแสของแบตเลยเพราะ SSR ตัดไปแล้ว

## Topology ของ rig — ก่อน/หลังที่แนะนำแก้

**ปัจจุบัน (พบว่า sense ไม่ถึงแบตเลย)**:
```
PSU Force+ ── SSR ขา1 ── SSR ขา2 ── e-Load +S     (ไม่ถึงแบต)
PSU Force- ──────────────────────── e-Load -S     (ไม่ถึงแบต)
e-Load Force+/- ──[เบรกเกอร์]── แบต                (เส้นทางไฟจริงที่ไปแบต — มี harness/เบรกเกอร์ resistance ปน)
```

**ที่แนะนำ (ทุก sense ไปจบที่แบต — ยังไม่ยืนยันว่าทำจริงแล้ว ดู status file)**:
```
PSU Force+ ── SSR ── แบต(+)   ← กำลังไฟ, SSR ตัด bleed 0.6A เหมือนเดิม (ไม่เปลี่ยน)
PSU Force- ──────────── แบต(-)
PSU S+     ──────────── แบต(+)   ← ย้ายจาก SSR ขา2 + ต้องถอด joining plate (S+↔+V) ออกก่อน
PSU S-     ──────────── แบต(-)   ← ย้ายจาก e-Load's S- + ต้องถอด joining plate (S-↔-V) ออกก่อน

e-Load Force+/- ──[เบรกเกอร์]── แบต   (ไม่เปลี่ยน)
e-Load +S       ──────────────── แบต(+)   ← ย้ายจาก SSR ขา2
e-Load -S       ──────────────── แบต(-)   ← ย้ายจาก PSU's -
```

**ทำไมย้าย sense ไปแบตได้โดยไม่กระทบการแก้ bleed 0.6A**: bleed 0.6A เป็นปรากฏการณ์ในวงจร Force (กำลัง) ของ PSU
เท่านั้น — SSR ที่คั่นอยู่ใน Force+ line ตัดกระแสนี้ทั้งหมด ขา sense เป็น high-impedance (กระแสระดับ µA) ไม่ว่าจะต่อไป
ที่ไหนก็ไม่ได้เปิดเส้นทางให้ 0.6A ไหลผ่านได้ — ย้าย sense ไปแบตได้อย่างปลอดภัย ไม่ทำให้ปัญหา bleed กลับมา

ขั้นตอน wiring/joining-plate เต็มอยู่ที่ [`pel3111_psw_hardware_reference.md`](pel3111_psw_hardware_reference.md#remote-sense--ขั้นตอนต่อสาย-ทั้ง-2-เครื่อง)

## 🔥 พบใหญ่สุด: PSW ปิด bleeder resistor ได้ตรงๆ ผ่าน SCPI — อาจไม่ต้องพึ่ง SSR เลย

จาก `PSW_programming_manual_EN_Ver_2_20241104-1.pdf` หัวข้อ "System Function Command":

```
SYSTem:CONFigure:BLEeder[:STATe] {OFF|ON|AUTO|0|1|2}

Description: Sets or queries the status of the bleeder resistor.
  0 | OFF  → ปิด bleeder resistor
  1 | ON   → เปิด bleeder resistor
  2 | AUTO → โหมดอัตโนมัติ
```

**นี่คือคำสั่ง SCPI ตัวเดียวที่ปิด "ตัวต้านทาน bleed 0.6A" ที่เป็นต้นตอของปัญหา `_I_STANDBY` ทั้งหมดในเธรดนี้ได้โดยตรง
ในซอฟต์แวร์/เฟิร์มแวร์ — ไม่ต้องพึ่งการต่อ SSR (FOTEK SSR-50DD) เพื่อตัดวงจรทางฮาร์ดแวร์เลย**

**นัยสำคัญ**:
- ถ้าส่ง `SYSTem:CONFigure:BLEeder:STATe OFF` ครั้งเดียวตอน connect เครื่อง (หรือ set ค้างไว้ที่หน้าเครื่อง) กระแส
  0.6A ที่ดึงตลอดตอน CV mode ไม่ทำงานที่แรงดันตั้งจะหายไปตั้งแต่ต้นทาง — SSR อาจกลายเป็นของที่ไม่จำเป็นอีกต่อไป
  (ลดความซับซ้อนของวงจร ลดจุดสัมผัส/contact resistance ที่ SSR เพิ่มเข้ามาด้วย)
- **ยังไม่ได้ทดสอบจริงกับเครื่อง** — ก่อนถอด SSR ออก ควรลองตั้งค่านี้แล้วเช็คพฤติกรรมจริงก่อน (เช่น ปิด bleeder แล้ว
  PSU ยังลดแรงดันได้เร็วพอไหมตอนสั่งลดค่า set — bleeder มักมีไว้ให้ PSU "ดูด" กระแสออกได้เองตอนต้องการลดแรงดันเร็ว
  ถ้าปิดไปเลย การลดแรงดันอาจช้าลงเพราะต้องรอให้โหลดภายนอกดึงประจุออกแทน) — โหมด `AUTO` (`2`) อาจเป็นทางเลือกที่ปลอดภัย
  กว่า (เปิดเฉพาะตอนจำเป็นจริงๆ)
- คำสั่งนี้ **"This setting is applied only after the unit is reset"** สำหรับคำสั่ง config อื่นๆ ในหมวดเดียวกัน (เช่น
  `SYSTem:CONFigure:CURRent:CONTrol`) ต้องเช็คว่า `BLEeder:STATe` ต้อง reset เครื่องก่อนมีผลด้วยหรือเปล่า (manual ไม่ได้
  เขียนข้อความนี้กำกับที่ตัว BLEeder โดยเฉพาะ แต่คำสั่งข้างเคียงหลายตัวมี — ควรทดสอบเพื่อยืนยัน)

## PEL-3111 — พบคำสั่ง SCPI ตั้ง Range ของโหมด static ที่หาไม่เจอมาก่อน

จาก `PEL-3000H_ProgrammingManual_EN_20190401.pdf` หมวด "Mode Subsystem Commands":

```
[:MODE]:CRANge {HIGH|MIDDle|LOW}   ← ตั้ง current range ของทุกโหมด (CC/CR/CV/CP) แบบ static
[:MODE]:VRANge {HIGH|LOW}          ← ตั้ง voltage range ของทุกโหมด แบบ static
```

ก่อนหน้านี้เข้าใจว่าไม่มีคำสั่ง SCPI สำหรับตั้ง Range โหมด static เลย (เจอแต่ `:NSEQuence:RANGe`, `:FSEQuence:RANGe`,
`:BATTery:RANGe {ILVL|IMVL|IHVL|ILVH|IMVH|IHVH}` สำหรับโหมด sequence/battery-test โดยเฉพาะ) — ตอนนี้เจอคำสั่งของ
static mode แล้ว สามารถเขียนโค้ดตั้ง `I Range=M(21A)` และ `V Range=L(1.5-15V)` อัตโนมัติตอน connect ได้จริง ผ่าน
`:CRANge MIDD` และ `:VRANge LOW` แทนที่จะต้องตั้งด้วยมือที่หน้าเครื่องทุกครั้ง — เหตุผลที่อยากตั้ง range แคบลง (accuracy
เพิ่ม 10x ทั้งฝั่ง V และ I) อยู่ที่ [`pel3111_psw_hardware_reference.md`](pel3111_psw_hardware_reference.md) หัวข้อ
สเปก PEL-3111

## PEL-3111 มี native BATT Test Automation ในตัวเครื่อง — verify แล้วเจอบั๊กในโค้ดเราเอง

เจอฟีเจอร์ที่ไม่รู้จักมาก่อน: **e-Load มีโหมดทดสอบแบตในตัว** — เก็บ profile ได้ 12 memory slot (`BATT.No 1-12`) แต่ละ
slot ตั้งได้ครบ: mode (CC/CR/CP), range, ค่าที่ตั้ง, slew rate ขึ้น/ลง, **Stop Voltage, Stop Time, Stop AH (ตัด
เมื่อครบ Ah ที่กำหนด!)**, และ datalog interval — ทั้งหมดตั้งได้ในคำสั่งเดียว `:BATTery:EDIT (1)...(10)`

**เช็คกับโค้ดที่มีอยู่แล้ว**: `aset_batt/hardware/pel_batt_test.py` มี `NATIVE_BATT_SCPI` dict ที่ comment ไว้ตรงๆ ว่า
"the strings below are best-effort... marked VERIFY" — ตอนนี้เจอ syntax จริงจาก Programming Manual แล้ว เทียบกันได้:

| ในโค้ด (เดา) | ของจริงจาก manual | ตรงไหม |
|---|---|---|
| `:BATT:MODE CC` | `:BATTery:MODE {CC\|CR\|CP}` | ✅ ตรง |
| `:BATT:CURR {a}` | `:BATTery:VALue {a}` | ❌ ผิด — คำสั่งจริงคือ `VALue` ไม่ใช่ `CURR` |
| `:BATT:STOP:VOLT {v}` | `:BATTery:STOP:VOLTage {v}` | ✅ ตรง |
| `:BATT:DLOG:TIM {s}` | `:BATTery:DATalog:TIMer {s}` (ตัวอย่างใน manual ย่อเป็น `:BATT:DAT:TIM`) | ❌ ผิด |
| `:BATT:STAR ON`/`OFF` | `:BATTery:STATe ON`/`OFF` (ต้อง run ต่อด้วย `:BATT:RUN` แยกอีกคำสั่ง) | ❌ ผิดชื่อคำสั่ง + ขาดขั้นตอน `:BATT:RUN` |
| `:BATT:STAT?` (คาด 0/1) | ของจริงมี **2 คำสั่งคนละความหมาย**: `:BATTery:STATe?` คืนค่าเป็น string (`OFF` หรือ `ON,{STOP\|RUN\|END}`), `:BATT:CHANnel:STATus?` คืน 0/1 ตัวเลขจริง | ❌ ชื่อคำสั่งผิด ไม่ตรงกับทั้งคู่ |
| `:BATT:FETC:AH?` / `:BATT:FETC:WH?` | **ไม่มีคำสั่งนี้ในคู่มือเลย** — `:BATT:RESult?` ที่มีจริงคืนแค่ "current,voltage" ขณะนั้น ไม่ใช่ Ah/Wh สะสม | ❌ ไม่มีคำสั่งแบบนี้จริง |

**ผลที่ตามมา**: `native_supported()` เรียก `:BATT:STAT?` เพื่อ probe — เป็นคำสั่งที่ไม่มีจริง จะ error/คืนค่าที่ parse
เป็นตัวเลขไม่ได้เสมอ ทำให้ `native_supported()` คืน `False` ตลอดเวลา **โค้ด native path นี้เลยไม่เคยถูกเรียกใช้จริงเลย
สักครั้ง (fallback ไป `run_pc_discharge()` เสมอ)** — แต่**ไม่ใช่บั๊กที่กระทบการทำงานจริง** เพราะ docstring ของไฟล์เขียน
ไว้เองว่า `run_pc_discharge()` คือ "the recommended path" อยู่แล้ว (ใช้ PC coulomb-counting ที่แม่นยำกว่า ไม่ต้องพึ่ง
readback 5Hz ของเครื่อง) — native path เป็นแค่ทางเลือกเสริมที่ไม่เคย engage ยังไม่กระทบผลลัพธ์การทดสอบจริงตอนนี้

ถ้าอยากได้ native path ใช้งานได้จริง ต้องแก้ `NATIVE_BATT_SCPI` ตามตารางข้างบน **และยังต้องหาวิธีดึงค่า Ah/Wh สะสม
จริงๆ เพิ่ม** (ไม่มีคำสั่ง SCPI แบบง่ายๆ ในสิ่งที่เจอตอนนี้ — อาจต้องดึงเป็นไฟล์ datalog จากเครื่องแทน ซึ่งเป็นคนละงาน
ใหญ่กว่านี้)

## ⚠️ พบปัญหาความเสถียร: USB บน PSW เคย BSOD หลังใช้งานต่อเนื่อง 2-4 ชั่วโมง

จาก `APN_PSW-series_USB_interface_issue_V1_E.pdf` (Application Note ของ GW Instek เอง):

> "The USB CDC/ACM is the USB class which we applied for our instrument for a long time. There have a lot of
> customer report that **the Windows will be crashed after a long time ATE testing. In general, it will happen
> around 2~4 hours while querying the voltage or current result time-by-time.**... On crashing, the system will
> show below blue screen operating system dump (BSOD)."

**เกี่ยวข้องกับเรามาก**: rig เรา poll `MEAS:VOLT?`/`MEAS:CURR?` ต่อเนื่องตลอดการทดสอบ และการทดสอบจริงบางไฟล์ที่วิเคราะห์
กันมา (เช่น `test_20260706_185655.csv`) ยาวถึง **13,527.8 วินาที (~3.75 ชั่วโมง)** — อยู่ในช่วง 2-4 ชั่วโมงที่ระบุว่า
เกิด BSOD พอดี ถ้าเคยเจอ Windows ค้าง/รีสตาร์ทเองระหว่างการทดสอบยาวๆ นี่อาจเป็นสาเหตุ ไม่ใช่บั๊กในโค้ดเรา

**วิธีแก้ที่ระบุใน AN**:
1. เปลี่ยน NI-VISA เป็นเวอร์ชัน 3.4 หรือ 4.2 (เวอร์ชันเก่ากว่าปัจจุบันมาก — ต้อง downgrade)
2. หรือใช้ LabVIEW driver แบบไม่พึ่ง NI-VISA ("XXX-win32api")
3. หรือใช้ LabVIEW driver พร้อม workaround package ("XXX.zip")

**ยังไม่ยืนยันว่ากระทบ rig เราจริงไหม** — ทางแก้ทั้ง 3 ข้อในเอกสารเจาะจงเรื่อง NI-VISA/LabVIEW ซึ่งเราไม่ได้ใช้ตรงๆ
(`aset_batt` ใช้ PyVISA ผ่าน `pyvisa.ResourceManager()`) **ต้องเช็คก่อนว่า PyVISA บนเครื่องเรา backend เป็นอะไร**:
- ถ้าใช้ NI-VISA runtime (ติดตั้ง NI-VISA driver ไว้) → มีความเสี่ยงเดียวกับที่ AN นี้อธิบาย
- ถ้าใช้ `pyvisa-py` (pure Python backend ไม่พึ่ง NI-VISA .dll) → อาจไม่ได้รับผลกระทบเลย เพราะเป็นคนละ implementation
- เช็คได้ด้วย `python -c "import pyvisa; print(pyvisa.ResourceManager().visalib)"` ที่เครื่องจริง

**เชื่อมโยงกับ LinkView**: LinkView (ซอฟต์แวร์ของ GW Instek เอง) เขียนด้วย LabVIEW ซึ่งพึ่ง NI-VISA runtime โดยตรง —
อธิบายได้ว่าทำไม AN นี้ถึงเสนอทางแก้เฉพาะฝั่ง NI-VISA/LabVIEW (เพราะซอฟต์แวร์ทางการของเขาเองก็ใช้ stack เดียวกัน)
ไม่ได้แปลว่า PyVISA ของเราจะเจอปัญหาเดียวกันเสมอไป — ขึ้นกับ backend ที่ติดตั้งจริง
