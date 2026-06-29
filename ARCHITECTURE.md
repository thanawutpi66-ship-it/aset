# ASET Battery Characterization System — Engineering Reference

เอกสารนี้สำหรับให้ทีมเข้าใจสถาปัตยกรรม, data flow, threading model, และ tech debt ของระบบตรงกัน

> ⚠️ **อัปเดต 2026-06-29 — โครงสร้างเปลี่ยนเป็นแพ็กเกจ `aset_batt/`**
> โค้ดทั้งหมดย้ายเข้า package แบบ layered: `aset_batt/{app,core,hardware,acquisition,ui,services,storage,web}`
> ดังนั้น **path แบบ flat ในเอกสารนี้ (เช่น `state_estimator.py`) ตอนนี้อยู่ใต้ `aset_batt/<layer>/`**
> สิ่งที่เพิ่มใหม่และยังไม่ได้ลงลึกในเอกสารนี้: **acquisition engine** (`aset_batt/acquisition/`:
> `AcquisitionWorker` QThread + `HardwareBackend` + วิธีวิเคราะห์เดียว `analyze_series/analyze_csv`),
> **1-RC ECM identifier** (`core/parameter_id.py`), **two-resistance grading**, **HPPC ปรับเวลาได้**
> 👉 **module map + สถานะล่าสุดที่ถูกต้องที่สุดดูที่ [context_summary.md](context_summary.md) §3**
> (GUI เหลือ PySide6 ISA-101 ตัวเดียวที่ `aset_batt/ui/isa101_views.py` — ลบ Tkinter + retire PyQt6 แล้ว)

---

## 1. ภาพรวมระบบ

แอปเดสก์ท็อป (PySide6) สำหรับ **characterize แบตเตอรี่** ตามมาตรฐาน **IEC 61960** —
ควบคุม Power Supply + Electronic Load ผ่าน VISA และอ่านอุณหภูมิจาก ESP32 ผ่าน serial,
ประมาณ SoC/SoH/Rin แบบ real-time, บันทึก CSV, และเปิด dashboard ดูผลผ่านเว็บได้

- **Language/runtime:** Python 3.11
- **UI:** **PySide6 + PyQtGraph** (`main.py` → `aset_batt/ui/isa101_views.py` — ISA-101 HMI desktop;
  cross-thread update ผ่าน Qt signal/slot + `QtRootShim.after()`)
- **Instrument I/O:** PyVISA (PSU/Load), pyserial (ESP32 temperature)
- **Compute:** numpy (core), scipy/pandas/scikit-learn/joblib (optional — analysis/ML)
- **Plotting/Web:** matplotlib (Agg), `http.server` (stdlib)
- **Remote:** Cloud dashboard (Azure) — use cloud_push to send snapshots to the cloud

---

## 2. สถาปัตยกรรมแบบชั้น (layers)

```
┌─────────────────────────────────────────────────────────────┐
│  Presentation         aset_batt/ui/isa101_views.py (PySide6 ISA-101)        │
│  Acquisition engine   aset_batt/acquisition/ (worker · backends · analysis)  │
├─────────────────────────────────────────────────────────────┤
│  Orchestration        auto_controller.AutoController          │
│                       (monitor loop / profile / IEC tests)    │
├─────────────────────────────────────────────────────────────┤
│  Domain / Compute     state_estimator · battery_model         │
│                       iec61960_standard · analysis_module     │
├─────────────────────────────────────────────────────────────┤
│  Hardware Abstraction hardware_driver  ⟷  mock_hardware       │
│                       (เปลี่ยนตาม simulation_mode)            │
├─────────────────────────────────────────────────────────────┤
│  Cross-cutting        config · event_system · service_locator │
│                       logging_config · exceptions · data_utils│
└─────────────────────────────────────────────────────────────┘
```

**Dependency injection:** [service_locator.py](service_locator.py) — `ServiceLocator` เป็น singleton เก็บ map `Type → instance`
ส่วน `ServiceProvider` register พร้อม track ไว้ cleanup. คอมโพเนนต์หลักถูก register ใน
`ApplicationBootstrapper._create_core_components()` แล้วดึงด้วย `ServiceLocator.get(Type)`

---

## 3. Lifecycle (จาก main จนถึง shutdown)

[main.py](main.py) → `app_bootstrapper.create_application()`:

1. `ApplicationBootstrapper.initialize()` — logging → config (+validate) → signal handlers → service locator
2. `create_ui(root)`:
   - สร้าง `UIEventHandler(root)` + start event bus
   - `_create_core_components()` — เลือก `MockHardwareController` ถ้า `simulation_mode` ไม่งั้น `HardwareController`; สร้าง `DataHandler`, `BatteryModel`, `StateEstimator`, `AutoController`; register ทั้งหมด
   - สร้าง `BatteryAppUI`, ผูก callback ของ UI เข้ากับ event handler
   - simulation: auto-connect mock instruments
   - ถ้า `enable_web_server` → start `ASETWebServer` (daemon thread)
   - ผูก `WM_DELETE_WINDOW` → confirm → cleanup
3. `root.mainloop()` (main thread block ที่นี่)
4. ปิดหน้าต่าง → `cleanup()`: stop web server → stop event bus → `AutoController.shutdown()` (hw off + stop logging) → `ServiceLocator.clear()`

---

## 4. Threading & Concurrency Model ⚠️ สำคัญ

ระบบมี **หลาย thread** ทำงานพร้อมกัน:

| Thread | สร้างที่ไหน | หน้าที่ |
|---|---|---|
| **Main** | `app.exec()` | Qt UI เท่านั้น |
| **EventBus** | `EventBus.start()` (daemon) | ดึง event จาก `queue.Queue` → เรียก listener |
| **Monitor loop** | `AutoController.start_monitor()` (daemon) | อ่าน HW @10Hz → estimator → log CSV → update UI |
| **Profile loop** | `start_profile()` (daemon) | ไล่ current steps ตาม profile |
| **IEC test** | `start_iec61960_test()` (daemon) | รัน capacity/DCIR/cycle-life |
| **Web requests** | `ThreadingHTTPServer` (thread/req) | serve dashboard |
| **ESP monitor** | `connect_esp32()` (daemon) | parse อุณหภูมิจาก serial |

**กติกาความปลอดภัยของ thread:**
- **UI ต้องอัปเดตผ่าน Qt signal/slot (queued) หรือ `QtRootShim.after(0, ...)` เท่านั้น** — worker thread ห้ามแตะ Qt widget ตรงๆ
- **VISA access serialize ด้วย `HardwareController.inst_lock`** (ทุก read/write ของ PSU/Load อยู่ใน `with self.inst_lock`)
- Web `_plot_lock` กัน matplotlib render ซ้อน + cache PNG 3 วินาที

---

## 5. Data Flow

### 5.1 Live monitoring (เส้นทางหลัก)
```
HW.read_vi() → (v, psu_i, load_i)
  └ i_net = load_i - psu_i        # convention: discharge = บวก
     ├→ check_safety_limits(v, i_net, temp)        # ตัดไฟถ้าเกิน limit
     ├→ StateEstimator.update(v, i_net, dt=0.1, temp)  → {soc, soh, rin}
     ├→ root.after → ui.update_display(...)        # อัปเดตจอ
     └→ DataHandler.log_row(elapsed, v, i_net, soc, rin*1000, temp)  → CSV
```

### 5.2 CSV → Dashboard (เส้นทาง read-only)
```
battery_data.csv → storage.data_utils._tail_csv_rows()
   ├→ /api/summary  → _compute_summary()  (latest, min/max/avg, capacity, energy)
   ├→ /plot/main.png → _render_main_plot() (6 แผง, cache 3s)
   └→ /api/last, /plot/soc.png
```

### 5.3 Offline analysis (AI grading) — เรียกผ่าน `/api/analysis` + เมนู "Analyze Last CSV"
```
CSV → BatteryAnalyzer.analyze()
   ├→ RandlesModelExtractor: detect_pulses → fit_pulse (1RC)  → RCParameters
   ├→ capacity/energy/temp features                          → AnalysisFeatures
   └→ BatteryGrader.predict()  (ML ถ้ามี .joblib, ไม่งั้น heuristic) → AnalysisResult
```

---

## 6. Data Model

### 6.1 CSV schema ([data_utils.py](data_utils.py)) — utf-8-sig
`Timestamp, Elapsed_s, Voltage_V, Current_A, SoC_pct, Resistance_mOhm, Temperature_C`
- `Elapsed_s` = วินาทีนับจากเริ่ม monitor (ไม่ใช่ unix time)
- เปิดไฟล์แบบ append; เขียน header เฉพาะตอนไฟล์ว่าง

### 6.2 Config ([config.py](config.py)) — dataclass + JSON ([config.json](config.json))
- `BatteryConfig` (type, nominal/max/min_voltage [**ต่อเซลล์**], rated_capacity [**ทั้งแพ็ค**], max_current, mass_grams, `cells_series`/`cells_parallel`). มี properties `pack_nominal/max/min_voltage` = per-cell × series. **ปัจจุบัน: 8S1P LiFePO4 → pack ~25.6V**
- `safety_limits` เป็น **ระดับแพ็ค** (30.8V / 19.2V / 20A) — ต้องแคบกว่าจุดอันตรายเสมอ
- `SystemConfig` (simulation_mode, enable_web_server, web_server_port, csv_filepath, **safety_limits**)
- `HardwareConfig` (psu_port, load_port, esp_port, visa_timeout, baudrate)
- โหลด/บันทึกผ่าน `ConfigManager`; มี global `config_manager`

### 6.3 Analysis dataclasses ([analysis_module.py](analysis_module.py))
`RCParameters` (r0/rp/cp/tau/rmse), `AnalysisFeatures` (10 features, `to_vector()` ตาม `FEATURE_NAMES`), `AnalysisResult` (grade/confidence/method/...)

---

## 7. Hardware Abstraction Layer (HAL)

`HardwareController` ([hardware_driver.py](hardware_driver.py)) และ `MockHardwareController` ([mock_hardware.py](mock_hardware.py))
**ต้องมี interface เหมือนกันทุก method/attribute** (สลับกันตาม `simulation_mode`)

Interface ที่ orchestrator พึ่งพา:
- Enumeration: `get_visa_ports()`, `get_com_ports()`
- Connect: `connect_instruments(psu, load)`, `connect_esp32(port, cb)`
- Control: `set_psu(state,v)`, `set_load(state,i)`, `set_load_raw(target)`, `load_on/off()`, `psu_off()`, `set_charge(state,i)`, `set_psu_cccv(v,i)`
- Measure: `read_vi() → (v, psu_i, load_i)`, `read_measurements() → (v, i_net)`, `transient_dcir_measure()`
- State: `is_connected`, `is_esp_connected`, `current_temp`, `inst_lock`, `psu_inst`, `load_inst`
- Shutdown: `shutdown_all()`, `disconnect_instruments()`

> `set_psu_cccv(v,i)` ตั้ง :VOLT+:CURR limit พร้อมกัน → PSU ทำ CC↔CV ในฮาร์ดแวร์เอง
> (ใช้โดย `ChargeController`). `psu_off()` ปิด :OUTP (ก่อนหน้านี้ `_emergency_shutdown`
> เรียก `psu_off` ที่ยังไม่มี → ถูก except กลืน = PSU ไม่ถูกตัด; เพิ่มแล้ว)

PSU/Load สื่อสารด้วย SCPI (`:VOLT`, `:CURR`, `:OUTP ON`, `:INP ON`, `MEAS:VOLT?` …) baud 9600.
ESP32: thread อ่าน serial หา pattern `"Object = … *C"` → `current_temp`

---

## 8. อัลกอริทึมหลัก

### 8.1 State Estimation ([state_estimator.py](aset_batt/core/state_estimator.py))
- **Coulomb counting:** `ah_accumulated += I·dt/3600`
  - *Charging* — `dah *= _coulomb_eta(soc, I)`: state-dependent η สำหรับ Lead-Acid (Faraday gassing loss):
    SoC < 75% → η=0.97, 75–90% → η=0.92, > 90% → η=0.75. Li-ion/LFP: η=0.99 คงที่
  - *Discharging* — `dah = _peukert_dah(I, dah)`: Peukert correction `(I/I_rated)^(k−1)`;
    Lead-Acid k=1.30 rated at C10 → ที่ 1C (10×I_rated) SoC ลดเร็วขึ้น ×2
- `soc_cc = soc_initial − ah_accumulated / rated_capacity × 100`
- **OCV correction:** เมื่อกระแส ≈ standby (< 0.15A จาก bleed) + พักนานพอ (`_min_rest_s`: LFP 120s / Lead-Acid 60s / Li-ion 30s) + slope ≥ threshold → blend 80-90% OCV + 10-20% CC
- **Endpoint anchors:** 100% เมื่อ V ≥ 98.6% CV + I_tail ≤ max(0.25A, C×tail_c_rate×1.5); 0% เมื่อ V ≤ empty OCV×1.01
- **Exponential smoothing:** `soc_filtered = (1−α)·prev + α·soc_cc`, α=0.05
- Init: `calibrate_from_ocv()` (instant) หรือ `calibrate_from_ocv_stable()` (ΔV/Δt criterion — PREPARE phase)

### 8.2 Battery Model ([battery_model.py](aset_batt/core/battery_model.py))
- OCV–SoC lookup ต่อ chemistry (**per-cell**), มีตารางแยกต่อ `temp_range = [-10, 0, 10, 25, 40, 60]°C`
- **Lead-Acid Nernst temperature compensation:** `_generate_ocv_tables()` shift ทุก OCV value ตาม
  `temp_coeff_mv_per_degc` จาก `ChemistryProfile` — Lead-Acid +0.40 mV/°C/cell จาก 25°C reference;
  Li-ion/LFP: tc=0 → ตารางเดิมทุกอุณหภูมิ (ผล entropic เล็กน้อย vs. Rin ที่เปลี่ยนมาก)
- **Pack scaling:** `series_cells` / `parallel_cells` → แรงดัน/ความต้านทานคูณ series,
  ความจุคูณ parallel; getters คืนค่า **ระดับแพ็ค** (ตรงกับที่วัดจริง)
- `estimate_rin()` — Rin: temp (**R สูงเมื่อเย็น, Arrhenius**), SoC (U-shape), aging;
  blend Thevenin (V/I) + base + measured DCIR; fallback ไปยัง `rin_base` เมื่อ |I| < 0.5A (CV phase)
- IEC helpers: capacity, energy density, DCIR, cycle life

### 8.3 IEC 61960 Tests ([auto_controller.py](auto_controller.py) + [iec61960_standard.py](iec61960_standard.py))
capacity / energy-density / internal-resistance / cycle-life / safety — แต่ละตัวรันใน thread แยก,
เก็บ array ของตัวเอง แล้วส่งให้ `iec61960_standard` คำนวณ + `generate_test_report`

### 8.4 Randles 1RC + Grading ([analysis_module.py](analysis_module.py))
- ฟิต `(V_pre − V)/I = R0 + Rp(1 − e^(−t/τ))` ด้วย scipy `curve_fit` (มี numpy fallback)
- Grader: ML (`RandomForest` .joblib) ถ้ามี, ไม่งั้น heuristic จาก SoH + R0 ratio

---

## 9. Remote Access (dashboard)

- `ASETWebServer` (ThreadingHTTPServer, daemon) อ่าน CSV → serve `/`, `/api/last`, `/api/summary`, `/api/analysis`, `/plot/main.png`, `/plot/soc.png`, `/api/health`
- เปิด public ผ่าน **Tailscale Funnel** → `https://thana.taildd719e.ts.net` (forward → localhost:8000)
- ⚠️ ต้องรันแอป **ตรงบน host** (`python main.py`) ไม่ใช่ผ่าน preview manager; ถ้า Funnel ขึ้น 502 ให้รีสตาร์ท instance

**Cloud dashboard (24 ชม.)** — [cloud_dashboard/](cloud_dashboard/) เป็น service แยก (stdlib ล้วน,
Chart.js) สำหรับดูผลแม้เครื่องแล็บปิด: เครื่องแล็บรัน [cloud_push.py](cloud_push.py) →
`POST /api/ingest` (auth token) ขึ้น Heroku/DO → เพื่อนเปิดดู. snapshot เก็บ in-memory
(ประวัติถาวรต้องต่อ DB — follow-up). ดู [cloud_dashboard/README.md](cloud_dashboard/README.md)

---

## 10. Tech Debt / ความเสี่ยงที่ต้องรู้ร่วมกัน

**✅ แก้แล้ว (commits ล่าสุด 2026-06-29):**
- Sign convention "discharge = บวก" รวมทุกจุด + regression test; verified กับ hardware จริง (FB FTZ6V)
- IEC capacity/energy/DCIR wiring + `iec61960_standard` bugs; wire `analysis_module` เข้าระบบจริง
- **4 accuracy bugs (state estimation):**
  - OCV settle time เป็น chemistry-aware (Lead-Acid 60s min) แทน 3s hardcode ใน PREPARE
  - Pre-charge OCV sync ที่ overwrote soc_initial ด้วย polarized voltage — ลบออก
  - Endpoint anchor threshold 1.2× → max(0.25A, 1.5×) ป้องกัน 0.18A ผ่านเกณฑ์ไม่ได้
  - REST phase current sign bug + PSU output state tracking
- **Lead-Acid physics accuracy (session 2026-06-29):**
  - Nernst OCV temperature compensation: +0.40 mV/°C/cell ใน `_generate_ocv_tables()`
  - State-dependent coulomb efficiency: 0.97/0.92/0.75 ตาม SoC gassing stage
  - Peukert correction real-time: k=1.30, C10, scale cap 5×
  - ΔV/Δt criterion ใน PREPARE: `calibrate_from_ocv_stable(cancel_check=...)` 300s+convergence

**ยังเหลือ:**

| ระดับ | ประเด็น | ตำแหน่ง |
|---|---|---|
| 🟠 | `BatteryGrader` heuristic thresholds ยังไม่ calibrate ด้วยข้อมูลจริง + ยังไม่มีโมเดล ML ที่เทรนแล้ว | analysis_module / train_grader |
| 🟠 | YTZ7V profile: ใส่ 7Ah/CCA130 แต่ datasheet = 6.3Ah(20HR)/CCA105A | battery_profiles.json |
| 🟡 | AI analysis ยังเป็น on-demand — ยังไม่ auto-run หลัง full sequence จบ | auto_controller |
| 🟡 | IEC test DCIR/cycle-life/safety ยังไม่ log ลง CSV | `_run_internal_resistance_test` |
| 🟡 | `dt=0.1` hardcode ใน monitor loop → coulomb error เมื่อ VISA ช้า | `_monitor_loop` |
| 🟡 | Peukert correction ไม่เหมาะสำหรับ HPPC pulse สั้น (< 1 min) — Peukert derivation ต้องการ discharge คงที่ | state_estimator |
| 🟡 | dashboard ไม่มี auth (เปิด public ผ่าน Funnel) | web_server |

---

## 11. การรัน & ขยายระบบ

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt        # core+GUI (PySide6/pyqtgraph/reportlab); scipy/pandas/sklearn/joblib = analysis เต็ม
python main.py                         # รันแอป (PySide6 desktop GUI)
python generate_sample_data.py         # สร้างข้อมูลเดโมลง battery_data.csv
python train_grader.py labels.csv -o grader_model.joblib   # เทรนโมเดล grader
pytest                                 # รัน test
```

**แนวทางขยาย:**
- เพิ่ม hardware ใหม่ → implement interface ใน §7 ทั้ง real + mock
- เพิ่ม event → เพิ่มใน `EventType` + listener ใน `UIEventHandler`
- เพิ่ม dependency → register ผ่าน `ServiceProvider` ใน bootstrapper
- เปิด/ปิด web → `enable_web_server` ใน config.json

---

## 12. Conventions

- Docstring/คอมเมนต์ผสมไทย-อังกฤษ (ตามโค้ดเดิม), identifier เป็นอังกฤษ
- ทุกโมดูลใช้ `logger = logging.getLogger(__name__)`
- ข้อผิดพลาดใช้ exception hierarchy ใน [exceptions.py](exceptions.py) (`ASETError` เป็นฐาน)
- worker thread เป็น `daemon=True` ทั้งหมด; UI ผ่าน `root.after` เสมอ
