"""Accelerated, software-only ASET acquisition soak and persistence audit.

Runs the production AcquisitionWorker and DataHandler against a deterministic
mock backend. A virtual monotonic clock advances only through the worker's
normal pacing seam; CSV formatting and real file flushes remain enabled.
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
import ctypes
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication

from aset_batt.acquisition.analysis import analyze_csv
from aset_batt.acquisition.models import BatteryProfile, OperationMode, TestConfig
from aset_batt.acquisition.worker import AcquisitionWorker
from aset_batt.storage.data_utils import DataHandler


class VirtualClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep_ms(self, milliseconds: int):
        self.value += max(0, milliseconds) / 1000.0


class MockBackend:
    def __init__(self, total_samples: int):
        self.total_samples = total_samples
        self.samples = 0
        self.shutdown_calls = 0
        self.cfg = None

    def start_mode(self, cfg):
        self.cfg = cfg

    def step(self, dt, elapsed):
        self.samples += 1
        # Cross the configured cutoff for exactly the final five confirmations.
        voltage = 9.5 if self.samples > self.total_samples - 5 else 12.6
        current_raw = -4.0 + 0.04 * math.sin(self.samples / 31.0)
        return voltage, current_raw

    def temperature_measurement(self):
        return {
            "temperature_c": 25.0 + 0.2 * math.sin(self.samples / 131.0),
            "temperature_valid": True,
            "temperature_age_s": 0.0,
            "temperature_source": "mock-thermometer",
            "temperature_status": "VALID",
        }

    def safe_shutdown(self):
        self.shutdown_calls += 1

    def emergency_zero(self):
        pass


def _process_memory():
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    handle_count = ctypes.c_ulong()
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(handle_count)):
        raise ctypes.WinError()
    return counters.WorkingSetSize, counters.PeakWorkingSetSize, handle_count.value


def _count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _capture(label, samples, worker, path, started):
    rss, peak_rss, handles = _process_memory()
    session = worker._active_session
    gc_objects = len(gc.get_objects())
    qthreads = sum(1 for obj in gc.get_objects() if isinstance(obj, QThread))
    return {
        "checkpoint": label,
        "samples": samples,
        "rss_mb": round(rss / 1e6, 2),
        "peak_rss_mb": round(peak_rss / 1e6, 2),
        "python_allocated_blocks": sys.getallocatedblocks(),
        "python_objects": gc_objects,
        "python_threads": threading.active_count(),
        "qthread_objects": qthreads,
        "process_handles": handles,
        "csv_rows": session._step_index if session is not None else 0,
        "flush_count": session.flush_count if session is not None else 0,
        "csv_size_bytes": path.stat().st_size if path.exists() else 0,
        "worker_history_sizes": {
            name: len(getattr(worker, name)) for name in
            ("v_hist", "q_hist", "t_hist", "time_hist", "i_hist", "soc_hist")
            if hasattr(worker, name)
        },
        "elapsed_wall_s": round(time.perf_counter() - started, 3),
    }


def _timed_peak(callable_):
    stop = threading.Event()
    peak = [ _process_memory()[0] ]
    def sample():
        while not stop.wait(0.025):
            peak[0] = max(peak[0], _process_memory()[0])
    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
    start = time.perf_counter()
    try:
        value = callable_()
    finally:
        duration = time.perf_counter() - start
        stop.set()
        watcher.join()
    return value, duration, peak[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", help="directory for soak CSV, sidecars, and JSON report")
    parser.add_argument("--samples", type=int, default=432_000)
    args = parser.parse_args()
    out_dir = Path(args.output_dir or tempfile.mkdtemp(prefix="aset_long_soak_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "accelerated_12h.csv"
    report_path = out_dir / "soak_report.json"
    if csv_path.exists():
        raise SystemExit(f"Refusing to overwrite existing session: {csv_path}")

    app = QApplication.instance() or QApplication([])
    profile = BatteryProfile(
        "Mock Soak Profile", "LiFePO4", 12.8, 4, 50.0,
        14.6, 10.0, 10.0, 20.0, 15.0, 5.0, 45.0, 55.0,
    )
    backend = MockBackend(args.samples)
    worker = AcquisitionWorker(
        backend, TestConfig(profile, OperationMode.CC_DISCHARGE, sample_hz=10.0), str(csv_path))
    virtual_clock = VirtualClock()
    worker._clock = virtual_clock.now
    worker._sleep_ms = virtual_clock.sleep_ms
    # The test audits acquisition and persistence separately from numerical
    # analysis; the complete CSV is analyzed below through the production path.
    worker._post_process = lambda *a, **kw: {"soh": None}
    checkpoints = []
    wanted = {10_000: "bench 10k", 18_000: "30 min", 50_000: "bench 50k",
              72_000: "2 h", 100_000: "bench 100k", 216_000: "6 h",
              args.samples: "12 h"}
    sample_count = [0]
    started = time.perf_counter()

    def on_state(state):
        if state == "RUNNING" and not checkpoints:
            checkpoints.append(_capture("start", 0, worker, csv_path, started))

    def on_telemetry(_row):
        sample_count[0] += 1
        label = wanted.get(sample_count[0])
        if label:
            checkpoints.append(_capture(label, sample_count[0], worker, csv_path, started))

    worker.state.connect(on_state, Qt.DirectConnection)
    worker.telemetry.connect(on_telemetry, Qt.DirectConnection)
    worker.run()
    soak_wall_s = time.perf_counter() - started

    # Validate every CSV record by streaming; retain no row collection.
    header_count = 0
    actual_rows = 0
    malformed_rows = 0
    first_elapsed = last_elapsed = None
    monotonic_elapsed = True
    middle = args.samples // 2
    probes = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        for row in reader:
            actual_rows += 1
            if row.get("Timestamp") == "Timestamp":
                header_count += 1
            if None in row or any(value is None for value in row.values()):
                malformed_rows += 1
            try:
                elapsed = float(row["Elapsed_s"])
                if first_elapsed is None:
                    first_elapsed = elapsed
                if last_elapsed is not None and elapsed <= last_elapsed:
                    monotonic_elapsed = False
                last_elapsed = elapsed
            except (ValueError, TypeError, KeyError):
                malformed_rows += 1
            if actual_rows in (1, middle, args.samples):
                probes[str(actual_rows)] = row

    meta = json.loads(Path(str(csv_path) + ".meta.json").read_text(encoding="utf-8"))
    integrity = DataHandler.verify_integrity(str(csv_path))
    profile.capacity_rating_validated = True
    profile.capacity_rating_basis = "C10_TEST"
    analysis, analysis_s, analysis_peak = _timed_peak(lambda: analyze_csv(str(csv_path), profile))

    from aset_batt.storage.report_generator import _render_csv_plot, generate_pdf_report
    plot_path, report_plot_s, report_plot_peak = _timed_peak(lambda: _render_csv_plot(str(csv_path)))
    plot_file_created = bool(plot_path and os.path.exists(plot_path))
    from aset_batt.core.config import ConfigManager
    pdf_path = out_dir / "accelerated_12h_report.pdf"
    _, pdf_s, pdf_peak = _timed_peak(lambda: generate_pdf_report(
        str(pdf_path), ConfigManager(), analysis=analysis, csv_path=str(csv_path)))

    # Benchmark the actual pyqtgraph TrendContainer setData path. The configured
    # 4,000 s history at 10 Hz has a 40,001-point maximum including its endpoints.
    from aset_batt.ui import theme
    theme.set_theme("light")
    from aset_batt.ui.widgets import TrendContainer
    trend = TrendContainer()
    plot_timings = {}
    for count in (100, 1_000, 5_000, 40_001):
        xs = [n / 10.0 for n in range(count)]
        vs = [12.6 - n * 1e-7 for n in range(count)]
        amps = [4.0 + 0.1 * math.sin(n / 30.0) for n in range(count)]
        temps = [25.0 + 0.2 * math.sin(n / 100.0) for n in range(count)]
        latencies = []
        for _ in range(12):
            begin = time.perf_counter()
            trend.update(xs, vs, amps, temps)
            app.processEvents()
            latencies.append((time.perf_counter() - begin) * 1000.0)
        sorted_latencies = sorted(latencies)
        plot_timings[str(count)] = {
            "median_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(sorted_latencies[min(len(latencies) - 1, math.ceil(.95 * len(latencies)) - 1)], 3),
            "max_ms": round(max(latencies), 3),
        }
    trend.close()

    final_rss, peak_rss, final_handles = _process_memory()
    report = {
        "software_only": True,
        "physical_battery_test_performed": False,
        "configuration": {"sample_hz": 10, "samples": args.samples,
                          "virtual_elapsed_s": virtual_clock.value,
                          "flush_interval_virtual_s": 1,
                          "checkpoint_interval_virtual_s": 30},
        "soak_wall_s": round(soak_wall_s, 3),
        "checkpoints": checkpoints,
        "completion": {
            "actual_rows": actual_rows, "expected_rows": args.samples,
            "header_count": header_count + 1, "columns": columns,
            "malformed_rows": malformed_rows,
            "elapsed_first_s": first_elapsed, "elapsed_last_s": last_elapsed,
            "elapsed_strictly_increasing": monotonic_elapsed,
            "sample_probes": probes,
            "metadata_status": meta.get("status"),
            "metadata_checkpoint": meta.get("checkpoint"),
            "sha256_verified": integrity,
            "csv_size_bytes": csv_path.stat().st_size,
            "file_closed": worker._active_session is None,
            "safe_shutdown_calls": backend.shutdown_calls,
            "telemetry_count": sample_count[0],
            "flush_count": checkpoints[-1].get("flush_count") if checkpoints else None,
        },
        "logger_benchmarks": {
            entry["samples"]: {
                "elapsed_s": entry["elapsed_wall_s"],
                "rows_per_second": round(entry["samples"] / max(entry["elapsed_wall_s"], 1e-9), 1),
                "file_size_bytes": entry["csv_size_bytes"],
                "flush_count": entry["flush_count"],
                "rss_mb": entry["rss_mb"],
            }
            for entry in checkpoints if entry["checkpoint"].startswith("bench ")
        },
        "analysis": {
            "csv_read_and_analysis_s": round(analysis_s, 3),
            "peak_rss_mb": round(analysis_peak / 1e6, 2),
            "grade": analysis.get("grade"), "soh": analysis.get("soh"),
            "capacity_ah": analysis.get("capacity_ah"),
            "session_complete": analysis.get("session_complete"),
            "plot_data_generation_s": round(report_plot_s, 3),
            "plot_generation_peak_rss_mb": round(report_plot_peak / 1e6, 2),
            "plot_file_created": plot_file_created,
            "pdf_report_generation_s": round(pdf_s, 3),
            "pdf_generation_peak_rss_mb": round(pdf_peak / 1e6, 2),
            "pdf_file_created": pdf_path.exists(),
        },
        "live_plot_setdata_latency": plot_timings,
        "final_process": {"rss_mb": round(final_rss / 1e6, 2),
                          "peak_rss_mb": round(peak_rss / 1e6, 2),
                          "process_handles": final_handles,
                          "python_threads": threading.active_count()},
        "artifacts": {"csv": str(csv_path), "metadata": str(csv_path) + ".meta.json",
                      "sha256": str(csv_path) + ".sha256", "pdf": str(pdf_path)},
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if (actual_rows == args.samples and integrity is True
                 and meta.get("status") == "completed" and malformed_rows == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
