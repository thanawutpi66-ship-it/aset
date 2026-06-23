# ASET Battery Performance Testing & Sorting System

Automated, multi-chemistry battery **characterization, charging, and sorting** controlled
from Python. Drives a DC power supply + DC electronic load over SCPI/PyVISA, reads surface
temperature from an MLX90614 (via ESP32/UART), estimates SoC/SoH/Rᵢ, grades cells, logs to
CSV, and serves a remote dashboard.

> University of Ubon Ratchathani — Electrical Engineering Capstone (A19/2568).

---

## Quick start

```bash
python -m venv venv && venv\Scripts\activate     # Windows
pip install -r requirements.txt
python main.py            # ISA-101 PySide6 desktop GUI (integrated app)
pytest -q                 # 49 tests
```

`config.json` ships in **simulation mode** (no hardware needed); set `"simulation_mode": false`
to drive real instruments.

---

## One unified GUI (PySide6, ISA-101)

**One program:** `python main.py` → `aset_batt/ui/isa101_views.py` — the integrated
ISA-101 HMI wired to the real domain stack (`battery_model`, `state_estimator`,
`charge_controller`, `analysis_module`, `hardware_driver`) via `app_bootstrapper`.
Run on real instruments with `"simulation_mode": false`; develop without hardware via
`MockHardwareController` (`"simulation_mode": true`). Follows the **ISA-101 High-Performance
HMI** standard: desaturated gray shell, color reserved for alarms, status pills, the
temperature gauge, and grading badges.

It covers the full test flow: connect, manual control, **chemistry-aware charge**
(Auto / CC-CV / 3-Stage), **characterization test** (CC-CV / CC-discharge) driven by the
QThread acquisition worker, IEC 61960 profiles (single-ambient), live multi-axis V/I/T trend +
digital readouts + temperature gauge, **ICA `dQ/dV`** diagnostics, and **A/B/C/Reject grading**
from **SoH + single-step DCIR + voltage-sag + CCA proxy** — the features this rig can measure at
its ~5 Hz SCPI readback. A prominent E-Stop, CSV logging, and a PDF report round it out.

> **Scope** (see [docs/project_pivot.md](docs/project_pivot.md)): this is a multi-chemistry
> **grading/sorting** bench, not a high-rate characteriser. 75 Hz acquisition, sharp Ohmic-drop
> capture, fine **1-RC ECM (R0/R1/C1) separation**, DTV `dT/dV`, and multi-temperature sweeps
> were dropped — they are not achievable at ~5 Hz / single ambient without extra hardware. The
> `HPPC` mode and the `parameter_id` 1-RC identifier remain in the tree for reference only.

### Acquisition engine (`aset_batt/acquisition/`)

The `QThread` worker, instrument backends, and analytics are a reusable package:

- **`worker.py`** — `AcquisitionWorker` (mutex-guarded I/O, immediate E-Stop override, safety
  interlocks) + `ReportTask` (PDF off the UI thread). Takes a `StateEstimator` for live
  OCV-corrected SoC/SoH.
- **`backends.py`** — `HardwareBackend` (drives the project **real HAL** → SCPI/VISA + ESP32
  temperature; use with `MockHardwareController` for no-hardware dev) and `VisaSerialBackend`.
- **`analytics.py`** — single-step DCIR, ICA `dQ/dV` (Gaussian-smoothed), A/B/C/Reject grading.

The GUI runs a test with `AcquisitionWorker(HardwareBackend(hw), cfg, csv, estimator)`.

---

## Architecture

```
GUI (PySide6, ISA-101)        ui/isa101_views.py  +  acquisition/worker (QThread)
  │  Qt signals / QtRootShim.after  (worker → UI, thread-safe)
Orchestration                 auto_controller.py · app_bootstrapper.py · charge_controller.py
Domain / compute              battery_model · state_estimator · iec61960_standard
                              analysis_module · battery_profiles
Hardware abstraction (HAL)    hardware_driver  ⟷  mock_hardware   (swap via simulation_mode)
Cross-cutting                 config · event_system · service_locator · logging_config · exceptions
Data / remote                 data_utils (CSV) · report_generator (PDF) · cloud_push
```

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md). Project history/pivot: [context_summary.md](context_summary.md).

---

## Battery profiles

Chemistry physics + charging strategy live in [battery_profiles.json](battery_profiles.json)
(`LiPO`, `LiFePO4`, `LeadAcid`, `Li-ion`) and are loaded by `battery_profiles.py` with a
built-in fallback. The integrated app's runtime config is `config.json` (managed by `config.py`).

---

## Layout

Clean package layout — `python main.py` (root shim) or `python -m aset_batt`.

```
ASET_BATT/
├── main.py                     # thin shim → aset_batt.app.run
├── pyproject.toml              # packaging + pytest/ruff config
├── config.json                 # runtime config (cwd-relative)
├── aset_batt/                  # application package
│   ├── __main__.py             # python -m aset_batt
│   ├── app/        run.py · app_bootstrapper.py · auto_controller.py
│   ├── core/       battery_model · state_estimator · charge_controller
│   │               analysis_module · iec61960_standard · battery_profiles(+json) · config
│   ├── hardware/   hardware_driver(HAL) · mock_hardware
│   ├── acquisition/  worker.py        # unified QThread acquisition worker
│   ├── ui/         isa101_views.py · logos
│   ├── services/   event_system · service_locator · logging_config · exceptions
│   ├── storage/    data_utils(CSV) · report_generator(PDF) · cloud_push
├── scripts/        generate_sample_data · train_grader · make_training_data
├── tests/          (49 tests)
├── docs/ · cloud_dashboard/
└── logs/  data/    (gitignored runtime output)
```

---

## Hardware

| Role | Device |
|---|---|
| DUT | 12 V motorcycle battery (lead-acid AGM, e.g. YTZ7V 7Ah) or lithium |
| DC supply | GW Instek PSW/PSB-1080L (SCPI) |
| DC load | GW Instek PEL-3111 (SCPI) |
| Temperature | MLX90614 (IR) → ESP32 → UART |
| Breaker | LUMIRA MCB (passive overcurrent backstop) |

SCPI readback is ~5 Hz; software cutoff (`:OUTP OFF`/`:INP OFF`) is the primary failsafe with
the MCB as a passive backstop.

---

## Remote dashboard

`web_server.py` serves a local dashboard on port 8000 (exposed publicly via Tailscale Funnel).
`cloud_push.py` + `cloud_dashboard/` push snapshots to an Azure service for 24/7 viewing — see
[cloud_dashboard/README.md](cloud_dashboard/README.md).
