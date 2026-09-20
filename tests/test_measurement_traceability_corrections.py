import csv
import json
import math
import time
import warnings

import numpy as np

from aset_batt.acquisition.analysis import (
    _integration_gap_metrics, identify_dcir, analyze_series,
)
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.hardware.hardware_driver import HardwareController
from aset_batt.storage.data_utils import (
    DataHandler, _compute_summary, write_session_metadata,
)


def _profile():
    return BatteryProfile("test", "LeadAcid", 12, 6, 7, 14.4, 10.5, 1.4,
                          7, 15, 10, 45, 55, 0.03)


def _temp_hw(connected=True, temp=0.0, sample_time=None):
    hw = object.__new__(HardwareController)
    hw.is_esp_connected = connected
    hw.current_temp = temp
    hw.last_temperature_sample_time = sample_time
    return hw


def test_temperature_initial_zero_is_not_valid_without_sample():
    info = _temp_hw(connected=True).temperature_measurement()
    assert info["temperature_c"] == 0.0
    assert info["temperature_status"] == "NOT_AVAILABLE"
    assert not info["temperature_valid"]


def test_temperature_disconnected_stale_and_nonfinite_statuses():
    assert _temp_hw(connected=False, temp=25, sample_time=time.time()).temperature_measurement()["temperature_status"] == "DISCONNECTED"
    assert _temp_hw(connected=True, temp=25, sample_time=time.perf_counter() - 20).temperature_measurement()["temperature_status"] == "STALE"
    assert _temp_hw(connected=True, temp=float("nan"), sample_time=time.time()).temperature_measurement()["temperature_status"] == "NONFINITE"


def test_fresh_temperature_is_valid():
    info = _temp_hw(connected=True, temp=24.5, sample_time=time.perf_counter()).temperature_measurement()
    assert info["temperature_valid"] and info["temperature_status"] == "VALID"
    assert info["temperature_age_s"] < 1


def test_gap_metrics_and_excessive_duration():
    result = _integration_gap_metrics([0, 1, 2, 100], ["VALID", "VALID", "GAP", "VALID"])
    assert result["gap_count"] >= 1
    assert result["excluded_gap_duration_s"] >= 98
    assert result["integration_quality_status"] == "EXCESSIVE_GAPS"


def test_dcir_rejects_gap_at_transient_edge():
    i = np.array([0, 0, 2, 2, 2], dtype=float)
    v = np.array([12.6, 12.6, 12.5, 12.5, 12.5])
    result = identify_dcir(i, v, np.full(5, 25.0), _profile(),
                           time_s=np.arange(5) * 0.1,
                           sample_quality=["VALID", "GAP", "VALID", "VALID", "VALID"])
    assert result[3] is False


def test_excessive_gap_withholds_quick_soh():
    t = np.array([0, 1, 2, 100, 101, 102], float)
    i = np.array([0, 2, 2, 2, 2, 2], float)
    v = np.array([12.6, 12.4, 12.3, 12.2, 12.0, 10.4])
    result = analyze_series(t, i, v, np.full(6, 25.0), np.zeros(6), _profile(),
                            False, modes=["OCV_SETTLE", "MINI_PULSE", "MAIN_DISCHARGE",
                                          "MAIN_DISCHARGE", "MAIN_DISCHARGE", "MAIN_DISCHARGE"],
                            quick_scan=True, sample_quality=["VALID", "VALID", "GAP",
                                                            "VALID", "VALID", "VALID"])
    assert result["integration_quality_status"] == "EXCESSIVE_GAPS"
    assert not result["quick_soh_est_valid"]


def test_session_metadata_snapshots_instrument_and_effective_offsets(tmp_path):
    path = tmp_path / "sample.csv"
    class Hw:
        _psu_voltage_offset = 0.01
        _psu_configured_current_offset = 0.02
        _psu_runtime_zero_offset = 0.03
        _psu_current_offset = 0.05
        _load_voltage_offset = 0.04
        _load_current_offset = 0.06
        def instrument_identity(self):
            return {"psu": {"idn": "Vendor,PSU,Model,FW", "resource": "USB::A"},
                    "electronic_load": {"idn": "Vendor,Load,Model,FW", "resource": "USB::B"}}
    write_session_metadata(str(path), hardware=Hw())
    meta = json.loads((tmp_path / "sample.csv.meta.json").read_text())
    assert meta["instruments"]["psu"]["idn"].endswith("FW")
    assert meta["calibration"]["source"] == "CONFIGURED_OFFSET_CORRECTION"
    assert meta["calibration"]["psu_runtime_zero_offset_a"] == 0.03
    assert meta["calibration"]["psu_effective_current_offset_a"] == 0.05
    assert meta["calibration"]["version"]


def test_csv_records_temperature_provenance(tmp_path):
    path = tmp_path / "t.csv"
    logger = DataHandler(throttle_redundant_rows=False)
    assert logger.start_logging(str(path))[0]
    logger.log_row(0.1, 12.2, 1.0, 50, 20, 25.0, mode="TEST",
                   temperature_status="VALID", temperature_age_s=0.2,
                   temperature_source="MLX90614 via ESP32")
    logger.stop_logging()
    with path.open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["Temperature_Status"] == "VALID"
    assert row["Temperature_Age_s"] == "0.200"
    assert row["Temperature_Source"] == "MLX90614 via ESP32"


def test_active_worker_faults_stale_temperature_before_estimator_and_logs_status(tmp_path):
    from aset_batt.acquisition.models import OperationMode, TestConfig
    from aset_batt.acquisition.worker import AcquisitionWorker

    class Backend:
        hw = type("HW", (), {"last_voltage_source": "eload", "last_current_source": "eload"})()
        def start_mode(self, cfg): pass
        def step(self, dt, elapsed): return 12.4, -1.0
        def temperature_measurement(self):
            return {"temperature_c": 25.0, "temperature_valid": False,
                    "temperature_age_s": 12.0, "temperature_source": "MLX90614 via ESP32",
                    "temperature_status": "STALE"}
        def emergency_zero(self): self.stopped = True
        def safe_shutdown(self): self.stopped = True

    class Estimator:
        calls = 0
        def update(self, *args, **kwargs): self.calls += 1

    cfg = TestConfig(_profile(), OperationMode.CC_DISCHARGE, sample_hz=10)
    backend, estimator = Backend(), Estimator()
    path = tmp_path / "stale.csv"
    worker = AcquisitionWorker(backend, cfg, str(path), estimator=estimator)
    worker._post_process = lambda *args: {"soh": float("nan")}
    rows, alarms = [], []
    worker.telemetry.connect(rows.append)
    worker.alarm.connect(lambda sev, msg: alarms.append(msg))
    worker.run()
    with path.open(encoding="utf-8-sig", newline="") as f:
        logged = next(csv.DictReader(f))
    assert estimator.calls == 0
    assert backend.stopped
    assert rows[0]["temp_status"] == "STALE"
    assert "TEMP_SENSOR_STALE" in alarms[0]
    assert logged["Sample_Quality"] == "INVALID"
    assert logged["Temperature_Status"] == "STALE"
    assert logged["Temperature_Age_s"] == "12.000"


def test_measured_energy_integrates_power_in_time():
    rows = [
        {"Elapsed_s": "0", "Voltage_V": "10", "Current_A": "0", "Sample_Quality": "VALID"},
        {"Elapsed_s": "1", "Voltage_V": "20", "Current_A": "10", "Sample_Quality": "VALID"},
    ]
    summary = _compute_summary(rows)
    assert math.isclose(summary["energy_out_wh"], 100 / 3600, rel_tol=1e-10)
    assert not math.isclose(summary["energy_out_wh"], 0.5 * 5 * 1 / 3600)
    assert summary["energy_integration_method"] == "TRAPEZOIDAL_MEASURED_VI"


def test_legacy_transient_dcir_is_explicitly_deprecated_and_quick_scan_avoids_it():
    source = open("aset_batt/ui/sequences/quick_scan.py", encoding="utf-8").read()
    assert "transient_dcir_measure" not in source
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        # No hardware I/O: call on an uninitialized instance and observe the
        # deprecation before its expected missing-instrument error.
        try:
            HardwareController.transient_dcir_measure(object.__new__(HardwareController), 1, 1)
        except Exception:
            pass
    assert any(x.category is DeprecationWarning for x in seen)
