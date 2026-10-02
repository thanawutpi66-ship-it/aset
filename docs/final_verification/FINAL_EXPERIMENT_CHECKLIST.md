# Final Experiment Checklist

Check each item and attach evidence references. Never proceed on an unverified safety item.

## A. Before powering anything

- [ ] Approved protocol/revision, campaign ID, specimen ID, operator and run index are frozen.
- [ ] Define acceptance formulas, fault set, denominators, exclusions and stop conditions before seeing results.
- [ ] Confirm independent reviewer approval and raw-data backup path.
- [ ] Confirm battery chemistry, product, serial/label, condition and permitted limits.
- [ ] Confirm software revision and `simulation_mode` setting through the reviewed UI/config procedure.
- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Inspect battery for swelling, leakage, damage, polarity and secure restraints.

## B. Instrument connection

- [ ] Record PSW/PEL/ESP32/sensor identity, firmware, VISA addresses and calibration references.
- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Connect instruments only after bench safety review; verify instrument outputs disabled.
- [ ] Verify VISA identity queries and ranges using approved no-load procedure.

## C. Wiring inspection

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Inspect polarity, conductor gauge, insulation, strain relief and fuse/breaker location.
- [ ] Independently verify no short, reversed lead or unintended ground path.

## D. Kelvin connection

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Photograph and inspect Force/Sense separation at battery terminals.
- [ ] Record lead/contact configuration and calibration snapshot.

## E. Temperature sensor placement

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Photograph MLX90614 distance, angle, emissivity/target and ambient reference placement.
- [ ] Verify fresh telemetry, source, age and plausible reading before accepting any temperature evidence.

## F. Safety hardware inspection

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Inspect SSR rating/installation, circuit breaker, E-STOP and independent disconnect.
- [ ] Check PSU/load output-inhibit status and approved safe state before each trial.
- [ ] Stop if any physical protection, identity, communication or output state is uncertain.

## G. Software revision

- [ ] Record full Git SHA, application version, firmware revision and config hash.
- [ ] Confirm checked-in tests and simulation-only preflight completed; no test energized hardware.
- [ ] Confirm session schema, metadata sidecar and hash generation on a non-hardware fixture.

## H. Validation campaign config

- [ ] Campaign/specimen/run index and protocol revision are nonempty and consistent.
- [ ] Record profile, C10 basis, current, cutoff, OTP setting, phase targets and all safety limits.
- [ ] Confirm Final profile denominator and run allocations are frozen.

## I. Dry run

- [ ] Complete simulation/mock workflow and traceability review.
- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Perform approved no-load/low-risk instrument communication check with outputs confirmed disabled.
- [ ] Verify abort/fault safe-off procedure and evidence capture arrangement before energizing.

## J. DCIR campaign

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Run only the approved low-energy pulse protocol after signed safety review.
- [ ] Capture raw timestamps, V/I, phase, sample quality, temperature and instrument source.
- [ ] Retain all pulse candidates and predefined rejection reasons; do not cherry-pick.
- [ ] Use independent runs; apply only the frozen run aggregation and sample-SD rule.

## K. C10 campaign

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Run the frozen full-charge, rested YTZ6V C10 sequence under attended supervision.
- [ ] Confirm controlled charge completed before discharge; capture protocol and terminal metadata.
- [ ] Capture pack voltage/current through the 10.50 V endpoint and distinguish UVP activation.
- [ ] Stop for safety trip, stale/missing critical temperature, invalid traceability or unplanned deviation.

## L. EKF validation

- [ ] Use only a qualifying independent C10 capacity reference.
- [ ] Freeze convergence/sustain definition before analysis; report signed and absolute error.
- [ ] Align reference and EKF values by timestamps and retain excluded-row diagnostics.

## M. Grading audit

- [ ] Link each issued grade to CSV hash, sidecar, software SHA and exact analysis output.
- [ ] Verify fallback/profile resistance is not represented as measured Final DCIR.
- [ ] Report all withheld grades and reasons; do not silently omit ungradeable runs.

## N. Fault-safe trials

- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Use only approved simulated sensor fault, controlled threshold or low-energy dummy injection.
- [ ] Do not create battery-threatening conditions; capture trigger, alarm, outputs and independent safe-state evidence.
- [ ] Retain every predefined trial, including failures and aborted trials.

## O. OTP configuration

- [ ] Simulation-check settings 45, 50, 55 and 60 °C through save → runtime consumer → sidecar/log path.
- [ ] Do not heat a battery to prove configuration range.
- [ ] **[MANUAL PHYSICAL ACTION — USER/TEAM ONLY]** Any thermal response test needs a separate approved safe dummy procedure.

## P. Evidence backup

- [ ] Copy CSV, `.meta.json`, `.sha256`, analysis JSON, photos/video and protocol snapshot to read-only campaign storage.
- [ ] Record custodian, copy time, hash and secondary backup location.

## Q. Post-run integrity check

- [ ] Confirm CSV/sidecar/session IDs and all timestamps reconcile.
- [ ] Verify hashes, row counts, schema, V/I/T completeness, sample quality and terminal status.
- [ ] Inventory missing, stale, gap and invalid fields before any requirement calculation.
- [ ] Freeze raw evidence before analysis; preserve original inputs unchanged.
