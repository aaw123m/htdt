from __future__ import annotations

from pathlib import Path

from htdt.cad_prediction_request import rectangular_geometry_request_identity
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='prediction-request',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def test_request_identity_matches_adapter_output_exactly(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision

    identity = rectangular_geometry_request_identity(
        revision,
        'point-mlp',
        max_mode_hz=140.0,
        sound_speed_m_s=342.5,
    )
    results = analyze_native_rectangular_geometry(
        revision,
        'point-mlp',
        max_mode_hz=140.0,
        sound_speed_m_s=342.5,
        constraint_workspace_hash='1' * 64,
    )

    assert identity.geometry_compatibility == 'exact_for_model_geometry'
    assert all(item.model_id == identity.model_id for item in results)
    assert all(item.model_version == identity.model_version for item in results)
    assert all(item.parameters_json == identity.parameters_json for item in results)
    assert all(item.input_snapshot_json == identity.input_snapshot_json for item in results)
    assert all(item.input_hash == identity.input_hash for item in results)


def _seat_scene() -> SceneDocument:
    return SceneDocument(
        document_id='prediction-request-seat',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='seat-1',
                kind='seat',
                name='Seat 1',
                position=Position3(x_m=3.0, y_m=3.0, z_m=0.45),
                size_m=Size3(x_m=0.6, y_m=0.7, z_m=0.8),
                acoustic_reference_offset_m=Offset3(z_m=0.65),
            ),
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
        ),
    )


def test_selected_listener_pose_binds_request_identity(tmp_path: Path) -> None:
    from json import loads

    from htdt.cad_listener_pose import build_listener_pose
    from htdt.cad_prediction_request import verify_prediction_input

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_seat_scene(), parent_revision_id=None).revision

    upright = build_listener_pose(
        seat_entity_id='seat-1',
        label='Upright',
        head_center_offset_local_m=Offset3(z_m=1.15),
        eye_reference_offset_local_m=Offset3(z_m=1.1),
        acoustic_reference_offset_local_m=Offset3(z_m=0.65),
        posture_kind='upright',
        provenance='test',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )
    reclined = build_listener_pose(
        seat_entity_id='seat-1',
        label='Reclined',
        head_center_offset_local_m=Offset3(y_m=-0.12, z_m=1.0),
        eye_reference_offset_local_m=Offset3(y_m=-0.12, z_m=0.95),
        acoustic_reference_offset_local_m=Offset3(y_m=-0.12, z_m=0.5),
        posture_kind='reclined',
        provenance='test',
        created_at_utc='2026-09-25T00:00:00+00:00',
    )

    legacy = rectangular_geometry_request_identity(revision, 'seat-1')
    upright_identity = rectangular_geometry_request_identity(
        revision, 'seat-1', listener_pose=upright
    )
    reclined_identity = rectangular_geometry_request_identity(
        revision, 'seat-1', listener_pose=reclined
    )

    # Two poses on one unchanged seat produce distinct exact identities —
    # and neither equals the legacy seat-offset fallback.
    assert len(
        {legacy.input_hash, upright_identity.input_hash,
         reclined_identity.input_hash}
    ) == 3

    snapshot = loads(upright_identity.input_snapshot_json)
    pose_block = snapshot['listener_pose']
    assert pose_block['authority']['pose_id'] == upright.pose_id
    assert pose_block['authority']['semantic_sha256'] == (
        upright.semantic_sha256
    )
    assert pose_block['receiver_resolution'] == 'listener_pose'
    assert pose_block['orientation_semantics'] == (
        'scalar_point_receiver_ignores_orientation'
    )
    assert 'listener_pose' not in loads(legacy.input_snapshot_json)

    # The pose's offset — not the seat's legacy offset — is what resolved.
    reclined_snapshot = loads(reclined_identity.input_snapshot_json)
    assert reclined_snapshot['receiver_position'] != (
        snapshot['receiver_position']
    )

    # Replay revalidates the pose-bound request byte-exactly (and fails
    # closed on a tampered receiver instead of trusting stored payload).
    verified = verify_prediction_input(
        revision,
        model_id=upright_identity.model_id,
        model_version=upright_identity.model_version,
        parameters_json=upright_identity.parameters_json,
        input_snapshot_json=upright_identity.input_snapshot_json,
        input_hash=upright_identity.input_hash,
        geometry_compatibility=upright_identity.geometry_compatibility,
    )
    assert verified.input_hash == upright_identity.input_hash


def _seat_scene_with_opening() -> SceneDocument:
    from htdt.cad_scene import RoomVertex, make_polygon_room
    from htdt.cad_wall_models import WallOpening
    from htdt.cad_walls import add_opening, make_wall_topology

    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='door-1',
            wall_id='wall:a->b',
            offset_m=1.0,
            width_m=0.9,
            height_m=2.0,
            kind='door',
            is_open=False,
        ),
    )
    return SceneDocument(
        document_id='prediction-request-state',
        schema_version=3,
        room=room,
        entities=(
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
        wall_topology=topology,
    )


def test_operating_state_binds_request_identity(tmp_path: Path) -> None:
    from json import loads

    from htdt.cad_prediction_request import verify_prediction_input
    from htdt.cad_room_operating_state import (
        CurtainState,
        HvacState,
        OperatingOpeningState,
        build_operating_state,
        compile_operating_state_consumption,
    )

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(
        _seat_scene_with_opening(), parent_revision_id=None
    ).revision

    door_closed = build_operating_state(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        name='Door closed',
        observed_at_utc='2026-09-25T00:00:00+00:00',
        openings=(OperatingOpeningState(opening_id='door-1', is_open=False),),
        curtains=(
            CurtainState(
                curtain_id='curtain-1',
                label='Side curtain',
                coverage_fraction=0.8,
            ),
        ),
        hvac=HvacState(is_on=False),
    )
    door_open = build_operating_state(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        name='Door open',
        observed_at_utc='2026-09-25T00:00:00+00:00',
        openings=(OperatingOpeningState(opening_id='door-1', is_open=True),),
    )

    legacy = rectangular_geometry_request_identity(revision, 'point-mlp')
    closed_identity = rectangular_geometry_request_identity(
        revision, 'point-mlp', operating_state=door_closed
    )
    open_identity = rectangular_geometry_request_identity(
        revision, 'point-mlp', operating_state=door_open
    )

    # Two states on one unchanged SceneRevision produce distinct exact
    # identities — and neither equals the unbound request.
    assert len(
        {legacy.input_hash, closed_identity.input_hash,
         open_identity.input_hash}
    ) == 3

    snapshot = loads(closed_identity.input_snapshot_json)
    block = snapshot['room_operating_state']
    assert block['authority']['state_id'] == door_closed.state_id
    consumption = block['consumption']
    # The declared door binds onto wall topology — a real portal/topology
    # input, not metadata-only. Curtain coverage stays UNKNOWN without a
    # material authority; HVAC never alters deterministic transfer.
    opening = consumption['openings'][0]
    assert opening['opening_id'] == 'door-1'
    assert opening['wall_opening_bound'] is True
    assert opening['effect'] == 'portal_topology'
    assert opening['persisted_is_open'] is False
    assert consumption['consumed_domains'] == ['openings']
    assert consumption['curtain_effect'] == (
        'unknown_effect_no_material_authority'
    )
    assert consumption['hvac_effect'] == 'no_transfer_change'
    assert 'room_operating_state' not in loads(legacy.input_snapshot_json)

    # Replay revalidates the sealed state authority against parameters.
    verified = verify_prediction_input(
        revision,
        model_id=closed_identity.model_id,
        model_version=closed_identity.model_version,
        parameters_json=closed_identity.parameters_json,
        input_snapshot_json=closed_identity.input_snapshot_json,
        input_hash=closed_identity.input_hash,
        geometry_compatibility=closed_identity.geometry_compatibility,
    )
    assert verified.input_hash == closed_identity.input_hash

    # A state pinned to a different revision fails closed.
    stale_state = build_operating_state(
        document_id=revision.document_id,
        scene_revision_id='other-revision',
        scene_content_hash=revision.content_hash,
        name='stale',
        observed_at_utc='2026-09-25T00:00:00+00:00',
    )
    import pytest

    with pytest.raises(ValueError, match='SceneRevision'):
        rectangular_geometry_request_identity(
            revision, 'point-mlp', operating_state=stale_state
        )
