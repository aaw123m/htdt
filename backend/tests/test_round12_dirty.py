"""Round-12: undo/redo, dirty-state & document-lifecycle coherence.

Regression coverage for the editing-model contract audit:

- every WorkingDocument mutation verb round-trips apply→undo→redo exactly;
- a composite command whose side effect raises leaves document and history
  clean (no partial state, no consumed undo step);
- placement hard constraints (#486) join the undo stack — the round-8
  bypass where ``_constraint_action`` persisted outside the history;
- ``restore_revision`` refuses to orphan a dirty working document or an
  unresolved recovery candidate;
- ``SystemExpansionWorkflowService.apply`` honours the workspace draft
  guard — a variant apply writes a new head outside the working document;
- measurement-side head writes (derive point, materialize pattern) refuse
  to orphan a persisted draft;
- constraint persist failures leave in-memory state consistent with disk
  (persist-first ordering).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_constraint_authoring import (
    add_constraint,
    make_walkway_constraint,
    remove_constraint,
)
from htdt.cad_document import (
    EditStateError,
    WorkingDocument,
)
from htdt.cad_geometric_constraints import make_symmetric_pair_constraint
from htdt.cad_measurement_target_pattern import (
    TargetPatternOffset,
    build_target_pattern,
    materialize_target_pattern,
)
from htdt.cad_measurement_target_pattern import CadTargetPatternRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from htdt.measurement_workflow import (
    MeasurementWorkflowController,
    MeasurementWorkflowError,
)
from htdt.room_workspace import RoomWorkspace, RoomWorkspaceController
from htdt.system_expansion_workflow import SystemExpansionWorkflowService


NOW = '2026-03-01T00:00:00Z'


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _controller(tmp_path: Path) -> RoomWorkspaceController:
    return RoomWorkspaceController(_repository(tmp_path), F1_DOCUMENT_ID)


class _FakePlotter:
    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass

    def add_mesh(self, *_args, **_kwargs):
        return None

    def view_xy(self, **_kwargs) -> None:
        pass

    def enable_parallel_projection(self, *_args, **_kwargs) -> None:
        pass

    def reset_camera(self, render=True, bounds=None) -> None:
        pass


class _FakeViewport(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()

    def render_document(self, document, **kwargs) -> None:
        pass

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id: str) -> None:
        pass

    def focus_entities(self, entity_ids) -> None:
        pass

    def _last_display_position(self) -> QPointF:
        return QPointF(10.0, 10.0)

    def pick_world_position(self, _position):
        return (1.0, -2.0, 0.0)


def _workspace(tmp_path: Path) -> RoomWorkspace:
    _app()
    repository = _repository(tmp_path)
    return RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
    )


def _new_entity(entity_id: str, *, name: str = '追加') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=name,
        position=Position3(x_m=4.0, y_m=3.0, z_m=1.0),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


# ---------------------------------------------------------------------
# Undo/redo contract: every mutation verb restores exact prior state.


def _verb_cases():
    def _move(working: WorkingDocument) -> bool:
        return working.move_entity(
            'speaker-fl', Position3(x_m=1.9, y_m=0.75, z_m=1.05)
        )

    def _rotate(working: WorkingDocument) -> bool:
        return working.rotate_entity(
            'speaker-fl',
            quaternion_from_euler_deg(yaw_deg=33.0, pitch_deg=0.0, roll_deg=0.0),
        )

    def _transform(working: WorkingDocument) -> bool:
        before = working.committed_document.entity('speaker-c')
        return working.transform_entities(
            (before,), (before.model_copy(update={'name': 'センターB'}),)
        )

    def _add(working: WorkingDocument) -> bool:
        return working.add_entity(_new_entity('speaker-added'))

    def _duplicate(working: WorkingDocument) -> bool:
        return working.duplicate_entity(
            'speaker-fl', new_entity_id='speaker-fl-copy'
        )

    def _update(working: WorkingDocument) -> bool:
        return working.update_entities({'speaker-fl': {'name': 'FL改名'}})

    def _delete(working: WorkingDocument) -> bool:
        return working.delete_entities(('speaker-fl', 'speaker-fr'))

    def _replace_document(working: WorkingDocument) -> bool:
        document = working.committed_document
        room = document.room.model_copy(update={'width_m': 7.0})
        return working.replace_document(document.model_copy(update={'room': room}))

    def _preview_commit(working: WorkingDocument) -> bool:
        working.begin_move('speaker-fl')
        working.preview_move(Position3(x_m=2.0, y_m=2.0, z_m=1.05))
        return working.commit_preview()

    def _entity_set(working: WorkingDocument) -> bool:
        before = working.committed_document.entity('speaker-c')
        return working.apply_entity_set_edit(
            replaced_before=(before,),
            replaced_after=(before.model_copy(update={'name': 'C差替'}),),
        )

    return [
        pytest.param(_move, id='move'),
        pytest.param(_rotate, id='rotate'),
        pytest.param(_transform, id='transform'),
        pytest.param(_add, id='add'),
        pytest.param(_duplicate, id='duplicate'),
        pytest.param(_update, id='update_entities'),
        pytest.param(_delete, id='delete_entities'),
        pytest.param(_replace_document, id='replace_document'),
        pytest.param(_preview_commit, id='preview_commit'),
        pytest.param(_entity_set, id='entity_set_edit'),
    ]


@pytest.mark.parametrize('edit', _verb_cases())
def test_every_edit_verb_round_trips_undo_and_redo(edit) -> None:
    """apply→undo restores the exact document; redo restores the applied one."""
    working = WorkingDocument(make_f1_scene())
    original = working.committed_document
    assert edit(working)
    applied = working.committed_document
    assert applied != original
    assert working.is_dirty

    assert working.undo()
    assert working.committed_document == original
    assert not working.is_dirty  # undo back to the saved hash is clean

    assert working.redo()
    assert working.committed_document == applied
    assert working.is_dirty


def test_state_only_composite_is_one_undo_step() -> None:
    """A command with only side effects still consumes exactly one step."""
    working = WorkingDocument(make_f1_scene())
    calls: list[str] = []
    assert working.apply_entity_set_edit(
        apply_side=lambda: calls.append('apply'),
        revert_side=lambda: calls.append('revert'),
    )
    assert calls == ['apply']
    assert working.history_length == 1

    assert working.undo()
    assert calls == ['apply', 'revert']
    assert working.redo()
    assert calls == ['apply', 'revert', 'apply']


def test_composite_apply_side_failure_leaves_document_and_history_clean() -> None:
    """A raising side effect must not consume a step or dirty the document."""
    working = WorkingDocument(make_f1_scene())
    original = working.committed_document

    def _boom() -> None:
        raise RuntimeError('persist failed')

    with pytest.raises(RuntimeError):
        working.apply_entity_set_edit(apply_side=_boom)
    assert working.committed_document == original
    assert working.history_length == 0
    assert not working.can_undo
    assert not working.is_dirty


def test_composite_revert_side_failure_keeps_command_applied() -> None:
    """A raising revert must not consume the command (round-9 contract)."""
    working = WorkingDocument(make_f1_scene())
    calls: list[str] = []

    def _revert_boom() -> None:
        calls.append('revert-tried')
        raise EditStateError('revert side failed')

    assert working.apply_entity_set_edit(
        apply_side=lambda: calls.append('apply'),
        revert_side=_revert_boom,
    )
    with pytest.raises(EditStateError):
        working.undo()
    assert calls == ['apply', 'revert-tried']
    assert working.can_undo  # the step is still applied and undoable


# ---------------------------------------------------------------------
# Placement hard constraints (#486): the panel path joins the undo stack.


def test_update_placement_constraints_participates_in_undo(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    constraint = make_walkway_constraint(controller.document, 'speaker-fl')
    new_set = add_constraint(controller.constraint_set, constraint)

    assert controller.update_placement_constraints(new_set)
    assert controller.constraint_set == new_set
    # Persisted eagerly — and undoable: Undo restores in-memory AND disk.
    stored = controller.constraint_repository.load(F1_DOCUMENT_ID)
    assert stored == new_set
    assert controller.working.can_undo
    assert controller.working.undo_label == '配置制約'

    assert controller.undo()
    assert controller.constraint_set.constraints == ()
    assert (
        controller.constraint_repository.load(F1_DOCUMENT_ID).constraints == ()
    )
    assert controller.redo()
    assert controller.constraint_set == new_set
    assert controller.constraint_repository.load(F1_DOCUMENT_ID) == new_set


def test_update_placement_constraints_delete_round_trips(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    constraint = make_walkway_constraint(controller.document, 'speaker-fl')
    first = add_constraint(controller.constraint_set, constraint)
    assert controller.update_placement_constraints(first)
    second = remove_constraint(first, constraint.constraint_id)
    assert controller.update_placement_constraints(second)
    assert controller.constraint_set.constraints == ()

    assert controller.undo()
    assert controller.constraint_set == first
    assert controller.constraint_repository.load(F1_DOCUMENT_ID) == first


def test_update_placement_constraints_noop_is_not_recorded(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    assert not controller.update_placement_constraints(controller.constraint_set)
    assert not controller.working.can_undo


def test_placement_constraint_persist_failure_leaves_state_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed save must not swap the in-memory set or consume a step."""
    controller = _controller(tmp_path)

    def _boom(*_args, **_kwargs) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(controller.constraint_repository, 'save', _boom)
    before = controller.constraint_set
    new_set = add_constraint(
        before, make_walkway_constraint(controller.document, 'speaker-fl')
    )
    with pytest.raises(OSError):
        controller.update_placement_constraints(new_set)
    assert controller.constraint_set == before
    assert not controller.working.can_undo


def test_authoring_constraint_persist_failure_leaves_state_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ordering guarantee for the #843 authoring sidecar."""
    controller = _controller(tmp_path)

    def _boom(*_args, **_kwargs) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(
        controller.repository, 'save_authoring_constraints', _boom
    )
    before = controller.authoring_constraints
    with pytest.raises(OSError):
        controller.add_authoring_constraint(
            make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
        )
    assert controller.authoring_constraints == before
    assert not controller.working.can_undo


def test_room_workspace_constraint_action_goes_through_undo(tmp_path: Path) -> None:
    """The panel's add/delete actions now run one undoable command each."""
    workspace = _workspace(tmp_path)
    workspace.controller.set_selection('speaker-fl')
    workspace._constraint_action('add_walkway')
    assert len(workspace.controller.constraint_set.constraints) == 1
    assert workspace.controller.working.can_undo

    workspace.undo()
    assert workspace.controller.constraint_set.constraints == ()
    workspace.redo()
    assert len(workspace.controller.constraint_set.constraints) == 1

    # Delete uses the selected row's constraint id.
    from htdt.room_constraints_panel import _CONSTRAINT_ID_ROLE

    constraint_id = workspace.controller.constraint_set.constraints[0].constraint_id
    workspace._sync_constraints_panel()
    tree = workspace.constraints_panel.results_tree
    for row in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(row)
        if item.data(0, _CONSTRAINT_ID_ROLE) == constraint_id:
            tree.setCurrentItem(item)
            break
    assert workspace.constraints_panel.selected_constraint_id() == constraint_id
    workspace._constraint_action('delete')
    assert workspace.controller.constraint_set.constraints == ()
    workspace.undo()
    assert len(workspace.controller.constraint_set.constraints) == 1


def test_room_workspace_constraint_action_reports_missing_selection(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace._constraint_action('add_allowed')
    assert workspace.controller.constraint_set.constraints == ()
    assert '対象を選択してください' in workspace.status.text()


# ---------------------------------------------------------------------
# restore_revision: a new head must never silently orphan dirty edits.


def test_restore_revision_refuses_while_dirty(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.delete_entities(('furniture-left',))
    controller.save()
    head = repository.current_head(F1_DOCUMENT_ID)
    assert head is not None
    older = next(
        revision
        for revision in controller.list_revisions()
        if revision.revision_id != head.revision_id
    )
    controller.working.add_entity(_new_entity('speaker-extra'))
    assert controller.working.is_dirty

    with pytest.raises(EditStateError, match='未保存の変更'):
        controller.restore_revision(older.revision_id)
    # The draft survives — head and working document are untouched.
    assert repository.current_head(F1_DOCUMENT_ID) == head
    assert controller.working.is_dirty


def test_restore_revision_refuses_pending_recovery(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.delete_entities(('furniture-left',))
    controller.save()
    head = repository.current_head(F1_DOCUMENT_ID)
    assert head is not None
    older = next(
        revision
        for revision in controller.list_revisions()
        if revision.revision_id != head.revision_id
    )
    # A persisted draft (keep_draft or crash recovery) becomes the
    # recovery candidate on the next bind.
    controller.working.add_entity(_new_entity('speaker-draft'))
    repository.save_recovery(
        controller.working.committed_document,
        source_revision_id=controller.working.source_revision_id,
    )
    rebound = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert rebound.recovery_candidate is not None
    with pytest.raises(EditStateError, match='復旧可能な下書き'):
        rebound.restore_revision(older.revision_id)


def test_restore_revision_succeeds_once_clean(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.delete_entities(('furniture-left',))
    controller.save()
    head = repository.current_head(F1_DOCUMENT_ID)
    older = next(
        revision
        for revision in controller.list_revisions()
        if revision.revision_id != head.revision_id
    )
    controller.working.add_entity(_new_entity('speaker-extra'))
    controller.undo()  # clean again
    restored = controller.restore_revision(older.revision_id)
    assert restored.revision_id != head.revision_id
    assert controller.committed_document == older.document


# ---------------------------------------------------------------------
# Variant apply: the head write honours the owning workspace's draft guard.


def test_system_expansion_apply_guard_vetoes_while_dirty(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    service = SystemExpansionWorkflowService(repository, F1_DOCUMENT_ID)
    assert service.apply_guard is None  # default: no guard installed

    service.apply_guard = lambda: (
        '未保存の変更を保存または破棄してから提案を適用してください'
    )
    with pytest.raises(ValueError, match='未保存の変更'):
        service.apply('variant-any')

    service.apply_guard = lambda: None
    with pytest.raises(ValueError, match='does not exist'):
        service.apply('variant-any')  # guard allows → real authority runs


def test_room_workspace_wires_apply_guard(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    guard = workspace.system_expansion.apply_guard
    assert guard is not None
    assert guard() is None  # clean workspace allows apply
    workspace.controller.delete_entities(('furniture-left',))
    assert guard() == '未保存の変更を保存または破棄してから提案を適用してください'
    workspace.undo()
    assert guard() is None


# ---------------------------------------------------------------------
# Measurement-side head writes refuse to orphan a persisted draft.


def test_derive_measurement_point_refuses_with_pending_draft(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    head = repository.current_head(F1_DOCUMENT_ID)
    assert head is not None
    draft = head.document.model_copy(
        update={'entities': head.document.entities[:-1]}
    )
    repository.save_recovery(draft, source_revision_id=head.revision_id)

    with pytest.raises(MeasurementWorkflowError, match='下書き'):
        controller.derive_measurement_point_from_seat(
            'seat-any', measurement_point_id='point-new'
        )


def test_derive_measurement_point_runs_without_draft(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = MeasurementWorkflowController(repository, F1_DOCUMENT_ID)
    # No recovery snapshot: the guard passes and the domain error wins —
    # proves the guard sits behind real validation, not ahead of it.
    with pytest.raises(MeasurementWorkflowError, match='seat'):
        controller.derive_measurement_point_from_seat(
            'seat-any', measurement_point_id='point-new'
        )


def test_materialize_target_pattern_refuses_with_pending_draft(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    head = repository.current_head(F1_DOCUMENT_ID)
    assert head is not None
    pattern = build_target_pattern(
        repository,
        document_id=F1_DOCUMENT_ID,
        anchor_kind='explicit_point',
        explicit_position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        offsets=(
            TargetPatternOffset(
                offset_index=0, label='T1', offset_m=(0.0, 0.0, 0.0)
            ),
        ),
        created_at=NOW,
    )
    pattern_repository = CadTargetPatternRepository(repository)
    draft = head.document.model_copy(
        update={'entities': head.document.entities[:-1]}
    )
    repository.save_recovery(draft, source_revision_id=head.revision_id)

    with pytest.raises(ValueError, match='下書き'):
        materialize_target_pattern(
            repository, pattern_repository, pattern, created_at=NOW
        )


# ---------------------------------------------------------------------
# Selection/focus after undo: deleted-then-undone entities are selectable.


def test_deleted_then_undone_entity_is_selectable(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.set_selection('furniture-left')
    assert 'furniture-left' in controller.view_state.selection
    controller.delete_entities(('furniture-left',))
    # Deletion sanitizes the selection away.
    assert 'furniture-left' not in controller.view_state.selection
    assert controller.undo()
    # The entity exists again and is selectable.
    controller.set_selection('furniture-left')
    assert 'furniture-left' in controller.view_state.selection


def test_undo_redo_keeps_selection_sanitized_across_deletes(
    tmp_path: Path,
) -> None:
    """View state is forward-only: undo restores entities, not chrome.

    Selection/hidden/locked assertions drop at delete and stay dropped —
    view state is persisted workspace chrome outside the command model.
    The undo contract is that every revived entity is present, sane, and
    selectable again (panels and viewport re-sync from ``sanitize``).
    """
    controller = _controller(tmp_path)
    controller.set_selection('furniture-left')
    controller.set_entities_hidden(('speaker-c',), True)
    controller.delete_entities(('furniture-left', 'speaker-c'))
    assert controller.undo()
    # Every view-state reference resolves against live entities only.
    assert not controller.view_state.is_hidden('speaker-c')
    for entity_id in (
        tuple(controller.view_state.selected_ids)
        + tuple(controller.view_state.hidden_ids)
        + tuple(controller.view_state.locked_ids)
    ):
        controller.committed_document.entity(entity_id)  # raises if dangling
    controller.set_selection('speaker-c')
    assert 'speaker-c' in controller.view_state.selection
