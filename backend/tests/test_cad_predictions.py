from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_prediction_jobs import PredictionJobApplyContext, PredictionJobGuard
from htdt.cad_prediction_models import canonical_prediction_json, prediction_input_hash
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_predictions import (
    RECTANGULAR_GEOMETRY_MODEL_ID,
    RECTANGULAR_GEOMETRY_MODEL_VERSION,
    analyze_native_rectangular_geometry,
    exact_rectangular_room_frame,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)


def _speaker(entity_id: str, x_m: float, y_m: float, z_m: float = 1.0) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
        acoustic_reference_offset_m=Offset3(),
        speaker_role='FL',
    )


def _shifted_rect_scene(document_id: str = 'prediction-rect') -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=10.0, y_m=20.0),
            RoomVertex(vertex_id='b', x_m=16.0, y_m=20.0),
            RoomVertex(vertex_id='c', x_m=16.0, y_m=24.0),
            RoomVertex(vertex_id='d', x_m=10.0, y_m=24.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            _speaker('speaker-fl', 11.4, 20.8, 1.05),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=13.0, y_m=23.0, z_m=1.1),
            ),
        ),
    )


def _l_scene(document_id: str = 'prediction-l') -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id='e', x_m=4.0, y_m=2.0),
            RoomVertex(vertex_id='f', x_m=2.0, y_m=2.0),
            RoomVertex(vertex_id='g', x_m=2.0, y_m=4.0),
            RoomVertex(vertex_id='h', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            _speaker('speaker-fl', 1.0, 0.8),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=1.0, y_m=1.5, z_m=1.1),
            ),
        ),
    )


def _saved(tmp_path: Path, scene: SceneDocument):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(scene, parent_revision_id=None).revision
    return repository, revision


def test_shifted_axis_aligned_rectangle_is_exact_and_reflections_return_world_coordinates(tmp_path: Path) -> None:
    _, revision = _saved(tmp_path, _shifted_rect_scene())

    frame = exact_rectangular_room_frame(revision.document.room)
    assert frame is not None
    assert (frame.origin_x_m, frame.origin_y_m, frame.width_m, frame.depth_m) == (10.0, 20.0, 6.0, 4.0)

    modes, reflections = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=120.0)

    assert modes.run_id == reflections.run_id
    assert modes.model_id == RECTANGULAR_GEOMETRY_MODEL_ID
    assert modes.model_version == RECTANGULAR_GEOMETRY_MODEL_VERSION
    assert modes.geometry_compatibility == 'exact_for_model_geometry'
    assert reflections.geometry_compatibility == 'exact_for_model_geometry'
    assert modes.modes
    assert len(reflections.reflections) == 6
    assert not reflections.warnings
    assert all(10.0 <= item.reflection_position.x_m <= 16.0 for item in reflections.reflections)
    assert all(20.0 <= item.reflection_position.y_m <= 24.0 for item in reflections.reflections)
    assert {item.surface_key for item in reflections.reflections} == {
        'left_x0', 'right_xW', 'front_y0', 'rear_yD', 'floor_z0', 'ceiling_zH'
    }
    snapshot = json.loads(reflections.input_snapshot_json)
    assert snapshot['room_frame']['origin_x_m'] == 10.0
    assert snapshot['room_frame']['origin_y_m'] == 20.0
    assert snapshot['approximation_rule'] is None


def test_nonrectangular_room_is_explicitly_unsupported_not_silently_approximated(tmp_path: Path) -> None:
    _, revision = _saved(tmp_path, _l_scene())

    assert exact_rectangular_room_frame(revision.document.room) is None
    modes, reflections = analyze_native_rectangular_geometry(revision, 'point-mlp')

    assert modes.geometry_compatibility == 'unsupported'
    assert reflections.geometry_compatibility == 'unsupported'
    assert modes.modes == ()
    assert reflections.reflections == ()
    assert modes.warnings == ('rectangular_geometry_model_requires_axis_aligned_rectangular_room',)
    snapshot = json.loads(modes.input_snapshot_json)
    assert snapshot['approximation_rule'] is None
    assert len(snapshot['room']['footprint_vertices']) == 8


def test_prediction_repository_round_trip_preserves_exact_revision_model_and_input(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    results = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)

    for result in results:
        prediction_repository.save(result)

    assert prediction_repository.list_results(revision.document_id) == results
    assert prediction_repository.list_run(results[0].run_id) == results
    assert prediction_repository.get(results[0].prediction_id) == results[0]

    wrong_hash = results[0].model_copy(update={'scene_content_hash': '0' * 64})
    with pytest.raises(ValueError, match='content hash'):
        prediction_repository.save(wrong_hash)


def _resigned(result, snapshot: dict):
    """Fabricate a result whose recomputed input hash matches a forged snapshot."""

    snapshot_json = canonical_prediction_json(snapshot)
    return result.model_copy(
        update={
            'input_snapshot_json': snapshot_json,
            'input_hash': prediction_input_hash(snapshot_json),
        }
    )


def test_prediction_repository_rejects_fabricated_input_snapshot(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    result = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)[0]
    honest_snapshot = json.loads(result.input_snapshot_json)

    mutations = []

    altered_room = json.loads(result.input_snapshot_json)
    altered_room['room_frame']['width_m'] = 99.0
    mutations.append(altered_room)

    altered_receiver = json.loads(result.input_snapshot_json)
    altered_receiver['receiver_position']['x_m'] = 11.0
    mutations.append(altered_receiver)

    omitted_speaker = json.loads(result.input_snapshot_json)
    omitted_speaker['speakers'] = []
    mutations.append(omitted_speaker)

    added_speaker = json.loads(result.input_snapshot_json)
    added_speaker['speakers'] = added_speaker['speakers'] + [
        {
            'entity_id': 'speaker-sub',
            'speaker_role': 'SUB',
            'acoustic_reference_position': {'x_m': 12.0, 'y_m': 21.0, 'z_m': 0.4},
        }
    ]
    mutations.append(added_speaker)

    altered_role = json.loads(result.input_snapshot_json)
    altered_role['speakers'][0]['speaker_role'] = 'SUB'
    mutations.append(altered_role)

    altered_surface = json.loads(result.input_snapshot_json)
    altered_surface['surface_identities']['left_x0'] = 'wall:forged'
    mutations.append(altered_surface)

    for snapshot in mutations:
        assert snapshot != honest_snapshot
        with pytest.raises(ValueError, match='canonical model request'):
            prediction_repository.save(_resigned(result, snapshot))

    assert prediction_repository.list_results(revision.document_id) == ()


def test_prediction_repository_rejects_foreign_receiver_entity(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    result = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)[0]

    wrong_receiver = json.loads(result.input_snapshot_json)
    wrong_receiver['receiver_entity_id'] = 'speaker-fl'
    with pytest.raises(ValueError, match='canonical model request'):
        prediction_repository.save(_resigned(result, wrong_receiver))

    unknown_receiver = json.loads(result.input_snapshot_json)
    unknown_receiver['receiver_entity_id'] = 'ghost-receiver'
    with pytest.raises(ValueError, match='not part of the source revision'):
        prediction_repository.save(_resigned(result, unknown_receiver))


def test_prediction_repository_rejects_parameters_outside_model_contract(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    result = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)[0]

    invalid_parameters = (
        {'max_mode_hz': 150.0},  # missing key
        {'max_mode_hz': 150.0, 'sound_speed_m_s': 343.0, 'extra': 1},  # unexpected key
        {'max_mode_hz': -150.0, 'sound_speed_m_s': 343.0},  # non-positive
        {'max_mode_hz': 150.0, 'sound_speed_m_s': 0.0},  # non-positive
        {'max_mode_hz': '150', 'sound_speed_m_s': 343.0},  # non-numeric
        {'max_mode_hz': True, 'sound_speed_m_s': 343.0},  # bool is not a parameter
        [150.0, 343.0],  # not an object
    )
    for parameters in invalid_parameters:
        forged = result.model_copy(
            update={'parameters_json': canonical_prediction_json(parameters)}
        )
        with pytest.raises(ValueError, match='rectangular model contract'):
            prediction_repository.save(forged)

    # A non-canonical spelling of valid values is not the pinned model request.
    forged = result.model_copy(
        update={'parameters_json': '{"max_mode_hz":150,"sound_speed_m_s":343.0}'}
    )
    with pytest.raises(ValueError, match='canonical model request'):
        prediction_repository.save(forged)


def test_prediction_repository_rejects_unsupported_model_version_and_compatibility(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    result = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)[0]

    unknown_version = result.model_copy(update={'model_version': 'rect-room-geometry-0'})
    with pytest.raises(ValueError, match='no registered input authority'):
        prediction_repository.save(unknown_version)

    unknown_model = result.model_copy(update={'model_id': 'htdt.unknown_model'})
    with pytest.raises(ValueError, match='no registered input authority'):
        prediction_repository.save(unknown_model)

    # Geometry compatibility is input-derived; a claimed approximation for an
    # exactly rectangular room cannot be the canonical classification.
    forged = result.model_copy(update={'geometry_compatibility': 'rectangular_approximation'})
    with pytest.raises(ValueError, match='canonical classification'):
        prediction_repository.save(forged)


def test_prediction_repository_reads_replay_canonical_input(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    results = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)
    for result in results:
        prediction_repository.save(result)

    assert prediction_repository.list_results(revision.document_id) == results

    forged = json.loads(results[0].input_snapshot_json)
    forged['receiver_position'] = {'x_m': 11.0, 'y_m': 21.0, 'z_m': 1.1}
    forged_json = canonical_prediction_json(forged)
    with sqlite3.connect(prediction_repository.path) as connection:
        connection.execute(
            'UPDATE cad_prediction_results SET input_snapshot_json=?, input_hash=? '
            'WHERE prediction_id=?',
            (forged_json, prediction_input_hash(forged_json), results[0].prediction_id),
        )

    with pytest.raises(ValueError, match='canonical model request'):
        prediction_repository.get(results[0].prediction_id)
    with pytest.raises(ValueError, match='canonical model request'):
        prediction_repository.list_results(revision.document_id)
    with pytest.raises(ValueError, match='canonical model request'):
        prediction_repository.list_run(results[0].run_id)


def test_prediction_repository_reads_reject_coherent_row_rewrite(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _shifted_rect_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    results = analyze_native_rectangular_geometry(revision, 'point-mlp', max_mode_hz=150.0)
    for result in results:
        prediction_repository.save(result)

    with sqlite3.connect(prediction_repository.path) as connection:
        connection.execute(
            'UPDATE cad_prediction_results SET model_version=? WHERE prediction_id=?',
            ('rect-room-geometry-0', results[0].prediction_id),
        )
    with pytest.raises(ValueError, match='no registered input authority'):
        prediction_repository.get(results[0].prediction_id)

    with sqlite3.connect(prediction_repository.path) as connection:
        connection.execute(
            'UPDATE cad_prediction_results SET model_version=?, scene_content_hash=? '
            'WHERE prediction_id=?',
            (RECTANGULAR_GEOMETRY_MODEL_VERSION, '0' * 64, results[0].prediction_id),
        )
    with pytest.raises(ValueError, match='content hash'):
        prediction_repository.get(results[0].prediction_id)


def test_prediction_repository_round_trips_unsupported_room_result(tmp_path: Path) -> None:
    scene_repository, revision = _saved(tmp_path, _l_scene())
    prediction_repository = CadPredictionRepository(scene_repository)
    results = analyze_native_rectangular_geometry(revision, 'point-mlp')

    for result in results:
        prediction_repository.save(result)

    assert prediction_repository.list_results(revision.document_id) == results
    assert prediction_repository.list_run(results[0].run_id) == results


def test_prediction_job_guard_rejects_superseded_cancelled_revision_and_constraint_stale_results(tmp_path: Path) -> None:
    _, revision = _saved(tmp_path, _shifted_rect_scene())
    guard = PredictionJobGuard()
    active = PredictionJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='1' * 64,
    )

    first = guard.submit(
        revision,
        model_id=RECTANGULAR_GEOMETRY_MODEL_ID,
        model_version=RECTANGULAR_GEOMETRY_MODEL_VERSION,
        parameters_json='{}',
        input_hash='a' * 64,
        constraint_workspace_hash='1' * 64,
    )
    assert guard.can_apply(first, active)

    second = guard.submit(
        revision,
        model_id=RECTANGULAR_GEOMETRY_MODEL_ID,
        model_version=RECTANGULAR_GEOMETRY_MODEL_VERSION,
        parameters_json='{}',
        input_hash='b' * 64,
        constraint_workspace_hash='1' * 64,
    )
    assert not guard.can_apply(first, active)
    assert guard.can_apply(second, active)

    guard.cancel(second)
    assert guard.is_cancelled(second)
    assert not guard.can_apply(second, active)

    third = guard.submit(
        revision,
        model_id=RECTANGULAR_GEOMETRY_MODEL_ID,
        model_version=RECTANGULAR_GEOMETRY_MODEL_VERSION,
        parameters_json='{}',
        input_hash='c' * 64,
        constraint_workspace_hash='1' * 64,
    )
    stale_revision = PredictionJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id='later-revision',
        scene_content_hash='f' * 64,
        constraint_workspace_hash='1' * 64,
    )
    stale_constraint = PredictionJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='2' * 64,
    )
    other_document = PredictionJobApplyContext(
        document_id='other-document',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash='1' * 64,
    )
    assert not guard.can_apply(third, stale_revision)
    assert not guard.can_apply(third, stale_constraint)
    assert not guard.can_apply(third, other_document)
    assert guard.can_apply(third, active)
