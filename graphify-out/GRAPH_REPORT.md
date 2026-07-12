# Graph Report - .  (2026-07-13)

## Corpus Check
- Large corpus: 204 files · ~510,771 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder.

## Summary
- 2919 nodes · 6767 edges · 170 communities (138 shown, 32 thin omitted)
- Extraction: 84% EXTRACTED · 16% INFERRED · 0% AMBIGUOUS · INFERRED: 1079 edges (avg confidence: 0.53)
- Token cost: 532,420 input · 0 output

## Community Hubs (Navigation)
- Test Analysis & Grading
- App Configuration Management
- Acquisition Worker & Backends
- Automated Test Controller
- Application Bootstrap
- Session Data Storage
- Parameter Identification (R0/ECM)
- SoC/SoH State Estimator
- Mock Hardware Simulation
- Battery Characterization Pipeline
- CSV Telemetry Analysis
- Charge Loop Control
- Hardware Driver (SCPI)
- Core Component Wiring
- Cloud Dashboard Server
- Cloud Dashboard Frontend JS
- Crash Recovery State
- IEC 61960 Standard Compliance
- Battery OCV Model & EKF
- Signal Analytics Utilities
- Characterize Tab UI
- Unified Series Analysis
- Settings Dialog (diff artifact)
- Estimator Scratch File
- Realtime Accuracy Test Fixtures
- ECM-based Health Grading
- PEL E-Load Discharge Driver
- Sequence Base Mixin
- Charge Voltage Gate Tests
- Event Bus System
- PSU Measurement Tests
- Analysis Helper Functions
- Mock Hardware & Direct Mode
- Architecture Doc: Grading Classes
- DCIR Identification
- Trend Graph Widgets
- Project Pivot Decisions
- Logging & Version Info
- Rin Temperature/SoC Model
- Battery Pack Config
- Session Path Naming
- Hardware Connect Flow Tests
- Config Field Validation Tests
- Acquisition Profile Builder
- Harness Resistance Correction
- Hardware Control UI
- Hardware Backend Interface
- PSU Self-Calibration
- Cloud Push Metadata
- Theme Retheme Registry
- PSU Command Result Reporting Tests
- Event Bus Core
- UI Slots & Signal Wiring
- Safety Limits Settings Tests
- Capacity & SoH Calculation
- Logging Initialization
- Self-Update Mechanism
- Main UI Layout Builder
- Chemistry Profile Validation Tests
- Rin-Calibrated Flag Tests
- Universal R0 Step Detector Tests
- Safety Shutdown Path Tests
- DCR Timepoint Reporting (G5)
- DCIR At Fixed Timepoints
- Chemistry Auto-Detection
- Instrument Safety Protection Config
- Event Type Definitions
- Alarm/Interlock UI Tests
- CSV Logging Fidelity Tests
- OCV Out-of-Range Detection Tests
- DCIR From V-I Slope
- Chemistry Profile Registry
- IEC 61960 Test Profiles
- Cloud Push Client
- Cloud Push Payload Builder
- EN 50342-1 Capacity Conditions
- CCA Proxy Test
- Cloud Push Background Loop
- PDF Report Generation
- Bench Diagnostic Script
- Current Card Color UI Tests
- Endpoint Anchor Sustain Gate Tests
- EKF Accuracy Fix Tests
- Stale Graph Generation Tests
- Session Integrity Verification
- HPPC Live ECM Feed Tests
- Sequence Estimator Feed Tests
- App Launcher Entry Point
- PEL-3111 Range Auto-Select
- Pre-Test Confirmation Dialog
- Trend Crosshair Widget
- Azure Deployment Guide
- Hardware Driver Coverage Tests
- PSU Trip Clear UI Tests
- Retheme & Crosshair Tests
- SSR Manual Control UI Tests
- Rig Status: Hardware Protection
- PEL/PSW Hardware Reference Doc
- Graph Idle View State
- Cloud Dashboard Limitations
- Charge Efficiency Calibration Script
- Estimator Replay/Backtest Script
- ML Grader Training Script
- Anchor Settle & SoH Reset Tests
- Uncalibrated R0 Runaway Tests
- Rig Investigation Findings Doc
- CSV Summary Stats
- Sequence Abort/Cancel Handling
- Theme Contrast/Color Utils
- HPPC 5Hz Pacing Tests
- Alarm Beep Tests
- R0 Plausibility Band Tests
- Measured Params Validation Tests
- Chemistry Registry Tests
- Rin-SoC Shape Tests
- Shutdown Cuts Outputs Tests
- Zero-Anchor Calibration Gate Tests
- Future Work: SoC Estimation
- Aging Factor Wiring Tests
- Force-HPPC Detection Tests
- Pack Scaling Tests
- Rig Status: Auto Protection Config
- PEL Native BATT SCPI
- CI/CD Split Workflows
- Dashboard HTML Page
- Synthetic Training Data Script
- OCV Interpolation Tests
- Stale-Check Ordering Tests
- Grade Decision Logging Tests
- IEC 61960 Enums
- University/Faculty Logos
- IEC 61960 DCIR Compliance
- Future Work: ML Grading
- Future Work: Rin Temperature Model
- HPPC Fit-and-Feed Regression Tests
- Write-Off Verified Tests
- Current Sign Convention Tests
- RUN Zone Crash Traceback
- Future Work: OCV Hysteresis
- Faculty Logo Asset
- Over-Temperature Protection
- ASET Brand Logo
- Measured Params Getter
- Measured Params Saver
- ASET Project & Logo
- UI Package Init
- Charge Status Text
- Charge Time Estimate
- Hardware Retry Helper
- Workflow Type Switch
- CV Tail ETA Projection
- Step Time Estimates
- Interruptible Sequence Sleep
- Cycle Life EN 50342 Check
- HPPC EN 50342 Check
- Quick Scan EN 50342 Check
- Material Stylesheet Cache
- Widget Color Resolution
- Project Package Metadata
- Git Log Diff Artifact

## God Nodes (most connected - your core abstractions)
1. `BatteryModel` - 263 edges
2. `ConfigManager` - 213 edges
3. `StateEstimator` - 194 edges
4. `BatteryQtWindow` - 159 edges
5. `BatteryProfile` - 144 edges
6. `AutoController` - 139 edges
7. `MockHardwareController` - 126 edges
8. `DataHandler` - 94 edges
9. `HardwareController` - 85 edges
10. `TestConfig` - 76 edges

## Surprising Connections (you probably didn't know these)
- `Analytics (local redefinition, acquisition/analysis.py:406)` --semantically_similar_to--> `Analytics`  [AMBIGUOUS] [semantically similar]
  flake8_report.txt → aset_batt/acquisition/analytics.py
- `qt-material theme override bug: window-level setStyleSheet cascades over Material theme` --conceptually_related_to--> `BatteryQtWindow`  [EXTRACTED]
  CLAUDE.md → aset_batt/ui/isa101_views.py
- `mycrash.txt (runtime crash log)` --references--> `ApplicationBootstrapper`  [EXTRACTED]
  mycrash.txt → aset_batt/app/app_bootstrapper.py
- `BatteryModel.__init__() unexpected keyword 'product_name' error on apply-product` --conceptually_related_to--> `BatteryModel`  [EXTRACTED]
  mycrash.txt → aset_batt/core/battery_model.py
- `mycrash.txt (runtime crash log)` --references--> `BatteryModel`  [EXTRACTED]
  mycrash.txt → aset_batt/core/battery_model.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **BatteryQtWindow composed from PySide6 mixins (Zones/Sequences/Characterize)** — aset_batt_ui_isa101_views_batteryqtwindow, aset_batt_ui_zones_zonesmixin, aset_batt_ui_sequences_sequencesmixin, aset_batt_ui_characterize_characterizemixin [EXTRACTED 1.00]
- **Repo's split CI/CD pipeline: GUI tests / cloud_dashboard tests / Azure deploy** — github_workflows_gui_tests, github_workflows_cloud_dashboard_tests, github_workflows_main_aset_batt_dashboard [EXTRACTED 1.00]
- **Lead-Acid state-estimation accuracy fix set (2026-06-29)** — concept_nernst_ocv_temp_compensation, concept_state_dependent_coulomb_efficiency, concept_peukert_correction, concept_endpoint_anchor_threshold, concept_ocv_settle_time_fix [EXTRACTED 1.00]
- **Lab-rig-to-cloud data flow: CSV -> cloud_push.py -> server.py -> dashboard UI** — cloud_push_module, cloud_dashboard_server_module, cloud_dashboard_static_index_html, cloud_dashboard_readme_md [INFERRED 0.85]
- **Three-way rig documentation split: hardware reference (lookup), investigation findings (rationale), action items (status)** — docs_pel3111_psw_hardware_reference_md, docs_rig_investigation_findings_md, docs_rig_status_action_items_md [EXTRACTED 0.90]
- **2026-06-29 SoC/Rin model improvement batch: Faraday efficiency, Peukert, OCV dV/dt anchor, Nernst temp compensation** — state_dependent_coulomb_efficiency, peukert_correction, ocv_anchor_delta_v_delta_t, nernst_ocv_temperature_compensation [INFERRED 0.80]

## Communities (170 total, 32 thin omitted)

### Community 0 - "Test Analysis & Grading"
Cohesion: 0.04
Nodes (57): analyze_series_mp(), Same result as analyze_series(), but off the calling thread's GIL — see     ana, [fallback] Single total-resistance grading for non-HPPC modes (no RC fit)., [fallback] Single-point total resistance Rᵢ = |ΔV / ΔI| across the pulse, BatteryProfile, load_profiles(), OperationMode, Enum (+49 more)

### Community 1 - "App Configuration Management"
Cohesion: 0.04
Nodes (41): ConfigManager, Centralized configuration management, Validate configuration parameters, BatteryQtWindow, Drop samples older than _TREND_MAX_DURATION_S from the front of the         tre, QMainWindow, Industrial-grade audit follow-up R2/G2.  _log_alarm() used to unconditionally, Regression for a specific bug caught during implementation: the         suppres (+33 more)

### Community 2 - "Acquisition Worker & Backends"
Cohesion: 0.10
Nodes (50): Instrument backends behind the acquisition worker.  Called ONLY from the worke, Acquisition value objects — operation modes, battery test profile, test config,, Background acquisition worker (QThread).  The worker owns ALL instrument I/O a, ChemistryDetector, ChemistryResult, Analysis Module: Chemistry Detection สำหรับ ASET Battery Characterization Syste, แยกชนิดเคมีของแบตคลาส 12V จากลายเซ็นไฟฟ้า (rule-based heuristic)      ตัวแยกหล, Battery Profile Registry — ฐานข้อมูลโปรไฟล์แบตเตอรี่ (chemistry + charging strat (+42 more)

### Community 3 - "Automated Test Controller"
Cohesion: 0.04
Nodes (38): AutoController, Trigger safety shutdown, Advanced controller for battery testing operations, Emergency shutdown of all systems, เริ่มลูปอ่านค่าจาก Hardware          reuse_session=True: เรียกจากกลางเซสชันที่, Stop the hardware monitoring loop, ปิด CSV session ปัจจุบันอย่างชัดเจน (ให้ workflow ถัดไปเริ่ม session ใหม่, Calibrate SoC from OCV reading when battery is rested (+30 more)

### Community 4 - "Application Bootstrap"
Cohesion: 0.05
Nodes (33): ApplicationBootstrapper, Application bootstrapper for ASET Battery Characterization System, ตัด PSU/Load/SSR ทันทีแบบ best-effort — เรียกได้หลายครั้ง ปลอดภัยเสมอ         (, Initialize service locator with core services, Wiring ที่ใช้ร่วมกัน: event callbacks, analyzer, auto-connect mock         hard, สร้าง event handler + core components + wire Qt window เข้ากับ controller, Application bootstrapper with proper initialization and cleanup, Cleanup application resources (idempotent — ถูก register กับ atexit ด้วย (+25 more)

### Community 5 - "Session Data Storage"
Cohesion: 0.04
Nodes (29): DataHandler, เริ่มบันทึก CSV — คืน (True, "") หรือ (False, error_message), บันทึก 1 แถวข้อมูล          Args:             elapsed_s      : วินาทีที่ผ่านไ, โหลด current profile จาก CSV          รูปแบบที่รองรับ:           - 2 คอลัมน์:, isolate_sessions_dir(), theme.style()/on_retheme() register widget stylesheets/callbacks in     module-, Automatically redirects DataHandler's session directory to a temporary path for, _reset_theme_registry() (+21 more)

### Community 6 - "Parameter Identification (R0/ECM)"
Cohesion: 0.06
Nodes (35): BatteryParameterIdentifier, FitResult, FitResult2RC, ndarray, 1-RC Thevenin ECM parameter identification for battery cells/packs (LiFePO4)., Summary dictionary (the public, serialisable result)., Identify 1-RC Thevenin ECM parameters from current-pulse time-series data., Edge-padded moving-average low-pass filter (removes sensor jitter). (+27 more)

### Community 7 - "SoC/SoH State Estimator"
Cohesion: 0.05
Nodes (25): Adaptive & robust SoC estimator, Minimum seconds the current must be near standby before OCV correction fires., Faraday coulombic efficiency for a charging step.          Lead-acid gassing l, Apply Peukert correction to a discharge Ah increment.          Peukert's law:, SoH-adjusted usable capacity (Ah).          Coulomb counting MUST divide accum, Externally set SoH (e.g. from analysis.py full-discharge capacity)., Clear everything this instance has learned about the PREVIOUS physical, Initial 1-RC parameters for the EKF from the pack model.         R0 from base_r (+17 more)

### Community 8 - "Mock Hardware Simulation"
Cohesion: 0.05
Nodes (20): MockHardwareController, _MockInst, จำลองพฤติกรรมแพ็ค: ชาร์จ → แรงดันไต่ขึ้นจน CV แล้วกระแส taper;         ดิสชาร์จ, จำลอง CC-CV charge: ตั้ง target + กระแส bulk ให้ read_vi ขับ state machine ได้, จำลอง VISA instrument object (ใช้ใน profile loop ที่เรียก load_inst.write), Mock parity with HardwareController.temp_is_stale(). Refreshed on every, TestTempStaleness, Confirms the actual call sites invoke write_session_metadata(), not just     th (+12 more)

### Community 9 - "Battery Characterization Pipeline"
Cohesion: 0.06
Nodes (31): build_ecm_table(), build_ocv_table(), compute_coulomb_eta(), fit_peukert_k(), Physics-based parameter identification from battery characterization experiments, Build a {soc_int: ocv_per_cell} table from GITT rest measurements.      Args:, Fit Peukert exponent k from multi-rate discharge data.      Peukert: t · I^k =, Compute coulomb efficiency per SoC band.      Args:         ah_in_by_band:  d (+23 more)

### Community 10 - "CSV Telemetry Analysis"
Cohesion: 0.06
Nodes (31): analyze_csv(), Read a canonical (or lowercase) telemetry CSV → arrays + mode strings., Parse a telemetry CSV and run the unified analysis. HPPC is inferred from     t, _read_csv(), Tests for the dict-returning analyze_csv() in aset_batt.acquisition.analysis., For a full constant-current discharge these fields must be finite numbers., CSV with a Mode column (non-HPPC) should still return a valid grade., Plain discharge CSV without HPPC pulses should not trigger ECM identification. (+23 more)

### Community 11 - "Charge Loop Control"
Cohesion: 0.06
Nodes (23): Wait for surface charge to relax then sync SoC from OCV.          Called autom, ChargeController, ChargeDecision, ChargeParams, decide(), Charge Controller — สเตทแมชชีนการชาร์จตามชนิดเคมี  - Lead-acid (VRLA):  3-stag, ขับ state machine การชาร์จกับฮาร์ดแวร์จริง (เรียกจาก thread แยกใน AutoController, Setpoint ระดับแพ็ค (คำนวณจาก per-cell profile × series + C-rate × capacity) (+15 more)

### Community 12 - "Hardware Driver (SCPI)"
Cohesion: 0.05
Nodes (21): HardwareController, Manual PSU control (CV with a CC current limit).          current_val is the C, Returns True if the SCPI write(s) actually succeeded, False otherwise —, Pop every pending entry out of the instrument's SCPI error queue.          The, OUTPut:PROTection:TRIPped? — True if OVP/OCP/OTP has tripped on the PSU., OUTPut:PROTection:CLEar — clears an OVP/OCP/OTP trip (not AC-fail, which, Undo harden_instrument_config()'s front-panel lock on disconnect, so the, Audible alert on the PSU (SYSTem:BEEPer[:IMMediate] {<NR1>}, 0-3600s — (+13 more)

### Community 13 - "Core Component Wiring"
Cohesion: 0.05
Nodes (24): Create and register core application components, BatteryModel, สร้าง OCV lookup tables สำหรับอุณหภูมิต่างๆ จาก chemistry profile          Bas, Parameters สำหรับ internal resistance model (จาก chemistry profile)          r, เตรียมข้อมูลสำหรับ interpolation ที่เร็วขึ้น, Wire a capacity-based SoH (aset_batt.core.state_estimator's live ``soh`` —, Advanced battery electrical model ด้วย temperature compensation, generate() (+16 more)

### Community 14 - "Cloud Dashboard Server"
Cohesion: 0.07
Nodes (23): _json_sanitize(), _load_sessions(), _load_snapshot(), main(), _make_handler(), ASET Cloud Dashboard — บริการแสดงผลเทสต์แบตเตอรี่ 24 ชม. (stdlib ล้วน)  แยกจาก, Recursively replace float NaN/Infinity with None.     json.dumps emits the lite, Structural check on an /api/ingest payload before it's stored and later     re- (+15 more)

### Community 15 - "Cloud Dashboard Frontend JS"
Cohesion: 0.10
Nodes (48): $(), addWrap(), alarmLog, applyThemeIcon(), backToLive(), buildIcaChart(), buildMainCharts(), clamp() (+40 more)

### Community 16 - "Crash Recovery State"
Cohesion: 0.05
Nodes (18): Any, Persist current execution state to disk for crash recovery, callback จาก ChargeController — อัปเดต UI ผ่าน root.after (thread-safe), push_alarm(), Queue an event from the GUI's alarm log to ride along on the next push(es)., Toggle bright/dim colours on every unACKed alarm row at 500 ms., Operator ACK: stop flashing, mark rows as ACKed (solid colour)., _gen: explicit generation stamp for callers that schedule this call         for (+10 more)

### Community 17 - "IEC 61960 Standard Compliance"
Cohesion: 0.06
Nodes (23): IEC61960Standard, ส่งรายชื่อ test ที่มีให้เลือก, คำนวณ capacity ตาม IEC 61960         Capacity = ∫ I dt (Ah), คำนวณ energy density ตาม IEC 61960         Energy Density = Energy / Mass (Wh/k, DC internal resistance ตาม IEC 61960 Clause 6.4 — **two-pulse method** (ถูกต้องต, ประเมิน cycle life ตาม IEC 61960         End of life = 80% of initial capacity, IEC 61960 Standard Implementation สำหรับ LiPO Battery Testing, CycleLifeMixin (+15 more)

### Community 18 - "Battery OCV Model & EKF"
Cohesion: 0.07
Nodes (20): is_plausible_r0(), Battery Model: Advanced OCV lookup table และ internal resistance estimation สำห, True if ``r0`` sits within [0.2x, 6x] of ``base_rin`` AND under     ``abs_ceili, 2-state Extended Kalman Filter for SoC estimation (1-RC Thévenin model).  Stat, Re-anchor SoC. ``soc_var`` is the SoC covariance to trust the anchor with:, Update ECM parameters from a fresh HPPC fit., Process step.          soc_delta_pct: SoC decrement (%) already computed by th, r_override: use this measurement variance for THIS call only, instead of (+12 more)

### Community 19 - "Signal Analytics Utilities"
Cohesion: 0.09
Nodes (18): Analytics (local redefinition, acquisition/analysis.py:406), Analytics, ndarray, Post-test analytics: HPPC internal resistance, Incremental Capacity Analysis (I, DTV: dT/dV vs V (thermal fingerprint). Same axis-first de-jitter as ICA., Gaussian low-pass; scipy if available, else a numpy kernel convolution., Hampel identifier: replace spikes with the local median.          For each sam, dy/dx via a Savitzky-Golay filter — it fits a local polynomial, so it smooths (+10 more)

### Community 20 - "Characterize Tab UI"
Cohesion: 0.09
Nodes (11): Three independent parameter-ID experiments: Peukert k, Coulomb η, OCV–SoC., Post-test ICA dQ/dV curve (populated by the worker).          DTV (dT/dV) was, Shared QTabWidget/QTabBar look for every tab group in the app         (left-pan, Top-border accent per metric card — matches that metric's curve         color i, วงจรสมมูลแบตเตอรี่ (Thévenin 1-RC) — ใช้ชื่อ R0/R1/C1 ให้ตรงกับตารางผล, Wipes the alarm/event log — irreversible (no undo, no export-first         prom, ปรับให้เฉพาะหน้าที่กำลังแสดงดันความสูงของ stack — หน้าที่ซ่อนตั้งเป็น         I, Direct hardware control — PSU voltage/current and e-load current. (+3 more)

### Community 21 - "Unified Series Analysis"
Cohesion: 0.11
Nodes (20): analyze_series(), _ocv_ceiling(), The chemistry OCV curve's own 100% point (pack-level) at ``temp_c`` — the     h, Run the unified analysis on raw series → the standard results dict., _profile(), Regression test: CCA proxy must use the best available resistance measurement., Rest -> pulse, with the first post-edge sample delayed by `edge_gap_s`     (> a, _synthetic_hppc_with_stale_first_sample() (+12 more)

### Community 22 - "Settings Dialog (diff artifact)"
Cohesion: 0.08
Nodes (10): BatteryQtWindow._on_cloud_push_toggle, BatteryQtWindow._on_cloud_url_changed, BatteryQtWindow._on_theme_toggle, SettingsDialog, DialogsMixin, ผู้ใช้เปลี่ยน Test discharge C-rate — อัป amp label + WF step desc, Parse all sessions for SoH, show a matplotlib window with timeline., Parse cycle-life sessions and show capacity fade bar chart. (+2 more)

### Community 23 - "Estimator Scratch File"
Cohesion: 0.09
Nodes (15): Adaptive & robust SoC estimator, Minimum seconds the current must be near standby before OCV correction fires., Faraday coulombic efficiency for a charging step.          Lead-acid gassing l, Apply Peukert correction to a discharge Ah increment.          Peukert's law:, SoH-adjusted usable capacity (Ah).          Coulomb counting MUST divide accum, Externally set SoH (e.g. from analysis.py full-discharge capacity)., Clear everything this instance has learned about the PREVIOUS physical, Initial 1-RC parameters for the EKF from the pack model.         R0 from base_r (+7 more)

### Community 24 - "Realtime Accuracy Test Fixtures"
Cohesion: 0.10
Nodes (13): _FakeConfig, _FakeData, _FakeEstimator, _FakeEventHandler, _FakeSystemConfig, Regression tests for the real-time SoC/Rin accuracy audit (12-item analysis):, Simulates SCPI round-trip latency inside read_vi()., A slow read (e.g. 60 ms of simulated SCPI latency) must not ALSO get a (+5 more)

### Community 25 - "ECM-based Health Grading"
Cohesion: 0.12
Nodes (16): _load_metrics(), _quality_flags(), Lead-acid health features the rig CAN measure at 5 Hz (see project pivot §3,§8.5, Data-integrity checks — a sorting bench must NOT grade on bad measurements., Sort A/B/C/REJECT from SoH plus **independent** growth of R0 and R1., get_product(), ProductProfile, แบตรุ่นจริงที่ผู้ใช้เลือกจาก dropdown — map ไป chemistry + ขนาดแพ็ค      max/m (+8 more)

### Community 26 - "PEL E-Load Discharge Driver"
Cohesion: 0.14
Nodes (17): DischargeResult, integrate_capacity(), PelBattTest, pel_batt_test.py — drive the GW Instek PEL-3111 for a capacity / SoH discharge t, Terminal V + I straight from the load (it is the active instrument while, CC discharge until the terminal hits ``stop_voltage`` (or timeout / stop)., Trapezoidal coulomb + energy integration over a discharge.      ``i_a`` discha, SoH % = measured discharge capacity ÷ rated, clamped to [0, 120]. (+9 more)

### Community 27 - "Sequence Base Mixin"
Cohesion: 0.09
Nodes (11): BaseSequenceMixin, Mirror workflow step changes to the cloud dashboard meta., Update the always-visible banner to '▶ TEST · PHASE' for the active step., Update a step indicator.  state: idle/active/done/skip., Cross-thread safe wrapper for lbl_wf_status.setText., Sound + popup notification when a sequence finishes., Chemistry-correct capacity-test standard label. "IEC 61960" is a         SECOND, Warn once per sequence run if the ESP32 temperature reading has gone         st (+3 more)

### Community 28 - "Charge Voltage Gate Tests"
Cohesion: 0.12
Nodes (13): _est(), Regression tests: the EKF must not trust terminal voltage while it carries no S, F3: the surface-charge gate must (a) build the implied OCV with the EKF's     O, Terminal voltage at which the implied OCV (v + cur*(R0+R1)) sits exactly, hppc.py's PHASE 2 (post-charge rest, before HPPC pulses) must call         cali, 60 s of absorption-voltage charging must move SoC by only the         coulomb+e, Sanity check that the gate is what prevents the race: with the         charging, Discharging at a terminal voltage whose implied OCV (V + I*rin) is         abov (+5 more)

### Community 29 - "Event Bus System"
Cohesion: 0.13
Nodes (12): Event, Thread-safe event handler bridging AutoController (background threads)     to t, Handle display update events.          หมายเหตุ: เส้นทางหลักคือ AutoController, Handle status update events, Handle message display events — routed to the real UI's show_message         (w, Handle safety trigger events, Handle profile completion events, Handle analysis (AI grading) completion events (+4 more)

### Community 30 - "PSU Measurement Tests"
Cohesion: 0.15
Nodes (6): _make_hw(), Regression tests for _meas_vi(), set_psu_cccv(), and transient_dcir_measure() in, TestMeasViCombinedQuery, TestMeasViFallback, TestSetPsuCccv, TestTransientDcirMeasure

### Community 31 - "Analysis Helper Functions"
Cohesion: 0.14
Nodes (15): _cca_cutoff_v(), _check_soh_start_soc(), _confidence(), _extract_ecm_metrics(), identify_ecm_fit(), Single, unified post-test analysis for the whole application.  Every grade in, 0..1 grade confidence from (a) DCIR repeatability across steps, (b) distance to, 1-RC (and optionally 2-RC) Thevenin fit on an HPPC pulse.      The polarisatio (+7 more)

### Community 32 - "Mock Hardware & Direct Mode"
Cohesion: 0.14
Nodes (11): Mock Hardware Controller — ใช้สำหรับ simulation_mode และ unit testing ต้องมี in, _make_window(), Regression test: MANUAL -> Direct control now feeds the live trend graph.  Direc, Read-only: must not call estimator.update(), so it can never double-count, TestDirectModeGraphFeed, _make_bound_window(), Regression test for the "cloud gets data but the live graph stays blank" bug re, Locks in the restart-then-re-stop mechanics the fix relies on:         start_ch (+3 more)

### Community 33 - "Architecture Doc: Grading Classes"
Cohesion: 0.16
Nodes (17): grade_from_ecm(), BatteryAnalyzer, BatteryGrader, RandlesModelExtractor, ASETWebServer, 1-RC Thevenin Equivalent Circuit Model (OCV + R0 + R1‖C1), Endpoint anchor tolerance widening (max(0.25A, 1.5× tail current)), A/B/C/Reject Battery Grading System (+9 more)

### Community 34 - "DCIR Identification"
Cohesion: 0.16
Nodes (12): _dcir_temp_normalizer(), identify_dcir(), Chemistry-specific Arrhenius temperature multiplier for identify_dcir's 25 °C, Repeatable single-step DCIR aggregated over EVERY current step in the record., Real-file bug (sessions/test_20260709_154818.csv, 2026-07-09): at the     charg, TestDcirPlausibilityBand, _lead_acid_profile(), Phase D1 regression: identify_dcir() (aset_batt/acquisition/analysis.py) now nor (+4 more)

### Community 35 - "Trend Graph Widgets"
Cohesion: 0.10
Nodes (6): MultiAxisTrend, Voltage (left) + Current (right) + Temperature (far right) over time., Sensible idle-state view before any real data exists — otherwise         pyqtgr, Voltage+Current (top) / Temperature (bottom) — 2 separate plots., SplitTrend, _triple_specs()

### Community 36 - "Project Pivot Decisions"
Cohesion: 0.12
Nodes (20): Decision to cut 75Hz / sharp Ohmic-drop capture (no INA226/ESP32 fast-sensing purchase), Dropped: active ms-level cutoff -> software SCPI cutoff + MCB LUMIRA passive backstop, 2-point linear ADC calibration vs GW Instek reference, aset_batt/acquisition/analysis.py (R0/DCIR extrapolation, analyze_csv), set_psu_resistance_emulation() (hardware_driver.py), aset_batt/hardware/pel_batt_test.py (PEL native/PC-path battery test), Chemistry detector (acid vs lithium) from electrical signature features, Method 3: fast R0 capture via ESP32 kHz ADC + PEL Dynamic mode + TRIG OUT (+12 more)

### Community 37 - "Logging & Version Info"
Cohesion: 0.14
Nodes (10): เปิด CSV logging + ตั้งเวลาเริ่ม ถ้ายังไม่ได้เปิด (ให้ IEC test โผล่บน dashboard, get_app_version(), Any, Best-effort short git commit hash identifying the exact code that produced, Write a companion <csv_path>.meta.json capturing the audit-trail context     th, write_session_metadata(), Industrial-grade audit follow-up R3.  Session results used to carry NO audit t, A malformed/duck-typed config object must not crash test startup —         this (+2 more)

### Community 38 - "Rin Temperature/SoC Model"
Cohesion: 0.12
Nodes (10): คำนวณ OCV จาก SoC และ temperature ด้วย interpolation          direction: +1 =, |dOCV/dSoC| ต่อเซลล์ (V ต่อ %SoC) ที่ SoC ที่กำหนด          ใช้ตรวจช่วง platea, Reverse lookup: OCV (แพ็ค) -> SoC          รับแรงดันระดับแพ็ค หารด้วย series ก, How far (mV, pack-level) ``ocv_pack`` sits outside the calibrated OCV         c, จำกัดอุณหภูมิให้อยู่ใน range ที่มีข้อมูล, หา index ของ temperature ที่ใกล้เคียงที่สุด, คำนวณ internal resistance ขั้นสูงพร้อม temperature และ SoC compensation, Shared temperature factor for BOTH temp_rin_multiplier() (a pure         temper (+2 more)

### Community 39 - "Battery Pack Config"
Cohesion: 0.10
Nodes (11): BatteryConfig, HardwareConfig, Any, Hardware configuration parameters, Load configuration from file with validation, Update dataclass object from dictionary, Save current configuration to file, Get all configuration as dictionary (+3 more)

### Community 40 - "Session Path Naming"
Cohesion: 0.16
Nodes (7): สร้าง path สำหรับ session ใหม่.          ไม่มี label → sessions/test_20260625_, แปลง timestamp ในชื่อไฟล์ → '28 Jun 2026  18:47'.          รองรับทั้ง test_YYY, ดึงชนิดเทสต์จากชื่อไฟล์ test_LABEL_YYYYMMDD_HHMMSS.csv → 'LABEL' (ถ้ามี)., อัพเดทรายการ session files จาก sessions/ directory.         แสดง: ลำดับ · ชนิดก, เลือก session file → analyze ทันทีในแท็บ Analytics เดียวกัน, บอกชนิดการทดสอบ. ถ้าชื่อไฟล์ฝัง label ไว้ (test_HPPC_...) ใช้อันนั้นเลย, SessionManagerMixin

### Community 41 - "Hardware Connect Flow Tests"
Cohesion: 0.22
Nodes (8): Exception, _make_hw(), _mock_instruments(), Regression tests for HardwareController.connect_instruments()/ disconnect_instru, Safe idle state — see the comment right after this in the source: the         SS, TestConnectInstrumentsFailure, TestConnectInstrumentsSuccess, TestDisconnectInstruments

### Community 42 - "Config Field Validation Tests"
Cohesion: 0.15
Nodes (9): Industrial-grade audit G4: ConfigManager.validate_config() used to check only r, 0 = not specified is a legitimate, pre-existing convention elsewhere in, Proves WHY this matters: these fields feed pack_*_voltage directly., TestCellsParallelValidation, TestCellsSeriesValidation, TestMassGramsValidation, TestNominalVoltageValidation, TestPackVoltageDerivesFromTheseFields (+1 more)

### Community 43 - "Acquisition Profile Builder"
Cohesion: 0.12
Nodes (13): AcqProfile, analyze_csv_mp(), _get_analysis_pool(), profile_from_config(), Build the analysis profile (pack limits + safety window + baseline Rᵢ) from, Lazily-started, process-wide worker pool for analyze_csv_mp()., Same result as analyze_csv(), but the ECM curve-fit (scipy.optimize.curve_fit,, รัน unified analysis (ECM/grade) บน CSV ล่าสุด แล้ว post ANALYSIS_COMPLETED -> U (+5 more)

### Community 44 - "Harness Resistance Correction"
Cohesion: 0.15
Nodes (9): _correct_for_harness_r(), Subtract the rig's harness/contact resistance (BatteryConfig.     harness_resis, HPPC Full Sequence: CHARGE → REST 30 min → N×HPPC pulse/relax → ECM fit., _profile(), Phase D2 regression: defense-in-depth for BatteryConfig.harness_resistance_ohm., The scenario the plan explicitly calls out: a would-be REJECT->A flip via an, Callers rely on the returned list, not in-place mutation of the input —, TestRuntimeGuardPreventsFalseGradeAAtIntegrationLevel (+1 more)

### Community 45 - "Hardware Control UI"
Cohesion: 0.11
Nodes (5): HardwareControlMixin, Manual SSR override for diagnostics/recovery — normally the relay is         dr, Manual SSR cutoff — always safe (cuts power), no confirmation needed,         s, Deliberate operator action — a trip means something real happened         (see, ปิด PSU+Load แล้วรอให้แรงดันนิ่ง (ΔV/Δt criterion) ก่อนคำนวณ SoC

### Community 46 - "Hardware Backend Interface"
Cohesion: 0.12
Nodes (3): HardwareBackend, InstrumentBackend, Adapter wiring the real instrument HAL into the QThread worker.      ``hw`` is

### Community 47 - "PSU Self-Calibration"
Cohesion: 0.14
Nodes (10): # NOTE: calibrate_psu_zero() is NOT called here because at this point the, _config_ports(), main(), self_calibration_test.py — validate the whole R0/DCIR measurement chain against, _make_hw(), Regression test: HardwareController._esp_monitor_loop() reports its own per-iter, TestEspMonitorLoopTiming, _make_hw() (+2 more)

### Community 48 - "Cloud Push Metadata"
Cohesion: 0.14
Nodes (5): อัปเดต phase/test_mode/ETA ปัจจุบัน — ถูก merge เข้า meta ใน push ถัดไป., set_cloud_meta(), Auto-generate a serial number if none is provided., Update the HPPC phase indicator (REST / PULSE / cycle count) from elapsed time., TestControlMixin

### Community 49 - "Theme Retheme Registry"
Cohesion: 0.13
Nodes (18): SequencesMixin, on_retheme(), Select the active palette ("light" or "dark"), preferring colors     derived fr, Apply fn() (a zero-arg callable returning a stylesheet string built     from th, Register a zero-arg callback to run after every retheme() — for     refreshing, Switch the active theme immediately: updates the palette constants,     re-appl, retheme(), set_theme() (+10 more)

### Community 50 - "PSU Command Result Reporting Tests"
Cohesion: 0.16
Nodes (6): _make_hw(), The bug this closes: set_ssr() used to fire unconditionally, even after, MockHardwareController must present the same True/False return contract as, TestMockHardwareMirrorsTheContract, TestSetLoadReturnValue, TestSetPsuReturnValue

### Community 51 - "Event Bus Core"
Cohesion: 0.12
Nodes (8): EventBus, Setup event handlers for common UI events, Thread-safe event bus for UI communication, Start the event processing thread, Stop the event processing thread, Post an event to the queue, Add an event listener, Process events from the queue

### Community 52 - "UI Slots & Signal Wiring"
Cohesion: 0.12
Nodes (3): Pre-test Connect readback: shows Voltage/Current/Temp immediately after, Display a unified-analysis result (dict). Same renderer as a live test, UiSlotsMixin

### Community 53 - "Safety Limits Settings Tests"
Cohesion: 0.18
Nodes (16): _make_window(), OVP/UVP/OTP/UTP safety-limit editing (ก.ค. 2026): check_safety_limits() (auto_c, Simulates _on_product_changed's chemistry-based OVP/UVP override,     then conf, Regression against reintroducing the popup design — the fields must     stay in, dialogs.py's _on_open_settings used SettingsDialog with no import in     scope, Regression: the dialog used to read/write dark_mode/cloud_push/cloud_url,     n, Safety limits live inline on the SETUP tab now — this dialog should     not car, test_appearance_and_cloud_fields_use_real_systemconfig_attrs() (+8 more)

### Community 54 - "Capacity & SoH Calculation"
Cohesion: 0.17
Nodes (9): _calc_capacity_and_soh(), peukert_capacity(), ndarray, Drop values that disagree with the median by >n_sigma robust deviations (MAD)., Normalise a measured discharge capacity to a reference C-rate (Peukert's law)., _reject_outliers_mad(), Unit tests for the accuracy-improvement helpers added to acquisition.analysis:, TestMadRejection (+1 more)

### Community 55 - "Logging Initialization"
Cohesion: 0.13
Nodes (12): Initialize logging system, ASETLogger, get_logger(), log_errors(), log_performance(), Logging configuration for ASET Battery Characterization System, Centralized logging configuration, Configure logging with both file and console handlers (+4 more)

### Community 56 - "Self-Update Mechanism"
Cohesion: 0.28
Nodes (14): apply_update(), check_for_updates(), current_branch(), _git_env(), In-app update check/apply via git — powers the GUI's "update available" banner., env ที่ทำให้ git ไม่บล็อกรอ input เด็ดขาด — ถ้า remote จะถาม credential (เช่น, รัน git command — คืน (rc, stdout, stderr). rc=-1 ถ้ารัน git ไม่ได้เลย.      e, path เต็มของ git repo root ที่มีแพ็กเกจนี้อยู่ — None ถ้าไม่ใช่ repo/ไม่มี git. (+6 more)

### Community 57 - "Main UI Layout Builder"
Cohesion: 0.18
Nodes (4): Left column: three top-level tabs (SETUP / TEST MODE / TOOLS) that         foll, Bold caption that groups related controls inside a zone., Let a combo with long items shrink below its content width (the         current, UiBuilderMixin

### Community 58 - "Chemistry Profile Validation Tests"
Cohesion: 0.20
Nodes (4): fix #6: chemistry ใน JSON ที่ ocv_curve ไม่ครบ ต้องไม่ทำให้ registry ล่ม, Industrial-grade audit R8: presence-only checks used to let an out-of-range, TestProfileValidation, TestR8RangeValidation

### Community 59 - "Rin-Calibrated Flag Tests"
Cohesion: 0.19
Nodes (6): _FakeConfig, _FakeDataHandler, _FakeHW, Regression tests for the Rin "estimated vs measured" distinction.  Before any, TestLogSampleUsesCalibratedFlag, TestRinCalibratedFlag

### Community 60 - "Universal R0 Step Detector Tests"
Cohesion: 0.17
Nodes (9): _est(), Regression tests for StateEstimator._detect_step_r0 — the universal single-step, R0-only must NOT claim the UI's stricter "fully measured" label --         R1/C, A real edge whose post-edge sample arrives too late (dt beyond the         stal, If the buffer itself already shows a wide voltage spread (not a         genuine, The original bug (SoC 80%->100% within ~2 minutes of a real 0.1C         charge, TestCleanStepIsDetected, TestGatesTheEkfRunawayGuard (+1 more)

### Community 61 - "Safety Shutdown Path Tests"
Cohesion: 0.21
Nodes (13): _make_char_host(), _make_seq_host(), Safety-shutdown wiring tests (ก.ค. 2026 safety audit).  ครอบเส้นทาง "ตัดไฟให้ไ, stop_charge ระเบิด → ยังต้องพยายาม load_off + psu_off ต่อ (และกลับกัน), test_bootstrapper_cleanup_is_idempotent(), test_char_safety_brief_stale_warns_once_but_continues(), test_char_safety_nan_temp_does_not_false_trip(), test_char_safety_ok_path() (+5 more)

### Community 62 - "DCR Timepoint Reporting (G5)"
Cohesion: 0.19
Nodes (8): ecm_r_at(), 1-RC/2-RC model DC resistance at pulse time ``t`` seconds:     ``R0 + R1·(1−e^(, G5: report DC resistance at the FreedomCAR/USABC pulse timepoints (R@0.1s / R@1, rest -> constant-current pulse, matching the real fit buffer shape., A plain discharge has no pulse edge to fit an ECM to — the timepoint         re, _synthetic_pulse(), TestEcmRAtPureFunction, TestTimepointResistancesInResults

### Community 63 - "DCIR At Fixed Timepoints"
Cohesion: 0.27
Nodes (8): identify_dcir_at_timepoints(), _lead_acid_profile(), identify_dcir_at_timepoints() (aset_batt/acquisition/analysis.py) — additive alo, Rest (I=0) then a clean current step held for pulse_s seconds, terminal     volt, A pulse shorter than a requested timepoint must not silently borrow         a sa, At exactly 25 C the Arrhenius multiplier is 1.0 (same normalizer as         iden, _synthetic_pulse(), TestDcirAtTimepoints

### Community 64 - "Chemistry Auto-Detection"
Cohesion: 0.16
Nodes (4): ดึง (rested_ocv_full, mid_slope) ระดับแพ็คจาก BatteryModel (สำหรับจำลอง/ทดสอบ), Tests สำหรับแนวใหม่ (แบตมอเตอร์ไซค์ 12V): - โมเดล lead-acid (OCV sloped, revers, TestChemistryDetector, TestLeadAcidModel

### Community 65 - "Instrument Safety Protection Config"
Cohesion: 0.15
Nodes (7): Set the PEL-3111 static-mode current/voltage range (CC/CR/CV/CP share         t, Query SYSTem:ERRor? once (verified syntax, both instruments — returns         ", Set PEL-3111 hardware trip points — verified syntax:         [:CONFigure]:OCP {, Set PSW hardware OCP/OVP trip points — verified syntax:         [SOURce:]CURRen, One-time defensive config applied on every connect:         - PSU: disable auto, Query model/serial/firmware for traceability (which exact unit/firmware, Apply the mandatory hardware-level safety backstop — PEL-3111 range         aut

### Community 66 - "Event Type Definitions"
Cohesion: 0.19
Nodes (9): EventType, Any, Enum, Thread-safe event system for UI communication, Event types for UI communication, Post an event to the bus, Remove an event listener, Regression test: EventType.SHOW_MESSAGE must reach a real UI handler, not a lef (+1 more)

### Community 67 - "Alarm/Interlock UI Tests"
Cohesion: 0.22
Nodes (6): _make_window(), Industrial-grade audit follow-ups R1 and R6.  R1: Alarm Log "Clear" used to wi, Regression guard for the stale-comment bug itself: if a second         _alarm_c, Turning OFF must never be blocked — that would leave no way to cut         powe, TestAlarmClearConfirmation, TestManualControlsRespectBusyReason

### Community 68 - "CSV Logging Fidelity Tests"
Cohesion: 0.21
Nodes (4): _LoggingCase, The current-step edge case the 0.25s cap protects: a changed value         (here, TestElapsedResolutionAndTimestampDate, TestRedundantRowThrottle

### Community 69 - "OCV Out-of-Range Detection Tests"
Cohesion: 0.26
Nodes (4): Safety: a BELOW-range reading means the pack already reads as         near-empt, True on the very first call (let one reading happen), False on every         ca, TestCalibrateFromOcvStableSurfacesOutOfRangeWarning, TestSurfaceChargeBleedOff

### Community 70 - "DCIR From V-I Slope"
Cohesion: 0.22
Nodes (7): dcir_from_vi_slope(), Robust DCIR from the slope of V vs I across distinct current levels:     ``V =, (current, terminal-voltage) points — one per distinct current level (rest + each, _vi_levels(), TestViSlopeDcir, Regression test: _vi_levels() must reject a "level" whose voltage spans a real, TestViLevelsRejectsSocDrift

### Community 71 - "Chemistry Profile Registry"
Cohesion: 0.18
Nodes (13): _charge_from_dict(), ChargeProfile, _chemistry_from_dict(), ChemistryProfile, get_chemistry(), _load_registry(), สร้าง ChargeProfile จาก dict โดยเริ่มจาก base (เติมเฉพาะ key ที่ให้มา), กลยุทธ์การชาร์จต่อเคมี (ค่าแรงดันเป็น 'ต่อเซลล์' — คูณ series เป็นแพ็คตอนใช้งาน) (+5 more)

### Community 72 - "IEC 61960 Test Profiles"
Cohesion: 0.15
Nodes (7): IEC61960TestProfile, Any, ดึง test profile ตามชื่อ, สร้าง test report ตาม IEC 61960 format, ตรวจสอบว่าการทดสอบเป็นไปตาม IEC 61960 หรือไม่, Test profile ตาม IEC 61960, สร้าง test profiles มาตรฐานตาม IEC 61960

### Community 73 - "Cloud Push Client"
Cohesion: 0.19
Nodes (8): CloudPushService (removed dead code), CloudPusher, Background daemon ที่ push CSV ล่าสุดขึ้น cloud เป็นช่วง ๆ (auto-push)      ให, หา ingest token: arg ตรง > env INGEST_TOKEN > ไฟล์ cloud_token.txt (gitignored), resolve_token(), test_cloud_pusher_failure(), test_cloud_pusher_send(), test_cloud_pusher_start_stop()

### Community 74 - "Cloud Push Payload Builder"
Cohesion: 0.22
Nodes (11): build_payload(), _downsample(), cloud_push.py — ส่งข้อมูลเทสต์ล่าสุดจากเครื่องแล็บขึ้น ASET Cloud Dashboard  อ, ลดจำนวนจุดของ series ให้ไม่เกิน max_points (stride sampling)      window_s: ถ้, Build push payload. Pass cached_analysis to skip the expensive ECM fitting., _extract_series(), Return up to *limit* rows from *csv_path* as a list of dicts., Like _tail_csv_rows but reuses *cache* (caller-owned, e.g. one dict per     Clo (+3 more)

### Community 75 - "EN 50342-1 Capacity Conditions"
Cohesion: 0.24
Nodes (5): en50342_capacity_conditions(), Check a capacity run's settings against EN 50342-1's Cn-test conditions., Tests for the EN 50342-1 (SLI lead-acid) capacity-test condition checker.  Conte, TestApplicability, TestStandardConditions

### Community 76 - "CCA Proxy Test"
Cohesion: 0.24
Nodes (6): _make_bound_window(), Regression tests for the CCA-proxy test added to the CHARACTERIZE tab.  Real C, TestCcaCurrentClamping, TestCcaFeedsGraphAndCsv, TestCcaPassFail, TestCcaSkipsWhenNoRating

### Community 77 - "Cloud Push Background Loop"
Cohesion: 0.20
Nodes (8): _NumpySafeEncoder, push(), JSON encoder ที่รองรับ numpy scalar/array โดยไม่ต้อง import numpy at module leve, push หนึ่งครั้ง — ใช้ cached analysis ถ้ายังไม่ถึงเวลา refresh (best-effort), Poll cloud for pending re-analysis requests; run them and push results back., Run unified analysis on *csv_path*; returns a result dict., _run_analysis(), test_numpy_safe_encoder()

### Community 78 - "PDF Report Generation"
Cohesion: 0.29
Nodes (9): generate_pdf_report(), _info_table(), PDF Report Generator — รายงานผลทดสอบแบตเตอรี่ (สำหรับงานคัดแยก / เล่ม capstone), render กราฟ V/I vs time จาก CSV → ไฟล์ PNG ชั่วคราว (คืน path หรือ None), สร้างไฟล์ PDF รายงานผลทดสอบ      path      : ปลายทาง .pdf     config    : Con, _render_csv_plot(), test_generate_pdf_report(), test_info_table() (+1 more)

### Community 79 - "Bench Diagnostic Script"
Cohesion: 0.33
Nodes (11): check1_psu_voltage_when_off(), check2_load_voltage_when_off(), check3_step_sharpness(), check4_dcir_repeatability(), _load_ports(), main(), _open(), _q() (+3 more)

### Community 80 - "Current Card Color UI Tests"
Cohesion: 0.26
Nodes (5): _make_window(), Industrial-grade audit follow-up G1 (partial — see the summary given to the use, TestChargingAndRestUnaffected, TestCurrentNearLimitEscalatesColor, TestNormalDischargeIsNeutralNotAmber

### Community 81 - "Endpoint Anchor Sustain Gate Tests"
Cohesion: 0.24
Nodes (6): A brief dip below threshold followed by recovery shouldn't accumulate         t, Reproduces the exact real-CSV failure: one sample crossing the 0%         thres, The gate must not block a REAL empty condition — just require it to         per, Regression for test_QuickScan_20260712_150458.csv: Quick Scan/IEC/Cycle, The extra consecutive-sample requirement must not block a genuinely         sus, TestZeroAnchorSustainGate

### Community 82 - "EKF Accuracy Fix Tests"
Cohesion: 0.23
Nodes (6): _est(), EKF accuracy fixes, and the live SoC/Rin display accuracy work:   #1 the measur, Live Rin must be temperature-aware AND SoC-aware, and returned alongside soc_std, TestLiveRinAccuracy, TestOhmicR0InUpdate, TestPlateauInitUncertainty

### Community 83 - "Stale Graph Generation Tests"
Cohesion: 0.26
Nodes (10): _make_window(), Regression test (ก.ค. 2026 — real-hardware-only "graph shows 2 overlapping line, Synchronous callers (sequences/characterize) don't pass _gen — it     should de, The monitor loop path: it must capture gen BEFORE scheduling via     root.after, test_char_guard_bumps_generation_only_when_nothing_else_running(), test_current_generation_sample_is_kept(), test_on_run_test_bumps_generation(), test_stale_generation_sample_is_dropped() (+2 more)

### Community 85 - "HPPC Live ECM Feed Tests"
Cohesion: 0.33
Nodes (6): A too-short buffer (fewer than fit_model's own 10-sample minimum)         must, rest -> pulse, matching sequences.py's real fit-and-feed buffer shape: a     fe, Reproduces the exact fit-and-feed statements sequences.py's HPPC pulse     leg, The exact logic from sequences.py's post-pulse block., _synthetic_pulse(), TestPerCyclePulseFeedsLiveEstimator

### Community 86 - "Sequence Estimator Feed Tests"
Cohesion: 0.22
Nodes (4): _method_src(), Regression tests for the frozen-SoC fix: HPPC pulse/relax legs and the Cycle Lif, TestCycleLifeDischargeFeedsEstimator, TestHppcLegsFeedEstimator

### Community 87 - "App Launcher Entry Point"
Cohesion: 0.29
Nodes (6): Application launcher — builds the QApplication, ISA-101 window, and wires the b, Launch the integrated PySide6 GUI. Returns a process exit code., run(), Enable ``python -m aset_batt`` to launch the integrated GUI., Root entry shim — keeps ``python main.py`` working.  The application lives in, test_run_main()

### Community 88 - "PEL-3111 Range Auto-Select"
Cohesion: 0.31
Nodes (4): Pick the narrowest PEL-3111 CRANge/VRANge that still leaves headroom     above, recommend_pel3111_ranges(), Regression tests for auto-selecting the PEL-3111 CRANge/VRANge on connect.  Cons, TestRecommendPel3111Ranges

### Community 89 - "Pre-Test Confirmation Dialog"
Cohesion: 0.20
Nodes (5): Show a pre-test confirmation card.  Returns True iff user clicks Confirm., ผู้ใช้เปลี่ยน C-rate selector — อัป amp label + stage breakdown, สร้างข้อความ stage breakdown และอัป lbl_charge_crate, In-app dialog to edit BatteryConfig fields and save to config.json., QDialog

### Community 90 - "Trend Crosshair Widget"
Cohesion: 0.33
Nodes (3): Shared crosshair for a TrendContainer: a vertical dashed line synced     across, Call after the visible graph mode changes — attaches a line+label         to ev, TrendCrosshair

### Community 91 - "Azure Deployment Guide"
Cohesion: 0.24
Nodes (10): Azure App Service (Linux, Python 3.11), Deploy ASET Cloud Dashboard on Azure (guide), Why `python server.py` startup-file is required on Azure, ASET Cloud Dashboard README, cloud_dashboard requirements.txt (stdlib-only marker), cloud_dashboard/server.py (stdlib HTTP server, /api/ingest, /api/health), cloud_push.py (root) - pushes lab data to cloud dashboard via POST /api/ingest, DigitalOcean deployment option (App Platform / Droplet) (+2 more)

### Community 93 - "PSU Trip Clear UI Tests"
Cohesion: 0.31
Nodes (4): _make_window(), Regression test for the manual "Clear Protection Trip" control (Direct tab).  Ha, TestCheckPsuTrip, TestClearPsuTrip

### Community 95 - "SSR Manual Control UI Tests"
Cohesion: 0.29
Nodes (3): Regression test for the manual SSR ON/OFF control added to the SETUP zone.  The, TestCharacterizeZoneNoLongerShadowed, TestSsrManualControlButtons

### Community 96 - "Rig Status: Hardware Protection"
Cohesion: 0.22
Nodes (9): calibrate_psu_zero() (hardware_driver.py), connect_esp32() (hardware_driver.py), set_ssr() (hardware_driver.py), CCA-proxy test in CHARACTERIZE tab (non-standard, self-comparison only), Rig Status / Action Items (harness-resistance / OCV-anchor bug follow-up), PEL-3111 range-setting accuracy tradeoff (full-scale error depends on set range), Automatic PEL-3111 CRANge/VRANge selection on connect (conservative 75% margin), Finding: PEL-3111 static-mode range SCPI commands ([:MODE]:CRANge/VRANge) exist (+1 more)

### Community 97 - "PEL/PSW Hardware Reference Doc"
Cohesion: 0.31
Nodes (9): aset_batt/hardware/hardware_driver.py (PyVISA/SCPI control of PSU+Load), PEL-3111 / PSW 80-40.5 Hardware Reference, LinkView vs aset_batt feature comparison (control-only vs battery health analysis), LinkView (GW Instek PC software, LabVIEW-based), GW Instek PEL-3111 e-Load (0-210A/1.5-150V, 1050W), GW Instek PSW 80-40.5 PSU (1080W, 80V/40.5A), Remote sense wiring procedure for PEL-3111 and PSW (force-before-sense ordering), Finding: rig sense wiring never reaches the battery; recommended topology fix (+1 more)

### Community 99 - "Cloud Dashboard Limitations"
Cohesion: 0.28
Nodes (9): Cloud dashboard has no persistent database, no auth, context_summary.md (latest status/architecture doc), Limitations and Future Work (ASET Battery Characterization), Future work: hardware-in-the-loop validation across chemistries, Future work: time-series DB, dashboard auth, CI, Hardware validation status: sign convention verified on FB FTZ6V, lithium 4S still simulated, In-memory snapshot has no persistent history (restart loses data), LFP flat OCV-SoC plateau (~20-90%) is ill-conditioned for SoC inversion (+1 more)

### Community 100 - "Charge Efficiency Calibration Script"
Cohesion: 0.36
Nodes (8): _config_ports(), main(), _model_eta_for_soc(), _print_band_summary(), charge_efficiency_calibration.py — measure REAL charging coulombic efficiency (η, Mirrors StateEstimator._coulomb_eta's lead-acid bands — kept in sync     manuall, Same ΔV/Δt settle criterion as AutoController.calibrate_from_ocv_stable():     r, wait_for_settle()

### Community 101 - "Estimator Replay/Backtest Script"
Cohesion: 0.33
Nodes (8): ground_truth(), load_csv(), main(), metrics(), Replay / ablation harness — validate SoC-estimation accuracy offline.  Records, Return lists (t_s, v, i_dis_positive, temp). Tolerant of column names., SoC_true(t) from current integration (trapezoidal). Returns (soc_true, cap)., run_config()

### Community 102 - "ML Grader Training Script"
Cohesion: 0.31
Nodes (8): build_dataset(), main(), ndarray, train_grader.py — สคริปต์ train โมเดล grader จาก labeled CSV  ใช้ทีหลังเมื่อมี, อ่าน labels file -> list ของ (csv_path_absolute, grade)      ถ้ามีคอลัมน์ 'tes, สกัด features จากทุกไฟล์ -> (X, y), _read_labels(), train()

### Community 103 - "Anchor Settle & SoH Reset Tests"
Cohesion: 0.33
Nodes (4): Fresh-calibration entry points already require the caller to have waited, Simulate the exact failure mode: 100% anchor fires, then terminal voltage, Once the settle window has elapsed, a persistently-low voltage SHOULD be, TestAnchorSettleWindow

### Community 104 - "Uncalibrated R0 Runaway Tests"
Cohesion: 0.31
Nodes (5): _lead_acid_estimator(), ~140s of a real 0.1C charge current must move SoC by roughly the         coulom, The gate only guards against active current -- near true rest the         IR-dr, Once a real HPPC/ECM fit lands, the gate must not suppress the         measurem, TestUncalibratedR0DoesNotRunawaySoc

### Community 105 - "Rig Investigation Findings Doc"
Cohesion: 0.32
Nodes (8): StateEstimator.standby_current attribute (state_estimator.py), Finding: PSW bleeder resistor can be disabled via SCPI, may eliminate need for SSR, Rig Investigation Findings (harness-resistance / OCV-anchor bug), Finding: _I_STANDBY=0.6 misidentified bulk charge current; corrected to 0.0, SYSTem:CONFigure:BLEeder[:STATe] SCPI command (PSW), Full SCPI command index for PEL-3111 and PSW (198 + 74 commands), FOTEK SSR-50DD solid state relay (in PSU Force+ line), Undecided: keep SSR hardware vs replace with BLEeder OFF SCPI command

### Community 106 - "CSV Summary Stats"
Cohesion: 0.32
Nodes (5): _compute_summary(), Compute simple summary stats from CSV rows., _compute_summary() (data_utils.py) is what the cloud dashboard payload's     su, Old CSVs logged before this field existed always logged a real per-sample, TestComputeSummaryRinCalibrated

### Community 107 - "Sequence Abort/Cancel Handling"
Cohesion: 0.25
Nodes (3): Reset every workflow's step LEDs to idle (start of a new run, or abort)., Slot for sig_seq_aborted — the banner alone used to reset on a safety trip,, Shared startup: reset all step leds, buffers, progress, result card.

### Community 108 - "Theme Contrast/Color Utils"
Cohesion: 0.29
Nodes (8): _adjust(), contrast_text(), _luminance(), _material_overrides(), Pick a readable text color (near-black or white) for an arbitrary     backgroun, Map a qt-material theme dict (qt_material.get_theme() result) onto our     base, _to_hex(), _to_rgb()

### Community 109 - "HPPC 5Hz Pacing Tests"
Cohesion: 0.25
Nodes (3): HPPC pulse/relax polling rate increased from 1Hz to ~5Hz via remaining-time sleep pacing, Regression test: HPPC relax/pulse legs are paced at ~5 Hz (0.2s), not the old fl, TestHppcPacingSourcePattern

### Community 110 - "Alarm Beep Tests"
Cohesion: 0.39
Nodes (3): _make_window(), Regression test: _log_alarm() beeps the PSU on genuine ALARM events (not WARNING, TestAlarmBeep

### Community 111 - "R0 Plausibility Band Tests"
Cohesion: 0.25
Nodes (3): The [0.2x, 6x]-relative-plus-absolute-ceiling check used to be     reimplemente, StateEstimator._STEP_MAX_DT_S/_STEP_REF_MAX_SPREAD_V and         analysis._DCIR, TestPlausibilityBandDedup

### Community 114 - "Rin-SoC Shape Tests"
Cohesion: 0.43
Nodes (3): _est(), Regression test: the live rin from the EKF path must follow the chemistry's SoC, TestLiveRinFollowsSocShape

### Community 115 - "Shutdown Cuts Outputs Tests"
Cohesion: 0.39
Nodes (3): _emergency_shutdown (psu_off/load_off) must run BEFORE         hw.shutdown_all, The idempotency latch must only be set on success: if         hw.shutdown_all r, TestControllerShutdownCutsOutputsFirst

### Community 116 - "Zero-Anchor Calibration Gate Tests"
Cohesion: 0.36
Nodes (4): Reproduces the real Quick Scan failure: the same voltage/current         numbers, The gate is about trust, not about permanently disabling the         anchor — on, The 0% anchor only ever evaluates while actively discharging         (cur>0 is b, TestZeroAnchorRequiresCalibration

### Community 117 - "Future Work: SoC Estimation"
Cohesion: 0.29
Nodes (7): calibrate_from_ocv_stable() (state_estimator.py), Future work: adaptive SoC estimation (EKF/UKF, sliding-mode, H-infinity), OCV anchor via dV/dt convergence (PREPARE phase, >=300s), Peukert correction (I/I_rated)^(k-1), k=1.30, C10 lead-acid, Enhanced coulomb counting method for SoC/SoH estimation (ScienceDirect, Applied Energy), SoC estimation: Coulomb counting + OCV correction + EMA(a=0.05), State-dependent Coulomb (Faraday) efficiency by SoC band, lead-acid

### Community 119 - "Force-HPPC Detection Tests"
Cohesion: 0.43
Nodes (3): The exact scenario from the bug report: HPPC Full Sequence just ran in, No in-session memory of the test type (e.g. after an app restart) —         mus, TestAnalyzeCsvForceHppc

### Community 121 - "Rig Status: Auto Protection Config"
Cohesion: 0.33
Nodes (6): get_instrument_info() (hardware_driver.py), harden_instrument_config() (hardware_driver.py), set_load_protection() (hardware_driver.py), set_psu_protection() (hardware_driver.py), Automatic hardware protection config on connect (OVP/OCP/UVP, panel lock, short-safety, beep, device-info), tests/test_instrument_protection.py

### Community 122 - "PEL Native BATT SCPI"
Cohesion: 0.33
Nodes (6): NATIVE_BATT_SCPI dict (pel_batt_test.py), native_supported() (pel_batt_test.py), run_pc_discharge() (pel_batt_test.py, recommended path), PEL-3111 native BATTery subsystem SCPI commands (:BATTery:*), Finding: NATIVE_BATT_SCPI dict has wrong command names; native_supported() always False, harmless (fallback path is recommended anyway), tests/test_pel_batt_native_scpi.py

### Community 123 - "CI/CD Split Workflows"
Cohesion: 0.40
Nodes (5): cloud_dashboard/README.md, Split CI/CD: cloud_dashboard/ auto-deploys to Azure on push to main, separate from GUI test workflow, GitHub Actions: cloud_dashboard test suite workflow, GitHub Actions: GUI (aset_batt/) test suite workflow, GitHub Actions: build+deploy cloud_dashboard to Azure Web App

### Community 124 - "Dashboard HTML Page"
Cohesion: 0.47
Nodes (6): ASET Cloud Dashboard HTML page (Battery Health), Analytics tab (Grade/SoH/Rin cards, sessions list, ECM values), Diagnostics tab (ICA - Incremental Capacity Analysis chart), Randles equivalent-circuit SVG diagram (Voc-Ra-Rd||Cd-Vt), Telemetry strip (VOLTAGE/CURRENT/SOC/RIN/TEMP/SOH cards), Test activity monitor panel (PREPARE/CHARGE/REST/TEST/ANALYZE steps)

### Community 125 - "Synthetic Training Data Script"
Cohesion: 0.47
Nodes (5): grade_of(), main(), make_training_data.py — สร้าง labeled dataset สังเคราะห์สำหรับเทรน BatteryGrader, กฎ label (ตรงแนวกับ heuristic) — โมเดลจะเรียนรู้ขอบเขตจากหลายฟีเจอร์, simulate_cell()

### Community 128 - "Grade Decision Logging Tests"
Cohesion: 0.47
Nodes (3): _profile(), Industrial-grade audit follow-up R5.  analyze_series()'s grading decision used, TestGradeDecisionIsLogged

### Community 129 - "IEC 61960 Enums"
Cohesion: 0.40
Nodes (5): DischargeRate, Enum, ประเภทการทดสอบตาม IEC 61960, อัตราการ discharge ตาม IEC 61960, TestType

### Community 130 - "University/Faculty Logos"
Cohesion: 0.60
Nodes (5): EN UBU Logo (Faculty of Engineering, Ubon Ratchathani University), Cloud Dashboard (Python stdlib web app), Ubon Ratchathani University Logo (English), Faculty of Engineering (EN), UBU, Ubon Ratchathani University (UBU)

### Community 131 - "IEC 61960 DCIR Compliance"
Cohesion: 0.40
Nodes (5): DCIR two-pulse method (V1-V2)/(I2-I1), IEC 61960 Clause 6.4, Future work: full IEC 61960 validation against reference instrument, IEC 61960 compliance status (DCIR two-pulse, clamped current), How to perform internal resistance measurement according to IEC 61960 (Arbin Instruments), Internal Resistance: DCIR and ACIR (Battery Design)

### Community 132 - "Future Work: ML Grading"
Cohesion: 0.40
Nodes (5): Future work: ML grading with real labeled cycling/EIS dataset, RandomForest grader trained on rule-labeled synthetic data (not real), State of Health estimation for Li-ion batteries using Random Forest and GRU (ScienceDirect), Machine learning pipeline for battery state of health estimation (arXiv), Optimized Random Forest regression model for Li-ion prognostics and health management (MDPI Batteries)

### Community 133 - "Future Work: Rin Temperature Model"
Cohesion: 0.40
Nodes (5): Future work: fit Rin Arrhenius model from multi-temperature DCIR data, Nernst OCV temperature compensation (+0.40 mV/C/cell, lead-acid), Measurement of temperature influence on current distribution in Li-ion batteries (Wiley, Arrhenius), Multi-factor dynamic internal resistance model with error compensation (ScienceDirect), Internal resistance (Rin) vs temperature - linear model (not Arrhenius)

### Community 137 - "RUN Zone Crash Traceback"
Cohesion: 0.83
Nodes (4): BatteryQtWindow._build_left_panel, _led (undefined helper), ZonesMixin._zone_run, zone_err.txt (RUN zone crash traceback)

### Community 138 - "Future Work: OCV Hysteresis"
Cohesion: 0.50
Nodes (4): Future work: 3D OCV-H-SoC hysteresis map / dual-polarization ECM, OCV-SoC model lacks voltage hysteresis modeling (LFP), Slope-adaptive SoC for LFP with temperature-aware hysteresis modeling (ScienceDirect), Enhanced SoC for LFP: Coulomb counting reset + ML + relaxation (ACS Energy Letters)

### Community 139 - "Faculty Logo Asset"
Cohesion: 0.67
Nodes (3): Faculty of Engineering, Ubon Ratchathani University Logo, ASET Battery Tester GUI branding asset, Faculty of Engineering, Ubon Ratchathani University

### Community 141 - "ASET Brand Logo"
Cohesion: 0.67
Nodes (3): ASET Logo (asetlogo.png), cloud_dashboard/static/index.html, ASET Battery Tester Brand/Project

## Ambiguous Edges - Review These
- `Analytics` → `Analytics (local redefinition, acquisition/analysis.py:406)`  [AMBIGUOUS]
  flake8_report.txt · relation: semantically_similar_to

## Knowledge Gaps
- **57 isolated node(s):** `lastSeriesRecent`, `lastSeriesFull`, `mainCharts`, `alarmLog`, `PHASE_MAP` (+52 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **32 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Analytics` and `Analytics (local redefinition, acquisition/analysis.py:406)`?**
  _Edge tagged AMBIGUOUS (relation: semantically_similar_to) - confidence is low._
- **Why does `BatteryModel` connect `Core Component Wiring` to `Test Analysis & Grading`, `Acquisition Worker & Backends`, `Automated Test Controller`, `Application Bootstrap`, `Session Data Storage`, `HPPC Fit-and-Feed Regression Tests`, `SoC/SoH State Estimator`, `Mock Hardware Simulation`, `Current Sign Convention Tests`, `Charge Loop Control`, `Battery OCV Model & EKF`, `Unified Series Analysis`, `Settings Dialog (diff artifact)`, `Estimator Scratch File`, `Realtime Accuracy Test Fixtures`, `Charge Voltage Gate Tests`, `Analysis Helper Functions`, `Mock Hardware & Direct Mode`, `Architecture Doc: Grading Classes`, `DCIR Identification`, `Logging & Version Info`, `Rin Temperature/SoC Model`, `Acquisition Profile Builder`, `Theme Retheme Registry`, `Chemistry Profile Validation Tests`, `Rin-Calibrated Flag Tests`, `Universal R0 Step Detector Tests`, `Chemistry Auto-Detection`, `OCV Out-of-Range Detection Tests`, `CCA Proxy Test`, `Endpoint Anchor Sustain Gate Tests`, `EKF Accuracy Fix Tests`, `HPPC Live ECM Feed Tests`, `Pre-Test Confirmation Dialog`, `Charge Efficiency Calibration Script`, `Estimator Replay/Backtest Script`, `Anchor Settle & SoH Reset Tests`, `Uncalibrated R0 Runaway Tests`, `CSV Summary Stats`, `R0 Plausibility Band Tests`, `Measured Params Validation Tests`, `Chemistry Registry Tests`, `Rin-SoC Shape Tests`, `Zero-Anchor Calibration Gate Tests`, `Aging Factor Wiring Tests`, `Pack Scaling Tests`, `Synthetic Training Data Script`, `OCV Interpolation Tests`, `Stale-Check Ordering Tests`?**
  _High betweenness centrality (0.193) - this node is a cross-community bridge._
- **Why does `ConfigManager` connect `App Configuration Management` to `Test Analysis & Grading`, `Acquisition Worker & Backends`, `Automated Test Controller`, `Application Bootstrap`, `Session Data Storage`, `HPPC Fit-and-Feed Regression Tests`, `SoC/SoH State Estimator`, `Mock Hardware Simulation`, `Write-Off Verified Tests`, `Core Component Wiring`, `Battery OCV Model & EKF`, `Settings Dialog (diff artifact)`, `Event Bus System`, `Mock Hardware & Direct Mode`, `Architecture Doc: Grading Classes`, `DCIR Identification`, `Logging & Version Info`, `Battery Pack Config`, `Config Field Validation Tests`, `Acquisition Profile Builder`, `Harness Resistance Correction`, `PSU Self-Calibration`, `Safety Limits Settings Tests`, `DCR Timepoint Reporting (G5)`, `Event Type Definitions`, `Alarm/Interlock UI Tests`, `OCV Out-of-Range Detection Tests`, `CCA Proxy Test`, `Current Card Color UI Tests`, `Stale Graph Generation Tests`, `HPPC Live ECM Feed Tests`, `PSU Trip Clear UI Tests`, `Retheme & Crosshair Tests`, `SSR Manual Control UI Tests`, `Charge Efficiency Calibration Script`, `Alarm Beep Tests`, `R0 Plausibility Band Tests`, `Shutdown Cuts Outputs Tests`, `Aging Factor Wiring Tests`, `Force-HPPC Detection Tests`, `CI/CD Split Workflows`, `Stale-Check Ordering Tests`?**
  _High betweenness centrality (0.112) - this node is a cross-community bridge._
- **Why does `BatteryQtWindow` connect `App Configuration Management` to `Test Analysis & Grading`, `Acquisition Worker & Backends`, `Automated Test Controller`, `Session Data Storage`, `Write-Off Verified Tests`, `Mock Hardware Simulation`, `Battery Characterization Pipeline`, `Core Component Wiring`, `Crash Recovery State`, `IEC 61960 Standard Compliance`, `Characterize Tab UI`, `Settings Dialog (diff artifact)`, `Event Bus System`, `Mock Hardware & Direct Mode`, `Trend Graph Widgets`, `Session Path Naming`, `Hardware Control UI`, `Hardware Backend Interface`, `Cloud Push Metadata`, `Theme Retheme Registry`, `UI Slots & Signal Wiring`, `Safety Limits Settings Tests`, `Main UI Layout Builder`, `Event Type Definitions`, `Alarm/Interlock UI Tests`, `CCA Proxy Test`, `Current Card Color UI Tests`, `Stale Graph Generation Tests`, `App Launcher Entry Point`, `PSU Trip Clear UI Tests`, `Retheme & Crosshair Tests`, `SSR Manual Control UI Tests`, `Alarm Beep Tests`, `Shutdown Cuts Outputs Tests`, `Aging Factor Wiring Tests`, `Force-HPPC Detection Tests`, `CI/CD Split Workflows`?**
  _High betweenness centrality (0.098) - this node is a cross-community bridge._
- **Are the 113 inferred relationships involving `BatteryModel` (e.g. with `ApplicationBootstrapper` and `._create_core_components()`) actually correct?**
  _`BatteryModel` has 113 INFERRED edges - model-reasoned connections that need verification._
- **Are the 88 inferred relationships involving `ConfigManager` (e.g. with `ApplicationBootstrapper` and `._initialize_services()`) actually correct?**
  _`ConfigManager` has 88 INFERRED edges - model-reasoned connections that need verification._
- **Are the 92 inferred relationships involving `StateEstimator` (e.g. with `ApplicationBootstrapper` and `._create_core_components()`) actually correct?**
  _`StateEstimator` has 92 INFERRED edges - model-reasoned connections that need verification._