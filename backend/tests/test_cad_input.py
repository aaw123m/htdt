from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLineEdit, QVBoxLayout, QWidget

from htdt.cad_input import (
    CAD_SCENE_COMMAND_IDS,
    CadAxis,
    CadCommandBindings,
    CadInputController,
    CadPointerGesture,
    bind_cad_input_commands,
    pointer_gesture_for,
    wheel_zoom_steps,
)
from htdt.command_registry import (
    CommandAvailability,
    CommandRegistry,
    ShortcutBehavior,
    default_command_definitions,
    register_default_commands,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _PointerEvent:
    def __init__(
        self,
        event_type: QEvent.Type,
        *,
        button: Qt.MouseButton = Qt.MouseButton.NoButton,
        buttons: Qt.MouseButton = Qt.MouseButton.NoButton,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
        position: QPointF = QPointF(),
        global_position: QPointF | None = None,
    ) -> None:
        self._event_type = event_type
        self._button = button
        self._buttons = buttons
        self._modifiers = modifiers
        self._position = QPointF(position)
        self._global_position = QPointF(global_position or position)
        self.accepted = False

    def type(self) -> QEvent.Type:
        return self._event_type

    def button(self) -> Qt.MouseButton:
        return self._button

    def buttons(self) -> Qt.MouseButton:
        return self._buttons

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers

    def position(self) -> QPointF:
        return QPointF(self._position)

    def globalPosition(self) -> QPointF:
        return QPointF(self._global_position)

    def accept(self) -> None:
        self.accepted = True


class _WheelEvent(_PointerEvent):
    def __init__(
        self,
        *,
        angle_y: int = 0,
        pixel_y: int = 0,
        position: QPointF = QPointF(),
    ) -> None:
        super().__init__(QEvent.Type.Wheel, position=position)
        self._angle_y = angle_y
        self._pixel_y = pixel_y

    def angleDelta(self) -> QPoint:
        return QPoint(0, self._angle_y)

    def pixelDelta(self) -> QPoint:
        return QPoint(0, self._pixel_y)


class _ViewportPort:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def begin_pan(self, position: QPointF) -> None:
        self.events.append(("pan-begin", position.x(), position.y()))

    def pan_by(self, delta: QPointF) -> None:
        self.events.append(("pan", delta.x(), delta.y()))

    def end_pan(self, position: QPointF) -> None:
        self.events.append(("pan-end", position.x(), position.y()))

    def begin_orbit(self, position: QPointF) -> None:
        self.events.append(("orbit-begin", position.x(), position.y()))

    def orbit_by(self, delta: QPointF) -> None:
        self.events.append(("orbit", delta.x(), delta.y()))

    def end_orbit(self, position: QPointF) -> None:
        self.events.append(("orbit-end", position.x(), position.y()))

    def zoom_by(self, steps: float, position: QPointF) -> None:
        self.events.append(("zoom", steps, position.x(), position.y()))

    def open_context_menu(
        self,
        position: QPointF,
        global_position: QPointF,
    ) -> None:
        self.events.append(
            (
                "context",
                position.x(),
                position.y(),
                global_position.x(),
                global_position.y(),
            )
        )


def test_canonical_cad_shortcuts_live_in_command_registry() -> None:
    definitions = {item.command_id: item for item in default_command_definitions()}

    assert set(CAD_SCENE_COMMAND_IDS) <= definitions.keys()
    assert definitions["room.transform.move"].shortcut == "M"
    assert definitions["room.transform.rotate"].shortcut == "R"
    assert definitions["room.view.fit_selection"].shortcut == "F"
    assert definitions["room.view.fit_all"].shortcut == "Home"
    assert definitions["room.edit.cancel"].shortcut == "Esc"
    assert definitions["room.edit.commit"].shortcut == "Enter"
    assert definitions["room.edit.duplicate"].shortcut == "Ctrl+D"
    assert definitions["room.transform.axis_x"].shortcut == "X"
    assert definitions["room.transform.axis_y"].shortcut == "Y"
    assert definitions["room.transform.axis_z"].shortcut == "Z"
    assert all(
        definitions[command_id].shortcut_behavior is ShortcutBehavior.FOCUS_SAFE
        for command_id in CAD_SCENE_COMMAND_IDS
    )


def test_command_bindings_delegate_and_keep_live_availability() -> None:
    registry = CommandRegistry()
    register_default_commands(registry)
    events: list[object] = []
    move_enabled = {"value": False}

    bind_cad_input_commands(
        registry,
        CadCommandBindings(
            move=lambda: events.append("move"),
            rotate=lambda: events.append("rotate"),
            fit_selection=lambda: events.append("fit-selection"),
            fit_all=lambda: events.append("fit-all"),
            cancel=lambda: events.append("cancel"),
            commit=lambda: events.append("commit"),
            duplicate=lambda: events.append("duplicate"),
            constrain_axis=lambda axis: events.append(axis),
            availability={
                "room.transform.move": lambda: (
                    CommandAvailability.available()
                    if move_enabled["value"]
                    else CommandAvailability.unavailable("選択してください")
                )
            },
        ),
    )

    assert registry.execute("room.transform.move") is False
    move_enabled["value"] = True
    assert registry.execute("room.transform.move") is True
    assert registry.execute("room.transform.rotate") is True
    assert registry.execute("room.view.fit_selection") is True
    assert registry.execute("room.view.fit_all") is True
    assert registry.execute("room.edit.cancel") is True
    assert registry.execute("room.edit.commit") is True
    assert registry.execute("room.edit.duplicate") is True
    assert registry.execute("room.transform.axis_y") is True

    assert events == [
        "move",
        "rotate",
        "fit-selection",
        "fit-all",
        "cancel",
        "commit",
        "duplicate",
        CadAxis.Y,
    ]


def test_pointer_policy_maps_middle_button_shift_wheel_and_right_click() -> None:
    _app()
    host = QWidget()
    viewport = QWidget(host)
    registry = CommandRegistry()
    register_default_commands(registry)
    port = _ViewportPort()
    controller = CadInputController(
        shortcut_parent=host,
        viewport=viewport,
        registry=registry,
        viewport_port=port,
    )

    press = _PointerEvent(
        QEvent.Type.MouseButtonPress,
        button=Qt.MouseButton.MiddleButton,
        buttons=Qt.MouseButton.MiddleButton,
        position=QPointF(10, 20),
    )
    assert controller.eventFilter(viewport, press)
    move = _PointerEvent(
        QEvent.Type.MouseMove,
        buttons=Qt.MouseButton.MiddleButton,
        position=QPointF(14, 17),
    )
    assert controller.eventFilter(viewport, move)
    release = _PointerEvent(
        QEvent.Type.MouseButtonRelease,
        button=Qt.MouseButton.MiddleButton,
        position=QPointF(14, 17),
    )
    assert controller.eventFilter(viewport, release)

    orbit_press = _PointerEvent(
        QEvent.Type.MouseButtonPress,
        button=Qt.MouseButton.MiddleButton,
        buttons=Qt.MouseButton.MiddleButton,
        modifiers=Qt.KeyboardModifier.ShiftModifier,
        position=QPointF(3, 4),
    )
    assert controller.eventFilter(viewport, orbit_press)
    orbit_move = _PointerEvent(
        QEvent.Type.MouseMove,
        buttons=Qt.MouseButton.MiddleButton,
        modifiers=Qt.KeyboardModifier.ShiftModifier,
        position=QPointF(1, 9),
    )
    assert controller.eventFilter(viewport, orbit_move)
    orbit_release = _PointerEvent(
        QEvent.Type.MouseButtonRelease,
        button=Qt.MouseButton.MiddleButton,
        position=QPointF(1, 9),
    )
    assert controller.eventFilter(viewport, orbit_release)

    wheel = _WheelEvent(angle_y=120, position=QPointF(7, 8))
    assert controller.eventFilter(viewport, wheel)

    right_press = _PointerEvent(
        QEvent.Type.MouseButtonPress,
        button=Qt.MouseButton.RightButton,
        buttons=Qt.MouseButton.RightButton,
        position=QPointF(5, 6),
    )
    assert controller.eventFilter(viewport, right_press)
    right_release = _PointerEvent(
        QEvent.Type.MouseButtonRelease,
        button=Qt.MouseButton.RightButton,
        position=QPointF(5, 6),
        global_position=QPointF(50, 60),
    )
    assert controller.eventFilter(viewport, right_release)

    assert port.events == [
        ("pan-begin", 10.0, 20.0),
        ("pan", 4.0, -3.0),
        ("pan-end", 14.0, 17.0),
        ("orbit-begin", 3.0, 4.0),
        ("orbit", -2.0, 5.0),
        ("orbit-end", 1.0, 9.0),
        ("zoom", 1.0, 7.0, 8.0),
        ("context", 5.0, 6.0, 50.0, 60.0),
    ]

    controller.dispose()


def test_scene_shortcuts_are_disabled_while_text_or_numeric_input_has_focus() -> None:
    app = _app()
    host = QWidget()
    layout = QVBoxLayout(host)
    viewport = QWidget(host)
    viewport.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    search = QLineEdit(host)
    numeric = QDoubleSpinBox(host)
    layout.addWidget(viewport)
    layout.addWidget(search)
    layout.addWidget(numeric)

    registry = CommandRegistry()
    register_default_commands(registry)
    port = _ViewportPort()
    controller = CadInputController(
        shortcut_parent=host,
        viewport=viewport,
        registry=registry,
        viewport_port=port,
        command_bindings=CadCommandBindings(move=lambda: None),
    )
    host.show()
    app.processEvents()

    move_shortcut = next(
        shortcut
        for command_id, shortcut in controller.shortcuts._shortcuts
        if command_id == "room.transform.move"
    )
    assert move_shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut

    search.setFocus()
    app.processEvents()
    controller.refresh_shortcuts()
    assert move_shortcut.isEnabled() is False

    numeric.setFocus()
    app.processEvents()
    controller.refresh_shortcuts()
    assert move_shortcut.isEnabled() is False

    viewport.setFocus()
    app.processEvents()
    controller.refresh_shortcuts()
    assert move_shortcut.isEnabled() is True

    controller.dispose()
    host.close()
    host.deleteLater()
    app.processEvents()


def test_pointer_helpers_cover_cad_contract() -> None:
    assert (
        pointer_gesture_for(
            Qt.MouseButton.MiddleButton,
            Qt.KeyboardModifier.NoModifier,
        )
        is CadPointerGesture.PAN
    )
    assert (
        pointer_gesture_for(
            Qt.MouseButton.MiddleButton,
            Qt.KeyboardModifier.ShiftModifier,
        )
        is CadPointerGesture.ORBIT
    )
    assert (
        pointer_gesture_for(
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
        is None
    )
    assert wheel_zoom_steps(_WheelEvent(angle_y=-240)) == -2.0
    assert wheel_zoom_steps(_WheelEvent(pixel_y=60)) == 0.5
