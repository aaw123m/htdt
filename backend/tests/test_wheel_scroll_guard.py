"""Tests for the app-wide wheel-scroll guard.

Scrolling a page must never silently edit a value-editing control that
happens to be under the cursor — the wheel event is forwarded to the
nearest scrollable ancestor instead.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import htdt.data_relocation  # noqa: F401  (must precede PySide6 import)

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from htdt.wheel_scroll_guard import install_wheel_scroll_guard


@pytest.fixture()
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wheel_event(target: QWidget, angle_delta: int = -240) -> QWheelEvent:
    center = target.rect().center()
    position = QPointF(target.mapToGlobal(center))
    return QWheelEvent(
        position,
        position,
        QPoint(0, 0),
        QPoint(0, angle_delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def _scroll_area_with(child: QWidget) -> QScrollArea:
    area = QScrollArea()
    content = QWidget()
    layout = QFormLayout(content)
    layout.addRow(child)
    content.setMinimumHeight(2000)
    area.setWidget(content)
    area.resize(320, 240)
    area.show()
    return area


def test_wheel_over_spinbox_inside_scroll_area_scrolls_page(app) -> None:
    install_wheel_scroll_guard(app)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    spin.setValue(50.0)
    area = _scroll_area_with(spin)
    app.processEvents()

    scrollbar = area.verticalScrollBar()
    assert scrollbar.maximum() > 0
    before_scroll = scrollbar.value()

    QApplication.sendEvent(spin, _wheel_event(spin))

    assert spin.value() == pytest.approx(50.0)
    assert scrollbar.value() > before_scroll
    area.deleteLater()


def test_wheel_over_combobox_inside_scroll_area_does_not_change_index(app) -> None:
    install_wheel_scroll_guard(app)
    combo = QComboBox()
    combo.addItems([f"項目{n}" for n in range(5)])
    combo.setCurrentIndex(1)
    area = _scroll_area_with(combo)
    app.processEvents()

    QApplication.sendEvent(combo, _wheel_event(combo))

    assert combo.currentIndex() == 1
    area.deleteLater()


def test_wheel_over_spinbox_outside_scroll_area_keeps_default_behavior(app) -> None:
    install_wheel_scroll_guard(app)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    spin.setValue(50.0)
    spin.show()
    app.processEvents()

    QApplication.sendEvent(spin, _wheel_event(spin, angle_delta=120))

    assert spin.value() != pytest.approx(50.0)
    spin.deleteLater()


def test_keyboard_editing_still_works(app) -> None:
    install_wheel_scroll_guard(app)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    area = _scroll_area_with(spin)
    app.processEvents()

    spin.setValue(42.5)
    spin.stepUp()

    assert spin.value() == pytest.approx(43.5)
    area.deleteLater()


def test_wheel_over_spinbox_inside_dialog_scroll_area_scrolls_it(app) -> None:
    """Dialog scroll areas are guarded too — the filter is app-wide and
    has no dialog carve-out (REV34-DIALOGUX)."""
    install_wheel_scroll_guard(app)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    spin.setValue(50.0)
    area = _scroll_area_with(spin)
    layout.addWidget(area)
    dialog.resize(360, 300)
    dialog.show()
    app.processEvents()

    scrollbar = area.verticalScrollBar()
    assert scrollbar.maximum() > 0

    QApplication.sendEvent(spin, _wheel_event(spin))

    assert spin.value() == pytest.approx(50.0)
    assert scrollbar.value() > 0
    dialog.deleteLater()


def test_wheel_over_spinbox_in_fixed_dialog_keeps_wheel_adjust(app) -> None:
    """A control in a non-scrolling dialog has no page to scroll — wheel
    stays a deliberate adjustment (REV34-DIALOGUX decision)."""
    install_wheel_scroll_guard(app)
    dialog = QDialog()
    layout = QFormLayout(dialog)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    spin.setValue(50.0)
    layout.addRow(spin)
    dialog.show()
    app.processEvents()

    QApplication.sendEvent(spin, _wheel_event(spin, angle_delta=120))

    assert spin.value() != pytest.approx(50.0)
    dialog.deleteLater()
