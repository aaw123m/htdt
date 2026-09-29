"""Round 16: multi-project / session-lifecycle state leakage.

The close+respawn project switch (``_switch_to_project`` →
``shell.close()`` → ``_open_document``) must not leave project A's
composition/subscribers behind: the app-scoped ``ApplicationPreferenceStore``
would otherwise keep invoking dead observers — and a raising dead listener
surfaces as ``PreferenceNotificationError`` on an unrelated live write.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from htdt.cad_repository import SceneRepository
from htdt.project_library import ProjectLibraryError
from htdt.workflow_application import WorkflowApplicationComposition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(tmp_path: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


def _flush_deferred_delete() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.sendPostedEvents()


def _open_project(composition: WorkflowApplicationComposition, document_id: str):
    """Drive the close+respawn switch path with a stubbed library."""
    composition.project_library.open_project = lambda project_id: SimpleNamespace(
        document_id=document_id, project_id=project_id
    )
    composition._switch_to_project(
        SimpleNamespace(document_id=document_id, project_id=f"proj-{document_id}")
    )


def test_switch_detaches_dead_composition_preference_listeners(
    tmp_path: Path,
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    store = composition.preferences
    dead_listener = composition._on_preference_change
    dead_panel_release = composition._preferences_panel
    base_count = len(store._listeners)

    _open_project(composition, 'doc-2')
    app.processEvents()

    assert dead_listener not in store._listeners
    assert dead_panel_release._on_external_change not in store._listeners
    live = composition.live_composition()
    assert live is not composition
    assert live._on_preference_change in store._listeners
    assert len(store._listeners) <= base_count


def test_listener_count_stable_across_repeated_switches(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    store = composition.preferences
    base_count = len(store._listeners)

    for index in range(3):
        _open_project(composition.live_composition(), f'doc-{index + 2}')
        app.processEvents()

    assert len(store._listeners) <= base_count


def test_dead_shell_is_released_after_respawn(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    dead_shell = composition.shell
    top_before = len(app.topLevelWidgets())

    _open_project(composition, 'doc-2')
    app.processEvents()
    _flush_deferred_delete()
    app.processEvents()

    # The dead shell was C++-deleted, not just hidden: Qt calls raise
    # RuntimeError while python attributes stay readable for the
    # launch-intent pump's workflow_application rebound.
    try:
        dead_shell.isVisible()
        raise AssertionError('dead shell still wraps a live C++ object')
    except RuntimeError:
        pass
    assert dead_shell.workflow_application is composition
    assert len(app.topLevelWidgets()) <= top_before


def test_preference_commit_after_switch_never_touches_dead_observers(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    composition = _composition(tmp_path)
    dead_panel = composition._preferences_panel
    dead_syncs: list[object] = []
    monkeypatch.setattr(dead_panel, '_sync_editor', dead_syncs.append)

    _open_project(composition, 'doc-2')
    app.processEvents()
    _flush_deferred_delete()

    # A live write must notify only live observers — the dead panel's
    # listener, still subscribed, would reach a torn-down editor tree and
    # raise inside the store's fan-out (PreferenceNotificationError on
    # an unrelated write).
    composition.preferences.set('display_input.numeric_precision', 3)
    assert dead_syncs == []


def test_live_composition_still_tracks_preference_commits(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app()
    composition = _composition(tmp_path)

    _open_project(composition, 'doc-2')
    app.processEvents()

    live = composition.live_composition()
    seen: list[object] = []
    # The stored listener calls ``self._apply_rew_endpoint`` through the
    # instance, so patching that attribute observes live notifications.
    monkeypatch.setattr(live, '_apply_rew_endpoint', lambda: seen.append(1))
    live.preferences.set('integrations.rew_port', 4800)
    assert len(seen) == 1


def test_dead_shell_close_keeps_switch_guard_behaviour(
    tmp_path: Path, monkeypatch
) -> None:
    """A vetoed close must not unsubscribe — the composition stays live."""
    app = _app()
    composition = _composition(tmp_path)
    monkeypatch.setattr(
        QMessageBox, 'warning', staticmethod(lambda *a, **k: 0)
    )

    def _failing_open(_project_id: str):
        raise ProjectLibraryError('entry vanished')

    monkeypatch.setattr(
        composition.project_library, 'open_project', _failing_open
    )
    composition._switch_to_project(
        SimpleNamespace(document_id='doc-2', project_id='proj-2')
    )
    app.processEvents()

    assert composition._on_preference_change in composition.preferences._listeners
