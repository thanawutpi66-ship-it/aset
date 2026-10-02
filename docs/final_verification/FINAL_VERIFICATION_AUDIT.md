# Final Verification Audit

**Audit date:** 2026-10-02
**Repository:** `thanawutpi66-ship-it/aset`
**Audited source:** `c6ec121f6daea671ba786037882bf4ca8cfe912f`
**Branch:** `prep/final-verification-2026-10-02`
**Environment:** Windows 10 (10.0.26200), Python 3.11.9, pytest 9.1.1, repository `.venv`; local `config.json` says `simulation_mode=true`. No instrument API or physical run was invoked.

## Executive finding

The repository has substantial simulation coverage, source-level protections, timestamped session logging, sidecar metadata, C10 qualification logic, and a validation-campaign helper. It does not currently contain a qualifying Final physical campaign bundle. Several metrics have calculation helpers, but profile denominators, evidence qualification, or method choices remain incomplete. No requirement is called Final PASS in this audit.

The current checkout has eight CSV files in `sessions/` (five with sidecars; three legacy CSVs without them). Sidecars identify QuickScan/manual-discharge/development or incomplete sessions; none is marked as an enabled complete Final campaign. These are useful for software/logging diagnostics, not Final physical evidence. Repository image assets are logos/UI resources and `tmp/` contains rendered document-review pages; neither is physical campaign-photo evidence. No campaign videos were found. See `EXISTING_EVIDENCE_INVENTORY.md`.

## Defined workflows and run lifecycle

The GUI offers IEC capacity, Quick Scan, HPPC, and Cycle Life sequence paths. Source has an explicit `c10-reference-v1` validation preset and forces charge for this preset (`validation_force_charge`) while routine runs retain the ordinary skip-charge/SOC decision. HPPC has `hppc-map-v1`; Quick Scan and Cycle Life are screening/development paths unless a protocol authority explicitly includes them in the Final profile denominator. No versioned Final profile registry supplies a closed denominator, total replicate counts, or a helper calculating completed-defined-profiles / total-defined-Final-profiles.

Sequence threads have shared safe-off behavior in `BaseSequenceMixin`, lifecycle outcome metadata, and cancellation/safety handling. Tests exercise mock sequence-thread targets and safety closures. Automated test coverage cannot establish physical profile completion or physical fault-to-safe success.

## Measurement, timebase, and storage

New `DataHandler` sessions use schema 2.3 with `Timestamp`, `Timestamp_ISO`, `Elapsed_s`, `Voltage_V`, `Current_A`, `Temperature_C`, `Capacity_Ah`, `Mode`, `Session_ID`, `Test_Type`, `Phase`, `Step_Index`, source, quality, temperature-status/age/source fields. Not every CSV on disk has the current schema: legacy examples omit `Timestamp_ISO`, quality, phase, and temperature freshness fields.

`AcquisitionWorker` paces against a configured target period and records achieved timing; sequence paths have separate phase pacing and near-cutoff behavior. DataHandler can throttle redundant CSV rows, so CSV row rate is not necessarily acquisition rate. Worker/SCPI loops and the stored CSV represent different evidence layers. The measurement timestamp basis in sidecar is host read-return; voltage and current can be sequential instrument reads. The physical rig’s USB/VISA response is not provable from unit tests.

`analyze_sampling.py` uses increasing `Elapsed_s` for logged sample cadence and explicitly labels it as logging cadence. The minimum is 3 Hz; 10 Hz remains a design target. A result between those values meets the stated minimum while missing the design target. Do not reinterpret the historical near-cutoff median interval and derived-rate pair without freezing a single metric definition for the Final report.

The offline checker was exercised on the eight local `sessions/*.csv` files (read-only). Examples of its logged-row mean-interval rate are 5.993 Hz in the 2026-09-03 QuickScan `MINI_PULSE`, 4.323 Hz in its `RELAX`, 4.387 Hz in the 2026-09-02 QuickScan `MINI_PULSE`, and 3.970 Hz for the legacy unlabeled HPPC file. The 2026-09-22 aborted 99-row discharge session reports 9.144 Hz. These are diagnostic logging-cadence calculations from historical/development or incomplete records, not acquisition-rate or Final evidence. Several phases are deliberately slow (OCV/rest/main-discharge); a whole-session average cannot be used to certify a high-rate phase.

## Pulse DCIR and repeatability

`identify_dcir()` receives elapsed sample times, pairs a current edge with its first post-edge sample, and rejects pairs where `dt <= 0` or `dt > MAX_STEP_EDGE_LATENCY_S` (0.5 s). It uses a previous-sample voltage baseline and reports a median across accepted within-record steps. This actual timestamp gate exists in batch/offline analysis; HPPC and Quick Scan capture paths have their own timing logic and must be traced against final phase labels and raw rows.

`validation_campaign.replicate_statistics()` uses `statistics.stdev` (`ddof=1`) correctly for its supplied values. It does not define how pulse values become one representative DCIR per independent physical run. The legacy `scripts/bench_check.py` uses `statistics.pstdev` and a different 10% message; it is a hardware-control script and is prohibited from this audit. It is not Req.6 Final evidence.

**NEEDS METHOD DECISION:** Final run-level Pulse-DCIR aggregation rule (mean or median of valid pulses, weighting, minimum valid pulses per run, and handling of temperature-normalized results). Until frozen, only within-run pulse diagnostics may be calculated; between-run CV must not be reported as Final.

## Capacity, SoC, grades, safety, and configuration

Issue #1’s forced-charge branch is present for `c10-reference-v1`; incomplete/aborted capacity results are not to be promoted. Issue #2 centralizes `en50342_capacity_conditions()` in `aset_batt/ui/sequences/base.py`; IEC pre-test and post-test pass temperature. YTZ6V’s profile records C10 = 5.0 Ah, 10 h, 0.500 A, and 10.50 V pack cutoff basis. Source configuration is not a measured capacity campaign.

`validation_campaign.reference_soc_from_capacity()` and `ekf_metrics()` can compare estimator output to an independent capacity ruler and calculate convergence/sustained error. No qualifying Final C10 reference exists in the local evidence, so Req.8 is not numerically established.

Analysis has gradeability/evidence gating and records measured-vs-fallback provenance. A Final grade audit still needs the exact issued analysis result linked to a completed traceable run; the session CSV sidecar alone does not guarantee that.

OTP UI writes `system.safety_limits.max_temperature`. `profile_from_config()` maps that configured value to `BatteryProfile.otp_crit`; the UI acquisition-profile builder uses this helper, and `AcquisitionWorker` uses `otp_crit` both for its safety check and for the `otp_critical_c` sidecar field. AutoController and sequence checks also read the configured safety limit. The GUI spin box permits −40 to 150 °C, so its selectable range is broader than Req.11, but the requested 45–60 °C values can be saved and flow into the worker. The new hardware-free regression test exercises save → reload → acquisition profile → worker trip threshold and sidecar field at 45, 50, 55, and 60 °C. This proves software configuration propagation, not physical thermal response; no production fix was needed.

Fault shutdown tests verify simulated command paths, idempotent safe-off and callbacks. They do not prove real SSR opening, breaker behavior, PSU/load response, or E-STOP end-to-end timing. Physical verification requires a safe, approved dummy/low-energy injection plan and an operator-controlled setup.

## Evidence and tooling

Historical values listed by the project owner remain development evidence only. No fitted R0 is relabelled as Final Pulse-DCIR; no UVP trip is relabelled as C10 cutoff; no OTP trip proves a full configurable range; and no E-STOP observation is relabelled as SSR switching time.

The `scripts/verification/` CLIs added in this branch are offline and read-only with respect to input evidence. They do not create default sample data or automatically issue Final PASS. Each accepts `--help`; applicable tools support JSON output. `build_evidence_manifest.py` inventories only specified repository paths. Tests use temporary files and synthetic rows strictly as software test fixtures, not experimental evidence.

## Test status

Full `QT_QPA_PLATFORM=offscreen .venv\Scripts\python.exe -m pytest -q` was started against the baseline source before the tooling tests were added. Progress showed three failures by 58% before the long-running process was interrupted to recover the session; pytest did not emit its summary, so exact node IDs/tracebacks were not captured. The likely modules inferred from progress percentages are not treated as confirmed failures. Separately rerun related modules `test_coulomb_efficiency.py`, `test_meas_vi_and_psu_control.py`, and `test_pel3111_range_autoset.py`: 39 passed, 4 deprecation warnings. New tooling and validation-campaign tests: 16 passed. No failure was edited away.

## Final campaign readiness

**NOT READY FOR FINAL PHYSICAL CAMPAIGN.** The missing campaign evidence is expected, but Req.1 denominator/protocol scope and Req.6 run aggregation method need decisions; Req.2/4 require a strict evidence bundle gate; Req.3 cadence basis needs one frozen reporting definition; Req.8 requires a qualifying independent C10 reference; and Req.10/12 require physical verification. Req.11's software setting path is covered in simulation, while proving thermal response still requires the protocol-defined physical evidence. No physical experiment was run.
