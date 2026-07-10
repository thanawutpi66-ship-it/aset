"""Safety-shutdown wiring tests (ก.ค. 2026 safety audit).

ครอบเส้นทาง "ตัดไฟให้ได้เสมอ" ที่เพิ่งแก้:
- _seq_hw_safe_off(): ทุก sequence thread ต้องตัด charge + load + PSU ใน finally
  แม้บางคำสั่งจะ raise เอง
- _char_check_safety(): CHARACTERIZE ต้อง abort เทสต์ของตัวเอง (event ราย-เทสต์)
  เมื่อ OTP เกิน หรือ temp stale จนมองไม่เห็นความร้อนจริง
- _shutdown_services(): ปิดหน้าต่างต้องหยุดเธรดทดสอบทุกชนิดก่อนตัด controller
- signal handler ของ bootstrapper: ตัดไฟก่อน แล้วค่อยขอให้ Qt จบ loop

เขียนแบบเรียก method ตรงๆ บน object ประกอบเอง (pattern เดียวกับ
tests/test_graph_feed_during_sequences.py) — ไม่ start เธรดจริง
"""
import math
import threading
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# _seq_hw_safe_off  (aset_batt/ui/sequences/base.py)
# ---------------------------------------------------------------------------

def _make_seq_host():
    from aset_batt.ui.sequences.base import BaseSequenceMixin

    class Host(BaseSequenceMixin):
        def __init__(self):
            self.hw = MagicMock()
            self.controller = MagicMock()

    return Host()


def test_seq_hw_safe_off_cuts_charge_load_and_psu():
    host = _make_seq_host()
    host._seq_hw_safe_off()
    host.controller.stop_charge.assert_called_once()
    host.hw.load_off.assert_called_once()
    host.hw.psu_off.assert_called_once()


def test_seq_hw_safe_off_continues_past_failures():
    """stop_charge ระเบิด → ยังต้องพยายาม load_off + psu_off ต่อ (และกลับกัน)"""
    host = _make_seq_host()
    host.controller.stop_charge.side_effect = RuntimeError("VISA I/O")
    host.hw.load_off.side_effect = RuntimeError("VISA I/O")
    host._seq_hw_safe_off()          # ต้องไม่ raise
    host.hw.psu_off.assert_called_once()


def test_seq_hw_safe_off_no_controller():
    host = _make_seq_host()
    host.controller = None
    host._seq_hw_safe_off()          # ต้องไม่ raise
    host.hw.psu_off.assert_called_once()


# ---------------------------------------------------------------------------
# _char_check_safety  (aset_batt/ui/characterize.py)
# ---------------------------------------------------------------------------

def _make_char_host(otp_limit=60.0, stale_trip=False, stale_warn=False):
    from aset_batt.ui.characterize import CharacterizeMixin

    class Host(CharacterizeMixin):
        def __init__(self):
            self.hw = MagicMock()
            # temp_is_stale(trip_s) → sustained trip; temp_is_stale() → warn-only
            self.hw.temp_is_stale = (
                lambda trip_s=None: stale_trip if trip_s else stale_warn)
            self.sig_alarm = MagicMock()
            self.controller = MagicMock()
            self._SEQ_TEMP_STALE_TRIP_S = 60.0

        def _otp_limit(self):
            return otp_limit

    return Host()


def test_char_safety_ok_path():
    host = _make_char_host()
    ev = threading.Event()
    ev.set()
    assert host._char_check_safety(ev, 25.0) is True
    assert ev.is_set()


def test_char_safety_otp_trip_clears_own_event():
    host = _make_char_host(otp_limit=60.0)
    ev = threading.Event()
    ev.set()
    assert host._char_check_safety(ev, 61.5) is False
    assert not ev.is_set()
    assert any("OTP" in str(c) for c in host.sig_alarm.emit.call_args_list)


def test_char_safety_nan_temp_does_not_false_trip():
    host = _make_char_host()
    ev = threading.Event()
    ev.set()
    assert host._char_check_safety(ev, float("nan")) is True
    assert ev.is_set()


def test_char_safety_sustained_stale_aborts():
    host = _make_char_host(stale_trip=True)
    ev = threading.Event()
    ev.set()
    assert host._char_check_safety(ev, 25.0) is False
    assert not ev.is_set()


def test_char_safety_brief_stale_warns_once_but_continues():
    host = _make_char_host(stale_warn=True)
    ev = threading.Event()
    ev.set()
    assert host._char_check_safety(ev, 25.0) is True
    assert ev.is_set()
    warn_calls = [c for c in host.sig_alarm.emit.call_args_list
                  if "WARNING" in str(c)]
    assert len(warn_calls) == 1
    # เรียกซ้ำ — ต้องไม่เตือนซ้ำ
    assert host._char_check_safety(ev, 25.0) is True
    warn_calls = [c for c in host.sig_alarm.emit.call_args_list
                  if "WARNING" in str(c)]
    assert len(warn_calls) == 1


# ---------------------------------------------------------------------------
# _shutdown_services stops test threads  (aset_batt/ui/views/dialogs.py)
# ---------------------------------------------------------------------------

def test_shutdown_services_stops_all_test_threads():
    from aset_batt.ui.views.dialogs import DialogsMixin

    class Host(DialogsMixin):
        def __init__(self):
            self._seq_running = threading.Event()
            self._seq_running.set()
            self._char_running = {"pk": threading.Event(), "eta": threading.Event()}
            for ev in self._char_running.values():
                ev.set()
            self._test_worker = MagicMock()
            self.controller = MagicMock()

    host = Host()
    with patch("aset_batt.acquisition.analysis.shutdown_analysis_pool"):
        host._shutdown_services()

    assert not host._seq_running.is_set()
    assert all(not ev.is_set() for ev in host._char_running.values())
    host._test_worker.stop.assert_called_once()
    host.controller.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# bootstrapper signal handler + cleanup idempotency
# ---------------------------------------------------------------------------

def test_bootstrapper_cleanup_is_idempotent():
    from aset_batt.app.app_bootstrapper import ApplicationBootstrapper

    b = ApplicationBootstrapper()
    b.event_handler = MagicMock()
    b.cleanup()
    b.cleanup()
    b.event_handler.stop.assert_called_once()


def test_emergency_hw_off_uses_controller_emergency_shutdown():
    from aset_batt.app.app_bootstrapper import ApplicationBootstrapper
    from aset_batt.app.auto_controller import AutoController
    from aset_batt.services.service_locator import ServiceLocator

    b = ApplicationBootstrapper()
    controller = MagicMock(spec=AutoController)
    ServiceLocator.register(AutoController, controller)
    try:
        b._emergency_hw_off()
        controller._emergency_shutdown.assert_called_once()
    finally:
        ServiceLocator.clear()


def test_emergency_hw_off_without_controller_is_noop():
    from aset_batt.app.app_bootstrapper import ApplicationBootstrapper
    from aset_batt.services.service_locator import ServiceLocator

    ServiceLocator.clear()
    b = ApplicationBootstrapper()
    b._emergency_hw_off()   # ต้องไม่ raise แม้ยังไม่มี controller
