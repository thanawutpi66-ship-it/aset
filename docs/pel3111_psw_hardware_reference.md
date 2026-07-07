# PEL-3111 / PSW 80-40.5 Hardware Reference

ข้อมูลอ้างอิงนิ่งๆ (สเปก, SCPI command index, ขั้นตอนต่อสาย) ของอุปกรณ์ 2 ตัวที่ใช้จริงใน rig — ไม่มีการวิเคราะห์/
เหตุผลอยู่ในไฟล์นี้ (ดูที่มาของแต่ละเรื่องได้ที่ [`rig_investigation_findings.md`](rig_investigation_findings.md)
และสิ่งที่ต้องทำต่อได้ที่ [`rig_status_action_items.md`](rig_status_action_items.md))

## อุปกรณ์ที่ใช้จริงใน rig

| อุปกรณ์ | รุ่น | สเปกหลัก |
|---|---|---|
| PSU | GW Instek **PSW 80-40.5** | 1080W, multi-range DC power supply |
| e-Load | GW Instek **PEL-3111** | 0–210A / 1.5–150V, 1050W |
| SSR | FOTEK **SSR-50DD** | DC-DC solid state relay, input 5–32VDC (ควบคุม), output 5–220VDC (สวิตช์), คั่นอยู่ใน PSU Force+ line |

`aset_batt/hardware/hardware_driver.py` คุมทั้งสองผ่าน PyVISA/SCPI (`MEAS:VOLT?`/`MEAS:CURR?`) — แรงดันที่ log
ทุกจุดมาจาก **เครื่องมือ (PSU หรือ e-Load) เอง** ไม่ใช่จาก ADC แยกที่ ESP32 (ESP32 คุมแค่ SSR relay + วัดอุณหภูมิ)

## PEL-3111 — สเปกเต็มจาก PEL-3000_Spec_E.pdf (กลุ่ม PEL-3021/3041/**3111**/3211)

สเปกใช้ได้เมื่อเปิดเครื่องอุ่น ≥30 นาที ที่ 20–30°C

```
Rated: Voltage 0–150V | Current 210A | Power 1050W | Input Resistance (local sense) 500kΩ
Min. Operating Voltage (typ.): 0.75V @105A, 1.5V @210A

── Constant Current (CC) Mode ──
Operating Range: H 0–210A / M 0–21A / L 0–2.1A
Accuracy: ±(0.2% of set + 0.1% of f.s.*H-range) + Vin/500kΩ
Resolution: H 10mA / M 1mA / L 0.1mA

── Constant Resistance (CR) Mode ──
Operating Range: H 21.428mΩ–1.25kΩ / M 71.427mΩ–4.16667kΩ / L 714.27mΩ–41.6667kΩ
Accuracy: ±(0.5% of set + 0.5% of f.s.) + Vin/500kΩ
Resolution: H 4µs / M 24µs / L 24µs (ตามช่วง)

── Constant Voltage (CV) Mode ──
Operating Range: H 1.5–150V / L 1.5–15V
Accuracy: ±(0.1% of set + 0.1% of f.s.)
Resolution: H,L 10mV / 1mV

── Constant Power (CP) Mode ──
Operating Range: H 105–1050W / M 10.5–105W / L 1.05–10.5W
Accuracy: ±(0.6% of set + 1.4% of f.s.)
Resolution: 100mW / 10mW / 1mW

── PARALLEL Mode ── (ไม่ใช้ใน rig นี้ — เครื่องเดียว)
Capacity: 5250W (4 units); PEL-3111 with 4 booster units: Max 9.45kW

── Slew Rate (CC/CR mode) ──
CC: H 16mA/µs–16A/µs | M 1.6mA/µs–1.6A/µs | L 160µA/µs–160mA/µs
CR: H 1.6mA/µs–1.6A/µs | M 160µA/µs–160mA/µs | L 16µA/µs–16mA/µs
Accuracy: ±(10% of set + 5µs)

── METER ──
Voltmeter Accuracy = ±(0.1% ของค่าที่วัด + 0.1% ของ Full-Scale ที่ตั้งไว้)
Ammeter Accuracy   = ±(0.2% ของค่าที่วัด + 0.3% ของ Full-Scale ที่ตั้งไว้)
(Parallel operation: Ammeter Accuracy = ±(1.2% of rdg + 1.1% of f.s.) — ไม่ใช้ใน rig นี้)

── DYNAMIC MODE (CC/CR) ──
T1 & T2: 0.025ms–10ms (res 1µs) หรือ 1ms–30s (res 1ms), accuracy ±100ppm
Current Accuracy: ±0.4% F.S.

── Protection Function ──
Overvoltage (OVP): ปรับได้ ตัดโหลดที่ 110% ของแรงดันที่ตั้ง
Overcurrent (OCP): 0.2A–231A (ปรับได้)
Overpower (OPP): 1W–1155W (ปรับได้)
Overheat (OHP): ตัดโหลดเมื่อ heat sink ถึง 95°C
Undervoltage (UVP): ปรับได้ 0–150V หรือ Off
Reverse connection (REV): ป้องกันด้วยไดโอด ตัดโหลดเมื่อเกิด alarm

── General Specifications ──
Line Input: 90–132VAC / 180–250VAC, single-phase, 47–63Hz
Power Max: 190VA
Interface: USB/RS232C/Analog Control (standard); GPIB/LAN (option)
Weight: ~17kg
Dimensions (WxHxD): 429.5×128×400mm
```

**สำคัญ**: error term "% of full-scale" ขึ้นกับ **Range ที่ตั้งไว้ที่หน้าเครื่อง** ไม่ใช่ค่าที่วัดจริง:

| Range ที่ตั้ง | Voltage f.s. error | Current f.s. error |
|---|---|---|
| ปัจจุบัน (เห็นจากภาพหน้าจอ): V=H(150V), I=H(210A) | 0.1%×150V = **150mV** | 0.3%×210A = **630mA** |
| แนะนำสำหรับแบต 12V/~5A: V=L(1.5–15V), I=M(0–21A) | 0.1%×15V = **15mV** (ลด 10x) | 0.3%×21A = **63mA** (ลด 10x) |

คำสั่ง SCPI ตั้ง Range: `[:MODE]:CRANge {HIGH|MIDDle|LOW}` (current), `[:MODE]:VRANge {HIGH|LOW}` (voltage) —
ใช้ได้กับทุกโหมด static (CC/CR/CV/CP) — ยังไม่ได้เขียนโค้ด automate (ดู action items)

## PSW 80-40.5 — สเปกเต็มจาก PSW-Series_Spec_E.pdf (รุ่น 1080W, 80V/40.5A)

**PSW-80 อยู่ในกลุ่ม "PSW-30/40/80/160" (low-voltage family)** — ใช้ขั้วต่อ output แบบ **M4 screw หรือ M8 bolt**
(ไม่ใช่ 9-pin connector แบบ 250V/800V models) สเปกใช้ได้เมื่อเปิดเครื่องอุ่น ≥30 นาที ที่ 20–30°C

```
Rated: Output Voltage 80V | Output Current 40.5A | Output Power 1080W | Power Ratio 3

── Constant Voltage (CV) Mode ──
Line Regulation: 43mV        Load Regulation: 45mV
Ripple & Noise: p-p 100mV, r.m.s. 14mV
Temperature coefficient: 100ppm/°C ของแรงดัน rated (หลังอุ่นเครื่อง 30 นาที)
Remote sense compensation voltage = 0.6 V/wire   ← ชดเชยแรงดันตกได้สูงสุด 0.6V ต่อสาย
Rise Time: 50ms (rated load) / 50ms (no load)
Fall Time: 50ms (rated load) / 500ms (no load)
Transient response time: 1ms

── Constant Current (CC) Mode ──
Line Regulation: 45.5mA      Load Regulation: 45.5mA
Ripple & Noise (r.m.s.): 81mA
Temperature coefficient: 200ppm/°C ของกระแส rated (หลังอุ่นเครื่อง 30 นาที)

── Protection Function ──
OVP: setting range 8–88V, accuracy ±2% ของแรงดัน rated
OCP: setting range 4.05–44.55A, accuracy ±2% ของกระแส rated
OTP: ตัด output
Low AC input (AC-FAIL): ตัด output
Power limit: ~105% ของกำลังไฟ rated (ค่าคงที่)

── Front Panel Display (4 digits) ──
Voltage accuracy: 0.1% + 20mV     Current accuracy: 0.1% + 50mA

── Programming & Measurement (Interface) ──
Voltage programming accuracy: 0.1% + 10mV    resolution: 2mV
Current programming accuracy: 0.1% + 40mA    resolution: 3mA
Voltage measurement accuracy: 0.1% + 10mV    resolution: 2mV
Current measurement accuracy: 0.1% + 40mA    resolution: 3mA

── Common Specification (ทุกรุ่นในตระกูล 30/40/80/160V) ──
Input: 100–240Vac, 50–60Hz, single-phase (85–265Vac / 47–63Hz operating range)
Max input current: 1080W → 15A@100Vac / 7.5A@200Vac
Inrush current: <75A (สำหรับรุ่น 1080W)
Max input power: 1500VA (1080W)
Power factor: 0.99@100Vac / 0.97@200Vac
Efficiency: 78%@100Vac / 80%@200Vac
Hold-up time: ≥20ms

External Analog Control:
  Ext. voltage control (output V): accuracy/linearity ±0.5% ของ rated V
  Ext. voltage control (output I): accuracy/linearity ±1% ของ rated I
  Ext. resistor control (output V/I): accuracy/linearity ±1.5%
  Output V/I monitor accuracy: ±1%
  Shutdown control: LOW (0–0.5V) หรือ short-circuit
  CV/CC/ALM/PWR ON/OUT ON indicator: photocoupler open-collector, max 30V/8mA sink

Series/Parallel capability: Parallel สูงสุด 3 units, Series สูงสุด 2 units (ไม่ใช้ใน rig นี้)

Interface: USB (TypeA Host/TypeB Slave, 1.1/2.0, CDC class), LAN, GPIB (option ผ่าน GUG-001 adapter)

── Environmental ──
Operating temp: 0–50°C     Storage temp: -25–70°C
Operating humidity: 20–85%RH (no condensation)   Storage humidity: ≤90%RH (no condensation)
Altitude: max 2000m

── General ──
Weight: ~7.5kg (1080W)      Dimensions (WxHxD): 214×124×350mm (1080W, 1/2 rack)
Cooling: forced air (internal fan)
EMC: EU EMC directive 2004/108/EC Class A
Safety: EU Low Voltage Directive 2006/95/EC, CE-marked
Withstand voltage: input–chassis 1500Vac/1min, input–output 3000Vac/1min, output–chassis 500Vdc/1min (30/40/80/160V models)
Insulation resistance: input–chassis ≥100MΩ@500Vdc, input–output ≥100MΩ@500Vdc, output–chassis ≥100MΩ@500Vdc
```

หมายเหตุจาก spec: "*2: Load Regulation... Measured at the sensing point in Remote Sense." — Load Regulation spec
(45mV ข้างบน) นับเฉพาะตอนต่อ remote sense เท่านั้น ถ้าใช้ local sense ตัวเลขจริงจะแย่กว่านี้

คำสั่ง SCPI เกี่ยวกับ bleeder resistor: `SYSTem:CONFigure:BLEeder[:STATe] {OFF|ON|AUTO|0|1|2}` — ยังไม่เจอคำสั่ง
สลับ local/remote sense ผ่าน SCPI (เป็นฮาร์ดแวร์ล้วน — ถอด/ใส่ joining plate)

## Remote Sense — ขั้นตอนต่อสาย (ทั้ง 2 เครื่อง)

**PEL-3111** (จาก UM-PEL-3000H หน้า 42):
1. ปิดไฟเครื่อง Load (หรือเข้า standby)
2. ปิดไฟฝั่ง DUT (แบต)
3. **ต่อสายกำลัง (force) เข้า DUT ให้เสร็จก่อน**
4. ต่อสาย sense: `+S` → ขั้วบวกของ DUT โดยตรง, `-S` → ขั้วลบของ DUT โดยตรง

**⚠️ คำเตือนจากคู่มือ (สำคัญมาก)**: ถ้าต่อสาย sense เข้า DUT ก่อนที่สาย force จะต่อเสร็จ/แน่น เครื่องจะเข้าใจผิดว่า
กระแสไหลผ่านขา sense แทน ทำให้ **ฟิวส์ภายในเสีย** (เข้า high-impedance state จาก over-temperature) ต้องรอเครื่องเย็น
ก่อนถึงจะใช้ได้ใหม่ — เรียงลำดับ force ก่อน sense เสมอ

Terminal เป็นแบบ M3 screw ตามคู่มือ (ของจริงที่ PEL-3111 เจอเป็น banana jack เล็กสีแดง/ดำ ที่ด้านหลังเครื่อง ตรงบล็อก
"DC INPUT 1050W 1.5V-150V")

**PSW** (จาก UM_PSW_EN):
```
Local sense wiring:   -S↔-V (jumper plate — ค่า default จากโรงงาน)   +S↔+V (jumper plate)
Remote sense wiring:  -S ── สายไปขั้วลบ DUT     +S ── สายไปขั้วบวก DUT
```
**⚠️ ต้องถอด "sense joining plate" ออกก่อน** ถึงจะต่อ remote sense ได้จริง — ไม่งั้นสาย sense ใหม่จะไปชนกับแผ่น
jumper เดิม ทำให้ Kelvin sensing ไม่ทำงานแม้ต่อสายไปแบตแล้วก็ตาม (M4 screw หรือ M8 bolt terminal)

**Analog Control connector (26-pin, PSW)** มีขา `D COM`/`A COM` ที่ auto-switch ตามสถานะ sense (ต่อกับ -S เมื่อใช้
remote sense, ต่อกับขั้วลบ output เมื่อไม่ใช้) — ไม่ได้ใช้งานในระบบเรา บันทึกไว้เผื่ออนาคต

**เงื่อนไขจากสเปก (PEL-3111)**: "All specifications apply when using the rear panel terminals. If the front panel
terminals are used or if operating with long cables, remote sense must be connected." — rig ต่อสายยาวจาก Load
ไปแบตบนโต๊ะ → เข้าเงื่อนไข "long cables" ต้องต่อ remote sense จึงจะได้ความแม่นยำตามสเปก

## LinkView (GW Instek PC Software) — เปรียบเทียบฟีเจอร์

LinkView คือซอฟต์แวร์ PC GUI สำเร็จรูปที่ GW Instek แจกมาพร้อมเครื่อง (เขียนด้วย LabVIEW ของ NI) คุมได้ทั้ง
PSW/PSU/PFR/PSB/GPP/PSR/PHX (ฝั่ง power) และ PEL-2KA(B)/PEL-3KA(H) (ฝั่ง load) รวมถึงรุ่น ODM

**สถาปัตยกรรม**: Channel Mapping (จับคู่ Power+Load เป็น channel, **รองรับหลาย channel พร้อมกัน**) → Pattern Edit
(Discharge/Charge/Sleep 3 action พื้นฐาน + End-Condition + Error-Detection hi/lo) → Testing (Play/Stop, กราฟ
real-time) → Analyzing (ดู log แบบ tree, export Excel/รูปภาพ) → Options (path, logging rate)

**ข้อจำกัดที่เจอในคู่มือ**: "the primary difference between the licensed and unlicensed versions is the data
logging capability" — เวอร์ชันไม่มี license **บันทึกข้อมูลไม่ได้เลย**

| ด้าน | LinkView | `aset_batt` |
|---|---|---|
| ขอบเขต | ควบคุมเครื่องมือทั่วไป (generic) | เฉพาะทาง battery testing/grading |
| การประเมินผล | Error Detection hi/lo pass/fail เท่านั้น ไม่มี SoC/SoH/Rin estimation | ECM 1RC/2RC, EKF SoC, HPPC-grading (A/B/C/REJECT), harness compensation, Peukert |
| โปรไฟล์การชาร์จ | ตั้ง V/I ตรงๆ ไม่รู้จักเคมี | 3-stage เฉพาะ lead-acid, CC-CV เฉพาะ Li-ion/LFP |
| Multi-channel | ✅ รองรับในตัว | ❌ ออกแบบสำหรับ 1 ริกต่อครั้ง |
| มาตรฐานอุตสาหกรรม | ไม่มี IEC 61960/ISA-101 | IEC 61960-compliant, ISA-101 HMI |
| รายงาน | Export Excel/รูปภาพ | PDF report, cloud dashboard (Azure) |
| License | Log ข้อมูลถูกล็อกด้วย license | ไม่มีข้อจำกัดแบบนี้ |

**สรุป**: LinkView เป็นเครื่องมือ "ควบคุม+บันทึก" ระดับล่าง ไม่มีชั้นวิเคราะห์สุขภาพแบตเลย — `aset_batt` สร้างชั้น
วิเคราะห์เฉพาะทางทับบนฐานเดียวกัน (SCPI) แต่ลึกกว่ามาก จุดเดียวที่ LinkView ได้เปรียบคือ multi-channel parallel testing

## ภาคผนวก: SCPI Command Index แบบครบ (จาก Programming Manual ทั้ง 2 เล่ม)

รวมคำสั่งทั้งหมด (198 คำสั่งจาก PEL-3000H, 74 คำสั่งจาก PSW) — ไม่ได้ใส่ Syntax/ตัวอย่างเต็มทุกอันเพราะจะยาวเกินไป
(ดู syntax เต็มในไฟล์ PDF ต้นฉบับถ้าต้องใช้จริง) แต่ครบทุกชื่อคำสั่งที่มีในคู่มือ

### PEL-3111 / PEL-3000(H) — SCPI Command Index

**Current subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:CURRent:DUTY` | Sets and queries for "Duty" % of the CC dynamic mode |
| `:CURRent:FALL` | Sets and queries for the falling of the current slew rate of the CC dynamic mode |
| `:CURRent:FREQuency` | Sets and queries for "Frequency" value of the CC dynamic mode |
| `:CURRent:LEVel` | Sets and queries for the "Level" % of the CC dynamic mode |
| `:CURRent:RECall` | Sets or queries whether A Value or B Value is the currently active value in CC static mode |
| `:CURRent:RISE` | Sets and queries for the rising current slew rate of the CC dynamic mode |
| `:CURRent:SET` | Sets and queries for the "Set" current of the CC dynamic mode |
| `:CURRent:SRATe` | Sets and queries for the current slew rate of CC static mode |
| `:CURRent:VB` | Sets and queries for the "B Value" current of the CC static mode |
| `:CURRent[:VA]` | Sets and queries for the "A Value" current of the CC static mode |
| `:CURRent[:VA]:TRIGgered` | Set the current value when the trigger is activated |

**Resistance subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:RESistance:DUTY` | Sets and queries for "Duty" % of the CR dynamic mode |
| `:RESistance:FALL` | Sets and queries for the falling current slew rate of the CR dynamic mode |
| `:RESistance:FREQuency` | Sets and queries for "Frequency" value of the CR dynamic mode |
| `:RESistance:LEVel` | Sets and queries for the "Level" % (percentage of the Set conductance value) of the CR dynamic mode |
| `:RESistance:RECall` | Sets or queries whether A Value or B Value is the currently active value in CR static mode when... |
| `:RESistance:RISE` | Sets and queries for the rising current slew rate of the CR dynamic mode |
| `:RESistance:SET` | Sets and queries for the "Set" resistance of the CR dynamic mode |
| `:RESistance:SRATe` | Sets and queries for the current slew rate of CR static mode |
| `:RESistance:VB` | Sets and queries for the "B Value" resistance of the CR static mode |
| `:RESistance[:VA]` | Sets and queries for the "A Value" resistance of the CR static mode |
| `:RESistance[:VA]:TRIGgered` | The command determines how long to delay any action after a trigger is received |

**Conductance subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:CONDuctance:RECall` | Sets or queries whether A Value or B Value is the currently active value in CR static mode when the... |
| `:CONDuctance:SET` | Sets and queries for the "Set" conductance of the CR dynamic mode |
| `:CONDuctance:VB` | Sets and queries for the "B Value" conductance of the CR static mode |
| `:CONDuctance[:VA]` | Sets and queries for the "A Value" conductance of the CR static mode |
| `:CONDuctance[:VA]:TRIGgered` | Set the conductance value when the trigger is activated |

**Voltage subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:VOLTage:RECall` | Sets or queries whether A Value or B Value is the currently active value in CV mode |
| `:VOLTage:VB` | Sets and queries for the CV mode "B Value" |
| `:VOLTage[:VA]` | Sets and queries for the CV mode "A Value" voltage or the +CV voltage value |

**Power subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:POWer:DUTY` | Sets and queries for "Duty" % of the CP dynamic mode |
| `:POWer:FREQuency` | Sets and queries for "Frequency" value of the CP dynamic mode |
| `:POWer:LEVel` | Sets and queries for the "Set" power of the CP dynamic mode |
| `:POWer:RECall` | Sets or queries whether A Value or B Value is the currently active value in CP mode |
| `:POWer:SET` | Sets and queries for the "Set" power of the CP dynamic mode |
| `:POWer:Set` | Sets and queries for the "Timer1" time of CP dynamic mode |
| `:POWer:VB` | Sets and queries for the "B Value" power of the CP static mode |
| `:POWer[:VA]` | Sets and queries for the "A Value" power of the CP static mode |

**Mode subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:MODE` | Sets and queries for the operating modes |
| `[:MODE]:CRANge` | Sets and queries for the current range of all operating modes (HIGH/MIDDle/LOW) |
| `[:MODE]:VRANge` | Sets and queries for the voltage range of all operating modes (HIGH/LOW) |
| `[:MODE]:RESPonse` | Sets and queries for the response speed of CV and +CV mode |
| `[:MODE]:DYNamic` | Sets and queries for the Dynamic/Static switching function |

**Configure subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:CONFigure:DYNamic` | Sets and queries for the setting conditions of dynamic mode |
| `:CONFigure:MEMory` | This command configures the how the files are recalled Local operation mode |
| `:CONFigure:RESPonse` | Sets and queries for the response speed of the CC, CR and CP mode |
| `:CONFigure:SHORt` | Sets and queries for the short key behavior |
| `:CONFigure:SHORt:FUNCtion` | Enables or disables the short function by short key |
| `:CONFigure:SHORt:SAFety` | Turns the Short Safety function on/off |
| `:CONFigure:STATus` | Sets the mode used for the set resolution when using the scroll wheel to edit parameters |
| `[:CONFigure]:OCP/OPP/UVP/OVP` | Sets/queries trip settings for over-current/over-power/under-voltage/over-voltage protection |
| `[:CONFigure]:SSTart` | Sets and queries for the Soft Start time setting |
| `[:CONFigure]:VON`/`:VDELay` | Sets and queries for the Von voltage/delay settings and latch |
| `[:CONFigure]:CNTime`/`:COTime` | Count Timer function / load cutoff time setting |
| `[:CONFigure]:CRUnit` | Sets and queries for the CR mode setting units (OHM/MHO) |
| `[:CONFigure]:GNG:*` | Go-NoGo test settings (mode/high/low/center/delay/spec-test/pass-result) |
| `[:CONFigure]:PARallel` | Configures the unit for parallel operation (Master/Slave/Booster) — ไม่ใช้ในrig นี้ |
| `[:CONFigure]:STEP:*` | Step resolution settings for CC/CR/CV/CP modes (H/M/L range each) |
| `[:CONFigure]:EXTernal:*` | External control settings (CC/CR/CV/CP external voltage/resistance control) |

**Program subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:PROGram` | Sets and queries for all parameters to specified step of the program function |
| `:PROGram:MEMory` | Sets and queries for memory number of selected program steps |
| `:PROGram:OFFTime` | Sets and queries for Off time of selected program steps |
| `:PROGram:ONTime` | Sets and queries for On time of selected program steps |
| `:PROGram:PFTime` | Sets and queries for Off time of selected program steps |
| `:PROGram:RUN` | Sets and queries for execution process of selected program steps |
| `:PROGram:SAVE` | Save program |
| `:PROGram:STARt` | Sets and queries for select program number |
| `:PROGram:STATe` | Sets and queries for the state of the program function |
| `:PROGram:STEP` | Sets and queries for the step number of the program to select |
| `:PROGram:STIMe` | Sets and queries for Off time of selected program steps |
| `:PROGram[:RECall]:DEFault` | All steps of a selected program are set by default value |

**Nsequence subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:NSEQuence` | Sets and queries for parameters of the Normal Sequence function |
| `:NSEQuence:CHAin` | Query and settings for the next sequence in the chain |
| `:NSEQuence:COTime` | Sets and queries for the display timer of the normal sequence |
| `:NSEQuence:EDIT` | Sets and queries for the data edit of normal sequence |
| `:NSEQuence:EDIT:END` | Returns the end of edit step number in the normal sequence |
| `:NSEQuence:EDIT:POINt` | Sets and queries for the edit step number of the normal sequence |
| `:NSEQuence:LAST` | Sets and queries for load value after the end of the normal sequence |
| `:NSEQuence:LLOAD` | Sets and queries for the Last Load state after the end of the normal sequence |
| `:NSEQuence:LOOP` | Sets and queries for number of loops of normal sequence |
| `:NSEQuence:MEMO` | Sets and queries for the memo string of normal sequence |
| `:NSEQuence:MODE` | Sets and queries for the operating mode of the selected normal sequence |
| `:NSEQuence:NUMBer` | Sets and queries for the sequence number of the normal sequence |
| `:NSEQuence:RANGe` | Sets and queries for the operating range of the selected normal sequence |
| `:NSEQuence:SAVE` | Save program of normal sequence |
| `:NSEQuence:STARt` | Sets and queries for the start sequence number of the normal sequence |
| `:NSEQuence:STATe` | Sets and queries for the state of the Normal Sequence function |
| `:NSEQuence[:DELet]:ALL` | Delete all the steps of the selected normal sequence |

**Fsequence subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:FSEQuence` | Sets and queries for parameters of fast sequence |
| `:FSEQuence:EDIT` | Sets and queries for data of fast sequence |
| `:FSEQuence:EDIT:END` | Returns the end of edit step number in the fast sequence |
| `:FSEQuence:EDIT:POINt` | Sets and queries for the edit step number of the fast sequence |
| `:FSEQuence:LAST` | Sets and queries for the Load Value after the end of Fast sequence |
| `:FSEQuence:LLOAD` | Sets and queries for the Last Load state of Fast sequence |
| `:FSEQuence:LOOP` | Sets and queries for number of loops of fast sequence |
| `:FSEQuence:MEMO` | Sets and queries for the memo of fast sequence |
| `:FSEQuence:MODE` | Sets and queries for the operating mode of fast sequence |
| `:FSEQuence:RANGe` | Sets and queries for the operating range of the fast sequence |
| `:FSEQuence:RPTStep` | Sets and queries for the last step number per loop of the fast sequence |
| `:FSEQuence:SAVE` | Save program of fast sequence |
| `:FSEQuence:STATe` | Sets and queries for the state of the fast sequence function |
| `:FSEQuence:TBASe` | Sets and queries for the time-based of fast sequence |
| `:FSEQuence[:DELet]:ALL` | Delete all programs of the fast sequence |
| `:FSEQuence[:EDIT]:FILL` | Query and setting for FILL of fast sequence |

**Ocp subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:OCP:CHANnel:STATus` | Queries the status of the OCP Test Automation function |
| `:OCP:EDIT[:CHANnel]` | Sets and queries for the settings of the selected OCP memory number |
| `:OCP:MEMO` | Sets and queries for user-created note of the currently selected OCP function |
| `:OCP:RESult` | Returns the OCP Test Automation results |
| `:OCP:RUN` | Turns the load on for the OCP Test Automation function |
| `:OCP:STATe` | Sets and queries for the state of the OCP function |
| `:OCP[:CHANnel]:DELay` | Sets and queries for the test delay time of the OCP Test Automation function |
| `:OCP[:CHANnel]:END` | Sets and queries for the ending current value of the test |
| `:OCP[:CHANnel]:LAST` | Sets and queries for the current value of after the DUT OCP protection has been activated |
| `:OCP[:CHANnel]:NUMBer` | Sets and queries for the OCP memory number |
| `:OCP[:CHANnel]:RANGe` | Sets and queries for the channel range |
| `:OCP[:CHANnel]:STARt` | Sets and queries for the starting current value |
| `:OCP[:CHANnel]:STEP:CURRent` | Sets and queries for the current step resolution of the OCP Test Automation |
| `:OCP[:CHANnel]:STEP:TIME` | Sets and queries for how long the step times of the OCP Test Automation function |
| `:OCP[:CHANnel]:TRIGger` | Sets and queries for the voltage trigger for when the power supply OCP has been triggered |

**Opp subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:OPP:CHANnel:STATus` | Queries the status of the OPP Test Automation function |
| `:OPP:EDIT[:CHANnel]` | Sets and queries for the settings of the selected OPP memory number |
| `:OPP:MEMO` | Sets and queries for user-created note of the currently selected OPP function |
| `:OPP:RESult` | Returns the OPP Test Automation results |
| `:OPP:RUN` | Turns the load on for the OPP Test Automation function |
| `:OPP:STATe` | Sets and queries for the state of the OPP function |
| `:OPP[:CHANnel]:DELay` | Sets and queries for the test delay time of the OPP Test Automation function |
| `:OPP[:CHANnel]:END` | Sets and queries for the ending watt value of the test |
| `:OPP[:CHANnel]:LAST` | Sets and queries for the watt value of after the DUT OPP protection has been activated |
| `:OPP[:CHANnel]:NUMBer` | Sets and queries for the OPP memory number |
| `:OPP[:CHANnel]:RANGe` | Sets and queries for the channel range |
| `:OPP[:CHANnel]:STARt` | Sets and queries for the starting current value |
| `:OPP[:CHANnel]:STEP:TIME` | Sets and queries for how long the step times of the OPP Test Automation function |
| `:OPP[:CHANnel]:STEP:WATT` | Sets and queries for the current step resolution of the OPP Test Automation |
| `:OPP[:CHANnel]:TRIGger` | Sets and queries for the voltage trigger for when the power supply OPP has been triggered |

**Battery subsystem** (native BATT Test Automation — ดู `rig_investigation_findings.md` สำหรับ bug-check กับโค้ดเรา)

| Command | คำอธิบายย่อ |
|---|---|
| `:BATTery:DATalog:TIMer` | Sets and queries for the time interval for data capture |
| `:BATTery:FALL` | Sets and queries for the test falling slew rate in mA/us |
| `:BATTery:MEMO` | Sets and queries for user-created note of the currently selected BATT function |
| `:BATTery:MODE` | Sets and queries for the operation mode |
| `:BATTery:RANGe` | Sets and queries for the channel range |
| `:BATTery:RISE` | Sets and queries for the test rising slew rate in mA/us |
| `:BATTery:STATe` | Sets and queries for the state of the BATT function |
| `:BATTery:STOP:AH` | Sets and queries for the discharged energy rate at which the test should be interrupted |
| `:BATTery:STOP:TIME` | Sets and queries for the time after which the test should be interrupted |
| `:BATTery:STOP:VOLTage` | Sets and queries for the voltage at which the test should be interrupted |
| `:BATTery:VALue` | Sets and queries for the setting value of the selected operation mode |
| `:BATT:CHANnel:STATus` | Queries the status of the BATT Test Automation function |
| `:BATT:EDIT` | Sets and queries for the settings of the selected BATT memory number (all 10 params at once) |
| `:BATT:RESult` | Returns the BATT Test Automation results (current, voltage — NOT accumulated Ah/Wh) |
| `:BATT:RUN` | Turns the load on for the BATT Test Automation function |

**Input subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:INPut` | Sets and queries for the status of the load |
| `:INPut:MODE` | Sets and queries for the operating function of the load |
| `:INPut[:STATe]:TRIGgered` | Sets whether to turn on the load input when the trigger is activated |

**Measure subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:MEASure:CURRent` | Query of current measurement |
| `:MEASure:ETIMe` | Query of the elapsed time of the load-on |
| `:MEASure:POWer` | Query of power measurement |
| `:MEASure:VOLTage` | Query of voltage measurement |

**Fetch subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:FETCh:CURRent` | This query returns the real-time current of the load input |
| `:FETCh:POWer` | This query returns the real-time power of the load input |
| `:FETCh:VOLTage` | This query returns the real-time voltage of the load input |

**Utility subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:UTILity:ALARm` | Sets and queries for the speaker sound of the alarm |
| `:UTILity:BRATe` | Sets and queries for the baud rate of RS-232C |
| `:UTILity:BRIghtness` | Sets and queries for brightness of the LCD display |
| `:UTILity:CONTrast` | Sets and queries for the contrast of the LCD display |
| `:UTILity:GNG` | Sets and queries for the speaker sound of the "Go-NoGo" judgment time |
| `:UTILity:INTerface` | Sets and queries for the interface |
| `:UTILity:KNOB` | Sets and queries for operational setting of the knob |
| `:UTILity:LANGuage` | Sets and queries for the language of the operation panel |
| `:UTILity:LOAD` | Sets and queries for Auto Load and load function at power on |
| `:UTILity:LOAD:MODE` | Sets and queries for the Load Off (Mode) setting |
| `:UTILity:LOAD:RANGe` | Sets and queries for the Load Off (Range) setting |
| `:UTILity:PARity` | Sets and queries for the parity bit of RS-232C interface |
| `:UTILity:REMote` | Turns the remote control on or off |
| `:UTILity:REMote:MODE` | Sets the remote mode to fast or normal |
| `:UTILity:SBIT` | Sets and queries for the stop bit of the RS-232C interface |
| `:UTILity:SPEAker` | Sets and queries for the speakers sound during scrolling and key input |
| `:UTILity:SYSTem` | Query for model number, serial number, and firmware version |
| `:UTILity:TIME` | Sets and queries for the date and time |
| `:UTILity:UNReg` | Sets and queries for the speaker sound of Anne-regulation |

**Memory/Preset/Setup/Factory/User subsystems**

| Command | คำอธิบายย่อ |
|---|---|
| `:MEMory:RECall` | Recall settings from the internal memory |
| `:MEMory:SAVE` | Save in the internal memory of the specified |
| `:PREset:RECall` | Recall settings from the preset memory |
| `:PREset:SAVE` | Save to the preset memory of the specified |
| `:SETup:RECall` | Recall settings from the setup data |
| `:SETup:SAVE` | Save to the setup data of the specified |
| `:FACTory[:RECall]` | Sets factory defaults |
| `:USER[:DEFault]:RECall` | Recall the default settings for the user |
| `:USER[:DEFault]:SAVE` | Save to the default settings for the user |

**Status subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `:STATus:CSUMmary:CONDition` | Query the Csummary Status Condition register |
| `:STATus:CSUMmary:ENABle` | Sets and queries for the Event Enable register of Csummary |
| `:STATus:CSUMmary:NTRansition` | Sets and queries for detection bit of Csummary status of changes of from positive to negative |
| `:STATus:CSUMmary:PTRansition` | Sets and queries for detection bit of Csummary status of changes of from negative to positive |
| `:STATus:CSUMmary[:EVENt]` | Query and setting for the Event register of Csummary |
| `:STATus:OPERation:CONDition` | Query the Operation Status Condition register |
| `:STATus:OPERation:ENABle` | Sets and queries for the Event Enable register of Operation |
| `:STATus:OPERation:NTRansition` | Sets and queries for detection bit of Operation status of changes of from positive to negative |
| `:STATus:OPERation:PTRansition` | Sets and queries for detection bit of Operation status of changes of from negative to positive |
| `:STATus:OPERation[:EVENt]` | Query for the Event register of Operation |
| `:STATus:PRESet` | Is the setting of the initial value for the Csummary status and the Questionable status and the... |
| `:STATus:QUEStionable:CONDition` | Query the Questionable Status Condition register |
| `:STATus:QUEStionable:ENABle` | Sets and queries for the Event Enable register of Questionable |
| `:STATus:QUEStionable:NTRansition` | Sets and queries for detection bit of Questionable status of changes of from positive to negative |
| `:STATus:QUEStionable:PTRansition` | Sets and queries for detection bit of Questionable status of changes of from negative to positive |
| `:STATus:QUEStionable[:EVENt]` | Query for the Event register of Questionable |

**Trigger/Initiate/Abort/Function/System subsystems**

| Command | คำอธิบายย่อ |
|---|---|
| `:TRIGger[:DELay]:TIME` | The command determines how long to delay any action after a trigger is received |
| `:TRIGger[:PULSe]:WIDTh` | Sets and queries for the trigger output signal's pulse width |
| `:INITiate:CONTinuous` | Sets or queries for state of the continuous waiting for the trigger |
| `:INITiate[:IMMediate]` | Sets the trigger to the wait state |
| `:ABORt` | Clears the trigger wait status and returns to the idle state |
| `:FUNCtion[:COMPlete][:RING]:TIME` | Sets and queries for how long the alarm will buzz for after a program, NSEQ, FSEQ or OCP test... |
| `:SYSTem:ERRor` | Queries the error queue |

### PSW 80-40.5 / PSW-Series — SCPI Command Index

**Source subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `[SOURce:]CURRent:PROTection:STATe` | Turns OCP (over-current protection) on or off |
| `[SOURce:]CURRent:PROTection[:LEVel]` | Sets or queries the OCP (over-current protection) level in amps |
| `[SOURce:]CURRent:SLEW:FALLing` | Sets or queries the falling current slew rate |
| `[SOURce:]CURRent:SLEW:RISing` | Sets or queries the rising current slew rate |
| `[SOURce:]CURRent[:LEVel]:TRIGgered[:AMPLitude]` | Sets or queries the current level in amps when a software trigger has been generated |
| `[SOURce:]CURRent[:LEVel][:IMMediate][:AMPLitude]` | Sets or queries the current level in amps |
| `[SOURce:]RESistance[:LEVel][:IMMediate][:AMPLitude]` | Sets or queries the internal resistance in ohms (0–1.975Ω สำหรับ PSW 80-40.5) |
| `[SOURce:]VOLTage:PROTection[:LEVel]` | Sets or queries the overvoltage protection level |
| `[SOURce:]VOLTage:SLEW:FALLing` | Sets or queries the falling voltage slew rate |
| `[SOURce:]VOLTage:SLEW:RISing` | Sets or queries the rising voltage slew rate |
| `[SOURce:]VOLTage[:LEVel]:TRIGgered[:AMPLitude]` | Sets or queries the voltage level in volts when a software trigger has been generated |
| `[SOURce:]VOLTage[:LEVel][:IMMediate][:AMPLitude]` | Sets or queries the voltage level in volts |

**Output subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `OUTPut:DELay:OFF` | Sets the Delay Time in seconds for turning the output off |
| `OUTPut:DELay:ON` | Sets the Delay Time in seconds for turning the output on |
| `OUTPut:MODE` | Sets the PSW output mode |
| `OUTPut:PROTection:CLEar` | Clears over-voltage, over-current and over-temperature (OVP, OCP, OTP) protection circuits |
| `OUTPut:PROTection:TRIPped` | Returns the state of the protection circuits (OVP, OCP, OTP) |

**Measure subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `MEASure[:SCALar]:ALL[:DC]` | Queries the measured voltage, current and power simultaneously |
| `MEASure[:SCALar]:CURRent[:DC]` | Queries the measured output current |
| `MEASure[:SCALar]:VOLTage[:DC]` | Queries the measured output voltage |
| `MEASure[:SCALar]:POWer[:DC]` | Queries the measured output power |

**Initiate subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `INITiate[:IMMediate]:NAME` | Initiates the trigger system for TRANsient or OUTPut |

**Display subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `DISPlay:BLINk` | Turns blink on or off for the display |
| `DISPlay:MENU[:NAME]` | The DISPlay MENU command selects a screen menu or queries the current screen menu |

**Trigger subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `TRIGger:OUTPut:SOURce` | Sets or queries the trigger source for the output system |
| `TRIGger:OUTPut[:IMMediate]` | Generates a software trigger for the output trigger system |
| `TRIGger:TRANsient:SOURce` | Sets or queries the trigger source for the transient system |
| `TRIGger:TRANsient[:IMMediate]` | Generates a software trigger for the transient trigger system |

**Status subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `STATus:OPERation:CONDition` | Queries the Operation Status register |
| `STATus:OPERation:ENABle` | Sets or queries the bit sum of the Operation Status Enable register |
| `STATus:OPERation:NTRansition` | Sets or queries the bit sum of the negative transition filter of the Operation Status register |
| `STATus:OPERation:PTRansition` | Sets or queries the bit sum of the positive transition filter of the Operation Status register |
| `STATus:OPERation[:EVENt]` | Queries the Operation Status Event register and clears the contents of the register |
| `STATus:PRESet` | This command resets the ENABle register, the PTRansistion filter and NTRansistion filter on the... |
| `STATus:QUEStionable:CONDition` | Queries the status (bit sum) of the Questionable Status register |
| `STATus:QUEStionable:ENABle` | Sets or queries the bit sum of the Questionable Status Enable register |
| `STATus:QUEStionable:NTRansition` | Sets or queries the negative transition filter of the Questionable Status register |
| `STATus:QUEStionable:PTRansition` | Sets or queries the bit sum of the positive transition filter of the Questionable Status register |
| `STATus:QUEStionable[:EVENt]` | Queries the bit sum of the Questionable Status Event register |

**System subsystem**

| Command | คำอธิบายย่อ |
|---|---|
| `SYSTem:BEEPer[:IMMediate]` | This command causes an audible tone to be generated by the instrument |
| `SYSTem:COMMunicate:ENABle` | Enables/Disables LAN, GPIB or USB remote interfaces as well as remote services (Sockets, Web Server) |
| `SYSTem:COMMunicate:GPIB[:SELF]:ADDRess` | Sets or queries the GPIB address |
| `SYSTem:COMMunicate:LAN:DHCP` | Turns DHCP on/off |
| `SYSTem:COMMunicate:LAN:DNS` | Sets or queries the DNS address |
| `SYSTem:COMMunicate:LAN:GATEway` | Sets or queries the Gateway address |
| `SYSTem:COMMunicate:LAN:HOSTname` | Queries the host name |
| `SYSTem:COMMunicate:LAN:IPADdress` | Sets or queries LAN IP address |
| `SYSTem:COMMunicate:LAN:MAC` | Returns the unit MAC address as a string |
| `SYSTem:COMMunicate:LAN:SMASk` | Sets or queries the LAN subnet mask |
| `SYSTem:COMMunicate:LAN:WEB:PACTive` | Sets or queries whether the web password is on or off |
| `SYSTem:COMMunicate:LAN:WEB:PASSword` | Sets or queries the web password |
| `SYSTem:COMMunicate:RLSTate` | Sets or queries the control state of the instrument |
| `SYSTem:COMMunicate:USB:FRONt:STATe` | Queries the front panel USB-A port state |
| `SYSTem:COMMunicate:USB:REAR:MODE` | Sets or queries the rear panel USB-B port mode |
| `SYSTem:COMMunicate:USB:REAR:STATe` | Queries the rear panel USB-B port state |
| `SYSTem:CONFigure:BEEPer[:STATe]` | Sets or queries the buzzer state on/off |
| `SYSTem:CONFigure:BLEeder[:STATe]` | **Sets or queries the status of the bleeder resistor** ({OFF\|ON\|AUTO}) — ดู `rig_investigation_findings.md` |
| `SYSTem:CONFigure:BTRip:PROTection` | Enables/Disables the power switch trip (circuit breaker) when the OVP or OCP protection settings... |
| `SYSTem:CONFigure:BTRip[:IMMediate]` | Trips the power switch trip (circuit breaker) to turn the unit off (shut down the power) |
| `SYSTem:CONFigure:CURRent:CONTrol` | Sets or queries the CC control mode (local control (panel), external voltage control, external... |
| `SYSTem:CONFigure:MSLave` | Sets or queries the unit operation mode |
| `SYSTem:CONFigure:OUTPut:EXTernal[:MODE]` | Sets the external logic as active high or active low |
| `SYSTem:CONFigure:OUTPut:PON[:STATe]` | Sets the unit to turn the output ON/OFF at power-up |
| `SYSTem:CONFigure:VOLTage:CONTrol` | Sets or queries the CV control mode (local control, external voltage control, external resistance... |
| `SYSTem:ERRor` | Queries the error queue |
| `SYSTem:INFormation` | Queries the system information |
| `SYSTem:KEYLock:MODE` | Sets or queries the key lock mode |
| `SYSTem:KLOCk` | Enables or disables the front panel key lock |
| `SYSTem:PRESet` | Resets all the settings to the factory default settings |
| `SYSTem:VERSion` | Returns the version of the SCPI specifications that the unit complies with |

## แหล่งอ้างอิง (ไฟล์ manual ที่อ่านแล้ว)

**PEL-3111 (e-Load)**: `UM-PEL-3000H_EN_Rev_B_20220914.pdf`, `PEL-3000H_ProgrammingManual_EN_20190401.pdf`,
`PEL-3000_Spec_E.pdf` — มีข้อมูลใช้จริงหมด

**PSW 80-40.5 (PSU)**: `UM_PSW_EN_Rev_N_20220906.pdf`, `PSW-Series_Spec_E_20220802.pdf`,
`PSW_programming_manual_EN_Ver_2_20241104-1.pdf` — มีข้อมูลใช้จริงหมด

**เช็คแล้ว ไม่เกี่ยวกับ rig นี้ (เปิดอ่านจริงแล้ว ไม่ใช่แค่เดาจากชื่อไฟล์)**:
- `PEL-3000H_Quick_Start_Guide`, `BH_PEL-30003000H_E`, `AM_GRA-413_414_PEL-3000_3000H`, `setup_guide_manual` —
  ซ้ำกับเนื้อหาใน UM-PEL-3000H
- `PEL-3000_Parallel_Assembly_Guide`, `PEL-3211H_Booster_*` (3 ไฟล์) — parallel/booster หลายเครื่อง เรามีเครื่องเดียว
- `RackPartsDetails_PEL-3000` — rack mount ทางกล
- `BH_PSW-Series_E202604`, `AM_GRA-410_PSW` (GRA-410 rack mount kit), `GW-PSW_QSG_EN`, `20110216_UM_GUG-001_E`
  (GPIB-to-USB adapter, ไม่ใช้ GPIB) — ยืนยันแล้วไม่เกี่ยวข้อง
- `20160503_UM_PSW-series_GUR-001_E` (RS-232C-to-USB adapter) — **ไม่แน่ใจ 100%** อาจเกี่ยวถ้า rig ต่อ PSU ผ่าน
  adapter ตัวนี้จริง (VISA address เป็น `ASRL5::INSTR` เข้าได้กับการต่อผ่าน serial adapter) แต่ไม่มีข้อมูลยืนยัน —
  ต้องเช็คฮาร์ดแวร์จริงถ้าอยากรู้แน่ชัด
- ไฟล์ `.7z`/`.rar`/`.zip` ทั้งหมด (`LVdriver_*`, `USB_PEL-3000_*`, `VBA_SampleProgram_*`) — ไดรเวอร์ LabVIEW/VBA
  ไม่ใช่เอกสาร
