"""Round-7 end-to-end workflow coherence regression tests
(docs/reviews/round7-workflow.md).

Covers the navigation/handoff wiring fixed this round:

* ``calibration`` is a canonical measurement context (the onboarding page is
  reachable through the context bar, not only from tests),
* activity-timeline deep links land on surfaces that can display the item,
* ``activity_focus`` covers the events and operations tables, not just the
  revision ledger,
* an unknown/stale section in a deep link degrades to a status message
  instead of raising inside ``navigate_to_target``,
* room history rows can be focused by revision id, and
* measurement ``focus_entity`` switches to the page that owns the row.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from htdt.activity_center import ActivityCenter, OperationClass
from htdt.application_pages import ActivityPage, activity_focus
from htdt.cad_project_activity import CadProjectActivityService
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.navigation_target import (
    ApplicationDestinationId,
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from htdt.workflow_navigation import (
    CANONICAL_WORKSPACE_CONTEXTS,
    WorkspaceId,
    normalize_workspace_context,
)
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _measurement_workspace(tmp_path: Path) -> MeasurementPageWorkspace:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    return MeasurementPageWorkspace(controller)


def _activity_service(repository: SceneRepository) -> CadProjectActivityService:
    return CadProjectActivityService(
        scene_repository=repository,
        notes_repository=CadProjectActivityNoteRepository(repository),
    )


# -- measurement calibration context is reachable ---------------------------


def test_calibration_is_a_canonical_measurement_context() -> None:
    context_ids = [
        context.context_id
        for context in CANONICAL_WORKSPACE_CONTEXTS[WorkspaceId.MEASUREMENT]
    ]
    assert 'calibration' in context_ids
    # Deep links must land on the onboarding page itself — the legacy alias
    # that rewrote 'calibration' to 'campaign' made the page unreachable.
    assert (
        normalize_workspace_context(WorkspaceId.MEASUREMENT, 'calibration')
        == 'calibration'
    )


# -- timeline deep links land on a surface that shows the item ---------------


def test_timeline_note_links_point_at_the_activity_page(
    tmp_path: Path,
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = _activity_service(repository)
    note = service.add_note(
        document_id=F1_DOCUMENT_ID,
        title='配置メモ',
        created_at_utc='2026-09-27T00:00:00+00:00',
    )

    note_events = [
        event
        for event in service.events(F1_DOCUMENT_ID)
        if event.kind == 'project_note'
    ]
    assert note_events
    for event in note_events:
        target = navigation_target_from_uri(event.deep_link)
        # The note's only surface is its own timeline row — the link must
        # carry the note id and prefer ACTIVITY so activity_focus can
        # select the row (before the fix these pointed at OVERVIEW, which
        # has no way to display a note).
        assert (
            target.preferred_destination
            == ApplicationDestinationId.ACTIVITY
        )
        assert target.primary_id == note.note_id


# -- activity_focus covers events + operations tables ------------------------


def test_activity_focus_selects_the_timeline_event_row(
    tmp_path: Path,
) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = _activity_service(repository)
    note = service.add_note(
        document_id=F1_DOCUMENT_ID,
        title='配置メモ',
        created_at_utc='2026-09-27T00:00:00+00:00',
    )
    page = ActivityPage(
        lambda limit: (),
        list_events=lambda limit: service.recent(
            F1_DOCUMENT_ID, limit=limit
        ),
    )

    result = activity_focus(
        page,
        NavigationTarget(
            kind=NavigationTargetKind.PROJECT_NOTE,
            object_ids=(note.note_id,),
        ),
    )
    assert result.focused is True
    row = page.events_table.currentRow()
    assert row >= 0
    link = page.events_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
    assert note.note_id in navigation_target_from_uri(link).object_ids


def test_activity_focus_selects_the_operation_row(tmp_path: Path) -> None:
    _app()
    center = ActivityCenter()
    operation_id = center.submit(
        operation_kind='create_backup',
        operation_class=OperationClass.DATA_MANAGEMENT,
        title='バックアップの作成',
    )
    page = ActivityPage(
        lambda limit: (),
        list_operations=lambda: (*center.active(), *center.recent()),
    )

    result = activity_focus(
        page,
        NavigationTarget(
            kind=NavigationTargetKind.ACTIVITY_JOB,
            object_ids=(operation_id,),
        ),
    )
    assert result.focused is True
    row = page.operations_table.currentRow()
    assert row >= 0
    assert (
        page.operations_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        == operation_id
    )


def test_activity_focus_reports_missing_targets(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    service = _activity_service(repository)
    page = ActivityPage(
        lambda limit: (),
        list_events=lambda limit: service.recent(
            F1_DOCUMENT_ID, limit=limit
        ),
    )

    result = activity_focus(
        page,
        NavigationTarget(
            kind=NavigationTargetKind.PROJECT_NOTE,
            object_ids=('no-such-note',),
        ),
    )
    assert result.focused is False
    assert result.message


# -- stale sections degrade to a status message, not an exception ------------


def test_navigate_to_target_ignores_unknown_sections() -> None:
    app = _app()

    def factory(workspace_id: WorkspaceId):
        return lambda: WorkspaceMount.from_widget(
            QLabel(workspace_id.value)
        )

    registrations = build_canonical_workspace_registrations(
        {
            workspace_id: factory(workspace_id)
            for workspace_id in WorkspaceId
        }
    )
    window = WorkflowShellWindow(registrations)
    app.processEvents()

    resolution = window.navigate_to_target(
        NavigationTarget(
            kind=NavigationTargetKind.MEASUREMENT,
            object_ids=('meas-1',),
            preferred_section='no-such-section',
        )
    )
    assert resolution.ok is True
    assert window.current_workspace_id is WorkspaceId.MEASUREMENT

    window.close()
    window.deleteLater()
    app.processEvents()


# -- room history rows are focusable by revision id --------------------------


def test_room_history_panel_select_revision(tmp_path: Path) -> None:
    from test_room_workspace import FakeRoomViewport
    from htdt.room_workspace import RoomWorkspace

    app = _app()
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.set_context('history')
    revisions = repository.list_revisions(F1_DOCUMENT_ID)
    assert revisions
    revision_id = revisions[0].revision_id

    assert workspace.history_panel.select_revision(revision_id) is True
    assert workspace.history_panel.selected_revision_id() == revision_id
    assert workspace.history_panel.select_revision('no-such-rev') is False

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


# -- measurement focus_entity shows the page that owns the row ---------------


def test_measurement_focus_entity_switches_to_quality_page(
    tmp_path: Path,
) -> None:
    _app()
    workspace = _measurement_workspace(tmp_path)
    # Quality data comes from several repositories; stub the rebuilders so
    # the test exercises only the focus/page-switch mechanics.
    workspace._refresh_quality = lambda *_a: None
    workspace._show_quality_row = lambda _index: None
    workspace._update_context_label = lambda: None
    workspace._quality_views = (
        SimpleNamespace(
            measurement_id='meas-1',
            target_entity_id='seat-1',
        ),
    )
    from PySide6.QtWidgets import QTableWidgetItem

    workspace.quality_table.setRowCount(1)
    workspace.quality_table.setItem(0, 0, QTableWidgetItem('x'))

    workspace.set_context('comparison')
    assert workspace.current_context_id == 'comparison'

    workspace.focus_entity('meas-1')
    # The selection only exists on the quality page — the page must switch
    # so the selected row is actually visible.
    assert workspace.current_context_id == 'quality'
    assert workspace.quality_table.currentRow() == 0

    workspace.close()
    workspace.deleteLater()
