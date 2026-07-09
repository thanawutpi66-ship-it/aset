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
    QDialog,
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
from aset_batt.ui.theme import (
    BG, PANEL, PANEL2, FIELD, BORDER, TEXT, MUTED, OK, WARN, CRIT, INFO, NEUTRAL,
)

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
    def _refresh_ports(self):
        if self.hw is None:
            return
        try:
            visa = self.hw.get_visa_ports() if hasattr(self.hw, "get_visa_ports") else []
            coms = self.hw.get_com_ports() if hasattr(self.hw, "get_com_ports") else []
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
    def _refresh_battery_readout(self):
        b = self.config.battery
        self.lbl_battery_readout.setText(
            f"{b.battery_type} · {b.cells_series}S{b.cells_parallel}P · {b.pack_nominal_voltage:.1f}V · {b.rated_capacity:.1f}Ah"
        )
    def _on_connect(self):
        psu, load, esp = self.cb_psu.currentText(), self.cb_load.currentText(), self.cb_esp.currentText()
        if not psu or not load:
            if not self._headless:
                QMessageBox.warning(self, "Connect", "Select PSU and Load ports first")
            return
        try:
            self.hw.connect_instruments(psu, load)
            # G7 (industrial-grade audit): range-set + OVP/OCP/UVP protection +
            # instrument hardening now live in ONE HardwareController method
            # (apply_default_safety_protection) instead of being inlined here only
            # — any other real-hardware entry point (a script, a test harness, a
            # future alternate UI) gets the exact same backstop by calling it too,
            # instead of silently getting none. MockHardwareController doesn't
            # implement it (simulation has nothing to protect), hence the hasattr.
            if hasattr(self.hw, "apply_default_safety_protection"):
                result = self.hw.apply_default_safety_protection(
                    max_current_a=self.config.battery.max_current,
                    pack_max_voltage_v=self.config.battery.pack_max_voltage,
                    min_voltage_v=self.config.system.safety_limits.get("min_voltage", 0.0),
                )
                for w in result.get("warnings", []):
                    self._log_alarm(w)
                info = result.get("info") or {}
                if info.get("psu"):
                    self._log_alarm(f"PSU: {info['psu']}")
                if info.get("load"):
                    self._log_alarm(f"Load: {info['load']}")
            if esp:
                baud = getattr(self.config.hardware, "serial_baudrate", 9600)
                try:
                    self.hw.connect_esp32(esp, baudrate=baud)
                    if hasattr(self.hw, "esp_connect_error"):
                        self.hw.esp_connect_error = ""
                except Exception as esp_exc:
                    # ESP32 fail is non-fatal — store error so _slot_conn shows ✗
                    if hasattr(self.hw, "esp_connect_error"):
                        self.hw.esp_connect_error = str(esp_exc)
                    self._log_alarm(f"ESP32 connect failed (non-fatal): {esp_exc}")
            self.config.hardware.psu_port = psu
            self.config.hardware.load_port = load
            self.config.hardware.esp_port = esp
            self.config.save_config()
            self._update_connection_status()
            self._log_alarm("Hardware connected.")
            self._cloud_push_start()
            if self.controller is not None:
                self.controller.start_live_readback()
        except Exception as exc:
            # connect_error already set in hw.connect_instruments — let _slot_conn show ✗
            self._update_connection_status()
            if not self._headless:
                QMessageBox.critical(self, "เชื่อมต่อล้มเหลว", str(exc))
    def _on_disconnect(self):
        try:
            if self.controller is not None:
                self.controller.stop_live_readback()
            self._cloud_push_stop()
            if hasattr(self.hw, "release_instrument_config"):
                try:
                    self.hw.release_instrument_config()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            if hasattr(self.hw, "disconnect_instruments"):
                self.hw.disconnect_instruments()
            if hasattr(self.hw, "disconnect_esp32"):
                self.hw.disconnect_esp32()
            self._update_connection_status()
            self._log_alarm("Hardware disconnected.")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "Disconnect Error", str(exc))
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
        try:
            self.hw.set_ssr(True)
            self._log_alarm("SSR manual ON (operator override)")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "SSR Error", str(exc))
        self._update_connection_status()
    def _on_ssr_manual_off(self):
        """Manual SSR cutoff — always safe (cuts power), no confirmation needed,
        same immediacy as E-STOP."""
        if not getattr(self.hw, "is_esp_connected", False):
            return
        try:
            self.hw.set_ssr(False)
            self._log_alarm("SSR manual OFF (operator override)")
        except Exception as exc:
            if not self._headless:
                QMessageBox.critical(self, "SSR Error", str(exc))
        self._update_connection_status()
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
            if on:
                ok = self.hw.set_psu(
                    True,
                    str(float(self.ed_psu_v.text())),
                    str(float(self.ed_psu_i.text())),
                )
            else:
                ok = self.hw.set_psu(False)
            # G9 (industrial-grade audit): set_psu() now reports whether the SCPI
            # write actually succeeded — a failed command used to just be logged,
            # so the operator watching this exact button had no way to know the PSU
            # didn't really change state.
            if not ok and not self._headless:
                QMessageBox.warning(self, "PSU", "PSU command failed — see log for details")
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "PSU", "Invalid voltage / current")
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
            ok = self.hw.set_load(on, str(float(self.ed_load_a.text())) if on else "0")
            # G9 (industrial-grade audit): see _psu_manual's comment — same fix.
            if not ok and not self._headless:
                QMessageBox.warning(self, "Load", "Load command failed — see log for details")
            try:
                from aset_batt.storage.cloud_push import set_cloud_meta
                if on:
                    set_cloud_meta(phase="discharge", test_mode="MANUAL", workflow="Manual — Direct Load", total_s=0)
                else:
                    set_cloud_meta(phase="", test_mode="", workflow="")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        except ValueError:
            if not self._headless:
                QMessageBox.warning(self, "Load", "Invalid current")
    def _on_check_psu_trip(self):
        if not hasattr(self.hw, "get_psu_protection_tripped"):
            return
        tripped = self.hw.get_psu_protection_tripped()
        if tripped:
            self.lbl_psu_trip.setText("Trip: ⛔ TRIPPED (OVP/OCP/OTP)")
            self.lbl_psu_trip.setStyleSheet(f"color:{CRIT}; font-weight:600;")
        else:
            self.lbl_psu_trip.setText("Trip: OK")
            self.lbl_psu_trip.setStyleSheet(f"color:{OK}; font-weight:600;")
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
        ok = self.hw.clear_psu_protection()
        self._log_alarm("PSU protection trip cleared (operator)." if ok else "Clear PSU trip failed.")
        self._on_check_psu_trip()
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
        if self._test_worker:
            self._test_worker.emergency_stop()   # immediate instrument override
        if self.controller:
            self.controller._trigger_safety("E-STOP pressed by operator")
        self._log_alarm("⛔ E-STOP issued.")