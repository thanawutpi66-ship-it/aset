# Fault-to-Safe-State Matrix

Freeze this set and denominator before collecting data. Do not add/remove trials after seeing outcomes. “Safe state” must be defined per bench wiring and independently observable.

| Fault class | Injection allowed for campaign | Expected application response | Safe-state evidence | Current software coverage | Physical requirement |
|---|---|---|---|---|---|
| UI/operator E-STOP | Controlled dry-run or approved dummy load only | Abort active workflow; request load/PSU off and SSR safe state; persist terminal reason | GUI state, instrument/output indicator, independent current measurement, video | Mock shutdown/callback tests exist | **[PHYSICAL TEST REQUIRED — DO NOT AUTO-RUN]** approved attended trial |
| OVP/UVP/OCP/OTP threshold trip | Simulated sensor/threshold injection in software; hardware trial only under approved low-energy plan | Alarm and safety shutdown; terminal status/reason recorded | injected value, effective threshold, output state, sidecar | Controller/sequence unit tests exist | Hardware response remains unverified |
| Stale/missing temperature | Simulated stale sensor value, no heat | Mark quality/status; enforce defined safety policy; do not silently count V/I/T complete | status, age, source, alarm, terminal outcome | stale escalation tests exist | verify sensor wiring/freshness on bench separately |
| PSU/load communication loss | Mock transport exception; physical unplug only if separately approved safe dummy procedure | Fault path closes outputs and records reason | instrument identity, exception/alarm, independent output/current observation | communication/shutdown tests exist | **[PHYSICAL TEST REQUIRED — DO NOT AUTO-RUN]** no live battery injection |
| Worker/sequence cancellation | Mock thread/runtime cancellation | Idempotent safe-off, no continuation after cancellation | terminal status, event, output states | cancellation tests exist | observe physical safe-off only in approved bench trial |
| CSV/sidecar write failure | Filesystem fault simulation | Acquisition stops safely or strict Final gate rejects incomplete traceability | injected file error, retained error log, no eligible grade | storage/checkpoint tests exist | no hardware fault required |
| SSR commanded open | Software/mock or low-energy dummy load | Controller command plus telemetry-bounded interruption evidence | command timestamp, current decay, contact indication/video | helper distinguishes command-to-next-zero bound from switching latency | **[PHYSICAL TEST REQUIRED — DO NOT AUTO-RUN]** switching latency needs independent instrumentation |

Never use an actual battery over-temperature, short-circuit, or destructive over-current as an injection method. Define numerical safe-state bounds, maximum response time and independent instrumentation before approval.
