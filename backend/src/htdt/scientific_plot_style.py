"""Shared scientific-plot visual language for 2D engineering graphs (#579).

This module is presentation-only: it standardizes trace pens, axes, legends,
reference lines, cursors and empty/stale states so every plot in the app
speaks one grammar. It never computes smoothing, targets, or any other
numerical authority — it only styles what the caller hands it.

Trace grammar (meaning must never ride on color alone):
- measured   — solid, strongest evidence weight
- predicted  — dashed, same weight as measured
- derived    — dash-dot, secondary weight (smoothed overlays, differences)
- target     — fine dotted, low-saturation accent, always named
- baseline   — thin + dimmed, historical/reference context, never dominant

Channel/seat identity within one graph comes from a bounded categorical
palette (``channel_color``); line style continues to carry evidence class.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Iterable, Sequence

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen

from .ui_theme import DARK_THEME


class TraceSemantic(Enum):
    """Evidence/role classes a plotted trace can carry (#579 §1)."""

    MEASURED = "measured"
    PREDICTED = "predicted"
    DERIVED = "derived"
    TARGET = "target"
    BASELINE = "baseline"


_PEN_STYLE = {
    TraceSemantic.MEASURED: Qt.PenStyle.SolidLine,
    TraceSemantic.PREDICTED: Qt.PenStyle.DashLine,
    TraceSemantic.DERIVED: Qt.PenStyle.DashDotLine,
    TraceSemantic.TARGET: Qt.PenStyle.DotLine,
    TraceSemantic.BASELINE: Qt.PenStyle.SolidLine,
}

_PEN_WIDTH = {
    TraceSemantic.MEASURED: 2.0,
    TraceSemantic.PREDICTED: 2.0,
    TraceSemantic.DERIVED: 1.6,
    TraceSemantic.TARGET: 1.4,
    TraceSemantic.BASELINE: 1.0,
}

_PEN_COLOR_TOKEN = {
    TraceSemantic.MEASURED: lambda s: s.measured,
    TraceSemantic.PREDICTED: lambda s: s.predicted,
    TraceSemantic.DERIVED: lambda s: s.secondary_trace,
    TraceSemantic.TARGET: lambda s: s.target,
    TraceSemantic.BASELINE: lambda s: s.secondary_trace,
}

# Human-relevant log-frequency ticks; labels avoid decimal noise (#579 §3).
FREQUENCY_TICK_HZ: tuple[float, ...] = (
    20.0, 50.0, 100.0, 200.0, 500.0,
    1000.0, 2000.0, 5000.0, 10000.0, 20000.0,
)


def format_frequency_tick(value_hz: float) -> str:
    """Compact axis label: ``1000`` renders as ``1k``, ``200`` as ``200``."""

    if value_hz >= 1000.0:
        kilo = value_hz / 1000.0
        return f"{kilo:g}k"
    return f"{value_hz:g}"


def trace_pen(
    semantic: TraceSemantic,
    *,
    color: str | QColor | None = None,
    width: float | None = None,
    selected: bool = False,
    dimmed: bool = False,
) -> QPen:
    """Canonical pen for an evidence class.

    ``selected`` boosts width only — selection never changes evidence meaning.
    ``dimmed`` lowers opacity for unselected traces in multi-trace views.
    """

    tokens = DARK_THEME.scientific
    if color is None:
        color = _PEN_COLOR_TOKEN[semantic](tokens).hex
    elif isinstance(color, QColor):
        color = color.name()
    style = _PEN_STYLE[semantic]
    pen_width = width if width is not None else _PEN_WIDTH[semantic]
    if selected:
        pen_width += 0.8
    pen = pg.mkPen(color, width=pen_width, style=style)
    if dimmed:
        pen_color = pen.color()
        pen_color.setAlphaF(0.45)
        pen.setColor(pen_color)
    return pen


def channel_color(index: int) -> str:
    """Bounded categorical palette for channel/seat identity (#579 §2).

    Wraps rather than running out; identity is per-graph only — line style
    still carries the evidence class.
    """

    palette = DARK_THEME.scientific.channels
    return palette[index % len(palette)].hex


def apply_frequency_ticks(plot: pg.PlotWidget) -> None:
    """Standard major ticks at human-relevant frequencies on a log X axis.

    pyqtgraph's log mode maps data into log-domain view coordinates, so
    tick *positions* must be given as log10(Hz) — raw Hz positions land
    outside the view and the labels never render.
    """

    axis = plot.getAxis("bottom")
    ticks = [
        (math.log10(tick), format_frequency_tick(tick))
        for tick in FREQUENCY_TICK_HZ
    ]
    axis.setTicks([ticks, []])


def apply_scientific_appearance(
    plot: pg.PlotWidget,
    *,
    log_x: bool = True,
    frequency_ticks: bool = True,
) -> None:
    """Standard dark scientific canvas: grid, axes, downsampling, ticks."""

    tokens = DARK_THEME
    plot.setBackground(tokens.surfaces.canvas.hex)
    plot.showGrid(x=True, y=True, alpha=0.18)
    if log_x:
        plot.setLogMode(x=True, y=False)
    item = plot.getPlotItem()
    item.setContentsMargins(10, 8, 10, 10)
    item.setDownsampling(auto=True, mode="peak")
    item.setClipToView(True)
    item.getViewBox().setDefaultPadding(0.03)
    for axis_name in ("bottom", "left"):
        axis = plot.getAxis(axis_name)
        axis.setPen(tokens.surfaces.border_strong.hex)
        axis.setTextPen(tokens.text.secondary.hex)
        axis.setStyle(tickTextOffset=8, autoExpandTextSpace=True)
        # SI-prefix auto-scaling misreads log-domain coordinates (the unit
        # label degenerates to e.g. "MHz"/"PHz"); ticks carry the "1k"
        # formatting instead.
        axis.enableAutoSIPrefix(False)
    if log_x and frequency_ticks:
        apply_frequency_ticks(plot)


def add_scientific_legend(plot: pg.PlotWidget) -> pg.LegendItem:
    """Standard legend — top-right, small, non-obscuring (#579 §9)."""

    legend = plot.addLegend(offset=(8, 8))
    legend.setBrush(pg.mkBrush(QColor(DARK_THEME.surfaces.base.hex)))
    legend.setPen(pg.mkPen(QColor(DARK_THEME.surfaces.separator.hex)))
    return legend


def add_reference_line(
    plot: pg.PlotWidget,
    value: float,
    *,
    axis: str = "y",
    label: str | None = None,
    movable: bool = False,
) -> pg.InfiniteLine:
    """Thin neutral reference (zero lines, targets, tolerances — #579 §6).

    Thinner than data traces and neutral in color; label only when ambiguous.
    """

    angle = 0.0 if axis == "y" else 90.0
    line = pg.InfiniteLine(
        pos=value,
        angle=angle,
        movable=movable,
        pen=pg.mkPen(
            DARK_THEME.scientific.secondary_trace.hex,
            width=1,
            style=Qt.PenStyle.DashLine,
        ),
        label=label,
        labelOpts={
            "position": 0.95,
            "color": QColor(DARK_THEME.text.secondary.hex),
        }
        if label
        else None,
    )
    plot.addItem(line)
    return line


def link_x_axis(primary: pg.PlotWidget, linked: pg.PlotWidget) -> None:
    """Keep stacked plots on one shared X navigation (#579 §8)."""

    linked.setXLink(primary.getPlotItem())


def show_plot_state(
    plot: pg.PlotWidget,
    message: str,
    *,
    detail: str | None = None,
) -> pg.TextItem:
    """Centered in-plot state message — no-data/loading/stale/unsupported.

    A designed empty state replaces the bare dark canvas (#579 §13). The
    caller removes it (or it is cleared with ``plot.clear()``) once real
    traces are drawn.
    """

    text = message if detail is None else f"{message}\n{detail}"
    item = pg.TextItem(
        text,
        anchor=(0.5, 0.5),
        color=QColor(DARK_THEME.text.secondary.hex),
    )
    viewbox = plot.getPlotItem().getViewBox()
    viewbox.addItem(item, ignoreBounds=True)
    # Center on the current view rectangle (state shows when no real data
    # occupies meaningful coordinates anyway).
    center = viewbox.viewRect().center()
    item.setPos(center.x(), center.y())
    return item


def hide_plot_state(plot: pg.PlotWidget, item: pg.TextItem | None) -> None:
    if item is not None:
        try:
            plot.getPlotItem().getViewBox().removeItem(item)
        except Exception:  # error-boundary: render teardown — a stale overlay-item remove failure is benign; the plot re-renders cleanly on next draw (noqa: BLE001)
            pass


class PlotCursor:
    """Presentation-only vertical probe + readout (#579 §7).

    ``attach`` adds a movable cursor line; ``readout`` formats the exact
    value under inspection for each named trace — it never creates
    measurement authority.
    """

    def __init__(self, plot: pg.PlotWidget, *, unit_hz: str = "Hz") -> None:
        self.plot = plot
        self.unit = unit_hz
        self.line = pg.InfiniteLine(
            angle=90.0,
            movable=True,
            pen=pg.mkPen(
                DARK_THEME.scientific.cursor.hex,
                width=1,
                style=Qt.PenStyle.DashLine,
            ),
        )
        self.line.setVisible(False)
        plot.addItem(self.line, ignoreBounds=True)

    @property
    def active(self) -> bool:
        return self.line.isVisible()

    def set_active(self, active: bool) -> None:
        self.line.setVisible(active)
        if active and self.line.value() == 0.0:
            # Start at view center rather than 0.
            x_range = self.plot.getPlotItem().getViewBox().viewRange()[0]
            if x_range and x_range[1] > x_range[0]:
                self.line.setValue(0.5 * (x_range[0] + x_range[1]))

    def value(self) -> float:
        return float(self.line.value())

    def nearest_readout(
        self,
        traces: Iterable[tuple[str, Sequence[float], Sequence[float]]],
        *,
        value_unit: str = "dB",
    ) -> str:
        """``63.0 Hz / Measured FL 74.2 dB`` style multi-line readout."""

        x = self._display_value()
        lines = [f"{x:.1f} {self.unit}"]
        for name, xs, ys in traces:
            if not xs:
                continue
            index = min(range(len(xs)), key=lambda i: abs(xs[i] - x))
            lines.append(f"{name}    {ys[index]:.2f} {value_unit}")
        return "\n".join(lines)

    def _display_value(self) -> float:
        """Cursor position in display units.

        On a log-mode axis the line's coordinate is log10(Hz); convert back
        so the readout says the real frequency, not the log coordinate.
        """

        raw = float(self.line.value())
        log_mode = self.plot.getPlotItem().getViewBox().state.get("logMode")
        if log_mode and log_mode[0]:
            # A reference line placed outside the representable decade
            # range must not crash the readout — saturate the display.
            return 10.0 ** min(max(raw, -300.0), 300.0)
        return raw


__all__ = [
    "FREQUENCY_TICK_HZ",
    "PlotCursor",
    "TraceSemantic",
    "add_reference_line",
    "add_scientific_legend",
    "apply_frequency_ticks",
    "apply_scientific_appearance",
    "channel_color",
    "format_frequency_tick",
    "hide_plot_state",
    "link_x_axis",
    "show_plot_state",
    "trace_pen",
]
