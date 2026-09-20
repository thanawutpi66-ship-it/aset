"""Dedicated software-only safety closure matrix.

These tests prove command ordering, independent failure handling, configuration
governance, and that every statically identified automated PEL phase has a
shared trip-check owner. They do not claim physical output confirmation.
"""
from pathlib import Path
from unittest.mock import MagicMock
import threading

import pytest

from aset_batt.app.auto_controller import AutoController
from aset_batt.core.config import ConfigManager
from aset_batt.ui.sequences.base import BaseSequenceMixin


def _controller_with_hw(hw):
    c = AutoController.__new__(AutoController)
    c.hw = hw
    c.safety_triggered = True
    return c


@pytest.mark.parametrize("failed", [set(), {"ssr"}, {"pel"}, {"psu"},
                                     {"ssr", "pel"}, {"ssr", "psu"},
                                     {"pel", "psu"}, {"ssr", "pel", "psu"}])
def test_safe_off_all_independent_paths_are_attempted(failed):
    hw = MagicMock()
    hw.set_ssr.return_value = False if "ssr" in failed else True
    hw.load_off.return_value = False if "pel" in failed else True
    hw.psu_off.return_value = False if "psu" in failed else True
    _controller_with_hw(hw)._emergency_shutdown()
    hw.set_ssr.assert_called_once_with(False)
    hw.load_off.assert_called_once_with()
    hw.psu_off.assert_called_once_with()


def test_safe_off_exception_does_not_suppress_remaining_paths():
    hw = MagicMock()
    hw.set_ssr.side_effect = OSError("serial")
    hw.load_off.return_value = False
    hw.psu_off.return_value = True
    _controller_with_hw(hw)._emergency_shutdown()
    hw.load_off.assert_called_once_with()
    hw.psu_off.assert_called_once_with()


def test_effective_limits_are_pack_based_and_gate_conflicts(tmp_path):
    cfg = ConfigManager(str(tmp_path / "config.json"))
    cfg.battery.cells_series = 6
    cfg.battery.nominal_voltage = 2.0
    cfg.system.safety_limits.update(max_voltage=15.0, min_voltage=10.0)
    effective = cfg.effective_safety_limits()
    assert effective["basis"]["ovp_v"] == "pack"
    assert effective["ovp_v"] == 15.0
    assert cfg.validate_effective_safety_limits() == []
    cfg.system.safety_limits["min_voltage"] = 16.0
    assert cfg.validate_effective_safety_limits()


def test_pel_phase_inventory_has_shared_trip_checks():
    root = Path(__file__).parents[1] / "aset_batt" / "ui"
    sources = {
        "quick": (root / "sequences" / "quick_scan.py").read_text(encoding="utf-8"),
        "iec": (root / "sequences" / "iec_capacity.py").read_text(encoding="utf-8"),
        "hppc": (root / "sequences" / "hppc.py").read_text(encoding="utf-8"),
        "cycle": (root / "sequences" / "cycle_life.py").read_text(encoding="utf-8"),
        "characterize": (root / "characterize.py").read_text(encoding="utf-8"),
    }
    for name in ("quick", "iec", "hppc", "cycle"):
        assert "_seq_check_load_trip" in sources[name]
    assert "_char_check_safety" in sources["characterize"]


@pytest.mark.parametrize("status", [False, True, None])
def test_pel_trip_helper_runtime_matrix(status):
    host = BaseSequenceMixin()
    host._seq_running = threading.Event(); host._seq_running.set()
    host.hw = MagicMock()
    host.hw.get_load_protection_tripped.return_value = status
    host.controller = MagicMock()
    host.sig_alarm = MagicMock(); host.sig_wf_status = MagicMock()
    result = host._seq_check_load_trip()
    if status is False:
        assert result is True and host._seq_running.is_set()
        host.controller._trigger_safety.assert_not_called()
    else:
        assert result is False and not host._seq_running.is_set()
        host.controller._trigger_safety.assert_called_once()
        assert "LOAD_PROTECTION" in host.controller._trigger_safety.call_args.args[0]


@pytest.mark.parametrize("valid", [True, False])
def test_temperature_helper_runtime_fail_closed(valid):
    host = BaseSequenceMixin()
    host._seq_running = threading.Event(); host._seq_running.set()
    host.hw = MagicMock()
    host.hw.temperature_measurement.return_value = {
        "temperature_valid": valid, "temperature_status": "VALID" if valid else "STALE"
    }
    host.hw.temp_is_stale.return_value = not valid
    host.controller = MagicMock()
    host.sig_alarm = MagicMock(); host.sig_wf_status = MagicMock()
    result = host._seq_check_temp_stale()
    assert result is valid
    if not valid:
        assert not host._seq_running.is_set()
        host.controller._trigger_safety.assert_called_once()


def test_watchdog_contract_is_unchanged():
    updater = (Path(__file__).parents[1] / "aset_batt" / "ui" / "views" / "hardware_control.py").read_text(encoding="utf-8")
    firmware = (Path(__file__).parents[1] / "firmware" / "esp32_temp_ssr" / "esp32_temp_ssr.ino").read_text(encoding="utf-8")
    assert "feed_watchdog" in updater
    assert "WATCHDOG_TIMEOUT_MS = 20000" in firmware
