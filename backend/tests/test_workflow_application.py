from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from htdt import dirty_state_dialog
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.command_palette import CommandPalette, CommandShortcutBinder
from htdt.navigation_target import NavigationTarget, NavigationTargetKind
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
    assert 'プロジェクト' in titles
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
