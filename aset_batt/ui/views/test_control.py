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

class TestControlMixin:
    def _on_charge(self):
        if self.controller is None:
            return
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Charge", "Connect hardware first")
            return
            
        mode_txt = self.cb_manual_charge_mode.currentText()
        strategy = {"CC-CV": "cc_cv", "3-Stage (Lead-Acid)": "three_stage"}.get(mode_txt)
        
        crate_override = None
        c_txt = self.cb_manual_charge_crate.currentText().replace("C", "")
        try:
            crate_override = float(c_txt)
        except ValueError:
            pass
            
        ok = self.controller.start_charge(strategy=strategy, bulk_c_rate_override=crate_override)
        mode = self.cb_charge_mode.currentText()
        self._log_alarm(f"Charge started ({mode})." if ok else "Charge start failed.")
        if ok:
            try:
                from aset_batt.storage.cloud_push import set_cloud_meta
                set_cloud_meta(phase="charge", test_mode="MANUAL", workflow=f"Manual — {mode}", total_s=0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    def _on_stop_charge(self):
        if self.controller:
            self.controller.stop_charge()
            self._log_alarm("Charge stopped.")
            try:
                from aset_batt.storage.cloud_push import set_cloud_meta
                set_cloud_meta(phase="", test_mode="", workflow="")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    def _acq_profile(self) -> AcqProfile:
        """Analysis/test profile from config (shared with the controller's
        auto-analyze), plus the HPPC durations entered in the Test Config panel."""
        from aset_batt.acquisition.analysis import profile_from_config

        def _fld(widget, default):
            try:
                return max(1.0, float(widget.text()))
            except (ValueError, AttributeError):
                return default

        p = profile_from_config(self.config)
        p.hppc_pulse_duration = _fld(self.ed_hppc_pulse, 30.0)
        p.hppc_relaxation_duration = _fld(self.ed_hppc_relax, 30.0)
        p.hppc_pulse_crate = _fld(self.ed_hppc_crate, 1.0)
        return p
    def _ensure_battery_sn(self):
        """Auto-generate a serial number if none is provided."""
        sn = self.config.battery.serial_number
        if not sn or not str(sn).strip():
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            new_sn = f"UNSPECIFIED_BATT_{ts}"
            self.config.battery.serial_number = new_sn
            if hasattr(self, "ed_sn"):
                self.ed_sn.setText(new_sn)
            self._log_alarm(f"No SN provided, auto-generated: {new_sn}")
    def _on_run_hppc(self):
        self._on_run_test(mode=OperationMode.HPPC)
    def _on_run_test(self, mode=None):
        if self._test_thread is not None:
            return
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Run Test", "Connect hardware first")
            return
        busy = self._busy_reason()
        if busy:
            if not self._headless:
                QMessageBox.warning(self, "Run Test", f"{busy} — หยุดก่อนแล้วค่อยเริ่มใหม่")
            return
        if self.controller and self.controller.is_charging:
            self.controller.stop_charge()
            self._log_alarm("Charge stopped (auto) — starting test.")
        if self.controller and self.controller.monitor_running:
            self.controller.stop_monitor()
            
        self._ensure_battery_sn()

        op_mode = mode or OperationMode(self.cb_op_mode.currentText())
        cfg = TestConfig(self._acq_profile(), op_mode)
        
        # Override with manual inputs if we are in manual discharge
        if op_mode not in (OperationMode.HPPC, OperationMode.CC_CV_CHARGE):
            # C-rate override
            if hasattr(self, 'cb_manual_discharge_crate'):
                c_txt = self.cb_manual_discharge_crate.currentText().replace("C", "")
                try:
                    cfg.profile.discharge_c_rate = float(c_txt)
                except ValueError:
                    pass
            # Cutoff V override
            if hasattr(self, 'ed_manual_cutoff_v'):
                v_txt = self.ed_manual_cutoff_v.text().strip()
                if v_txt:
                    try:
                        cfg.profile.cutoff_v = float(v_txt)
                    except ValueError:
                        pass
        
        self.buf_t.clear(); self.buf_v.clear(); self.buf_i.clear()
        self.buf_soc.clear(); self.buf_rin.clear(); self.buf_temp.clear()
        self._elapsed_t0 = None
        self._last_hppc_phase_text = None   # force the first sample of this run to render
        from aset_batt.storage.data_utils import DataHandler
        csv_path = DataHandler.make_session_path()
        self._last_csv = csv_path
        self.lbl_csv.setText(f"CSV: {csv_path}")

        backend = HardwareBackend(self.hw)
        self._test_thread = QThread()
        self._test_worker = AcquisitionWorker(backend, cfg, csv_path, estimator=self.estimator)
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.telemetry.connect(self._on_test_telemetry)
        self._test_worker.alarm.connect(lambda sev, msg: self._log_alarm(f"[{sev}] {msg}"))
        self._test_worker.state.connect(lambda st: self.lbl_test_status.setText(f"Test: {st}"))
        self._test_worker.finished.connect(self._on_test_finished)
        self._test_worker.finished.connect(self._test_thread.quit)
        self._test_thread.finished.connect(self._cleanup_test_thread)
        if op_mode == OperationMode.HPPC:
            self._test_worker.telemetry.connect(self._on_hppc_telemetry)
            self.btn_run_hppc.setEnabled(False)
            self.lbl_hppc_phase.setText("RUNNING…")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{theme.INFO}; color:white; border:1px solid {theme.BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        else:
            self.btn_run_test.setEnabled(False)
        self._test_thread.start()
        self._log_alarm(f"Characterization started: {cfg.mode.value}")
    def _on_stop_test(self):
        if self._test_worker:
            self._test_worker.stop()
            self._log_alarm("Test stop requested.")
    def _on_test_telemetry(self, row: dict):
        self.buf_t.append(row["elapsed"]); self.buf_v.append(row["v"])
        self.buf_i.append(row["i"]); self.buf_temp.append(row["temp"])
        v_lbl = self.metric_labels.get("Voltage")
        if v_lbl:
            self.metric_labels["Voltage"][0].setText(f'{row["v"]:.2f} {self.metric_labels["Voltage"][1]}')
            self.metric_labels["Current"][0].setText(f'{row["i"]:.3f} {self.metric_labels["Current"][1]}')
            if row.get("soc") == row.get("soc"):  # not NaN
                _u = self.metric_labels["SoC"][1]
                _std = row.get("soc_std", getattr(getattr(self, "estimator", None), "soc_std", None))
                if _std is not None and _std == _std:
                    self.metric_labels["SoC"][0].setText(f'{row["soc"]:.1f} ±{min(_std, 99):.0f} {_u}')
                else:
                    self.metric_labels["SoC"][0].setText(f'{row["soc"]:.1f} {_u}')
            self.metric_labels["Temp"][0].setText(f'{row["temp"]:.1f} {self.metric_labels["Temp"][1]}')
        self._set_temp_label_color(row["temp"])
        # Throttled the same way as _slot_display — see its comment.
        import time as _time
        _now_redraw = _time.perf_counter()
        if _now_redraw - self._last_trend_redraw >= 0.2:
            self._last_trend_redraw = _now_redraw
            self.trend.update(list(self.buf_t), list(self.buf_v), list(self.buf_i), list(self.buf_temp))
    def _on_hppc_telemetry(self, row: dict):
        """Update the HPPC phase indicator (REST / PULSE / cycle count) from elapsed time.

        setText()+setStyleSheet() ran unconditionally on EVERY sample regardless of
        whether the phase/remaining-seconds text actually changed — unlike
        _on_test_telemetry's trend redraw (throttled to ~5 Hz), this had no guard at
        all. setStyleSheet() in particular re-parses the whole stylesheet string and
        can cost real time; at the worker's target 5 Hz that's up to 5x/s of Qt
        style recalculation for a label whose text only changes once a second (the
        "remaining seconds" countdown). A real-rig log showed the manual HPPC run's
        per-sample cost 80-92% unaccounted for ("other") despite SCPI being only
        8-20% — this call runs synchronously on the GUI thread reacting to the same
        signal that also feeds the worker thread's own pacing, so GIL contention
        here can slow the WORKER down too, not just the GUI. Skip the Qt calls when
        the text hasn't changed."""
        try:
            pulse = max(1.0, float(self.ed_hppc_pulse.text() or "30"))
            relax = max(1.0, float(self.ed_hppc_relax.text() or "30"))
            elapsed = row["elapsed"]
            cycle = pulse + relax
            cycle_num = int(elapsed / cycle) + 1
            t_in_cycle = elapsed % cycle
            if t_in_cycle < relax:
                remaining = int(relax - t_in_cycle)
                text = f"Cycle {cycle_num}  ·  REST  ({remaining} s left)"
                bg, fg = theme.PANEL2, theme.MUTED
            else:
                remaining = int(pulse - (t_in_cycle - relax))
                text = f"Cycle {cycle_num}  ·  PULSE  ({remaining} s left)"
                bg, fg = theme.OK, "white"
            if text == self._last_hppc_phase_text:
                return
            self._last_hppc_phase_text = text
            self.lbl_hppc_phase.setText(text)
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{bg}; color:{fg}; border:1px solid {theme.BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    def _on_test_finished(self, results: dict):
        # Final unthrottled redraw — _slot_display's redraw is rate-limited to ~5 Hz
        # (see its own comment), so the very last sample or two collected right before
        # the test ended could still be sitting un-painted when this fires.
        if self.buf_t:
            self.trend.update(list(self.buf_t), list(self.buf_v), list(self.buf_i), list(self.buf_temp))
        # SoH is N/A when not measurable (e.g. HPPC pulse test — see analyze_series).
        # Written to the separate "Analysis Results" row (metric_labels_final), never
        # the live telemetry row — a final result can't be mistaken for a live reading.
        soh = results["soh"]
        # D3: feed a measurable SoH into the live estimator's Rin-baseline aging
        # factor — same wiring as AcquisitionWorker.run()'s post-test feedback (the
        # command-center pipeline). NaN (not measurable, e.g. an HPPC-only test) is a
        # deliberate no-op — set_soh() is only called with a real value, so
        # aging_factor is left at whatever a PRIOR completed capacity test in this
        # session already set it to (or 1.0, the safe default, if none yet).
        if self.controller is not None and getattr(self.controller, "estimator", None) \
                is not None and soh == soh:
            self.controller.estimator.set_soh(soh)
        soh_txt = "N/A" if soh != soh else f"{soh:.1f}"   # soh != soh → NaN
        soh_final_lbl, _soh_unit = self.metric_labels_final["SoH"]
        soh_final_lbl.setText("N/A" if soh != soh else f"{soh:.1f} {_soh_unit}")
        rin_final_lbl, _rin_unit = self.metric_labels_final["Rin"]
        rin_final_lbl.setText(f"{results['ri_mohm']:.1f} {_rin_unit}")
        grade = results["grade"]
        conf = results.get("confidence", 1.0)
        self.lbl_grade.setText(grade if grade == "REVIEW" else f"{grade}")
        grade_lbl, _ = self.metric_labels_final["Grade"]
        grade_lbl.setText(grade)
        # Colors for the whole final-analysis row are state+theme hybrids —
        # cache the state, then let one shared re-apply path handle both this
        # call AND every later retheme() (see _apply_final_metric_styles).
        self._last_grade = grade
        self._last_soh_valid = (soh == soh)
        self._apply_final_metric_styles()
        dcir = results.get("dcir_mohm", results.get("ri_mohm", 0.0))
        dstd = results.get("dcir_std_mohm", 0.0)
        nstep = results.get("dcir_n_steps", 0)
        warns = results.get("quality_warnings", [])
        
        # explicitly mark fallback vs measured DCIR
        if nstep > 0:
            dcir_txt = f"DCIR {dcir:.1f}±{dstd:.1f} mΩ"
        else:
            dcir_txt = f"R_base {dcir:.1f} mΩ (No pulse)"
            
        self.lbl_analytics.setText(
            f"Grade {grade} (conf {conf*100:.0f}%) · SoH {soh_txt}% · "
            f"{dcir_txt} · Sag {results.get('voltage_sag_v', 0.0):.3f} V · "
            f"CCA~{results.get('cca_est_a', 0.0):.0f} A · Cap {results['capacity_ah']:.3f} Ah")
        # 5 Hz-measurable sorting features (see project pivot): SoH + DCIR + sag + CCA proxy
        if results.get("ecm_identified"):
            svg = self._build_ecm_svg(
                r0=results['r0_mohm'], r1=results['r1_mohm'],
                c1=results['c1_farad'], tau=results['tau_s'],
                ocv=results.get('ocv_v', 0.0),
            )
            self._ecm_identified = True
        else:
            # ไม่ใช่ HPPC — แสดงวงจรเดียวกัน แต่ R1/C1 เป็นตัวแปร (ไม่มีค่า)
            svg = self._build_ecm_svg(
                r0=results.get('dcir_mohm', results.get('ri_mohm', 0.0)),
                ocv=results.get('ocv_v', 0.0),
            )
            self._ecm_identified = False
        self.lbl_ecm_diagram.load(QByteArray(svg.encode()))
        self.btn_ecm_toggle.setEnabled(True)
        # Same style fn the theme.style() registry replays on retheme — the
        # cached _ecm_identified flag above is what switches the accent border.
        self.btn_ecm_toggle.setStyleSheet(self._ecm_toggle_style())
        self.txt_analytics.setHtml(build_results_html(results))
        iv, ic = results["ica"]
        if len(iv):
            self.plot_ica.clear(); self.plot_ica.plot(iv, ic, pen=pg.mkPen(theme.INFO, width=2))
        wmsg = f" — {len(warns)} quality flag(s), review" if warns else ""
        # echo the headline grade in the RUN zone (full breakdown is in this tab)
        if hasattr(self, "lbl_run_grade"):
            self.lbl_run_grade.setText(
                f"Grade: {grade} · SoH {soh_txt}% · conf {conf*100:.0f}%")
        if hasattr(self, "lbl_hppc_phase") and results.get("ecm_identified"):
            ecm_r2 = results.get("ecm_r2", 0.0)
            r0 = results.get("r0_mohm", 0.0)
            r1 = results.get("r1_mohm", 0.0)
            tau = results.get("tau_s", 0.0)
            self.lbl_hppc_phase.setText(
                f"DONE · R₀={r0:.1f} mΩ  R₁={r1:.1f} mΩ  τ={tau:.1f} s  R²={ecm_r2:.3f}")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{theme.INFO}; color:white; border:1px solid {theme.BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        self._log_alarm(
            f"Test complete — Grade {grade} (conf {conf*100:.0f}%), "
            f"SoH {soh_txt}%, DCIR {dcir:.1f}±{dstd:.1f} mΩ{wmsg}")
    def _cleanup_test_thread(self):
        if self._test_thread:
            self._test_thread.deleteLater()
        self._test_thread = None
        self._test_worker = None
        self.btn_run_test.setEnabled(True)
        if hasattr(self, "btn_run_hppc"):
            self.btn_run_hppc.setEnabled(True)
        if hasattr(self, "lbl_hppc_phase"):
            self.lbl_hppc_phase.setText("IDLE")
            self.lbl_hppc_phase.setStyleSheet(
                f"background:{theme.PANEL2}; color:{theme.MUTED}; border:1px solid {theme.BORDER}; "
                f"border-radius:4px; padding:5px 8px; font-weight:600; font-size:11px;"
            )
        self.lbl_test_status.setText("Test idle")

    def _on_start_monitor(self):
        if not getattr(self.hw, "is_connected", False):
            if not self._headless:
                QMessageBox.warning(self, "Monitor", "Connect hardware first")
            return
        self._ensure_battery_sn()
        self.controller.start_monitor()
        import time
        self._elapsed_t0 = time.perf_counter()   # interval only — see _slot_display
        self.status_label.setText("Monitor running")
        # แสดงชื่อ session file ที่เพิ่งสร้าง
        if self.data and self.data.current_path:
            self._last_csv = self.data.current_path
            self.lbl_csv.setText(f"CSV: {os.path.basename(self.data.current_path)}")