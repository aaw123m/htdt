from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .ui_theme import (
    ControlSize,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_surface_role,
    set_typography_role,
)
from .workflow_navigation import (
    CANONICAL_WORKSPACE_CONTEXTS,
    CANONICAL_WORKSPACE_LABELS,
    WorkspaceContext,
    WorkspaceDeepLink,
    WorkspaceId,
    normalize_workspace_context,
    normalize_workspace_id,
)


DeactivationGuard: TypeAlias = Callable[[], tuple[bool, str | None]]
CloseGuard: TypeAlias = Callable[[], tuple[bool, str | None]]


@dataclass(slots=True)
class WorkspaceMount:
    """Boundary between shell composition and a concrete workspace implementation."""

    widget: QWidget
    on_activate: Callable[[], None] | None = None
    on_deactivate: Callable[[], None] | None = None
    before_deactivate: DeactivationGuard | None = None
    on_context_changed: Callable[[str], None] | None = None
    on_entity_requested: Callable[[str], None] | None = None
    on_close: Callable[[], None] | None = None

    @classmethod
    def from_widget(
        cls,
        widget: QWidget,
        *,
        on_activate: Callable[[], None] | None = None,
        on_deactivate: Callable[[], None] | None = None,
        before_deactivate: DeactivationGuard | None = None,
        on_context_changed: Callable[[str], None] | None = None,
        on_entity_requested: Callable[[str], None] | None = None,
    ) -> "WorkspaceMount":
        return cls(
            widget=widget,
            on_activate=on_activate,
            on_deactivate=on_deactivate,
            before_deactivate=before_deactivate,
            on_context_changed=on_context_changed,
            on_entity_requested=on_entity_requested,
            on_close=widget.close,
        )


WorkspaceFactory: TypeAlias = Callable[[], WorkspaceMount]


@dataclass(frozen=True, slots=True)
class WorkspaceRegistration:
    workspace_id: WorkspaceId
    label: str
    factory: WorkspaceFactory
    contexts: tuple[WorkspaceContext, ...] = ()


def build_canonical_workspace_registrations(
    factories: Mapping[WorkspaceId, WorkspaceFactory],
) -> tuple[WorkspaceRegistration, ...]:
    missing = tuple(workspace_id for workspace_id in WorkspaceId if workspace_id not in factories)
    if missing:
        names = ", ".join(workspace_id.value for workspace_id in missing)
        raise ValueError(f"missing workspace factories: {names}")
    return tuple(
        WorkspaceRegistration(
            workspace_id=workspace_id,
            label=CANONICAL_WORKSPACE_LABELS[workspace_id],
            contexts=CANONICAL_WORKSPACE_CONTEXTS[workspace_id],
            factory=factories[workspace_id],
        )
        for workspace_id in WorkspaceId
    )


class WorkspaceRouter(QStackedWidget):
    """Lazy workspace host with a fail-closed deactivation boundary."""

    def __init__(self, registrations: Iterable[WorkspaceRegistration], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workflowRouter")
        set_surface_role(self, SurfaceRole.BASE)
        self._registrations = {registration.workspace_id: registration for registration in registrations}
        if set(self._registrations) != set(WorkspaceId):
            raise ValueError("workflow router requires exactly the four canonical workspace destinations")
        self._mounts: dict[WorkspaceId, WorkspaceMount] = {}
        self._current_workspace_id: WorkspaceId | None = None
        self.last_block_reason: str | None = None

    @property
    def current_workspace_id(self) -> WorkspaceId | None:
        return self._current_workspace_id

    def registration(self, workspace_id: WorkspaceId | str) -> WorkspaceRegistration:
        return self._registrations[normalize_workspace_id(workspace_id)]

    def mount(self, workspace_id: WorkspaceId | str) -> WorkspaceMount | None:
        return self._mounts.get(normalize_workspace_id(workspace_id))

    def navigate(self, workspace_id: WorkspaceId | str) -> WorkspaceMount | None:
        destination = normalize_workspace_id(workspace_id)
        self.last_block_reason = None
        if destination == self._current_workspace_id:
            return self._ensure_mount(destination)

        current = self._mounts.get(self._current_workspace_id) if self._current_workspace_id is not None else None
        if current is not None and current.before_deactivate is not None:
            allowed, reason = current.before_deactivate()
            if not allowed:
                self.last_block_reason = reason or "現在の作業を完了してから画面を切り替えてください"
                return None

        if current is not None and current.on_deactivate is not None:
            current.on_deactivate()

        mount = self._ensure_mount(destination)
        self.setCurrentWidget(mount.widget)
        self._current_workspace_id = destination
        if mount.on_activate is not None:
            mount.on_activate()
        return mount

    def select_context(self, workspace_id: WorkspaceId | str, context_id: str) -> str:
        destination = normalize_workspace_id(workspace_id)
        normalized_context = normalize_workspace_context(destination, context_id)
        registration = self._registrations[destination]
        valid_ids = {context.context_id for context in registration.contexts}
        if normalized_context not in valid_ids:
            raise ValueError(
                f"unknown context {context_id!r} for workspace {destination.value!r}"
            )
        mount = self._ensure_mount(destination)
        if mount.on_context_changed is not None:
            mount.on_context_changed(normalized_context)
        return normalized_context

    def request_entity(self, workspace_id: WorkspaceId | str, entity_id: str) -> None:
        mount = self._ensure_mount(normalize_workspace_id(workspace_id))
        if mount.on_entity_requested is not None:
            mount.on_entity_requested(entity_id)

    def can_dispose_all(self) -> tuple[bool, str | None]:
        for workspace_id, mount in self._mounts.items():
            if mount.before_deactivate is None:
                continue
            allowed, reason = mount.before_deactivate()
            if not allowed:
                label = self._registrations[workspace_id].label
                return False, reason or f"{label}の作業を完了してから復元してください"
        return True, None

    def dispose_mounts(self) -> None:
        mounts = tuple(self._mounts.values())
        self._mounts.clear()
        self._current_workspace_id = None
        for mount in mounts:
            if mount.on_close is not None:
                mount.on_close()
            else:
                mount.widget.close()
            self.removeWidget(mount.widget)
            mount.widget.setParent(None)
            mount.widget.deleteLater()

    def shutdown(self) -> None:
        self.dispose_mounts()

    def _ensure_mount(self, workspace_id: WorkspaceId) -> WorkspaceMount:
        existing = self._mounts.get(workspace_id)
        if existing is not None:
            return existing
        mount = self._registrations[workspace_id].factory()
        if not isinstance(mount, WorkspaceMount):
            raise TypeError("workspace factory must return WorkspaceMount")
        if mount.widget.parent() is not None:
            raise ValueError("workspace factory must return an unparented widget")
        self._mounts[workspace_id] = mount
        self.addWidget(mount.widget)
        return mount


class WorkflowRail(QFrame):
    EXPANDED_WIDTH = 184
    COMPACT_WIDTH = 112

    def __init__(
        self,
        registrations: Iterable[WorkspaceRegistration],
        on_navigate: Callable[[WorkspaceId], bool],
        on_settings: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workflowRail")
        set_surface_role(self, SurfaceRole.RAISED)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._compact = False

        self._buttons: dict[WorkspaceId, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 16, 12, 16)
        self._layout.setSpacing(6)

        self._brand = QLabel("HTDT")
        set_typography_role(self._brand, TypographyRole.WORKSPACE_TITLE)
        self._layout.addWidget(self._brand)
        self._layout.addSpacing(12)

        for registration in registrations:
            button = QPushButton(registration.label)
            button.setCheckable(True)
            button.setProperty("workspaceId", registration.workspace_id.value)
            set_control_size(button, ControlSize.STANDARD)
            button.clicked.connect(
                lambda checked=False, workspace_id=registration.workspace_id: on_navigate(workspace_id)
            )
            self._group.addButton(button)
            self._buttons[registration.workspace_id] = button
            self._layout.addWidget(button)

        self._layout.addStretch(1)

        self.settings_button = QPushButton("設定")
        self.settings_button.setObjectName("workflowSettingsButton")
        set_control_size(self.settings_button, ControlSize.STANDARD)
        if on_settings is not None:
            self.settings_button.clicked.connect(lambda checked=False: on_settings())
        self._layout.addWidget(self.settings_button)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(button.text() for button in self._buttons.values())

    @property
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self.setFixedWidth(self.COMPACT_WIDTH if compact else self.EXPANDED_WIDTH)
        self._brand.setVisible(not compact)
        margin_x = 8 if compact else 12
        margin_y = 10 if compact else 16
        self._layout.setContentsMargins(margin_x, margin_y, margin_x, margin_y)
        self._layout.setSpacing(4 if compact else 6)

    def set_active(self, workspace_id: WorkspaceId | str) -> None:
        self._buttons[normalize_workspace_id(workspace_id)].setChecked(True)


class TopContextBar(QFrame):
    def __init__(self, on_context_selected: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workflowContextBar")
        set_surface_role(self, SurfaceRole.RAISED)
        self._on_context_selected = on_context_selected
        self._context_buttons: dict[str, QPushButton] = {}
        self._context_group: QButtonGroup | None = None

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(22, 10, 18, 10)
        self._layout.setSpacing(8)
        self._compact = False

        self._title = QLabel()
        set_typography_role(self._title, TypographyRole.SECTION_TITLE)
        self._layout.addWidget(self._title)
        self._layout.addSpacing(12)

        self._context_container = QWidget()
        self._context_layout = QHBoxLayout(self._context_container)
        self._context_layout.setContentsMargins(0, 0, 0, 0)
        self._context_layout.setSpacing(4)
        self._layout.addWidget(self._context_container)
        self._layout.addStretch(1)

    @property
    def context_labels(self) -> tuple[str, ...]:
        return tuple(button.text() for button in self._context_buttons.values())

    @property
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if self._compact == compact:
            return
        self._compact = compact
        self._title.setVisible(not compact)
        self._layout.setContentsMargins(
            10 if compact else 22,
            7 if compact else 10,
            10 if compact else 18,
            7 if compact else 10,
        )
        self._layout.setSpacing(4 if compact else 8)
        self._context_layout.setSpacing(2 if compact else 4)

    def set_workspace(self, registration: WorkspaceRegistration, selected_context_id: str | None) -> None:
        self._title.setText(registration.label)
        self._clear_contexts()
        if not registration.contexts:
            self._context_container.setVisible(False)
            return

        self._context_container.setVisible(True)
        self._context_group = QButtonGroup(self)
        self._context_group.setExclusive(True)
        for context in registration.contexts:
            button = QPushButton(context.label)
            button.setCheckable(True)
            set_control_size(button, ControlSize.COMPACT)
            button.clicked.connect(
                lambda checked=False, context_id=context.context_id: self._on_context_selected(context_id)
            )
            self._context_group.addButton(button)
            self._context_buttons[context.context_id] = button
            self._context_layout.addWidget(button)

        if selected_context_id is not None and selected_context_id in self._context_buttons:
            self._context_buttons[selected_context_id].setChecked(True)

    def set_active_context(self, context_id: str) -> None:
        button = self._context_buttons.get(context_id)
        if button is None:
            raise ValueError(f"context {context_id!r} is not visible in the current workspace")
        button.setChecked(True)

    def _clear_contexts(self) -> None:
        if self._context_group is not None:
            self._context_group.deleteLater()
            self._context_group = None
        self._context_buttons.clear()
        while self._context_layout.count():
            item = self._context_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


class WorkflowShellWindow(QMainWindow):
    """Workflow-first shell; domain and SceneRevision authority stay in workspaces."""

    settingsRequested = Signal()

    def __init__(
        self,
        registrations: Iterable[WorkspaceRegistration],
        *,
        initial_workspace: WorkspaceId = WorkspaceId.OVERVIEW,
    ) -> None:
        super().__init__()
        self.setObjectName("workflowShell")
        self.setWindowTitle("Home Theater Digital Twin")

        registration_tuple = tuple(registrations)
        self._registrations = {registration.workspace_id: registration for registration in registration_tuple}
        if set(self._registrations) != set(WorkspaceId):
            raise ValueError("workflow shell requires exactly the four canonical workspace destinations")

        self._selected_context: dict[WorkspaceId, str] = {}
        self._close_guards: list[CloseGuard] = []
        self._data_mutations_frozen = False
        for registration in registration_tuple:
            if registration.contexts:
                self._selected_context[registration.workspace_id] = registration.contexts[0].context_id

        root = QWidget()
        set_surface_role(root, SurfaceRole.BASE)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.rail = WorkflowRail(
            registration_tuple,
            self.navigate,
            on_settings=self.settingsRequested.emit,
        )
        root_layout.addWidget(self.rail)

        content = QFrame()
        set_surface_role(content, SurfaceRole.BASE)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.context_bar = TopContextBar(self._select_current_context)
        content_layout.addWidget(self.context_bar)

        self.router = WorkspaceRouter(registration_tuple)
        content_layout.addWidget(self.router, 1)

        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)
        self.resize(1440, 900)
        self._update_responsive_layout()
        if not self.navigate(initial_workspace):
            raise RuntimeError("initial workflow workspace could not be activated")

    def _update_responsive_layout(self) -> None:
        compact = self.width() < 1120
        self.rail.set_compact(compact)
        self.context_bar.set_compact(compact)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_responsive_layout()

    @property
    def current_workspace_id(self) -> WorkspaceId:
        current = self.router.current_workspace_id
        if current is None:
            raise RuntimeError("workflow shell has not selected a workspace")
        return current

    @property
    def navigation_labels(self) -> tuple[str, ...]:
        return self.rail.labels

    @property
    def context_labels(self) -> tuple[str, ...]:
        return self.context_bar.context_labels

    def navigate(self, workspace_id: WorkspaceId | str) -> bool:
        if self._data_mutations_frozen:
            self.statusBar().showMessage("データ処理中は画面を切り替えられません")
            return False
        destination = normalize_workspace_id(workspace_id)
        previous = self.router.current_workspace_id
        registration = self._registrations[destination]
        mount = self.router.navigate(destination)
        if mount is None:
            if previous is not None:
                self.rail.set_active(previous)
            self.statusBar().showMessage(self.router.last_block_reason or "画面を切り替えられません")
            return False

        self.rail.set_active(destination)
        selected_context = self._selected_context.get(destination)
        self.context_bar.set_workspace(registration, selected_context)
        if selected_context is not None:
            self.router.select_context(destination, selected_context)
        self.statusBar().clearMessage()
        return True

    def select_context(self, context_id: str) -> None:
        self._select_current_context(context_id)

    def handle_deep_link(self, target: WorkspaceDeepLink) -> bool:
        if not self.navigate(target.workspace):
            return False
        if target.subsection is not None:
            self.select_context(target.subsection)
        if target.entity_id is not None:
            self.router.request_entity(target.workspace, target.entity_id)
        return True

    def _select_current_context(self, context_id: str) -> None:
        if self._data_mutations_frozen:
            return
        workspace_id = self.current_workspace_id
        normalized_context = self.router.select_context(workspace_id, context_id)
        self._selected_context[workspace_id] = normalized_context
        self.context_bar.set_active_context(normalized_context)

    def register_close_guard(self, guard: CloseGuard) -> None:
        self._close_guards.append(guard)

    def freeze_data_mutations(self) -> None:
        self._data_mutations_frozen = True
        self.rail.setEnabled(False)
        self.context_bar.setEnabled(False)
        self.router.setEnabled(False)

    def thaw_data_mutations(self) -> None:
        self._data_mutations_frozen = False
        self.rail.setEnabled(True)
        self.context_bar.setEnabled(True)
        self.router.setEnabled(True)

    def dispose_data_workspaces(self) -> None:
        allowed, reason = self.router.can_dispose_all()
        if not allowed:
            raise RuntimeError(reason or "現在の作業を完了してから復元してください")
        self.router.dispose_mounts()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        for guard in self._close_guards:
            allowed, reason = guard()
            if not allowed:
                self.statusBar().showMessage(reason or "現在の処理が完了してから終了してください")
                event.ignore()
                return
        current = self.router.mount(self.router.current_workspace_id) if self.router.current_workspace_id else None
        if current is not None and current.before_deactivate is not None:
            allowed, reason = current.before_deactivate()
            if not allowed:
                self.statusBar().showMessage(reason or "現在の作業を完了してから終了してください")
                event.ignore()
                return
        self.router.shutdown()
        super().closeEvent(event)


__all__ = [
    "WorkflowShellWindow",
    "WorkspaceFactory",
    "WorkspaceMount",
    "WorkspaceRegistration",
    "build_canonical_workspace_registrations",
]
