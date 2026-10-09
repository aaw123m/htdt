"""Dynamic-UI accessibility support (#975, on behalf of #804).

The dynamic panels (#941 decision brief, #942 geometry intake, the
measurement quality table and the standards criterion tree) rebuild
item views and card layouts on every state sync. Three guarantees are
shared here so each surface binds the same contract:

* **Announcements fire once per meaningful change.** ``DynamicAnnouncer``
  carries ``StatusAnnouncement`` transitions through
  ``announce_status``; its ``announce_state`` guard turns repeated
  refresh calls into a single announcement per state transition —
  routine repaints and cursor movement never spam the reader.
* **Focus survives refresh.** ``capture_focus`` records the focused
  control (and, for item views, the stable id bound on
  ``STABLE_ID_ROLE``/``UserRole``); ``restore_focus`` re-points it at
  the rebuilt content and only falls back to the surface's primary
  control when the item or control genuinely disappeared — the
  ``FocusSurface.restore_focus`` rule lifted onto real widgets.
* **Disabled controls explain themselves on screen.** A disabled button
  is skipped by Tab navigation, so its reason can never live on the
  button alone: ``reason_label`` renders the ``DisabledControlReason``
  text (why + how to resolve) in a focusable label next to the control.
"""

from __future__ import annotations

from dataclasses import dataclass
import weakref
from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QWidget,
)

from .accessible_labels import announce_status
from .native_accessibility import (
    AnnouncementEvent,
    DisabledControlReason,
    StatusAnnouncement,
)
from .ui_theme import TypographyRole, set_typography_role


# Stable item identity for focus retention. Column-0 items in dynamic
# tables/trees already bind their record on UserRole; surfaces whose
# UserRole payload is a whole object (e.g. GeometryDefect) additionally
# bind the string id here so capture reads a stable value either way.
STABLE_ID_ROLE = Qt.ItemDataRole.UserRole + 40


def _focus_name(widget: QWidget) -> str:
    return widget.objectName() or widget.metaObject().className()


@dataclass(frozen=True)
class FocusToken:
    """Where keyboard focus was anchored before a rebuild.

    ``widget_name`` is the focus widget's objectName (falling back to
    its class name). ``is_view`` marks an item view: those are not
    rebuilt themselves, but their items are — ``item_id`` is then the
    stable identity of the current row (STABLE_ID_ROLE / UserRole).
    ``widget_ref`` weakly references the focused widget so restore can
    leave focus alone when the anchor simply survived the rebuild.
    """

    widget_name: str
    is_view: bool = False
    item_id: Any = None
    widget_ref: Any = None


def capture_focus(root: QWidget) -> FocusToken | None:
    """Record the current keyboard anchor inside ``root``.

    Returns ``None`` when focus is outside the surface — routine
    refresh must never steal focus it did not hold.
    """

    focused = QApplication.focusWidget()
    if focused is None:
        return None
    if focused is not root and not root.isAncestorOf(focused):
        return None
    if isinstance(focused, QAbstractItemView):
        index = focused.currentIndex()
        item_id = None
        if index.isValid():
            item_id = (
                index.data(STABLE_ID_ROLE)
                or index.data(Qt.ItemDataRole.UserRole)
            )
        return FocusToken(
            _focus_name(focused),
            is_view=True,
            item_id=item_id,
            widget_ref=weakref.ref(focused),
        )
    return FocusToken(
        _focus_name(focused), widget_ref=weakref.ref(focused)
    )


def _find_item_index(view: QAbstractItemView, item_id: Any):
    model = view.model()
    for row in range(model.rowCount()):
        index = model.index(row, 0)
        if not index.isValid():
            continue
        if (
            index.data(STABLE_ID_ROLE) == item_id
            or index.data(Qt.ItemDataRole.UserRole) == item_id
        ):
            return index
    return None


def _widget_alive(widget: QWidget | None, root: QWidget) -> bool:
    """A weakref may outlive the wrapped C++ object — guard access."""
    if widget is None:
        return False
    try:
        return widget.isVisibleTo(root) and widget.isEnabled()
    except RuntimeError:
        return False


def restore_focus(
    root: QWidget,
    token: FocusToken | None,
    *,
    fallback: QWidget | None = None,
) -> QWidget | None:
    """Re-anchor keyboard focus after ``root`` rebuilt its content.

    Resolution mirrors ``FocusSurface.restore_focus``: an item view
    re-points its current index at the same stable item id (keeping
    keyboard focus on the view itself when the item vanished, so
    arrows re-anchor to a real row); a destroyed control resolves to
    the rebuilt widget carrying the same stable objectName; a control
    that simply survived keeps focus untouched. Only then the
    fallback. Returns the widget that received focus.
    """

    if token is None:
        return None
    # A rebuild leaves two kinds of pending lifecycle events: the old
    # controls die with deleteLater (still resolvable by weakref /
    # findChild until the DeferredDelete posts — a dead-man-walking
    # survivor would shadow the rebuilt same-named control), and the
    # replacement controls added to a visible hierarchy get their
    # show() as a queued MetaCall so they still test hidden right now.
    # Dispatch both queues so the anchor resolves against the settled
    # widget tree, not the half-mutated one.
    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.sendPostedEvents(None, QEvent.Type.MetaCall)
    if token.is_view:
        view = root.findChild(QAbstractItemView, token.widget_name)
        if view is not None and not view.isHidden():
            if token.item_id is not None:
                index = _find_item_index(view, token.item_id)
                if index is not None:
                    view.setCurrentIndex(index)
            view.setFocus()
            return view
    original = (
        token.widget_ref() if token.widget_ref is not None else None
    )
    if _widget_alive(original, root):
        # The anchor survived the rebuild — do not steal focus back
        # to a rebuilt widget of the same name.
        original.setFocus()
        return original
    for widget in root.findChildren(QWidget, token.widget_name):
        if widget.isEnabled() and widget.isVisibleTo(root):
            widget.setFocus()
            return widget
    if (
        fallback is not None
        and fallback.isEnabled()
        and fallback.isVisibleTo(root)
    ):
        fallback.setFocus()
        return fallback
    return None


class DynamicAnnouncer:
    """Exactly-once announcements for meaningful state transitions.

    ``announce`` emits unconditionally — the caller decides the moment
    is meaningful. ``announce_state`` emits only when ``(aspect,
    state)`` changes, so a refresh that re-renders unchanged state
    stays silent. ``urgent`` maps to the assertive politeness level
    (failures); routine transitions stay polite. Emitted
    ``StatusAnnouncement`` objects are recorded on ``events`` so tests
    can assert the model, not just the Qt sink.
    """

    def __init__(self, target: QWidget) -> None:
        self._target = target
        self._states: dict[str, Any] = {}
        self.events: list[StatusAnnouncement] = []

    def announce(
        self,
        event: AnnouncementEvent,
        text: str,
        *,
        urgent: bool = False,
    ) -> StatusAnnouncement:
        announcement = StatusAnnouncement(
            event=event, text=text, urgent=urgent
        )
        self.events.append(announcement)
        announce_status(self._target, text, assertive=urgent)
        return announcement

    def announce_state(
        self,
        aspect: str,
        state: Any,
        event: AnnouncementEvent,
        text: str,
        *,
        urgent: bool = False,
    ) -> StatusAnnouncement | None:
        """Announce only on a real transition of ``aspect`` to ``state``.

        ``state=None`` clears the aspect silently (a cleared verdict is
        covered by the surface going blank, not an announcement) and
        re-arms the transition.
        """

        if state is None:
            self._states.pop(aspect, None)
            return None
        if self._states.get(aspect) == state:
            return None
        self._states[aspect] = state
        return self.announce(event, text, urgent=urgent)


def reason_label(
    text: str = '',
    parent: QWidget | None = None,
) -> QLabel:
    """On-screen disabled-action explanation for a surface.

    Disabled buttons leave the Tab order, so a keyboard-only user can
    never reach a reason carried only in their tooltip. This label is
    keyboard-focusable and text-selectable: Tab lands on it, the screen
    reader reads it as static text, and the reason stays visible on the
    same screen as the disabled control.
    """

    label = QLabel(text, parent)
    label.setWordWrap(True)
    set_typography_role(label, TypographyRole.SECONDARY)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return label


def disabled_hint(
    control_id: str,
    reason: str,
    resolution: str | None,
) -> str:
    """Validated 'why + how to resolve' text for a disabled control.

    Routed through ``DisabledControlReason`` so an internal token can
    never reach the screen as a 'reason'.
    """

    return DisabledControlReason(
        control_id=control_id, reason=reason, resolution=resolution
    ).display_text()


__all__ = [
    'STABLE_ID_ROLE',
    'DynamicAnnouncer',
    'FocusToken',
    'capture_focus',
    'disabled_hint',
    'reason_label',
    'restore_focus',
]
