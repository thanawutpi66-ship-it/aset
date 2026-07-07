"""
Self-contained UI building blocks for the ISA-101 GUI: readouts, trend plots,
the Qt root shim, and the background PDF task. None of these touch
BatteryQtWindow state — they are extracted from isa101_views.py so the main
window file stays focused on layout and behavior.

Like isa101_views, this module bakes the active theme palette into
stylesheets at import/construction time, so it must only be imported after
theme.set_theme() has run (isa101_views imports it, preserving that order).
"""

import logging
import math

import pyqtgraph as pg
from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt, QRunnable
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aset_batt.ui.theme import (
    PANEL2, FIELD, BORDER, TEXT, MUTED, OK, WARN, CRIT, INFO,
)

logger = logging.getLogger(__name__)


def _btn(text, bg=PANEL2, fg=TEXT, hover=FIELD):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        "QPushButton {{ background:{0}; color:{1}; border:1px solid {2}; "
        "border-radius:4px; padding:7px 10px; font-weight:600; }}"
        "QPushButton:hover {{ background:{3}; }}".format(bg, fg, BORDER, hover)
    )
    return b


def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color:{BORDER}; background:{BORDER}; max-height:1px;")
    return line


class QtRootShim(QObject):
    _invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception as exc:
            logger.error("QtRootShim invoke error: %s", exc)

    def after(self, ms, fn=None, *args):
        if fn is None:
            return
        cb = (lambda: fn(*args)) if args else fn
        if ms and ms > 0:
            QTimer.singleShot(int(ms), lambda: self._invoke.emit(cb))
        else:
            self._invoke.emit(cb)

    def protocol(self, name, fn):
        self._close_handler = fn

    def destroy(self):
        QApplication.quit()


class DigitalReadout(QFrame):
    def __init__(self, label: str, unit: str):
        super().__init__()
        self.unit = unit
        self.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(1)
        cap = QLabel(label.upper())
        cap.setStyleSheet(
            f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;"
        )
        self.value = QLabel(f"-- {unit}")
        self.value.setFont(QFont("Consolas", 20, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap)
        lay.addWidget(self.value)

    def set_value(self, value: float, fmt: str = "{:.3f}", alarm: bool = False):
        self.value.setText(f"{fmt.format(value)} {self.unit}")
        self.value.setStyleSheet(f"color:{CRIT if alarm else TEXT}; border:0;")


class TemperatureGauge(QFrame):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(
            f"QFrame {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 10)
        cap = QLabel("CASE TEMPERATURE")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(
            f"color:{MUTED}; font-size:10px; font-weight:700; letter-spacing:1px; border:0;"
        )
        self.value = QLabel("-- °C")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setFont(QFont("Consolas", 30, QFont.Weight.Bold))
        self.value.setStyleSheet(f"color:{TEXT}; border:0;")
        lay.addWidget(cap)
        lay.addWidget(self.value)

    def update_temp(self, temp: float, warn: float, crit: float):
        if math.isnan(temp):
            self.value.setText("-- °C")
            return
        color = CRIT if temp >= crit else WARN if temp >= warn else OK
        self.value.setText(f"{temp:.1f} °C")
        self.value.setStyleSheet(f"color:{color}; border:0;")


class MultiAxisTrend(pg.GraphicsLayoutWidget):
    """Voltage (left) + Current (right) + Temperature (far right) over time."""

    def __init__(self):
        super().__init__()
        self.setBackground(PANEL2)
        self.p = self.addPlot()
        self.p.setLabel("bottom", "Elapsed", units="s")
        self.p.setLabel("left", "Voltage", units="V", color=INFO)
        self.p.showGrid(x=True, y=True, alpha=0.2)
        self.p.getAxis("left").setPen(INFO)

        self.vb_i = pg.ViewBox()
        self.p.showAxis("right")
        self.p.scene().addItem(self.vb_i)
        self.p.getAxis("right").linkToView(self.vb_i)
        self.p.getAxis("right").setLabel("Current", units="A", color=WARN)
        self.p.getAxis("right").setPen(WARN)
        self.vb_i.setXLink(self.p)

        self.ax_t = pg.AxisItem("right")
        self.p.layout.addItem(self.ax_t, 2, 3)
        self.vb_t = pg.ViewBox()
        self.p.scene().addItem(self.vb_t)
        self.ax_t.linkToView(self.vb_t)
        self.ax_t.setLabel("Temp", units="°C", color=CRIT)
        self.ax_t.setPen(CRIT)
        self.vb_t.setXLink(self.p)

        self.c_v = self.p.plot(pen=pg.mkPen(INFO, width=2))
        self.c_i = pg.PlotCurveItem(pen=pg.mkPen(WARN, width=2))
        self.c_t = pg.PlotCurveItem(pen=pg.mkPen(CRIT, width=2, style=Qt.PenStyle.DashLine))
        self.vb_i.addItem(self.c_i)
        self.vb_t.addItem(self.c_t)

        self.p.vb.sigResized.connect(self._sync)

    def _sync(self):
        self.vb_i.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_t.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_i.linkedViewChanged(self.p.vb, self.vb_i.XAxis)
        self.vb_t.linkedViewChanged(self.p.vb, self.vb_t.XAxis)

    def update(self, t, v, i, temp):
        self.c_v.setData(t, v)
        self.c_i.setData(t, i)
        self.c_t.setData(t, temp)


class SplitTrend(QWidget):
    """Voltage+Current (top) / Temperature (bottom) — 2 separate plots."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._vi = pg.PlotWidget()
        self._vi.setBackground(PANEL2)
        self._vi.setLabel("bottom", "Elapsed", units="s")
        self._vi.setLabel("left", "Voltage", units="V", color=INFO)
        self._vi.showGrid(x=True, y=True, alpha=0.2)
        self._vi.getAxis("left").setPen(INFO)
        self._vi.showAxis("right")
        self._vb_i = pg.ViewBox()
        self._vi.scene().addItem(self._vb_i)
        self._vi.getAxis("right").linkToView(self._vb_i)
        self._vi.getAxis("right").setLabel("Current", units="A", color=WARN)
        self._vi.getAxis("right").setPen(WARN)
        self._vb_i.setXLink(self._vi.getPlotItem())
        self._c_v = self._vi.plot(pen=pg.mkPen(INFO, width=2))
        self._c_i = pg.PlotCurveItem(pen=pg.mkPen(WARN, width=2))
        self._vb_i.addItem(self._c_i)
        self._vi.getPlotItem().vb.sigResized.connect(self._sync_vi)

        self._tp = pg.PlotWidget()
        self._tp.setBackground(PANEL2)
        self._tp.setLabel("bottom", "Elapsed", units="s")
        self._tp.setLabel("left", "Temp", units="°C", color=CRIT)
        self._tp.showGrid(x=True, y=True, alpha=0.2)
        self._tp.getAxis("left").setPen(CRIT)
        self._c_t = self._tp.plot(pen=pg.mkPen(CRIT, width=2, style=Qt.PenStyle.DashLine))

        lay.addWidget(self._vi, 3)
        lay.addWidget(self._tp, 1)

    def _sync_vi(self):
        self._vb_i.setGeometry(self._vi.getPlotItem().vb.sceneBoundingRect())
        self._vb_i.linkedViewChanged(self._vi.getPlotItem().vb, self._vb_i.XAxis)

    def update(self, t, v, i, temp):
        self._c_v.setData(t, v)
        self._c_i.setData(t, i)
        self._c_t.setData(t, temp)


class TripleTrend(QWidget):
    """Voltage / Current / Temperature — 3 fully independent plots."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        specs = [
            ("Voltage", "V", INFO, Qt.PenStyle.SolidLine),
            ("Current", "A", WARN, Qt.PenStyle.SolidLine),
            ("Temp",    "°C", CRIT, Qt.PenStyle.DashLine),
        ]
        self._curves = []
        for label, unit, color, style in specs:
            pw = pg.PlotWidget()
            pw.setBackground(PANEL2)
            pw.setLabel("bottom", "Elapsed", units="s")
            pw.setLabel("left", label, units=unit, color=color)
            pw.showGrid(x=True, y=True, alpha=0.2)
            pw.getAxis("left").setPen(color)
            curve = pw.plot(pen=pg.mkPen(color, width=2, style=style))
            self._curves.append(curve)
            lay.addWidget(pw, 1)

    def update(self, t, v, i, temp):
        for curve, data in zip(self._curves, [v, i, temp]):
            curve.setData(t, data)


class TrendContainer(QWidget):
    """Wraps the 3 trend modes with a toggle bar. Press A to toggle 10s zoom."""

    MODES = ["Combined", "Split 2", "Split 3"]
    _ZOOM_WINDOW = 10  # seconds

    def __init__(self):
        super().__init__()
        self._zoom_active = False
        self._last_t: list = []
        # Cache the latest series so a mode we switch INTO can be back-filled
        # immediately — otherwise the two hidden trend widgets never receive data
        # (update() only feeds the visible one) and show blank until the next tick,
        # which never comes after a test finishes.
        self._last_v: list = []
        self._last_i: list = []
        self._last_temp: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        bar = QHBoxLayout()
        self._zoom_btn = QPushButton("A")
        self._zoom_btn.setCheckable(True)
        self._zoom_btn.setFixedSize(24, 22)
        self._zoom_btn.setToolTip("Toggle 10s zoom")
        self._zoom_btn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{MUTED};border:1px solid {MUTED};border-radius:3px;font-weight:bold;}}"
            f"QPushButton:checked{{background:{INFO};color:#000;border:1px solid {INFO};}}"
            f"QPushButton:hover{{border-color:#aaa;}}"
        )
        self._zoom_btn.clicked.connect(self._on_zoom_btn)
        bar.addWidget(self._zoom_btn)
        bar.addStretch()
        bar.addWidget(QLabel("Graph mode:"))
        self._btn_group = QButtonGroup(self)
        for idx, label in enumerate(self.MODES):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            self._btn_group.addButton(btn, idx)
            bar.addWidget(btn)
        bar.addStretch()
        root.addLayout(bar)

        self._stack = QStackedWidget()
        self._combined = MultiAxisTrend()
        self._split2   = SplitTrend()
        self._split3   = TripleTrend()
        self._stack.addWidget(self._combined)
        self._stack.addWidget(self._split2)
        self._stack.addWidget(self._split3)
        root.addWidget(self._stack, 1)

        self._btn_group.buttons()[1].setChecked(True)
        self._stack.setCurrentIndex(1)
        self._btn_group.idClicked.connect(self._on_mode_changed)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _on_mode_changed(self, idx: int):
        self._stack.setCurrentIndex(idx)
        # Back-fill the newly-visible trend from cache so it isn't blank (it was
        # never fed while hidden, and after a test ends no further ticks arrive).
        if self._last_t:
            [self._combined, self._split2, self._split3][idx].update(
                self._last_t, self._last_v, self._last_i, self._last_temp)
        self._apply_zoom()

    def _all_plots(self):
        """Return all PlotWidget/PlotItem x-axes currently visible."""
        plots = []
        idx = self._stack.currentIndex()
        if idx == 0:
            plots.append(self._combined.p)
        elif idx == 1:
            plots.append(self._split2._vi.getPlotItem())
            plots.append(self._split2._tp.getPlotItem())
        else:
            for i in range(self._split3.layout().count()):
                w = self._split3.layout().itemAt(i).widget()
                if isinstance(w, pg.PlotWidget):
                    plots.append(w.getPlotItem())
        return plots

    def _apply_zoom(self):
        if not self._last_t:
            return
        plots = self._all_plots()
        if self._zoom_active and len(self._last_t) >= 2:
            t_end = self._last_t[-1]
            t_start = max(self._last_t[0], t_end - self._ZOOM_WINDOW)
            for p in plots:
                p.setXRange(t_start, t_end, padding=0.02)
        else:
            for p in plots:
                p.enableAutoRange(axis='x')

    def _on_zoom_btn(self, checked: bool):
        self._zoom_active = checked
        self._apply_zoom()

    def update(self, t, v, i, temp):
        self._last_t = t
        self._last_v, self._last_i, self._last_temp = v, i, temp
        idx = self._stack.currentIndex()
        [self._combined, self._split2, self._split3][idx].update(t, v, i, temp)
        if self._zoom_active:
            self._apply_zoom()


class _PdfNotifier(QObject):
    finished = Signal(bool, str)


class _PdfTask(QRunnable):
    def __init__(self, notifier: _PdfNotifier, path: str, config, estimator, analysis, csv_path: str):
        super().__init__()
        self.notifier = notifier
        self.path = path
        self.config = config
        self.estimator = estimator
        self.analysis = analysis
        self.csv_path = csv_path

    def run(self):
        try:
            from aset_batt.storage.report_generator import generate_pdf_report

            generate_pdf_report(
                self.path,
                self.config,
                self.estimator,
                analysis=self.analysis,
                csv_path=self.csv_path,
            )
            self.notifier.finished.emit(True, self.path)
        except Exception as exc:
            logger.exception("PDF generation failed")
            self.notifier.finished.emit(False, str(exc))
