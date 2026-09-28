"""Round 13: cross-project / cross-document state isolation.

Repro tests for the review findings — each one documents a spec-level
leak where state from context A surfaced in context B:

- the in-place ``_switch_project`` path kept the outgoing project's live
  window-state (workspace context selections) and never persisted it
  under the outgoing project ref, nor replayed the target project's own
  persisted layout — the close+respawn path round-trips the same state
  correctly, so the two switch paths disagreed;
- forwarded launch intents stayed bound to the FIRST shell forever, so
  after a close+respawn project switch an ``.htdtproject`` open silently
  rebound the hidden, closed composition while the visible window stayed
  on the old project;
- a respawned composition never restarted its automatic-backup runner,
  so the once-per-launch due check silently stopped after any
  close+respawn switch.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.launch_intents import build_launch_intent
from htdt.native_cad import _route_launch_intent
from htdt.window_state import (
    PersistedWindowState,
    load_window_state,
    save_window_state,
)
from htdt.workflow_application import WorkflowApplicationComposition
from htdt.workflow_navigation import ApplicationDestinationId, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _composition(tmp_path: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "data" / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


def _second_project(composition: WorkflowApplicationComposition):
    composition.repository.save(
        make_empty_scene('document-2'), parent_revision_id=None
    )
    return composition.project_library.create_project(
        'Second Project', document_id='document-2'
    )


def _third_project(composition: WorkflowApplicationComposition):
    composition.repository.save(
        make_empty_scene('document-3'), parent_revision_id=None
    )
    return composition.project_library.create_project(
        'Third Project', document_id='document-3'
    )


def test_in_place_switch_persists_outgoing_window_state(
    tmp_path: Path,
) -> None:
    """Leaving project A must persist A's live layout under A's ref —
    the close hook only fires at shell close, so an in-place switch
    previously dropped A's mid-session context/workspace changes."""
    app = _app()
    composition = _composition(tmp_path)
    entry_a = composition.project_entry
    shell = composition.shell
    shell.show()

    # A's operator picked a non-default context in the room workspace.
    shell._selected_context[WorkspaceId.ROOM] = 'history'
    shell.navigate(ApplicationDestinationId.ACTIVITY)
    app.processEvents()

    _second_project(composition)
    reason = composition._switch_project('document-2')
    assert reason is None

    saved_a = load_window_state(
        tmp_path / "data", project_ref=entry_a.project_id
    )
    assert saved_a is not None
    assert saved_a.workspace == 'activity'
    assert saved_a.contexts.get('room') == 'history'
    assert saved_a.project_ref == entry_a.project_id

    shell.close()
    shell.deleteLater()
    app.processEvents()


def test_in_place_switch_replays_target_contexts_not_outgoing(
    tmp_path: Path,
) -> None:
    """B must see its own persisted contexts — not A's live selections
    carried over through the merge-only seed."""
    app = _app()
    composition = _composition(tmp_path)
    shell = composition.shell
    shell.show()
    shell._selected_context[WorkspaceId.ROOM] = 'history'

    entry_b = _second_project(composition)
    save_window_state(
        tmp_path / "data",
        PersistedWindowState(contexts={'room': 'acoustics'}),
        project_ref=entry_b.project_id,
    )

    reason = composition._switch_project('document-2')
    assert reason is None

    contexts = shell.selected_contexts()
    assert contexts.get('room') == 'acoustics'
    assert contexts.get('room') != 'history'

    shell.close()
    shell.deleteLater()
    app.processEvents()


def test_in_place_switch_restores_target_workspace(
    tmp_path: Path,
) -> None:
    """The close+respawn path lands on B's saved workspace; the in-place
    path must agree instead of forcing Overview."""
    app = _app()
    composition = _composition(tmp_path)
    shell = composition.shell
    shell.show()

    entry_b = _second_project(composition)
    save_window_state(
        tmp_path / "data",
        PersistedWindowState(workspace='projects'),
        project_ref=entry_b.project_id,
    )

    reason = composition._switch_project('document-2')
    assert reason is None
    assert (
        shell.current_workspace_id is ApplicationDestinationId.PROJECTS
    )

    shell.close()
    shell.deleteLater()
    app.processEvents()


def test_in_place_switch_round_trip_keeps_a_intact(tmp_path: Path) -> None:
    """A -> B -> A: switching back replays A's persisted state, not
    whatever context B's operator picked in between."""
    app = _app()
    composition = _composition(tmp_path)
    entry_a = composition.project_entry
    shell = composition.shell
    shell.show()
    shell._selected_context[WorkspaceId.ROOM] = 'history'
    app.processEvents()

    _second_project(composition)
    assert composition._switch_project('document-2') is None
    # B's operator picks a different context.
    shell._selected_context[WorkspaceId.ROOM] = 'objects'

    assert composition._switch_project('document-1') is None
    contexts = shell.selected_contexts()
    assert contexts.get('room') == 'history'

    shell.close()
    shell.deleteLater()
    app.processEvents()


def test_launch_intent_routes_to_live_composition(
    tmp_path: Path, monkeypatch
) -> None:
    """After a close+respawn switch the intent pump's window is a closed
    shell — an ``.htdtproject`` open must act on the live composition,
    not silently rebind the hidden one."""
    app = _app()
    monkeypatch.setattr(
        QMessageBox, 'information', staticmethod(lambda *a, **k: 0)
    )
    monkeypatch.setattr(
        QMessageBox, 'warning', staticmethod(lambda *a, **k: 0)
    )
    composition = _composition(tmp_path)
    entry_b = _second_project(composition)
    _third_project(composition)
    old_shell = composition.shell
    old_shell.show()
    app.processEvents()

    composition._switch_to_project(entry_b)
    app.processEvents()
    assert len(composition._spawned_compositions) == 1
    live = composition._spawned_compositions[0]
    assert live.document_id == 'document-2'

    descriptor = tmp_path / 'third.htdtproject'
    descriptor.write_text(
        json.dumps(
            {
                'kind': 'htdt-project-ref',
                'schema_version': 1,
                'document_id': 'document-3',
            }
        ),
        encoding='utf-8',
    )
    intent = build_launch_intent(descriptor)
    diagnostics = SimpleNamespace(logger=logging.getLogger('test'))
    result = _route_launch_intent(
        intent,
        window=old_shell,
        repository=composition.repository,
        diagnostics=diagnostics,
    )
    app.processEvents()

    assert result.outcome == 'routed_and_opened'
    # The intent opened project C in the window the user sees — the
    # closed composition must not absorb the switch.
    assert live.document_id == 'document-3'
    assert composition.document_id == 'document-1'

    live.shell.close()
    live.shell.deleteLater()
    app.processEvents()


def test_respawned_composition_restarts_backup_runner(
    tmp_path: Path,
) -> None:
    """The close hook shuts the outgoing runner down; the respawned
    composition must run its own due check or automatic backups stop."""
    app = _app()
    composition = _composition(tmp_path)
    entry_b = _second_project(composition)
    composition.start_automatic_backup()
    assert composition._automatic_backup_runner is not None

    composition._switch_to_project(entry_b)
    app.processEvents()

    live = composition._spawned_compositions[0]
    assert live._automatic_backup_runner is not None

    live.shell.close()
    live.shell.deleteLater()
    app.processEvents()
