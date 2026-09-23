from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from .command_palette import CommandShortcutBinder
from .command_registry import AvailabilityProvider, CommandRegistry


class CadAxis(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"


class CadPointerGesture(StrEnum):
    PAN = "pan"
    ORBIT = "orbit"


CAD_SCENE_COMMAND_IDS: tuple[str, ...] = (
    "room.transform.move",
    "room.transform.rotate",
    "room.view.fit_selection",
    "room.view.fit_all",
    "room.edit.cancel",
    "room.edit.commit",
    "room.edit.duplicate",
    "room.transform.axis_x",
    "room.transform.axis_y",
    "room.transform.axis_z",
)

# Save/undo/redo remain document/application commands owned by the existing registry.
# The CAD controller only gives them a Room-scoped keyboard entry point.
CAD_SHORTCUT_COMMAND_IDS: tuple[str, ...] = (
    "project.save",
    "edit.undo",
    "edit.redo",
    *CAD_SCENE_COMMAND_IDS,
)


class CadViewportInputPort(Protocol):
    """Camera/context-menu boundary implemented by the Room viewport.

    This port deliberately contains no Scene/WorkingDocument mutation methods.
    Pointer navigation remains a viewport concern; edits continue through commands.
    """

    def begin_pan(self, position: QPointF) -> None: ...

    def pan_by(self, delta: QPointF) -> None: ...

    def end_pan(self, position: QPointF) -> None: ...

    def begin_orbit(self, position: QPointF) -> None: ...

    def orbit_by(self, delta: QPointF) -> None: ...

    def end_orbit(self, position: QPointF) -> None: ...

    def zoom_by(self, steps: float, position: QPointF) -> None: ...

    def open_context_menu(
        self,
        position: QPointF,
        global_position: QPointF,
    ) -> None: ...


CommandCallback = Callable[[], None]
AxisConstraintCallback = Callable[[CadAxis], None]


@dataclass(slots=True)
class CadCommandBindings:
    """Room-owned callbacks attached to canonical command IDs.

    Availability providers are optional and keyed by command ID. When present they
    remain the live source of truth used by CommandRegistry.execute() and the command
    palette. Missing callbacks are explicitly unbound to avoid stale workspace
    references.
    """

    move: CommandCallback | None = None
    rotate: CommandCallback | None = None
    fit_selection: CommandCallback | None = None
    fit_all: CommandCallback | None = None
    cancel: CommandCallback | None = None
    commit: CommandCallback | None = None
    duplicate: CommandCallback | None = None
    constrain_axis: AxisConstraintCallback | None = None
    availability: Mapping[str, AvailabilityProvider] = field(default_factory=dict)


def _bind_optional(
    registry: CommandRegistry,
    command_id: str,
    callback: CommandCallback | None,
    availability: Mapping[str, AvailabilityProvider],
) -> None:
    if callback is None:
        registry.unbind(command_id)
        return
    registry.bind(
        command_id,
        execute=callback,
        availability=availability.get(command_id),
    )


def bind_cad_input_commands(
    registry: CommandRegistry,
    bindings: CadCommandBindings,
) -> None:
    """Attach Room input callbacks without moving edit/domain authority here."""

    _bind_optional(
        registry,
        "room.transform.move",
        bindings.move,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.transform.rotate",
        bindings.rotate,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.view.fit_selection",
        bindings.fit_selection,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.view.fit_all",
        bindings.fit_all,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.edit.cancel",
        bindings.cancel,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.edit.commit",
        bindings.commit,
        bindings.availability,
    )
    _bind_optional(
        registry,
        "room.edit.duplicate",
        bindings.duplicate,
        bindings.availability,
    )

    axis_callback = bindings.constrain_axis
    for axis in CadAxis:
        command_id = f"room.transform.axis_{axis.value}"
        callback = None
        if axis_callback is not None:
            callback = lambda axis=axis: axis_callback(axis)
        _bind_optional(
            registry,
            command_id,
            callback,
            bindings.availability,
        )


def unbind_cad_input_commands(registry: CommandRegistry) -> None:
    for command_id in CAD_SCENE_COMMAND_IDS:
        registry.unbind(command_id)


def pointer_gesture_for(
    button: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier,
) -> CadPointerGesture | None:
    if button != Qt.MouseButton.MiddleButton:
        return None
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return CadPointerGesture.ORBIT
    return CadPointerGesture.PAN


def wheel_zoom_steps(event: QWheelEvent) -> float:
    """Normalize a standard wheel notch to one zoom step.

    High-resolution wheels can return fractions of 120. Pixel-only devices are kept
    proportional rather than discarded, while the viewport remains free to choose
    the actual camera zoom curve.
    """

    angle_y = event.angleDelta().y()
    if angle_y:
        return float(angle_y) / 120.0
    pixel_y = event.pixelDelta().y()
    if pixel_y:
        return float(pixel_y) / 120.0
    return 0.0


class CadInputController(QObject):
    """Reusable UX120 mouse/keyboard controller for a Room workspace.

    Keyboard metadata comes only from CommandRegistry. Mouse gestures are translated
    into a narrow viewport port so Agent A can choose the concrete renderer/camera
    implementation without duplicating shortcut policy.
    """

    def __init__(
        self,
        *,
        shortcut_parent: QWidget,
        viewport: QWidget,
        registry: CommandRegistry,
        viewport_port: CadViewportInputPort,
        command_bindings: CadCommandBindings | None = None,
    ) -> None:
        super().__init__(shortcut_parent)
        self._viewport = viewport
        self._registry = registry
        self._viewport_port = viewport_port
        self._gesture: CadPointerGesture | None = None
        self._last_pointer_position: QPointF | None = None
        self._right_button_pressed = False
        self._owns_command_bindings = command_bindings is not None

        if command_bindings is not None:
            bind_cad_input_commands(registry, command_bindings)

        self.shortcuts = CommandShortcutBinder(
            shortcut_parent,
            registry,
            command_ids=CAD_SHORTCUT_COMMAND_IDS,
            shortcut_context=Qt.ShortcutContext.WidgetWithChildrenShortcut,
        )
        viewport.installEventFilter(self)

    def refresh_shortcuts(self) -> None:
        """Refresh focus + live availability after selection/edit state changes."""

        self.shortcuts.refresh()

    def dispose(self) -> None:
        """Detach this controller before replacing/remounting its Room workspace."""

        self._viewport.removeEventFilter(self)
        self._gesture = None
        self._last_pointer_position = None
        self._right_button_pressed = False
        if self._owns_command_bindings:
            unbind_cad_input_commands(self._registry)
            self._owns_command_bindings = False
        self.shortcuts.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is not self._viewport:
            return False

        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            return self._mouse_press(cast(QMouseEvent, event))
        if event_type == QEvent.Type.MouseMove:
            return self._mouse_move(cast(QMouseEvent, event))
        if event_type == QEvent.Type.MouseButtonRelease:
            return self._mouse_release(cast(QMouseEvent, event))
        if event_type == QEvent.Type.Wheel:
            return self._wheel(cast(QWheelEvent, event))
        if event_type in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self._cancel_pointer_gesture()
        return False

    def _mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton:
            self._right_button_pressed = True
            event.accept()
            return True

        gesture = pointer_gesture_for(event.button(), event.modifiers())
        if gesture is None:
            return False

        self._gesture = gesture
        self._last_pointer_position = QPointF(event.position())
        if gesture is CadPointerGesture.PAN:
            self._viewport_port.begin_pan(QPointF(event.position()))
        else:
            self._viewport_port.begin_orbit(QPointF(event.position()))
        event.accept()
        return True

    def _mouse_move(self, event: QMouseEvent) -> bool:
        if event.buttons() & Qt.MouseButton.RightButton:
            # RMB is reserved for the context menu and must never become camera nav.
            event.accept()
            return True

        if self._gesture is None:
            return False
        if not (event.buttons() & Qt.MouseButton.MiddleButton):
            self._cancel_pointer_gesture()
            return False

        current = QPointF(event.position())
        previous = self._last_pointer_position
        self._last_pointer_position = current
        if previous is None:
            return True

        delta = current - previous
        if delta.isNull():
            return True
        if self._gesture is CadPointerGesture.PAN:
            self._viewport_port.pan_by(delta)
        else:
            self._viewport_port.orbit_by(delta)
        event.accept()
        return True

    def _mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.RightButton:
            if self._right_button_pressed:
                self._viewport_port.open_context_menu(
                    QPointF(event.position()),
                    QPointF(event.globalPosition()),
                )
            self._right_button_pressed = False
            event.accept()
            return True

        if event.button() != Qt.MouseButton.MiddleButton:
            return False

        position = QPointF(event.position())
        gesture = self._gesture
        self._gesture = None
        self._last_pointer_position = None
        if gesture is CadPointerGesture.PAN:
            self._viewport_port.end_pan(position)
        elif gesture is CadPointerGesture.ORBIT:
            self._viewport_port.end_orbit(position)
        event.accept()
        return True

    def _wheel(self, event: QWheelEvent) -> bool:
        steps = wheel_zoom_steps(event)
        if steps == 0.0:
            return False
        self._viewport_port.zoom_by(steps, QPointF(event.position()))
        event.accept()
        return True

    def _cancel_pointer_gesture(self) -> None:
        gesture = self._gesture
        position = self._last_pointer_position
        self._gesture = None
        self._last_pointer_position = None
        if gesture is CadPointerGesture.PAN and position is not None:
            self._viewport_port.end_pan(position)
        elif gesture is CadPointerGesture.ORBIT and position is not None:
            self._viewport_port.end_orbit(position)


__all__ = [
    "CAD_SCENE_COMMAND_IDS",
    "CAD_SHORTCUT_COMMAND_IDS",
    "CadAxis",
    "CadCommandBindings",
    "CadInputController",
    "CadPointerGesture",
    "CadViewportInputPort",
    "bind_cad_input_commands",
    "pointer_gesture_for",
    "unbind_cad_input_commands",
    "wheel_zoom_steps",
]
