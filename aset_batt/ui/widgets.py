"""
Self-contained UI building blocks for the ISA-101 GUI: readouts, trend plots,
the Qt root shim, and the background PDF task. None of these touch
BatteryQtWindow state — they are extracted from isa101_views.py so the main
window file stays focused on layout and behavior.

The app-wide qt-material stylesheet (applied once in aset_batt/app/run.py)
owns the base look of standard widgets (QPushButton shape/padding/hover
ripple, QComboBox, etc.); this module only layers semantic accent colors
(OK/WARN/CRIT/INFO) on top where needed. Colors are read from aset_batt.ui.
theme live (via theme.style()/theme.PANEL2/etc., not baked in at import
time), so retheme() can update everything built here without recreating it.
"""

import bisect
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

from aset_batt.ui import theme

logger = logging.getLogger(__name__)

pg.setConfigOptions(antialias=True)

_THEME_ATTR_NAMES = {"BG", "PANEL", "PANEL2", "FIELD", "BORDER", "TEXT", "MUTED",
                      "OK", "WARN", "CRIT", "INFO", "NEUTRAL"}


def _resolve_color(value):
    """bg/fg/hover may be a theme.* attribute name (e.g. "OK") to keep tracking
    that semantic color across retheme(), or a literal CSS color string."""
    if isinstance(value, str) and value in _THEME_ATTR_NAMES:
        return getattr(theme, value)
    return value


def _btn(text, bg=None, fg=None, hover=None):
    """QPushButton styled by the app-wide qt-material stylesheet; bg
    optionally layers a semantic accent color on top (pass a theme.* name
    like "OK"/"CRIT"/"INFO"/"WARN" to track that color across retheme(), or a
    literal hex string for a one-off, non-themed color). fg defaults to an
    auto-computed contrast color from the resolved bg (see
    theme.contrast_text()) — pass it explicitly only to override that choice;
    leaving it out is what you want for a neutral/surface bg like "PANEL2"
    that's light in one theme and dark in the other."""
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if bg is None:
        return b

    def _style():
        bg_resolved = _resolve_color(bg)
        fg_resolved = _resolve_color(fg) if fg is not None else theme.contrast_text(bg_resolved)
        return (f"QPushButton {{ background:{bg_resolved}; color:{fg_resolved}; }}"
                f"QPushButton:hover {{ background:{_resolve_color(hover or bg)}; }}")

    theme.style(b, _style)
    return b


def _hline():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    theme.style(line, lambda: f"color:{theme.BORDER}; background:{theme.BORDER}; max-height:1px;")
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


class MultiAxisTrend(pg.GraphicsLayoutWidget):
    """Voltage (left) + Current (right) + Temperature (far right) over time."""

    def __init__(self):
        super().__init__()
        self.setBackground(theme.PANEL2)
        self.p = self.addPlot()
        self.p.setLabel("bottom", "Elapsed", units="s")
        self.p.setLabel("left", "Voltage", units="V", color=theme.INFO)
        self.p.showGrid(x=True, y=True, alpha=0.2)
        self.p.getAxis("left").setPen(theme.INFO)
        # Lock to a plain-seconds display — with no data yet (idle) pyqtgraph's
        # SI-prefix auto-scaling can pick ms/µs based on a near-zero default range,
        # which looks like a bug (and did not match the sibling Temp plot's own
        # independently-scaled axis) rather than an empty-state placeholder.
        self.p.getAxis("bottom").enableAutoSIPrefix(False)

        self.vb_i = pg.ViewBox()
        self.p.showAxis("right")
        self.p.scene().addItem(self.vb_i)
        self.p.getAxis("right").linkToView(self.vb_i)
        self.p.getAxis("right").setLabel("Current", units="A", color=theme.WARN)
        self.p.getAxis("right").setPen(theme.WARN)
        self.vb_i.setXLink(self.p)

        self.ax_t = pg.AxisItem("right")
        self.p.layout.addItem(self.ax_t, 2, 3)
        self.vb_t = pg.ViewBox()
        self.p.scene().addItem(self.vb_t)
        self.ax_t.linkToView(self.vb_t)
        self.ax_t.setLabel("Temp", units="°C", color=theme.CRIT)
        self.ax_t.setPen(theme.CRIT)
        self.vb_t.setXLink(self.p)

        self.c_v = self.p.plot(pen=pg.mkPen(theme.INFO, width=2))
        self.c_i = pg.PlotCurveItem(pen=pg.mkPen(theme.WARN, width=2))
        self.c_t = pg.PlotCurveItem(pen=pg.mkPen(theme.CRIT, width=2, style=Qt.PenStyle.DashLine))
        self.vb_i.addItem(self.c_i)
        self.vb_t.addItem(self.c_t)

        self.p.vb.sigResized.connect(self._sync)

    def _sync(self):
        self.vb_i.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_t.setGeometry(self.p.vb.sceneBoundingRect())
        self.vb_i.linkedViewChanged(self.p.vb, self.vb_i.XAxis)
        self.vb_t.linkedViewChanged(self.p.vb, self.vb_t.XAxis)

    def set_default_ranges(self, v_max, i_max, t_max, x_max=60):
        """Sensible idle-state view before any real data exists — otherwise
        pyqtgraph auto-ranges an empty curve to an arbitrary small window (e.g.
        0-1.2 for a pack that reads 12.4V), which looks broken rather than empty."""
        self.p.setXRange(0, x_max, padding=0.02)
        self.p.setYRange(0, v_max, padding=0.05)
        self.vb_i.setYRange(-i_max, i_max, padding=0.05)
        self.vb_t.setYRange(0, t_max, padding=0.05)

    def retheme(self):
        self.setBackground(theme.PANEL2)
        self.p.setLabel("left", "Voltage", units="V", color=theme.INFO)
        self.p.getAxis("left").setPen(theme.INFO)
        self.p.getAxis("right").setLabel("Current", units="A", color=theme.WARN)
        self.p.getAxis("right").setPen(theme.WARN)
        self.ax_t.setLabel("Temp", units="°C", color=theme.CRIT)
        self.ax_t.setPen(theme.CRIT)
        self.c_v.setPen(pg.mkPen(theme.INFO, width=2))
        self.c_i.setPen(pg.mkPen(theme.WARN, width=2))
        self.c_t.setPen(pg.mkPen(theme.CRIT, width=2, style=Qt.PenStyle.DashLine))

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
        self._vi.setBackground(theme.PANEL2)
        self._vi.setLabel("bottom", "Elapsed", units="s")
        self._vi.setLabel("left", "Voltage", units="V", color=theme.INFO)
        self._vi.showGrid(x=True, y=True, alpha=0.2)
        self._vi.getAxis("left").setPen(theme.INFO)
        self._vi.getAxis("bottom").enableAutoSIPrefix(False)
        self._vi.showAxis("right")
        self._vb_i = pg.ViewBox()
        self._vi.scene().addItem(self._vb_i)
        self._vi.getAxis("right").linkToView(self._vb_i)
        self._vi.getAxis("right").setLabel("Current", units="A", color=theme.WARN)
        self._vi.getAxis("right").setPen(theme.WARN)
        self._vb_i.setXLink(self._vi.getPlotItem())
        self._c_v = self._vi.plot(pen=pg.mkPen(theme.INFO, width=2))
        self._c_i = pg.PlotCurveItem(pen=pg.mkPen(theme.WARN, width=2))
        self._vb_i.addItem(self._c_i)
        self._vi.getPlotItem().vb.sigResized.connect(self._sync_vi)

        self._tp = pg.PlotWidget()
        self._tp.setBackground(theme.PANEL2)
        self._tp.setLabel("bottom", "Elapsed", units="s")
        self._tp.setLabel("left", "Temp", units="°C", color=theme.CRIT)
        self._tp.showGrid(x=True, y=True, alpha=0.2)
        self._tp.getAxis("left").setPen(theme.CRIT)
        self._tp.getAxis("bottom").enableAutoSIPrefix(False)
        self._c_t = self._tp.plot(pen=pg.mkPen(theme.CRIT, width=2, style=Qt.PenStyle.DashLine))

        lay.addWidget(self._vi, 3)
        lay.addWidget(self._tp, 1)

    def _sync_vi(self):
        self._vb_i.setGeometry(self._vi.getPlotItem().vb.sceneBoundingRect())
        self._vb_i.linkedViewChanged(self._vi.getPlotItem().vb, self._vb_i.XAxis)

    def set_default_ranges(self, v_max, i_max, t_max, x_max=60):
        self._vi.setXRange(0, x_max, padding=0.02)
        self._vi.setYRange(0, v_max, padding=0.05)
        self._vb_i.setYRange(-i_max, i_max, padding=0.05)
        self._tp.setXRange(0, x_max, padding=0.02)
        self._tp.setYRange(0, t_max, padding=0.05)

    def retheme(self):
        self._vi.setBackground(theme.PANEL2)
        self._vi.setLabel("left", "Voltage", units="V", color=theme.INFO)
        self._vi.getAxis("left").setPen(theme.INFO)
        self._vi.getAxis("right").setLabel("Current", units="A", color=theme.WARN)
        self._vi.getAxis("right").setPen(theme.WARN)
        self._c_v.setPen(pg.mkPen(theme.INFO, width=2))
        self._c_i.setPen(pg.mkPen(theme.WARN, width=2))
        self._tp.setBackground(theme.PANEL2)
        self._tp.setLabel("left", "Temp", units="°C", color=theme.CRIT)
        self._tp.getAxis("left").setPen(theme.CRIT)
        self._c_t.setPen(pg.mkPen(theme.CRIT, width=2, style=Qt.PenStyle.DashLine))

    def update(self, t, v, i, temp):
        self._c_v.setData(t, v)
        self._c_i.setData(t, i)
        self._c_t.setData(t, temp)


def _triple_specs():
    return [
        ("Voltage", "V", theme.INFO, Qt.PenStyle.SolidLine),
        ("Current", "A", theme.WARN, Qt.PenStyle.SolidLine),
        ("Temp",    "°C", theme.CRIT, Qt.PenStyle.DashLine),
    ]


class TripleTrend(QWidget):
    """Voltage / Current / Temperature — 3 fully independent plots."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._curves = []
        self._plots = []
        for label, unit, color, style in _triple_specs():
            pw = pg.PlotWidget()
            pw.setBackground(theme.PANEL2)
            pw.setLabel("bottom", "Elapsed", units="s")
            pw.setLabel("left", label, units=unit, color=color)
            pw.showGrid(x=True, y=True, alpha=0.2)
            pw.getAxis("left").setPen(color)
            pw.getAxis("bottom").enableAutoSIPrefix(False)
            curve = pw.plot(pen=pg.mkPen(color, width=2, style=style))
            self._curves.append(curve)
            self._plots.append(pw)
            lay.addWidget(pw, 1)

    def set_default_ranges(self, v_max, i_max, t_max, x_max=60):
        for pw, (lo, hi) in zip(self._plots, [(0, v_max), (-i_max, i_max), (0, t_max)]):
            pw.setXRange(0, x_max, padding=0.02)
            pw.setYRange(lo, hi, padding=0.05)

    def retheme(self):
        for pw, curve, (label, unit, color, style) in zip(self._plots, self._curves, _triple_specs()):
            pw.setBackground(theme.PANEL2)
            pw.setLabel("left", label, units=unit, color=color)
            pw.getAxis("left").setPen(color)
            curve.setPen(pg.mkPen(color, width=2, style=style))

    def update(self, t, v, i, temp):
        for curve, data in zip(self._curves, [v, i, temp]):
            curve.setData(t, data)


class TrendCrosshair:
    """Shared crosshair for a TrendContainer: a vertical dashed line synced
    across every subplot of the currently-visible graph mode, plus one HTML
    tooltip (t/V/I/Temp sampled at the nearest cached point) anchored to
    whichever subplot the mouse is over. Re-wired on every mode switch since
    Combined/Split2/Split3 expose a different set of PlotItems."""

    def __init__(self, container: "TrendContainer"):
        self._container = container
        self._lines = {}    # PlotItem -> InfiniteLine
        self._labels = {}   # PlotItem -> TextItem
        self._wired = set()  # PlotItems whose sigMouseMoved is already connected
        self._active_plots = []
        self._last_idx = None
        self._last_origin = None
        self.rewire()

    def _line_pen(self):
        return pg.mkPen(theme.MUTED, width=1, style=Qt.PenStyle.DashLine)

    def rewire(self):
        """Call after the visible graph mode changes — attaches a line+label
        to every currently-visible PlotItem (idempotent: skips ones already
        wired, so switching back to a previously-seen mode doesn't double up)."""
        self._active_plots = self._container._all_plots()
        for p in self._active_plots:
            if p in self._lines:
                continue
            line = pg.InfiniteLine(angle=90, pen=self._line_pen(), movable=False)
            line.setVisible(False)
            p.addItem(line, ignoreBounds=True)
            self._lines[p] = line

            label = pg.TextItem(html="", anchor=(0, 1))
            label.setVisible(False)
            label.setZValue(100)
            p.addItem(label, ignoreBounds=True)
            self._labels[p] = label

            p.scene().sigMouseMoved.connect(self._make_handler(p))
            self._wired.add(p)
        self._hide_all()

    def retheme(self):
        pen = self._line_pen()
        for line in self._lines.values():
            line.setPen(pen)

    def _make_handler(self, origin_plot):
        def handler(scene_pos):
            self._on_mouse_moved(origin_plot, scene_pos)
        return handler

    def _hide_all(self):
        for line in self._lines.values():
            line.setVisible(False)
        for label in self._labels.values():
            label.setVisible(False)
        self._last_idx = None
        self._last_origin = None

    def _on_mouse_moved(self, origin_plot, scene_pos):
        # Signals from plots that belong to a no-longer-visible mode can still
        # arrive briefly (queued before the mode switch) — ignore them.
        if origin_plot not in self._active_plots or origin_plot not in self._lines:
            return
        vb = origin_plot.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            self._hide_all()
            return

        t = self._container._last_t
        if not t:
            self._hide_all()
            return

        x = vb.mapSceneToView(scene_pos).x()
        idx = bisect.bisect_left(t, x)
        if idx >= len(t):
            idx = len(t) - 1
        elif idx > 0 and (x - t[idx - 1]) < (t[idx] - x):
            idx -= 1
        x_snap = t[idx]

        for p in self._active_plots:
            line = self._lines[p]
            line.setPos(x_snap)
            line.setVisible(True)

        # Rebuilding the HTML tooltip re-triggers Qt's rich-text layout, which is
        # too expensive to redo on every single mouse-move event (many per second
        # while the pointer is moving) — only do it when the snapped sample
        # actually changed, so tiny sub-pixel jitter is nearly free.
        if idx == self._last_idx and origin_plot is self._last_origin:
            return
        self._last_idx = idx
        self._last_origin = origin_plot

        v = self._container._last_v[idx]
        i = self._container._last_i[idx]
        temp = self._container._last_temp[idx]
        html = (
            f'<div style="background:{theme.PANEL2}; border:1px solid {theme.BORDER}; '
            f'padding:4px 6px; font-size:10px; white-space:nowrap;">'
            f'<div style="color:{theme.MUTED};">t = {x_snap:.1f} s</div>'
            f'<div style="color:{theme.INFO};">&#9679; V: {v:.3f} V</div>'
            f'<div style="color:{theme.WARN};">&#9679; I: {i:.3f} A</div>'
            f'<div style="color:{theme.CRIT};">&#9679; Temp: {temp:.1f} &#176;C</div>'
            f'</div>'
        )
        for p, label in self._labels.items():
            label.setVisible(p is origin_plot)
        label = self._labels[origin_plot]
        label.setHtml(html)

        (x0, x1), (_, y1) = vb.viewRange()
        near_right = (x1 - x_snap) < (x1 - x0) * 0.25
        # anchor y=0 (top of the box at pos) so it hangs down from the top of
        # the visible range into the plot, instead of extending upward off-screen.
        label.setAnchor((1, 0) if near_right else (0, 0))
        label.setPos(x_snap, y1)


class TrendContainer(QWidget):
    """Wraps the 3 trend modes with a toggle bar. Press A to toggle 10s zoom."""

    MODES = ["Combined", "Split 2", "Split 3"]
    _ZOOM_WINDOW = 10  # seconds

    def __init__(self):
        super().__init__()
        self._zoom_active = False
        self._y_zoom_active = False
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
        theme.style(self._zoom_btn, self._zoom_btn_style)
        self._zoom_btn.clicked.connect(self._on_zoom_btn)
        bar.addWidget(self._zoom_btn)

        self._y_zoom_btn = QPushButton("Y-Auto")
        self._y_zoom_btn.setCheckable(True)
        self._y_zoom_btn.setFixedHeight(22)
        self._y_zoom_btn.setToolTip("Toggle Y-Axis Auto-Scale")
        theme.style(self._y_zoom_btn, self._zoom_btn_style)
        self._y_zoom_btn.clicked.connect(self._on_y_zoom_btn)
        bar.addWidget(self._y_zoom_btn)

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

        self._crosshair = TrendCrosshair(self)
        theme.on_retheme(self.retheme)

    @staticmethod
    def _zoom_btn_style():
        return (
            f"QPushButton{{background:{theme.PANEL2};color:{theme.MUTED};border:1px solid {theme.MUTED};border-radius:3px;font-weight:bold;}}"
            f"QPushButton:checked{{background:{theme.INFO};color:#000;border:1px solid {theme.INFO};}}"
            f"QPushButton:hover{{border-color:#aaa;}}"
        )

    def set_default_ranges(self, v_max, i_max, t_max, x_max=60):
        """Sensible idle-state view for all 3 modes (not just the currently-visible
        one) so switching modes before any real data exists never lands on an
        auto-ranged-on-nothing, seemingly-broken graph."""
        self._default_ranges = (v_max, i_max, t_max, x_max)
        self._combined.set_default_ranges(v_max, i_max, t_max, x_max)
        self._split2.set_default_ranges(v_max, i_max, t_max, x_max)
        self._split3.set_default_ranges(v_max, i_max, t_max, x_max)

    def retheme(self):
        self._combined.retheme()
        self._split2.retheme()
        self._split3.retheme()
        self._crosshair.retheme()

    def _on_mode_changed(self, idx: int):
        self._stack.setCurrentIndex(idx)
        # Back-fill the newly-visible trend from cache so it isn't blank (it was
        # never fed while hidden, and after a test ends no further ticks arrive).
        if self._last_t:
            [self._combined, self._split2, self._split3][idx].update(
                self._last_t, self._last_v, self._last_i, self._last_temp)
        self._apply_zoom()
        self._crosshair.rewire()

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
        
        # X-Axis
        if self._zoom_active and len(self._last_t) >= 2:
            t_end = self._last_t[-1]
            t_start = max(self._last_t[0], t_end - self._ZOOM_WINDOW)
            for p in plots:
                p.setXRange(t_start, t_end, padding=0.02)
        else:
            for p in plots:
                p.enableAutoRange(axis='x')

        # Y-Axis
        if self._y_zoom_active:
            for p in plots:
                p.enableAutoRange(axis='y')
            # For ViewBoxes not directly in plots list (like vb_i, vb_t)
            self._combined.vb_i.enableAutoRange(axis='y')
            self._combined.vb_t.enableAutoRange(axis='y')
            self._split2._vb_i.enableAutoRange(axis='y')
        else:
            for p in plots:
                p.disableAutoRange(axis='y')
            self._combined.vb_i.disableAutoRange(axis='y')
            self._combined.vb_t.disableAutoRange(axis='y')
            self._split2._vb_i.disableAutoRange(axis='y')
            if hasattr(self, '_default_ranges'):
                v_max, i_max, t_max, x_max = self._default_ranges
                self.set_default_ranges(v_max, i_max, t_max, x_max=self._last_t[-1] if not self._zoom_active else x_max)

    def _on_zoom_btn(self, checked: bool):
        self._zoom_active = checked
        self._apply_zoom()

    def _on_y_zoom_btn(self, checked: bool):
        self._y_zoom_active = checked
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
