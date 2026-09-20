"""Regression tests for the close-the-program-outputs-stay-on bug
("ปิดกากบาทแล้ว PSU/Load ยังจ่ายไฟ"): the X-close path must cut instrument
outputs first, verify them, stop running test threads, and stay retryable
when the hardware step fails.
"""
import os
import unittest
from unittest.mock import MagicMock, call

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestControllerShutdownCutsOutputsFirst(unittest.TestCase):
    def _make_controller(self):
        from aset_batt.app.auto_controller import AutoController
        ctrl = AutoController.__new__(AutoController)
        ctrl._shutdown_done = False
        ctrl.monitor_running = False
        ctrl.is_profile_running = False
        ctrl.is_charging = False
        ctrl._charge_ctrl = None
        ctrl.hw = MagicMock()
        ctrl.data = MagicMock()
        return ctrl

    def test_outputs_cut_before_anything_else(self):
        """_emergency_shutdown (psu_off/load_off) must run BEFORE
        hw.shutdown_all — the immediate cut must not wait for the flag/sleep
        dance, so even if a later step raises the bench is already safe."""
        ctrl = self._make_controller()
        order = []
        ctrl.hw.psu_off.side_effect = lambda: order.append("psu_off")
        ctrl.hw.load_off.side_effect = lambda: order.append("load_off")
        ctrl.hw.shutdown_all.side_effect = lambda: order.append("shutdown_all")
        ctrl.shutdown()
        self.assertIn("psu_off", order)
        self.assertIn("load_off", order)
        self.assertLess(order.index("psu_off"), order.index("shutdown_all"))
        self.assertLess(order.index("load_off"), order.index("shutdown_all"))

    def test_emergency_shutdown_cuts_ssr_before_waiting_for_instrument_io(self):
        """The independent relay cutoff must precede VISA-backed OFF commands."""
        ctrl = self._make_controller()
        order = []
        ctrl.hw.set_ssr.side_effect = lambda state: order.append(("ssr", state)) or True
        ctrl.hw.load_off.side_effect = lambda: order.append(("load_off",))
        ctrl.hw.psu_off.side_effect = lambda: order.append(("psu_off",))

        ctrl._emergency_shutdown()

        self.assertEqual(order[0], ("ssr", False))
        self.assertEqual(order[1:], [("load_off",), ("psu_off",)])

    def test_emergency_shutdown_attempts_every_path_and_records_failed_ssr(self):
        ctrl = self._make_controller()
        ctrl.hw.set_ssr.return_value = False
        ctrl.hw.load_off.side_effect = RuntimeError("VISA timeout")

        with self.assertLogs("aset_batt.app.auto_controller", level="CRITICAL") as logs:
            ctrl._emergency_shutdown()

        ctrl.hw.psu_off.assert_called_once()
        self.assertTrue(any("SSR OFF" in line for line in logs.output))
        self.assertTrue(any("incomplete" in line for line in logs.output))

    def test_failed_hardware_shutdown_is_retryable(self):
        """The idempotency latch must only be set on success: if
        hw.shutdown_all raised (instrument busy / USB hiccup), a second call
        (bootstrapper.cleanup) must actually retry, not be skipped."""
        ctrl = self._make_controller()
        ctrl.hw.shutdown_all.side_effect = RuntimeError("VISA timeout")
        ctrl.shutdown()
        self.assertFalse(ctrl._shutdown_done)
        ctrl.hw.shutdown_all.side_effect = None
        ctrl.shutdown()
        self.assertTrue(ctrl._shutdown_done)
        self.assertEqual(ctrl.hw.shutdown_all.call_count, 2)

    def test_unconfirmed_off_result_does_not_latch_shutdown_complete(self):
        ctrl = self._make_controller()
        ctrl.hw.shutdown_all.return_value = False
        ctrl.shutdown()
        self.assertFalse(ctrl._shutdown_done)

    def test_successful_shutdown_is_idempotent(self):
        ctrl = self._make_controller()
        ctrl.shutdown()
        ctrl.shutdown()
        self.assertEqual(ctrl.hw.shutdown_all.call_count, 1)

    def test_charge_ctrl_stop_failure_does_not_skip_hardware_cut(self):
        ctrl = self._make_controller()
        ctrl._charge_ctrl = MagicMock()
        ctrl._charge_ctrl.stop.side_effect = RuntimeError("boom")
        ctrl.shutdown()
        ctrl.hw.shutdown_all.assert_called_once()


class TestWriteOffVerified(unittest.TestCase):
    def _hw(self):
        from aset_batt.hardware.hardware_driver import HardwareController
        return HardwareController.__new__(HardwareController)

    def test_confirmed_off_returns_true_first_try(self):
        hw = self._hw()
        inst = MagicMock()
        inst.query.return_value = "0\n"
        self.assertTrue(hw._write_off_verified(inst, ":OUTP OFF", ":OUTP?", "PSU"))
        inst.write.assert_called_once_with(":OUTP OFF")

    def test_unconfirmed_state_retries_then_fails(self):
        hw = self._hw()
        inst = MagicMock()
        inst.query.return_value = "1"   # instrument insists it is still ON
        self.assertFalse(hw._write_off_verified(inst, ":OUTP OFF", ":OUTP?", "PSU"))
        self.assertEqual(inst.write.call_count, 2)

    def test_write_timeout_then_success_recovers_on_retry(self):
        hw = self._hw()
        inst = MagicMock()
        inst.write.side_effect = [RuntimeError("VISA timeout"), None]
        inst.query.return_value = "OFF"
        self.assertTrue(hw._write_off_verified(inst, ":INP OFF", ":INP?", "Load"))
        self.assertEqual(inst.write.call_count, 2)

    def test_disconnect_propagates_unconfirmed_output_state(self):
        import threading
        hw = self._hw()
        hw.is_connected = True
        hw.is_esp_connected = True
        hw.esp_serial = MagicMock()
        hw._esp_write_lock = threading.Lock()
        hw.inst_lock = threading.Lock()
        hw._psu_output_on = True
        hw.psu_inst = MagicMock()
        hw.load_inst = MagicMock()
        hw.psu_inst.query.return_value = "1"  # stays ON after both OFF attempts
        hw.load_inst.query.return_value = "0"

        self.assertFalse(hw.disconnect_instruments())


class TestCloseStopsRunningTestThreads(unittest.TestCase):
    def test_shutdown_services_clears_seq_and_char_run_flags(self):
        """closeEvent's "stop the test?" promise: a running sequence thread
        polls self._seq_running (and characterize threads their per-test
        events) — _shutdown_services must clear them so those loops exit
        instead of racing the output-off writes during interpreter teardown."""
        from aset_batt.ui import theme
        theme.set_theme("light")
        from PySide6.QtWidgets import QApplication
        from aset_batt.core.config import ConfigManager
        from aset_batt.ui.isa101_views import BatteryQtWindow
        app = QApplication.instance() or QApplication([])
        win = BatteryQtWindow(ConfigManager())
        try:
            win.controller = MagicMock()
            win._seq_running.set()
            import threading
            evt = threading.Event()
            evt.set()
            win._char_running["pk"] = evt
            win._shutdown_services()
            self.assertFalse(win._seq_running.is_set(),
                             "running sequence must be signalled to stop on close")
            self.assertFalse(evt.is_set(),
                             "running characterize test must be signalled to stop on close")
            win.controller.shutdown.assert_called_once()
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
