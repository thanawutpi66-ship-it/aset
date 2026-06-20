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

## Two GUIs (PySide6, single Qt binding)

| Entry point | What it is |
|---|---|
| `python main.py` → `ui/isa101_views.py` | **Integrated app** — wired to the real domain stack (`battery_model`, `state_estimator`, `charge_controller`, `analysis_module`, `hardware_driver`/`mock_hardware`) via `auto_controller` + `app_bootstrapper`. Starts the web dashboard. |
| `python command_center.py` | **Standalone test bench** — a self-contained ISA-101 HMI with a dedicated `QThread` acquisition worker, mode state machines (CC-CV / CC-discharge / HPPC), ICA `dQ/dV` + DTV `dT/dV` (Gaussian-smoothed), HPPC Rᵢ, and grading. Currently runs on a **simulated backend** (SCPI placeholders for real hardware). |

Both follow the **ISA-101 High-Performance HMI** standard: desaturated gray shell, with
saturated color reserved for alarms, status pills, the temperature gauge, and grading badges.

> Note: the two share concepts but not code; unifying the `command_center` worker architecture
> with the integrated app's real backend is the next planned refactor.

---

## Architecture

```
GUI (PySide6, ISA-101)        ui/isa101_views.py · command_center.py
  │  Qt signals / QtRootShim.after  (worker → UI, thread-safe)
Orchestration                 auto_controller.py · app_bootstrapper.py · charge_controller.py
Domain / compute              battery_model · state_estimator · iec61960_standard
                              analysis_module · battery_profiles
Hardware abstraction (HAL)    hardware_driver  ⟷  mock_hardware   (swap via simulation_mode)
Cross-cutting                 config · event_system · service_locator · logging_config · exceptions
Data / remote                 data_utils (CSV) · report_generator (PDF) · web_server · cloud_push
```

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md). Project history/pivot: [context_summary.md](context_summary.md).

---

## Battery profiles

Chemistry physics + charging strategy live in [battery_profiles.json](battery_profiles.json)
(`LiPO`, `LiFePO4`, `LeadAcid`, `Li-ion`) and are loaded by `battery_profiles.py` with a
built-in fallback. The integrated app's runtime config is `config.json` (managed by `config.py`).

---

## Layout

```
ASET_BATT/
├── main.py                  # integrated-app entry (PySide6 ISA-101)
├── command_center.py        # standalone ISA-101 test bench (QThread worker)
├── app_bootstrapper.py      # DI wiring + lifecycle
├── auto_controller.py       # monitor/profile/charge/IEC orchestration
├── battery_model.py · state_estimator.py · charge_controller.py
├── analysis_module.py · iec61960_standard.py · battery_profiles.py
├── hardware_driver.py · mock_hardware.py        # HAL
├── config.py · config.json · battery_profiles.json · command_center_profiles.json
├── data_utils.py · report_generator.py          # CSV + PDF
├── web_server.py · cloud_push.py · cloud_dashboard/   # remote dashboards
├── event_system.py · service_locator.py · logging_config.py · exceptions.py
├── generate_sample_data.py · train_grader.py · make_training_data.py   # scripts
├── ui/  (isa101_views.py, widgets/logos)
├── tests/  (49 tests)
└── docs/
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
