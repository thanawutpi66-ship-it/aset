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

class DialogsMixin:
    def _on_update_clicked(self):
        if self._updating or self._headless:
            return
        if self._seq_running.is_set():
            QMessageBox.warning(self, "Update",
                                "หยุดการทดสอบที่กำลังรันก่อนอัปเดต")
            return
        reply = QMessageBox.question(
            self, "อัปเดตโปรแกรม",
            "ดึงอัปเดตล่าสุดจาก GitHub?\n\nจะดึงเฉพาะเมื่อ fast-forward ได้ "
            "(ไม่ทับไฟล์ที่แก้ค้าง) — หลังอัปเดตต้องปิดแล้วเปิดโปรแกรมใหม่",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._updating = True
        self.btn_update.setEnabled(False)
        self.btn_update.setText("⭯ Updating…")

        def work():
            try:
                from aset_batt.services.updater import repo_root, apply_update
                ok, msg = apply_update(repo_root())
            except Exception as exc:
                ok, msg = False, str(exc)
            self.sig_update_done.emit(bool(ok), str(msg))

        threading.Thread(target=work, daemon=True).start()
    def _on_about(self):
        if not self._headless:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.about(
                self,
                "About ASET Battery Tester",
                "ASET Battery Tester — ISA-101 Command Center\n\n"
                "มหาวิทยาลัยอุบลราชธานี  Faculty of Engineering — ASET Lab\n\n"
                "Built with PySide6 · Python",
            )
    # NOTE: no QPalette-based dark mode here — theming is owned end-to-end by
    # qt-material + aset_batt/ui/theme.py (Tools → APPEARANCE checkbox →
    # _on_theme_toggle). A resurrected _on_toggle_dark_mode would fight that
    # system's app-wide stylesheet; it was removed deliberately (ก.ค. 2026).
    def _on_sn_changed(self, text):
        if getattr(self, "config", None):
            self.config.battery.serial_number = text.strip()
    def _on_product_changed(self, name):
        prod = battery_profiles.get_product(name)
        if not prod or self.config is None:
            return
        b = self.config.battery
        b.product_name = name
        b.battery_type = prod.chemistry
        b.nominal_voltage = prod.nominal_voltage_per_cell
        b.cells_series = prod.cells_series
        b.cells_parallel = prod.cells_parallel
        b.rated_capacity = prod.rated_capacity_ah
        if prod.mass_grams:
            b.mass_grams = prod.mass_grams
        if prod.max_voltage_per_cell:
            b.max_voltage = prod.max_voltage_per_cell
        if prod.min_voltage_per_cell:
            b.min_voltage = prod.min_voltage_per_cell
        # อัป max_current จากสเปคแบต (ถ้าระบุ) — ใช้เป็น clamp สำหรับ 1C Quick Scan
        if prod.max_cont_discharge_a:
            b.max_current = prod.max_cont_discharge_a
        if self.config.system.safety_limits:
            if prod.safety_ovp_pack:
                self.config.system.safety_limits["max_voltage"] = prod.safety_ovp_pack
            if prod.safety_uvp_pack:
                self.config.system.safety_limits["min_voltage"] = prod.safety_uvp_pack
            # safety max_current = peak ถ้ามี, ไม่งั้นใช้ cont * 1.5
            if prod.max_peak_discharge_a:
                self.config.system.safety_limits["max_current"] = prod.max_peak_discharge_a
            elif prod.max_cont_discharge_a:
                self.config.system.safety_limits["max_current"] = prod.max_cont_discharge_a * 1.5
        try:
            from aset_batt.core.battery_model import BatteryModel

            model = BatteryModel(b.battery_type, b.nominal_voltage, b.cells_series, b.cells_parallel,
                                 product_name=b.product_name)
            # Per-product Peukert override (e.g. 20HR standby vs 10HR motorcycle).
            # Copy the shared chemistry instance so we never mutate the registry cache.
            ov_k  = getattr(prod, "peukert_k", 0.0)
            ov_hr = getattr(prod, "peukert_hr", 0.0)
            if ov_k or ov_hr:
                import dataclasses
                model.chemistry = dataclasses.replace(
                    model.chemistry,
                    peukert_k=ov_k or model.chemistry.peukert_k,
                    peukert_hr=ov_hr or model.chemistry.peukert_hr,
                )
            if self.estimator is not None:
                self.estimator.battery_model = model
                if hasattr(self.estimator, "rated_capacity"):
                    self.estimator.rated_capacity = b.rated_capacity
                # A different product selection means a different physical battery —
                # any SoH this instance measured on whatever was tested before no
                # longer applies (see reset_battery_state's docstring for the failure
                # this prevents: coulomb counting racing to 100% during bulk charge).
                if hasattr(self.estimator, "reset_battery_state"):
                    self.estimator.reset_battery_state()
            self.iec_standard = IEC61960Standard(b.rated_capacity, b.battery_type, b.pack_nominal_voltage)
        except Exception as exc:
            logger.error("apply product: %s", exc)
        # อัป CHARGE step description ให้ตรงกับ strategy ของเคมีแบต
        cp   = battery_profiles.get_chemistry(prod.chemistry).charge
        charge_desc = "Full 3-stage (Bulk→Absorption→Float)" if cp.strategy == "three_stage" else "CC-CV"
        if len(self._wf_desc_lbls) > 1:
            self._wf_desc_lbls[1].setText(charge_desc)

        # Sync C-rate selector กับค่า default ของ profile (ถ้ามีใน list)
        default_crate_text = f"{cp.bulk_c_rate:g}C"
        idx = self.cb_seq_crate.findText(default_crate_text)
        if idx >= 0:
            self.cb_seq_crate.setCurrentIndex(idx)
        # Force-อัป lbl_seq_crate_a เสมอ (capacity อาจเปลี่ยนแม้ C-rate text เหมือนเดิม)
        self._on_seq_crate_changed(self.cb_seq_crate.currentText())

        # Reset charge mode → "Auto (by chemistry)" ให้สอดคล้องกับแบตใหม่
        self.cb_charge_mode.setCurrentText("Auto (by chemistry)")

        # อัป IEC TEST step (index 3) → แสดง A จริงของ C-rate ที่เลือก
        try:
            c_test = float(self.cb_test_crate.currentText().rstrip("C"))
        except (AttributeError, ValueError):
            c_test = 0.2
        i_test = round(c_test * prod.rated_capacity_ah, 2)
        if len(self._wf_desc_lbls) > 3:
            self._wf_desc_lbls[3].setText(f"Discharge {c_test:g}C = {i_test:.3f} A")
        if hasattr(self, "lbl_test_crate_a"):
            self.lbl_test_crate_a.setText(f"= {i_test:.3f} A")

        # อัป Quick Scan DISCHARGE step (index 2) → แสดง A จริงของ 1C
        i_1c = prod.max_cont_discharge_a if prod.max_cont_discharge_a else prod.rated_capacity_ah
        if len(self._qs_desc_lbls) > 2:
            self._qs_desc_lbls[2].setText(f"1C = {i_1c:.3f} A")

        self._refresh_battery_readout()
        self._log_alarm(f"Selected product: {name} → {prod.chemistry} {prod.cells_series}S")
        # refresh characterize tab params panel (if already built)
        if hasattr(self, "txt_char_params"):
            self._refresh_char_params()
        if hasattr(self, "_wf_time_lbls"):
            self._refresh_step_time_estimates()
    def _on_test_crate_changed(self, text: str):
        """ผู้ใช้เปลี่ยน Test discharge C-rate — อัป amp label + WF step desc"""
        try:
            c_test = float(text.rstrip("C"))
        except ValueError:
            return
        prod_name = self.cb_product.currentText() if hasattr(self, "cb_product") else ""
        prod = battery_profiles.get_product(prod_name)
        cap = prod.rated_capacity_ah if prod else (
            self.config.battery.rated_capacity if self.config else 0.0)
        i_test = round(c_test * cap, 2) if cap else 0.0
        if hasattr(self, "lbl_test_crate_a"):
            self.lbl_test_crate_a.setText(f"= {i_test:.3f} A" if cap else "— A")
        if len(self._wf_desc_lbls) > 3:
            self._wf_desc_lbls[3].setText(
                f"Discharge {c_test:g}C = {i_test:.3f} A" if cap else f"Discharge {c_test:g}C"
            )
        self._refresh_step_time_estimates()
    def _on_seq_crate_changed(self, text: str):
        """ผู้ใช้เปลี่ยน C-rate selector — อัป amp label + stage breakdown"""
        try:
            c_rate = float(text.rstrip("C"))
        except ValueError:
            return
        prod_name = self.cb_product.currentText() if hasattr(self, "cb_product") else ""
        prod = battery_profiles.get_product(prod_name)
        cap = prod.rated_capacity_ah if prod else (
            self.config.battery.rated_capacity if self.config else 0.0)
        self.lbl_seq_crate_a.setText(f"= {c_rate * cap:.3f} A" if cap else "— A")
        if prod:
            self._update_charge_crate_label(prod, c_rate_override=c_rate)
        self._refresh_step_time_estimates()
    def _update_charge_crate_label(self, prod, c_rate_override: float = None):
        """สร้างข้อความ stage breakdown และอัป lbl_charge_crate"""
        cp    = battery_profiles.get_chemistry(prod.chemistry).charge
        cap   = prod.rated_capacity_ah
        s     = prod.cells_series
        c_rate = c_rate_override if c_rate_override is not None else cp.bulk_c_rate
        i_bulk = c_rate * cap
        i_tail = cp.tail_current_c_rate * cap
        if cp.strategy == "cc_cv":
            cv_v = cp.cv_voltage_per_cell * s
            lines = [
                f"① CC: {c_rate:.2g}C = {i_bulk:.3f} A",
                f"② CV: {cv_v:.1f} V  (กระแส taper ลง)",
                f"จบเมื่อ < {cp.tail_current_c_rate:.2g}C = {i_tail:.3f} A",
            ]
        else:
            abs_v = cp.absorption_voltage_per_cell * s
            flt_v = cp.float_voltage_per_cell * s
            lines = [
                f"① Bulk CC: {c_rate:.2g}C = {i_bulk:.3f} A",
                f"② Absorption CV: {abs_v:.1f} V  (taper)",
                f"③ Float: {flt_v:.1f} V  "
                f"(จบเมื่อ < {cp.tail_current_c_rate:.2g}C = {i_tail:.3f} A)",
            ]
        self.lbl_charge_crate.setText("\n".join(lines))
    def _on_save_default(self):
        if self.config.save_config():
            self._log_alarm("Saved as default (config.json).")
            if not self._headless:
                QMessageBox.information(self, "Save as Default", "config.json saved")
        elif not self._headless:
            QMessageBox.critical(self, "Save as Default", "Save failed")
    def _on_edit_battery_profile(self):
        """In-app dialog to edit BatteryConfig fields and save to config.json."""
        b = self.config.battery
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Battery Profile")
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        form.setSpacing(6)

        fields = [
            ("battery_type",  "Chemistry / Type",   str),
            ("nominal_voltage","Nominal V (per cell)", float),
            ("max_voltage",    "Max V (per cell)",   float),
            ("min_voltage",    "Min V cutoff (per cell)", float),
            ("rated_capacity", "Rated Capacity (Ah)", float),
            ("max_current",    "Max Current (A)",    float),
            ("cells_series",   "Cells Series",       int),
            ("cells_parallel", "Cells Parallel",     int),
            ("mass_grams",     "Mass (g)",           float),
        ]
        editors: dict[str, QLineEdit] = {}
        for attr, label, _typ in fields:
            ed = QLineEdit(str(getattr(b, attr, "")))
            form.addRow(label + ":", ed)
            editors[attr] = ed
        lay.addLayout(form)

        hint = QLabel("Changes saved to config.json and applied immediately.")
        hint.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_ok = _btn("Save", bg="INFO", fg="white", hover="#0d4a89")
        btn_cancel = _btn("Cancel", bg="PANEL", hover="PANEL2")
        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_ok, 2); btn_row.addWidget(btn_cancel, 1)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        errors = []
        for attr, label, typ in fields:
            raw = editors[attr].text().strip()
            try:
                setattr(b, attr, typ(raw))
            except (ValueError, TypeError):
                errors.append(f"{label}: '{raw}' is not a valid {typ.__name__}")
        if errors:
            QMessageBox.warning(self, "Edit Battery Profile",
                                "Some fields were invalid:\n" + "\n".join(errors))
        self.config.save_config()
        self._on_product_changed(self.cb_product.currentText())
        self._log_alarm("[CONFIG] Battery profile updated and saved")
    def _on_detect_chemistry(self):
        if self.estimator is None:
            return
        try:
            model = self.estimator.battery_model
            v, s = ChemistryDetector.features_from_model(model)
            res = ChemistryDetector().detect(v, s)
            self._log_alarm(f"Chemistry detect → {res.chemistry} ({res.confidence * 100:.0f}%)")
            if not self._headless:
                ans = QMessageBox.question(
                    self,
                    "Chemistry Detection",
                    f"Detected: {res.chemistry}\nConfidence: {res.confidence * 100:.0f}%\n\nDo you want to apply a matching profile?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if ans == QMessageBox.StandardButton.Yes:
                    for p in battery_profiles.list_products():
                        if battery_profiles.get_product(p).chemistry == res.chemistry:
                            self.cb_product.setCurrentText(p)
                            break
        except Exception as exc:
            if not self._headless:
                QMessageBox.warning(self, "Chemistry Detection", str(exc))
    def _cloud_push_start(self):
        if not getattr(self.config.system, "cloud_push_enabled", False):
            return
        if self._cloud_svc is not None and getattr(self._cloud_svc, "_running", False):
            return  # already running — avoid spawning a duplicate push thread
        try:
            from aset_batt.storage.cloud_push import CloudPusher
            self._cloud_svc = CloudPusher(
                url=self.config.system.cloud_dashboard_url,
                csv_path=self.config.system.csv_filepath,
                interval=getattr(self.config.system, "cloud_push_interval", 5.0),
                analysis_interval=getattr(self.config.system, "cloud_analysis_interval", 60.0),
                data_handler=self.data,
                config=self.config,
            )
            self._cloud_svc.start()
            if self._cloud_svc.enabled:
                self._log_alarm("[CLOUD] Push service started")
        except Exception as e:
            self._log_alarm(f"[CLOUD] Start failed: {e}")
    def _cloud_push_stop(self):
        try:
            if self._cloud_svc:
                self._cloud_svc.stop()
                self._cloud_svc = None
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    def _on_pdf_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF Report", "battery_report.pdf", "PDF (*.pdf)")
        if not path:
            return
        self.btn_pdf.setEnabled(False)
        self.btn_pdf.setText("Generating...")
        task = _PdfTask(self._pdf_notifier, path, self.config, self.estimator,
                        self._last_analysis, self._last_csv or self.config.system.csv_filepath)
        self.thread_pool.start(task)
    def _on_pdf_finished(self, ok: bool, payload: str):
        self.btn_pdf.setEnabled(True)
        self.btn_pdf.setText("Generate PDF Report")
        if ok:
            self._log_alarm(f"PDF generated: {payload}")
            if not self._headless:
                QMessageBox.information(self, "PDF Report", f"Saved:\n{payload}")
        else:
            self._log_alarm(f"PDF failed: {payload}")
            if not self._headless:
                QMessageBox.critical(self, "PDF Report", payload)
    def _on_soh_trend(self):
        """Parse all sessions for SoH, show a matplotlib window with timeline."""
        import threading
        threading.Thread(target=self._soh_trend_worker, daemon=True).start()
    def _soh_trend_worker(self):
        try:
            import matplotlib
            matplotlib.use("Qt5Agg")
            import matplotlib.pyplot as plt
            from aset_batt.acquisition.analysis import analyze_csv_mp, profile_from_config

            from aset_batt.storage.data_utils import DataHandler
            logs_dir = os.path.dirname(DataHandler.make_session_path())
            if not os.path.isdir(logs_dir):
                return
            files = sorted(
                [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")]
            )
            profile = profile_from_config(self.config)
            dates, sohs, labels = [], [], []
            for fname in files:
                fpath = os.path.join(logs_dir, fname)
                try:
                    res = analyze_csv_mp(fpath, profile)
                    import math
                    if not math.isnan(res.get("soh", float("nan"))):
                        from datetime import datetime as _dt
                        stem = fname[len("test_"):-len(".csv")]
                        d = _dt.strptime(stem, "%Y%m%d_%H%M%S")
                        dates.append(d)
                        sohs.append(res["soh"])
                        meta = self._load_session_meta()
                        e = meta.get(fname, {})
                        labels.append(e.get("label") or e.get("tag") or stem[-6:])
                except Exception:
                    continue

            if not sohs:
                self.sig_alarm.emit("[TREND] No sessions with valid SoH found")
                return
            fig, ax = plt.subplots(figsize=(9, 4))
            ax.plot(dates, sohs, "o-", color="#005a9e", linewidth=1.8)
            for d, s, lb in zip(dates, sohs, labels):
                ax.annotate(f"{s:.1f}%", (d, s), textcoords="offset points",
                            xytext=(0, 7), ha="center", fontsize=8)
            ax.axhline(80, color="orange", linestyle="--", linewidth=0.9, label="80% SoH limit")
            ax.set_ylabel("SoH (%)")
            ax.set_title("State of Health Trend")
            ax.legend()
            ax.grid(True, alpha=0.3)
            fig.autofmt_xdate()
            fig.tight_layout()
            plt.show()
        except Exception as e:
            self.sig_alarm.emit(f"[TREND] Error: {e}")
    def _on_capacity_fade(self):
        """Parse cycle-life sessions and show capacity fade bar chart."""
        import threading
        threading.Thread(target=self._capacity_fade_worker, daemon=True).start()
    def _capacity_fade_worker(self):
        try:
            import matplotlib
            matplotlib.use("Qt5Agg")
            import matplotlib.pyplot as plt
            import csv as _csv
            from aset_batt.storage.data_utils import DataHandler

            logs_dir = os.path.dirname(DataHandler.make_session_path())
            if not os.path.isdir(logs_dir):
                return
            files = sorted(
                [f for f in os.listdir(logs_dir) if f.startswith("test_") and f.endswith(".csv")]
            )
            # collect per-session capacity
            session_caps = []
            session_labels = []
            meta = self._load_session_meta()
            for fname in files:
                fpath = os.path.join(logs_dir, fname)
                try:
                    cap_ah = 0.0
                    with open(fpath, encoding="utf-8-sig") as f:
                        reader = _csv.DictReader(f)
                        rows = list(reader)
                    # find last Capacity_Ah value
                    for row in reversed(rows):
                        v = row.get("Capacity_Ah") or row.get("capacity_ah", "")
                        try:
                            cap_ah = float(v)
                            break
                        except (ValueError, TypeError):
                            continue
                    if cap_ah > 0.01:
                        e = meta.get(fname, {})
                        stem = fname[len("test_"):-len(".csv")]
                        session_caps.append(cap_ah)
                        session_labels.append(e.get("label") or stem[-8:])
                except Exception:
                    continue

            if not session_caps:
                self.sig_alarm.emit("[FADE] No sessions with capacity data found")
                return

            fig, ax = plt.subplots(figsize=(max(6, len(session_caps) * 0.6 + 2), 4))
            colors_list = ["#005a9e" if c >= session_caps[0] * 0.8 else "#d83b01"
                           for c in session_caps]
            bars = ax.bar(range(len(session_caps)), session_caps, color=colors_list)
            ax.set_xticks(range(len(session_caps)))
            ax.set_xticklabels(session_labels, rotation=45, ha="right", fontsize=8)
            ax.set_ylabel("Capacity (Ah)")
            ax.set_title("Capacity Fade — Session History")
            ax.axhline(session_caps[0] * 0.8, color="orange", linestyle="--",
                       linewidth=0.9, label="80% of first session")
            for bar, cap in zip(bars, session_caps):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.002,
                        f"{cap:.3f}", ha="center", va="bottom", fontsize=7)
            ax.legend()
            ax.grid(axis="y", alpha=0.3)
            fig.tight_layout()
            plt.show()
        except Exception as e:
            self.sig_alarm.emit(f"[FADE] Error: {e}")
    def _on_cloud_push_toggle(self, state):
        enabled = bool(state)
        self.config.system.cloud_push_enabled = enabled
        self.config.save_config()
        if enabled:
            self._cloud_push_stop()
            self._cloud_push_start()
        else:
            self._cloud_push_stop()
        self._log_alarm(f"[CLOUD] Push {'enabled' if enabled else 'disabled'}")
    def _on_cloud_url_changed(self):
        url = self.ed_cloud_url.text().strip()
        self.config.system.cloud_dashboard_url = url
        self.config.save_config()
        # restart push service with new URL
        if getattr(self.config.system, "cloud_push_enabled", False):
            self._cloud_push_stop()
            self._cloud_push_start()
    def _on_theme_toggle(self, state):
        mode = "dark" if bool(state) else "light"
        self.config.system.ui_theme = mode
        self.config.save_config()
        # QApplication.setStyleSheet() re-matches every QSS selector against the
        # whole widget tree — on a window this size that's genuinely ~2-3s of CPU
        # work no matter how it's called, so give clear feedback instead of
        # letting the UI just look frozen for a couple of seconds.
        app = QApplication.instance()
        app.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # Each stage runs independently — a failure in one (e.g. a bad
            # qt-material install) must not silently skip the others, which
            # would leave the UI in a half-retheme'd state with no visible error.
            try:
                theme.retheme(mode)
            except Exception:
                logger.exception("theme.retheme(%s) failed", mode)
            try:
                # get_material_stylesheet() caches the built CSS per theme, and
                # setStyle("Fusion") only needs to run once at startup (the
                # style itself never changes, only the stylesheet colors do —
                # re-setting it here would cost another ~2s for nothing).
                app.setStyleSheet(theme.get_material_stylesheet(mode))
            except ImportError:
                logger.warning("qt-material not installed — theme colors updated, "
                                "but app-wide widget chrome did not.")
            except Exception:
                logger.exception("apply_stylesheet(%s) failed", mode)
            try:
                self._on_retheme()
            except Exception:
                logger.exception("_on_retheme() failed")
        finally:
            app.restoreOverrideCursor()
        self._log_alarm(f"[UI] Theme switched to {mode}")
    def _on_open_dashboard(self):
        url = getattr(self.config.system, "cloud_dashboard_url", "").strip()
        if url:
            webbrowser.open(url)
            return
        # Local web server removed; inform the user instead of opening localhost
        if not self._headless:
            QMessageBox.information(self, "Cloud Dashboard", "Cloud dashboard URL not configured. See cloud_dashboard/README.md for deployment instructions.")
        else:
            logger.warning("Cloud dashboard URL not configured")
    def _show_text_dialog(self, title, text):
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text[:4000])
        dlg.exec()
    def _shutdown_services(self):
        """Tear down the controller + the analysis worker pool on quit. The pool must
        be shut down or a running curve_fit keeps a child process (and CPU) alive and
        the interpreter hangs on atexit joining it. Also stops the window's own
        QTimers — closeEvent() doesn't destroy the underlying C++ object (that needs
        deleteLater() + the event loop actually running), so a merely-closed window's
        heartbeat/pulse/flash timers otherwise keep firing for the rest of the
        process's life against a "closed" window, which is harmless in production
        (the process exits right after) but silently compounds in any long-lived
        process that constructs more than one window, e.g. the test suite."""
        for timer_attr in ("_tick", "_pulse_timer", "_flash_timer"):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                timer.stop()
        # หยุดเธรดทดสอบทุกตัวก่อนตัดไฟ — ไม่งั้นเธรด daemon ที่ยังวิ่งอยู่อาจสั่ง
        # output กลับมาเปิดหลัง controller.shutdown() ตัดไฟไปแล้ว หรือค้างวน error
        # จนโปรเซสจบ (sequence 4 ตัวใช้ _seq_running, CHARACTERIZE ใช้ event
        # ราย-เทสต์ใน _char_running, RUN TEST ใช้ AcquisitionWorker ใน _test_worker)
        try:
            self._seq_running.clear()
            for char_ev in getattr(self, "_char_running", {}).values():
                char_ev.clear()
            if getattr(self, "_test_worker", None) is not None:
                self._test_worker.stop()
        except Exception as exc:
            logger.error("stopping test threads on close: %s", exc)
        try:
            if self.controller:
                self.controller.shutdown()
        except Exception as exc:
            logger.error("shutdown on close: %s", exc)
        try:
            from aset_batt.acquisition.analysis import shutdown_analysis_pool
            shutdown_analysis_pool()
        except Exception as exc:
            logger.error("analysis pool shutdown on close: %s", exc)
    def _on_open_settings(self):
        # Lazy import: SettingsDialog lives in isa101_views.py, which imports
        # DialogsMixin (this class) — a top-level import here would be
        # circular. This was previously missing entirely, so Tools ->
        # Preferences raised NameError before the dialog could even open.
        from aset_batt.ui.isa101_views import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.exec()