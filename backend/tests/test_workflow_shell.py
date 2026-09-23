from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtWidgets import QApplication, QLabel

from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _registrations(factory):
    return build_canonical_workspace_registrations(
        {workspace_id: factory(workspace_id) for workspace_id in WorkspaceId}
    )


def test_workflow_shell_routes_canonical_workspaces_lazily() -> None:
    app = _app()
    created: list[WorkspaceId] = []
    context_events: dict[WorkspaceId, list[str]] = {workspace_id: [] for workspace_id in WorkspaceId}

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            created.append(workspace_id)
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                on_context_changed=context_events[workspace_id].append,
            )

        return build

    window = WorkflowShellWindow(_registrations(factory))
    app.processEvents()

    assert window.navigation_labels == ("概要", "部屋", "測定", "最適化")
    assert window.current_workspace_id is WorkspaceId.OVERVIEW
    assert created == [WorkspaceId.OVERVIEW]

    assert window.navigate(WorkspaceId.ROOM)
    assert window.context_labels == ("形状", "物体", "スピーカー・座席", "音響", "履歴")
    assert context_events[WorkspaceId.ROOM] == ["geometry"]

    window.select_context("acoustics")
    assert context_events[WorkspaceId.ROOM] == ["geometry", "acoustics"]

    assert window.handle_deep_link(WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "quality"))
    assert window.current_workspace_id is WorkspaceId.MEASUREMENT
    assert context_events[WorkspaceId.MEASUREMENT][-1] == "quality"

    assert window.navigate(WorkspaceId.OPTIMIZATION)
    assert window.context_labels == ("探索設定", "候補", "比較", "ばらつき耐性", "測定・検証")
    assert context_events[WorkspaceId.OPTIMIZATION][-1] == "setup"

    assert window.handle_deep_link(
        WorkspaceDeepLink(WorkspaceId.OPTIMIZATION, "objectives")
    )
    assert context_events[WorkspaceId.OPTIMIZATION][-1] == "comparison"

    window.close()
    window.deleteLater()
    app.processEvents()


def test_navigation_fails_closed_when_current_workspace_refuses_deactivation() -> None:
    app = _app()
    blocked = {"value": False}

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            guard = None
            if workspace_id is WorkspaceId.ROOM:
                guard = lambda: (
                    (False, "未保存の変更があります")
                    if blocked["value"]
                    else (True, None)
                )
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=guard,
            )

        return build

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.ROOM)
    blocked["value"] = True

    assert window.navigate(WorkspaceId.MEASUREMENT) is False
    assert window.current_workspace_id is WorkspaceId.ROOM
    assert "未保存" in window.statusBar().currentMessage()

    blocked["value"] = False
    assert window.navigate(WorkspaceId.MEASUREMENT)
    window.close()
    window.deleteLater()
    app.processEvents()


def test_entity_deep_link_is_forwarded_to_workspace_callback() -> None:
    app = _app()
    selected: list[str] = []

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                on_entity_requested=selected.append,
            )

        return build

    window = WorkflowShellWindow(_registrations(factory))
    target = WorkspaceDeepLink(
        WorkspaceId.ROOM,
        "placement",
        entity_id="speaker-1",
    )
    assert window.handle_deep_link(target)
    assert selected == ["speaker-1"]

    window.close()
    window.deleteLater()
    app.processEvents()


def test_settings_utility_emits_without_becoming_a_workspace() -> None:
    app = _app()

    def factory(workspace_id: WorkspaceId):
        return lambda: WorkspaceMount.from_widget(QLabel(workspace_id.value))

    window = WorkflowShellWindow(_registrations(factory))
    events: list[str] = []
    window.settingsRequested.connect(lambda: events.append("settings"))

    window.rail.settings_button.click()
    app.processEvents()

    assert events == ["settings"]
    assert window.current_workspace_id is WorkspaceId.OVERVIEW
    assert window.navigation_labels == ("概要", "部屋", "測定", "最適化")
    window.close()
    window.deleteLater()
    app.processEvents()


def test_restore_release_checks_hidden_mounted_workspaces() -> None:
    app = _app()
    blocked = {"room": False}

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            guard = None
            if workspace_id is WorkspaceId.ROOM:
                guard = lambda: (
                    (False, "部屋の処理が実行中です")
                    if blocked["room"]
                    else (True, None)
                )
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=guard,
            )

        return build

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.ROOM)
    assert window.navigate(WorkspaceId.MEASUREMENT)
    blocked["room"] = True

    with pytest.raises(RuntimeError, match="部屋の処理"):
        window.dispose_data_workspaces()

    assert window.router.mount(WorkspaceId.ROOM) is not None
    blocked["room"] = False
    window.dispose_data_workspaces()
    assert window.router.current_workspace_id is None
    assert window.router.mount(WorkspaceId.ROOM) is None

    window.deleteLater()
    app.processEvents()



def test_workflow_shell_layout_profiles_do_not_clip_context_navigation() -> None:
    app = _app()

    def factory(workspace_id: WorkspaceId):
        return lambda: WorkspaceMount.from_widget(QLabel(workspace_id.value))

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.OPTIMIZATION)
    window.show()
    app.processEvents()

    # Windows scaling reduces the logical client area available at a fixed
    # physical display resolution. Exercise the UX150 acceptance matrix without
    # depending on a particular CI host DPI.
    profiles = (
        (1280, 800, 1.00),
        (1440, 900, 1.00),
        (1280, 800, 1.50),
        (1440, 900, 1.50),
        (1280, 800, 2.00),
        (1440, 900, 2.00),
    )
    for physical_width, physical_height, scale in profiles:
        logical_width = round(physical_width / scale)
        logical_height = round(physical_height / scale)
        window.resize(logical_width, logical_height)
        app.processEvents()

        compact = logical_width < 1120
        assert window.rail.is_compact is compact
        assert window.context_bar.is_compact is compact
        assert window.rail.width() == (
            window.rail.COMPACT_WIDTH if compact else window.rail.EXPANDED_WIDTH
        )

        buttons = tuple(window.context_bar._context_buttons.values())
        assert len(buttons) == 5
        assert all(button.isVisible() for button in buttons)
        for index, left in enumerate(buttons):
            for right in buttons[index + 1:]:
                assert not left.geometry().intersects(right.geometry())
        assert (
            max(button.geometry().right() for button in buttons)
            < window.context_bar._context_container.width()
        )

    window.close()
    window.deleteLater()
    app.processEvents()
