"""Focused software-only regression coverage for the Coulomb η cycle."""
import hashlib
import json

import pytest

from aset_batt.core import battery_profiles
from aset_batt.core.characterization import (
    evaluate_coulomb_efficiency,
    integrate_coulomb_ah,
)
from aset_batt.storage.data_utils import DataHandler, write_session_metadata


def test_ytz6v_eta_reference_is_c10_half_amp_not_c20():
    product = battery_profiles.get_product("YTZ6V (12V 5.3Ah VRLA)")
    reference_a = product.capacity_10h_ah / 10.0
    assert product.capacity_10h_ah == 5.0
    assert product.capacity_20h_ah == 5.3
    assert reference_a == 0.500


def test_eta_busy_guard_blocks_all_shared_instrument_entries():
    import threading
    from unittest.mock import MagicMock
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aset_batt.ui import theme
    theme.set_theme("light")
    from PySide6.QtWidgets import QApplication
    from aset_batt.core.config import ConfigManager
    from aset_batt.ui.isa101_views import BatteryQtWindow
    app = QApplication.instance() or QApplication([])
    window = BatteryQtWindow(ConfigManager())
    try:
        window.hw = MagicMock(is_connected=True)
        window.controller = MagicMock(monitor_running=False)
        window._test_thread = None
        window._seq_running = threading.Event()
        window._char_running = {"eta": threading.Event()}
        window._char_running["eta"].set()
        assert "Coulomb η" in window._busy_reason()
        assert window._char_guard() is False
    finally:
        window._char_running["eta"].clear()
        window.close()
        app.processEvents()


def test_timestamp_integrator_uses_measured_signs_and_nonuniform_intervals():
    # Accelerated synthetic samples retain the 20 s span while using deliberately
    # nonuniform timestamps; currents are scaled to yield practical Ah totals.
    qin = integrate_coulomb_ah([0, 7, 20], [-936, -936, -936], phase="charge",
                               expected_dt_s=5.0, max_integrable_gap_s=15.0)
    qout = integrate_coulomb_ah([0, 7, 20], [891, 891, 891], phase="discharge",
                                expected_dt_s=5.0, max_integrable_gap_s=15.0)
    assert qin["ah"] == pytest.approx(5.2)
    assert qout["ah"] == pytest.approx(4.95)
    assert qin["integration_quality_status"] == "VALID"
    result = evaluate_coulomb_efficiency(
        qin, qout, conditioning_endpoint_valid=True,
        full_charge_confirmed=True, reference_cutoff_reached=True)
    assert result["eta_coulomb_pct"] == pytest.approx(95.1923077)
    assert result["valid"] is True


@pytest.mark.parametrize("times,currents", [
    ([0, 0, 5], [-1, -1, -1]),       # duplicate timestamp
    ([0, 5, 100], [-1, -1, -1]),    # integration gap exceeds 5% span
])
def test_invalid_sample_timing_prevents_valid_eta(times, currents):
    bad = integrate_coulomb_ah(times, currents, phase="charge")
    good = {"ah": 1.0, "valid": True}
    result = evaluate_coulomb_efficiency(
        bad, good, conditioning_endpoint_valid=True,
        full_charge_confirmed=True, reference_cutoff_reached=True)
    assert result["status"] == "SAMPLING_INVALID"
    assert result["eta_coulomb_pct"] is None


def test_incomplete_boundaries_and_abort_never_report_numeric_eta():
    good = {"ah": 5.0, "valid": True}
    cases = [
        ({"conditioning_endpoint_valid": False, "full_charge_confirmed": True,
          "reference_cutoff_reached": True}, "CONDITIONING_INCOMPLETE"),
        ({"conditioning_endpoint_valid": True, "full_charge_confirmed": False,
          "reference_cutoff_reached": True}, "FULL_CHARGE_NOT_CONFIRMED"),
        ({"conditioning_endpoint_valid": True, "full_charge_confirmed": True,
          "reference_cutoff_reached": False}, "CUTOFF_NOT_REACHED"),
        ({"conditioning_endpoint_valid": True, "full_charge_confirmed": True,
          "reference_cutoff_reached": True, "aborted": True,
          "abort_reason": "temperature trip"}, "TEMPERATURE_ABORT"),
    ]
    for gates, expected_status in cases:
        result = evaluate_coulomb_efficiency(good, good, **gates)
        assert result["status"] == expected_status
        assert result["eta_coulomb_pct"] is None


def test_over_100_percent_is_flagged_without_clamping():
    result = evaluate_coulomb_efficiency(
        {"ah": 5.0, "valid": True}, {"ah": 5.1, "valid": True},
        conditioning_endpoint_valid=True, full_charge_confirmed=True,
        reference_cutoff_reached=True)
    assert result["eta_coulomb_pct"] == pytest.approx(102.0)
    assert result["status"] == "SUSPECT_RESULT"
    assert result["valid"] is False


def test_integrity_finalization_persists_a_cancelled_partial_session(tmp_path):
    csv_path = tmp_path / "test_CoulombEfficiency_simulated.csv"
    data = DataHandler(throttle_redundant_rows=False)
    ok, _ = data.start_logging(str(csv_path), test_type="CoulombEfficiency")
    assert ok
    write_session_metadata(str(csv_path), test_type="CoulombEfficiency",
                           session_id=data.session_id,
                           extra={"protocol": {"id": "coulomb-efficiency-v1"}})
    data.log_row(0, 12.4, -0.5, 50, 20, 25, mode="CHARGE_CC",
                 phase="CHARGE_CC", expected_dt_s=5)
    data.flush()
    # File is created and a complete row is durable before test completion.
    assert csv_path.exists() and len(csv_path.read_text(encoding="utf-8-sig").splitlines()) == 2
    data.stop_logging("cancelled", "simulated operator cancel")

    meta = json.loads((tmp_path / "test_CoulombEfficiency_simulated.csv.meta.json").read_text())
    assert meta["status"] == "cancelled"
    assert meta["end_reason"] == "simulated operator cancel"
    digest, *_ = (tmp_path / "test_CoulombEfficiency_simulated.csv.sha256").read_text().split()
    assert digest == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert DataHandler.verify_integrity(str(csv_path)) is True


def test_generated_pdf_report_includes_session_eta_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch
    from aset_batt.storage.report_generator import generate_pdf_report

    csv_path = tmp_path / "eta.csv"
    csv_path.write_text("Elapsed_s,Voltage_V,Current_A\n0,12.4,-0.5\n", encoding="utf-8")
    (tmp_path / "eta.csv.meta.json").write_text(json.dumps({
        "test_type": "CoulombEfficiency", "Qin_Ah": 5.20, "Qout_Ah": 4.95,
        "eta_coulomb_pct": 95.19, "eta_status": "VALID",
        "reference_discharge_current_a": 0.5,
        "charge_duration_s": 36000, "discharge_duration_s": 36000,
        "capacity_10h_ah": 5.0, "capacity_20h_ah": 5.3,
    }), encoding="utf-8")
    config = SimpleNamespace(battery=SimpleNamespace(
        battery_type="LeadAcid", product_name="YTZ6V", cells_series=6,
        cells_parallel=1, pack_nominal_voltage=12.0, rated_capacity=5.0,
        mass_grams=900))
    with patch("aset_batt.storage.report_generator.SimpleDocTemplate") as doc, \
            patch("aset_batt.storage.report_generator._render_csv_plot", return_value=None):
        generate_pdf_report(str(tmp_path / "eta.pdf"), config, csv_path=str(csv_path))
        rendered_text = " ".join(str(x) for x in doc.call_args[0])
        story = doc.return_value.build.call_args.args[0]
        assert any(getattr(item, "getPlainText", lambda: "")() == "Coulombic Efficiency"
                   for item in story)


def test_actual_eta_worker_cancel_and_communication_error_finalize_safely(tmp_path, monkeypatch):
    """Run the real worker body with an instrument mock and shortened phase loops."""
    import os
    import threading

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aset_batt.ui import theme
    theme.set_theme("light")
    from PySide6.QtWidgets import QApplication
    from aset_batt.core.config import ConfigManager
    from aset_batt.core.battery_model import BatteryModel
    from aset_batt.core.state_estimator import StateEstimator
    from aset_batt.app.auto_controller import AutoController
    from aset_batt.hardware.mock_hardware import MockHardwareController
    from aset_batt.ui.isa101_views import BatteryQtWindow

    app = QApplication.instance() or QApplication([])
    cfg = ConfigManager.__new__(ConfigManager)
    from aset_batt.core.config import BatteryConfig, SystemConfig, HardwareConfig
    cfg.battery = BatteryConfig(product_name="YTZ6V (12V 5.3Ah VRLA)",
                                battery_type="LeadAcid", rated_capacity=5.0,
                                cells_series=6, cells_parallel=1,
                                min_voltage=1.75, max_voltage=2.45)
    cfg.system = SystemConfig()
    cfg.hardware = HardwareConfig()
    hw = MockHardwareController()
    hw.connect_esp32("MOCK")
    model = BatteryModel("LeadAcid", 5.0, 6, 1)
    ctrl = AutoController(None, hw, DataHandler(), StateEstimator(5.0, model), cfg)
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    monkeypatch.chdir(tmp_path)
    def cancel_after_first_sample(ev, seconds):
        ev.clear()
        return False
    monkeypatch.setattr(win, "_char_sleep", cancel_after_first_sample)
    hardware_stops = []
    original_stop = win._char_hw_stop
    def safe_stop():
        hardware_stops.append(True)
        original_stop()
    monkeypatch.setattr(win, "_char_hw_stop", safe_stop)
    try:
        event = threading.Event()
        event.set()
        win._char_running["eta"] = event
        win._char_eta_thread()
        assert not hw._load_current
        assert not hw._psu_output_on
        assert hardware_stops
        assert not ctrl.data.is_recording
        files = list((tmp_path / "sessions").glob("test_CoulombEfficiency_*.csv"))
        assert len(files) == 1
        meta = json.loads((tmp_path / "sessions" / (files[0].name + ".meta.json")).read_text())
        assert meta["status"] == "cancelled"
        assert meta["eta_status"] == "CANCELLED"
        assert meta["session_id"] == ctrl.data.session_id
        assert DataHandler.verify_integrity(str(files[0])) is True

        # Inject a communication failure on the first acquisition and verify that
        # the independent exception path also closes outputs and finalizes files.
        win._char_running["eta"] = threading.Event()
        win._char_running["eta"].set()
        monkeypatch.setattr(win, "_char_sleep", lambda ev, seconds: True)
        monkeypatch.setattr(hw, "read_measurements", lambda **kwargs: (_ for _ in ()).throw(OSError("simulated SCPI read failure")))
        win._char_eta_thread()
        assert not hw._load_current
        assert not hw._psu_output_on
        assert not ctrl.data.is_recording
        files = list((tmp_path / "sessions").glob("test_CoulombEfficiency_*.csv"))
        assert len(files) == 2
        assert all(DataHandler.verify_integrity(str(path)) is True for path in files)
        metas = [json.loads((path.parent / (path.name + ".meta.json")).read_text()) for path in files]
        assert any(m.get("eta_status") == "CANCELLED" and m["status"] == "cancelled" for m in metas)
        assert any(m["status"] == "fault" and m.get("eta_status") == "ERROR" for m in metas)
    finally:
        win.close()
        app.processEvents()


def test_actual_eta_worker_completes_accelerated_boundary_matched_cycle(tmp_path, monkeypatch):
    """Execute every real η worker phase and the real charge state machine offline."""
    import os
    import threading
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aset_batt.ui import theme
    theme.set_theme("light")
    from PySide6.QtWidgets import QApplication
    from aset_batt.core.config import ConfigManager, BatteryConfig, SystemConfig, HardwareConfig
    from aset_batt.core.battery_model import BatteryModel
    from aset_batt.core.state_estimator import StateEstimator
    from aset_batt.core.charge_controller import ChargeController
    from aset_batt.app.auto_controller import AutoController
    from aset_batt.hardware.mock_hardware import MockHardwareController
    from aset_batt.storage.data_utils import DataHandler
    from aset_batt.ui.isa101_views import BatteryQtWindow

    app = QApplication.instance() or QApplication([])
    if not hasattr(BatteryQtWindow, "_on_hardware_task_done"):
        monkeypatch.setattr(BatteryQtWindow, "_on_hardware_task_done",
                            lambda self, *args: None, raising=False)
    cfg = ConfigManager.__new__(ConfigManager)
    cfg.battery = BatteryConfig(product_name="YTZ6V (12V 5.3Ah VRLA)",
                                battery_type="LeadAcid", rated_capacity=5.0,
                                cells_series=6, cells_parallel=1,
                                min_voltage=1.75, max_voltage=2.45)
    cfg.system = SystemConfig()
    cfg.hardware = HardwareConfig()

    class FullCycleMock(MockHardwareController):
        def __init__(self):
            super().__init__()
            self.virtual_time = 0.0
            self.load_cycles = 0
            self.load_samples = 0
            self.charge_samples = 0
            self.connect_esp32("MOCK")
            self.temp_is_stale = lambda max_age_s=10.0: False

        def set_load(self, state, current_val="0"):
            was_on = self._load_current > 0
            result = super().set_load(state, current_val)
            if state and not was_on:
                self.load_cycles += 1
                self.load_samples = 0
            return result

        def read_vi(self):
            if self._charging and self._cccv_v:
                self.virtual_time += 30.0
                self.charge_samples += 1
                n = self.charge_samples
                if n < 1246:
                    return 13.8, 0.5, 0.0
                if n == 1246:
                    return 14.4, 0.5, 0.0
                    return 14.4, 0.10, 0.0
            return 12.0, 0.0, self._load_current

        def read_measurements(self, prefer_load_v=False):
            self.virtual_time += 30.0
            if self._load_current > 0:
                self.load_samples += 1
                # Conditioning reaches the active JSON profile's 10.0 V cutoff immediately.
                # The reference discharge supplies 4.95 Ah at 0.500 A before cutoff.
                cutoff = (self.load_cycles == 1 or self.load_samples >= 1185)
                v = 9.9 if cutoff else 12.0
                return v, self._load_current
            if self._psu_output_on:
                return 14.4, -0.15
            return 12.0, 0.0

    class SimulationDataHandler(DataHandler):
        csv_observed_early = False

        def log_row(self, *args, **kwargs):
            super().log_row(*args, **kwargs)
            if self.is_recording and not self.csv_observed_early:
                self.csv_file.flush()
                self.csv_observed_early = self.csv_file.tell() > 0
                # Keep the accelerated test from issuing thousands of physical
                # flush calls; stop_logging closes and flushes the complete file.
                self._last_flush = 1e100

    hw = FullCycleMock()
    model = BatteryModel("LeadAcid", 5.0, 6, 1)
    data = SimulationDataHandler(throttle_redundant_rows=False)
    ctrl = AutoController(None, hw, data,
                          StateEstimator(5.0, model), cfg)
    # Recovery snapshots are separately exercised by controller tests; omitting
    # disk writes here makes the time-scaled end-to-end cycle deterministic/fast.
    ctrl.save_recovery_state = lambda state: None
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(time, "perf_counter", lambda: hw.virtual_time)
    original_init = ChargeController.__init__

    def accelerated_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.poll_interval_s = 0.0001
        self.params.stage_timeout_s = 1e9

    monkeypatch.setattr(ChargeController, "__init__", accelerated_init)
    monkeypatch.setattr(win, "_char_sleep", lambda ev, seconds: ev.wait(0.0001))
    try:
        event = threading.Event()
        main_thread_id = threading.get_ident()
        worker_threads = []
        original_worker = win._char_eta_thread
        def tracked_worker():
            worker_threads.append(threading.get_ident())
            original_worker()
        event = threading.Event(); event.set()
        win._char_running["eta"] = event
        worker = threading.Thread(target=tracked_worker, daemon=True)
        worker.start()
        while worker.is_alive():
            app.processEvents(); time.sleep(0.0001)
        app.processEvents()

        assert worker_threads and worker_threads[0] != main_thread_id
        result = win._char_results["eta"]
        assert result["valid"] is True
        assert result["q_in_ah"] == pytest.approx(5.20, abs=0.01)
        assert result["q_out_ah"] == pytest.approx(4.95, abs=0.01)
        assert result["eta_coulomb_pct"] == pytest.approx(95.1923, abs=0.2)
        assert hw.charge_samples >= 1251
        assert data.csv_observed_early is True
        assert not hw._load_current
        assert not hw._psu_output_on

        files = list((tmp_path / "sessions").glob("test_CoulombEfficiency_*.csv"))
        assert len(files) == 1
        csv_path = files[0]
        assert csv_path.stat().st_size > 0
        rows = csv_path.read_text(encoding="utf-8-sig").splitlines()
        assert any(",CHARGE_TAPER," in row for row in rows)
        assert any(",REFERENCE_DISCHARGE," in row for row in rows)
        metadata_path = csv_path.with_name(csv_path.name + ".meta.json")
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert meta["session_id"] == ctrl.data.session_id
        assert meta["test_type"] == "CoulombEfficiency"
        assert meta["status"] == "completed"
        assert meta["eta_status"] == "VALID"
        assert meta["full_charge_confirmed"] is True
        assert meta["conditioning_endpoint_valid"] is True
        assert meta["reference_cutoff_reached"] is True
        assert meta["eta_coulomb_pct"] == pytest.approx(result["eta_coulomb_pct"])
        assert DataHandler.verify_integrity(str(csv_path)) is True
        digest_file = csv_path.with_name(csv_path.name + ".sha256")
        assert digest_file.exists()
        assert "LOWER_REST" in json.dumps(meta["protocol"])
        assert "FINAL_REST" in json.dumps(meta["protocol"])
        assert f"{result['eta_coulomb_pct']:.2f}%" in win.lbl_char_eta_status.text()
        from aset_batt.storage.report_generator import generate_pdf_report
        pdf_path = tmp_path / "CoulombEfficiency-result.pdf"
        generate_pdf_report(str(pdf_path), cfg, csv_path=str(csv_path))
        assert pdf_path.exists() and pdf_path.stat().st_size > 0
    finally:
        win.close()
        app.processEvents()


@pytest.mark.parametrize("scenario", [
    "cancel_charge", "cancel_reference", "comm_charge", "comm_reference",
], ids=[
    "ETA-PERSIST-01-cancel-charge", "ETA-PERSIST-02-cancel-reference",
    "ETA-PERSIST-03-communication-charge", "ETA-PERSIST-04-communication-reference",
])
def test_eta_interruption_persistence_matrix(tmp_path, monkeypatch, scenario):
    """Inject each interruption in the real worker and verify persisted evidence."""
    import os
    import threading
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aset_batt.ui import theme
    theme.set_theme("light")
    from PySide6.QtWidgets import QApplication
    from aset_batt.core.config import ConfigManager, BatteryConfig, SystemConfig, HardwareConfig
    from aset_batt.core.battery_model import BatteryModel
    from aset_batt.core.state_estimator import StateEstimator
    from aset_batt.core.charge_controller import ChargeController
    from aset_batt.app.auto_controller import AutoController
    from aset_batt.hardware.mock_hardware import MockHardwareController
    from aset_batt.storage.data_utils import DataHandler
    from aset_batt.ui.isa101_views import BatteryQtWindow

    app = QApplication.instance() or QApplication([])
    if not hasattr(BatteryQtWindow, "_on_hardware_task_done"):
        monkeypatch.setattr(BatteryQtWindow, "_on_hardware_task_done",
                            lambda self, *args: None, raising=False)
    cfg = ConfigManager.__new__(ConfigManager)
    cfg.battery = BatteryConfig(product_name="YTZ6V (12V 5.3Ah VRLA)",
                                battery_type="LeadAcid", rated_capacity=5.0,
                                cells_series=6, cells_parallel=1,
                                min_voltage=1.75, max_voltage=2.45)
    cfg.system = SystemConfig()
    cfg.hardware = HardwareConfig()

    class InjectionMock(MockHardwareController):
        def __init__(self):
            super().__init__()
            self.virtual_time = 0.0
            self.load_cycles = 0
            self.load_samples = 0
            self.charge_samples = 0
            self.connect_esp32("MOCK")
            self.temp_is_stale = lambda max_age_s=10.0: False

        def set_load(self, state, current_val="0"):
            was_on = self._load_current > 0
            result = super().set_load(state, current_val)
            if state and not was_on:
                self.load_cycles += 1
                self.load_samples = 0
            return result

        def read_vi(self):
            if self._charging and self._cccv_v:
                if scenario == "comm_charge" and self.charge_samples >= 20:
                    raise OSError("simulated charge VISA communication failure")
                self.virtual_time += 30.0
                self.charge_samples += 1
                n = self.charge_samples
                if n < 1246:
                    return 13.8, 0.5, 0.0
                if n == 1246:
                    return 14.4, 0.5, 0.0
            return 12.0, 0.0, self._load_current

        def read_measurements(self, prefer_load_v=False):
            self.virtual_time += 30.0
            if self._load_current > 0:
                self.load_samples += 1
                if scenario == "comm_reference" and self.load_cycles == 2 \
                        and self.load_samples >= 3:
                    raise OSError("simulated reference VISA communication failure")
                cutoff = (self.load_cycles == 1 or self.load_samples >= 1185)
                return (9.9 if cutoff else 12.0), self._load_current
            if self._psu_output_on:
                return 14.4, -0.15
            return 12.0, 0.0

    hw = InjectionMock()
    model = BatteryModel("LeadAcid", 5.0, 6, 1)
    data = DataHandler(throttle_redundant_rows=False)
    ctrl = AutoController(None, hw, data, StateEstimator(5.0, model), cfg)
    ctrl.save_recovery_state = lambda state: None
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(time, "perf_counter", lambda: hw.virtual_time)
    original_init = ChargeController.__init__

    def accelerated_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.poll_interval_s = 0.0001
        self.params.stage_timeout_s = 1e9

    monkeypatch.setattr(ChargeController, "__init__", accelerated_init)
    cancel_sent = [False]

    def accelerated_sleep(ev, seconds):
        if not cancel_sent[0] and scenario == "cancel_charge" \
                and hw.charge_samples >= 8:
            cancel_sent[0] = True
            win._on_char_eta_cancel()
        elif not cancel_sent[0] and scenario == "cancel_reference" \
                and hw.load_cycles == 2 and hw.load_samples >= 3:
            cancel_sent[0] = True
            win._on_char_eta_cancel()
        return ev.wait(0.0001)

    monkeypatch.setattr(win, "_char_sleep", accelerated_sleep)
    hardware_stops = []
    original_stop = win._char_hw_stop

    def counted_stop():
        hardware_stops.append(True)
        original_stop()

    monkeypatch.setattr(win, "_char_hw_stop", counted_stop)
    prior_eta = {"q_in_ah": 5.0, "q_out_ah": 4.8,
                 "eta_coulomb_pct": 96.0, "status": "VALID", "valid": True}
    win._char_results["eta"] = prior_eta.copy()
    try:
        event = threading.Event()
        event.set()
        win._char_running["eta"] = event
        win.btn_char_eta_start.setEnabled(False)
        worker = threading.Thread(target=win._char_eta_thread, daemon=True)
        win._char_threads["eta"] = worker
        worker.start()
        deadline = time.monotonic() + 30.0
        while worker.is_alive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.0001)
        worker.join(timeout=0.1)
        app.processEvents()

        assert not worker.is_alive(), f"η worker did not exit for {scenario}"
        assert not event.is_set()
        assert not win._char_any_running()
        assert win.btn_char_eta_start.isEnabled()
        assert not hw._load_current
        assert not hw._psu_output_on
        assert hardware_stops
        assert not data.is_recording
        files = list((tmp_path / "sessions").glob("test_CoulombEfficiency_*.csv"))
        assert len(files) == 1
        csv_path = files[0]
        rows = csv_path.read_text(encoding="utf-8-sig").splitlines()
        assert len(rows) > 1
        assert csv_path.with_name(csv_path.name + ".meta.json").exists()
        meta = json.loads(csv_path.with_name(csv_path.name + ".meta.json")
                          .read_text(encoding="utf-8"))
        assert meta["test_type"] == "CoulombEfficiency"
        assert meta["session_id"] == data.session_id
        assert meta["protocol"]["id"] == "coulomb-efficiency-v1"
        assert DataHandler.verify_integrity(str(csv_path)) is True
        assert csv_path.with_name(csv_path.name + ".sha256").exists()
        assert not list(tmp_path.glob("*.pdf"))
        assert meta["eta_coulomb_pct"] is None
        assert meta["eta_coulomb_valid"] is False
        assert win._char_results["eta"]["eta_coulomb_pct"] is None
        assert win._char_results["eta"]["valid"] is False
        assert meta["termination_phase"] in {
            "CHARGE_CC", "CHARGE_CV", "CHARGE_TAPER", "REFERENCE_DISCHARGE"
        }

        if scenario == "cancel_charge":
            assert meta["status"] == "cancelled"
            assert meta["termination_status"] == "CANCELLED"
            assert meta["termination_cause"] == "cancellation"
            assert "cancel" in meta["termination_reason"].lower()
            assert meta["termination_phase"].startswith("CHARGE_")
            assert meta["full_charge_confirmed"] is False
            assert any(",CHARGE_CC," in row for row in rows)
            assert not any(",REFERENCE_DISCHARGE," in row for row in rows)
        elif scenario == "cancel_reference":
            assert meta["status"] == "cancelled"
            assert meta["termination_status"] == "CANCELLED"
            assert meta["termination_cause"] == "cancellation"
            assert "cancel" in meta["termination_reason"].lower()
            assert meta["termination_phase"] == "REFERENCE_DISCHARGE"
            assert meta["full_charge_confirmed"] is True
            assert any(",CHARGE_TAPER," in row for row in rows)
            assert any(",REFERENCE_DISCHARGE," in row for row in rows)
            assert meta["reference_cutoff_reached"] is False
            assert meta["reference_capacity_complete"] is False
            assert meta["integration"]["Qout"].get("complete") is False
        elif scenario == "comm_charge":
            assert meta["status"] == "fault"
            assert meta["termination_status"] == "ERROR"
            assert meta["termination_cause"] == "communication_failure"
            assert "communication failure" in meta["termination_reason"].lower()
            assert meta["termination_phase"].startswith("CHARGE_")
            assert meta["full_charge_confirmed"] is False
            assert any(",CHARGE_CC," in row for row in rows)
            assert not any(",REFERENCE_DISCHARGE," in row for row in rows)
            assert len(hardware_stops) == 1
        else:
            assert meta["status"] == "fault"
            assert meta["termination_status"] == "ERROR"
            assert meta["termination_cause"] == "communication_failure"
            assert "communication failure" in meta["termination_reason"].lower()
            assert meta["termination_phase"] == "REFERENCE_DISCHARGE"
            assert meta["full_charge_confirmed"] is True
            assert any(",REFERENCE_DISCHARGE," in row for row in rows)
            assert meta["reference_cutoff_reached"] is False
            assert meta["reference_capacity_complete"] is False
            assert meta["integration"]["Qout"].get("complete") is False
            assert len(hardware_stops) == 1
    finally:
        win.close()
        app.processEvents()
