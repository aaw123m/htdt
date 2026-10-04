"""Display-unit-aware length spin boxes — SI metres stay authoritative.

Shared home for the length-field classes originally built inside
``room_workspace`` for the inspector (#583). Room geometry, installation
and field-explorer surfaces reuse the same widgets so the
``display_input.length_unit`` / ``display_input.numeric_precision``
preferences (#496) apply consistently everywhere a length is edited.

Conversion semantics come from :mod:`htdt.cad_display_units` — these
classes add only Qt cosmetics (suffix text, step sizes, pending-text and
tooltip handling) on top of them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDoubleSpinBox, QWidget

from .cad_display_units import (
    DEFAULT_DISPLAY_DECIMALS,
    DISPLAY_LENGTH_UNITS,
    display_to_si,
    si_to_display,
)


class PendingTextSpinBox(QDoubleSpinBox):
    """SpinBox that keeps typed-but-uninterpreted text across hide/show.

    ``QAbstractSpinBox`` re-syncs the line editor to the current value on
    ``showEvent`` — any ancestor hide/show cycle silently drops in-flight
    input (#583). A line that differs from the canonical display text is a
    pending edit, so it is restored after the base re-sync runs.

    The tooltip also mirrors onto the embedded line edit: Qt does not
    propagate a spin box's tooltip to its internal QLineEdit — the cursor
    sits on the line edit, so field tooltips would never appear over the
    text area without this forward.
    """

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setToolTip(text)
        editor = self.lineEdit()
        if editor is not None:
            editor.setToolTip(text)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        editor = self.lineEdit()
        pending = editor.text() if editor is not None else ""
        # Canonical display text for the current (committed) value — a line
        # that differs from it is an uncommitted edit worth preserving.
        canonical = (
            self.prefix() + self.textFromValue(self.value()) + self.suffix()
        ).strip()
        super().showEvent(event)
        if editor is not None and pending and pending.strip() != canonical:
            editor.setText(pending)


class MetricSpinBox(PendingTextSpinBox):
    """Length field: SI metres stay authoritative; display unit is cosmetic.

    Step size scales with keyboard modifiers — plain = fine, Shift = ×10,
    Ctrl = ×0.1 — so arrow keys cover both rough and precise adjustment
    (#583). Wheel input is ignored unless the field has focus, which keeps
    page scrolling from silently editing values.
    """

    # Conversion semantics come from cad_display_units (#496) — this class
    # adds only Qt cosmetics (suffix text, step sizes) on top of them.
    UNIT_SCALES: dict[str, float] = {
        unit: si_to_display(1.0, unit) for unit in DISPLAY_LENGTH_UNITS
    }
    UNIT_SUFFIXES: dict[str, str] = {
        'm': ' m',
        'cm': ' cm',
        'mm': ' mm',
        'inch': ' in',
    }
    # Sensible per-unit base steps (display units).
    UNIT_STEPS: dict[str, float] = {
        'm': 0.001,
        'cm': 0.1,
        'mm': 1.0,
        'inch': 0.05,
    }
    UNIT_DECIMALS: dict[str, int] = dict(DEFAULT_DISPLAY_DECIMALS)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        minimum_m: float = -1000.0,
        maximum_m: float = 1000.0,
    ) -> None:
        super().__init__(parent)
        self._display_unit = 'm'
        self._minimum_m = minimum_m
        self._maximum_m = maximum_m
        self._base_step = self.UNIT_STEPS['m']
        # Exact SI authority for this field. The displayed value is quantized
        # to ``decimals`` in the display unit, so deriving SI back from
        # ``self.value()`` loses precision (1.5 m -> 59.06 in -> 1500.12 mm).
        # User commits (valueChanged) refresh the cache; programmatic writes
        # set it directly and suppress that sync.
        self._exact_m = 0.0
        self._exact_sync_blocked = False
        self.setKeyboardTracking(False)
        self.valueChanged.connect(self._sync_exact_from_display)
        self._apply_unit()

    def _sync_exact_from_display(self, display_value: float) -> None:
        if self._exact_sync_blocked:
            return
        self._exact_m = display_to_si(display_value, self._display_unit)

    def _apply_unit(self) -> None:
        scale = self.UNIT_SCALES[self._display_unit]
        self.setRange(self._minimum_m * scale, self._maximum_m * scale)
        self._base_step = self.UNIT_STEPS[self._display_unit]
        self.setSingleStep(self._base_step)
        self.setDecimals(self.UNIT_DECIMALS[self._display_unit])
        self.setSuffix(self.UNIT_SUFFIXES[self._display_unit])

    def set_display_unit(self, unit: str, *, decimals: int | None = None) -> None:
        if unit not in self.UNIT_SCALES:
            return
        if unit == self._display_unit and decimals is None:
            return
        value_m = self._exact_m
        self._exact_sync_blocked = True
        try:
            self._display_unit = unit
            self._apply_unit()
            if decimals is not None:
                self.setDecimals(decimals)
            self.setValue(si_to_display(value_m, unit))
        finally:
            self._exact_sync_blocked = False

    def display_unit(self) -> str:
        return self._display_unit

    def set_minimum_m(self, value: float) -> None:
        """Lower bound in SI metres, converted through the display unit."""

        self._minimum_m = float(value)
        self._apply_unit()

    def set_maximum_m(self, value: float) -> None:
        """Upper bound in SI metres, converted through the display unit."""

        self._maximum_m = float(value)
        self._apply_unit()

    def value_m(self) -> float:
        return self._exact_m

    def set_value_m(self, value: float) -> None:
        self._exact_sync_blocked = True
        try:
            self.setValue(si_to_display(value, self._display_unit))
        finally:
            self._exact_sync_blocked = False
        self._exact_m = float(value)

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt override
        modifiers = QGuiApplication.keyboardModifiers()
        factor = 1.0
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            factor = 10.0
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            factor = 0.1
        self.setSingleStep(self._base_step * factor)
        try:
            super().stepBy(steps)
        finally:
            self.setSingleStep(self._base_step)

    def wheelEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        # Ignore wheel changes while unfocused so scrolling the inspector page
        # never mutates a field the user happened to hover (#583 scroll-safety).
        if self.hasFocus():
            super().wheelEvent(event)  # type: ignore[arg-type]
        else:
            event.ignore()  # type: ignore[attr-defined]


__all__ = ['MetricSpinBox', 'PendingTextSpinBox']
