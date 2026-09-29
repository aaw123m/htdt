"""Round-14: undo / command-model depth.

The WorkingDocument command history is the editor's safety net: every command
must round-trip the *serialized* document (canonical_scene_json — all fields,
ids, order), multi-call-site operations must stay one Undo step, the history
must honour its in-memory bound, and refresh-derived state maintenance must
never push phantom commands that livelock Undo.

Coverage:

- every mutation verb round-trips canonical JSON byte-for-byte
  (apply -> undo == original, redo == applied);
- batch delete / entity-set edits restore exact entity order including
  interleaved removal indices;
- a failed push leaves the redo branch intact;
- the bounded history drops the oldest commands honestly and the dirty
  marker stays truthful when the saved state falls out of reach;
- merge fuses a driver edit and its constraint propagation into a single
  Undo step;
- batch duplicate is one Undo step (the previous loop pushed N commands);
- deleting constrained entities marks them broken inside the same Undo
  step, and refresh-time broken marking never pushes commands (the
  pre-fix behaviour livelocked Undo);
- input hygiene: unknown update fields and missing entity ids raise
  EditStateError instead of leaking KeyError/StopIteration.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from htdt.cad_document import (
    AddEntitiesCommand,
    CommandHistory,
    EditStateError,
    ReplaceDocumentCommand,
    SequentialEditCommand,
    WorkingDocument,
)
from htdt.cad_geometric_constraints import make_symmetric_pair_constraint
from htdt.cad_repository import SceneRepository
from htdt.cad_room import RoomWorkingDocument
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    SceneEntity,
    Size3,
    canonical_scene_json,
    make_f1_scene,
    quaternion_from_euler_deg,
)
from htdt.room_workspace import RoomWorkspaceController


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _controller(tmp_path: Path) -> RoomWorkspaceController:
    return RoomWorkspaceController(_repository(tmp_path), F1_DOCUMENT_ID)


def _new_entity(entity_id: str, *, name: str = '追加') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=name,
        position=Position3(x_m=4.0, y_m=3.0, z_m=1.0),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


def _state(working: WorkingDocument) -> str:
    """Serialized committed document — byte-for-byte semantic state."""
    return canonical_scene_json(working.committed_document)


# ---------------------------------------------------------------------
# Serialized-state round-trips: undo must restore the exact document.


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
        return working.add_entity(_new_entity('speaker-added'), index=0)

    def _duplicate(working: WorkingDocument) -> bool:
        return working.duplicate_entity(
            'speaker-fl', new_entity_id='speaker-fl-copy'
        )

    def _update(working: WorkingDocument) -> bool:
        return working.update_entity('speaker-fl', name='FL改名')

    def _update_batch(working: WorkingDocument) -> bool:
        return working.update_entities(
            {'speaker-fl': {'name': 'FL改名'}, 'speaker-fr': {'name': 'FR改名'}}
        )

    def _delete(working: WorkingDocument) -> bool:
        return working.delete_entities(('speaker-fl', 'speaker-c'))

    def _replace_document(working: WorkingDocument) -> bool:
        document = working.committed_document
        room = document.room.model_copy(update={'width_m': 7.0})
        return working.replace_document(
            document.model_copy(update={'room': room})
        )

    def _preview_commit(working: WorkingDocument) -> bool:
        working.begin_move('speaker-fl')
        working.preview_move(Position3(x_m=2.0, y_m=2.0, z_m=1.05))
        return working.commit_preview()

    def _entity_set_mixed(working: WorkingDocument) -> bool:
        document = working.committed_document
        removed = document.entity('speaker-c')
        replaced = document.entity('speaker-fr')
        added = _new_entity('furniture-new')
        return working.apply_entity_set_edit(
            removed=(removed,),
            replaced_before=(replaced,),
            replaced_after=(replaced.model_copy(update={'name': 'FR差替'}),),
            added=(added,),
        )

    def _composite_side(working: WorkingDocument) -> bool:
        before = working.committed_document.entity('speaker-c')
        return working.apply_entity_set_edit(
            replaced_before=(before,),
            replaced_after=(before.model_copy(update={'name': 'C追従'}),),
            apply_side=lambda: None,
            revert_side=lambda: None,
        )

    return [
        pytest.param(_move, id='move'),
        pytest.param(_rotate, id='rotate'),
        pytest.param(_transform, id='transform'),
        pytest.param(_add, id='add_at_index'),
        pytest.param(_duplicate, id='duplicate'),
        pytest.param(_update, id='update_entity'),
        pytest.param(_update_batch, id='update_entities'),
        pytest.param(_delete, id='delete_entities'),
        pytest.param(_replace_document, id='replace_document'),
        pytest.param(_preview_commit, id='preview_commit'),
        pytest.param(_entity_set_mixed, id='entity_set_mixed'),
        pytest.param(_composite_side, id='composite_side'),
    ]


@pytest.mark.parametrize('edit', _verb_cases())
def test_verbs_round_trip_serialized_state(edit) -> None:
    """apply→undo is byte-identical to the original; redo to the applied doc."""
    working = WorkingDocument(make_f1_scene())
    original = _state(working)
    assert edit(working)
    applied = _state(working)
    assert applied != original

    assert working.undo()
    assert _state(working) == original

    assert working.redo()
    assert _state(working) == applied


def test_replace_room_round_trips_serialized_state() -> None:
    working = RoomWorkingDocument(make_f1_scene())
    original = _state(working)
    room = working.committed_document.room.model_copy(update={'width_m': 8.0})
    assert working.replace_room(room)
    applied = _state(working)
    assert working.undo()
    assert _state(working) == original
    assert working.redo()
    assert _state(working) == applied


# ---------------------------------------------------------------------
# Index drift: index-keyed restores must be byte-exact.


def test_batch_delete_restores_interleaved_order() -> None:
    """Deleting entities at positions 1 and 3 restores the exact tuple order."""
    working = WorkingDocument(make_f1_scene())
    original = _state(working)
    assert working.delete_entities(('speaker-c', 'point-mlp'))
    assert [e.entity_id for e in working.committed_document.entities] == [
        'speaker-fl',
        'speaker-fr',
        'furniture-left',
    ]
    assert working.undo()
    assert _state(working) == original


def test_insert_at_index_survives_other_edits() -> None:
    """Identity-keyed undo: deleting/undeleting another entity does not corrupt
    the recorded insert position of an earlier add."""
    working = WorkingDocument(make_f1_scene())
    original = _state(working)
    assert working.add_entity(_new_entity('inserted'), index=0)
    added_state = _state(working)
    assert working.delete_entity('speaker-c')
    assert working.undo()  # restore speaker-c
    assert _state(working) == added_state
    assert working.undo()  # remove inserted
    assert _state(working) == original


def test_entity_set_edit_restores_removed_at_positions() -> None:
    """Mixed batch: removed-at-indices + replaced + added is one byte-exact step."""
    working = WorkingDocument(make_f1_scene())
    document = working.committed_document
    removed = (document.entities[1], document.entities[3])  # speaker-c, point-mlp
    replaced = document.entities[0]
    added = _new_entity('added-tail')
    original = _state(working)
    assert working.apply_entity_set_edit(
        removed=removed,
        replaced_before=(replaced,),
        replaced_after=(replaced.model_copy(update={'name': 'FL差替'}),),
        added=(added,),
    )
    applied = _state(working)
    assert working.undo()
    assert _state(working) == original
    assert working.redo()
    assert _state(working) == applied


# ---------------------------------------------------------------------
# Failure atomicity: a command that throws must not corrupt the stack.


def test_failed_push_preserves_redo_tail() -> None:
    """A rejected command neither truncates the redo branch nor consumes a step."""
    working = WorkingDocument(make_f1_scene())
    assert working.move_entity('speaker-fl', Position3(x_m=2.0, y_m=1.0, z_m=1.0))
    applied = _state(working)
    assert working.undo()
    original = _state(working)

    # A command whose recorded before-state no longer matches must raise
    # inside apply() — before history truncates the redo branch.
    mismatched = make_f1_scene().model_copy(update={'room': None})
    command = ReplaceDocumentCommand(
        before=mismatched,
        after=mismatched.model_copy(update={'entities': ()}),
    )
    with pytest.raises(EditStateError):
        working.push_command(command)

    assert _state(working) == original
    assert working.can_redo
    assert working.redo()
    assert _state(working) == applied


def test_failed_undo_keeps_command_applied() -> None:
    """A revert that raises leaves the command applied and retryable."""
    working = WorkingDocument(make_f1_scene())
    original = _state(working)
    calls: list[str] = []

    def _boom() -> None:
        calls.append('revert-tried')
        raise EditStateError('revert side failed')

    assert working.apply_entity_set_edit(
        apply_side=lambda: calls.append('apply'),
        revert_side=_boom,
    )
    with pytest.raises(EditStateError):
        working.undo()
    assert _state(working) == original
    assert working.can_undo  # still applied — retryable, not consumed


# ---------------------------------------------------------------------
# History bound: oldest commands drop honestly.


def test_history_bound_drops_oldest() -> None:
    """The stack honours its limit — extra pushes evict the oldest entries."""
    history = CommandHistory(limit=3)
    document = make_f1_scene()
    for index in range(5):
        entity = _new_entity(f'furniture-{index}')
        document = history.push(
            AddEntitiesCommand(entities=(entity,), index=len(document.entities)),
            document,
        )
    assert history.length == 3
    assert history.index == 3
    assert history.can_undo
    assert not history.can_redo
    # Only the last three commands can be reverted; the evicted two stay.
    for _ in range(3):
        document = history.undo(document)
    assert not history.can_undo
    assert len(document.entities) == len(make_f1_scene().entities) + 2


def test_dirty_marker_stays_honest_past_bound() -> None:
    """When the saved state falls out of undo reach the doc stays dirty."""
    working = WorkingDocument(make_f1_scene())
    for index in range(600):
        assert working.add_entity(_new_entity(f'furniture-{index}'))
    while working.undo():
        pass
    # The earliest edits were evicted — the saved state is unreachable,
    # so the document must still report dirty.
    assert working.is_dirty
    assert working.history_length <= 500


# ---------------------------------------------------------------------
# Sequential fusion: one semantic op = one Undo step.


def test_merge_last_fuses_into_single_step() -> None:
    working = WorkingDocument(make_f1_scene())
    original = _state(working)
    assert working.move_entity('speaker-fl', Position3(x_m=2.0, y_m=1.0, z_m=1.0))
    assert working.move_entity('speaker-fr', Position3(x_m=4.0, y_m=1.0, z_m=1.0))
    assert working.history_length == 2

    assert working.merge_last(2)
    assert working.history_length == 1
    fused = _state(working)
    # The fused step keeps the driver's label.
    assert working.undo_label is not None and '移動' in working.undo_label

    assert working.undo()
    assert _state(working) == original
    assert working.redo()
    assert _state(working) == fused


def test_merge_since_marker_fuses_only_new_commands() -> None:
    working = WorkingDocument(make_f1_scene())
    assert working.move_entity('speaker-c', Position3(x_m=3.2, y_m=1.0, z_m=1.0))
    marker = working.history_index
    assert working.move_entity('speaker-fl', Position3(x_m=2.0, y_m=1.0, z_m=1.0))
    assert working.move_entity('speaker-fr', Position3(x_m=4.0, y_m=1.0, z_m=1.0))
    assert working.merge_history_since(marker)
    assert working.history_length == 2


def test_merge_last_refuses_empty_or_single() -> None:
    working = WorkingDocument(make_f1_scene())
    assert not working.merge_last(2)
    working.move_entity('speaker-fl', Position3(x_m=2.0, y_m=1.0, z_m=1.0))
    assert not working.merge_last(2)
    assert not working.merge_last(1)


def test_sequential_command_rejects_single_member() -> None:
    with pytest.raises(EditStateError):
        SequentialEditCommand(
            commands=(AddEntitiesCommand(entities=(), index=0),)
        )


# ---------------------------------------------------------------------
# Batch duplicate: one semantic op must consume exactly one step.


def test_duplicate_selected_is_one_undo_step(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    original = canonical_scene_json(controller.committed_document)
    controller.set_selection('speaker-fl')
    controller.set_selection('speaker-fr', additive=True)
    assert controller.duplicate_selected() == 2
    duplicated = canonical_scene_json(controller.committed_document)
    assert controller.working.history_length == 1

    assert controller.undo()
    assert canonical_scene_json(controller.committed_document) == original
    assert controller.redo()
    assert canonical_scene_json(controller.committed_document) == duplicated


# ---------------------------------------------------------------------
# Constraint marking: broken-by-delete joins the delete's Undo step; the
# refresh-time marker never pushes phantom commands.


def test_delete_marks_constraints_inside_same_undo_step(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    constraint = make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    controller.add_authoring_constraint(constraint)
    history_depth = controller.working.history_length

    assert controller.delete_entities(('speaker-fl',))
    assert controller.working.history_length == history_depth + 1
    broken = [
        item
        for item in controller.authoring_constraints.constraints
        if item.broken
    ]
    assert [item.constraint_id for item in broken] == [constraint.constraint_id]

    # One Undo restores the entity AND the unbroken constraint atomically.
    assert controller.undo()
    assert canonical_scene_json(controller.committed_document) == canonical_scene_json(
        make_f1_scene()
    )
    assert not any(
        item.broken for item in controller.authoring_constraints.constraints
    )

    # Redo re-applies delete and marking together.
    assert controller.redo()
    assert 'speaker-fl' not in {
        entity.entity_id for entity in controller.committed_document.entities
    }
    assert any(
        item.broken for item in controller.authoring_constraints.constraints
    )


def test_mark_broken_constraints_pushes_no_command(tmp_path: Path) -> None:
    """Refresh-derived marking is state maintenance, never a history entry."""
    controller = _controller(tmp_path)
    constraint = make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    controller.add_authoring_constraint(constraint)
    depth = controller.working.history_length

    # Lose the member through a path that does not mark constraints.
    entity = controller.committed_document.entity('speaker-fl')
    assert controller.working.apply_entity_set_edit(removed=(entity,))
    labels = controller.mark_broken_constraints()
    assert labels
    assert controller.working.history_length == depth + 1  # only the delete


def test_undo_after_marking_reaches_clean_state(tmp_path: Path) -> None:
    """Regression: refresh-time marking must not livelock Undo.

    Before the fix the marker pushed a fresh command on each refresh, so
    Undo could never reach the delete that caused the break.
    """
    controller = _controller(tmp_path)
    clean = canonical_scene_json(controller.committed_document)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    controller.delete_entities(('speaker-fl',))
    controller.mark_broken_constraints()  # refresh tick: nothing new to mark

    steps = 0
    while controller.undo():
        steps += 1
        controller.mark_broken_constraints()
        assert steps < 10, 'undo must terminate'
    assert canonical_scene_json(controller.committed_document) == clean


# ---------------------------------------------------------------------
# Constraint propagation: the driver edit and its propagation are one step.


def test_propagated_move_is_one_undo_step(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    original = canonical_scene_json(controller.committed_document)
    depth = controller.working.history_length

    assert controller.update_entities(
        {'speaker-fl': {'position': Position3(x_m=2.0, y_m=0.75, z_m=1.05)}}
    )
    notes = controller.propagate_constraints(
        {'speaker-fl'}, merge_with_previous=True
    )
    assert notes
    moved = controller.committed_document.entity('speaker-fr')
    assert moved.position.x_m == pytest.approx(4.0)
    assert controller.working.history_length == depth + 1

    assert controller.undo()
    assert canonical_scene_json(controller.committed_document) == original
    assert controller.redo()
    assert controller.committed_document.entity(
        'speaker-fr'
    ).position.x_m == pytest.approx(4.0)


def test_propagate_without_merge_flag_stays_separate(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.add_authoring_constraint(
        make_symmetric_pair_constraint('speaker-fl', 'speaker-fr')
    )
    depth = controller.working.history_length
    assert controller.update_entities(
        {'speaker-fl': {'position': Position3(x_m=2.0, y_m=0.75, z_m=1.05)}}
    )
    assert controller.propagate_constraints({'speaker-fl'})
    assert controller.working.history_length == depth + 2


# ---------------------------------------------------------------------
# Preview merge semantics: separate commits stay separate steps.


def test_two_preview_commits_do_not_merge() -> None:
    working = WorkingDocument(make_f1_scene())
    working.begin_move('speaker-fl')
    working.preview_move(Position3(x_m=2.0, y_m=1.0, z_m=1.0))
    assert working.commit_preview()
    working.begin_move('speaker-fl')
    working.preview_move(Position3(x_m=2.5, y_m=1.0, z_m=1.0))
    assert working.commit_preview()
    assert working.history_length == 2

    assert working.undo()
    assert working.committed_document.entity('speaker-fl').position.x_m == 2.0
    assert working.undo()
    assert working.committed_document.entity('speaker-fl').position.x_m == 1.35


def test_noop_preview_commit_records_nothing() -> None:
    working = WorkingDocument(make_f1_scene())
    working.begin_move('speaker-fl')
    working.preview_move(
        working.committed_document.entity('speaker-fl').position
    )
    assert not working.commit_preview()
    assert working.history_length == 0
    assert not working.can_undo


# ---------------------------------------------------------------------
# Input hygiene: command helpers fail closed with domain errors.


def test_unknown_entity_ids_raise_edit_state_error() -> None:
    working = WorkingDocument(make_f1_scene())
    with pytest.raises(EditStateError):
        working.move_entity('ghost', Position3(x_m=0, y_m=0, z_m=0))
    with pytest.raises(EditStateError):
        working.update_entity('ghost', name='x')
    with pytest.raises(EditStateError):
        working.duplicate_entity('ghost', new_entity_id='g2')


def test_update_entity_rejects_unknown_fields() -> None:
    working = WorkingDocument(make_f1_scene())
    with pytest.raises(EditStateError):
        working.update_entity('speaker-fl', positon={'x_m': 1})
    with pytest.raises(EditStateError):
        working.update_entities({'speaker-fl': {'nme': 'typo'}})


def test_transform_entities_revalidates_replacements() -> None:
    """model_copy bypasses validators — a command must not admit invalid entities."""
    working = WorkingDocument(make_f1_scene())
    before = working.committed_document.entity('speaker-fl')
    invalid = before.model_copy(update={'speaker_role': None})
    with pytest.raises((EditStateError, ValueError)):
        working.transform_entities((before,), (invalid,))


def test_entity_set_edit_revalidates_added() -> None:
    working = WorkingDocument(make_f1_scene())
    invalid = _new_entity('bad').model_copy(update={'size_m': None})
    with pytest.raises((EditStateError, ValueError)):
        working.apply_entity_set_edit(added=(invalid,))


# ---------------------------------------------------------------------
# Save/dirty across the undo boundary.


def test_save_then_undo_tracks_real_clean_state(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    assert controller.update_entities({'speaker-fl': {'name': 'FL-1'}})
    assert controller.save()
    assert not controller.is_dirty

    # Undo crosses the save boundary: the draft differs from the saved head.
    assert controller.undo()
    assert controller.is_dirty

    # Redo restores the saved content — the clean marker points at the saved revision.
    assert controller.redo()
    assert not controller.is_dirty

    assert controller.undo()
    assert controller.is_dirty
