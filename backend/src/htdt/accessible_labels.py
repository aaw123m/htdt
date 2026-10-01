"""Accessible-name wiring for caption-style labels.

Inputs named via ``QFormLayout.addRow`` resolve their accessible name
through the row label automatically. Inputs named by a caption ``QLabel``
placed immediately before them do NOT — the label is a sibling, not a
buddy, so the control's accessible name stays empty and screen readers
announce only the role. ``wire_label_buddies`` promotes each such orphan
caption into the following control's buddy at mount time, which fills the
accessible name and gives the label a focus proxy.
"""

from __future__ import annotations

from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QScrollArea,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QToolBox,
    QWidget,
)

# Controls that accept a caption label as their accessible name. Buttons
# are deliberately excluded: they already name themselves via their text.
_LABELABLE = (
    QComboBox,
    QLineEdit,
    QAbstractSpinBox,
    QAbstractItemView,
    QTabWidget,
    QAbstractSlider,
    QTextEdit,
    QPlainTextEdit,
)

# A caption's reach ends at self-named or self-describing widgets: bare
# layout containers (plain QWidget/QFrame subclasses) are transparent and
# the caption applies to the first control inside.
_NON_CONTAINER = (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QAbstractSlider,
    QTabWidget,
    QTextEdit,
    QGroupBox,
    QToolBox,
)


def resolved_accessible_name(widget: QWidget) -> str:
    """The name assistive technology resolves for ``widget`` ('' if none)."""

    interface = QAccessible.queryAccessibleInterface(widget)
    if interface is None or not interface.isValid():
        return ''
    return interface.text(QAccessible.Text.Name)


def _needs_name(widget: QWidget) -> bool:
    return isinstance(widget, _LABELABLE) and not resolved_accessible_name(widget)


def _is_plain_container(widget: QWidget) -> bool:
    return isinstance(widget, (QWidget, QFrame)) and not isinstance(
        widget, _NON_CONTAINER
    )


def _first_unnamed_labelable(layout, depth: int = 0):
    """First control inside ``layout`` the caption could name.

    Only the first real widget on each path counts — scanning past a
    button or a labelled control would pair the caption with an unrelated
    distant control.
    """
    if layout is None or depth > 4:
        return None
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None:
            if _needs_name(widget):
                return widget
            if isinstance(widget, _LABELABLE) or isinstance(
                widget, _NON_CONTAINER
            ):
                return None
            if _is_plain_container(widget):
                return _first_unnamed_labelable(widget.layout(), depth + 1)
            return None
        nested = item.layout()
        if nested is not None:
            found = _first_unnamed_labelable(nested, depth + 1)
            if found is not None:
                return found
    return None


def _owning_layout(widget: QWidget):
    """Innermost layout of the parent widget that holds ``widget`` directly.

    Caption rows are frequently nested (``outer.addLayout(row)``): the
    label's parent widget owns only the outer layout, so a flat sibling
    scan never finds the label. Walking the layout tree finds the row.
    """
    parent = widget.parentWidget()
    if parent is None:
        return None
    outer = parent.layout()
    if outer is None:
        return None
    stack = [outer]
    while stack:
        layout = stack.pop()
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return layout
            nested = item.layout()
            if nested is not None:
                stack.append(nested)
    return None


def _following_unnamed_control(label: QLabel) -> QWidget | None:
    """The control a caption label is written for, or None.

    The caption convention is label-then-control inside one layout. The
    scan stops at the first real widget after the label — a section
    header followed by a button row or another label is not a caption.
    """
    layout = _owning_layout(label)
    if layout is None:
        return None
    index = -1
    for i in range(layout.count()):
        if layout.itemAt(i).widget() is label:
            index = i
            break
    if index < 0:
        return None
    for i in range(index + 1, layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None:
            if _needs_name(widget):
                return widget
            if _is_plain_container(widget):
                return _first_unnamed_labelable(widget.layout())
            return None
        nested = item.layout()
        if nested is not None:
            # A nested row is transparent to the caption: take its first
            # unnamed control if it has one, otherwise keep scanning.
            found = _first_unnamed_labelable(nested)
            if found is not None:
                return found
    return None


def _is_caption_text(text: str) -> bool:
    """Real words only — separators like '〜' or '–' are not names."""

    return any(char.isalnum() for char in text)


def _retarget_container_buddy(label: QLabel) -> bool:
    """Name the first control inside a container's form-row label.

    ``QFormLayout.addRow('…', container)`` auto-buddies the row label to
    the container widget, which never takes focus — the name is lost
    there. A buddy only yields an accessible name for same-parent
    widgets, so the inner control needs an explicit name; the re-pointed
    buddy still gives the label a real focus target.
    """

    buddy = label.buddy()
    if buddy is None or not _is_plain_container(buddy):
        return False
    inner = _first_unnamed_labelable(buddy.layout())
    if inner is None:
        return False
    _apply_caption(label, inner)
    return True


def _apply_caption(label: QLabel, target: QWidget) -> None:
    """Give ``target`` the caption's name.

    A buddy only yields an accessible name when label and widget share a
    parent — a caption that reaches into a nested row or container must
    write the name directly; the buddy is still set for focus proxying.
    """

    label.setBuddy(target)
    if target.parentWidget() is not label.parentWidget() and not (
        resolved_accessible_name(target)
    ):
        target.setAccessibleName(label.text().strip())


def wire_label_buddies(root: QWidget) -> int:
    """Buddy every orphan caption in ``root``; returns the count wired."""

    wired = 0
    for label in root.findChildren(QLabel):
        if not _is_caption_text(label.text().strip()):
            continue
        if label.buddy() is not None:
            wired += _retarget_container_buddy(label)
            continue
        target = _following_unnamed_control(label)
        if target is not None:
            _apply_caption(label, target)
            wired += 1
    return wired


def announce_status(
    target: QWidget,
    message: str,
    *,
    assertive: bool = False,
) -> None:
    """Announce ``message`` to assistive technology as a live-region update.

    Status text rendered in a QLabel or QStatusBar is invisible to screen
    readers until it is announced through an accessible event; call this
    wherever the visible text updates. ``assertive`` interrupts the
    reader's current speech — keep the default polite mode for routine
    progress and status updates.
    """

    if not message:
        return
    event = QAccessibleAnnouncementEvent(target, message)
    event.setPoliteness(
        QAccessible.AnnouncementPoliteness.Assertive
        if assertive
        else QAccessible.AnnouncementPoliteness.Polite
    )
    QAccessible.updateAccessibility(event)


def wire_status_announcements(window: QMainWindow) -> QStatusBar:
    """Announce every status-bar message as a polite live-region update.

    QStatusBar text changes are not announced on their own; routing
    ``messageChanged`` through an announcement event makes every existing
    showMessage() call site audible without touching each one.
    """

    bar = window.statusBar()
    bar.messageChanged.connect(lambda text: announce_status(bar, text))
    return bar
