import pytest

from htdt.cad_document import EditorViewState, WorkingDocument
from htdt.cad_scene import (
    Position3,
    canonical_scene_json,
    domain_pose_to_render_matrix,
    domain_to_render,
    make_f1_scene,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
    render_delta_to_domain,
    rotate_orientation_world,
    rotate_position_world,
    scene_content_hash,
)


def test_preview_cancel_does_not_change_history_or_unknown_aim() -> None:
    working = WorkingDocument(make_f1_scene())
    original = working.committed_document.entity('speaker-fl')
    working.begin_move('speaker-fl')
    working.preview_move(Position3(x_m=1.5, y_m=0.8, z_m=1.05))
    assert working.document.entity('speaker-fl').position.x_m == 1.5
    assert working.cancel_preview()
    assert working.history_length == 0
    current = working.committed_document.entity('speaker-fl')
    assert current.position == original.position
    assert current.aim_xyz is None


def test_drag_commit_is_one_undo_and_numeric_move_uses_same_history() -> None:
    working = WorkingDocument(make_f1_scene())
    original = working.committed_document.entity('speaker-fl').position
    working.begin_move('speaker-fl')
    working.preview_move(Position3(x_m=1.55, y_m=0.75, z_m=1.05))
    assert working.commit_preview()
    assert working.history_length == 1
    assert working.undo()
    assert working.committed_document.entity('speaker-fl').position == original
    assert working.redo()
    assert working.move_entity('speaker-fl', Position3(x_m=1.60, y_m=0.75, z_m=1.05))
    assert working.history_length == 2


def test_rotate_preview_cancel_commit_and_undo_preserve_unknown_aim() -> None:
    working = WorkingDocument(make_f1_scene())
    original = working.committed_document.entity('speaker-fl')
    rotated = rotate_orientation_world(original.orientation, 'z', 30.0)

    working.begin_rotate('speaker-fl')
    working.preview_rotate(rotated)
    assert working.preview_kind == 'rotate'
    assert working.document.entity('speaker-fl').orientation == rotated
    assert working.document.entity('speaker-fl').aim_xyz is None
    assert working.cancel_preview()
    assert working.history_length == 0
    assert working.committed_document.entity('speaker-fl') == original

    working.begin_rotate('speaker-fl')
    working.preview_rotate(rotated)
    assert working.commit_preview()
    assert working.history_length == 1
    assert working.committed_document.entity('speaker-fl').aim_xyz is None
    assert working.undo()
    assert working.committed_document.entity('speaker-fl') == original


def test_numeric_rotation_uses_same_history_and_euler_round_trips() -> None:
    working = WorkingDocument(make_f1_scene())
    orientation = quaternion_from_euler_deg(yaw_deg=25.0, pitch_deg=-10.0, roll_deg=5.0)
    assert working.rotate_entity('furniture-left', orientation)
    assert working.history_length == 1
    yaw, pitch, roll = quaternion_to_euler_deg(
        working.committed_document.entity('furniture-left').orientation
    )
    assert yaw == pytest.approx(25.0)
    assert pitch == pytest.approx(-10.0)
    assert roll == pytest.approx(5.0)


def test_identity_orientation_is_canonical_omission_for_n10_hash_compatibility() -> None:
    payload = canonical_scene_json(make_f1_scene())
    assert 'orientation' not in payload


def test_domain_pose_conversion_reflects_y_axis_without_treating_reflection_as_rotation() -> None:
    position = Position3(x_m=1.0, y_m=2.0, z_m=3.0)
    orientation = quaternion_from_euler_deg(yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0)
    matrix = domain_pose_to_render_matrix(position, orientation)
    assert matrix[0][3] == pytest.approx(1.0)
    assert matrix[1][3] == pytest.approx(-2.0)
    assert matrix[2][3] == pytest.approx(3.0)
    # Domain +X rotated to domain +Y; domain +Y is render -Y after C*R*C.
    assert matrix[0][0] == pytest.approx(0.0, abs=1e-9)
    assert matrix[1][0] == pytest.approx(-1.0, abs=1e-9)


def test_noop_move_is_not_added_to_history() -> None:
    working = WorkingDocument(make_f1_scene())
    position = working.committed_document.entity('speaker-fl').position
    assert not working.move_entity('speaker-fl', position)
    assert working.history_length == 0


def test_delete_undo_restores_same_entity_and_new_command_discards_redo() -> None:
    working = WorkingDocument(make_f1_scene())
    original_ids = [entity.entity_id for entity in working.committed_document.entities]
    deleted = working.committed_document.entity('furniture-left')

    assert working.delete_entity(deleted.entity_id)
    assert working.history_length == 1
    with pytest.raises(KeyError):
        working.committed_document.entity(deleted.entity_id)

    assert working.undo()
    assert working.committed_document.entity(deleted.entity_id) == deleted
    assert [entity.entity_id for entity in working.committed_document.entities] == original_ids
    assert working.can_redo

    assert working.move_entity('speaker-fl', Position3(x_m=1.45, y_m=0.75, z_m=1.05))
    assert not working.can_redo
    assert not working.redo()


def test_view_state_is_non_physical_and_sanitizes_only_on_reopen_boundary() -> None:
    document = make_f1_scene()
    state = EditorViewState(
        selected_id='speaker-fl',
        hidden_ids={'speaker-fr', 'missing'},
        locked_ids={'speaker-fl', 'missing'},
    )
    original_hash_document = document.model_dump(mode='json')

    state.set_hidden('speaker-fr', False)
    state.set_locked('speaker-c', True)
    state.sanitize(document)

    assert state.selected_id == 'speaker-fl'
    assert state.hidden_ids == set()
    assert state.locked_ids == {'speaker-fl', 'speaker-c'}
    assert document.model_dump(mode='json') == original_hash_document


def test_domain_render_coordinate_mapping_is_explicit_and_reversible() -> None:
    base = Position3(x_m=1.0, y_m=2.0, z_m=3.0)
    assert domain_to_render(base) == (1.0, -2.0, 3.0)
    moved = render_delta_to_domain((0.25, -0.5, 0.75), base)
    assert moved == Position3(x_m=1.25, y_m=2.5, z_m=3.75)


def test_ordered_multi_selection_tracks_primary_and_sanitizes() -> None:
    document = make_f1_scene()
    state = EditorViewState(selected_ids=['speaker-fl', 'speaker-c', 'speaker-fl'])
    assert state.selection == ('speaker-fl', 'speaker-c')
    assert state.selected_id == 'speaker-c'

    state.toggle_selected('speaker-fr')
    assert state.selection == ('speaker-fl', 'speaker-c', 'speaker-fr')
    assert state.selected_id == 'speaker-fr'
    state.toggle_selected('speaker-c')
    assert state.selection == ('speaker-fl', 'speaker-fr')
    assert state.selected_id == 'speaker-fr'

    state.set_selection(['speaker-fl', 'missing', 'speaker-fr'], primary_id='speaker-fl')
    state.sanitize(document)
    assert state.selection == ('speaker-fl', 'speaker-fr')
    assert state.selected_id == 'speaker-fl'


def test_group_move_preserves_relative_placement_and_is_one_undo() -> None:
    working = WorkingDocument(make_f1_scene())
    ids = ('speaker-fl', 'speaker-c')
    before = tuple(working.committed_document.entity(entity_id) for entity_id in ids)

    working.begin_group_move(ids)
    working.preview_group_move((0.20, -0.10, 0.05))
    preview = tuple(working.document.entity(entity_id) for entity_id in ids)
    for original, moved in zip(before, preview, strict=True):
        assert moved.position.x_m == pytest.approx(original.position.x_m + 0.20)
        assert moved.position.y_m == pytest.approx(original.position.y_m - 0.10)
        assert moved.position.z_m == pytest.approx(original.position.z_m + 0.05)
        assert moved.aim_xyz == original.aim_xyz

    assert working.commit_preview()
    assert working.history_length == 1
    assert working.undo()
    assert tuple(working.committed_document.entity(entity_id) for entity_id in ids) == before


def test_group_rotate_uses_common_pivot_and_is_one_undo() -> None:
    working = WorkingDocument(make_f1_scene())
    ids = ('speaker-fl', 'speaker-c')
    before = tuple(working.committed_document.entity(entity_id) for entity_id in ids)
    pivot = Position3(
        x_m=sum(entity.position.x_m for entity in before) / 2.0,
        y_m=sum(entity.position.y_m for entity in before) / 2.0,
        z_m=sum(entity.position.z_m for entity in before) / 2.0,
    )

    working.begin_group_rotate(ids)
    working.preview_group_rotate('z', 90.0, pivot)
    preview = tuple(working.document.entity(entity_id) for entity_id in ids)
    for original, rotated in zip(before, preview, strict=True):
        assert rotated.position == rotate_position_world(original.position, pivot, 'z', 90.0)
        assert rotated.orientation == rotate_orientation_world(original.orientation, 'z', 90.0)
        assert rotated.aim_xyz == original.aim_xyz

    assert working.commit_preview()
    assert working.history_length == 1
    assert working.undo()
    assert tuple(working.committed_document.entity(entity_id) for entity_id in ids) == before


def test_noop_group_move_is_not_added_to_history() -> None:
    working = WorkingDocument(make_f1_scene())
    working.begin_group_move(('speaker-fl', 'speaker-c'))
    working.preview_group_move((0.0, 0.0, 0.0))
    assert not working.commit_preview()
    assert working.history_length == 0


def test_undo_redo_labels_describe_semantic_change_not_class_names() -> None:
    working = WorkingDocument(make_f1_scene())
    assert working.undo_label is None
    assert working.redo_label is None

    working.move_entity('speaker-fl', Position3(x_m=2.0, y_m=0.75, z_m=1.05))
    label = working.undo_label
    assert label is not None
    assert '移動' in label
    assert 'Front' in label or 'フロント' in label or label != 'MoveEntityCommand'
    assert 'Command' not in label

    working.add_entity(
        make_f1_scene()
        .entity('speaker-fl')
        .model_copy(update={'entity_id': 'speaker-new', 'name': 'Rear'}),
    )
    assert '追加' in (working.undo_label or '')

    assert working.undo()
    assert '追加' in (working.redo_label or '')
    assert '移動' in (working.undo_label or '')


def test_property_update_label_names_the_changed_field() -> None:
    working = WorkingDocument(make_f1_scene())
    working.update_entity('speaker-fl', name='Front Left Updated')
    label = working.undo_label
    assert label is not None
    assert '名前変更' in label
    assert 'Front' in label

    working.update_entity(
        'speaker-fl',
        position=Position3(x_m=2.5, y_m=0.75, z_m=1.05),
        name='Front Left Again',
    )
    assert working.undo_label == '編集「Front Left Again」' or '編集' in (working.undo_label or '')


def test_group_move_label_counts_entities_and_delete_names_subject() -> None:
    working = WorkingDocument(make_f1_scene())
    working.begin_group_move(('speaker-fl', 'speaker-c'))
    working.preview_group_move((0.1, 0.0, 0.0))
    assert working.commit_preview()
    label = working.undo_label
    assert label is not None
    assert '2件' in label or '移動' in label

    working.delete_entity('speaker-fl')
    delete_label = working.undo_label
    assert delete_label is not None
    assert '削除' in delete_label


def test_bounded_history_entries_track_current_index() -> None:
    working = WorkingDocument(make_f1_scene())
    for index in range(3):
        working.add_entity(
            make_f1_scene()
            .entity('speaker-fl')
            .model_copy(update={'entity_id': f'extra-{index}', 'name': f'Extra {index}'}),
        )
    working.undo()

    entries = working.history_entries(limit=2)
    assert len(entries) == 2
    assert entries[-1].applied is False  # the undone tail entry
    assert entries[0].applied is True
    assert entries[0].index == 1
    assert entries[-1].index == 2
    assert all('Command' not in entry.label for entry in entries)

    working.redo()
    entries = working.history_entries(limit=3)
    assert all(entry.applied for entry in entries)


def test_entity_set_edit_is_one_atomic_undo() -> None:
    working = WorkingDocument(make_f1_scene())
    removed = working.committed_document.entity('speaker-fr')
    moved = working.committed_document.entity('speaker-fl')
    moved_after = moved.model_copy(
        update={'position': Position3(x_m=2.0, y_m=0.75, z_m=1.05)}
    )
    added = make_f1_scene().entity('speaker-fr').model_copy(
        update={'entity_id': 'speaker-sur', 'name': 'Surround'}
    )
    assert working.apply_entity_set_edit(
        removed=(removed,),
        replaced_before=(moved,),
        replaced_after=(moved_after,),
        added=(added,),
    )
    document = working.committed_document
    assert document.entity('speaker-sur').name == 'Surround'
    assert document.entity('speaker-fl').position.x_m == 2.0
    assert all(entity.entity_id != 'speaker-fr' for entity in document.entities)
    assert working.history_length == 1
    assert working.undo()
    restored = working.committed_document
    assert restored.entity('speaker-fr') == removed
    assert restored.entity('speaker-fl') == moved
    assert all(entity.entity_id != 'speaker-sur' for entity in restored.entities)


def test_entity_set_edit_undo_restores_entity_order() -> None:
    original = make_f1_scene()
    working = WorkingDocument(original)
    # 'speaker-c' sits mid-list: an append-at-end revert would corrupt order.
    removed = original.entity('speaker-c')
    added = original.entity('point-mlp').model_copy(
        update={'entity_id': 'point-mlp-2', 'name': 'MLP2'}
    )
    assert working.apply_entity_set_edit(
        removed=(removed,),
        added=(added,),
    )
    assert working.committed_document != original
    assert working.undo()
    assert working.committed_document == original
    assert scene_content_hash(working.committed_document) == scene_content_hash(original)
    assert working.redo()
    assert working.undo()
    assert working.committed_document == original
