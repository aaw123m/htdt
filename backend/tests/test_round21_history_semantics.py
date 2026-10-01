"""Round-21: user-visible history / undo / revision semantics.

Round 14 audited the command model itself (round-trip, atomic batches,
bounded storage). This round verifies the semantics a user actually
observes: redo invalidation after a divergent edit, undo across
save/restore/rebind boundaries, atomic composite restores, byte-exact
revision restores, a diff view that reports EVERY persisted delta category,
history displays that show the true order with truthful labels, and the
history cap behaving honestly at the boundary — including telling the user
that oldest edits are gone and still fusing batch ops pushed at the cap.

Findings fixed here (#REV21):

- ``merge_history_since`` measured the batch by history *index* delta, but
  cap eviction moves the index backwards — a multi-push op (group
  duplicate, drag+fused propagation) committed at a full history was left
  unfused and Undo then restored it one entity at a time. The marker is now
  a monotonic push epoch.
- ``diff_scene_documents`` compared entities by id-set only: a reorder-only
  revision diffed empty ("同一内容") although entity order feeds the content
  hash, and changes to ``kind`` / ``semantic_bindings`` / ``operational_zones``
  produced a change record with an empty field list ("name: を変更").
- Removed-entity rows in the history diff fell back to raw entity ids
  because names were resolved only against the newer side; the summary now
  accepts the older document as a name fallback and the panel passes it.
- Restoring a revision whose content equals the current head deduped to the
  existing head while the status line claimed a new head was created.
- The bounded history evicted silently: nothing exposed how many oldest
  commands were dropped, so the edit menu could read as complete history.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

from htdt.cad_document import (
    CommandHistory,
    EditStateError,
    MoveEntityCommand,
    WorkingDocument,
)
from htdt.cad_listener_pose import CadListenerPoseRepository, build_listener_pose
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    SemanticCapabilityBinding,
    Size3,
    canonical_scene_json,
    make_f1_scene,
    scene_content_hash,
)
from htdt.cad_scene_history import diff_scene_documents, diff_summary_lines
from htdt.room_workspace import RoomWorkspaceController


def _repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _controller(tmp_path: Path) -> RoomWorkspaceController:
    return RoomWorkspaceController(_repository(tmp_path), F1_DOCUMENT_ID)


def _entity(entity_id: str, *, name: str | None = None, x: float = 4.0) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=name or entity_id,
        position=Position3(x_m=x, y_m=3.0, z_m=1.0),
        size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.4),
    )


def _seat(entity_id: str = 'seat-1') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name='Seat ' + entity_id,
        position=Position3(x_m=2.0, y_m=3.0, z_m=0.0),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
        acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
    )


def _pos(x: float) -> Position3:
    return Position3(x_m=x, y_m=3.0, z_m=1.0)


def _ids(document: SceneDocument) -> list[str]:
    return [entity.entity_id for entity in document.entities]


# --- D1: redo invalidation -------------------------------------------------


def test_new_edit_after_undo_clears_redo_stack() -> None:
    working = WorkingDocument(make_f1_scene())
    working.add_entity(_entity('a-1'))
    working.add_entity(_entity('a-2'))
    assert working.undo()
    assert working.can_redo

    assert working.add_entity(_entity('a-3'))
    assert not working.can_redo
    assert working.redo_label is None
    entries = working.history_entries()
    assert [entry.applied for entry in entries] == [True, True]
    assert 'a-2' not in _ids(working.document)
    assert _ids(working.document)[-1] == 'a-3'


def test_failed_edit_after_undo_does_not_truncate_redo() -> None:
    working = WorkingDocument(make_f1_scene())
    working.add_entity(_entity('b-1'))
    working.add_entity(_entity('b-2'))
    working.undo()

    with pytest.raises(EditStateError):
        working.add_entity(_entity('b-1'))  # duplicate id — apply fails
    assert working.can_redo  # the redo future was never forked
    assert working.redo()
    assert 'b-2' in _ids(working.document)


def test_noop_edit_after_undo_keeps_redo_stack() -> None:
    # A no-op push does not fork the timeline, so the pending redo future is
    # still reachable — honest semantics, not a silently resurrected edit.
    working = WorkingDocument(make_f1_scene())
    working.add_entity(_entity('c-1'))
    working.undo()
    current = working.document.entity('speaker-fl')
    assert not working.move_entity('speaker-fl', current.position)
    assert working.can_redo
    working.redo()
    assert 'c-1' in _ids(working.document)


def test_undo_and_redo_labels_describe_the_exact_command() -> None:
    working = WorkingDocument(make_f1_scene())
    working.move_entity('speaker-fl', _pos(9.9))
    assert working.undo_label == '移動「Front Left」'
    assert working.undo()
    assert working.redo_label == '移動「Front Left」'
    assert working.document.entity('speaker-fl').position.x_m == pytest.approx(1.35)
    assert working.redo()
    assert working.document.entity('speaker-fl').position.x_m == pytest.approx(9.9)


# --- D2: undo across boundaries --------------------------------------------


def test_undo_after_save_marks_dirty_and_redo_cleans(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.working.add_entity(_entity('d-1'))
    assert controller.save()
    controller.undo()
    assert controller.is_dirty
    controller.redo()
    assert not controller.is_dirty


def test_save_after_undo_writes_honest_new_head(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.working.add_entity(_entity('e-1'))
    controller.save()
    head_after_first_save = controller.repository.current_head(F1_DOCUMENT_ID)
    controller.undo()
    controller.save()
    head = controller.repository.current_head(F1_DOCUMENT_ID)
    assert head is not None
    assert controller.working.source_revision_id == head.revision_id
    assert head.parent_revision_id == head_after_first_save.revision_id


def test_undo_while_preview_active_raises_not_silently(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    controller.working.begin_move('speaker-fl')
    with pytest.raises(EditStateError):
        controller.working.undo()
    controller.working.cancel_preview()


def test_undo_while_recovery_pending_is_blocked_not_corrupt(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.working.add_entity(_entity('f-1'))
    controller._sync_recovery()
    snapshot = repository.recovery(F1_DOCUMENT_ID)
    assert snapshot is not None
    # Simulate the pending-recovery state the app binds into after a restart.
    controller.recovery_candidate = snapshot
    before = controller.committed_document
    assert controller.undo() is False
    assert controller.redo() is False
    assert controller.committed_document == before


def test_rebind_clears_undo_history(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    first.working.add_entity(_entity('g-1'))
    first.save()
    rebound = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert not rebound.working.can_undo
    assert not rebound.working.can_redo


# --- D3: composite ops ------------------------------------------------------


def test_group_delete_is_one_atomic_undo_step(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    for index in range(5):
        controller.working.add_entity(_entity(f'g-{index}', x=5.0 + index))
    before = canonical_scene_json(controller.committed_document)
    controller.working.delete_entities(
        tuple(f'g-{index}' for index in range(5)) + ('speaker-fl',)
    )
    assert len(controller.committed_document.entities) == len(
        make_f1_scene().entities
    ) - 1
    controller.undo()
    assert canonical_scene_json(controller.committed_document) == before


def test_merge_history_since_fuses_batch_pushed_at_the_cap() -> None:
    """Regression: cap eviction moved the index backwards mid-batch, so an
    index-delta marker computed ``count=0`` and the batch stayed unfused —
    Undo restored a group op one item at a time. The epoch marker survives
    eviction because it counts pushes, not cursor position.
    """
    working = WorkingDocument(make_f1_scene(), history_limit=3)
    for index in range(3):
        working.add_entity(_entity(f'fill-{index}', x=6.0 + index))
    assert working.history_length == 3

    marker = working.history_epoch
    working.add_entity(_entity('batch-1', x=8.0))
    working.add_entity(_entity('batch-2', x=8.5))
    assert working.merge_history_since(marker)

    working.undo()
    ids = _ids(working.document)
    assert 'batch-1' not in ids and 'batch-2' not in ids
    assert not working.can_undo or working.redo()  # single step either way


def test_merge_with_previous_survives_cap_eviction() -> None:
    """The constraint-propagation fuse (``merge_last(2)`` gated on an epoch
    check) must still fire when the driver edit pushed history past the cap.
    """
    working = WorkingDocument(make_f1_scene(), history_limit=2)
    working.add_entity(_entity('fill', x=6.0))
    working.move_entity('speaker-fl', _pos(7.0))  # driver edit (1 push)
    epoch_before = working.history_epoch
    working.add_entity(_entity('prop', x=7.5))    # propagation (1 push)
    fused = (
        working.history_epoch == epoch_before + 1
        and working.merge_last(2)
    )
    assert fused
    working.undo()
    assert 'prop' not in _ids(working.document)
    assert working.document.entity('speaker-fl').position.x_m == pytest.approx(1.35)


# --- D4: revision restore ---------------------------------------------------


def test_restore_creates_honest_new_head_with_exact_content(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    rev1 = controller.repository.current_head(F1_DOCUMENT_ID)
    controller.working.add_entity(_entity('r-1'))
    controller.save()
    rev2 = controller.repository.current_head(F1_DOCUMENT_ID)

    restored = controller.restore_revision(rev1.revision_id)
    head = controller.repository.current_head(F1_DOCUMENT_ID)
    assert head.revision_id == restored.revision_id
    assert head.revision_id != rev1.revision_id
    assert head.parent_revision_id == rev2.revision_id
    assert not restored.detached
    # Byte-for-byte: canonical payload and content hash match the original.
    assert head.content_hash == rev1.content_hash
    assert canonical_scene_json(head.document) == canonical_scene_json(rev1.document)
    # The restore itself is a commit boundary — undo history does not cross it.
    assert not controller.working.can_undo
    assert not controller.is_dirty


def test_restore_detached_revision_becomes_mainline_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    rev1 = repository.current_head(F1_DOCUMENT_ID)
    detached = repository.save_detached_revision(
        make_f1_scene().model_copy(
            update={'entities': make_f1_scene().entities + (_entity('det-1'),)}
        ),
        parent_revision_id=rev1.revision_id,
        reason='fixture',
    ).revision
    restored = controller.restore_revision(detached.revision_id)
    head = repository.current_head(F1_DOCUMENT_ID)
    assert head.revision_id == restored.revision_id
    assert 'det-1' in _ids(head.document)
    assert not head.detached


def test_restore_same_content_dedupes_to_existing_head(tmp_path: Path) -> None:
    """Restoring a different revision whose content equals the current head
    returns the existing head (no phantom new revision) — callers can detect
    it by comparing revision ids.
    """
    controller = _controller(tmp_path)
    rev1 = controller.repository.current_head(F1_DOCUMENT_ID)
    controller.working.add_entity(_entity('r-2'))
    controller.save()
    controller.restore_revision(rev1.revision_id)
    head_before = controller.repository.current_head(F1_DOCUMENT_ID)

    result = controller.restore_revision(rev1.revision_id)
    head_after = controller.repository.current_head(F1_DOCUMENT_ID)
    assert result.revision_id == head_before.revision_id == head_after.revision_id
    assert len(controller.repository.list_revisions(F1_DOCUMENT_ID)) == 3


def test_restore_does_not_touch_sidecars_but_stale_rows_stay_inert(
    tmp_path: Path,
) -> None:
    """Sidecars are document-scoped live state, not revision content: restore
    leaves them untouched, and a selection bound to an entity the restored
    document lacks is dormant — readers key off document entities — yet still
    present so it revives if a later restore brings the entity back.
    """
    repository = _repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    controller.working.add_entity(_seat('seat-1'))
    controller.save()
    seat_revision = repository.current_head(F1_DOCUMENT_ID)

    poses = CadListenerPoseRepository(repository.path, repository)
    pose = build_listener_pose(
        seat_entity_id='seat-1',
        document_id=F1_DOCUMENT_ID,
        label='upright',
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.2),
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.15),
        acoustic_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=1.1),
        provenance='fixture',
        created_at_utc='2026-01-01T00:00:00+00:00',
    )
    poses.save_pose(pose)
    poses.select_pose(F1_DOCUMENT_ID, pose)
    controller._sidecar_baseline = controller._capture_sidecars()

    controller.working.delete_entity('seat-1')
    controller.save()
    controller.restore_revision(seat_revision.revision_id)

    assert poses.selections_for_document(F1_DOCUMENT_ID).get('seat-1') == pose
    assert not controller.is_dirty  # restore leaves sidecar digest consistent


# --- D5: diff view -----------------------------------------------------------


def test_diff_flags_reorder_only_revision() -> None:
    document = make_f1_scene()
    reordered = document.model_copy(
        update={'entities': tuple(reversed(document.entities))}
    )
    assert scene_content_hash(reordered) != scene_content_hash(document)
    diff = diff_scene_documents(document, reordered)
    assert diff.entity_order_changed
    assert not diff.is_empty
    lines = diff_summary_lines(diff, reordered)
    assert 'オブジェクトの順序を変更' in lines


def test_diff_reports_kind_and_binding_and_zone_fields() -> None:
    base = _entity('x-1')
    before = SceneDocument(
        document_id='doc', room=None, entities=(base,)
    )
    rebound = base.model_copy(
        update={
            'semantic_bindings': (
                SemanticCapabilityBinding(
                    capability='acoustic_source', parameters={}
                ),
            )
        }
    )
    after = SceneDocument(document_id='doc', room=None, entities=(rebound,))
    diff = diff_scene_documents(before, after)
    assert diff.entity_changes[0].fields == ('semantic_bindings',)
    lines = diff_summary_lines(diff, after)
    assert '機能割り当て' in lines[0]


def test_diff_removed_entity_resolves_name_from_older_side() -> None:
    before = SceneDocument(
        document_id='doc',
        room=None,
        entities=(_entity('gone-1', name='撤去した棚'),),
    )
    after = SceneDocument(document_id='doc', room=None, entities=())
    diff = diff_scene_documents(before, after)
    lines = diff_summary_lines(diff, after, fallback_document=before)
    assert '撤去した棚を削除' in lines
    # Without the fallback document the raw id remains the honest last resort.
    assert 'gone-1を削除' in diff_summary_lines(diff, after)


def test_diff_schema_only_changes_stay_empty_by_design() -> None:
    document = make_f1_scene()
    bumped = document.model_copy(update={'schema_version': 6})
    diff = diff_scene_documents(document, bumped)
    # schema_version is migration plumbing, not authored content — a bump on
    # its own is not a user-visible delta.
    assert diff.is_empty


# --- D6: history UI truthfulness ---------------------------------------------


def test_entries_order_and_applied_flags_match_real_cursor() -> None:
    working = WorkingDocument(make_f1_scene())
    working.add_entity(_entity('u-1'))
    working.add_entity(_entity('u-2'))
    working.undo()
    entries = working.history_entries()
    assert [entry.index for entry in entries] == [0, 1]
    assert [entry.applied for entry in entries] == [True, False]
    assert entries[-1].label == working.redo_label


def test_revision_listing_is_chronological(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    rev1 = controller.repository.current_head(F1_DOCUMENT_ID)
    controller.working.add_entity(_entity('h-1'))
    controller.save()
    rev2 = controller.repository.current_head(F1_DOCUMENT_ID)
    detached = controller.repository.save_detached_revision(
        make_f1_scene().model_copy(
            update={'entities': make_f1_scene().entities + (_entity('det-0'),)}
        ),
        parent_revision_id=rev1.revision_id,
        reason='fixture',
    ).revision
    revisions = controller.list_revisions()
    assert [r.revision_id for r in revisions] == [
        rev1.revision_id,
        rev2.revision_id,
        detached.revision_id,
    ]


# --- D7: limits ---------------------------------------------------------------


def test_cap_eviction_lands_undo_exactly_at_the_retained_boundary() -> None:
    history = CommandHistory(limit=3)
    document = make_f1_scene()
    for step in range(5):
        document = history.push(
            MoveEntityCommand(
                'speaker-fl', document.entity('speaker-fl').position, _pos(100.0 + step)
            ),
            document,
        )
    assert history.length == 3
    assert history.dropped == 2
    # Kept commands target 102/103/104; undoing all three must land exactly on
    # the state produced by the last evicted command (position 101).
    for _ in range(3):
        document = history.undo(document)
    assert not history.can_undo
    assert document.entity('speaker-fl').position.x_m == pytest.approx(101.0)


def test_dropped_count_and_hidden_tail_are_exposed_for_the_menu() -> None:
    working = WorkingDocument(make_f1_scene(), history_limit=3)
    for index in range(5):
        working.add_entity(_entity(f'k-{index}', x=6.0 + index))
    assert working.history_dropped == 2
    entries = working.history_entries(limit=2)
    assert len(entries) == 2
    hidden = working.history_length - len(entries)
    assert hidden == 1  # one retained command older than the shown slice
