# ASET Battery Tester performance and UI-freeze audit

Audit date: 2026-09-20. Scope: current PySide6 application in `aset_batt/`; simulated and synthetic workloads only. No physical battery test or PSU/PEL output command was run. Synthetic CSVs and logger output were written under the system temporary directory; historical data was not modified.

## 1. Execution and thread map

| Component | Source | Context and rate | Can block / GUI effect |
|---|---|---|---|
| Qt window, widgets, timer callbacks, plot and CSV Analyze result slots | `aset_batt/ui/isa101_views.py`, `ui/views/*`, `ui/widgets.py` | Qt main thread | Any synchronous callback or slot delays painting and input. |
| Manual Test / HPPC acquisition | `acquisition/worker.py`, started from `ui/views/test_control.py` | `AcquisitionWorker` moved to a `QThread`; target 10 Hz (`DEFAULT_SAMPLE_HZ`) | Instrument I/O, append logging, estimator, safety, and final analysis run off GUI. Telemetry is emitted once/sample and received by GUI slots. |
| Legacy Monitor loop | `app/auto_controller.py` | Daemon Python thread, nominal 10 Hz | SCPI, estimator and CSV append run in worker. UI updates are queued through `QtRootShim.after()` and Qt signals. |
| Automated sequences / characterization | `ui/sequences/*.py`, `ui/characterize.py` | `threading.Thread` workers | Hardware loops and waits run off GUI; Qt signals marshal display updates. A slow instrument delays the sequence/sample, not the Qt event loop. |
| PSW / PEL SCPI | `hardware/hardware_driver.py` | Called from worker in acquisition/sequence paths; also called synchronously by Connect and Direct-page heartbeat paths | VISA timeout is 5,000 ms. `_meas_vi` has a 200 ms retry path. Calls made by the two GUI paths can hold up the UI. |
| ESP32 serial reader | `hardware/hardware_driver.py::_esp_monitor_loop` | Daemon thread, nominal 20 iterations/s (`sleep(0.05)`); `Serial(..., timeout=1)` | Checks `in_waiting` before `readline`; ordinary idle loop does not wait on an empty port. Serial connect/disconnect itself is initiated by GUI callback. |
| Plot updates | `ui/views/ui_slots.py`, `ui/views/test_control.py`, `ui/widgets.py` | GUI thread; acquisition/monitor samples target 10 Hz, redraw capped at 5 Hz | Reuses three plot curves per view and calls `setData`; no Matplotlib recreation in live path. Work grows with visible point count. |
| CSV logger | `storage/data_utils.py::DataHandler.log_row`, `acquisition/worker.py` | Same worker as monitor/acquisition, per sample; flush about once/s | Append-only row writes; no full-file rewrite. Disk stalls can delay acquisition worker but do not directly block Qt. |
| File Analyze | `ui/views/session_manager.py::_on_analyze_csv`, `acquisition/analysis.py` | UI click dispatches daemon thread; CPU analysis runs via one-process `ProcessPoolExecutor` | Result can take seconds, but does not synchronously hold the GUI thread. Initial SciPy import adds cold-start cost. |
| Report export | `ui/views/dialogs.py`, `ui/widgets.py::_ReportPackageTask` | `QThreadPool` `QRunnable` | Word/PDF generation and file reads run off GUI. |
| Cloud push / analysis | `storage/cloud_push.py::CloudPusher` | Optional daemon worker, configured default push every 5 s and reanalysis every 60 s | Network and CSV work run off GUI; may compete for CPU/GIL while enabled. Not enabled in this simulation audit. |
| UI timers | `isa101_views.py`, `zones.py`, `app/run.py` | Heartbeat 1 s; pulse 500 ms; alarm flash 500 ms when active; process signal wakeup 200 ms; short-lived sequence/characterization wait timers | Heartbeat can synchronously read SCPI if Direct page is selected. Other recurring timers perform UI updates. Alarm flash timer stops when cleared. |

## 2. Main freeze causes found

| Priority | Cause and location | Context | Evidence / impact |
|---|---|---|---|
| P1 | Analyze CPU in `acquisition/analytics.py::Analytics.hampel_filter` | Analysis process, not GUI | Before change, a 10,000-row synthetic Analyze took 4.60 s; cProfile attributed 4.21 s to `analyze_series` and 4.21 s to four Hampel-filter calls (about 3.74 s in NumPy medians). This explains delayed Analyze results, not a blocked Qt thread. |
| P1 risk | Synchronous hardware Connect: `ui/views/hardware_control.py::_on_connect` → `HardwareController.connect_instruments` and `apply_default_safety_protection` | GUI thread | Opens resources, makes sequential `*IDN?` queries, protection writes/queries, hardening, and info queries. Instrument timeout is 5 s per VISA operation. Hardware was not contacted, so real elapsed duration is not measured. A nonresponding instrument can make this callback freeze visibly. |
| P1 risk | Direct-page read in `ui/views/ui_updater.py::_on_heartbeat_tick` | GUI thread, 1 s timer | When Direct is selected and hardware is connected, it calls `hw.read_vi()` synchronously. `read_vi()` takes the instrument lock and queries both load and PSU. A timeout or lock contention can stall the GUI. Not exercised against hardware. |
| P2 | Long visible plot history | GUI thread, redraw 5 Hz | Existing duration cap bounds trend history to 4,000 s (~40,000 samples at 10 Hz). Measured redraw scaling below shows 50k points around 18.5 ms median and 100k around 26.1 ms median, maximum 39.9 ms in this offscreen environment. The normal cap should keep plot work bounded, though redraws may approach a visible frame cost near the cap. |

No evidence of live Matplotlib redraws: the live trend uses PyQtGraph. Matplotlib is used for offline/report/trend rendering paths, which are launched in workers where located.

## 3. Event-loop latency

Qt watchdog: 50 ms timer; recorded actual callback lateness relative to scheduled interval. Offscreen Windows environment, no hardware.

| Scenario | median | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Idle window, 80 ticks | 13.0 ms | 20.0 ms | 23.2 ms | 27.7 ms |
| Simulated telemetry, 10 Hz feed / 5 Hz plot, 80 ticks | 11.8 ms | 19.7 ms | 20.6 ms | 20.7 ms |
| 10,000-row Analyze running in background, 100 ticks | 12.4 ms | 22.0 ms | 33.2 ms | 54.1 ms |

The watchdog values are lateness above the requested 50 ms interval. Analyze had one measured maximum slightly above the 50 ms visible-lag guideline; no large stall occurred during this synthetic run. This does not measure physical Connect/Direct callbacks.

## 4. SCPI and serial latency

- Normal acquisition uses worker context. Discharge readback uses one combined measurement query on the load; general `read_vi()` queries the load and PSU serially. Supported combined queries reduce round trips; unsupported instruments fall back to separate V/I queries.
- VISA instruments have `timeout = 5000` ms. `_meas_vi` retries split-query failures once after 200 ms. Hardware response latency was deliberately not measured because that requires contacting instruments.
- Therefore SCPI timeout behavior delays worker measurements in normal monitor/test flows. It can freeze the GUI in Connect and in Direct-page heartbeat, as described above.
- ESP32 serial is read on its own daemon thread and checks `in_waiting` before `readline()`. A disconnected/stale sensor should not make the GUI wait for `readline`; connect uses serial timeout 1 s and is currently reached within GUI Connect.

## 5. Plot and memory findings

The trend constructors create curve objects once; updates call `setData` on those objects. Plot updates are throttled by 0.2 s checks in both monitor and acquisition GUI slots. Display history is duration-limited in `_trim_trend_buffers`; CSV telemetry remains raw and is not downsampled.

Synthetic PyQtGraph update timing (four calls per size, one active mode; includes `processEvents`, offscreen; indicative, not a hardware/display benchmark):

| Points | Median | Max |
|---:|---:|---:|
| 1,000 | 2.1 ms | 4.1 ms |
| 10,000 | 4.6 ms | 5.7 ms |
| 50,000 | 18.5 ms | 20.4 ms |
| 100,000 | 26.1 ms | 39.9 ms |

An accelerated 1,800-sample GUI-feed run retained one combined-view plot artist, 651 fixed window widgets, and added about 0.32 MB of traced Python allocations. This checks for per-update artist/widget creation, not a literal 2-hour or 6-hour wall-clock soak. Source-level trimming bounds plot buffers at 4,000 seconds. No evidence of duplicate timers/workers accumulating in the single-window lifecycle was found; repeated view-open/close soak was not performed.

## 6. Analyze and profile results

Synthetic Analyze timings:

| Rows | Before optimization | After optimization |
|---:|---:|---:|
| 10,000 | 4.60 s (cProfile run) | 0.140 s (warm wall time); 0.312 s under cProfile |

Cold-import 10,000-row analysis took 2.82 s in a separate run; the first 1,000-row profile took 3.75 s, including about 2.35 s importing SciPy's signal package. These cold timings are not representative of subsequent calls. The warmed 10,000-row cProfile attributed 0.180 s to CSV parsing and 0.118 s to analysis; Hampel filtering totaled 0.063 s.

Direct filter microbenchmark with identical synthetic 100,000-value input: 4.06 s before and 0.149 s after, bit-for-bit equal output. A separate final implementation run with an in-band NaN took 0.114 s; the regression test confirms original NaN propagation plus exact outputs. The change vectorizes local median/MAD calculation while preserving truncated edge windows and MAD-zero fallback.

Other requested profiler scenarios (idle GUI, full Quick Scan sequence, 5-minute/30-minute/2-hour soak and instrument latency) were not run end-to-end in this environment; see remaining limits. The actual GUI timer was measured idle, during simulated telemetry, and during synthetic Analyze.

## 7. Logging performance

Synthetic append-only logger benchmark: 2,000 rows; median 0.011 ms, p95 0.022 ms, p99 0.117 ms, max 1.71 ms per `log_row`; 292 KB output. Worker flush cadence is about once/s. CSV writes are not a significant measured source of UI lag in this benchmark. This did not test slow/OneDrive-synced storage.

## 8. Startup timing

Cold offscreen process timings, from a fresh interpreter: PySide6/PyQtGraph/NumPy imports 990 ms; app/UI imports 264 ms; QApplication construction 18.6 ms; config load 4.5 ms; window construction 733 ms. Total through window construction was about 2.01 s. This isolated timing did not run the production bootstrap sequence. Production `bind_controller()` also calls `_refresh_ports()` synchronously, which asks VISA for resources and serial for COM ports; the real VISA discovery time was not measured to avoid touching hardware.

## 9. Fix applied

| File | Change | Before / after | Risk |
|---|---|---|---|
| `aset_batt/acquisition/analytics.py` | Vectorized Hampel filter, preserving output, NaN, and edge semantics | 100k values: 4.06 s / 0.149 s; 10k Analyze: 4.60 s / 0.140 s warm wall time (0.312 s profiled) | Low; numerical output was bit-for-bit equal for reference and synthetic tests. Does not change C10/C20 basis, DCIR gating, OCV validation, Peukert, HPPC, EKF, safety, or CSV data. |
| `tests/test_analytics_smoothing.py` | Added reference-equivalence regression including edge windows and spikes | 13 focused analytics tests pass | None at runtime. |

No changes were made to the hardware/UI callback paths: the risk is source-confirmed, but safely validating a moved hardware connect sequence requires instrument lifecycle testing unavailable without contacting hardware. No reduced acquisition, safety-polling, or logging rate was applied.

## 10. Regression results and limits

- `.venv/Scripts/python.exe -m pytest tests/test_analytics_smoothing.py -q`: **13 passed**.
- Full-suite attempt stopped at `TestWorkerEcmAndDcirWiring.test_post_process_fits_ecm_and_reports_dcir`: sandbox `PermissionError [WinError 5]` creating the Windows multiprocessing named pipe in `ProcessPoolExecutor`. This is an environment permission failure before analysis runs, not an assertion failure from this change. The broader suite was not completed.
- No physical hardware, real test, or output command was used.
- Synthetic event-loop measurements were headless/offscreen on this workstation; another GPU, display scale, Windows power plan, instrument, disk, and antivirus may differ.

## 11. Explicit answers

1. **Main reason it feels slow:** Analyze had a confirmed CPU bottleneck in repeated Python-loop median/MAD calculations (fixed here). Hardware Connect and Direct readback are separate, source-confirmed ways to produce a true visible freeze because they perform synchronous SCPI work on the GUI thread.
2. **Is the GUI thread blocked?** Not by ordinary acquisition, monitoring, analysis, or report generation in the inspected paths. Yes, potentially during Connect and Direct-page hardware polling. This was not timed against physical instruments.
3. **Is Matplotlib significant?** Not for live plots: live charts are PyQtGraph with reused artists. Offline report/analytics Matplotlib generation is not in the live redraw path.
4. **Do SCPI/serial timeouts freeze the GUI or delay measurements?** In worker paths they delay measurements. Connect and Direct SCPI calls happen on the GUI thread and can freeze it. The serial read loop is a worker thread, guarded by `in_waiting` before `readline`.
5. **Does CSV logging contribute significantly?** No in the synthetic benchmark; append cost was 0.011 ms median. Slow storage remains unmeasured.
6. **Why does Analyze feel slow?** It parses the CSV and runs filtering/engineering analysis; four Hampel passes dominated the pre-fix CPU profile. First-time SciPy import also cost about 2.35 s. The confirmed filtering bottleneck is fixed; analysis remains asynchronous.
7. **Does performance worsen as runtime grows?** Source bounds displayed history to 4,000 seconds and reuses artists, so the most obvious unbounded plot cost is controlled. The 1,800-sample accelerated run showed no duplicated artists/widgets; a true multi-hour soak was not performed.
8. **Are duplicate signals/timers/workers accumulating?** No accumulation was found in inspected single-window setup; graph line count stayed one and widget count remained fixed in the simulation. Repeated view lifecycle soak was not run.
9. **Largest improvement?** Vectorizing Hampel filtering: 100k samples 4.06 s to 0.149 s; synthetic 10k Analyze 4.60 s to 0.140 s warm wall time (0.312 s under profiler).
10. **Responsive enough for a multi-hour test?** Worker separation, 5 Hz plot throttling, bounded display history, and low CSV append costs support that architecture. I cannot certify multi-hour readiness because real USB/SCPI behavior and a wall-clock soak were not tested; GUI-thread Connect/Direct SCPI paths should be treated as known responsiveness risks.

## 12. Remaining risks / next validation

The highest-value follow-up is moving Connect and Direct readback SCPI into an existing worker pattern, preserving instrument protection setup order and E-STOP priority; then validate with simulated timeout injection and a bench disconnected-device check before a real test. Run the full regression suite in an environment that permits Windows multiprocessing pipes. A multi-hour hardware-free accelerated soak should separately track process RSS, timer lateness, queue depth, and CSV row integrity.

## 13. Focused Connect and Direct responsiveness correction (2026-09-20)

The two remaining GUI-thread SCPI paths identified above were moved to one window-owned `QThreadPool` with `maxThreadCount=1`. This is one worker lane, not a worker per poll. `HardwareTask` returns a structured success/error result through a Qt signal; widget changes occur in `_on_hardware_task_done` on the GUI thread. At most one Direct poll may be active; requests arriving while it is active are skipped. Direct timer cadence remains the existing 1 second and displayed values are marked stale after 3 seconds without a successful read. ESP32 temperature keeps the controller's separate 10-second source freshness policy.

### Thread traces

**Connect before:** GUI `btn_connect.clicked` → `_on_connect` → `connect_instruments` → lazy `ResourceManager` / `open_resource` / `*IDN?` → safe output state → `apply_default_safety_protection` and ID/info queries → optional `connect_esp32` / serial open. These calls could synchronously wait on GUI. VISA resource timeout remains **5,000 ms**; `_meas_vi` retry delay remains 200 ms.

**Connect after:** GUI validates selections and state, loads software calibration, disables duplicate Connect, sets `Connecting…`, and submits one task. Worker performs VISA and optional ESP32 open/setup. It emits a structured result; GUI slot updates LEDs, status, errors, config, and live readback. ResourceManager creation and port discovery (`list_resources`, COM enumeration) use the same worker. A setup exception attempts ESP32 and instrument disconnect before reporting failure. Partial PSU/load open and IDN failures close opened VISA resources in `HardwareController`.

**Direct before:** 1-second `_tick` → `_on_heartbeat_tick` → synchronous `hw.read_vi()` → PEL and PSU SCPI reads/retries → display and plot widgets, all from GUI.

**Direct after:** 1-second `_tick` → stale-age check and `_request_direct_poll` guard → worker performs `hw.read_vi()` and reads temperature freshness → result signal → GUI updates readback and plot. No direct timer path queries an instrument. There is at most one poll in flight; no backlog is created. The worker lane is shared with Connect/disconnect and has one thread.

### Ownership and safety notes

The PSW and PEL `Resource` objects remain owned by `HardwareController`. Direct reads, live monitor/acquisition, sequence workers, Connect/setup, disconnect, and controller E-STOP SCPI all use the existing `inst_lock`; sequence and monitor work were not moved. The app-owned lane serializes GUI-initiated connection/disconnection/poll work; it does not replace the shared instrument lock. Connect refuses duplicate/overlapping initiation during discovery, connection, disconnect, close, sequence, or characterization activity. Normal poll work cannot form a worker queue because the in-flight guard skips repeat ticks.

E-STOP logic and its instrument shutdown were not weakened or queued behind repeated poll jobs. The existing E-STOP handler first attempts the independent ESP32 SSR OFF command, then acquisition emergency override and `controller._trigger_safety`. These still use synchronous safety code and may contend with the one currently active instrument transaction on `inst_lock`; with the driver timeout unchanged, a real unresponsive instrument can delay SCPI safe-state confirmation. The mock test proves the handler remains callable, not real-world emergency cutoff timing. Hardware verification is still required for that timing.

Disconnect now stops live readback/cloud push and runs output-safe disconnect calls in the worker. Close stops UI timers and requests sequence/test worker stop before waiting for the current hardware operation; controller shutdown runs on the worker before final widget teardown, so its signal receiver is not destroyed early. GUI shutdown does not cancel an active VISA operation; it waits for that operation's existing timeout behavior so safe-state cleanup can run.

### Mocked responsiveness measurements

Offscreen Windows run on this workstation. A 10 ms Qt timer measured callback lateness during fake blocking hardware work. Numbers are lateness above the timer interval, not VISA performance. Percentiles are empirical per run; very short 20 ms polling has few timer samples.

| Operation | Simulated wait | Median lateness | p95 lateness | Max lateness |
|---|---:|---:|---:|---:|
| Idle window | — | 0.0 ms | 1.3 ms | 2.8 ms |
| Connect | 100 ms | 7.5 ms | 10.7 ms | 12.3 ms |
| Connect | 500 ms | 5.9 ms | 9.1 ms | 17.8 ms |
| Connect | 1,500 ms | 6.0 ms | 10.9 ms | 17.6 ms |
| Direct poll | 20 ms | 20.3 ms* | 20.3 ms* | 20.3 ms* |
| Direct poll | 200 ms | 5.7 ms | 9.6 ms | 17.6 ms |
| Direct poll | 1,000 ms | 8.8 ms | 11.0 ms | 23.6 ms |

`*` Only one 10 ms timer sample was collected in this very short polling run, so the median/p95 are not stable estimates; the max is the observed single sample. Connect callback dispatch duration was asserted under 50 ms while a worker continued for 100 ms; test passed (no physical device involved).

### Slow-poll stress and regression results

With poll requests every 200 ms and fake `read_vi()` duration 800 ms: **9 requests, 2 completed, 7 skipped, maximum active reads 1**. The latest successful sample reached the GUI. Repeated requests did not create a queue or additional workers. Simulated reads were confirmed to execute on a thread other than the Qt test thread.

Focused command: `.venv/Scripts/python.exe -m pytest tests/test_ui_hardware_responsiveness.py tests/test_direct_mode_graph_feed.py tests/test_hardware_connect_flow.py tests/test_analytics_smoothing.py -q -s` — **37 passed**. Includes mock success, connection exception cleanup, instrument partial-open/IDN resource cleanup, asynchronous disconnect/close, E-STOP handler availability, timer polling coalescing, graph update, and existing analytics regression. Existing `test_hardware_connect_flow.py` fixture was adjusted for lazy ResourceManager construction by injecting its mock manager directly.

This is mock-only evidence. It does not simulate all VISA driver behavior, all specific PSW/PEL timeout combinations, serial-open failures at the OS driver level, UI interaction under an actual USB stall, or true long-duration operation. No physical battery test or instrument output command was run. Real VISA latency and E-STOP timing still require a later controlled bench check.

**Are all known software-side GUI freeze risks removed from Connect and Direct polling? YES, for those two paths as source-reviewed and under mocks.** This does not certify all GUI hardware actions: Direct PSU/Load ON/OFF buttons still call their write methods synchronously, and E-STOP's existing synchronous controller safety path may wait on one active VISA transaction. Real instrument responsiveness remains unverified.
