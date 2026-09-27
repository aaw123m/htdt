"""Round 8: persisted shell window state (window-state.json)."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from htdt.cad_repository import SceneRepository
from htdt.window_state import (
    PersistedWindowState,
    load_window_state,
    save_window_state,
    window_state_path,
)
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    WorkspaceId,
)
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _factory(label: str):
    return lambda: WorkspaceMount.from_widget(QLabel(label))


def _shell() -> WorkflowShellWindow:
    registrations = build_canonical_workspace_registrations(
        {workspace_id: _factory(workspace_id.value) for workspace_id in WorkspaceId}
    )
    return WorkflowShellWindow(registrations)


def _composition(tmp_path: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


def test_window_state_roundtrip(tmp_path: Path) -> None:
    state = PersistedWindowState(
        geometry_b64='AAAA',
        workspace='activity',
        contexts={'room': 'layout'},
    )
    save_window_state(tmp_path, state)
    loaded = load_window_state(tmp_path)
    assert loaded == state


def test_window_state_missing_and_corrupt_degrade(tmp_path: Path) -> None:
    assert load_window_state(tmp_path) is None
    window_state_path(tmp_path).write_text('{not json', encoding='utf-8')
    assert load_window_state(tmp_path) is None


def test_window_state_drops_unknown_destinations(tmp_path: Path) -> None:
    save_window_state(
        tmp_path,
        PersistedWindowState(
            workspace='does-not-exist',
            contexts={'does-not-exist': 'x', 'room': 'layout'},
        ),
    )
    loaded = load_window_state(tmp_path)
    assert loaded is not None
    assert loaded.workspace is None
    assert loaded.contexts == {'room': 'layout'}


def test_shell_close_hook_persists_state(tmp_path: Path) -> None:
    app = _app()
    shell = _shell()
    saved: list[PersistedWindowState] = []
    shell.register_close_hook(
        lambda: saved.append(
            PersistedWindowState(
                workspace=str(shell.current_workspace_id),
                contexts=shell.selected_contexts(),
            )
        )
    )
    shell.show()
    shell.navigate(WorkspaceId.MEASUREMENT)
    app.processEvents()
    shell.close()
    app.processEvents()
    assert saved and saved[0].workspace == 'measurement'


def test_shell_close_hook_not_run_when_close_vetoed(tmp_path: Path) -> None:
    app = _app()
    shell = _shell()
    fired: list[str] = []
    shell.register_close_guard(lambda: (False, 'busy'))
    shell.register_close_hook(lambda: fired.append('hook'))
    shell.show()
    app.processEvents()
    shell.close()
    app.processEvents()
    assert fired == []
    assert shell.isVisible()  # close was vetoed


def test_seed_selected_contexts_ignores_unknown(tmp_path: Path) -> None:
    _app()
    shell = _shell()
    shell.seed_selected_contexts(
        {'not-a-workspace': 'x', 'overview': 'no-such-context'}
    )
    # Everything dropped — nothing seeded for unregistered contexts.
    assert all(
        context != 'x' for context in shell.selected_contexts().values()
    )


def test_composition_restores_saved_workspace(tmp_path: Path) -> None:
    app = _app()
    save_window_state(
        tmp_path / "data",
        PersistedWindowState(workspace='activity'),
    )
    composition = _composition(tmp_path / "data")
    app.processEvents()
    assert (
        composition.shell.current_workspace_id
        is ApplicationDestinationId.ACTIVITY
    )
    shell = composition.shell
    shell.close()


def test_composition_close_persists_state_for_next_launch(
    tmp_path: Path,
) -> None:
    app = _app()
    composition = _composition(tmp_path / "data")
    shell = composition.shell
    shell.show()
    shell.navigate(ApplicationDestinationId.ACTIVITY)
    app.processEvents()
    shell.close()
    app.processEvents()

    loaded = load_window_state(tmp_path / "data")
    assert loaded is not None
    assert loaded.workspace == 'activity'
    assert loaded.geometry_b64


def test_safe_mode_skips_layout_restore(tmp_path: Path) -> None:
    app = _app()
    save_window_state(
        tmp_path / "data",
        PersistedWindowState(workspace='activity'),
    )
    composition = _composition(tmp_path / "data", safe_mode=True)
    app.processEvents()
    assert (
        composition.shell.current_workspace_id is WorkspaceId.OVERVIEW
    )
    assert 'セーフモード' in composition.shell.windowTitle()


def test_safe_mode_start_automatic_backup_is_noop(tmp_path: Path) -> None:
    composition = _composition(tmp_path / "data", safe_mode=True)
    composition.start_automatic_backup()
    assert composition._automatic_backup_runner is None
