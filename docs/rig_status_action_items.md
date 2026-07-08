# Rig Status / สิ่งที่ต้องทำต่อ

สรุปสถานะล่าสุดของการสืบสวน harness-resistance/OCV-anchor bug (ก.ค. 2026) — **อ่านไฟล์นี้ก่อนไฟล์อื่น** เป็นจุดเดียว
ที่บอกว่า "ต้องทำอะไรต่อ" ไม่ต้องไล่อ่านไฟล์อ้างอิง/บันทึกการสืบสวนทั้งหมด

รายละเอียดเชิงเทคนิคของแต่ละข้ออยู่ที่:
- [`pel3111_psw_hardware_reference.md`](pel3111_psw_hardware_reference.md) — สเปก, SCPI command index, ขั้นตอน wiring
- [`rig_investigation_findings.md`](rig_investigation_findings.md) — ที่มาของแต่ละข้อค้นพบ, การวิเคราะห์เต็ม

## สิ่งที่ต้องทำต่อ เรียงตามความคุ้มค่า (ต้นทุนต่ำ→สูง)

| # | เรื่อง | ต้นทุนเช็ค | ความเสี่ยงถ้าไม่ทำ | สถานะ |
|---|---|---|---|---|
| 1 | เช็ค PyVISA backend ว่าเสี่ยง USB BSOD ไหม | รันคำสั่งเดียว ~10 วินาที | เทสยาว (2-4+ ชม.) อาจล้มกลางคันจริง | ⚠️ เช็คบนเครื่อง dev แล้ว (native VISA DLL, `visa32.dll`) — **ต้องเช็คซ้ำบน PC lab จริง** เพราะอาจคนละเครื่อง |
| 1b | เรียก `calibrate_psu_zero()` อัตโนมัติ (แก้ offset 0.6A แบบ real-time ไม่ใช่แค่ post-test) | เขียนโค้ดในเครื่อง dev ได้เลย ไม่ต้องมีฮาร์ดแวร์ | ก่อนหน้านี้เป็น dead code (มีเมธอดแต่ไม่มีใครเรียก) — ค่า `_psu_current_offset` เป็น 0.0 เสมอ ตัวเลขสด (ไม่ใช่แค่หลังจบเทส) เพี้ยน 0.6A ตลอด | ✅ **แก้แล้ว** — เรียกใน `connect_esp32()` ทันทีหลัง `set_ssr(False)` (จุดที่ PSU แยกขาดจากแบตจริงตามเงื่อนไขเดิมของฟังก์ชัน) มี unit test 3 เคสใน `tests/test_psu_zero_calibration_hook.py` |
| 2 | ยืนยันว่าย้ายสาย sense (+S/-S) ไปแบตจริงหรือยัง | ต้องไปดู rig จริง (ถอด joining plate ของ PSW ด้วย) | ประโยชน์เรื่อง harness-resistance ทั้งหมดที่วิเคราะห์มายังไม่เกิดขึ้นจริงถ้ายังไม่ทำ | ✅ **user ยืนยันต่อสายเสร็จแล้ว** (ก.ค. 2026) — PSU/Load Force+/− แยกสายตรงไปแบตคนละเส้น (ไม่ผ่านกันเอง), S+/S− ของทั้งสองเครื่องต่อตรงไปขั้วแบต |
| 6 | ตั้ง hardware protection (OVP/OCP/UVP) + ล็อกหน้าเครื่อง + short-safety + beep + device-info อัตโนมัติตอน connect | เขียนโค้ดได้จากเครื่อง dev, SCPI verify แล้วจากคู่มือจริง (ไม่เดา) | ตอนนี้ safety พึ่งซอฟต์แวร์ล้วนๆ ถ้า PC ค้าง/แครช ฝั่ง Load ไม่มี hardware backstop เลย | ✅ **เขียนแล้ว** — `set_load_protection()`/`set_psu_protection()` (OCP margin 25%, OVP margin 10%, UVP = `safety_limits.min_voltage`) + `harden_instrument_config()` (ล็อกหน้าเครื่องทั้งคู่, ปิด PSU auto-power-on, เปิด Load Short-Safety + onboard alarm) เรียกอัตโนมัติใน `_on_connect()`, ปลดล็อกใน `_on_disconnect()`; `_log_alarm()` สั่ง beep ที่ PSU (`SYST:BEEP`) เฉพาะ event ระดับ ALARM จริง (ไม่ beep แค่ WARNING); connect ครั้งแรก log รุ่น/serial/firmware ของทั้งสองเครื่องไว้ (`get_instrument_info()`) เพื่อ traceability; ปุ่ม "Clear Trip" แมนนวลไว้ที่แท็บ MANUAL→Direct (ตั้งใจไม่ auto-clear) — ทดสอบใน `tests/test_instrument_protection.py` (25 เคส) + `tests/test_psu_trip_ui.py` (5 เคส) + `tests/test_alarm_beep_and_device_info.py` (4 เคส) — **ยังไม่ได้ทดสอบกับเครื่องจริง** ต้องเช็ค SYST:ERR? หลัง connect ครั้งแรกว่าคำสั่งถูก syntax จริงไหม |
| 7 | `[:CONFigure]:VON`/`:VDELay` (PEL) — ตัด load ถ้า DUT ไม่ถึงแรงดันที่กำหนดในเวลาที่กำหนด | verify แล้วว่า syntax คืออะไร แต่ยังไม่แน่ใจ semantics เต็มๆ ว่าใช้ป้องกันอะไรกันแน่ในบริบทแบต — ต้องอ่านเพิ่ม/ถามผู้ผลิต หรือทดสอบระวังๆ กับเครื่องจริงก่อน | ต่ำ — เป็นฟีเจอร์เสริม ไม่ใช่ safety gap ที่มีอยู่แล้ว | ❌ ตั้งใจไม่ทำตอนนี้ — เสี่ยงเกินไปที่จะเดา semantics แล้ว auto-apply |
| 8 | Native dynamic-mode (`CURRent:LEVel/DUTY/FREQuency/RISE/FALL` แบบ pulsed) หรือ `:PROGram`/`:NSEQuence` — ให้เครื่องรัน HPPC pulse/relax เองแทน PC-timed loop | ใหญ่ — เปลี่ยนสถาปัตยกรรม HPPC ทั้งหมดจาก PC-polling เป็น instrument-native timing (µs-level slew rate) | ต่อให้เพิ่ม polling rate เป็น 5Hz แล้ว (ดูข้อ 12) sub-200ms dynamics (ohmic step แท้ๆ ตอน t=0) ก็ยังจับตรงไม่ได้อยู่ดี — เพดานจริงของ SCPI-over-USB polling | ❌ ยังไม่ทำ — ต้องคุยแยกต่างหาก ไม่ใช่งานเล็ก ทำข้อ 12 (ของถูก) ไปก่อนแล้ว |
| 12 | เพิ่ม polling rate ของ HPPC pulse/relax loop จาก 1Hz → ~5Hz | เขียนโค้ดได้จากเครื่อง dev ล้วนๆ ไม่ต้องมีฮาร์ดแวร์ | `identify_ecm_fit()` เองสมมติไว้ว่าต้อง 5Hz ("30s pulse at 5Hz → ~150 points... R1/C1 are well-resolved at 5Hz") แต่ loop จริงเก็บได้แค่ 1Hz (~30 จุด) — R1/C1 fit ได้ข้อมูลน้อยกว่าที่ออกแบบไว้ 5 เท่า | ✅ **แก้แล้ว** — ใช้เทคนิคเดียวกับ `AutoController._monitor_loop` (sleep เฉพาะเวลาที่เหลือหลัง SCPI round-trip แทนบวก 1.0s ตายตัว) ที่ทั้ง relax leg และ pulse leg — real achieved rate จะต่ำกว่า 5Hz จริง (USB latency ~40-200ms) แต่ดีขึ้นกว่า 1Hz มาก ทดสอบใน `tests/test_hppc_5hz_pacing.py` (3 เคส, source-level เพราะ loop ข้าม wall-clock phase หลายจุดที่ mock time ยาก) |
| 9 | PREPARE phase (OCV-settle wait) ไม่เคย log ลง CSV เลย — `_quality_flags` ขึ้น "no clear rest before load" ทุกไฟล์แม้ rest จริงเกิดขึ้นแล้ว | เขียนโค้ดได้จากเครื่อง dev ล้วนๆ | confidence score ของทุกเทสถูกหักลดลงเพราะ false-positive warning — ยืนยันด้วย `test_20260706_185655.csv` จริง (confidence เหลือ 0.44) | ✅ **แก้แล้ว** — สาเหตุ: `_ensure_logging()` เดิมถูกเรียกที่ CHARGE/DISCHARGE phase แต่ `start_monitor()` (ผ่าน `start_charge()`) เปิดไฟล์ CSV ไปก่อนแล้วด้วยชื่อ generic ทำให้ label หาย และ PREPARE ไม่เคยถูกบันทึก — ย้าย `_ensure_logging()` ไปเรียกที่ PREPARE ของทั้ง 4 sequence + feed `_log_sample()`/`update_display()` เข้า `on_progress` callback ด้วย ทดสอบใน `tests/test_prepare_phase_rest_logging.py` (3 เคส) |
| 10 | Self-calibration: ใช้ PSW resistance-emulation (`[SOURce:]RESistance[:LEVel]`, 0–1.975Ω) จำลอง "แบต" ที่รู้ค่า R ชัดเจน มา validate measurement chain ทั้งสาย | เขียนโค้ดได้จากเครื่อง dev, SCPI verify แล้ว | ตอนนี้ validate ความแม่นยำ R0/DCIR ได้แค่เทียบกับมิเตอร์ ACIR ภายนอกครั้งเดียว ไม่มีทาง regression-test อัตโนมัติ | ✅ **เขียนแล้ว** — `hw.set_psu_resistance_emulation(ohms)` + `scripts/self_calibration_test.py` (จำลอง R, pulse ด้วย Load, รัน `analyze_csv()` ตัวเดียวกับที่ใช้จริง เทียบ R0/DCIR วัดได้กับค่า known) — **ยังไม่เคยรันกับเครื่องจริงเลย** เป็น hardware-only script (ตามธรรมเนียม `scripts/` ไม่มี pytest coverage) |
| 11 | เพิ่มโหมด CCA-proxy test ใน CHARACTERIZE tab | เขียนโค้ดได้จากเครื่อง dev | `cca_a` มีอยู่ใน `battery_profiles.json` ทุก product แต่ไม่เคยถูกใช้เทสอะไรเลยมาก่อน | ✅ **เขียนแล้ว** — การ์ดที่ 4 ใน CHARACTERIZE (ชาร์จเต็ม→พัก 5 นาที→pulse 30s ที่กระแส `min(cca_a, max_current)`→pass/fail กับ floor 1.2V/cell) **ไม่ใช่ CCA มาตรฐาน** (ไม่คุม 0°C, กระแสถูก clamp เพราะ rig ต่อสายไว้สำหรับแบตเล็ก ไม่รองรับกระแส CCA จริงเช่น 95A) — เตือนไว้ในหน้าจอชัดเจน ใช้เป็นตัวเทียบสุขภาพแบตกับตัวเองเท่านั้น — ทดสอบใน `tests/test_cca_proxy_test.py` (5 เคส) |
| 3 | ตัดสินใจ SSR (ฮาร์ดแวร์) vs `BLEeder:STATe OFF` (SCPI) — ดูหัวข้อด้านล่าง | ตัดสินใจ + ทดสอบ | ถ้าไม่ตัดสินใจ วงจรจะซับซ้อนเกินจำเป็นต่อไปเรื่อยๆ | ❌ ยังไม่ตัดสินใจ — ต้องมีฮาร์ดแวร์ทดสอบ |
| 4 | ตั้ง `:CRANge`/`:VRANge` อัตโนมัติตอน connect (PEL-3111) | เขียนโค้ดเพิ่มใน `hardware_driver.py` ได้จากเครื่อง dev แต่ **การเลือก margin ปลอดภัยต้องคุยกับ user ก่อน** (ตั้งแคบไปเสี่ยง out-of-range กลางเทสจริง) | เสียความแม่นยำวัดฟรีๆ (10x บน V, 10x บน I) จนกว่าจะทำ | ✅ **เขียนแล้ว** — `recommend_pel3111_ranges()` แบบ conservative (margin 75%, ต้องมี headroom เหลือ ≥25% ถึงจะตั้ง range แคบ) เรียกใน `_on_connect()` ทันทีหลัง `connect_instruments()`; แพ็ค 12V lead-acid (เช่น YTZ6V) จะ fallback ไป VRANge=HIGH เองเพราะใกล้ขอบ 15V เกินไป — ทดสอบใน `tests/test_pel3111_range_autoset.py` (8 เคส) |
| 5 | แก้ `NATIVE_BATT_SCPI` ใน `pel_batt_test.py` | เขียนโค้ด + หาวิธีดึง Ah/Wh สะสม (ยังไม่มีคำสั่งง่ายๆ) | **ไม่เร่งด่วน** — path นี้เป็น dead code อยู่แล้ว (fallback ไป PC-path เสมอ) ไม่กระทบผลทดสอบปัจจุบัน | ✅ **แก้ string ตามตารางแล้ว** แต่ `native_supported()` ยัง hardcode `False` ต่อไปโดยตั้งใจ — เพราะคู่มือจริงไม่มีคำสั่งดึง Ah/Wh สะสมเลย (มีแต่ `:BATT:RESult?` ที่คืนแค่ค่าขณะนั้น) เปิดใช้ตอนนี้จะเสี่ยง discharge แบตจริงแล้วดึงผลไม่ได้ — รอ feature ดึง datalog file ก่อนถึงจะเปิด native path ได้จริง ทดสอบใน `tests/test_pel_batt_native_scpi.py` (4 เคส) |

## จุดที่ต้องตัดสินใจ: ข้อ 2 กับ 3 แก้ปัญหาเดียวกัน

**ข้อ 2 (ย้าย sense wiring)** และ **การถอด SSR ออกแล้วใช้ `BLEeder OFF` แทน (ข้อ 3)** ทั้งคู่เกี่ยวกับปัญหาเดียวกัน
(bleed 0.6A ของ PSW ปนกับการวัด/ตรวจจับ rest) แต่เป็นคนละกลไก — **ยังไม่ได้เลือกว่าจะทำแบบไหน:**

- **ทำทั้งคู่ (กันเหนียว)**: ย้าย sense wiring ตามที่วางแผนไว้ + ยังคง SSR ไว้เหมือนเดิม (ไม่ต้องเปลี่ยนอะไรเพิ่ม เสี่ยงน้อยสุด)
- **แทนที่ SSR ด้วย `BLEeder OFF`**: ถ้าทดสอบแล้วว่าปิด bleeder ไม่กระทบการทำงานปกติของ PSU (ดูว่าลดแรงดันได้เร็วพอไหม)
  จะลดความซับซ้อนวงจร + ลดจุดสัมผัส/contact resistance ที่ SSR เพิ่มเข้ามา — แต่ต้องทดสอบให้แน่ใจก่อนถอด SSR จริง

**แนะนำ**: ทำข้อ 1 (เช็ค USB backend) และข้อ 2 (ยืนยัน/ทำ wiring) ก่อน เพราะมีผลกับความแม่นยำของทุกการทดสอบที่ทำต่อจากนี้
ส่วนข้อ 3 (SSR vs BLEeder) ค่อยตัดสินใจทีหลังเมื่อมีเวลาทดสอบเพิ่มเติมกับเครื่องจริง

## คำสั่งเช็ค USB backend (ข้อ 1)

```bash
python -c "import pyvisa; print(pyvisa.ResourceManager().visalib)"
```
ถ้าขึ้น NI-VISA (`.dll` ของ National Instruments) → มีความเสี่ยง BSOD ตาม AN ที่เจอ (ดู `rig_investigation_findings.md`)
ถ้าขึ้น `pyvisa-py` → เป็นคนละ implementation อาจไม่ได้รับผลกระทบ
