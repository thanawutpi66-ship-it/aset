# UI responsiveness software audit

Audit date: 2026-09-20. Scope is the Connect/Direct responsiveness correction and preparation for controlled physical validation. All timing below is simulated/offscreen software evidence. No real instrument, battery, or physical E-STOP validation has been performed.

## 1. Responsiveness architecture audit

The GUI owns one window-scoped `QThreadPool` with a maximum of one task at a time. `HardwareTask` runs the blocking callable and forwards a structured result through `sig_hw_task_done`; `_on_hardware_task_done` changes Qt widgets on the GUI thread. Existing monitor, acquisition, sequence, and characterization workers remain in their established contexts. The existing `HardwareController.inst_lock` serializes operations on PSW/PEL sessions across all those callers.

Connect initiates only state validation, widget state, software calibration copy, and task dispatch. VISA resource manager creation/discovery, resource open, IDN/setup/protection, optional serial open, and cleanup are in the worker. Disconnect and close-time controller shutdown are worker tasks. The worker lane is bounded to one thread; the Direct in-flight guard prevents poll requests building a pool backlog. A completed or failed worker emits one result and is removed from the active-task map.

Close stops timers and requests sequence/acquisition cancellation before waiting for currently running hardware work. It ignores the close event until that task finishes, then runs controller shutdown on the worker and closes the window only after receiving its result. The window remains alive to receive queued task completion signals. This is safe for Qt widget lifetime, but it waits for the active call's unchanged VISA timeout before teardown. Close does not forcibly terminate a blocking driver call.

## 2. GUI-thread blocking audit

| File / function | Potential operation | Context | Risk | Action |
|---|---|---|---|---|
| `ui/views/hardware_control.py::_on_connect` | Reads combo values, copies calibration, submits worker | GUI | Low; no I/O after dispatch | None for this path |
| `ui/views/hardware_control.py::_refresh_ports` | Dispatches VISA/COM discovery | GUI dispatch; discovery worker | Low after dispatch | None |
| `ui/views/hardware_control.py::_on_disconnect` | Stops live-readback flag/cloud service then dispatches disconnect | GUI dispatch; output-safe disconnect worker | Low; stop calls are flag/event operations | None identified |
| `ui/views/hardware_control.py::_request_direct_poll` | Dispatches one worker read | GUI dispatch | Low; duplicate skipped | None |
| `ui/views/hardware_control.py::_psu_manual`, `_load_manual` | `set_psu` / `set_load`, SCPI writes/queries under instrument lock | GUI | Can wait for VISA lock/I/O timeout and freeze input | Separate follow-up; not part of Connect/poll correction |
| `ui/views/hardware_control.py::_on_check_psu_trip`, `_on_clear_psu_trip` | SCPI trip query/clear/query | GUI | Can wait for VISA timeout | Separate follow-up |
| `ui/views/hardware_control.py::_on_ssr_manual_on`, `_on_ssr_manual_off` | ESP32 serial write/flush | GUI | Usually short; a blocked serial write could stall UI | Separate follow-up; SSR OFF remains directly callable |
| `ui/views/ui_updater.py::_on_heartbeat_tick` | `feed_watchdog()` serial write/flush; status update | GUI, every 1 s | Small write but can block on serial driver/write lock | Consider a dedicated prioritized serial writer if measured stalls occur; preserve watchdog cadence |
| `ui/views/hardware_control.py::_on_estop` | SSR OFF serial write, `AcquisitionWorker.emergency_stop()` I/O mutex/backend override, controller emergency load/PSU writes | GUI | SSR OFF is attempted first; later SCPI can wait behind the one active transaction, up to driver timeouts | No queue of Direct polls exists. Bench must measure real cutoff; consider a non-GUI emergency dispatch only if a safe priority lane can preserve immediate SSR command |
| `app/app_bootstrapper.py::_wire_runtime` simulation branch | Mock port discovery/connect | GUI | Mock-only in simulation mode, not physical I/O | No instrument risk; could dispatch too for strict thread uniformity |
| `app/app_bootstrapper.py::_emergency_hw_off`, `cleanup` | Controller hardware shutdown | Main thread on signal/exit paths | Blocking at process shutdown; signal path cuts SSR before VISA writes | Preserve safety order; outside normal interactive Connect/Direct flow |

No long sleeps were found in the corrected Connect/Direct callbacks. The real controller's VISA timeout remains 5,000 ms; split measurement retry remains 200 ms. No timeout was lowered to improve UI feel.

## 3. Worker lifecycle audit

- Worker count: one window-owned pool thread; no per-poll threads.
- Connect duplicate: guarded by discovery/connect/disconnect/close/test ownership state; buttons are disabled while connection is active.
- Failure: task wrapper catches exceptions and emits one failure result. Connect callable attempts ESP32 and instrument cleanup; driver partial-open/IDN failure closes local VISA resources.
- Disconnect: returns to GUI immediately; output-safe disconnect work is on worker.
- Close: timers stop; close waits for active worker result; controller shutdown runs before final window teardown.
- Signals: result is marshalled to a QObject slot; window remains alive until task map is empty and async shutdown completes.
- GUI mutation: worker only accesses hardware/config values and returns results; widget updates occur in `_on_hardware_task_done`.
- VISA thread safety: PSW/PEL resource methods share `inst_lock`. This does not guarantee that arbitrary external integrations also use the lock; app runtime callers reviewed here do.

## 4. Direct polling and stale data

Normal Direct request period is 1 second from the existing heartbeat timer. For each request `n`, if `_direct_poll_inflight == True`, the request increments requested/skipped counters and does not call `QThreadPool.start()`. Otherwise the flag is set before task submission and cleared in the GUI completion slot. Therefore the Direct lane has `active_direct_reads ∈ {0,1}` and repeated timer ticks cannot accumulate a poll queue. Shared pool maximum active task count is also 1.

`direct_poll_diagnostics()` exposes requested, executed, skipped-busy, in-flight, last successful monotonic timestamp, last-success age, pending age when no sample has ever succeeded, stale threshold, stale state, and last sample. Direct page status reports pending, current, ESP32-temperature-stale, readback-stale with last values retained, or unavailable if the first read has not succeeded after 3 seconds. Heartbeat status refresh preserves Direct status. The read result timestamp is sampled after `read_vi` returns; it is a software receive/completion timestamp, not the instruments' internal acquisition timestamp. Stale status only affects display; it does not issue a hardware command. A later successful poll clears stale state.

## 5. E-STOP safety path audit

`_on_estop` remains directly available and does not submit behind the polling pool. It commands independent SSR OFF first, then invokes the existing acquisition override and controller safety shutdown. The controller also repeats SSR OFF before instrument writes. Direct polls are single-flight, so E-STOP cannot be delayed by a backlog of Direct reads; it can contend with one active query. Failure paths log/emit critical SSR/instrument errors. The UI message says the E-STOP command was issued after synchronous command attempts; it does not constitute proof that the physical relay opened. The E-STOP mock test verifies the handler and SSR-OFF attempt while a slow Direct read is active, not physical timing.

Disconnect and close call output-safe instrument cleanup; disconnect sends SSR OFF before closing the ESP32 link. No code in these paths commands an output ON. The existing sequence/acquisition cancellation flags are set before close-time shutdown. A race-free physical safe state is still a bench-validation question; use independent isolation if any output is uncertain.

## 6. Tests and software-only measurements

Latest completed focused command:

```text
.venv/Scripts/python.exe -m pytest tests/test_ui_hardware_responsiveness.py tests/test_direct_mode_graph_feed.py tests/test_hardware_driver_coverage.py tests/test_hardware_connect_flow.py tests/test_analytics_smoothing.py -q
53 passed
```

The focused suite covers 100/500/1,500 ms Connect delays, 20/200/1,000 ms Direct-read delays, 4.5-second first-read staleness and recovery, 200 ms request cadence vs 800 ms read delay (9 requests, 2 completed, 7 skipped, one max active read), serial-connect failure, VISA setup failure cleanup, repeated Connect/Disconnect, close during slow I/O, E-STOP during slow Direct I/O, existing driver resource cleanup, Direct graph behavior, and analytics regression. The idle watchdog and delayed Connect/Direct scenarios have shown max lateness under about 25 ms in this offscreen environment. No timing is a real VISA measurement.

Callback dispatch is asserted under 50 ms on the mocked 100 ms Connect path. Qt worker reads/connect are asserted to run off the test/GUI thread.

## 7. Full-suite failure triage

One completed full-suite snapshot before the final stale-display refinement and lazy ResourceManager setter reported **51 failed, 791 passed, 10 skipped, 10 errors**. Ten driver-coverage setup errors were caused by tests assigning `controller.rm` after it became lazy. A compatibility setter was added, and the exact driver/connect tests now pass in the focused run. A later full-suite capture on the current tree stalled at approximately 79% without producing a pytest summary; it was interrupted after several minutes. Thus a final complete full-suite count on the current tree is unavailable.

Observed previous-run failures, classified relative to this responsiveness change:

| Test(s) / group | Failure observed | Classification | Related? / recommended action |
|---|---|---|---|
| `test_acquisition.py::TestWorkerEcmAndDcirWiring::test_post_process_fits_ecm_and_reports_dcir`; worker/analysis tests that initialize `ProcessPoolExecutor` | Windows `PermissionError: [WinError 5]` creating multiprocessing named pipes | WINDOWS_ENVIRONMENT_FAILURE | No; rerun in environment permitting named pipes or configure a supported single-process analysis test fixture |
| `tests/test_hardware_driver_coverage.py` (10 fixture setup cases) | `AttributeError: rm has no setter` from existing fixture injection | RESPONSIVENESS_REGRESSION (test seam compatibility) | Initially related to lazy ResourceManager API compatibility; fixed with setter and focused driver suite now passes |
| `tests/test_load_trip_ui.py` (15 cases) | Test expects `_on_check_load_trip`, `_on_clear_load_trip`, `get_load_protection_tripped`, `clear_load_protection`, `_seq_check_load_trip`; those methods are absent from current implementation | OBSOLETE_TEST_EXPECTATION | No; reconcile/remove stale tests or separately implement load-trip feature after owner review |
| `tests/test_acquisition_worker.py::TestHappyPath::test_discharge_loop_runs_to_cutoff_and_emits_telemetry` | `_SlowStepBackend` lacks `temperature_measurement()` called by worker | PREEXISTING_UNRELATED_FAILURE | No; update test fake to conform to current backend contract or restore optional protocol handling |
| `tests/test_code_review_round2_fixes.py` sequence end-session cases; `test_worker_ecm_feedback...`; `test_coulomb_efficiency.py`; `test_evidence_gated_grading.py`; `test_i_standby_and_product_calibration.py`; `test_quickscan_coordinated_corrections.py` | Assertions/analysis outcomes in acquisition, calculations, sequencing | PREEXISTING_UNRELATED_FAILURE | No; triage against their respective recent domain changes; do not alter UI worker design |
| `tests/test_realtime_accuracy_fixes.py::TestMonitorLoopTiming` (2); `tests/test_worker_self_correcting_pacing.py` | Timing/pacing mock expectations | OBSOLETE_TEST_EXPECTATION | No; reconcile expected pacing contract with current monitor implementation |
| `tests/test_recharge_after_test.py` cases | Recharge config/gating/execution expectations | PREEXISTING_UNRELATED_FAILURE | No; separate recharge feature review |
| `tests/test_retheme_and_crosshair.py::TestRetheme::test_metric_cards_do_not_go_stale_when_idle_across_a_toggle` | Retheme/card state assertion | PREEXISTING_UNRELATED_FAILURE | No; separate UI-ret theme regression review |
| `tests/test_rin_calibrated_flag.py` (2); `tests/test_safety_shutdown_paths.py::test_char_safety_sustained_stale_aborts`; `tests/test_temp_stale_escalation.py` (2) | Analysis/safety/temperature behavior expectations | PREEXISTING_UNRELATED_FAILURE | No direct change in this task; review with their corresponding dirty acquisition/hardware changes before physical test signoff |
| `tests/test_standards_accuracy_fixes.py` (multiple); `tests/test_quickscan_coordinated_corrections.py` | Standards/calculation assertions | PREEXISTING_UNRELATED_FAILURE | No; unrelated calculation scope, review separately |

The classifications above refer to the last completed full-suite output available. The current-tree full-suite attempt has no completed summary, so additional current failures cannot be conclusively enumerated.

## 8. Controlled physical procedure and template

Follow [UI_RESPONSIVENESS_PHYSICAL_VALIDATION.md](UI_RESPONSIVENESS_PHYSICAL_VALIDATION.md) with the blank [CSV evidence template](ui_responsiveness_physical_validation_template.csv). It covers UI-01 through UI-06, output-off requirements, safe software delay injection, serial observations, controlled low-voltage E-STOP trials, independent physical cutoff measurement, and disconnect/close cleanup. The procedure is not evidence that any test has been run.

## 9. Verdict

**UI RESPONSIVENESS SOFTWARE READY FOR PHYSICAL VALIDATION** for the corrected Connect and Direct polling paths, with the separate synchronous GUI hardware controls in section 2 kept in scope for observation and follow-up. The full suite is not clean and its current-tree run did not complete. This verdict is not a hardware validation result. Real VISA behavior, serial timing, and physical E-STOP response remain unmeasured.
