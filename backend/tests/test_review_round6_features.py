"""Round-6 feature-gap wiring regression tests (docs/reviews/round6-features.md).

Covers the capabilities wired in this round:

* ``help_registry`` → command-palette provider + topic dialog,
* ``activity_center`` → DataManagementController mirroring + ActivityPage,
* ``cad_project_activity`` → ActivityPage project timeline with deep links,
* pending (unwired) preference keys render disabled in PreferencesWidget.
"""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from htdt.activity_center import (
    ActivityCenter,
    OperationClass,
    OperationState,
)
from htdt.application_pages import ActivityPage
from htdt.application_preferences import (
    PENDING_PREFERENCE_KEYS,
    PREFERENCE_DEFINITIONS,
    ApplicationPreferenceStore,
)
from htdt.cad_project_activity import CadProjectActivityService
from htdt.cad_project_activity_repository import (
    CadProjectActivityNoteRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.command_registry import CommandRegistry, register_default_commands
from htdt.data_management import (
    DataManagementBackend,
    DataManagementController,
    DataOperationKind,
    DataOperationPhase,
)
from htdt.help_registry import build_help_registry
from htdt.localization import PresentationLocale
from htdt.palette_search import (
    HelpTopicPaletteProvider,
    PaletteResultKind,
)
from htdt.workflow_help import HelpDialog
from htdt.workflow_settings import PreferencesWidget


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- help registry → palette + dialog ----------------------------------------


def test_help_topic_provider_searches_registry_bilingually() -> None:
    registry = build_help_registry()
    opened: list[str] = []

    def on_open(topic_id: str) -> bool:
        opened.append(topic_id)
        return True

    provider = HelpTopicPaletteProvider(registry, on_open)

    hits = provider.search('スピーカー', limit=5)
    assert hits, 'registry topics must be searchable in Japanese'
    assert all(hit.kind is PaletteResultKind.HELP for hit in hits)
    assert all(hit.result_id.startswith('help-topics:') for hit in hits)

    topic_id = hits[0].result_id.split(':', 1)[1]
    assert registry.get(topic_id) is not None
    assert provider.activate(hits[0]) is True
    assert opened == [topic_id]


def test_help_topic_provider_ignores_empty_query() -> None:
    provider = HelpTopicPaletteProvider(build_help_registry(), lambda _id: True)
    assert provider.search('   ') == ()


def test_help_dialog_topic_renders_localized_content() -> None:
    _app()
    registry = build_help_registry()
    topic = next(
        topic
        for topic_id in registry.topic_ids()
        if (topic := registry.get(topic_id)) is not None
        and topic.localized(PresentationLocale.JAPANESE).sections
    )
    dialog = HelpDialog.topic(topic, locale=PresentationLocale.JAPANESE)
    content = topic.localized(PresentationLocale.JAPANESE)
    assert dialog.windowTitle() == content.title
    rendered = '\n'.join(
        label.text() for label in dialog.findChildren(QLabel)
    )
    assert content.summary in rendered
    assert content.sections[0].body in rendered


def test_help_registry_integrity_against_shipped_commands() -> None:
    registry_commands = CommandRegistry()
    register_default_commands(registry_commands)
    report = build_help_registry().validate(
        command_ids=(d.command_id for d in registry_commands.definitions()),
    )
    assert not report.errors, report.errors


# -- activity center → data management controller ----------------------------


def _lifecycle():
    from htdt.data_management import ApplicationDataLifecycle

    return ApplicationDataLifecycle(
        freeze_mutations=lambda: None,
        release_data_handles=lambda: None,
        reopen_data_handles=lambda: None,
        thaw_mutations=lambda: None,
    )


def test_data_management_op_mirrored_into_activity_center(tmp_path: Path) -> None:
    _app()
    center = ActivityCenter()
    controller = DataManagementController(
        DataManagementBackend(tmp_path),
        _lifecycle(),
        activity_center=center,
    )

    released = Event()

    def job(emit, _cancel_event, _on_commit_point):
        emit(DataOperationPhase.SCANNING, '走査しています')
        released.wait(10)
        return None

    controller._start(
        operation_id='op-round6',
        kind=DataOperationKind.SCAN_STORAGE,
        job=job,
        lifecycle_mode='none',
    )
    snapshot = center.get('op-round6')
    assert snapshot is not None
    assert snapshot.state in (OperationState.RUNNING,)
    assert snapshot.title == 'ストレージのスキャン'

    released.set()
    assert _pump_until(
        lambda: center.get('op-round6') is not None
        and center.get('op-round6').state is OperationState.COMPLETED
    )
    assert center.get('op-round6').result_summary
    controller.deleteLater()


def test_immediate_failure_is_recorded_in_activity_center(tmp_path: Path) -> None:
    _app()
    center = ActivityCenter()
    controller = DataManagementController(
        DataManagementBackend(tmp_path),
        _lifecycle(),
        activity_center=center,
    )
    controller._emit_immediate_failure(
        'op-immediate',
        DataOperationKind.CREATE_BACKUP,
        DataOperationPhase.PREPARING,
        'バックアップを開始できませんでした',
        RuntimeError('blocked'),
    )
    snapshot = center.get('op-immediate')
    assert snapshot is not None
    assert snapshot.state is OperationState.FAILED
    assert snapshot.error_summary == 'バックアップを開始できませんでした'
    controller.deleteLater()


# -- cad_project_activity → ActivityPage timeline ----------------------------


def test_activity_page_lists_project_timeline_events(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save(make_empty_scene('doc-round6'), parent_revision_id=None)
    service = CadProjectActivityService(
        scene_repository=repository,
        notes_repository=CadProjectActivityNoteRepository(repository),
    )
    opened: list[str] = []
    page = ActivityPage(
        lambda limit: (),
        list_events=lambda limit: service.recent('doc-round6', limit=limit),
        open_link=opened.append,
    )
    assert page.events_table is not None
    assert page.events_table.rowCount() >= 1
    anchor = page.events_table.item(0, 0)
    link = anchor.data(Qt.ItemDataRole.UserRole)
    assert isinstance(link, str) and link.startswith('htdt://nav/')
    page._activate_event(anchor)
    assert opened == [link]


def test_activity_page_lists_operations(tmp_path: Path) -> None:
    _app()
    center = ActivityCenter()
    op_id = center.submit(
        operation_kind='create_backup',
        operation_class=OperationClass.DATA_MANAGEMENT,
        title='バックアップの作成',
    )
    center.mark_running(op_id)
    page = ActivityPage(lambda limit: (), list_operations=lambda: (*center.active(), *center.recent()))
    assert page.operations_table is not None
    assert page.operations_table.rowCount() == 1
    assert page.operations_table.item(0, 0).text() == '実行中'
    assert 'バックアップの作成' in page.operations_table.item(0, 1).text()


# -- pending preferences ------------------------------------------------------


def test_pending_preference_keys_are_real_definitions() -> None:
    unknown = PENDING_PREFERENCE_KEYS - set(PREFERENCE_DEFINITIONS)
    assert not unknown, unknown


def test_pending_preferences_render_disabled(tmp_path: Path) -> None:
    _app()
    widget = PreferencesWidget(
        ApplicationPreferenceStore(tmp_path / 'prefs.json')
    )
    pending_editor = widget.findChild(
        QWidget, 'preferenceEditor:display_input.theme',
    )
    live_editor = widget.findChild(
        QWidget, 'preferenceEditor:display_input.length_unit',
    )
    assert pending_editor is not None and not pending_editor.isEnabled()
    assert live_editor is not None and live_editor.isEnabled()
    widget.deleteLater()
