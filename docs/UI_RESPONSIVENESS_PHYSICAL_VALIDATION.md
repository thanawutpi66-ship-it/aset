# ASET UI responsiveness physical validation procedure

**Status:** Procedure only. No physical measurements are recorded in this file.

**Scope:** GW Instek PSW 80-40.5, GW Instek PEL-3111, ESP32 temperature subsystem, SSR cutoff, GUI Connect/Direct polling/disconnect/close.

**Restrictions:** A trained operator must be at the bench. Do not connect a battery for UI responsiveness checks. Do not enable PSU or electronic-load outputs except in UI-05, and then only with an approved, isolated, low-voltage, current-limited dummy fixture. Do not unplug USB/VISA/serial cables to induce an error while outputs may be enabled. Stop if instrument state, wiring, or cutoff behavior is uncertain. The software tests are mock evidence only; this procedure has not been run.

## Before testing

1. Confirm the bench is approved for unattended output-off instrument discovery and connection. Remove the battery from the circuit. Keep PSW output OFF and PEL input OFF. Record instrument model, serial, firmware, VISA resources, ESP32 firmware, ASET build/commit, Windows version, and test operator/observer.
2. Confirm independent bench emergency isolation and a safe way to observe SSR state. For UI-05 prefer an oscilloscope/logic analyzer on the SSR control signal plus an isolated indicator or approved dummy fixture; software timestamps alone do not establish physical cutoff time.
3. Start with an empty copy of [the physical evidence CSV template](ui_responsiveness_physical_validation_template.csv). Use a unique `run_id` for each trial. Leave physical measurements blank until measured.
4. Use a separate GUI responsiveness logger/watchdog at a fixed documented interval (suggested 10 ms). Keep the logger independent of ASET's worker pool and record its sampling interval, clock source, and timestamp resolution. Record application exceptions and Windows VISA/COM events.
5. Verify the instrument front panels show outputs OFF before starting. Do not press Direct PSU/Load ON during UI-01/02/03.

## UI-01 — Real Connect responsiveness

1. Start ASET and the independent watchdog logger. Confirm PSU/Load output/input are OFF and no test is running.
2. Note the Connect click timestamp, click **Connect** once, and record when the UI reports Connected or Failed.
3. While it connects, interact only with a harmless display control (for example, switch a non-hardware tab or resize the window). Do not click output controls.
4. Record total Connect duration, watchdog maximum lateness, whether paint/input stopped, connection result, displayed PSU/PEL identities, and exceptions. Confirm any failed initialization returns to a usable disconnected state and opened resources close.
5. Confirm both hardware outputs remain OFF after successful initialization.

## UI-02 — Direct read responsiveness (at least 60 seconds)

1. Connect with outputs OFF. Open Direct/Manual; do not switch on PSU or load.
2. Start a 60-second capture. Record timestamp for each successful measurement and each timer request if diagnostics are enabled.
3. Record read duration, sample interval, skipped/busy count, maximum in-flight reads, stale events, watchdog maximum lateness, and any exceptions.
4. Compute min/mean/max sample interval from successful-read timestamps; report read count and skipped count separately. The normal software timer cadence is 1 second; do not increase it for this check.
5. Verify a successful new sample clears a previous stale indication. Confirm no stale value is described as current.

## UI-03 — Induced slow VISA response (safe method only)

1. Do not induce a real instrument timeout by disconnecting cabling or changing a battery/load condition.
2. Use only an approved local diagnostic build/wrapper that adds a software delay before the Direct `read_vi()` call and does not issue extra instrument commands. Keep outputs OFF. If such a wrapper has not been reviewed, skip this test and record “not run — no approved safe delay hook.”
3. Apply a delay longer than 1 second, then longer than 3 seconds. Observe UI responsiveness, single-flight counts, skipped polls, and stale status.
4. Remove the delay and verify the next successful sample clears stale status. Confirm only one instrument read runs at a time and no delayed queue drains afterward.

## UI-04 — ESP32 serial responsiveness

1. Keep PSU/PEL outputs OFF. Connect the ESP32 and record its firmware and selected COM port.
2. Capture parsed temperature/watchdog arrivals for at least 60 seconds using existing timestamps/logs or a reviewed diagnostic observer. Do not add competing reads from the same serial port.
3. Record normal arrival interval, longest parser-arrival interval, serial errors, GUI watchdog lateness, and whether temperature freshness status matches arrival age.
4. Test a normal UI Disconnect/reconnect only while outputs remain OFF. Do not pull the cable to simulate failure. Record whether the old port/thread is released and the new connection receives fresh telemetry.

## UI-05 — Physical E-STOP response (controlled safety test)

1. Obtain bench safety approval and have an observer at the independent isolation switch. Remove all battery chemistry from the circuit.
2. Use the approved isolated low-voltage, current-limited dummy fixture to make the SSR transition observable. Use manufacturer-approved instrument settings; outputs must remain at the approved safe test level.
3. Prefer independent logic analyzer/oscilloscope capture of the E-STOP trigger input/UI event and SSR output transition. Synchronize clocks or use a shared trigger. Record software timestamps separately from physical signal timing.
4. Trigger E-STOP once, confirm SSR opens and instrument output-off state independently, and preserve traces. Repeat at least five times only if the first trial is safe and consistent.
5. Stop immediately on any missed/late cutoff, inconsistent state, or uncertainty. Use independent isolation; do not continue repeated trials to diagnose a failure.
6. Record trigger timestamp, SSR/output transition timestamp, physical response time, GUI event timestamp, UI watchdog maximum lateness, trial number, measurement instrument/sample rate, and evidence filename. Do not infer physical cutoff from Python timestamps alone.

## UI-06 — Disconnect and close cleanup

1. With outputs OFF, click Disconnect and record duration, final front-panel state, UI state, and any errors.
2. Reconnect and verify both instrument identities and ESP32 telemetry refresh. Confirm there is no stale device/session ownership.
3. Close ASET normally. Confirm safe output-off state, VISA release, serial release, and no background worker/error remains.
4. Reopen ASET and reconnect once more. Record result and any OS-level resource/port errors.

## Stop / escalation criteria

Stop the procedure and use the independent bench isolation if any output state is unexpected, the SSR does not open, instrument communication is uncertain, the UI reports Connected while a required instrument is not responding, or exceptions leave the connection state ambiguous. Do not continue physical testing until the cause is reviewed by the responsible hardware/safety owner.
