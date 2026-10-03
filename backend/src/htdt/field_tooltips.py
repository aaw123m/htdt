"""Shared helper for dialog field tooltips.

Dialogs use :func:`apply_field_tooltip` instead of a bare ``setToolTip``
because two spots must show the same text:

- a spin box's embedded ``lineEdit()`` — Qt does not propagate the box's
  tooltip to the text area the cursor actually sits on;
- the ``QFormLayout`` row label — users hover the label at least as often
  as the field itself.

The pattern was established hand-rolled in ``MaterialDialog`` /
``ProjectorSpecDialog``; this module is the shared form so new dialogs do
not have to re-derive it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractSpinBox, QFormLayout, QWidget


def apply_field_tooltip(
    field: QWidget,
    tip: str,
    form: QFormLayout | None = None,
) -> None:
    """Set ``tip`` on the field, its embedded line edit, and its form label."""
    field.setToolTip(tip)
    if isinstance(field, QAbstractSpinBox):
        # Spin boxes do not forward their tooltip to the internal editor;
        # without this the text area shows nothing on hover.
        field.lineEdit().setToolTip(tip)
    if form is not None:
        label = form.labelForField(field)
        if label is not None:
            label.setToolTip(tip)
