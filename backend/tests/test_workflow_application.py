from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
)

from htdt import dirty_state_dialog
from htdt.activity_center import OperationState
from htdt.cad_project_template_repository import CadProjectTemplateRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene, make_f1_scene
from htdt.command_palette import CommandPalette, CommandShortcutBinder
from htdt.navigation_target import NavigationTarget, NavigationTargetKind
from htdt.native_worker import WORKER_CANCELLED
from htdt.command_registry import (
    DATA_MUTATIONS_FROZEN_REASON,
    CommandAvailability,
)
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId
from htdt.workflow_shell import WorkspaceMount


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(tmp_path: Path) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1")


def _palette_item(palette: CommandPalette, command_id: str):
    for row in range(palette.results_list.count()):
        item = palette.results_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == command_id:
            return item
    raise AssertionError(f"{command_id} not found in palette results")


def test_backup_freeze_blocks_mutating_commands_via_shortcut_and_palette(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = _app()
    # Activating a frozen command in the palette opens its bound 'why' help
    # topic — a modal QDialog the offscreen run must auto-dismiss.
    monkeypatch.setattr(
        QDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    composition = _composition(tmp_path)
    registry = composition.registry
    lifecycle = composition.data_management_controller.lifecycle

    events: list[str] = []
    registry.bind(
        "project.save",
        execute=lambda: events.append("save"),
        availability=CommandAvailability.available,
    )
    registry.bind(
        "room.view.fit_all",
        execute=lambda: events.append("fit-all"),
        availability=CommandAvailability.available,
    )
    binder = CommandShortcutBinder(
        composition.shell,
        registry,
        command_ids=("project.save", "room.view.fit_all"),
    )
    shortcuts = {command_id: shortcut for command_id, shortcut in binder._shortcuts}

    # The Ctrl+S executor is reachable before the data operation starts.
    shortcuts["project.save"].activated.emit()
    app.processEvents()
    assert events == ["save"]

    lifecycle.begin_backup()

    assert registry.data_mutations_frozen
    assert composition.shell.rail.isEnabled() is False

    availability = registry.availability("project.save")
    assert availability.enabled is False
    assert availability.disabled_reason == DATA_MUTATIONS_FROZEN_REASON

    # A GLOBAL shortcut activation still routes through the registry, which now
    # fails closed even though the workspace-local provider reports enabled.
    shortcuts["project.save"].activated.emit()
    app.processEvents()
    assert registry.execute("project.save") is False
    assert events == ["save"]

    # Read-only commands keep working while mutations are frozen.
    shortcuts["room.view.fit_all"].activated.emit()
    app.processEvents()
    assert events == ["save", "fit-all"]

    # The palette surfaces the data-operation reason and refuses activation.
    palette = composition.command_palette.palette
    palette.refresh_results("保存")
    item = _palette_item(palette, "project.save")
    assert DATA_MUTATIONS_FROZEN_REASON in item.text()
    palette.results_list.setCurrentItem(item)
    palette.activate_current()
    assert events == ["save", "fit-all"]
    assert DATA_MUTATIONS_FROZEN_REASON in palette.detail_label.text()

    lifecycle.finish_backup()

    assert registry.data_mutations_frozen is False
    assert composition.shell.rail.isEnabled() is True
    assert registry.availability("project.save").enabled is True
    shortcuts["project.save"].activated.emit()
    app.processEvents()
    assert events == ["save", "fit-all", "save"]

    binder.deleteLater()
    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_in_progress_backup_holds_the_freeze_until_the_worker_completes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    registry = composition.registry
    controller = composition.data_management_controller

    events: list[str] = []
    registry.bind(
        "project.save",
        execute=lambda: events.append("save"),
        availability=CommandAvailability.available,
    )
    binder = CommandShortcutBinder(
        composition.shell,
        registry,
        command_ids=("project.save",),
    )
    save_shortcut = next(
        shortcut
        for command_id, shortcut in binder._shortcuts
        if command_id == "project.save"
    )

    release_worker = threading.Event()
    real_create_backup = controller.backend.create_backup

    def gated_create_backup(
        destination: Path,
        *,
        allow_stale: bool = False,
        is_cancelled=None,
    ):
        release_worker.wait(timeout=15)
        return real_create_backup(
            destination,
            allow_stale=allow_stale,
            is_cancelled=is_cancelled,
        )

    monkeypatch.setattr(
        controller.backend,
        "create_backup",
        gated_create_backup,
    )

    backup_results: list[object] = []
    controller.backup_created.connect(backup_results.append)
    failures: list[object] = []
    controller.operation_failed.connect(failures.append)
    backup_path = tmp_path / "portable.htdt-backup"

    try:
        controller.create_backup(backup_path)

        # begin_backup() runs synchronously; the worker is still parked.
        assert controller.is_busy
        assert registry.data_mutations_frozen
        assert registry.availability("project.save").enabled is False
        assert registry.execute("project.save") is False
        save_shortcut.activated.emit()
        app.processEvents()
        assert events == []
    finally:
        release_worker.set()

    deadline = time.monotonic() + 15
    while controller.is_busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not controller.is_busy
    assert backup_results, f"backup_created signal never fired: {failures}"
    assert backup_path.is_file()
    assert registry.data_mutations_frozen is False
    assert registry.availability("project.save").enabled is True

    save_shortcut.activated.emit()
    app.processEvents()
    assert events == ["save"]

    binder.deleteLater()
    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_restore_freeze_disposes_and_rebuilds_data_workspaces(
    tmp_path: Path,
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    # document-1 must exist in the restored generation for the rebind to
    # keep it active (#768): an unsaved id would be a stale authority.
    composition.repository.save(
        make_empty_scene("document-1"), parent_revision_id=None
    )
    registry = composition.registry
    lifecycle = composition.data_management_controller.lifecycle

    lifecycle.begin_restore()

    assert registry.data_mutations_frozen
    assert composition.shell.router.current_workspace_id is None
    assert composition.shell.router.mount(WorkspaceId.OVERVIEW) is None

    lifecycle.resume_after_restore_attempt()

    assert registry.data_mutations_frozen is False
    assert composition.shell.rail.isEnabled() is True
    assert composition.shell.current_workspace_id is WorkspaceId.OVERVIEW
    assert composition.shell.router.mount(WorkspaceId.OVERVIEW) is not None

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_restore_rebind_routes_to_project_selection_when_project_is_gone(
    tmp_path: Path,
) -> None:
    """#768: a pre-restore project absent from the restored generation can
    never remain active — the shell must land on the selection surface."""
    app = _app()
    composition = _composition(tmp_path)
    lifecycle = composition.data_management_controller.lifecycle

    # Pre-restore history entries address a different data epoch — a
    # whole-data restore establishes a new navigation epoch (#768).
    window = composition.shell
    window.navigation_history.record(
        NavigationTarget(
            kind=NavigationTargetKind.SCENE_ENTITY,
            object_ids=("stale-object",),
            project_id="document-1",
        ),
        WorkspaceDeepLink(WorkspaceId.ROOM),
    )
    assert window.navigation_history.entries()

    lifecycle.begin_restore()

    # The restored generation lacks the pre-restore project: while handles
    # are released, drop its library row the way a whole-data restore would.
    with sqlite3.connect(composition.repository_path) as connection:
        connection.execute(
            'DELETE FROM htdt_project_documents WHERE document_id=?',
            ("document-1",),
        )
        connection.commit()

    lifecycle.resume_after_restore_attempt()

    assert composition.document_id == ""
    assert composition.shell.current_workspace_id is not WorkspaceId.OVERVIEW
    assert window.navigation_history.entries() == ()

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_project_menu_present_and_title_shows_project(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)

    titles = [
        action.text()
        for action in composition.shell.menuBar().actions()
    ]
    assert 'プロジェクト(&P)' in titles
    assert composition.shell.windowTitle() == (
        f'Home Theater Digital Twin — '
        f'{composition.project_entry.display_name}'
    )

    # Switching to the already-open document is a no-op: identity intact.
    composition._switch_to_project(composition.project_entry)
    assert composition.shell.windowTitle() == (
        f'Home Theater Digital Twin — '
        f'{composition.project_entry.display_name}'
    )
    assert composition.document_id == 'document-1'

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_project_switch_rebinds_canonical_entry_and_title(tmp_path: Path) -> None:
    """#919: an application-page switch rebinds document + entry + title +
    chip atomically — no split-brain identity."""
    app = _app()
    composition = _composition(tmp_path)
    composition.repository.save(
        make_empty_scene('document-2'), parent_revision_id=None
    )
    entry = composition.project_library.create_project(
        'Second Project', document_id='document-2'
    )

    reason = composition._switch_project('document-2')

    assert reason is None
    assert composition.document_id == 'document-2'
    assert composition.project_entry.project_id == entry.project_id
    assert composition.project_entry.display_name == 'Second Project'
    assert composition.shell.windowTitle() == (
        'Home Theater Digital Twin — Second Project'
    )
    assert (
        composition.shell.context_bar._project_label.text()
        == 'プロジェクト: Second Project'
    )
    reopened = composition.project_library.get_by_document_id('document-2')
    assert reopened is not None
    assert reopened.last_opened_at_utc is not None

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_save_project_as_template_persists_user_template(
    tmp_path: Path, monkeypatch
) -> None:
    """REV44: the dead ``save_template`` writer gets a production path —
    the project-menu action captures the persisted head's speaker layout
    and the wizard template listing can then offer it."""
    app = _app()
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    composition = WorkflowApplicationComposition(repository, 'fixture-f1')

    def _fill_and_accept(dialog: QDialog) -> QDialog.DialogCode:
        for edit in dialog.findChildren(QLineEdit):
            if edit.text() == '1':
                continue  # version field keeps its default
            edit.setText('シアター雛形')
        combo = dialog.findChild(QComboBox)
        if combo is not None and combo.count() > 1:
            combo.setCurrentIndex(1)  # MLP as layout reference
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", _fill_and_accept)
    # The QMessageBox statics are native-level calls — patch them directly
    # or the offscreen run blocks on a real modal.
    monkeypatch.setattr(
        QMessageBox,
        'information',
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        'warning',
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    composition._save_project_as_template()

    templates = CadProjectTemplateRepository(repository).list_templates()
    user_templates = [item for item in templates if item.kind == 'user']
    assert len(user_templates) == 1
    saved = user_templates[0]
    assert saved.name == 'シアター雛形'
    assert saved.version == '1'
    assert {spec.speaker_role for spec in saved.speaker_specs} == {
        'FL', 'C', 'FR'
    }
    assert saved.layout_reference is not None
    assert saved.layout_reference.source_kind == 'measurement_point'
    assert saved.layout_reference.source_entity_id == 'point-mlp'
    # Geometry intent is normalized around the picked reference, not raw
    # entity positions.
    fl = next(
        spec for spec in saved.speaker_specs if spec.speaker_role == 'FL'
    )
    assert fl.nominal_azimuth_deg is not None

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_save_project_as_template_cancel_persists_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    composition = WorkflowApplicationComposition(repository, 'fixture-f1')
    monkeypatch.setattr(
        QDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    monkeypatch.setattr(
        QMessageBox,
        'information',
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        'warning',
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )

    composition._save_project_as_template()

    templates = CadProjectTemplateRepository(repository).list_templates()
    assert [item for item in templates if item.kind == 'user'] == []

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_projects_page_open_routes_through_project_id(tmp_path: Path) -> None:
    """#919: the Projects listing opens by canonical project_id."""
    app = _app()
    composition = _composition(tmp_path)
    composition.repository.save(
        make_empty_scene('document-2'), parent_revision_id=None
    )
    entry = composition.project_library.create_project(
        'Second Project', document_id='document-2'
    )

    composition._open_project_by_id(entry.project_id)

    assert composition.document_id == 'document-2'
    assert composition.project_entry.display_name == 'Second Project'

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def _dirty_mount(state: dict) -> WorkspaceMount:
    def guard():
        return (False, '未保存の変更があります') if state['dirty'] else (True, None)

    def dirty_state():
        return 'dirty_recoverable' if state['dirty'] else 'clean'

    def resolve(action):
        state['actions'].append(action)
        if action == 'save':
            state['dirty'] = False
            return True, '保存しました'
        return False, '残りました'

    return WorkspaceMount.from_widget(
        QLabel('fake'),
        before_deactivate=guard,
        dirty_state=dirty_state,
        resolve_dirty_state=resolve,
    )


def test_snapshot_decision_clean_and_frozen(tmp_path: Path) -> None:
    """#918/#927: a clean project snapshots immediately; a frozen one refuses."""
    app = _app()
    composition = _composition(tmp_path)

    # Only clean (or guard-less) mounts are mounted: the decision is 'saved'.
    assert composition._project_snapshot_decision('複製') == 'saved'

    composition._freeze_data_mutations()
    assert composition._project_snapshot_decision('複製') is None
    composition._thaw_data_mutations()

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_snapshot_decision_last_saved_keeps_working_copy(
    tmp_path: Path, monkeypatch
) -> None:
    """#918/#927: 'last_saved' serializes persisted state without touching
    the dirty working copy."""
    app = _app()
    composition = _composition(tmp_path)
    state = {'dirty': True, 'actions': []}
    composition.shell.router._mounts[WorkspaceId.ROOM] = _dirty_mount(state)

    monkeypatch.setattr(
        dirty_state_dialog,
        'choose_snapshot_action',
        lambda label, parent: 'last_saved',
    )
    assert composition._project_snapshot_decision('エクスポート') == 'last_saved'
    assert state['dirty'] is True
    assert state['actions'] == []

    monkeypatch.setattr(
        dirty_state_dialog,
        'choose_snapshot_action',
        lambda label, parent: None,
    )
    assert composition._project_snapshot_decision('エクスポート') is None
    assert state['dirty'] is True

    # The mount stays dirty on purpose — clean it before the shell's
    # closeEvent would open a real resolve dialog.
    state['dirty'] = False
    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_snapshot_decision_save_resolves_dirty_mounts(
    tmp_path: Path, monkeypatch
) -> None:
    """#918/#927: 'save' resolves every dirty mount before the artifact."""
    app = _app()
    composition = _composition(tmp_path)
    state = {'dirty': True, 'actions': []}
    composition.shell.router._mounts[WorkspaceId.ROOM] = _dirty_mount(state)

    monkeypatch.setattr(
        dirty_state_dialog,
        'choose_snapshot_action',
        lambda label, parent: 'save',
    )
    assert composition._project_snapshot_decision('複製') == 'saved'
    assert state['actions'] == ['save']
    assert state['dirty'] is False

    # An unresolvable mount still refuses the artifact; the interactive
    # fallback is patched to report "keep open".
    state['dirty'] = True
    mount = composition.shell.router._mounts[WorkspaceId.ROOM]
    monkeypatch.setattr(
        mount,
        'resolve_dirty_state',
        lambda action: (False, '残りました'),
        raising=False,
    )
    monkeypatch.setattr(
        dirty_state_dialog,
        'resolve_mount_dirty_state',
        lambda mount, context, parent: (False, '残りました'),
    )
    assert composition._project_snapshot_decision('複製') is None

    state['dirty'] = False
    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------
# REV24-UXFLOW: status-bar truth + dialog affordance regressions.
# These construct a WorkflowApplicationComposition — they must live in a
# module without native_editor/room_workspace (PyVista) imports, which
# crash xdist workers during composition construction.


def test_bundle_completion_clears_busy_status(tmp_path: Path) -> None:
    """A finished bundle job must not keep its 'running' status line."""
    app = _app()
    composition = _composition(tmp_path)
    bar = composition.shell.statusBar()

    composition._begin_bundle_job("プロジェクトバンドルをエクスポートしています…")
    assert "エクスポートしています" in bar.currentMessage()

    composition._bundle_job_completed(
        "project.bundle.export", None, WORKER_CANCELLED
    )
    assert composition._bundle_status_message is None
    assert "エクスポートしています" not in bar.currentMessage()

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_bundle_completion_keeps_fresher_status(
    tmp_path: Path, monkeypatch
) -> None:
    """A notice posted while the job ran must survive its cleanup."""
    app = _app()
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    composition = _composition(tmp_path)
    bar = composition.shell.statusBar()

    composition._begin_bundle_job("プロジェクトバンドルをエクスポートしています…")
    bar.showMessage("別の通知")

    result = SimpleNamespace(row_count=3, asset_count=2, manifest_sha256="abc")
    composition._bundle_job_completed("project.bundle.export", result, None)

    assert bar.currentMessage() == "別の通知"
    assert composition._bundle_busy is False

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def _exec_activating_first_row(self: QDialog) -> int:
    """Stand-in for QDialog.exec: fire the list's primary gesture."""
    listing = self.findChild(QListWidget)
    if listing is not None and listing.count():
        listing.setCurrentRow(0)
        listing.itemActivated.emit(listing.item(0))
    return int(self.result())


def test_pick_one_accepts_on_item_activation(
    tmp_path: Path, monkeypatch
) -> None:
    """Enter/double-click on a picker row accepts — same gesture as the
    command palette list."""
    app = _app()
    composition = _composition(tmp_path)
    monkeypatch.setattr(QDialog, "exec", _exec_activating_first_row)

    picked = composition._pick_one(
        "対象を選択", "対象:", (("Alpha", "id-a"), ("Beta", "id-b"))
    )
    assert picked == "id-a"

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_choose_project_accepts_on_item_activation(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    monkeypatch.setattr(QDialog, "exec", _exec_activating_first_row)

    entries = (
        SimpleNamespace(
            project_id="p-1",
            display_name="リビング",
            created_at_utc="2026-01-01T00:00:00Z",
            last_opened_at_utc="2026-01-02T00:00:00Z",
            archived=False,
        ),
        SimpleNamespace(
            project_id="p-2",
            display_name="シアター",
            created_at_utc="2026-01-01T00:00:00Z",
            last_opened_at_utc=None,
            archived=False,
        ),
    )
    picked = composition._choose_project(entries, "開く", "対象:")
    assert picked is entries[0]

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_prompt_project_name_reprompts_until_named(
    tmp_path: Path, monkeypatch
) -> None:
    """Blank names re-prompt instead of dead-ending the flow."""
    app = _app()
    composition = _composition(tmp_path)
    answers = iter([("", True), ("   ", True), ("  リビング  ", True)])
    warnings: list[str] = []

    monkeypatch.setattr(
        QInputDialog, "getText", lambda *a, **k: next(answers)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(a[2])
    )

    assert (
        composition._prompt_project_name("新規プロジェクト", "プロジェクト名:")
        == "リビング"
    )
    assert warnings == ["プロジェクト名を入力してください"] * 2

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_prompt_project_name_cancel_aborts(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    answers = iter([("", True), ("", False)])
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *a, **k: next(answers)
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

    assert (
        composition._prompt_project_name("新規プロジェクト", "プロジェクト名:")
        is None
    )

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_bundle_completion_runs_on_ui_thread(tmp_path: Path, monkeypatch) -> None:
    """REV24-UXFLOW: ``_bundle_job_completed`` is a bound method of the
    plain (non-QObject) composition — connected directly to
    ``worker.completed`` it ran ON the worker thread, so every Qt call in
    the handler (QMessageBox parenting, ``.exec()``, statusBar writes) was
    a cross-thread violation that ghosted modal dialogs and deadlocked
    the app after export/import. The pool now relays ``on_completed``
    through its own queued slot, so any plain callable lands on the UI
    thread."""
    app = _app()
    composition = _composition(tmp_path)
    delivered_on: list[object] = []
    loop = QEventLoop()

    original = composition._bundle_job_completed

    def spy(*args):
        delivered_on.append(QThread.currentThread())
        result = original(*args)
        loop.quit()
        return result

    monkeypatch.setattr(composition, '_bundle_job_completed', spy)
    monkeypatch.setattr(
        QMessageBox, 'exec', lambda self: QDialog.DialogCode.Accepted
    )

    composition._begin_bundle_job('エクスポート中…')
    composition._bundle_pool.start(
        'project.bundle.export',
        lambda _cancel: SimpleNamespace(
            row_count=1, asset_count=1, manifest_sha256='x'
        ),
        composition._bundle_job_completed,
    )
    QTimer.singleShot(10_000, loop.quit)
    loop.exec()
    try:
        assert delivered_on == [app.thread()]
        assert composition._bundle_busy is False
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_analysis_export_refuses_during_bundle_job(
    tmp_path: Path, monkeypatch
) -> None:
    """REV27: ``analysis.export_bundle`` was the only bundle-pool entry
    point missing the ``_bundle_busy`` guard its siblings share — a
    re-entry would double-book the pool and corrupt the in-flight
    status-line bookkeeping."""
    app = _app()
    composition = _composition(tmp_path)
    starts: list[str] = []
    monkeypatch.setattr(
        composition._bundle_pool,
        'start',
        lambda operation_id, job, on_completed: starts.append(operation_id),
    )
    composition._begin_bundle_job('エクスポート中…')

    composition._export_analysis_bundle()

    assert starts == []

    composition._bundle_job_completed(
        'project.bundle.export', None, WORKER_CANCELLED
    )
    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_project_menu_entries_survive_unbound_project_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """REV27: a restore that lands on zero projects leaves
    ``project_entry=None`` — the project menu must not AttributeError
    into excepthook."""
    app = _app()
    composition = _composition(tmp_path)
    composition.document_id = ''
    composition.project_entry = None

    chosen: list[tuple] = []
    monkeypatch.setattr(
        composition,
        '_choose_project',
        lambda entries, *args: chosen.append(tuple(entries)) or None,
    )
    composition._open_project_dialog()
    assert chosen[0] == composition.project_library.list_projects()

    composition._archive_dialog(archived=True)
    assert chosen[1] == tuple(
        entry
        for entry in composition.project_library.list_projects(
            include_archived=True
        )
        if not entry.archived
    )

    for action in (
        composition._rename_project,
        composition._duplicate_project,
        composition._export_project_bundle,
        composition._open_deliverables,
    ):
        composition.shell.statusBar().clearMessage()
        action()
        assert '先にプロジェクトを選択してください' in (
            composition.shell.statusBar().currentMessage()
        )

    composition.shell.close()
    composition.shell.deleteLater()
    app.processEvents()


def test_restore_rebind_note_shows_display_name(tmp_path: Path) -> None:
    """REV27: the post-restore note interpolated the raw ``document_id``
    uuid into user-facing text — it must name the project by display_name
    like the title bar and context-bar chip do."""
    app = _app()
    composition = _composition(tmp_path)
    composition.project_library.rename_project(
        composition.project_entry.project_id, 'リビングシアター'
    )
    try:
        composition._reopen_data_handles()
        assert composition._restore_rebind_note == (
            'アクティブプロジェクト: リビングシアター'
        )
        assert composition.document_id not in composition._restore_rebind_note
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_capture_watch_completion_reports_rejected_outcomes(
    tmp_path: Path,
) -> None:
    """REV43-SEAMS: every non-success outcome must surface — a rejected or
    operator-action-needed drop used to fall through both the ``staged``
    and the ``failed`` filters and vanished with zero user signal."""
    app = _app()
    composition = _composition(tmp_path)
    results = [
        (
            Path('/watch/desc.htdtcapture'),
            SimpleNamespace(outcome='invalid_or_unsupported'),
            None,
        ),
        (
            Path('/watch/needs.htdtcapture'),
            SimpleNamespace(outcome='user_action_required'),
            None,
        ),
        (
            Path('/watch/dup.htdtcapture'),
            SimpleNamespace(outcome='already_staged'),
            None,
        ),
    ]
    try:
        composition._on_capture_watch_completed(results)
        ops = [
            op
            for op in composition.activity_center.recent(limit=20)
            if op.operation_kind == 'capture_watch_stage'
        ]
        assert len(ops) == 1
        # #1022: an all-failed batch ends FAILED — the error is carried in
        # error_summary, never smuggled through result_summary as a fake
        # completion.
        assert ops[0].state == OperationState.FAILED
        assert '2 件は取り込めませんでした' in (ops[0].error_summary or '')
        # already_staged stays quiet — only genuine non-success is counted.
        assert '3 件' not in (ops[0].error_summary or '')
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()


def test_capture_watch_completion_quiet_on_duplicates_only(
    tmp_path: Path,
) -> None:
    """REV43-SEAMS: a batch of pure re-arrivals emits no activity entry."""
    app = _app()
    composition = _composition(tmp_path)
    results = [
        (
            Path('/watch/dup.htdtcapture'),
            SimpleNamespace(outcome='already_staged'),
            None,
        ),
    ]
    try:
        composition._on_capture_watch_completed(results)
        assert [
            op
            for op in composition.activity_center.recent(limit=20)
            if op.operation_kind == 'capture_watch_stage'
        ] == []
    finally:
        composition.shell.close()
        composition.shell.deleteLater()
        app.processEvents()
