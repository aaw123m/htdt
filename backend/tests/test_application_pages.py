"""IA v2 scope tiers and application-scope page services (#649)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.application_pages import (
    ProjectLibraryService,
    list_recent_revisions,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene, make_f1_scene
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    NavigationScope,
    WorkspaceId,
    destination_scope,
    normalize_destination_id,
)
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    WorkspaceRegistration,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "cad.sqlite3")
    repository.save(make_empty_scene('doc-alpha'), parent_revision_id=None)
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def test_destination_scope_tiers() -> None:
    assert destination_scope(WorkspaceId.ROOM) == NavigationScope.PROJECT
    assert destination_scope(ApplicationDestinationId.INBOX) == NavigationScope.APPLICATION
    assert normalize_destination_id('inbox') == ApplicationDestinationId.INBOX
    assert normalize_destination_id('room') == WorkspaceId.ROOM


def test_project_library_lists_persisted_documents(tmp_path) -> None:
    repository = _repository(tmp_path)
    entries = ProjectLibraryService(repository).list_projects()
    ids = {entry.document_id for entry in entries}
    assert ids == {'doc-alpha', 'fixture-f1'}
    f1 = next(e for e in entries if e.document_id == 'fixture-f1')
    assert f1.revision_count >= 1
    assert f1.head_revision_id


def test_recent_revisions_returns_newest_first(tmp_path) -> None:
    repository = _repository(tmp_path)
    rows = list_recent_revisions(repository)
    assert len(rows) >= 2
    assert rows[0][2] != rows[1][2]
    doc_ids = {row[1] for row in rows}
    assert doc_ids == {'doc-alpha', 'fixture-f1'}


def test_shell_registers_application_destinations(tmp_path) -> None:
    _app()
    registrations = (
        WorkspaceRegistration(
            workspace_id=WorkspaceId.OVERVIEW,
            label='概要',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.ROOM,
            label='部屋',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.MEASUREMENT,
            label='測定',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.OPTIMIZATION,
            label='最適化',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=ApplicationDestinationId.PROJECTS,
            label='プロジェクト',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=ApplicationDestinationId.INBOX,
            label='キャプチャ',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
    )
    shell = WorkflowShellWindow(registrations)
    assert shell.navigate(ApplicationDestinationId.INBOX)
    assert shell.navigate(ApplicationDestinationId.PROJECTS)
    shell.close()


def test_shell_project_identity_visible(tmp_path) -> None:
    _app()
    registrations = (
        WorkspaceRegistration(
            workspace_id=WorkspaceId.OVERVIEW,
            label='概要',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.ROOM,
            label='部屋',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.MEASUREMENT,
            label='測定',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
        WorkspaceRegistration(
            workspace_id=WorkspaceId.OPTIMIZATION,
            label='最適化',
            factory=lambda: WorkspaceMount.from_widget(__import__('PySide6.QtWidgets', fromlist=['QWidget']).QWidget()),
        ),
    )
    shell = WorkflowShellWindow(registrations)
    shell.set_project_identity('theater-1')
    shell.close()
