from __future__ import annotations

import json

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_predictions import exact_rectangular_room_frame
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim import (
    CadRoomSimBinding,
    CadRoomSimSourceBinding,
    build_cad_roomsim_batch_request,
)
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)
from htdt.cad_search_models import (
    CAD_SEARCH_SCHEMA_VERSION,
    CadCandidate,
    CadSearchAxis,
    CadSearchSpec,
    canonical_search_json,
    canonical_search_sha256,
    constraint_workspace_snapshot,
)


def _shifted_scene(*, source_has_reference: bool = True) -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=10.0, y_m=20.0),
            RoomVertex(vertex_id='b', x_m=14.0, y_m=20.0),
            RoomVertex(vertex_id='c', x_m=14.0, y_m=25.0),
            RoomVertex(vertex_id='d', x_m=10.0, y_m=25.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id='roomsim-native-fixture',
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=11.0, y_m=21.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                acoustic_reference_offset_m=(
                    Offset3(x_m=0.1, y_m=0.0, z_m=0.0)
                    if source_has_reference else None
                ),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=12.0, y_m=23.0, z_m=1.1),
            ),
        ),
    )


def _spec(revision) -> CadSearchSpec:
    constraint = CadConstraintSet(document_id=revision.document_id, constraints=())
    snapshot_json, snapshot_hash = constraint_workspace_snapshot(constraint)
    engine = {}
    engine_json = canonical_search_json(engine)
    engine_hash = canonical_search_sha256(engine)
    axis = CadSearchAxis(
        entity_id='speaker-fl',
        axis='x',
        min_m=11.0,
        max_m=12.0,
        step_m=0.5,
    )
    provisional = CadSearchSpec.model_construct(
        schema_version=CAD_SEARCH_SCHEMA_VERSION,
        search_spec_id='roomsim-search',
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash=snapshot_hash,
        constraint_snapshot_json=snapshot_json,
        constraint_engine_spec_json=engine_json,
        constraint_engine_spec_sha256=engine_hash,
        axes=(axis,),
        candidate_limit=10,
        o10_spec_json='{}',
        search_spec_sha256='0' * 64,
        created_at_utc='2026-09-18T00:00:00+00:00',
    )
    return CadSearchSpec(
        **provisional.model_dump(exclude={'search_spec_sha256'}),
        search_spec_sha256=canonical_search_sha256(provisional.identity_payload()),
    )


def _binding() -> CadRoomSimBinding:
    return CadRoomSimBinding(
        receiver_entity_id='point-mlp',
        sources=(
            CadRoomSimSourceBinding(entity_id='speaker-fl', rew_source_name='Left'),
        ),
        response_source_name='Left',
    )


def test_native_roomsim_adapter_uses_shifted_rectangle_local_coordinates_and_acoustic_reference(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_shifted_scene(), parent_revision_id=None).revision
    spec = _spec(revision)
    candidate = CadCandidate(
        candidate_id='candidate-a',
        raw_index=0,
        feasible_index=0,
        positions={
            'speaker-fl': {'x_m': 11.5, 'y_m': 21.0, 'z_m': 1.0},
        },
    )

    request = build_cad_roomsim_batch_request(revision, spec, candidate, _binding())

    assert request.room_width_m == pytest.approx(4.0)
    assert request.room_depth_m == pytest.approx(5.0)
    assert request.room_height_m == pytest.approx(2.4)
    assert request.head_position_htdt == pytest.approx({'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.1})
    # Candidate moves the body centre to 11.5 m; explicit +0.1 m acoustic offset is preserved.
    assert request.source_positions_htdt['Left'] == pytest.approx(
        {'x_m': 1.6, 'y_m': 1.0, 'z_m': 1.0}
    )


def test_native_roomsim_adapter_rejects_speaker_without_explicit_acoustic_reference(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(
        _shifted_scene(source_has_reference=False),
        parent_revision_id=None,
    ).revision
    spec = _spec(revision)
    candidate = CadCandidate(
        candidate_id='candidate-a',
        raw_index=0,
        feasible_index=0,
        positions={'speaker-fl': {'x_m': 11.5, 'y_m': 21.0, 'z_m': 1.0}},
    )

    with pytest.raises(ValueError, match='no explicit acoustic reference'):
        build_cad_roomsim_batch_request(revision, spec, candidate, _binding())


def test_native_roomsim_adapter_rejects_unbound_moved_speaker(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene = _shifted_scene()
    extra = SceneEntity(
        entity_id='speaker-fr',
        kind='speaker',
        name='FR',
        speaker_role='FR',
        position=Position3(x_m=13.0, y_m=21.0, z_m=1.0),
        size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
        acoustic_reference_offset_m=Offset3(),
    )
    scene = scene.model_copy(update={'entities': scene.entities + (extra,)})
    revision = repository.save(scene, parent_revision_id=None).revision
    spec = _spec(revision)
    candidate = CadCandidate(
        candidate_id='candidate-a',
        raw_index=0,
        feasible_index=0,
        positions={'speaker-fr': {'x_m': 12.5, 'y_m': 21.0, 'z_m': 1.0}},
    )

    with pytest.raises(ValueError, match='without a Room Simulator binding'):
        build_cad_roomsim_batch_request(revision, spec, candidate, _binding())


def test_native_roomsim_adapter_rejects_non_rectangular_room(tmp_path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene = _shifted_scene()
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
    scene = scene.model_copy(update={'room': room})
    revision = repository.save(scene, parent_revision_id=None).revision
    spec = _spec(revision)
    candidate = CadCandidate(
        candidate_id='candidate-a',
        raw_index=0,
        feasible_index=0,
        positions={'speaker-fl': {'x_m': 11.5, 'y_m': 21.0, 'z_m': 1.0}},
    )

    assert exact_rectangular_room_frame(room) is None
    with pytest.raises(ValueError, match='requires an axis-aligned rectangular room'):
        build_cad_roomsim_batch_request(revision, spec, candidate, _binding())
