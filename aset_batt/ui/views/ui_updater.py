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

class UiUpdaterMixin:
    def _start_update_check(self):
        """Background check: is origin/<branch> ahead of us? Fires sig_update_available.
        Silent on any failure (no git / offline / not a repo) — the banner just stays
        hidden. Runs once at startup, off the UI thread."""
        if self._headless:
            return

        def work():
            try:
                from aset_batt.services.updater import repo_root, check_for_updates
                info = check_for_updates(repo_root())
                if info and info.get("behind", 0) > 0:
                    self.sig_update_available.emit(int(info["behind"]),
                                                   info.get("subject", ""))
            except Exception as exc:
                logger.debug("update check failed: %s", exc)

        threading.Thread(target=work, daemon=True).start()
    def _slot_update_available(self, behind, subject):
        if not hasattr(self, "btn_update"):
            return
        self.btn_update.setText(f"⭯ Update available ({behind})")
        self.btn_update.setToolTip(
            f"{behind} update(s) on GitHub — click to pull the latest & restart\n"
            f"Latest: {subject}")
        self.btn_update.setEnabled(True)
        self.btn_update.setVisible(True)
    def _slot_update_done(self, ok, msg):
        # State first, dialog second — the dialog is guarded so a headless run
        # (no one to click OK) doesn't block on a modal, matching the rest of the UI.
        self._updating = False
        if ok:
            self.btn_update.setVisible(False)
            if not self._headless:
                QMessageBox.information(
                    self, "อัปเดตสำเร็จ",
                    "อัปเดตเรียบร้อย ✓\n\nปิดแล้วเปิดโปรแกรมใหม่ (main.py) "
                    "เพื่อใช้เวอร์ชันล่าสุด")
        else:
            self.btn_update.setEnabled(True)
            self.btn_update.setText("⭯ Update available")
            if not self._headless:
                QMessageBox.warning(
                    self, "อัปเดตไม่สำเร็จ",
                    f"ดึงอัปเดตไม่ได้:\n{msg}\n\nอาจมีไฟล์แก้ค้าง/commit ในเครื่องที่ชนกัน "
                    "— ติดต่อผู้พัฒนา หรืออัปเดตผ่าน git ในเทอร์มินัลเอง")
    def _alarm_flash_tick(self):
        """Toggle bright/dim colours on every unACKed alarm row at 500 ms."""
        if not self._unack_rows:
            self._flash_timer.stop()
            return
        self._flash_state = not self._flash_state
        tbl = self.tbl_alarms
        for row_idx in list(self._unack_rows):
            if row_idx >= tbl.rowCount():
                continue
            bright_bg, dim_bg, fg, evt_fg = self._alarm_row_colors.get(
                row_idx, ("#FF0000", "#3D1A1A", "#FFFFFF", "#FF5555")
            )
            bg = QColor(bright_bg if self._flash_state else dim_bg)
            for col in range(tbl.columnCount()):
                item = tbl.item(row_idx, col)
                if item:
                    item.setBackground(bg)
        # Status bar flash (alternates text colour red <-> dark)
        if self._flash_state:
            self._alarm_statusbar.setStyleSheet(
                "background:#5A0000; color:#FFFFFF; padding:3px 10px; font-size:10px;"
                " font-family:Consolas,monospace; border-top:2px solid #FF0000; font-weight:700;"
            )
        else:
            self._alarm_statusbar.setStyleSheet(
                "background:#2A0000; color:#FF5555; padding:3px 10px; font-size:10px;"
                " font-family:Consolas,monospace; border-top:2px solid #770000; font-weight:700;"
            )
    def _alarm_acknowledge(self):
        """Operator ACK: stop flashing, mark rows as ACKed (solid colour)."""
        tbl = self.tbl_alarms
        ts_ack = datetime.now().strftime("%H:%M:%S")
        for row_idx in list(self._unack_rows):
            if row_idx >= tbl.rowCount():
                continue
            _bright_bg, dim_bg, fg, evt_fg = self._alarm_row_colors.get(
                row_idx, ("#FF0000", "#3D1A1A", "#FFFFFF", "#FF5555")
            )
            # Lock to dim (acknowledged) solid colour
            bg = QColor(dim_bg)
            for col in range(tbl.columnCount()):
                item = tbl.item(row_idx, col)
                if item:
                    item.setBackground(bg)
            # Update ACK STATUS column
            ack_item = tbl.item(row_idx, 4)
            if ack_item:
                ack_item.setText(f"ACK  {ts_ack}")
                ack_item.setForeground(QColor("#55CC55"))
        self._unack_rows.clear()
        self._flash_timer.stop()
        self._btn_ack.setEnabled(False)
        self._alarm_statusbar.setText("  ALL ALARMS ACKNOWLEDGED")
        self._alarm_statusbar.setStyleSheet(
            "background:#1A2A1A; color:#55CC55; padding:3px 10px; font-size:10px;"
            " font-family:Consolas,monospace; border-top:1px solid #336633;"
        )
    def update_display(self, v, i, soc, rin, temp=None, soh=None):
        if temp is None:
            temp = getattr(self.hw, "current_temp", 25.0)
        if soh is None:
            soh = getattr(self.estimator, "soh", 100.0)
        self.sig_display.emit(float(v), float(i), float(soc), float(rin), float(temp), float(soh))
    def update_live_readback(self, v, i, temp):
        """Lightweight display-only update — used right after Connect, before any
        test is running (no CSV logging, no state estimator). See _slot_live_readback."""
        self.sig_live_readback.emit(float(v), float(i), float(temp))
    def set_profile_status(self, text, color=None):
        self.sig_profile_status.emit(str(text), str(color or theme.MUTED))
    def set_charge_status(self, text):
        self.sig_charge_status.emit(str(text))
    def set_button_enabled(self, key, enabled):
        self.sig_button.emit(str(key), bool(enabled))
    def set_loading_state(self, key, loading, text=None):
        self.sig_loading.emit(str(key), bool(loading), str(text or ""))
    def _update_connection_status(self):
        self.sig_conn.emit()
    def _on_heartbeat_tick(self):
        """Runs every 1s regardless of test state — LED refresh + ESP32 watchdog
        heartbeat. As long as this keeps firing, the ESP32 firmware knows the PC
        process is alive and lets the SSR relay stay in whatever state it's in.
        If the process crashes/hangs/gets killed, this stops firing and the
        firmware's own watchdog cuts the relay after its timeout — a real
        safety net that a Python signal handler can't provide for a hard kill."""
        self._update_connection_status()
        if getattr(self.hw, "is_esp_connected", False):
            try:
                self.hw.feed_watchdog()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        # Direct control (raw PSU/Load jog) has no test/monitor loop of its own by
        # design (see _direct_page's warning — no SoC, no CSV) so nothing else feeds
        # the graph while it's active; piggyback on this 1s tick instead. Read-only —
        # does not touch the estimator, so it can't double-count against whatever
        # else might still be running (e.g. a manual Charge left on while the
        # operator switches over to peek at Direct).
        if getattr(self, "rb_direct", None) is not None and self.rb_direct.isChecked() \
                and getattr(self.hw, "is_connected", False):
            try:
                v, psu_i, load_i = self.hw.read_vi()
                if load_i > 0.02:
                    i_net = load_i
                elif getattr(self.hw, "_psu_output_on", False):
                    i_net = -psu_i
                else:
                    i_net = psu_i
                soc = getattr(self.estimator, "soc", 0.0) if self.estimator else 0.0
                rin = getattr(self.estimator, "rin", 0.0) if self.estimator else 0.0
                self.update_display(v, i_net, soc, rin, self.hw.current_temp)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    def update_status_bar(self):
        self._update_connection_status()
    def handle_safety_trigger(self, reason):
        self.sig_safety.emit(str(reason))
    def show_message(self, title: str, message: str, msg_type: str = "info"):
        """Wired onto UIEventHandler (see app_bootstrapper._wire_runtime) as the
        real handler for EventType.SHOW_MESSAGE — replaces a leftover tkinter
        messagebox call that silently swallowed operator-facing safety warnings
        (OCV out-of-range, sustained ESP32 temp-stale trip, monitor loop fatal
        error) in this PySide6-only app: QtRootShim._run caught the tkinter
        exception and only logged it, so the popup never appeared."""
        self._log_alarm(f"[{msg_type.upper()}] {title}: {message}")
        if self._headless:
            return
        if msg_type == "error":
            QMessageBox.critical(self, title, message)
        elif msg_type == "warning":
            QMessageBox.warning(self, title, message)
        else:
            QMessageBox.information(self, title, message)
    def handle_profile_completed(self, data):
        self.sig_profile_done.emit(data)
    def handle_analysis_completed(self, result):
        self.sig_analysis_done.emit(result)
    def _set_temp_label_color(self, temp):
        """Color-code the TEMP metric card (CRIT/WARN/OK) against the REAL configured
        safety_limits.max_temperature — single source of truth for all 3 telemetry
        paths (_slot_display, _slot_live_readback, _on_test_telemetry), which used to
        each drive a separate, now-removed "CASE TEMPERATURE" duplicate box, two of
        them with a hardcoded 35/45°C threshold that didn't match the configured
        safety limit at all."""
        if math.isnan(temp):
            return
        lbl, unit = self.metric_labels["Temp"]
        crit = self.config.system.safety_limits.get("max_temperature", 55)
        warn = crit - 10
        color = theme.CRIT if temp >= crit else theme.WARN if temp >= warn else theme.TEXT
        lbl.setStyleSheet(f"color:{color}; border:0;")
    def _update_vi_temp_labels(self, v, i, temp):
        """Voltage/Current/Temp labels + current-direction badge — the subset of
        metrics valid even without a running test (no SoC/Rin, those need the
        state estimator). Shared by _slot_display (full test telemetry) and
        _slot_live_readback (pre-test Connect readback)."""
        # Cached so _on_retheme() can recolor these against the NEW theme even
        # when idle (no telemetry arriving to naturally trigger a recolor) —
        # otherwise they keep whatever color was picked under the OLD theme,
        # which can be unreadable after a light<->dark switch (e.g. a light
        # theme's near-white MUTED-on-dark-bg choice showing white-on-white).
        self._last_vit = (v, i, temp)
        for name, val, fmt in [("Voltage", v, "{:.2f}"), ("Temp", temp, "{:.2f}")]:
            lbl, unit = self.metric_labels[name]
            lbl.setText(f"{fmt.format(val)} {unit}")
        i_lbl, i_unit = self.metric_labels["Current"]
        i_lbl.setText(f"{abs(i):.3f} {i_unit}")
        _IDLE = self._I_IDLE
        # G1 (industrial-grade audit): discharging is this device's normal, expected
        # operating state — coloring it WARN (amber) unconditionally for the entire
        # duration of every routine discharge test misused the "caution" color for a
        # non-caution state (ISA-101: color reserved for abnormal, not routine
        # status — see theme.py's own docstring). Amber now only appears if the
        # current is actually approaching the configured max_current limit; CHG
        # keeps the existing accent color (INFO reads as a neutral "active" tag in
        # this app's palette, not an alarm color, so charging isn't misrepresented
        # the same way discharging was).
        max_i = max(1e-6, getattr(self.config.battery, "max_current", 0.0))
        load_frac = abs(i) / max_i
        if i < -_IDLE:                              # charging (convention: negative)
            i_lbl.setStyleSheet(f"color:{theme.INFO}; border:0;")
            self._lbl_i_dir.setText("▲  CHG")
            self._lbl_i_dir.setStyleSheet(f"color:{theme.INFO}; border:0;")
        elif i > _IDLE:                             # discharging (convention: positive)
            i_color = theme.CRIT if load_frac >= 1.0 else theme.WARN if load_frac >= 0.9 else theme.TEXT
            i_lbl.setStyleSheet(f"color:{i_color}; border:0;")
            self._lbl_i_dir.setText("▼  DSG")
            self._lbl_i_dir.setStyleSheet(f"color:{i_color if load_frac >= 0.9 else theme.MUTED}; border:0;")
        else:                                       # at rest
            i_lbl.setStyleSheet(f"color:{theme.TEXT}; border:0;")
            self._lbl_i_dir.setText("—  REST")
            self._lbl_i_dir.setStyleSheet(f"color:{theme.MUTED}; border:0;")
    def _on_pulse_tick(self):
        self._pulse_state = not getattr(self, '_pulse_state', False)
        # Pulse state_pill border if active
        if hasattr(self, 'state_pill'):
            text = self.state_pill.text().upper()
            if "CHARGE" in text or "DISCHARGE" in text or "RUN" in text:
                color = theme.OK if self._pulse_state else "#1A2E1A"
                if "DISCHARGE" in text:
                    color = theme.WARN if self._pulse_state else "#3D3010"
                self.state_pill.setStyleSheet(self._pill(color))
            elif "STOP" in text or "FAIL" in text or "ESTOP" in text:
                color = theme.CRIT if self._pulse_state else "#3D1A1A"
                self.state_pill.setStyleSheet(self._pill(color))
    def _log_alarm(self, msg: str):
        import time as _time
        now = _time.time()
        ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        m = msg.strip()
        m_low = m.lower()

        # ── Classify event ─────────────────────────────────────────
        if any(x in m_low for x in ["safety", "estop", "e-stop", "fail", "error",
                                      "abort", "⛔", "alarm", "overvolt", "underv",
                                      "overtemp", "otp"]):
            event, state = "ALARM",   "ACTIVE"
            row_bg, row_fg, evt_fg = "#3D1A1A", "#E0E3E6", "#FF5555"
            try:
                import winsound
                import threading
                threading.Thread(target=winsound.Beep, args=(1000, 800), daemon=True).start()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        elif any(x in m_low for x in ["warn", "⚠", "timeout", "timeout"]):
            event, state = "WARNING", "ACTIVE"
            row_bg, row_fg, evt_fg = "#3D3010", "#E0E3E6", "#FFB700"
        elif any(x in m_low for x in ["complete", "✓", "success", "connected",
                                        "ready", "done", "normal"]):
            event, state = "NORMAL",  "CLEARED"
            row_bg, row_fg, evt_fg = "#1A2E1A", "#E0E3E6", "#55CC55"
        elif any(x in m_low for x in ["start", "started", "enable", "begin",
                                        "on ", "charge started", "discharge"]):
            event, state = "ON",      "ACTIVE"
            row_bg, row_fg, evt_fg = "#1A2240", "#E0E3E6", "#5599FF"
        elif any(x in m_low for x in ["stop", "stopped", "disable", "disconnected",
                                        "cancel", "off"]):
            event, state = "OFF",     "INACTIVE"
            row_bg, row_fg, evt_fg = "#282828", "#A8A8A8", "#888888"
        else:
            event, state = "INFO",    ""
            row_bg, row_fg, evt_fg = "#1C1F23", "#C0C4C8", "#7A9A5A"

        # ── Parse POINTNAME ────────────────────────────────────────
        prefix_m = re.match(r'^\[([^\]]+)\]\s*', m)
        if prefix_m:
            prefix = prefix_m.group(1)
            body   = m[prefix_m.end():]
            point  = f"{prefix} · {body}" if body else prefix
        else:
            point = m

        if not hasattr(self, "tbl_alarms"):
            return
        tbl = self.tbl_alarms

        # ── R2/G2 dedup ──────────────────────────────────────────────
        # The SAME (event, point) repeating within _ALARM_DEDUP_WINDOW_S updates
        # the existing row in place (bumping an "(xN)" occurrence count + its
        # timestamp) instead of inserting a new one — a stuck sensor re-firing the
        # identical fault every telemetry tick no longer floods the log or buries
        # whatever alarm actually mattered. ACK/flash/cloud-push/beep are NOT
        # re-triggered on a repeat — they already fired for the first occurrence.
        key = (event, point)
        if (key == self._last_alarm_key and self._last_alarm_row is not None
                and (now - self._last_alarm_time) <= self._ALARM_DEDUP_WINDOW_S
                and self._last_alarm_row < tbl.rowCount()):
            self._last_alarm_occurrence += 1
            self._last_alarm_time = now
            row = self._last_alarm_row
            item0 = tbl.item(row, 0)
            item1 = tbl.item(row, 1)
            if item0 is not None:
                item0.setText(ts)
            if item1 is not None:
                item1.setText(f"{point}  (×{self._last_alarm_occurrence})")
            tbl.scrollToBottom()
            return

        # ── R2/G2 rate limit ─────────────────────────────────────────
        # More than _ALARM_RATE_LIMIT DISTINCT rows within _ALARM_RATE_WINDOW_S is
        # treated as a flood, not real information (ISA-18.2: alarm flood
        # suppression) — further rows are coalesced into one running "rate limit"
        # row instead of each getting its own, until the rate drops back down.
        while self._alarm_recent_times and \
                now - self._alarm_recent_times[0] > self._ALARM_RATE_WINDOW_S:
            self._alarm_recent_times.popleft()
        if len(self._alarm_recent_times) >= self._ALARM_RATE_LIMIT:
            self._alarm_rate_suppressed += 1
            rl_row = self._alarm_rate_limit_row
            rl_text = (f"Alarm rate limit — {self._alarm_rate_suppressed} event(s) "
                      f"suppressed in the last {self._ALARM_RATE_WINDOW_S:.0f}s "
                      f"(most recent: {point})")
            if rl_row is not None and rl_row < tbl.rowCount():
                item0 = tbl.item(rl_row, 0)
                item1 = tbl.item(rl_row, 1)
                if item0 is not None:
                    item0.setText(ts)
                if item1 is not None:
                    item1.setText(rl_text)
                tbl.scrollToBottom()
                return
            # First trip — insert one WARNING-styled row for the rate-limit notice
            # itself, and remember its index so further floods coalesce into it.
            rl_row = tbl.rowCount()
            tbl.insertRow(rl_row)
            for col, (text, bold, f_color) in enumerate([
                (ts, False, "#E0E3E6"), (rl_text, False, "#E0E3E6"),
                ("ACTIVE", False, "#E0E3E6"), ("WARNING", True, "#FFB700"),
                ("", True, theme.MUTED),
            ]):
                item = QTableWidgetItem(text)
                item.setBackground(QColor("#3D3010"))
                item.setForeground(QColor(f_color))
                if bold:
                    fnt = item.font(); fnt.setBold(True); item.setFont(fnt)
                tbl.setItem(rl_row, col, item)
            tbl.setRowHeight(rl_row, 24)
            tbl.scrollToBottom()
            self._alarm_rate_limit_row = rl_row
            self._alarm_count_lbl.setText(f"{tbl.rowCount()} events") \
                if hasattr(self, "_alarm_count_lbl") else None
            return
        self._alarm_recent_times.append(now)
        # A genuinely new/distinct alarm invalidates the rate-limit coalescing row
        # (it's no longer "the most recent" flood) so a future flood starts fresh,
        # with its own suppressed-count starting back at 0.
        self._alarm_rate_limit_row = None
        self._alarm_rate_suppressed = 0

        # ── Insert row ─────────────────────────────────────────────
        row = tbl.rowCount()
        tbl.insertRow(row)

        # Forward every log line to the cloud dashboard's Alarm Log, not just
        # ALARM/WARNING — operators watching remotely wanted the full activity
        # feed (Connected, Charge started, etc.), not just safety trips.
        try:
            from aset_batt.storage.cloud_push import push_alarm
            push_alarm(event, point)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

        # Determine if this event needs ACK (ALARM or WARNING only) — GUI-side
        # flash/acknowledge behavior only, independent of the cloud push above.
        needs_ack = event in ("ALARM", "WARNING")
        # Audible alert on genuine ALARM events only (not WARNING — stays visual-only
        # so a routine "temperature reading stale" warning doesn't beep as loudly as
        # a real safety trip). hw.beep() is itself non-fatal.
        if event == "ALARM" and hasattr(self.hw, "beep"):
            try:
                self.hw.beep(1)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        # For SCADA flash: bright = saturated alert, dim = muted background
        if event == "ALARM":
            bright_bg, dim_bg = "#8B0000", "#3D1A1A"
        elif event == "WARNING":
            bright_bg, dim_bg = "#7A5500", "#3D3010"
        else:
            bright_bg = dim_bg = row_bg

        bg = QColor(row_bg)
        fg = QColor(row_fg)
        ack_text = "UNACK" if needs_ack else ""
        for col, (text, bold, f_color) in enumerate([
            (ts,       False, row_fg),
            (point,    False, row_fg),
            (state,    False, row_fg),
            (event,    True,  evt_fg),
            (ack_text, True,  "#FF5555" if needs_ack else theme.MUTED),
        ]):
            item = QTableWidgetItem(text)
            item.setBackground(bg)
            item.setForeground(QColor(f_color))
            if bold:
                fnt = item.font()
                fnt.setBold(True)
                item.setFont(fnt)
            tbl.setItem(row, col, item)
        tbl.setRowHeight(row, 24)
        tbl.scrollToBottom()

        # ── SCADA: register unACKed rows and start flash timer ─────────
        if needs_ack:
            self._alarm_row_colors[row] = (bright_bg, dim_bg, row_fg, evt_fg)
            self._unack_rows.add(row)
            self._btn_ack.setEnabled(True)
            unack_count = len(self._unack_rows)
            self._alarm_statusbar.setText(
                f"  ⚠  UNACKNOWLEDGED {event}S: {unack_count} — PRESS [ACKNOWLEDGE] TO CLEAR"
            )
            if not self._flash_timer.isActive():
                self._flash_state = True
                self._flash_timer.start()

        # ── Update header count & status bar ───────────────────────
        n = tbl.rowCount()
        if hasattr(self, "_alarm_count_lbl"):
            self._alarm_count_lbl.setText(f"{n} events")
        if hasattr(self, "_alarm_statusbar"):
            if event == "ALARM":
                self._alarm_statusbar.setText(f"  ⛔  ALARM ACTIVE — {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#7A0000; color:#FFCCCC; padding:3px 10px; font-size:10px;"
                    " font-weight:700; font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            elif event == "WARNING":
                self._alarm_statusbar.setText(f"  ⚠  WARNING — {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#5A4000; color:#FFE080; padding:3px 10px; font-size:10px;"
                    " font-weight:700; font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            elif event == "NORMAL":
                self._alarm_statusbar.setText(f"  ✓  {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
                    " font-family:Consolas,monospace; border-top:1px solid #333;"
                )
            else:
                self._alarm_statusbar.setText(f"  {point}")
                self._alarm_statusbar.setStyleSheet(
                    "background:#1C1F23; color:#7A9A5A; padding:3px 10px; font-size:10px;"
                    " font-family:Consolas,monospace; border-top:1px solid #333;"
                )

        # R2/G2: remember this row so an immediate repeat coalesces into it
        # instead of inserting a duplicate (see the dedup check above).
        self._last_alarm_key = key
        self._last_alarm_row = row
        self._last_alarm_occurrence = 1
        self._last_alarm_time = now

    @staticmethod
    def _pill_color_for(text: str):
        text = text.upper()
        if "RUN" in text:
            return theme.INFO
        if "STOP" in text or "FAIL" in text:
            return theme.CRIT
        return theme.NEUTRAL
    def _mode_badge_style(self):
        color = theme.WARN if self.config.system.simulation_mode else theme.OK
        return (f"background:transparent; color:{color}; border:1px solid {color}; "
                f"border-radius:4px; padding:3px 8px; font-weight:700; letter-spacing:1px; margin-right: 10px;")
    def _update_window_title(self):
        self.setWindowTitle(
            f"ASET Battery Tester — ISA-101 Command Center  [{theme.current_theme()}]")
    def _on_retheme(self):
        """Refresh everything that isn't covered by theme.style()'s automatic
        registry: state-dependent widgets whose color depends on runtime state,
        not just the active palette, so they need their own recompute.
        (self.trend.retheme() is NOT called here — it's already registered via
        theme.on_retheme() in TrendContainer.__init__, so theme.retheme(mode)
        above already ran it; calling it again here would just redo the same
        graph-pen/background work twice for nothing.)"""
        self._update_window_title()
        self._update_connection_status()
        self.state_pill.setStyleSheet(self._pill(self._pill_color_for(self.state_pill.text())))
        # Current/direction and Temp are colored from runtime state (charge vs.
        # discharge vs. rest, temp vs. safety threshold), not just the palette —
        # _metric_card()'s theme.style() baseline alone would show the wrong
        # color for them (e.g. still MUTED-at-rest while actually charging).
        # Re-run with the last known reading so they reflect BOTH the current
        # state AND the new theme, same as if a fresh sample had just arrived.
        last_vit = getattr(self, "_last_vit", None)
        if last_vit is not None:
            self._update_vi_temp_labels(*last_vit)
            self._set_temp_label_color(last_vit[2])
        # Rin has the same "pending vs. measured" split as SoH/Grade (see
        # _metric_card), but flips back to pending between loads within a
        # single test rather than only once — _rin_ema is non-None exactly
        # when a real (non-placeholder) reading is currently displayed.
        if getattr(self, "_rin_ema", None) is not None:
            rin_lbl, _ = self.metric_labels["Rin"]
            rin_lbl.setStyleSheet(f"color:{theme.TEXT}; border:0;")
        # Same "only ever styled from one event-driven slot" situation as
        # Current/Temp/Rin above, for the CHARACTERIZE tab's Peukert/ETA/GITT/
        # CCA status labels (see CharacterizeMixin._refresh_char_status_colors).
        self._refresh_char_status_colors()
        # ...and for the ANALYTICS final-analysis row (SoH/Rin/Grade cards),
        # colored once by _slot_analysis_done when a test's analysis lands.
        self._apply_final_metric_styles()
    def _apply_final_metric_styles(self):
        """Re-apply the final-analysis row's state-dependent colors (Grade card
        in its grade color, SoH TEXT-vs-MUTED for N/A, Rin TEXT) from cached
        state — shared by _slot_analysis_done (fresh result) and _on_retheme()
        (recolor the same result for a new theme). No-op before any analysis:
        the theme.style() baselines from _metric_card()/_grade_bar_style()
        already cover the pristine placeholder look."""
        grade = getattr(self, "_last_grade", None)
        if grade is None:
            return
        self.lbl_grade.setStyleSheet(self._grade_bar_style())
        grade_lbl, _ = self.metric_labels_final["Grade"]
        grade_lbl.setStyleSheet(f"color:{self._grade_color(grade)}; border:0;")
        rin_final_lbl, _ = self.metric_labels_final["Rin"]
        rin_final_lbl.setStyleSheet(f"color:{theme.TEXT}; border:0;")
        # "N/A" SoH is still a pending/no-data state (not measurable this test)
        # — keep it MUTED like the initial "—" placeholder; only a real number
        # gets full TEXT contrast (see _metric_card's pending styling).
        soh_final_lbl, _ = self.metric_labels_final["SoH"]
        soh_color = theme.TEXT if getattr(self, "_last_soh_valid", False) else theme.MUTED
        soh_final_lbl.setStyleSheet(f"color:{soh_color}; border:0;")
