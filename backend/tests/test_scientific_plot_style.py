"""Tests for the shared scientific-plot visual grammar (#579)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from htdt.scientific_plot_style import (
    FREQUENCY_TICK_HZ,
    PlotCursor,
    TraceSemantic,
    add_reference_line,
    add_scientific_legend,
    apply_scientific_appearance,
    channel_color,
    format_frequency_tick,
    link_x_axis,
    show_plot_state,
    trace_pen,
)
from htdt.ui_theme import DARK_THEME


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_trace_pen_style_grammar_is_distinct_per_semantic() -> None:
    styles = {
        semantic: trace_pen(semantic).style() for semantic in TraceSemantic
    }
    # Evidence class must never ride on color alone: solid/dash/dash-dot/dot
    # are all distinct, and baseline is thin-solid but at half the width.
    assert styles[TraceSemantic.MEASURED] == Qt.PenStyle.SolidLine
    assert styles[TraceSemantic.PREDICTED] == Qt.PenStyle.DashLine
    assert styles[TraceSemantic.DERIVED] == Qt.PenStyle.DashDotLine
    assert styles[TraceSemantic.TARGET] == Qt.PenStyle.DotLine
    assert styles[TraceSemantic.BASELINE] == Qt.PenStyle.SolidLine
    assert trace_pen(TraceSemantic.MEASURED).widthF() == pytest.approx(2.0)
    assert trace_pen(TraceSemantic.PREDICTED).widthF() == pytest.approx(2.0)
    assert (
        trace_pen(TraceSemantic.BASELINE).widthF()
        < trace_pen(TraceSemantic.MEASURED).widthF()
    )


def test_selected_trace_boosts_width_not_style() -> None:
    pen = trace_pen(TraceSemantic.MEASURED, selected=True)
    assert pen.style() == Qt.PenStyle.SolidLine
    assert pen.widthF() > trace_pen(TraceSemantic.MEASURED).widthF()


def test_dimmed_pen_lowers_opacity() -> None:
    dim = trace_pen(TraceSemantic.MEASURED, dimmed=True)
    assert dim.color().alphaF() < 1.0


def test_target_pen_uses_target_token() -> None:
    pen = trace_pen(TraceSemantic.TARGET)
    assert pen.color().name() == QColor(
        DARK_THEME.scientific.target.hex
    ).name()


def test_channel_color_bounded_and_wrapping() -> None:
    palette = DARK_THEME.scientific.channels
    first = channel_color(0)
    assert first == palette[0].hex
    assert channel_color(len(palette)) == first
    assert channel_color(len(palette) + 2) == palette[2].hex


def test_format_frequency_tick() -> None:
    assert format_frequency_tick(20.0) == "20"
    assert format_frequency_tick(500.0) == "500"
    assert format_frequency_tick(1000.0) == "1k"
    assert format_frequency_tick(2000.0) == "2k"
    assert format_frequency_tick(20000.0) == "20k"


def test_apply_scientific_appearance_sets_dark_log_canvas() -> None:
    _app()
    plot = pg.PlotWidget()
    apply_scientific_appearance(plot)
    assert plot.getPlotItem().getViewBox().state["logMode"] == [True, False]
    assert plot.backgroundBrush().color().name() == QColor(
        DARK_THEME.surfaces.canvas.hex
    ).name()
    # Standard major ticks are the human-relevant set, positioned in
    # log-domain coordinates (log mode maps data through log10).
    axis = plot.getAxis("bottom")
    assert axis is not None
    assert len(FREQUENCY_TICK_HZ) == 10
    # SI-prefix auto-scaling misreads log-domain coordinates — disabled.
    assert axis.autoSIPrefix is False
    plot.deleteLater()


def test_add_reference_line_is_horizontal_thin_and_neutral() -> None:
    _app()
    plot = pg.PlotWidget()
    line = add_reference_line(plot, 0.0)
    assert line.angle == 0.0
    assert line.value() == 0.0
    assert line.pen.widthF() <= 1.0
    assert line in plot.getPlotItem().items
    plot.deleteLater()


def test_link_x_axis_shares_navigation() -> None:
    _app()
    top = pg.PlotWidget()
    bottom = pg.PlotWidget()
    link_x_axis(top, bottom)
    assert (
        bottom.getPlotItem().getViewBox().linkedView(pg.ViewBox.XAxis)
        is top.getPlotItem().getViewBox()
    )
    top.deleteLater()
    bottom.deleteLater()


def test_show_plot_state_adds_centered_text_item() -> None:
    _app()
    plot = pg.PlotWidget()
    item = show_plot_state(plot, "測定がありません", detail="読み込んでください")
    viewbox = plot.getPlotItem().getViewBox()
    assert item in viewbox.childGroup.childItems() or item.scene() is not None
    assert "測定がありません" in item.textItem.toPlainText()
    plot.deleteLater()


def test_add_scientific_legend_returns_legend() -> None:
    _app()
    plot = pg.PlotWidget()
    legend = add_scientific_legend(plot)
    assert legend is not None
    plot.deleteLater()


def test_plot_cursor_readout_is_presentation_only() -> None:
    _app()
    plot = pg.PlotWidget()
    cursor = PlotCursor(plot)
    assert not cursor.active
    cursor.set_active(True)
    assert cursor.active
    cursor.line.setValue(63.0)
    readout = cursor.nearest_readout(
        [("A: Measured", [20.0, 63.0, 100.0], [70.0, 74.2, 76.0])]
    )
    assert "63.0 Hz" in readout
    assert "A: Measured" in readout
    assert "74.20" in readout or "74.2" in readout
    plot.deleteLater()


def test_plot_cursor_readout_converts_log_domain() -> None:
    _app()
    plot = pg.PlotWidget()
    plot.setLogMode(x=True, y=False)
    cursor = PlotCursor(plot)
    cursor.set_active(True)
    # On a log axis the line coordinate is log10(Hz) — the readout must
    # show the real frequency, not the log coordinate.
    cursor.line.setValue(2.7)  # ~501 Hz
    readout = cursor.nearest_readout(
        [("A", [20.0, 500.0, 1000.0], [70.0, 74.2, 76.0])]
    )
    assert "501" in readout or "500" in readout
    assert "2.7 Hz" not in readout
    assert "74.20" in readout or "74.2" in readout
    plot.deleteLater()
