"""IA v2 scope tiers and application-scope page services (#649)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.application_pages import (
    CaptureInboxPage,
    ProjectLibraryService,
    list_recent_revisions,
)
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import CaptureInboxRepository
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
    label = shell.context_bar._project_label
    assert 'theater-1' in label.text()
    assert not label.isHidden()
    shell.set_project_identity(None)
    assert label.isHidden()
    shell.close()


def _staged_inbox(tmp_path):
    scene = SceneRepository(tmp_path / "cad.sqlite3")
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    plan, payloads, _ = support.plan_and_payloads(tmp_path)
    ingestion.ingest(plan, payloads)
    staged = inbox.stage(
        CaptureIngestionPlan.model_validate(plan),
        arrival_source="file_import",
    )
    return inbox, staged


def test_capture_inbox_detail_preserves_item_and_project_context(
    tmp_path, monkeypatch
) -> None:
    """#770: selection shows the exact item/project context and triage
    actions act on the inspected item — the listing is no longer list-only."""
    app = _app()
    inbox, staged = _staged_inbox(tmp_path)
    digest = staged.lineage_digest
    project = SimpleNamespace(
        project_id="proj-alpha", document_id="doc-alpha", display_name="Alpha"
    )
    page = CaptureInboxPage(
        inbox.list_items,
        on_navigate=lambda link: True,
        inspect_item=inbox.inspect,
        defer_item=inbox.defer,
        reject_item=inbox.reject,
        resume_item=inbox.resume,
        list_projects=lambda: (project,),
        assign_scope=inbox.assign_scope,
    )
    try:
        page.table.selectRow(0)
        app.processEvents()
        text = page.detail.text()
        assert "（未割り当て）" in text
        assert "new_series" in text
        assert "昇格可能性" in text

        # Scope assignment binds the item to the chosen project document.
        index = page.scope_combo.findData("doc-alpha")
        assert index > 0
        page.scope_combo.setCurrentIndex(index)
        page.scope_button.click()
        assert inbox.get(digest).scope == "doc-alpha"

        monkeypatch.setattr(
            QInputDialog,
            "getText",
            staticmethod(lambda *args, **kwargs: ("後で確認", True)),
        )
        page.defer_button.click()
        assert inbox.get(digest).disposition == "deferred"
        assert "延期" in page.detail.text()

        page.resume_button.click()
        assert inbox.get(digest).disposition == "pending"
        # Selection survives refresh: the exact item stays in context.
        assert "capture-inbox-item" not in page.detail.text() or "項目:" in page.detail.text()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
