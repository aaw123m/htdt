from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TypeAlias

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .accessible_labels import (
    wire_label_buddies,
    wire_status_announcements,
)
from .ui_theme import (
    ControlSize,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_surface_role,
    set_typography_role,
)
from .navigation_target import (
    NavigationHistory,
    NavigationIntent,
    NavigationResolution,
    NavigationResolver,
    NavigationTarget,
    NavigationTargetKind,
    navigation_kind_label,
)
from .workflow_navigation import (
    CANONICAL_WORKSPACE_CONTEXTS,
    CANONICAL_WORKSPACE_LABELS,
    DestinationId,
    NavigationScope,
    PROJECT_WORKSPACE_IDS,
    WorkspaceContext,
    WorkspaceDeepLink,
    WorkspaceId,
    destination_label,
    destination_scope,
    normalize_destination_id,
    normalize_workspace_context,
    normalize_workspace_id,
    workspace_context_label,
)
from .workspace_dirty_state import (
    DeactivationContext,
    DirtyResolutionAction,
    WorkspaceDirtyState,
)


_LOGGER = logging.getLogger(__name__)


DeactivationGuard: TypeAlias = Callable[[], tuple[bool, str | None]]
CloseGuard: TypeAlias = Callable[[], tuple[bool, str | None]]
DirtyStateProvider: TypeAlias = Callable[[], WorkspaceDirtyState]
DirtyStateResolver: TypeAlias = Callable[
    [DirtyResolutionAction], tuple[bool, str | None]
]


@dataclass(frozen=True, slots=True)
class TargetFocusResult:
    """What a workspace focus port did with a typed navigation target."""

    focused: bool
    message: str | None = None


@dataclass(slots=True)
class WorkspaceMount:
    """Boundary between shell composition and a concrete workspace implementation."""

    widget: QWidget
    on_activate: Callable[[], None] | None = None
    on_deactivate: Callable[[], None] | None = None
    before_deactivate: DeactivationGuard | None = None
    dirty_state: DirtyStateProvider | None = None
    resolve_dirty_state: DirtyStateResolver | None = None
    on_context_changed: Callable[[str], None] | None = None
    on_entity_requested: Callable[[str], None] | None = None
    on_close: Callable[[], None] | None = None
    focus_kinds: frozenset[NavigationTargetKind] = field(
        default_factory=frozenset
    )
    focus_target: Callable[[NavigationTarget], TargetFocusResult] | None = None

    @classmethod
    def from_widget(
        cls,
        widget: QWidget,
        *,
        on_activate: Callable[[], None] | None = None,
        on_deactivate: Callable[[], None] | None = None,
        before_deactivate: DeactivationGuard | None = None,
        dirty_state: DirtyStateProvider | None = None,
        resolve_dirty_state: DirtyStateResolver | None = None,
        on_context_changed: Callable[[str], None] | None = None,
        on_entity_requested: Callable[[str], None] | None = None,
        focus_kinds: Iterable[NavigationTargetKind] = (),
        focus_target: Callable[[NavigationTarget], TargetFocusResult] | None = None,
    ) -> "WorkspaceMount":
        return cls(
            widget=widget,
            on_activate=on_activate,
            on_deactivate=on_deactivate,
            before_deactivate=before_deactivate,
            dirty_state=dirty_state,
            resolve_dirty_state=resolve_dirty_state,
            on_context_changed=on_context_changed,
            on_entity_requested=on_entity_requested,
            on_close=widget.close,
            focus_kinds=frozenset(focus_kinds),
            focus_target=focus_target,
        )


WorkspaceFactory: TypeAlias = Callable[[], WorkspaceMount]


@dataclass(frozen=True, slots=True)
class WorkspaceRegistration:
    """One navigable destination; scope says which IA domain owns it."""

    workspace_id: DestinationId
    label: str
    factory: WorkspaceFactory
    contexts: tuple[WorkspaceContext, ...] = ()
    scope: NavigationScope = NavigationScope.PROJECT
    focus_kinds: frozenset[NavigationTargetKind] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope", destination_scope(self.workspace_id)
        )


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
        if not PROJECT_WORKSPACE_IDS.issubset(self._registrations):
            missing = ", ".join(
                sorted(item.value for item in PROJECT_WORKSPACE_IDS - set(self._registrations))
            )
            raise ValueError(
                f"workflow router requires the four canonical project workspaces; missing: {missing}"
            )
        self._mounts: dict[DestinationId, WorkspaceMount] = {}
        self._current_workspace_id: DestinationId | None = None
        self._navigating = False
        self.last_block_reason: str | None = None

    @property
    def current_workspace_id(self) -> DestinationId | None:
        return self._current_workspace_id

    def registration(self, workspace_id: DestinationId | str) -> WorkspaceRegistration:
        return self._registrations[normalize_destination_id(workspace_id)]

    def registered_ids(self) -> set[DestinationId]:
        return set(self._registrations)

    def focus_capabilities(self) -> dict[DestinationId, frozenset[NavigationTargetKind]]:
        """Target kinds each destination declares it can focus.

        A mounted workspace's own declaration wins over the registration's —
        capabilities follow the concrete surface, not its registration label.
        """
        result: dict[DestinationId, frozenset[NavigationTargetKind]] = {}
        for destination, registration in self._registrations.items():
            mount = self._mounts.get(destination)
            if mount is not None and mount.focus_kinds:
                result[destination] = mount.focus_kinds
            else:
                result[destination] = registration.focus_kinds
        return result

    def mount(self, workspace_id: DestinationId | str) -> WorkspaceMount | None:
        return self._mounts.get(normalize_destination_id(workspace_id))

    def navigate(self, workspace_id: DestinationId | str) -> WorkspaceMount | None:
        destination = normalize_destination_id(workspace_id)
        self.last_block_reason = None
        if destination == self._current_workspace_id:
            return self._ensure_mount(destination)
        if self._navigating:
            # A reentrant call — a queued signal or forwarded intent running
            # inside this transition's dirty-state resolution modal — is
            # refused: the in-flight navigation owns the router until it
            # settles, so mounts can never be switched mid-resolution.
            self.last_block_reason = "画面を切り替え中です"
            return None
        self._navigating = True
        try:
            current = self._mounts.get(self._current_workspace_id) if self._current_workspace_id is not None else None
            if current is not None and current.before_deactivate is not None:
                allowed, reason = current.before_deactivate()
                if not allowed:
                    # #610/#678: offer the operator an explicit Save/Discard/
                    # Recover-Draft decision before hard-blocking the switch.
                    allowed, reason = self._resolve_or_keep(
                        current, "navigate", reason
                    )
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
        finally:
            self._navigating = False

    def select_context(self, workspace_id: DestinationId | str, context_id: str) -> str:
        destination = normalize_destination_id(workspace_id)
        registration = self._registrations[destination]
        normalized_context = (
            normalize_workspace_context(destination, context_id)
            if isinstance(destination, WorkspaceId)
            else context_id
        )
        valid_ids = {context.context_id for context in registration.contexts}
        if normalized_context not in valid_ids:
            raise ValueError(
                f"unknown context {context_id!r} for destination {destination.value!r}"
            )
        mount = self._ensure_mount(destination)
        if mount.on_context_changed is not None:
            mount.on_context_changed(normalized_context)
        return normalized_context

    def request_entity(self, workspace_id: DestinationId | str, entity_id: str) -> None:
        mount = self._ensure_mount(normalize_destination_id(workspace_id))
        if mount.on_entity_requested is not None:
            mount.on_entity_requested(entity_id)

    def focus_target(
        self, destination: DestinationId | str, target: NavigationTarget
    ) -> TargetFocusResult:
        """Route a typed target through the mount's focus port (or entity hook)."""
        mount = self._ensure_mount(normalize_destination_id(destination))
        if mount.focus_target is not None:
            return mount.focus_target(target)
        if (
            mount.on_entity_requested is not None
            and target.kind == NavigationTargetKind.SCENE_ENTITY
            and target.primary_id is not None
        ):
            mount.on_entity_requested(target.primary_id)
            return TargetFocusResult(focused=True)
        return TargetFocusResult(
            focused=False,
            message=(
                f"{navigation_kind_label(target.kind)}"
                "の個別フォーカスはこの画面では未対応です"
            ),
        )

    def can_dispose_all(self) -> tuple[bool, str | None]:
        for workspace_id, mount in self._mounts.items():
            if mount.before_deactivate is None:
                continue
            allowed, reason = mount.before_deactivate()
            if not allowed:
                label = self._registrations[workspace_id].label
                return False, reason or f"{label}の作業を完了してから復元してください"
        return True, None

    def mounts(self) -> tuple[tuple[DestinationId, WorkspaceMount], ...]:
        """All mounted workspaces (project switch / exit resolution scans them)."""
        return tuple(self._mounts.items())

    def resolve_dispose_all(
        self,
        context: DeactivationContext,
    ) -> tuple[bool, str | None]:
        """``can_dispose_all`` with explicit dirty-state resolution (#610).

        Each blocked mount gets the operator's explicit choice (save, discard,
        keep as draft, recover, cancel). Only mounts that resolve cleanly
        allow the dispose to proceed.
        """
        for workspace_id, mount in self._mounts.items():
            if mount.before_deactivate is None:
                continue
            allowed, reason = mount.before_deactivate()
            if not allowed:
                allowed, reason = self._resolve_or_keep(mount, context, reason)
            if not allowed:
                label = self._registrations[workspace_id].label
                return False, reason or f"{label}の作業を完了してから復元してください"
        return True, None

    def _resolve_or_keep(
        self,
        mount: WorkspaceMount,
        context: DeactivationContext,
        reason: str | None,
    ) -> tuple[bool, str | None]:
        from .dirty_state_dialog import resolve_mount_dirty_state

        if resolve_mount_dirty_state(mount, context, parent=self):
            if mount.before_deactivate is None:
                return True, None
            return mount.before_deactivate()
        return False, reason

    def dispose_mounts(self) -> None:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        from shiboken6 import isValid

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
        # Mount-owned popups (plot context menus, ViewBoxMenu, tooltip
        # frames) are not QObject children of the mount tree, so the
        # mounts' destruction never reaches them: ~26 hidden unparented
        # Qt.Popup top-levels accumulated per heavy-mount dispose. Queue
        # their deletes BEFORE the mounts': a popup's ~ walks its logical
        # owner (a ViewBox, an action group) even though it is unparented,
        # so it must be delivered while the owning mount is still alive —
        # FIFO posted-event order makes the drain destroy popups first.
        # Each receiver's pending queue is also drained: a queued
        # DeferredDelete reposts behind each still-pending event, which is
        # how strays survived single flushes and died inside whichever
        # code path next pumped events — mid-switch or mid-test in a
        # foreign context. Application-level top-levels (dialogs, the
        # command palette) are parented and unaffected.
        app = QApplication.instance()
        if app is not None:
            for widget in app.topLevelWidgets():
                if not isValid(widget):
                    # Died as a child of an already-deleted popup.
                    continue
                if (
                    widget.parentWidget() is None
                    and widget.windowType() == Qt.WindowType.Popup
                ):
                    widget.deleteLater()
        for mount in mounts:
            mount.widget.deleteLater()
        # Each doomed receiver's delete is delivered by a DeferredDelete-
        # TYPED send: on this Qt build neither processEvents nor an
        # unfiltered sendPostedEvents dispatches DeferredDelete events —
        # a queued delete otherwise sits undelivered until some unrelated
        # typed flush runs it in a foreign context, mid-switch or
        # mid-test — the xdist worker-crash class (verified empirically).
        # Qt posts a DeferredDelete only on the first deleteLater()
        # (deleteLaterCalled stays set), so removing that posted event
        # would make every later deleteLater a silent no-op and the
        # receiver immortal. The doomed set is restricted to objects with
        # no live QObject parent — parented top-levels (combobox dropdown
        # containers, submenus) are deleted by their owner through a raw
        # pointer the child's ~ never clears, so an early standalone ~
        # double-frees them the moment the owner dies. Ordering is
        # popups, then owners (widgets that still own children — their ~
        # walks satellite top-levels held by raw pointer, which must be
        # alive), then leaf windows (childless — their ~ is self-
        # contained and safe on dead collaborators). The global sweep
        # then covers deletes posted during the cascade (finished worker
        # threads, timers, freed children).
        doomed = (
            [
                w
                for w in app.topLevelWidgets()
                if isValid(w)
                and (w.parent() is None or not isValid(w.parent()))
            ]
            if app is not None
            else []
        )
        doomed.sort(
            key=lambda w: (
                w.windowType() != Qt.WindowType.Popup,
                not w.children(),
            )
        )
        for _ in range(4):
            for widget in doomed:
                if isValid(widget):
                    app.sendPostedEvents(widget)
            for widget in doomed:
                if isValid(widget):
                    app.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(
                None, QEvent.Type.DeferredDelete
            )
            QCoreApplication.processEvents()

    def shutdown(self) -> None:
        self.dispose_mounts()

    def _ensure_mount(self, workspace_id: DestinationId) -> WorkspaceMount:
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
        # Caption QLabel siblings created without a buddy leave their
        # control's accessible name empty; wire them once per mount so
        # every lazily built panel names its inputs to screen readers.
        wire_label_buddies(mount.widget)
        return mount


class WorkflowRail(QFrame):
    EXPANDED_WIDTH = 184
    # Compact is a real mode (#782/#788): 72 px icon-led rail where each
    # destination shows a single glyph and the full label moves to a tooltip —
    # not just a narrower text rail.
    COMPACT_WIDTH = 72

    def __init__(
        self,
        registrations: Iterable[WorkspaceRegistration],
        on_navigate: Callable[[DestinationId], bool],
        on_settings: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("workflowRail")
        set_surface_role(self, SurfaceRole.RAISED)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._compact = False

        self._buttons: dict[DestinationId, QPushButton] = {}
        self._labels: dict[DestinationId, str] = {}
        self._section_headers: list[QLabel] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(12, 16, 12, 16)
        self._outer_layout.setSpacing(6)

        self._brand = QLabel("HTDT")
        set_typography_role(self._brand, TypographyRole.WORKSPACE_TITLE)
        self._outer_layout.addWidget(self._brand)
        self._outer_layout.addSpacing(12)

        # The destination column scrolls instead of clipping when the rail is
        # shorter than its content (constrained heights / 200% DPI, #788).
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("workflowRailScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll_host = QWidget()
        set_surface_role(scroll_host, SurfaceRole.RAISED)
        self._layout = QVBoxLayout(scroll_host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        last_scope: NavigationScope | None = None
        for registration in registrations:
            if registration.scope != last_scope:
                header = QLabel(
                    "プロジェクト"
                    if registration.scope == NavigationScope.PROJECT
                    else "アプリケーション"
                )
                header.setObjectName("workflowRailSectionHeader")
                set_typography_role(header, TypographyRole.SECONDARY)
                self._layout.addWidget(header)
                self._section_headers.append(header)
                last_scope = registration.scope
            button = QPushButton(registration.label)
            button.setCheckable(True)
            # Compact mode truncates the text to one glyph; the accessible
            # name keeps the full destination label.
            button.setAccessibleName(registration.label)
            button.setProperty("workspaceId", registration.workspace_id.value)
            set_control_size(button, ControlSize.STANDARD)
            button.clicked.connect(
                lambda checked=False, workspace_id=registration.workspace_id: on_navigate(workspace_id)
            )
            self._group.addButton(button)
            self._buttons[registration.workspace_id] = button
            self._labels[registration.workspace_id] = registration.label
            self._layout.addWidget(button)

        self._layout.addStretch(1)
        self._scroll.setWidget(scroll_host)
        self._outer_layout.addWidget(self._scroll, 1)

        self.settings_button = QPushButton("設定")
        self.settings_button.setAccessibleName("設定")
        self.settings_button.setObjectName("workflowSettingsButton")
        set_control_size(self.settings_button, ControlSize.STANDARD)
        if on_settings is not None:
            self.settings_button.clicked.connect(lambda checked=False: on_settings())
        self._outer_layout.addWidget(self.settings_button)

    @property
    def labels(self) -> tuple[str, ...]:
        """Full destination labels — compact mode swaps glyphs, not labels."""
        return tuple(self._labels.values())

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
        for header in self._section_headers:
            header.setVisible(not compact)
        for workspace_id, button in self._buttons.items():
            label = self._labels[workspace_id]
            button.setText(label[:1] if compact else label)
            button.setToolTip(label if compact else "")
        self.settings_button.setText("設" if compact else "設定")
        self.settings_button.setToolTip("設定" if compact else "")
        margin_x = 6 if compact else 12
        margin_y = 10 if compact else 16
        self._outer_layout.setContentsMargins(margin_x, margin_y, margin_x, margin_y)
        self._outer_layout.setSpacing(4 if compact else 6)
        self._layout.setSpacing(4 if compact else 6)

    def set_active(self, workspace_id: DestinationId | str) -> None:
        button = self._buttons.get(normalize_destination_id(workspace_id))
        if button is not None:
            button.setChecked(True)


class TopContextBar(QFrame):
    def __init__(
        self,
        on_context_selected: Callable[[str], None],
        on_palette: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
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
        # Keep the buttons at their natural width inside a horizontal scroll
        # area (#1087): the container fills the bar when it fits and the
        # overflow scrolls instead of compressing buttons into slivers.
        self._context_scroll = QScrollArea(self)
        self._context_scroll.setObjectName("workflowContextScroll")
        self._context_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._context_scroll.setWidgetResizable(True)
        self._context_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._context_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._context_scroll.setWidget(self._context_container)
        self._layout.addWidget(self._context_scroll, 1)

        self._project_label = QLabel()
        self._project_label.setObjectName("workflowProjectChip")
        set_typography_role(self._project_label, TypographyRole.SECONDARY)
        self._layout.addWidget(self._project_label)

        self._project_button = QPushButton("切替")
        self._project_button.setObjectName("workflowProjectSwitchButton")
        set_control_size(self._project_button, ControlSize.COMPACT)
        self._project_button.setToolTip("プロジェクトを切り替える")
        self._layout.addWidget(self._project_button)

        # Visible entry point into the command palette: Ctrl+K alone is not
        # discoverable, and the palette is the app's primary action surface.
        self._palette_button = QPushButton("検索・操作")
        self._palette_button.setObjectName("workflowPaletteButton")
        set_control_size(self._palette_button, ControlSize.COMPACT)
        self._palette_button.setToolTip(
            "機能・画面・項目を検索 (Ctrl+K)"
        )
        self._palette_button.setEnabled(on_palette is not None)
        if on_palette is not None:
            self._palette_button.clicked.connect(lambda checked=False: on_palette())
        self._layout.addWidget(self._palette_button)

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
        self._update_context_min_width()

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
        self._update_context_min_width()

    def set_active_context(self, context_id: str) -> None:
        button = self._context_buttons.get(context_id)
        if button is None:
            raise ValueError(f"context {context_id!r} is not visible in the current workspace")
        button.setChecked(True)

    def set_project_identity(
        self,
        label: str | None,
        on_switch: Callable[[], None] | None = None,
    ) -> None:
        """Show the active project name; the button switches via the library."""
        visible = bool(label)
        self._project_label.setVisible(visible)
        self._project_label.setText(f"プロジェクト: {label}" if label else "")
        if on_switch is not None:
            if self._project_button.receivers("clicked()"):
                self._project_button.clicked.disconnect()
            self._project_button.clicked.connect(
                lambda checked=False: on_switch()
            )
            self._project_button.setEnabled(True)
        else:
            self._project_button.setEnabled(False)
        self._project_button.setVisible(visible)

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
        self._context_container.setMinimumWidth(0)

    def _update_context_min_width(self) -> None:
        # minimumWidth pins the container at its natural width so the scroll
        # area shows a horizontal scrollbar only when the buttons overflow.
        self._context_container.setMinimumWidth(
            self._context_layout.sizeHint().width()
        )


class WorkflowShellWindow(QMainWindow):
    """Workflow-first shell; domain and SceneRevision authority stay in workspaces."""

    settingsRequested = Signal()
    projectSwitchRequested = Signal()
    paletteRequested = Signal()
    helpRequested = Signal()

    def __init__(
        self,
        registrations: Iterable[WorkspaceRegistration],
        *,
        initial_workspace: DestinationId = WorkspaceId.OVERVIEW,
    ) -> None:
        super().__init__()
        self.setObjectName("workflowShell")
        self.setWindowTitle("Home Theater Digital Twin")

        registration_tuple = tuple(registrations)
        self._registrations = {registration.workspace_id: registration for registration in registration_tuple}
        if not PROJECT_WORKSPACE_IDS.issubset(self._registrations):
            missing = ", ".join(
                sorted(item.value for item in PROJECT_WORKSPACE_IDS - set(self._registrations))
            )
            raise ValueError(
                f"workflow shell requires the four canonical project workspaces; missing: {missing}"
            )

        self._navigation_history = NavigationHistory()
        self._navigation_resolver = NavigationResolver()
        self._selected_context: dict[DestinationId, str] = {}
        self._close_guards: list[CloseGuard] = []
        # Hooks fire once a close has passed every guard and the exit-time
        # dirty-state resolution — the point where the close is committed.
        self._close_hooks: list[Callable[[], None]] = []
        # The deactivation context closeEvent feeds to dirty-state
        # resolution. A project switch reuses close() to tear this shell
        # down, but "アプリケーションの終了" prompts would mislabel that
        # flow — the switcher sets 'project_switch' for its close attempt
        # and restores 'exit' afterwards (#REV18).
        self._deactivation_context: DeactivationContext = 'exit'
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

        self.context_bar = TopContextBar(
            self._select_current_context,
            on_palette=self.paletteRequested.emit,
        )
        content_layout.addWidget(self.context_bar)

        self.router = WorkspaceRouter(registration_tuple)
        content_layout.addWidget(self.router, 1)

        root_layout.addWidget(content, 1)
        self.setCentralWidget(root)
        self.resize(1440, 900)
        self._update_responsive_layout()
        # Application-level Back/Forward history (typed navigation context);
        # intentionally independent of Scene Undo inside any workspace.
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.navigation_back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self.navigation_forward)
        QShortcut(QKeySequence("F1"), self, activated=self.helpRequested.emit)
        self.context_bar.set_project_identity(None, None)
        # Name the shell's own unlabeled inputs (rail scroll surface,
        # context bar) through the same caption-buddy pass the router
        # applies to every mount.
        wire_label_buddies(self)
        # Every showMessage() call site becomes a live-region
        # announcement via the status bar's messageChanged signal.
        wire_status_announcements(self)
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
    def current_workspace_id(self) -> DestinationId:
        current = self.router.current_workspace_id
        if current is None:
            raise RuntimeError("workflow shell has not selected a workspace")
        return current

    @property
    def navigation_history(self) -> NavigationHistory:
        return self._navigation_history

    @property
    def navigation_labels(self) -> tuple[str, ...]:
        return self.rail.labels

    @property
    def data_mutations_frozen(self) -> bool:
        return self._data_mutations_frozen

    @property
    def context_labels(self) -> tuple[str, ...]:
        return self.context_bar.context_labels

    def set_project_identity(self, label: str | None) -> None:
        """Show the active project in the context bar with a switch affordance."""
        self.context_bar.set_project_identity(
            label,
            self.projectSwitchRequested.emit if label else None,
        )

    def navigate(self, workspace_id: DestinationId | str) -> bool:
        if self._data_mutations_frozen:
            self.statusBar().showMessage("データ処理中は画面を切り替えられません")
            return False
        destination = normalize_destination_id(workspace_id)
        if destination not in self._registrations:
            self.statusBar().showMessage(
                f"未登録の画面です: {destination_label(destination)}"
            )
            return False
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

    def navigate_to_target(
        self,
        target: NavigationTarget,
        *,
        referrer: str | None = None,
        record_history: bool = True,
    ) -> NavigationResolution:
        """Resolve a typed target, navigate, then focus through the port.

        The resolved link keeps the target's revision/variant authority
        context; unsupported kinds surface an actionable message instead of
        any name/newest substitution.
        """
        target = self._establish_target_project(target)
        if isinstance(target, NavigationResolution):
            self.statusBar().showMessage(target.message or "対象を開けません")
            return target
        resolution = self._navigation_resolver.resolve(
            target,
            registered=self.router.registered_ids(),
            capabilities=self.router.focus_capabilities(),
        )
        if resolution.link is None:
            self.statusBar().showMessage(resolution.message or "対象を開けません")
            return resolution

        link = resolution.link
        if not self.navigate(link.workspace):
            return NavigationResolution(
                target=target,
                link=link,
                status="unsupported",
                message=self.router.last_block_reason or "画面を切り替えられません",
            )
        if link.section is not None and link.workspace in PROJECT_WORKSPACE_IDS:
            try:
                self.select_context(link.section)
            except ValueError:
                # A stale or foreign link must not crash the navigation —
                # land on the workspace and say which section was missing.
                self.statusBar().showMessage(
                    f"対象のセクション {workspace_context_label(link.workspace, link.section)} "
                    "はこの画面にありません"
                )

        if resolution.status == "focused":
            result = self.router.focus_target(link.workspace, target)
            if result.message:
                self.statusBar().showMessage(result.message)
        elif link.entity_id is not None:
            self.router.request_entity(link.workspace, link.entity_id)

        if record_history:
            self._navigation_history.record(target, link, referrer=referrer)
        return resolution

    def _establish_target_project(
        self, target: NavigationTarget
    ) -> NavigationTarget | NavigationResolution:
        """Resolve ``target.project_id`` to the active project before routing.

        Project-scoped targets with no explicit identity are stamped with
        the current canonical project id so recorded history replays against
        the same authority context. An explicit ``project_id`` goes through
        the application-level guarded project switch; on failure the target
        is never routed into the wrong project and no history entry is
        created.
        """
        if target.scope is not NavigationScope.PROJECT:
            return target
        application = getattr(self, "workflow_application", None)
        project_id = target.project_id
        if project_id is None:
            if application is None:
                return target
            project_id = application.navigation_project_identity()
            return replace(target, project_id=project_id)
        if application is None:
            return NavigationResolution(
                target=target,
                link=None,
                status="unsupported",
                message="プロジェクト切替機能が利用できないため対象を開けません",
            )
        established, message = application.establish_navigation_project(project_id)
        if not established:
            return NavigationResolution(
                target=target,
                link=None,
                status="unsupported",
                message=message or "対象のプロジェクトを開けません",
            )
        return target

    def navigation_back(self) -> bool:
        """Application Back: replay the previous typed navigation context."""
        entry = self._navigation_history.back()
        if entry is None:
            return False
        resolution = self.navigate_to_target(entry.target, record_history=False)
        if not resolution.ok:
            self._navigation_history.forward()
            return False
        return True

    def navigation_forward(self) -> bool:
        entry = self._navigation_history.forward()
        if entry is None:
            return False
        resolution = self.navigate_to_target(entry.target, record_history=False)
        if not resolution.ok:
            self._navigation_history.back()
            return False
        return True

    def handle_deep_link(self, target: WorkspaceDeepLink) -> bool:
        """Legacy workspace deep links route through typed navigation."""
        kind = target.kind or (
            NavigationTargetKind.SCENE_ENTITY.value
            if target.entity_id is not None
            else NavigationTargetKind.WORKSPACE.value
        )
        resolution = self.navigate_to_target(
            NavigationTarget(
                kind=NavigationTargetKind(kind),
                object_ids=(target.entity_id,) if target.entity_id else (),
                revision_id=target.revision_id,
                system_variant_id=target.system_variant_id,
                preferred_destination=target.workspace,
                preferred_section=target.section,
                intent=(
                    NavigationIntent(target.intent)
                    if target.intent is not None
                    else NavigationIntent.INSPECT
                ),
            )
        )
        return resolution.ok

    def _select_current_context(self, context_id: str) -> None:
        if self._data_mutations_frozen:
            return
        workspace_id = self.current_workspace_id
        normalized_context = self.router.select_context(workspace_id, context_id)
        self._selected_context[workspace_id] = normalized_context
        self.context_bar.set_active_context(normalized_context)

    def register_close_guard(self, guard: CloseGuard) -> None:
        self._close_guards.append(guard)

    def register_close_hook(self, hook: Callable[[], None]) -> None:
        """Register a callback run after close checks pass, before teardown.

        Unlike a close guard a hook cannot veto; it is for cheap committed-
        close work like persisting window state. Hook failures are logged
        and never abort the close.
        """
        self._close_hooks.append(hook)

    def selected_contexts(self) -> dict[str, str]:
        """Current per-workspace context selections (for persistence)."""
        return {
            str(workspace_id): context_id
            for workspace_id, context_id in self._selected_context.items()
        }

    def reset_selected_contexts(self) -> None:
        """Reset context selections to each registration's default.

        The live map is process state, not project state — a project
        switch reseeds it from the target project's persisted record via
        ``seed_selected_contexts``, so selections made under the outgoing
        project must not linger through the merge-only seed.
        """
        self._selected_context = {
            registration.workspace_id: registration.contexts[0].context_id
            for registration in self._registrations.values()
            if registration.contexts
        }

    def seed_selected_contexts(self, contexts: Mapping[str, str]) -> None:
        """Pre-seed context selections for persistence restore.

        Unknown workspaces and contexts the registration does not declare
        are dropped rather than replayed into a context bar that cannot
        select them.
        """
        for workspace, context_id in contexts.items():
            try:
                destination = normalize_destination_id(workspace)
            except ValueError:
                continue
            registration = self._registrations.get(destination)
            if registration is None or not registration.contexts:
                continue
            known = {ctx.context_id for ctx in registration.contexts}
            if context_id in known:
                self._selected_context[destination] = context_id

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
        allowed, reason = self.router.resolve_dispose_all("dispose")
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
        # #610: app exit gets the same explicit resolution as navigation,
        # checked across every mounted workspace, not only the current one.
        allowed, reason = self.router.resolve_dispose_all(
            self._deactivation_context
        )
        if not allowed:
            self.statusBar().showMessage(reason or "現在の作業を完了してから終了してください")
            event.ignore()
            return
        for hook in self._close_hooks:
            try:
                hook()
            except Exception:
                _LOGGER.exception("close hook failed")
        self.router.shutdown()
        super().closeEvent(event)


__all__ = [
    "WorkflowShellWindow",
    "WorkspaceFactory",
    "WorkspaceMount",
    "WorkspaceRegistration",
    "build_canonical_workspace_registrations",
]
