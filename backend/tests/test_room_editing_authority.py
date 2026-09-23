"""Backend authority tests for the G1 room-editing batch (#480/#481/#482/#485/#486/#491/#530)."""

from __future__ import annotations

import pytest

from htdt.cad_constraint_authoring import (
    add_constraint,
    constraint_entity_ids,
    make_allowed_region_constraint,
    make_pair_distance_constraint,
    make_walkway_constraint,
    remove_constraint,
)
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_document import EditorViewState, WorkingDocument
from htdt.cad_measure import (
    build_angle_result,
    build_distance_result,
    format_measure_result,
    measure_angle_deg,
    measure_components,
    MeasureEndpoint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneEntity, Size3, make_f1_scene
from htdt.cad_scene_history import diff_scene_documents, summarize_revision
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_video_workspace import (
    CadVideoWorkspaceRepository,
    VideoGeometryWorkspace,
    video_workspace_missing_inputs,
)
from htdt.cad_document import EditStateError
from htdt.optimization_search_domain import (
    axis_cardinality,
    cardinality_summary,
    search_cardinality,
)


def _endpoint(entity_id: str | None, x: float, y: float, z: float) -> MeasureEndpoint:
    return MeasureEndpoint(
        position=Position3(x_m=x, y_m=y, z_m=z),
        entity_id=entity_id,
        entity_name=entity_id,
        reference_kind='entity_center',
        reference_label='中心',
    )


# -- #482: Undo-safe batch delete -------------------------------------------------


def test_delete_entities_is_one_undo_and_restores_exact_order() -> None:
    working = WorkingDocument(make_f1_scene())
    original_ids = [entity.entity_id for entity in working.committed_document.entities]
    assert working.delete_entities(('speaker-fl', 'speaker-fr'))
    remaining = [entity.entity_id for entity in working.committed_document.entities]
    assert 'speaker-fl' not in remaining and 'speaker-fr' not in remaining
    assert working.history_length == 1
    assert working.undo()
    assert [entity.entity_id for entity in working.committed_document.entities] == original_ids


def test_delete_entities_rejects_missing_and_preview() -> None:
    working = WorkingDocument(make_f1_scene())
    with pytest.raises(EditStateError):
        working.delete_entities(('no-such-entity',))
    working.begin_move('speaker-fl')
    with pytest.raises(EditStateError):
        working.delete_entities(('speaker-fl',))
    working.cancel_preview()


def test_update_entities_patches_as_one_atomic_undo() -> None:
    working = WorkingDocument(make_f1_scene())
    assert working.update_entities(
        {
            'speaker-fl': {'name': 'FL Renamed'},
            'speaker-c': {'name': 'C Renamed'},
        }
    )
    assert working.committed_document.entity('speaker-fl').name == 'FL Renamed'
    assert working.committed_document.entity('speaker-c').name == 'C Renamed'
    assert working.history_length == 1
    assert working.undo()
    assert working.committed_document.entity('speaker-fl').name == 'Front Left'
    with pytest.raises(EditStateError):
        working.update_entities({'speaker-fl': {'entity_id': 'renamed-id'}})


# -- #480: ordered multi-selection -------------------------------------------------


def test_selection_order_and_primary_follow_last_pick() -> None:
    state = EditorViewState()
    state.set_selection(('a', 'b', 'c'))
    assert state.selection == ('a', 'b', 'c')
    assert state.selected_id == 'c'
    state.set_selection(('a', 'b', 'c'), primary_id='a')
    assert state.selected_id == 'a'
    state.toggle_selected('b')
    assert state.selection == ('a', 'c')
    assert state.selected_id == 'c'
    state.toggle_selected('d')
    assert state.selection == ('a', 'c', 'd')
    assert state.selected_id == 'd'


def test_hidden_and_locked_sets_toggle_and_sanitize(tmp_path) -> None:
    state = EditorViewState()
    state.set_hidden('x', True)
    state.set_locked('x', True)
    assert state.is_hidden('x') and state.is_locked('x')
    state.set_hidden('x', False)
    assert not state.is_hidden('x') and state.is_locked('x')
    document = make_f1_scene()
    state.set_selection(('speaker-fl', 'ghost'))
    state.set_hidden('ghost', True)
    state.sanitize(document)
    assert 'ghost' not in state.selected_ids
    assert not state.is_hidden('ghost')


# -- #481: snap preference persistence ---------------------------------------------


def test_view_state_persists_snap_preferences(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    document_id = "doc-1"
    repository.save_view_state(
        document_id,
        selected_id=None,
        hidden_ids=set(),
        locked_ids=set(),
        object_snap_enabled=False,
        grid_snap_enabled=True,
        grid_step_m=0.10,
        angle_snap_enabled=True,
        angle_step_deg=22.5,
    )
    record = repository.view_state(document_id)
    assert record is not None
    assert record.object_snap_enabled is False
    assert record.grid_snap_enabled is True
    assert record.grid_step_m == pytest.approx(0.10)
    assert record.angle_snap_enabled is True
    assert record.angle_step_deg == pytest.approx(22.5)


def test_view_state_without_snap_kwargs_keeps_defaults(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save_view_state(
        "doc-2",
        selected_id=None,
        hidden_ids=set(),
        locked_ids=set(),
    )
    record = repository.view_state("doc-2")
    assert record is not None
    assert record.object_snap_enabled is True
    assert record.grid_snap_enabled is False
    assert record.grid_step_m == pytest.approx(0.05)
    assert record.angle_step_deg == pytest.approx(15.0)


# -- #485: revision labels + diff ----------------------------------------------------


def test_revision_labels_roundtrip(tmp_path) -> None:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    saved = repository.save(make_f1_scene(), parent_revision_id=None)
    revision_id = saved.revision.revision_id
    repository.set_revision_label(revision_id, label="ベースライン", note="v1")
    labels = repository.revision_labels(saved.revision.document_id)
    assert labels[revision_id].label == "ベースライン"
    assert labels[revision_id].note == "v1"
    repository.set_revision_label(revision_id, label="ベースライン2", note="")
    labels = repository.revision_labels(saved.revision.document_id)
    assert labels[revision_id].label == "ベースライン2"
    repository.clear_revision_label(revision_id)
    assert repository.revision_labels(saved.revision.document_id) == {}


def test_scene_diff_reports_added_removed_and_changed() -> None:
    before = make_f1_scene()
    working = WorkingDocument(before)
    after_document = working.committed_document.model_copy(
        update={
            'entities': tuple(
                entity
                for entity in working.committed_document.entities
                if entity.entity_id != 'speaker-fr'
            )
            + (
                working.committed_document.entity('speaker-c').model_copy(
                    update={'name': 'Center Renamed'}
                ),
                SceneEntity(
                    entity_id='screen-1',
                    kind='screen',
                    name='Screen',
                    position=Position3(x_m=0.0, y_m=5.0, z_m=1.2),
                    size_m=Size3(x_m=2.0, y_m=0.1, z_m=1.1),
                ),
            )
        }
    )
    diff = diff_scene_documents(before, after_document)
    assert not diff.is_empty
    assert 'screen-1' in diff.added_entity_ids
    assert 'speaker-fr' in diff.removed_entity_ids
    changed = {change.entity_id: change.fields for change in diff.entity_changes}
    assert changed.get('speaker-c') == ('name',)
    empty = diff_scene_documents(before, before)
    assert empty.is_empty
    summary = summarize_revision(after_document)
    assert summary.entity_count == len(after_document.entities)


# -- #486: constraint authoring helpers ---------------------------------------------


def test_constraint_authoring_produces_valid_placement_constraints() -> None:
    document = make_f1_scene()
    constraint_set = CadConstraintSet(document_id=document.document_id)
    walkway = make_walkway_constraint(document, 'speaker-fl')
    allowed = make_allowed_region_constraint(document, 'speaker-c')
    pair = make_pair_distance_constraint(document, 'speaker-fl', 'speaker-fr', min_m=1.5)
    for constraint in (walkway, allowed, pair):
        constraint_set = add_constraint(constraint_set, constraint)
    assert len(constraint_set.constraints) == 3
    assert 'speaker-fl' in constraint_entity_ids(pair)
    assert 'speaker-fr' in constraint_entity_ids(pair)
    updated = remove_constraint(constraint_set, walkway.constraint_id)
    assert len(updated.constraints) == 2
    assert all(item.constraint_id != walkway.constraint_id for item in updated.constraints)


# -- #491: measurement ------------------------------------------------------------


def test_distance_measurement_components_and_azimuth() -> None:
    start = Position3(x_m=0.0, y_m=0.0, z_m=0.0)
    end = Position3(x_m=3.0, y_m=4.0, z_m=0.0)
    result = measure_components(start, end)
    assert result.distance_m == pytest.approx(5.0)
    assert result.horizontal_m == pytest.approx(5.0)
    assert result.dx_m == pytest.approx(3.0)
    assert result.dy_m == pytest.approx(4.0)
    # Canonical aim convention: 0° azimuth = +Y toward room rear.
    assert result.azimuth_deg == pytest.approx(36.87, abs=0.01)
    assert result.elevation_deg == pytest.approx(0.0)
    elevated = measure_components(start, Position3(x_m=0.0, y_m=0.0, z_m=2.0))
    assert elevated.elevation_deg == pytest.approx(90.0)


def test_angle_measurement_and_format() -> None:
    a = Position3(x_m=1.0, y_m=0.0, z_m=0.0)
    v = Position3(x_m=0.0, y_m=0.0, z_m=0.0)
    b = Position3(x_m=0.0, y_m=1.0, z_m=0.0)
    assert measure_angle_deg(a, v, b) == pytest.approx(90.0)
    distance = build_distance_result(_endpoint('e1', 0, 0, 0), _endpoint('e2', 0, 3, 0))
    assert distance.distance_m == pytest.approx(3.0)
    angle = build_angle_result(_endpoint('a', 1, 0, 0), _endpoint('v', 0, 0, 0), _endpoint('b', 0, 1, 0))
    assert angle.angle_deg == pytest.approx(90.0)
    assert '角度' in format_measure_result(angle)
    assert '距離' in format_measure_result(distance)
    with pytest.raises(ValueError):
        measure_angle_deg(v, v, b)


# -- #530: search cardinality ------------------------------------------------------


def test_search_cardinality_counts_exact_grid() -> None:
    axis = CadSearchAxis(entity_id='speaker-fl', axis='x', min_m=-0.5, max_m=0.5, step_m=0.25)
    assert axis_cardinality(axis) == 5
    axis2 = CadSearchAxis(entity_id='speaker-fl', axis='z', min_m=0.0, max_m=0.4, step_m=0.2)
    assert search_cardinality((axis, axis2)) == 15
    summary = cardinality_summary((axis, axis2))
    assert '15' in summary
    assert search_cardinality(()) == 1
    degenerate = CadSearchAxis(entity_id='e', axis='x', min_m=0.5, max_m=0.5, step_m=0.1)
    assert axis_cardinality(degenerate) == 1


# -- #455: video workspace state ----------------------------------------------------


def test_video_workspace_reports_missing_inputs_and_roundtrips(tmp_path) -> None:
    document = make_f1_scene()
    workspace = VideoGeometryWorkspace(document_id=document.document_id)
    missing = video_workspace_missing_inputs(document, workspace)
    assert 'プロジェクター未選択' in missing
    assert 'プロジェクター仕様が未バインドです' in missing
    assert 'スクリーンの画素設定が未バインドです' in missing

    repository = CadVideoWorkspaceRepository(tmp_path / "video.sqlite3")
    bound = VideoGeometryWorkspace(
        document_id=document.document_id,
        projector_entity_id='proj-1',
        projector_specification_sha256='abc123',
    )
    repository.save(bound)
    loaded = repository.load(document.document_id)
    assert loaded.projector_entity_id == 'proj-1'
    missing = video_workspace_missing_inputs(document, loaded)
    assert 'プロジェクター参照が無効です' in missing  # proj-1 is not a projector entity
