"""
PySide6 ISA-101 HMI for ASET Battery Tester.

This is the supported desktop UI for the main application. It keeps the
existing controller / estimator / analysis contracts, but presents them in the
desaturated high-performance style used by the standalone command center.
"""

import csv
import logging
import math
import os
import threading
import webbrowser
from collections import deque
from datetime import datetime
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt, QThread, QRunnable, QThreadPool, QLocale, QByteArray
from PySide6.QtSvgWidgets import QSvgWidget

from aset_batt.acquisition.models import TestConfig, OperationMode, BatteryProfile as AcqProfile
from aset_batt.acquisition.backends import HardwareBackend
from aset_batt.acquisition.worker import AcquisitionWorker
import re
from PySide6.QtGui import QColor, QDoubleValidator, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication, QToolBar,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QProgressBar,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import aset_batt.core.battery_profiles as battery_profiles
from aset_batt.core.analysis_module import ChemistryDetector
from aset_batt.core.iec61960_standard import IEC61960Standard

logger = logging.getLogger(__name__)

# ISA-101 palette: neutral gray shell with color reserved for state/alarm only.
from aset_batt.ui import theme

from aset_batt.ui.widgets import (
    _btn, _hline, QtRootShim,
    MultiAxisTrend, SplitTrend, TripleTrend, TrendContainer,
    _PdfNotifier, _PdfTask,
)
from aset_batt.ui.report_html import format_seq_result, build_results_html
from aset_batt.ui.zones import ZonesMixin
from aset_batt.ui.sequences import SequencesMixin
from aset_batt.ui.characterize import CharacterizeMixin


class HardwareControlMixin:
    def _submit_hardware_task(self, name, operation):
        """Submit blocking GUI-initiated VISA/serial work to the single worker lane."""
        if name in self._hardware_tasks:
            raise RuntimeError(f"hardware task already active: {name}")
        from aset_batt.ui.hardware_tasks import HardwareTask
        task = HardwareTask(name, operation)
        task.signals.finished.connect(self.sig_hw_task_done)
        self._hardware_tasks[name] = task
        self._hardware_pool.start(task)

    def _request_watchdog(self):
        if not getattr(self.hw, "is_esp_connected", False):
            return
        self._watchdog_requests = getattr(self, "_watchdog_requests", 0) + 1
        if "watchdog" in self._hardware_tasks:
            self._watchdog_skipped = getattr(self, "_watchdog_skipped", 0) + 1
            return
        self._submit_hardware_task("watchdog", self.hw.feed_watchdog)

    def _refresh_ports(self):
        if self.hw is None or self._hardware_ports_busy or self._hardware_connecting:
            return
        self._hardware_ports_busy = True
        self.btn_connect.setEnabled(False)
        def discover():
            visa = self.hw.get_visa_ports() if hasattr(self.hw, "get_visa_ports") else []
            coms = self.hw.get_com_ports() if hasattr(self.hw, "get_com_ports") else []
            return visa, coms
        self._submit_hardware_task("ports", discover)

    def _apply_discovered_ports(self, visa, coms):
        try:
            for cb, items in ((self.cb_psu, visa), (self.cb_load, visa), (self.cb_esp, coms)):
                cb.clear()
                cb.addItems(items)
            # Restore saved selections from config; fall back to positional defaults
            hw = self.config.hardware if self.config else None
            def _restore(cb, saved):
                if saved:
                    idx = cb.findText(saved)
                    if idx >= 0:
                        cb.setCurrentIndex(idx)
            if hw:
                _restore(self.cb_psu, hw.psu_port)
                _restore(self.cb_load, hw.load_port)
                _restore(self.cb_esp, hw.esp_port)
            elif len(visa) > 1:
                self.cb_load.setCurrentIndex(1)
        except Exception as exc:
            logger.error("refresh ports: %s", exc)
        self.btn_connect.setEnabled(bool(self.cb_psu.currentText() and self.cb_load.currentText()))
    def _refresh_battery_readout(self):
        b = self.config.battery
        self.lbl_battery_readout.setText(
            f"{b.battery_type} · {b.cells_series}S{b.cells_parallel}P · {b.pack_nominal_voltage:.1f}V · {b.rated_capacity:.1f}Ah"
        )
        # Product selection / Detect Chemistry can update safety_limits behind
        # the operator's back (see _on_product_changed) — push those values
        # into the SETUP-tab spinboxes too, or they'd show stale numbers until
        # the next manual edit. blockSignals: these spinboxes have no
        # valueChanged wiring today, but avoids surprises if that ever changes.
        if hasattr(self, "spn_ovp") and self.config.system.safety_limits:
            limits = self.config.system.safety_limits
            for spn, key, default in (
                (self.spn_ovp, "max_voltage", 15.0),
                (self.spn_uvp, "min_voltage", 10.0),
                (self.spn_max_current, "max_current", 100.0),
                (self.spn_otp, "max_temperature", 60.0),
                (self.spn_utp, "min_temperature", -10.0),
            ):
                spn.blockSignals(True)
                spn.setValue(float(limits.get(key, default)))
                spn.blockSignals(False)
    def _on_save_safety_limits(self):
        ovp, uvp = self.spn_ovp.value(), self.spn_uvp.value()
        otp, utp = self.spn_otp.value(), self.spn_utp.value()
        if ovp <= uvp:
            if not self._headless:
                QMessageBox.warning(self, "Safety Limits",
                    f"OVP ({ovp:.2f}V) ต้องมากกว่า UVP ({uvp:.2f}V)")
            return
        if otp <= utp:
            if not self._headless:
                QMessageBox.warning(self, "Safety Limits",
                    f"OTP ({otp:.1f}°C) ต้องมากกว่า UTP ({utp:.1f}°C)")
            return

        if self.config.system.safety_limits is None:
            self.config.system.safety_limits = {}
        self.config.system.safety_limits["max_voltage"] = ovp
        self.config.system.safety_limits["min_voltage"] = uvp
        self.config.system.safety_limits["max_current"] = self.spn_max_current.value()
        self.config.system.safety_limits["max_temperature"] = otp
        self.config.system.safety_limits["min_temperature"] = utp
        self.config.save_config()
        self._log_alarm(
            f"Safety limits saved — OVP {ovp:.2f}V, UVP {uvp:.2f}V, "
            f"OTP {otp:.1f}°C, UTP {utp:.1f}°C")

    def _request_direct_poll(self):
        """Queue at most one Direct-page read; the GUI timer never queries SCPI."""
        import time
        self._direct_poll_requests += 1
        if (self._direct_poll_inflight or self._hardware_connecting
                or self._hardware_disconnecting):
            self._direct_poll_skipped += 1
            return
        self._direct_poll_inflight = True
        self._direct_poll_started_at = time.perf_counter()
        if self.rb_direct.isChecked() and self._direct_last_success_at is None:
            self.status_label.setText("Direct readback pending")

        def read_direct():
            v, psu_i, load_i = self.hw.read_vi()
            if load_i > 0.02:
                i_net = load_i
            elif getattr(self.hw, "_psu_output_on", False):
                i_net = -psu_i
            else:
                i_net = psu_i
            temp = getattr(self.hw, "current_temp", float("nan"))
            temp_stale = bool(getattr(self.hw, "temp_is_stale", lambda: False)())
            if temp_stale:
                temp = float("nan")
            return float(v), float(i_net), float(temp), time.perf_counter(), temp_stale

        self._submit_hardware_task("direct_poll", read_direct)

    def _mark_direct_stale_if_needed(self):
        import time
        if not self.rb_direct.isChecked():
            return
        last = self._direct_last_success_at
        reference = last if last is not None else self._direct_poll_started_at
        stale = bool(reference is not None
                     and time.perf_counter() - reference > self._direct_stale_after_s)
        self._direct_stale_notified = stale
        if stale:
            if last is None:
                self.status_label.setText("Direct readback unavailable (>3 s; no successful sample)")
            else:
                self.status_label.setText("Direct readback stale (>3 s); last values retained")
        elif last is not None:
            self.status_label.setText(
                "Readback current; ESP32 temperature is stale"
                if self._direct_temp_stale else "Direct readback current")
        elif self._direct_poll_inflight:
            self.status_label.setText("Direct readback pending")

    def direct_poll_diagnostics(self):
        """Snapshot Direct poll counters and sample freshness for support/audit."""
        import time
        last = self._direct_last_success_at
        age = max(0.0, time.perf_counter() - last) if last is not None else None
        pending_age = None
        if last is None and self._direct_poll_started_at is not None:
            pending_age = max(0.0, time.perf_counter() - self._direct_poll_started_at)
        stale = bool((age is not None and age > self._direct_stale_after_s)
                     or (pending_age is not None and pending_age > self._direct_stale_after_s))
        return {
            "requested": self._direct_poll_requests,
            "executed": self._direct_poll_executed,
            "skipped_busy": self._direct_poll_skipped,
            "in_flight": self._direct_poll_inflight,
            "last_success_monotonic_s": last,
            "last_success_age_s": age,
            "pending_age_s": pending_age,
            "stale_after_s": self._direct_stale_after_s,
            "stale": stale,
            "last_sample": self._direct_last_sample,
        }
    def _on_connect(self):
        if (self._hardware_connecting or self._hardware_disconnecting
                or self._hardware_ports_busy or self._close_after_hardware_task
                or self.operation_state.owns_hardware
                or getattr(self, "_seq_running", threading.Event()).is_set()
                or any(ev.is_set() for ev in getattr(self, "_char_running", {}).values())):
            return
        psu, load, esp = self.cb_psu.currentText(), self.cb_load.currentText(), self.cb_esp.currentText()
        if not psu or not load:
            if not self._headless:
                QMessageBox.warning(self, "Connect", "Select PSU and Load ports first")
            return
        self._load_calibration()
        self._hardware_connecting = True
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(False)
        self.status_label.setText("Connecting…")

        def connect_hardware():
            try:
                self.hw.connect_instruments(psu, load)
                setup = {}
                if hasattr(self.hw, "apply_default_safety_protection"):
                    setup = self.hw.apply_default_safety_protection(
                        max_current_a=self.config.battery.max_current,
                        pack_max_voltage_v=self.config.battery.pack_max_voltage,
                        min_voltage_v=self.config.system.safety_limits.get("min_voltage", 0.0),
                    ) or {}
                esp_error = ""
                if esp:
                    try:
                        baud = getattr(self.config.hardware, "serial_baudrate", 9600)
                        self.hw.connect_esp32(esp, baudrate=baud)
                        if hasattr(self.hw, "esp_connect_error"):
                            self.hw.esp_connect_error = ""
                    except Exception as exc:
                        esp_error = str(exc)
                        if hasattr(self.hw, "esp_connect_error"):
                            self.hw.esp_connect_error = esp_error
                return {"psu": psu, "load": load, "esp": esp,
                        "setup": setup, "esp_error": esp_error}
            except Exception:
                try:
                    self.hw.disconnect_esp32()
                except Exception:
                    pass
                try:
                    self.hw.disconnect_instruments()
                except Exception:
                    pass
                raise

        self._submit_hardware_task("connect", connect_hardware)

    def _on_disconnect(self):
        if self._hardware_disconnecting or self._hardware_connecting:
            return
        if (self.operation_state.owns_hardware
                or getattr(self.controller, "is_charging", False)
                or getattr(self.controller, "monitor_running", False)
                or getattr(self, "_test_thread", None) is not None
                or (getattr(self, "_seq_thread", None) is not None
                    and self._seq_thread.is_alive())
                or any(t.is_alive() for t in getattr(self, "_char_threads", {}).values())):
            self._log_alarm("HARDWARE_BUSY — wait for worker cleanup before disconnecting")
            return
        self._hardware_disconnecting = True
        self._direct_poll_enabled = False
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.setEnabled(False)
        if self.controller is not None:
            self.controller.stop_live_readback()
        self._cloud_push_stop()

        def disconnect_hardware():
            if hasattr(self.hw, "release_instrument_config"):
                self.hw.release_instrument_config()
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            return True

        self._submit_hardware_task("disconnect", disconnect_hardware)

    @Slot(str, object)
    def _on_hardware_task_done(self, name, result):
        self._hardware_tasks.pop(name, None)
        ok = bool(result.get("ok"))
        value = result.get("value")
        if name == "ports":
            self._hardware_ports_busy = False
            if ok:
                visa, coms = value
                self._apply_discovered_ports(visa, coms)
            else:
                logger.error("Port discovery failed: %s", result.get("error"))
                self._apply_discovered_ports([], [])
            self.status_label.setText("Ready — connect hardware to begin")
        elif name == "simulation_autoconnect":
            if ok and value:
                self._update_connection_status()
                if self.controller is not None:
                    self.controller.start_live_readback()
                logger.info("Auto-connected mock hardware (simulation mode)")
            elif not ok:
                logger.warning("Simulation auto-connect failed: %s", result.get("error"))
        elif name == "connect":
            self._hardware_connecting = False
            if ok:
                setup = value.get("setup", {})
                for warning in setup.get("warnings", []):
                    self._log_alarm(warning)
                info = setup.get("info") or {}
                if info.get("psu"):
                    self._log_alarm(f"PSU: {info['psu']}")
                if info.get("load"):
                    self._log_alarm(f"Load: {info['load']}")
                esp_error = value.get("esp_error", "")
                if esp_error:
                    self._log_alarm(f"ESP32 connect failed (non-fatal): {esp_error}")
                self.config.hardware.psu_port = value["psu"]
                self.config.hardware.load_port = value["load"]
                self.config.hardware.esp_port = value["esp"]
                self.config.save_config()
                self.set_profile_status("Idle")
                self._log_alarm("Hardware connected.")
                self._cloud_push_start()
                if self.controller is not None and not self._close_after_hardware_task:
                    self.controller.start_live_readback()
            else:
                self.hw.connect_error = result.get("error", "Hardware connection failed")
                self._log_alarm(f"Hardware connection failed: {self.hw.connect_error}")
                if not self._headless:
                    QMessageBox.critical(self, "Connection Failed", self.hw.connect_error)
            self._update_connection_status()
            self.btn_disconnect.setEnabled(bool(getattr(self.hw, "is_connected", False)))
            self.btn_connect.setEnabled(bool(self.cb_psu.currentText() and self.cb_load.currentText()))
        elif name == "disconnect":
            self._hardware_disconnecting = False
            if not ok:
                self._log_alarm(f"Disconnect failed: {result.get('error')}")
            else:
                self._log_alarm("Hardware disconnected.")
            self._update_connection_status()
            self.btn_connect.setEnabled(bool(self.cb_psu.currentText() and self.cb_load.currentText()))
            self.btn_disconnect.setEnabled(bool(getattr(self.hw, "is_connected", False)))
        elif name == "direct_poll":
            self._direct_poll_inflight = False
            self._direct_poll_executed += 1
            if ok:
                v, i, temp, sample_time, temp_stale = value
                self._direct_last_success_at = sample_time
                self._direct_last_sample = (v, i, temp)
                self._direct_stale_notified = False
                self._direct_temp_stale = bool(temp_stale)
                if self.rb_direct.isChecked() and not self._hardware_disconnecting:
                    soc = getattr(self.estimator, "soc", 0.0) if self.estimator else 0.0
                    rin = getattr(self.estimator, "rin", 0.0) if self.estimator else 0.0
                    self.update_live_readback(v, i, temp)
                    self.update_display(v, i, soc, rin, temp)
                    if temp_stale:
                        self.status_label.setText("Readback current; ESP32 temperature is stale")
                    else:
                        self.status_label.setText("Direct readback current")
            else:
                logger.warning("Direct readback failed: %s", result.get("error"))
                if self._direct_last_success_at is None:
                    self.status_label.setText("Direct readback unavailable")
            if self._close_after_hardware_task:
                self.close()
        elif name in ("manual_psu", "manual_load", "manual_ssr"):
            self._set_manual_command_busy({"manual_psu":"psu", "manual_load":"load", "manual_ssr":"ssr"}[name], False)
            if not ok:
                self._log_alarm(f"{name} failed: {result.get('error')}")
            elif not bool(value):
                self._log_alarm(f"{name} command failed")
            else:
                self._log_alarm(f"{name} command completed")
                if name == "manual_load":
                    try:
                        from aset_batt.storage.cloud_push import set_cloud_meta
                        set_cloud_meta(phase="discharge" if value else "", test_mode="MANUAL" if value else "", workflow="Manual — Direct Load" if value else "")
                    except Exception:
                        pass
                if bool(value):
                    if hasattr(self, "_ensure_battery_sn"):
                        self._ensure_battery_sn()
                    self.sig_profile_status.emit("RUN", theme.INFO)
            self._update_connection_status()
        elif name == "psu_trip_query":
            if ok:
                self._psu_tripped = bool(value)
                self.lbl_psu_trip.setText("Trip: ⛔ TRIPPED (OVP/OCP/OTP)" if value else "Trip: OK")
                self.lbl_psu_trip.setStyleSheet(self._psu_trip_style())
            else:
                self._log_alarm(f"PSU trip query failed: {result.get('error')}")
        elif name == "psu_trip_clear":
            if ok:
                cleared, tripped = value
                self._log_alarm("PSU protection trip cleared (operator)." if cleared else "Clear PSU trip failed.")
                self._psu_tripped = bool(tripped)
                self.lbl_psu_trip.setText("Trip: ⛔ TRIPPED (OVP/OCP/OTP)" if tripped else "Trip: OK")
                self.lbl_psu_trip.setStyleSheet(self._psu_trip_style())
            else:
                self._log_alarm(f"Clear PSU trip failed: {result.get('error')}")
        elif name == "shutdown":
            self._close_hardware_shutdown_done = True
        elif name == "watchdog":
            if not ok or not value:
                self._watchdog_failures = getattr(self, "_watchdog_failures", 0) + 1
                logger.warning("ESP32 watchdog heartbeat failed: %s", result.get("error"))
        if self._close_after_hardware_task and not self._hardware_tasks:
            self.close()

    def _on_ssr_manual_on(self):
        """Manual SSR override for diagnostics/recovery — normally the relay is
        driven automatically by set_psu()/charge state. This only closes the
        physical relay; it does NOT start a test or turn the PSU output on by
        itself, but if the PSU output was already left ON, current will start
        flowing the instant this closes — hence the confirmation."""
        if not getattr(self.hw, "is_esp_connected", False):
            return
        if not self._headless:
            reply = QMessageBox.warning(
                self, "Manual SSR ON",
                "สั่งปิดวงจร SSR ตรงๆ (ไม่ผ่านการควบคุมอัตโนมัติ)\n\n"
                "ใช้สำหรับ diagnostic/recovery เท่านั้น — ถ้า PSU output ยังเปิดค้างอยู่ "
                "กระแสจะไหลทันทีที่กดยืนยัน\n\nยืนยันจะสั่ง SSR ON ตรงๆ หรือไม่?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._queue_manual_ssr(True)
    def _on_ssr_manual_off(self):
        """Manual SSR cutoff — always safe (cuts power), no confirmation needed,
        same immediacy as E-STOP."""
        if not getattr(self.hw, "is_esp_connected", False):
            return
        self._queue_manual_ssr(False)

    def _queue_manual_ssr(self, on):
        if "manual_ssr" in self._hardware_tasks:
            return
        self._set_manual_command_busy("ssr", True)
        self._submit_hardware_task("manual_ssr", lambda: self.hw.set_ssr(bool(on)))
    def _on_direct_toggled(self, on: bool):
        if not on:
            return
        # Same guard as _psu_manual/_load_manual (see their comment) — refuses entry
        # into the tab at all rather than letting the operator in and then rejecting
        # the ON action, which would be a confusing dead end.
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(
                    self, "Direct Control",
                    f"{busy}\nหยุดก่อนแล้วค่อยใช้ Direct Control"
                )
            # Revert radio selection back to whichever page was showing.
            idx = self.run_stack.currentIndex()
            [self.rb_charge, self.rb_discharge, self.rb_hppc][min(idx, 2)].setChecked(True)
            return
        self.run_stack.setCurrentIndex(3)
        if getattr(self.hw, "is_connected", False):
            self._mark_direct_stale_if_needed()
            self._request_direct_poll()
    def _psu_manual(self, on):
        # _seq_running alone missed RUN TEST (AcquisitionWorker) and CHARACTERIZE-tab
        # tests, which drive self.hw from their own background thread exactly like a
        # sequence does — an operator clicking Manual PSU ON while one of those was
        # active could issue a conflicting SCPI command to the same instrument mid-test.
        # _busy_reason() already covers all three entry points (see its own docstring).
        if on:
            busy = self._busy_reason()
            if busy:
                if not self._headless:
                    QMessageBox.warning(self, "Direct Control",
                                        f"{busy} — หยุดก่อนแล้วค่อยใช้ Direct Control")
                return
        try:
            args = (True, str(float(self.ed_psu_v.text())), str(float(self.ed_psu_i.text()))) if on else (False,)
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "PSU", "Invalid voltage / current")
            return
        if "manual_psu" in self._hardware_tasks:
            return
        self._set_manual_command_busy("psu", True)
        self._submit_hardware_task("manual_psu", lambda: self.hw.set_psu(*args))
    def _load_manual(self, on):
        # See _psu_manual's comment — same interlock gap, same fix.
        if on:
            busy = self._busy_reason()
            if busy:
                if not self._headless:
                    QMessageBox.warning(self, "Direct Control",
                                        f"{busy} — หยุดก่อนแล้วค่อยใช้ Direct Control")
                return
        try:
            current = str(float(self.ed_load_a.text())) if on else "0"
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "Load", "Invalid current")
            return
        if "manual_load" in self._hardware_tasks:
            return
        self._set_manual_command_busy("load", True)
        self._submit_hardware_task("manual_load", lambda: self.hw.set_load(on, current))
    def _on_check_psu_trip(self):
        if not hasattr(self.hw, "get_psu_protection_tripped"):
            return
        tripped = bool(self.hw.get_psu_protection_tripped())
        self._psu_tripped = bool(tripped)
        self.lbl_psu_trip.setText(
            "Trip: ⛔ TRIPPED (OVP/OCP/OTP)" if tripped else "Trip: OK")
        # Shared fn the theme.style() registry replays on retheme — the cached
        # _psu_tripped flag above is what picks CRIT/OK.
        self.lbl_psu_trip.setStyleSheet(self._psu_trip_style())
    def _on_clear_psu_trip(self):
        """Deliberate operator action — a trip means something real happened
        (see harden_instrument_config), so this is never auto-retried by software."""
        if not hasattr(self.hw, "clear_psu_protection"):
            return
        if not self._headless:
            reply = QMessageBox.warning(
                self, "Clear PSU Protection Trip",
                "ล้างสถานะ OVP/OCP/OTP ของ PSU\n\n"
                "ใช้เฉพาะหลังตรวจสอบแล้วว่าสาเหตุที่ trip ได้รับการแก้ไขแล้วจริงๆ "
                "(เช่น ต่อสายผิด/โหลดเกิน) — ไม่งั้นอาจ trip ซ้ำทันที\n\nยืนยันล้าง trip?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.hw.clear_psu_protection()
        self._on_check_psu_trip()

    def _set_manual_command_busy(self, kind, busy):
        for attr in (f"btn_{kind}_on", f"btn_{kind}_off"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setEnabled(not busy)

    def _on_check_load_trip(self):
        if not hasattr(self.hw, "get_load_protection_tripped"):
            return
        try:
            status = self.hw.get_load_protection_tripped()
            if status is None:
                self.lbl_load_trip.setText("Trip: UNKNOWN (status query failed)")
                self.lbl_load_trip.setStyleSheet(f"color:{theme.CRIT}; font-weight:600;")
                self._log_alarm("CRITICAL: PEL protection status unavailable")
                return
            tripped = bool(status)
        except Exception as exc:
            self.lbl_load_trip.setText("Trip: UNKNOWN (status query failed)")
            self.lbl_load_trip.setStyleSheet(f"color:{theme.CRIT}; font-weight:600;")
            self._log_alarm(f"CRITICAL: PEL protection status unavailable: {exc}")
            return
        self._load_tripped = tripped
        self.lbl_load_trip.setText("Trip: ⛔ TRIPPED" if tripped else "Trip: OK")
        self.lbl_load_trip.setStyleSheet(
            f"color:{theme.CRIT if tripped else theme.OK}; font-weight:600;")

    def _on_clear_load_trip(self):
        if not hasattr(self.hw, "clear_load_protection"):
            return
        if not self._headless:
            reply = QMessageBox.warning(
                self, "Clear E-Load Protection Trip",
                "Clear the PEL protection event only after investigating and correcting its cause?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        ok = bool(self.hw.clear_load_protection())
        self._log_alarm("PEL protection trip cleared (operator)." if ok else
                        "Clear PEL protection trip failed.")
        self._on_check_load_trip()
    def _on_ocv_calibrate(self):
        """ปิด PSU+Load แล้วรอให้แรงดันนิ่ง (ΔV/Δt criterion) ก่อนคำนวณ SoC"""
        if self.controller is None or not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "OCV", "Connect hardware first")
            return
        if getattr(self.controller, "is_charging", False):
            if not self._headless:
                QMessageBox.warning(self, "OCV", "Stop charging before OCV calibration")
            return

        chemistry = getattr(self.controller.config.battery, "battery_type", "LiPO")
        _min_labels = {"LeadAcid": "5 นาที", "LiFePO4": "2 นาที"}
        min_label = _min_labels.get(chemistry, "1 นาที")

        self.sig_loading.emit("btn_ocv", True, "Settling…")
        self.sig_charge_status.emit(
            f"OCV: ปิดอุปกรณ์ — รอ settle ({chemistry}, ขั้นต่ำ {min_label})…"
        )

        import threading
        def _run():
            try:
                self.hw.psu_off()
                self.hw.load_off()

                def on_progress(elapsed, v, dv_mv, status):
                    chemistry_now = getattr(
                        self.controller.config.battery, "battery_type", "LiPO"
                    )
                    min_rest = self.controller._OCV_SETTLE.get(
                        chemistry_now, self.controller._OCV_SETTLE["LiPO"]
                    )[0]
                    dv_str = f"{dv_mv:.1f} mV" if dv_mv == dv_mv else "—"
                    if status == "waiting":
                        remaining = max(0, int(min_rest - elapsed))
                        self.sig_charge_status.emit(
                            f"OCV รอขั้นต่ำ: {remaining}s | {v:.3f} V | ΔV {dv_str}"
                        )
                    elif status == "checking":
                        self.sig_charge_status.emit(
                            f"OCV กำลัง settle: {int(elapsed)}s | {v:.3f} V | ΔV {dv_str}"
                        )

                soc, v_final, result = self.controller.calibrate_from_ocv_stable(
                    on_progress=on_progress
                )
                temp = self.controller.hw.current_temp
                self.update_display(v_final, 0.0, soc,
                                    self.controller.estimator.rin, temp)
                flag = "✓ settled" if result == "settled" else "⚠ timeout (ใช้ค่าล่าสุด)"
                msg = (
                    f"OCV {flag}: {v_final:.3f} V  →  SoC {soc:.1f}%"
                    f"  (Temp {temp:.1f}°C)"
                )
                self.sig_alarm.emit(f"[OCV] {msg}")
                self.sig_charge_status.emit(msg)
            except Exception as exc:
                self.sig_alarm.emit(f"[OCV] failed: {exc}")
                self.sig_charge_status.emit(f"OCV failed: {exc}")
            finally:
                self.sig_loading.emit("btn_ocv", False, "")
        threading.Thread(target=_run, daemon=True).start()
    def _on_estop(self):
        self.operation_state.latch_estop()
        # Invalidate queued monitor/worker display callbacks from the stopped
        # operation before any safety shutdown I/O begins.
        self._run_generation += 1
        self._set_hardware_start_controls(False)
        if hasattr(self, "_seq_running"):
            self._seq_running.clear()
        for char_ev in getattr(self, "_char_running", {}).values():
            char_ev.clear()

        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtCore import QUrl
            import os
            
            # Keep references so they aren't garbage collected
            self._estop_player = QMediaPlayer()
            self._estop_audio = QAudioOutput()
            self._estop_player.setAudioOutput(self._estop_audio)
            
            estop_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "estop_siren.mp3"))
            if os.path.exists(estop_path):
                self._estop_player.setSource(QUrl.fromLocalFile(estop_path))
                self._estop_audio.setVolume(1.0)
                # ISO 7731 (auditory danger signals): a danger signal must persist
                # until acknowledged, not play once and go silent — a continuous/
                # one-shot tone habituates or gets missed entirely if the operator
                # wasn't looking at the screen the instant it fired. Looped here,
                # stopped from _alarm_acknowledge() (ui_updater.py) when the
                # operator presses ACKNOWLEDGE.
                self._estop_player.setLoops(QMediaPlayer.Loops.Infinite)
                self._estop_player.play()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to play estop_siren.mp3: {e}")

        # The relay is the independent physical cutoff. Command it before the
        # acquisition worker can wait on its I/O mutex or a VISA transaction.
        # The controller repeats this as part of its normal shutdown path.
        try:
            set_ssr = getattr(self.hw, "set_ssr", None)
            if callable(set_ssr) and set_ssr(False) is False:
                self.sig_alarm.emit("CRITICAL: E-STOP could not confirm SSR OFF")
        except Exception as exc:
            self.sig_alarm.emit(f"CRITICAL: E-STOP SSR OFF failed: {exc}")

        if self._test_worker:
            self._test_worker.emergency_stop()   # immediate instrument override
        if self.controller:
            self.controller._trigger_safety("E-STOP pressed by operator")
            self.controller.stop_charge()
            self.controller.stop_monitor()
            self.controller.stop_live_readback()
        self._log_alarm("⛔ E-STOP issued.")

    def _set_hardware_start_controls(self, enabled):
        for name in ("btn_run_test", "btn_run_hppc", "btn_auto_seq", "btn_quick_scan",
                     "btn_hppc_seq", "btn_cycle_life", "btn_char_pk_start",
                     "btn_char_eta_start", "btn_char_gitt_start", "btn_char_cca_start",
                     "btn_charge_start", "btn_start_monitor"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(bool(enabled))

    def _on_estop_reset(self):
        """Explicitly clear the software latch only after all workers exit and OFF commands succeed."""
        workers_exited = (
            not self.operation_state.owns_hardware
            and self._test_thread is None
            and not (self._seq_thread and self._seq_thread.is_alive())
            and not any(t.is_alive() for t in self._char_threads.values())
            and not getattr(self.controller, "is_charging", False)
            and not getattr(self.controller, "monitor_running", False)
            and not getattr(self.controller, "live_readback_running", False)
            and not (getattr(self.controller, "_charge_thread", None)
                     and self.controller._charge_thread.is_alive())
            and not (getattr(self.controller, "_monitor_thread", None)
                     and self.controller._monitor_thread.is_alive())
            and not (getattr(self.controller, "_live_readback_thread", None)
                     and self.controller._live_readback_thread.is_alive())
        )
        if not workers_exited:
            self._log_alarm("E_STOP_LATCHED — worker cleanup is still active")
            return
        try:
            relay_ok = self.hw.set_ssr(False) if hasattr(self.hw, "set_ssr") else False
            load_ok = self.hw.load_off()
            psu_ok = self.hw.psu_off()
            off_confirmed = (relay_ok is True and load_ok is True and psu_ok is True
                             and getattr(self.hw, "ssr_state", False) is False)
        except Exception as exc:
            off_confirmed = False
            self._log_alarm(f"SAFE_STATE_UNCONFIRMED — OFF command failed: {exc}")
        if not self.operation_state.reset_estop(
                workers_exited=workers_exited, outputs_off_confirmed=off_confirmed):
            self._log_alarm("SAFE_STATE_UNCONFIRMED — E-STOP remains latched")
            return
        if self.controller is not None:
            self.controller.safety_triggered = False
        self._set_hardware_start_controls(True)
        self.set_profile_status("IDLE")
        self._log_alarm("E-STOP reset by operator; OFF commands succeeded")

    def _play_test_complete_sound(self):
        """~15s completion chime, played once for every mode's finish event
        (Run Test, all 4 sequences, all 4 CHARACTERIZE tests) so an operator
        away from the screen is called back. Deliberately test_complete.wav,
        not estop_siren.mp3 — reusing the E-STOP siren here would make a
        normal, successful finish sound identical to an emergency."""
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtCore import QUrl
            import os

            # Keep references so they aren't garbage collected mid-playback
            self._done_player = QMediaPlayer()
            self._done_audio = QAudioOutput()
            self._done_player.setAudioOutput(self._done_audio)

            wav_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "test_complete.wav"))
            if os.path.exists(wav_path):
                self._done_player.setSource(QUrl.fromLocalFile(wav_path))
                self._done_audio.setVolume(1.0)
                self._done_player.play()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to play test_complete.wav: {e}")

    def _stop_test_complete_sound(self):
        """Cut the completion chime short — wired to the sequence-done
        popup's OK button (see _slot_seq_done) so acknowledging the result
        doesn't also mean sitting through the rest of the ~15s clip."""
        player = getattr(self, "_done_player", None)
        if player is not None:
            try:
                player.stop()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to stop test_complete.wav: {e}")
