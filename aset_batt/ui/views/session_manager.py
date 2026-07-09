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

class SessionManagerMixin:
    def _detect_session_type(self, fpath: str) -> str:
        """บอกชนิดการทดสอบ. ถ้าชื่อไฟล์ฝัง label ไว้ (test_HPPC_...) ใช้อันนั้นเลย
        (แม่นสุด แยก Quick Scan/IEC ได้) — ไม่งั้น fallback อ่านคอลัมน์ Mode ของ CSV.
        ไฟล์จาก START DATA LOGGING ไม่มีคอลัมน์ Mode → 'Data Log'."""
        flabel = self._label_from_filename(os.path.basename(fpath))
        if flabel:
            return self._FILENAME_LABEL_MAP.get(flabel.lower(), flabel)
        try:
            with open(fpath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return "—"
                mode_idx = next((i for i, h in enumerate(header)
                                 if h.strip().lower() == "mode"), None)
                if mode_idx is None:
                    return "Data Log"
                modes = set()
                for n, row in enumerate(reader):
                    if mode_idx < len(row) and row[mode_idx]:
                        modes.add(row[mode_idx].lower())
                    if n > 500:          # อ่านพอประมาณ — ชนิดไม่เปลี่ยนกลางคัน
                        break
                if not modes:
                    return "Data Log"
                for key, label in self._SESSION_TYPE_MAP.items():
                    if any(key in m for m in modes):
                        return label
                return next(iter(modes)).title()
        except OSError:
            return "—"
    @staticmethod
    def _format_session_time(fname: str) -> str:
        """แปลง timestamp ในชื่อไฟล์ → '28 Jun 2026  18:47'.

        รองรับทั้ง test_YYYYMMDD_HHMMSS.csv (เดิม) และ
        test_LABEL_YYYYMMDD_HHMMSS.csv (ใหม่ — มีชนิดเทสต์นำหน้า timestamp)."""
        m = re.search(r"(\d{8}_\d{6})", fname)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").strftime("%d %b %Y  %H:%M")
            except ValueError:
                pass
        return fname
    @staticmethod
    def _label_from_filename(fname: str) -> str:
        """ดึงชนิดเทสต์จากชื่อไฟล์ test_LABEL_YYYYMMDD_HHMMSS.csv → 'LABEL' (ถ้ามี)."""
        m = re.match(r"test_(.+?)_\d{8}_\d{6}\.csv$", fname)
        return m.group(1) if m else ""
    @property
    def _session_meta_file(self) -> str:
        from aset_batt.storage.data_utils import DataHandler
        return os.path.join(os.path.dirname(DataHandler.make_session_path()), ".session_meta.json")
    def _load_session_meta(self) -> dict:
        try:
            import json as _json
            with open(self._session_meta_file, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}
    def _save_session_meta(self, meta: dict):
        try:
            import json as _json
            from aset_batt.storage.data_utils import DataHandler
            os.makedirs(os.path.dirname(DataHandler.make_session_path()), exist_ok=True)
            with open(self._session_meta_file, "w", encoding="utf-8") as f:
                _json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log_alarm(f"session meta save failed: {e}")
    def _on_session_context_menu(self, pos):
        item = self.lst_sessions.itemAt(pos)
        if item is None:
            return
        fpath = item.data(Qt.ItemDataRole.UserRole)
        fname = os.path.basename(fpath)
        from PySide6.QtWidgets import QMenu, QInputDialog
        menu = QMenu(self)
        act_rename = menu.addAction("✏  Rename / Label")
        act_tag    = menu.addAction("🏷  Add Tag")
        act_clear  = menu.addAction("✗  Clear Label & Tag")
        action = menu.exec(self.lst_sessions.mapToGlobal(pos))
        meta = self._load_session_meta()
        entry = meta.get(fname, {})
        if action == act_rename:
            text, ok = QInputDialog.getText(self, "Rename Session",
                                            "Label:", text=entry.get("label", ""))
            if ok:
                entry["label"] = text.strip()
                meta[fname] = entry
                self._save_session_meta(meta)
                self._refresh_session_list()
        elif action == act_tag:
            text, ok = QInputDialog.getText(self, "Add Tag",
                                            "Tag:", text=entry.get("tag", ""))
            if ok:
                entry["tag"] = text.strip()
                meta[fname] = entry
                self._save_session_meta(meta)
                self._refresh_session_list()
        elif action == act_clear:
            meta.pop(fname, None)
            self._save_session_meta(meta)
            self._refresh_session_list()
    def _refresh_session_list(self):
        """อัพเดทรายการ session files จาก sessions/ directory.
        แสดง: ลำดับ · ชนิดการทดสอบ · วันเวลา · ขนาด · label/tag ถ้ามี"""
        if not hasattr(self, "lst_sessions"):
            return
        self.lst_sessions.clear()
        from aset_batt.storage.data_utils import DataHandler
        logs_dir = os.path.dirname(DataHandler.make_session_path())
        if not os.path.isdir(logs_dir):
            return
        meta = self._load_session_meta()
        files = sorted(
            [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")],
            reverse=True,
        )
        for seq, fname in enumerate(files, start=1):
            fpath = os.path.join(logs_dir, fname)
            ttype = self._detect_session_type(fpath)
            when = self._format_session_time(fname)
            try:
                size_kb = os.path.getsize(fpath) / 1024
                size_txt = f"{size_kb:.0f} KB"
            except OSError:
                size_txt = "—"
            entry   = meta.get(fname, {})
            label_s = f"  [{entry['label']}]" if entry.get("label") else ""
            tag_s   = f"  #{entry['tag']}" if entry.get("tag") else ""
            label   = f"{seq}.  {ttype:<12}{when}   ·  {size_txt}{label_s}{tag_s}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, fpath)
            item.setToolTip(f"{fname}\nType: {ttype}\n{when}  ·  {size_txt}"
                            f"{label_s}{tag_s}\nRight-click to rename/tag")
            self.lst_sessions.addItem(item)
    def _on_session_selected(self, item):
        """เลือก session file → analyze ทันทีในแท็บ Analytics เดียวกัน"""
        fpath = item.data(Qt.ItemDataRole.UserRole)
        if fpath:
            self._last_csv = fpath
            self.lbl_csv.setText(f"CSV: {os.path.basename(fpath)}")
            self._on_analyze_csv()
    def _on_toggle_logging(self):
        if self.controller is None:
            return
        if getattr(self.controller, "monitor_running", False):
            self.controller.stop_monitor()
            if self.data and self.data.is_recording:
                self.data.stop_logging()
            self.controller.start_live_readback()
            self.btn_log.setText("START DATA LOGGING")
            self._refresh_session_list()
        else:
            if not getattr(self.hw, "is_connected", False):
                if not self._headless:
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.warning(self, "Logging", "Connect hardware first")
                return
            self._ensure_battery_sn()
            self.controller.start_monitor(reuse_session=False)
            self.btn_log.setText("STOP DATA LOGGING")
            if self.data and self.data.current_path:
                self._last_csv = self.data.current_path
                self.lbl_csv.setText(f"CSV: {os.path.basename(self.data.current_path)}")
    def _on_analyze_csv(self):
        csv_path = self._last_csv or self.config.system.csv_filepath
        if not csv_path or not os.path.exists(csv_path):
            if not self._headless:
                QMessageBox.warning(self, "Analyze CSV",
                                    f"CSV not found:\n{csv_path}\n\nRun a test first.")
            return
        self.lbl_analytics.setText(f"Analyzing {os.path.basename(csv_path)}...")
        prof = self._acq_profile()
        # analyze_csv()'s own Mode-column auto-detection is dead in practice — the
        # CSV writer (DataHandler.log_row) never writes a Mode column, so without an
        # explicit force_hppc hint every re-analysis default-classifies as a plain
        # capacity/discharge test. An HPPC record read that way "never reaches
        # cut-off" (it isn't supposed to) and comes back ungradeable (grade REVIEW,
        # shown as N/A) even though the same file grades correctly right after the
        # sequence finishes (sequences.py calls _auto_analyze(force_hppc=True) then).
        # self._current_test_name (set by _seq_common_start, persists after the
        # sequence completes) is the best in-session signal. Fall back to
        # _detect_session_type()'s filename/Mode-column sniffing — the same
        # classifier that labels this file in the session list — so re-analysing
        # an older, properly test_HPPC_*-labelled file still works after an app
        # restart even though _current_test_name is gone by then.
        force_hppc = "hppc" in getattr(self, "_current_test_name", "").lower()
        if not force_hppc:
            force_hppc = self._detect_session_type(csv_path).lower() == "hppc"

        def work():
            from aset_batt.acquisition.analysis import analyze_csv_mp
            try:
                res = analyze_csv_mp(csv_path, prof, force_hppc=force_hppc)
            except Exception as e:
                res = {"error": str(e)}
            self.sig_analysis_done.emit(res)   # → _slot_analysis_done → _on_test_finished

        threading.Thread(target=work, daemon=True).start()