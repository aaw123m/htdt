"""Application-wide guard against accidental wheel edits.

Scrolling a page must never silently change a field value. Qt delivers
wheel events to the widget under the cursor, so a spin box / combo box /
slider that happens to be focused edits its value while the user only
meant to scroll the page — a classic source of unnoticed changes.

Installing :func:`install_wheel_scroll_guard` redirects wheel events on
those controls to the nearest scrollable ancestor, so the wheel always
scrolls the page. Fields are still editable via typing, arrow keys, and
the spin buttons. Controls outside any scroll area keep Qt's default
wheel behavior (wheel-adjust works there because nothing else scrolls).

Dialogs (REV34-DIALOGUX decision — kept, not extended)
-----------------------------------------------------
The filter is installed on the QApplication, so it already sees wheel
events inside dialogs and wizards — there is no dialog carve-out in the
code. The visible outcome differs by structure, and that difference is
deliberate:

* A control inside a scroll area hosted in a dialog forwards its wheel
  events to that viewport exactly like a page — verified empirically.
  Scrollable dialog content gets the same protection for free.
* A control in a fixed (non-scrolling) dialog keeps Qt's wheel-adjust.
  The hazard this guard exists for is scroll *intent* being misread as
  an edit; a modal form has no page to scroll, so a wheel event over a
  control is an unambiguous deliberate adjustment — and dense numeric
  dialogs (display specs, seating layouts) benefit from keeping it.

"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollBar,
    QWidget,
)

_WHEEL_EDITING_WIDGETS = (QAbstractSpinBox, QComboBox, QAbstractSlider)


class _WheelScrollGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel:
            return False
        # QScrollBar is an QAbstractSlider too — guarding it would loop the
        # forwarded wheel event back through this filter forever. Scroll
        # bars are exactly what wheel events should reach.
        if not isinstance(watched, _WHEEL_EDITING_WIDGETS) or isinstance(watched, QScrollBar):
            return False
        scroll_viewport = _scroll_viewport_for(watched)
        if scroll_viewport is None:
            return False
        # Forwarding a wheel event is the standard pattern here: the
        # scroll area uses the event's delta, not its position.
        QCoreApplication.sendEvent(scroll_viewport, event)
        return True


def _scroll_viewport_for(widget: QWidget) -> QWidget | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent.viewport()
        parent = parent.parentWidget()
    return None


def install_wheel_scroll_guard(app: QApplication) -> QObject:
    guard = _WheelScrollGuard(app)
    app.installEventFilter(guard)
    return guard
